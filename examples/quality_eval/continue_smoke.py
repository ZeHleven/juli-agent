"""Explicit E03 trials; retain E02 unknown reservations within one CNY 5 ceiling.

Default execution is read-only: no key lookup, network call, or ledger creation.
The source ledger is never repaired, settled, released, or otherwise written.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from juli.core.common import DomainError

from .smoke import ROOT, CASES, preflight, run_case, wait_for_answer, emit, save_json

SOURCE = ROOT / '.local/quality-eval/v1/e02-deepseek-flash-20261002/budget/budget.sqlite3'
BATCH = ROOT / '.local/quality-eval/v1/e03-deepseek-flash-20261002'
TOTAL_MICROS = 5_000_000
TOTAL_CALLS = 36
MANIFEST = 'inheritance.json'
STATE = 'budget-state.json'


def fail(message):
    raise DomainError('continuation_budget_invalid', message)


def money(value):
    return format(Decimal(value) / 1_000_000, '.6f')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ordinary(path, *, required=True):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            fail('Linked evaluation paths are not permitted.')
    if required and not path.is_file():
        fail('Required evaluation state is missing; do not create a fresh ledger to retry.')
    return path


def read_ledger(path):
    """Read only a fully validated v1 ledger with no SQLite sidecar state."""
    path = ordinary(path)
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        fail('Source or child SQLite sidecar state requires operator review.')
    before = sha(path)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if [(row[0], row[1]) for row in objects] != [('table', 'calls'), ('table', 'settings')]:
                fail('Unknown budget schema objects.')
            known_sql = {
                'settings': 'CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER NOT NULL, max_calls INTEGER NOT NULL, version INTEGER NOT NULL)',
                'calls': 'CREATE TABLE calls (id INTEGER PRIMARY KEY, status TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER, input_tokens INTEGER, output_tokens INTEGER)',
            }
            for _, name, sql in objects:
                if ' '.join(sql.split()).lower() != known_sql[name].lower():
                    fail('Unknown budget table definition.')
            expected = {
                'settings': [('id', 'INTEGER', 0, None, 1), ('ceiling', 'INTEGER', 1, None, 0),
                             ('max_calls', 'INTEGER', 1, None, 0), ('version', 'INTEGER', 1, None, 0)],
                'calls': [('id', 'INTEGER', 0, None, 1), ('status', 'TEXT', 1, None, 0),
                          ('reserved', 'INTEGER', 1, None, 0), ('charged', 'INTEGER', 0, None, 0),
                          ('input_tokens', 'INTEGER', 0, None, 0), ('output_tokens', 'INTEGER', 0, None, 0)],
            }
            for name, columns in expected.items():
                if [tuple(row[1:]) for row in db.execute(f'PRAGMA table_info({name})')] != columns:
                    fail('Unknown budget column schema.')
            settings = db.execute('SELECT id,ceiling,max_calls,version FROM settings').fetchall()
            if len(settings) != 1:
                fail('Budget must contain exactly one settings record.')
            identity, ceiling, max_calls, version = settings[0]
            if (any(type(v) is not int for v in settings[0]) or identity != 1 or version != 1
                    or not 0 < ceiling <= TOTAL_MICROS or not 1 <= max_calls <= TOTAL_CALLS):
                fail('Budget version or limits do not match the CNY 5 authorization.')
            rows = db.execute('SELECT id,status,reserved,charged,input_tokens,output_tokens FROM calls ORDER BY id').fetchall()
    except sqlite3.DatabaseError:
        fail('Budget is not a readable supported SQLite ledger.')
    if sha(path) != before:
        fail('Ledger changed during read-only inspection.')
    committed = reserved = 0
    for index, (call_id, status, allocation, charged, input_tokens, output_tokens) in enumerate(rows, 1):
        if (type(call_id) is not int or call_id != index or status not in ('completed', 'reserved', 'unknown')
                or type(allocation) is not int or not 128008 <= allocation <= 160768
                or (allocation - 128000) % 8):
            fail('Invalid budget call record.')
        if status == 'completed':
            if (any(type(v) is not int for v in (charged, input_tokens, output_tokens))
                    or not 0 <= input_tokens <= 64000 or not 0 <= output_tokens <= (allocation - 128000) // 8
                    or charged != input_tokens * 2 + output_tokens * 8 or not 0 <= charged <= allocation):
                fail('Invalid completed call usage or charge.')
            committed += charged
        else:
            if any(v is not None for v in (charged, input_tokens, output_tokens)):
                fail('Unresolved calls cannot carry an assumed settled charge.')
            reserved += allocation
    if len(rows) > max_calls or committed + reserved > ceiling:
        fail('Existing budget exceeds its authorized limits.')
    return {'sha256': before, 'ceiling_micros': ceiling, 'max_calls': max_calls,
            'calls': len(rows), 'committed_micros': committed, 'reserved_micros': reserved,
            'stopped': any(row[1] != 'completed' for row in rows)}


def inheritance(source):
    source = ordinary(source)
    old = read_ledger(source)
    if old['ceiling_micros'] != TOTAL_MICROS or old['max_calls'] != TOTAL_CALLS:
        fail('E02 must retain its original CNY 5 and 36-call settings.')
    remaining = TOTAL_MICROS - old['committed_micros'] - old['reserved_micros']
    calls = TOTAL_CALLS - old['calls']
    if remaining <= 0 or calls <= 0:
        fail('No cumulative authorization remains for E03.')
    return {'version': 1, 'round_id': 'e03', 'source_round_id': 'e02',
            'source_ledger': 'e02-deepseek-flash-20261002/budget/budget.sqlite3',
            'source_ledger_sha256': old['sha256'], 'total_ceiling_cny': money(TOTAL_MICROS),
            'source_committed_upper_bound_cny': money(old['committed_micros']),
            'source_unresolved_reserved_cny': money(old['reserved_micros']),
            'source_calls': old['calls'], 'child_ceiling_cny': money(remaining),
            'child_max_calls': calls, 'total_max_calls': TOTAL_CALLS,
            'user_approved_new_trial': True, 'approved_cases': list(CASES),
            'trials': {'career-01': 2, 'career-02': 1, 'tender-03': 1},
            'cost_basis': 'conservative_upper_bounds_not_actual_bill',
            'unresolved_source_calls_released': False}


def read_json(path):
    path = ordinary(path)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        fail('Evaluation state is not valid JSON.')


def verify_existing(source, batch, expected):
    if inheritance(source) != expected or read_json(batch / MANIFEST) != expected:
        fail('Source ledger or fixed inheritance manifest changed.')
    current = read_ledger(batch / 'budget/budget.sqlite3')
    state = read_json(batch / STATE)
    if state != current:
        fail('Child ledger changed outside the continuation runner; no reset is allowed.')
    if (money(current['ceiling_micros']) != expected['child_ceiling_cny']
            or current['max_calls'] != expected['child_max_calls']):
        fail('Child limits do not match the inherited authorization.')
    return current


@contextmanager
def locked(batch):
    path = batch / '.budget-operation.lock'
    try:
        with path.open('x', encoding='utf-8'):
            pass
    except FileExistsError:
        fail('Another operation or interrupted operation holds the budget lock.')
    try:
        yield
    finally:
        path.unlink()


def save_state(batch):
    target = batch / STATE
    temporary = batch / 'budget-state.next.json'
    save_json(temporary, read_ledger(batch / 'budget/budget.sqlite3'))
    temporary.replace(target)


class ContinuationBudget:
    """A shared child ledger guarded by fixed inheritance and durable fingerprints."""

    def __init__(self, source, batch, manifest):
        self.source, self.batch, self.manifest = Path(source), Path(batch), manifest

    def snapshot(self):
        with locked(self.batch):
            state = verify_existing(self.source, self.batch, self.manifest)
        return {'ceiling_cny': money(state['ceiling_micros']), 'max_calls': state['max_calls'],
                'calls': state['calls'], 'committed_cny': money(state['committed_micros']),
                'reserved_cny': money(state['reserved_micros']),
                'remaining_cny': money(state['ceiling_micros'] - state['committed_micros'] - state['reserved_micros']),
                'stopped': state['stopped'], 'inherited_source_reserved_cny': self.manifest['source_unresolved_reserved_cny'],
                'total_ceiling_cny': '5.000000', 'cost_basis': self.manifest['cost_basis']}

    def _operate(self, operation, *args):
        from .deepseek_probe import BatchBudget
        with locked(self.batch):
            verify_existing(self.source, self.batch, self.manifest)
            budget = BatchBudget(self.batch / 'budget', max_cny=self.manifest['child_ceiling_cny'],
                                 max_calls=self.manifest['child_max_calls'])
            try:
                return getattr(budget, operation)(*args)
            finally:
                save_state(self.batch)

    def reserve(self, max_output_tokens):
        return self._operate('reserve', max_output_tokens)

    def settle(self, call_id, reply, max_output_tokens):
        return self._operate('settle', call_id, reply, max_output_tokens)

    def unknown(self, call_id):
        return self._operate('unknown', call_id)


def open_continuation_budget(source=SOURCE, batch=BATCH):
    """Create once after explicit live authorization, or validate and reopen unchanged."""
    source, batch = ordinary(source), ordinary(batch, required=False)
    expected = inheritance(source)
    if batch.exists():
        with locked(batch):
            verify_existing(source, batch, expected)
    else:
        batch.mkdir(parents=True, exist_ok=False)
        with locked(batch):
            save_json(batch / MANIFEST, expected)
            from .deepseek_probe import BatchBudget
            BatchBudget(batch / 'budget', max_cny=expected['child_ceiling_cny'],
                        max_calls=expected['child_max_calls'])
            save_state(batch)
    return ContinuationBudget(source, batch, expected)


def preflight_continuation(*, repo=ROOT, source=SOURCE, batch=BATCH, case_id='career-01'):
    """Inspect the frozen case, old ledger, and any child state; never create files."""
    case = preflight(repo=Path(repo), case_id=case_id)
    batch = ordinary(batch, required=False)
    expected = inheritance(source)
    if (batch / case_id).exists() or (batch / (case_id + '-model-trace')).exists():
        fail('This E03 case already has artifacts; no implicit rerun is permitted.')
    current = verify_existing(source, batch, expected) if batch.exists() else None
    if current and current['stopped']:
        fail('The E03 ledger has a reserved or unknown call; further cases are stopped.')
    return case, {'status': 'preflight_passed', 'round_id': 'e03', 'case_id': case_id,
                  'approved_batch': list(CASES), 'credentials_read': False, 'model_calls': 0,
                  'inheritance': expected, 'existing_child_ledger': current}


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, default='career-01')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--api-key-file', type=Path)
    args = parser.parse_args(argv)
    case, summary = preflight_continuation(case_id=args.case)
    if not args.live:
        emit(summary)
        return 0
    if args.api_key_file is None:
        parser.error('--live requires an explicitly supplied local --api-key-file')
    budget = open_continuation_budget()
    if budget.snapshot()['stopped']:
        fail('The E03 ledger is stopped; no credential was read.')
    key = args.api_key_file.read_text(encoding='utf-8-sig').strip()
    if not key or any(c.isspace() for c in key) or key.startswith(('"', "'")):
        parser.error('The key file must contain one nonempty credential without quotes')
    from .deepseek_probe import DeepSeekProbeProvider
    trace = BATCH / (args.case + '-model-trace')
    trace.mkdir(parents=True, exist_ok=False)
    provider = DeepSeekProbeProvider(api_key=key, trace_directory=trace, budget=budget)
    try:
        outcome, error = run_case(case, BATCH / args.case, provider,
                                  answer_callback=wait_for_answer, profile='e03-multitool-v1',
                                  trial_number=summary['inheritance']['trials'][args.case], round_id='e03')
    finally:
        emit({'event': 'batch_budget', 'budget': budget.snapshot()})
    return 0 if outcome['state']['status'] == 'completed' and error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
