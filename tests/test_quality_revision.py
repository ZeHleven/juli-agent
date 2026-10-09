"""E04 budget-chain and entrypoint checks with synthetic ledgers, never network."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from examples.quality_eval import continue_smoke as previous
from examples.quality_eval import revision_smoke as revision
from examples.quality_eval import deepseek_probe
from examples.quality_eval.deepseek_probe import BatchBudget
from juli.core.common import DomainError
from juli.core.models import ModelReply, ModelUsage


REPLY = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=10, output_tokens=5))


@pytest.fixture
def chain(tmp_path):
    old = BatchBudget(tmp_path / 'e02/budget', max_cny='5', max_calls=36)
    old.unknown(old.reserve(4096))
    prior = tmp_path / 'e03'
    recent = previous.open_continuation_budget(old.path, prior)
    for _ in range(8):
        recent.settle(recent.reserve(4096), REPLY, 4096)
    # 8 * 60 + (59902 * 2 + 1 * 8) = 120292 micro-CNY, nine calls.
    last = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=59902, output_tokens=1))
    recent.settle(recent.reserve(4096), last, 4096)
    return old.path, prior, tmp_path / 'e04'


def rejected(call, match):
    with pytest.raises(DomainError, match=match):
        call()


def hashes(source, prior):
    return revision.inheritance(source, prior)['source_sha256']


def configure_main(monkeypatch, chain):
    source, prior, batch = chain
    monkeypatch.setattr(revision, 'SOURCE', source)
    monkeypatch.setattr(revision, 'PREVIOUS', prior)
    monkeypatch.setattr(revision, 'BATCH', batch)
    monkeypatch.setattr(revision, 'APPROVED_HASHES', hashes(source, prior))

    def forbidden(*args, **kwargs):
        raise AssertionError('No provider or task should run')

    monkeypatch.setattr(deepseek_probe, 'DeepSeekProbeProvider', forbidden)
    monkeypatch.setattr(revision, 'run_case', forbidden)


def test_preflight_reads_no_key_and_writes_nothing(chain, monkeypatch, capsys):
    configure_main(monkeypatch, chain)
    source, prior, batch = chain
    key = batch.parent / 'must-not-read-key'
    original_read = Path.read_text

    def guarded(path, *args, **kwargs):
        if path == key:
            raise AssertionError('Preflight attempted credential access')
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded)
    before = {p: p.read_bytes() for p in batch.parent.rglob('*') if p.is_file()}
    assert revision.main(['--api-key-file', str(key)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['credentials_read'] is False and summary['model_calls'] == 0
    assert summary['round_id'] == 'e04'
    manifest = summary['inheritance']
    assert manifest['source_calls'] == 10
    assert manifest['source_unresolved_reserved_cny'] == '0.160768'
    assert manifest['source_committed_upper_bound_cny'] == '0.120292'
    assert manifest['child_ceiling_cny'] == '4.718940'
    assert manifest['child_max_calls'] == 26
    assert manifest['trials'] == {'career-01': 3, 'career-02': 2, 'tender-03': 2}
    assert before == {p: p.read_bytes() for p in batch.parent.rglob('*') if p.is_file()}


@pytest.mark.parametrize('credential', [None, 'missing', '', '"synthetic-secret"', 'two tokens', 'short'])
def test_missing_or_invalid_key_makes_no_artifacts_or_calls(chain, monkeypatch, credential):
    configure_main(monkeypatch, chain)
    source, prior, batch = chain
    args = ['--live']
    if credential is not None:
        key = batch.parent / 'synthetic-key'
        if credential != 'missing':
            key.write_text(credential, encoding='utf-8')
        args.extend(['--api-key-file', str(key)])
    with pytest.raises(SystemExit) as error:
        revision.main(args)
    assert error.value.code == 2
    assert not batch.exists() and not revision.marker_path(batch).exists()


def test_both_source_ledgers_unchanged_and_remaining_authorization_accumulates(chain):
    source, prior, batch = chain
    original = {p: p.read_bytes() for p in (source, prior / 'budget/budget.sqlite3',
                                           prior / 'inheritance.json', prior / 'budget-state.json')}
    child = revision.open_revision_budget(source, prior, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    state = child.snapshot()
    assert state['committed_cny'] == '0.000060'
    assert state['remaining_cny'] == '4.718880'
    assert state['inherited_source_calls'] == 10
    assert state['inherited_source_reserved_cny'] == '0.160768'
    assert state['inherited_source_committed_cny'] == '0.120292'
    assert state['total_ceiling_cny'] == '5.000000'
    assert all(path.read_bytes() == value for path, value in original.items())


def test_another_process_reopens_same_ledger_and_increments_cost(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.revision_smoke import open_revision_budget
from juli.core.models import ModelReply, ModelUsage
b = open_revision_budget(*(Path(p) for p in sys.argv[1:]))
assert b.snapshot()['calls'] == 1
b.settle(b.reserve(4096), ModelReply(text='synthetic', usage=ModelUsage(input_tokens=20, output_tokens=10)), 4096)
print(b.snapshot()['committed_cny'])
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(prior), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == '0.000180'
    _, summary = revision.preflight_revision(source=source, prior=prior, batch=batch, case_id='career-02')
    assert summary['existing_child_ledger']['calls'] == 2
    assert child.snapshot()['calls'] == 2


def test_unknown_child_stops_next_process_and_case_without_retry(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    child.unknown(child.reserve(4096))
    rejected(lambda: revision.preflight_revision(source=source, prior=prior, batch=batch), 'stopped')
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.revision_smoke import open_revision_budget
from juli.core.common import DomainError
try:
    open_revision_budget(*(Path(p) for p in sys.argv[1:])).reserve(4096)
except DomainError as error:
    print(error.code)
else:
    raise AssertionError('Unknown call was bypassed')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(prior), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'probe_batch_stopped'
    assert child.snapshot()['calls'] == 1


def test_unknown_e03_stops_e04_before_initialization(chain):
    source, prior, batch = chain
    old = previous.open_continuation_budget(source, prior)
    old.unknown(old.reserve(4096))
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'Historical E03 has an unresolved')
    assert not batch.exists() and not revision.marker_path(batch).exists()


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'budget-state.json', 'inheritance.json', 'marker'])
def test_missing_child_state_never_recreates_it(chain, member):
    source, prior, batch = chain
    revision.open_revision_budget(source, prior, batch)
    path = revision.marker_path(batch) if member == 'marker' else batch / member
    path.unlink()
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'missing')
    assert not path.exists()


def test_removing_whole_batch_cannot_reset_budget_because_sibling_marker_remains(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    marker = revision.marker_path(batch)
    marker_bytes = marker.read_bytes()
    shutil.rmtree(batch)  # Synthetic pytest-owned directory only.
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'marker or batch is missing')
    assert not batch.exists() and marker.read_bytes() == marker_bytes


def test_partial_initialization_is_fail_closed(chain, monkeypatch):
    source, prior, batch = chain
    original_mkdir = Path.mkdir

    def interrupted(path, *args, **kwargs):
        if path == batch:
            raise OSError('synthetic interruption after marker')
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        revision.open_revision_budget(source, prior, batch)
    assert json.loads(revision.marker_path(batch).read_text())['status'] == 'initializing'
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'missing')


def test_commit_then_state_write_interruption_blocks_next_call(chain, monkeypatch):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    original_save = revision.save_state

    def interrupted(*args):
        raise OSError('synthetic interruption after SQLite commit')

    monkeypatch.setattr(revision, 'save_state', interrupted)
    with pytest.raises(OSError):
        child.reserve(4096)
    monkeypatch.setattr(revision, 'save_state', original_save)
    assert revision.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 1
    rejected(lambda: child.reserve(4096), 'outside the revision runner')


def test_replacing_child_and_state_with_empty_ledger_does_not_bypass_external_marker(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    ledger = batch / 'budget/budget.sqlite3'
    ledger.unlink()
    BatchBudget(ledger.parent, max_cny='4.718940', max_calls=26)
    (batch / 'budget-state.json').write_text(json.dumps(revision.read_ledger(ledger)))
    rejected(lambda: child.reserve(4096), 'external initialization marker')


@pytest.mark.parametrize('relative', ['inheritance.json', 'budget-state.json'])
def test_historical_json_byte_change_is_detected_even_when_semantically_equivalent(chain, relative):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    path = prior / relative
    path.write_bytes(path.read_bytes() + b'\n')
    rejected(lambda: child.reserve(4096), 'Fixed historical inheritance changed')


def test_live_pins_block_changed_e03_even_before_first_e04_write(chain):
    source, prior, batch = chain
    approved = hashes(source, prior)
    recent = previous.open_continuation_budget(source, prior)
    recent.settle(recent.reserve(4096), REPLY, 4096)
    rejected(lambda: revision.open_revision_budget(source, prior, batch, approved_hashes=approved), 'Approved historical')
    assert not batch.exists() and not revision.marker_path(batch).exists()


def test_changed_e02_or_e03_rejects_reserve_settle_and_unknown(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    call_id = child.reserve(4096)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE calls SET status='reserved' WHERE id=1")
    for operation in (lambda: child.reserve(4096), lambda: child.settle(call_id, REPLY, 4096),
                      lambda: child.unknown(call_id), child.snapshot):
        with pytest.raises(DomainError):
            operation()
    state = revision.read_ledger(batch / 'budget/budget.sqlite3')
    assert state['calls'] == 1 and state['reserved_micros'] == 160768


@pytest.mark.parametrize('relative', ['budget/budget.sqlite3', 'inheritance.json', 'budget-state.json'])
def test_missing_prior_source_stops_without_new_state(chain, relative):
    source, prior, batch = chain
    (prior / relative).unlink()
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'missing')
    assert not batch.exists() and not revision.marker_path(batch).exists()


@pytest.mark.parametrize('artifact', ['career-01', 'career-01-model-trace'])
def test_existing_case_artifact_rejects_implicit_rerun(chain, artifact):
    source, prior, batch = chain
    revision.open_revision_budget(source, prior, batch)
    (batch / artifact).mkdir()
    rejected(lambda: revision.preflight_revision(source=source, prior=prior, batch=batch), 'already has artifacts')


def test_cumulative_call_limit_remains_thirty_six(chain):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    for _ in range(26):
        child.settle(child.reserve(4096), REPLY, 4096)
    with pytest.raises(DomainError) as error:
        child.reserve(4096)
    assert error.value.code == 'probe_budget_exhausted'
    assert child.snapshot()['calls'] + child.snapshot()['inherited_source_calls'] == 36


def test_cumulative_money_limit_includes_unknown_and_all_completed_sources(tmp_path):
    old = BatchBudget(tmp_path / 'e02/budget', max_cny='5', max_calls=36)
    old.unknown(old.reserve(4096))
    prior = tmp_path / 'e03'
    recent = previous.open_continuation_budget(old.path, prior)
    worst = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=64000, output_tokens=4096))
    for _ in range(30):
        recent.settle(recent.reserve(4096), worst, 4096)
    child = revision.open_revision_budget(old.path, prior, tmp_path / 'e04')
    assert child.snapshot()['ceiling_cny'] == '0.016192'
    assert child.snapshot()['inherited_source_calls'] == 31
    with pytest.raises(DomainError) as error:
        child.reserve(4096)
    assert error.value.code == 'probe_budget_exhausted'
    assert child.snapshot()['calls'] == 0


def test_all_evidence_and_sibling_marker_can_be_copied_to_another_repo(chain, tmp_path):
    source, prior, batch = chain
    child = revision.open_revision_budget(source, prior, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    relocated = tmp_path / 'independent'
    relocated.mkdir()
    shutil.copytree(source.parent.parent, relocated / 'e02')
    shutil.copytree(prior, relocated / 'e03')
    shutil.copytree(batch, relocated / 'e04')
    shutil.copyfile(revision.marker_path(batch), revision.marker_path(relocated / 'e04'))
    copied = revision.open_revision_budget(relocated / 'e02/budget/budget.sqlite3', relocated / 'e03', relocated / 'e04')
    assert copied.snapshot()['calls'] == 1
    assert copied.snapshot()['committed_cny'] == '0.000060'
    copied.settle(copied.reserve(4096), REPLY, 4096)
    assert copied.snapshot()['committed_cny'] == '0.000120'
    assert child.snapshot()['calls'] == 1


def test_prior_sidecar_and_interrupted_locks_are_not_removed(chain):
    source, prior, batch = chain
    sidecar = prior / 'budget/budget.sqlite3-wal'
    sidecar.write_bytes(b'synthetic sidecar')
    rejected(lambda: revision.open_revision_budget(source, prior, batch), 'sidecar')
    assert sidecar.exists()
    sidecar.unlink()
    revision.open_revision_budget(source, prior, batch)
    lock = batch / '.budget-operation.lock'
    lock.write_text('synthetic interrupted operation')
    rejected(lambda: revision.preflight_revision(source=source, prior=prior, batch=batch), 'lock')
    assert lock.read_text() == 'synthetic interrupted operation'
