"""Opt-in evaluation adapter; not imported by Juli's production runtime.

Pricing checked 2026-10-02: peak cache-miss input CNY 2/M, output CNY 8/M.
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
The requested legacy name may be routed to a newer Flash model by DeepSeek.
No credentials/environment lookup, retry, or ledger-reset operation is provided.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time

import httpx

from juli.adapters.chat_completions import ChatCompletionsProvider, ChatModelConfig
from juli.core.common import DomainError, canonical_json
from juli.core.models import ModelReply, ModelRequest


ENDPOINT = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-v4-flash'
WINDOW = 64000
MAX_OUTPUT = 4096
MAX_BODY = 1024 * 1024
DEFAULT_CONFIG = ChatModelConfig(endpoint=ENDPOINT, model=MODEL, context_window=WINDOW, timeout_seconds=120)


def _money(micros: int) -> str:
    return format(Decimal(micros) / 1000000, '.6f')


class BatchBudget:
    """Durable batch-wide ceiling using integer micro-CNY and serialized writes.

    A pending or unknown call blocks every later call, even in another process.
    Successful usage is costed at peak cache-miss rates, not presented as a bill.
    Keep this directory for the entire batch. Do not start a fresh ledger to retry.
    """

    def __init__(self, root: Path, max_cny='10', max_calls=36):
        try:
            amount = Decimal(str(max_cny)) * 1000000
            valid = amount.is_finite() and 0 < amount <= 10000000 and amount == amount.to_integral_value()
        except (InvalidOperation, ValueError):
            valid = False
        if not valid or type(max_calls) is not int or not 1 <= max_calls <= 36:
            raise DomainError('invalid_probe_budget', 'Use at most CNY 10 and 36 calls.')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'budget.sqlite3'
        self.max_micros, self.max_calls = int(amount), max_calls
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER NOT NULL, max_calls INTEGER NOT NULL, version INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, status TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER, input_tokens INTEGER, output_tokens INTEGER)')
            settings = db.execute('SELECT ceiling,max_calls,version FROM settings WHERE id=1').fetchone()
            if settings is None:
                db.execute('INSERT INTO settings VALUES (1,?,?,1)', (self.max_micros, max_calls))
            elif settings != (self.max_micros, max_calls, 1):
                raise DomainError('probe_budget_mismatch', 'Existing batch budget cannot be reset or reconfigured.')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def snapshot(self):
        with self._transaction() as db:
            rows = db.execute('SELECT id,status,reserved,charged,input_tokens,output_tokens FROM calls ORDER BY id').fetchall()
        committed = sum(row[3] or 0 for row in rows if row[1] == 'completed')
        reserved = sum(row[2] for row in rows if row[1] != 'completed')
        return {'ceiling_cny': _money(self.max_micros), 'max_calls': self.max_calls,
                'calls': len(rows), 'committed_cny': _money(committed), 'reserved_cny': _money(reserved),
                'remaining_cny': _money(self.max_micros - committed - reserved),
                'stopped': any(row[1] != 'completed' for row in rows),
                'cost_basis': 'peak_cache_miss_upper_bound_not_actual_bill',
                'entries': [{'call_id': r[0], 'status': r[1], 'reserved_cny': _money(r[2]),
                             'charged_upper_bound_cny': _money(r[3]) if r[3] is not None else None,
                             'input_tokens': r[4], 'output_tokens': r[5]} for r in rows]}

    def reserve(self, max_output_tokens: int) -> int:
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT:
            raise DomainError('probe_output_limit', 'Evaluation output limit is 4096 tokens.')
        cost = WINDOW * 2 + max_output_tokens * 8
        with self._transaction() as db:
            rows = db.execute('SELECT status,reserved,charged FROM calls').fetchall()
            if any(r[0] != 'completed' for r in rows):
                raise DomainError('probe_batch_stopped', 'A reserved or unknown call blocks further requests; no retry is allowed.')
            if len(rows) >= self.max_calls or sum(r[2] for r in rows) + cost > self.max_micros:
                raise DomainError('probe_budget_exhausted', 'The batch call or conservative CNY ceiling would be exceeded.')
            return db.execute('INSERT INTO calls(status,reserved) VALUES (?,?)', ('reserved', cost)).lastrowid

    def settle(self, call_id: int, reply: ModelReply, max_output_tokens: int):
        usage = reply.usage
        if usage.input_tokens > WINDOW or usage.output_tokens > max_output_tokens:
            raise DomainError('probe_usage_exceeded', 'Reported usage exceeded the reservation; batch must stop.')
        cost = usage.input_tokens * 2 + usage.output_tokens * 8
        with self._transaction() as db:
            row = db.execute('SELECT status,reserved FROM calls WHERE id=?', (call_id,)).fetchone()
            if row is None or row[0] != 'reserved' or cost > row[1]:
                raise DomainError('probe_budget_state', 'Only a matching reserved call can be settled once.')
            db.execute("UPDATE calls SET status='completed',charged=?,input_tokens=?,output_tokens=? WHERE id=?",
                       (cost, usage.input_tokens, usage.output_tokens, call_id))

    def unknown(self, call_id: int):
        with self._transaction() as db:
            db.execute("UPDATE calls SET status='unknown' WHERE id=? AND status='reserved'", (call_id,))


class _CaptureStream(httpx.SyncByteStream):
    def __init__(self, stream, capture):
        self.stream, self.capture = stream, capture

    def __iter__(self):
        try:
            for chunk in self.stream:
                self.capture.add(chunk)
                yield chunk
        finally:
            self.capture.save()

    def close(self):
        try:
            self.stream.close()
        finally:
            self.capture.save()


class _CaptureTransport(httpx.BaseTransport):
    def __init__(self, transport, directory, secret):
        self.transport, self.directory, self.secret = transport, directory, secret
        self.data = bytearray()
        self.status = None
        self.truncated = False
        self.saved = False

    def handle_request(self, request):
        # Capture the uncompressed body without reading ahead of the base adapter.
        request.headers['Accept-Encoding'] = 'identity'
        response = self.transport.handle_request(request)
        self.status = response.status_code
        if self.status == 200:
            # A fresh response also makes MockTransport's preloaded ByteStream
            # follow exactly the same bounded read path as a live response.
            response = httpx.Response(response.status_code, headers=response.headers,
                                      stream=_CaptureStream(response.stream, self), extensions=response.extensions)
        return response

    def add(self, chunk):
        available = MAX_BODY - len(self.data)
        self.data.extend(chunk[:available])
        self.truncated = self.truncated or len(chunk) > available

    def save(self):
        if self.status == 200 and not self.saved:
            _write_bytes(self.directory / 'response.raw.json', bytes(self.data), self.secret)
            self.saved = True

    def close(self):
        try:
            self.transport.close()
        finally:
            self.save()


def _write_bytes(path, data, secret):
    # Also cover a key that JSON serialization escaped. Replace after buffering so
    # a secret split over HTTP chunks cannot leak into a trace.
    for value in {secret.encode('ascii'), json.dumps(secret)[1:-1].encode('ascii')}:
        # Eight bytes never expands even the shortest accepted credential, so a
        # bounded response remains bounded after redaction.
        data = data.replace(value, b'[HIDDEN]')
    with path.open('xb') as target:
        target.write(data)


def _write_json(path, value, secret):
    _write_bytes(path, (canonical_json(value) + '\n').encode('utf-8'), secret)


class DeepSeekProbeProvider(ChatCompletionsProvider):
    def __init__(self, config=DEFAULT_CONFIG, *, api_key: str, trace_directory: Path,
                 budget: BatchBudget, transport=None):
        if (config.endpoint, config.model, config.context_window) != (ENDPOINT, MODEL, WINDOW):
            raise DomainError('invalid_probe_config', 'This evaluation is restricted to the approved DeepSeek endpoint, model and 64000-token window.')
        super().__init__(config, api_key=api_key, approved_endpoint=ENDPOINT, transport=None)
        self.trace_directory, self.budget = Path(trace_directory), budget
        self._injected_transport = transport

    def _payload(self, request):
        payload, aliases = super()._payload(request)
        payload['max_tokens'] = payload.pop('max_completion_tokens')
        payload['thinking'] = {'type': 'disabled'}
        payload.pop('store')
        payload.pop('n')
        return payload, aliases

    def complete(self, request: ModelRequest) -> ModelReply:
        request = ModelRequest.model_validate(request)
        payload, _ = self._payload(request)
        wire = canonical_json(payload)
        if self._api_key in wire or json.dumps(self._api_key)[1:-1] in wire:
            raise DomainError('probe_secret_in_input', 'A credential was detected in the model input; no request was sent.')
        # A byte ceiling stricter than the parent 256 KiB limit keeps this tiny
        # smoke batch below the declared input reservation without token estimates.
        if len(wire.encode('utf-8')) > WINDOW:
            raise DomainError('probe_input_limit', 'Evaluation request exceeds its conservative 64000-byte limit.')
        call_id = self.budget.reserve(request.max_output_tokens)
        directory = self.trace_directory / f'call-{call_id:04d}'
        capture = None
        started = time.monotonic()
        try:
            directory.mkdir(parents=True, exist_ok=False)
            _write_json(directory / 'request.json', request.model_dump(mode='json'), self._api_key)
            capture = _CaptureTransport(self._injected_transport or httpx.HTTPTransport(retries=0), directory, self._api_key)
            self._transport = capture
            reply = super().complete(request)
            serialized = reply.model_dump_json()
            if self._api_key in serialized or json.dumps(self._api_key)[1:-1] in serialized:
                raise DomainError('probe_secret_in_reply', 'A credential echo was redacted; the batch was stopped.')
            _write_json(directory / 'reply.json', reply.model_dump(mode='json'), self._api_key)
            _write_json(directory / 'trace.json', {'call_id': call_id, 'status': 'response_received',
                        'requested_model': self.config.model, 'thinking': 'disabled',
                        'http_status': capture.status, 'raw_body_truncated': capture.truncated,
                        'elapsed_seconds': round(time.monotonic() - started, 6)}, self._api_key)
            self.budget.settle(call_id, reply, request.max_output_tokens)
            return reply
        except BaseException as exc:
            self.budget.unknown(call_id)
            # Do not serialize exception messages, request headers or HTTP errors.
            if directory.is_dir() and not (directory / 'failure.json').exists():
                _write_json(directory / 'failure.json', {'call_id': call_id, 'status': 'unknown',
                            'error_code': exc.code if isinstance(exc, DomainError) else 'probe_internal_error',
                            'http_status': capture.status if capture else None,
                            'raw_body_truncated': capture.truncated if capture else False}, self._api_key)
            if isinstance(exc, DomainError):
                raise
            raise DomainError('probe_internal_error', 'Evaluation stopped; the reservation is retained and no retry was made.') from None
        finally:
            self._transport = None
