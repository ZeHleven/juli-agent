"""Offline E03 inheritance checks: synthetic ledgers, no key access or network."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from examples.quality_eval import continue_smoke as continuation
from examples.quality_eval.deepseek_probe import BatchBudget
from juli.core.common import DomainError
from juli.core.models import ModelReply, ModelUsage


REPLY = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=10, output_tokens=5))


@pytest.fixture
def ledgers(tmp_path):
    old = BatchBudget(tmp_path / 'e02/budget', max_cny='5', max_calls=36)
    old.unknown(old.reserve(4096))
    source = old.path
    return source, tmp_path / 'e03'


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rejected(call, text):
    with pytest.raises(DomainError, match=text) as error:
        call()
    assert error.value.code == 'continuation_budget_invalid'


def test_preflight_reads_no_credential_and_creates_no_state(ledgers, tmp_path, monkeypatch, capsys):
    source, batch = ledgers
    original_preflight = continuation.preflight_continuation
    monkeypatch.setattr(continuation, 'preflight_continuation',
                        lambda **kwargs: original_preflight(source=source, batch=batch, **kwargs))
    key = tmp_path / 'must-not-read-key'
    read_text = Path.read_text

    def guarded(path, *args, **kwargs):
        if path == key:
            raise AssertionError('Default preflight read credentials')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded)
    before = source.read_bytes()
    assert continuation.main(['--api-key-file', str(key)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['credentials_read'] is False and summary['model_calls'] == 0
    assert summary['inheritance']['child_ceiling_cny'] == '4.839232'
    assert summary['inheritance']['child_max_calls'] == 35
    assert summary['approved_batch'] == ['career-01', 'career-02', 'tender-03']
    assert not batch.exists() and source.read_bytes() == before


def test_child_keeps_unknown_source_reserved_and_preserves_source_bytes(ledgers):
    source, batch = ledgers
    before = source.read_bytes()
    budget = continuation.open_continuation_budget(source, batch)
    manifest = json.loads((batch / 'inheritance.json').read_text())
    assert manifest['source_ledger_sha256'] == fingerprint(source)
    assert manifest['source_unresolved_reserved_cny'] == '0.160768'
    assert manifest['source_committed_upper_bound_cny'] == '0.000000'
    assert manifest['user_approved_new_trial'] is True
    assert manifest['unresolved_source_calls_released'] is False
    budget.settle(budget.reserve(4096), REPLY, 4096)
    assert budget.snapshot()['committed_cny'] == '0.000060'
    assert budget.snapshot()['remaining_cny'] == '4.839172'
    assert source.read_bytes() == before
    old = continuation.read_ledger(source)
    assert old['stopped'] and old['reserved_micros'] == 160768


def test_different_cases_and_processes_share_one_accumulating_ledger(ledgers):
    source, batch = ledgers
    first = continuation.open_continuation_budget(source, batch)
    first.settle(first.reserve(4096), REPLY, 4096)
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.continue_smoke import open_continuation_budget
from juli.core.models import ModelReply, ModelUsage
budget = open_continuation_budget(Path(sys.argv[1]), Path(sys.argv[2]))
assert budget.snapshot()['calls'] == 1
budget.settle(budget.reserve(4096), ModelReply(text='synthetic', usage=ModelUsage(input_tokens=20, output_tokens=10)), 4096)
print(budget.snapshot()['committed_cny'])
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == '0.000180'
    for case in ('career-02', 'tender-03'):
        _, summary = continuation.preflight_continuation(source=source, batch=batch, case_id=case)
        assert summary['existing_child_ledger']['calls'] == 2
        assert summary['existing_child_ledger']['committed_micros'] == 180
    assert first.snapshot()['calls'] == 2


def test_unknown_child_blocks_next_case_and_another_process(ledgers):
    source, batch = ledgers
    budget = continuation.open_continuation_budget(source, batch)
    budget.unknown(budget.reserve(4096))
    rejected(lambda: continuation.preflight_continuation(source=source, batch=batch, case_id='career-02'), 'stopped')
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.continue_smoke import open_continuation_budget
from juli.core.common import DomainError
try:
    open_continuation_budget(Path(sys.argv[1]), Path(sys.argv[2])).reserve(4096)
except DomainError as error:
    print(error.code)
else:
    raise AssertionError('Pending reservation was bypassed')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'probe_batch_stopped'
    assert budget.snapshot()['calls'] == 1


@pytest.mark.parametrize('state', ['budget/budget.sqlite3', 'budget-state.json', 'inheritance.json'])
def test_missing_persistent_state_never_reinitializes_the_child(ledgers, state):
    source, batch = ledgers
    continuation.open_continuation_budget(source, batch)
    path = batch / state
    path.unlink()
    rejected(lambda: continuation.open_continuation_budget(source, batch), 'missing')
    assert not path.exists()


def test_existing_child_ledger_cannot_be_replaced_by_fresh_empty_budget(ledgers):
    source, batch = ledgers
    child = continuation.open_continuation_budget(source, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    (batch / 'budget/budget.sqlite3').unlink()
    BatchBudget(batch / 'budget', max_cny='4.839232', max_calls=35)
    rejected(lambda: continuation.open_continuation_budget(source, batch), 'outside')


def test_manifest_tampering_and_new_source_hash_fail_before_reservation(ledgers):
    source, batch = ledgers
    child = continuation.open_continuation_budget(source, batch)
    path = batch / 'inheritance.json'
    original = path.read_bytes()
    manifest = json.loads(original)
    manifest['child_ceiling_cny'] = '5.000000'
    path.write_text(json.dumps(manifest))
    rejected(lambda: child.reserve(4096), 'manifest changed')
    path.write_bytes(original)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE calls SET status='reserved' WHERE id=1")
    rejected(lambda: child.reserve(4096), 'ledger or fixed')
    assert continuation.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 0


@pytest.mark.parametrize('name', ['career-01', 'career-01-model-trace'])
def test_existing_case_or_trace_rejects_implicit_rerun(ledgers, name):
    source, batch = ledgers
    continuation.open_continuation_budget(source, batch)
    (batch / name).mkdir()
    rejected(lambda: continuation.preflight_continuation(source=source, batch=batch), 'already has artifacts')


@pytest.mark.parametrize('statement', [
    'CREATE TABLE surprise (value TEXT)',
    'ALTER TABLE calls ADD COLUMN surprise TEXT',
    'UPDATE settings SET version=2',
    'UPDATE settings SET ceiling=5000001',
    "UPDATE calls SET status='forgiven'",
    'UPDATE calls SET charged=0',
    'UPDATE calls SET reserved=0',
])
def test_unknown_source_schema_or_invalid_state_is_rejected(ledgers, statement):
    source, batch = ledgers
    with sqlite3.connect(source) as db:
        db.execute(statement)
    with pytest.raises(DomainError):
        continuation.open_continuation_budget(source, batch)
    assert not batch.exists()


def test_committed_source_usage_is_also_deducted(tmp_path):
    old = BatchBudget(tmp_path / 'source', max_cny='5')
    old.settle(old.reserve(4096), REPLY, 4096)
    old.unknown(old.reserve(4096))
    manifest = continuation.inheritance(old.path)
    assert manifest['child_ceiling_cny'] == '4.839172'
    assert manifest['source_committed_upper_bound_cny'] == '0.000060'
    assert manifest['child_max_calls'] == 34


def test_identical_evidence_can_move_to_independent_repository(ledgers, tmp_path):
    source, batch = ledgers
    child = continuation.open_continuation_budget(source, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    relocated = tmp_path / 'independent-repo'
    relocated.mkdir()
    copied_source = relocated / 'e02/budget/budget.sqlite3'
    copied_source.parent.mkdir(parents=True)
    shutil.copyfile(source, copied_source)
    copied_batch = relocated / 'e03'
    shutil.copytree(batch, copied_batch)
    reopened = continuation.open_continuation_budget(copied_source, copied_batch)
    assert reopened.snapshot()['calls'] == 1
    assert reopened.snapshot()['committed_cny'] == '0.000060'
    assert (copied_batch / 'inheritance.json').read_bytes() == (batch / 'inheritance.json').read_bytes()


def test_full_child_cost_cannot_bypass_cumulative_five_yuan_cap(ledgers):
    source, batch = ledgers
    child = continuation.open_continuation_budget(source, batch)
    worst = ModelReply(text='synthetic maximum', usage=ModelUsage(input_tokens=64000, output_tokens=4096))
    for _ in range(30):
        child.settle(child.reserve(4096), worst, 4096)
    assert child.snapshot()['committed_cny'] == '4.823040'
    with pytest.raises(DomainError) as error:
        child.reserve(4096)
    assert error.value.code == 'probe_budget_exhausted'
    assert child.snapshot()['calls'] == 30


def test_source_sidecar_and_interrupted_lock_are_not_silently_removed(ledgers):
    source, batch = ledgers
    sidecar = Path(str(source) + '-wal')
    sidecar.write_bytes(b'synthetic sidecar')
    rejected(lambda: continuation.open_continuation_budget(source, batch), 'sidecar')
    assert sidecar.exists()
    sidecar.unlink()
    continuation.open_continuation_budget(source, batch)
    lock = batch / '.budget-operation.lock'
    lock.write_text('synthetic interrupted operation')
    rejected(lambda: continuation.open_continuation_budget(source, batch), 'lock')
    assert lock.read_text() == 'synthetic interrupted operation'
