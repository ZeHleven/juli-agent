"""E05 budget-chain and entrypoint checks with synthetic ledgers, never network."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from examples.quality_eval import continue_smoke as previous
from examples.quality_eval import revision_smoke as prior_round
from examples.quality_eval import review_smoke as review
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
    e04 = tmp_path / 'e04'
    fourth = prior_round.open_revision_budget(old.path, prior, e04)
    for _ in range(8):
        fourth.settle(fourth.reserve(4096), REPLY, 4096)
    # 8 * 60 + (52845 * 2 + 1 * 8) = 106178 micro-CNY, nine calls.
    last_fourth = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=52845, output_tokens=1))
    fourth.settle(fourth.reserve(4096), last_fourth, 4096)
    return old.path, prior, e04, tmp_path / 'e05'


def rejected(call, match):
    with pytest.raises(DomainError, match=match):
        call()


def hashes(source, prior, e04):
    return review.inheritance(source, prior, e04)['source_sha256']


def configure_main(monkeypatch, chain):
    source, prior, e04, batch = chain
    monkeypatch.setattr(review, 'SOURCE', source)
    monkeypatch.setattr(review, 'E03', prior)
    monkeypatch.setattr(review, 'E04', e04)
    monkeypatch.setattr(review, 'BATCH', batch)
    monkeypatch.setattr(review, 'APPROVED_HASHES', hashes(source, prior, e04))

    def forbidden(*args, **kwargs):
        raise AssertionError('No provider or task should run')

    monkeypatch.setattr(deepseek_probe, 'DeepSeekProbeProvider', forbidden)
    monkeypatch.setattr(review, 'run_case', forbidden)


def test_preflight_reads_no_key_and_writes_nothing(chain, monkeypatch, capsys):
    configure_main(monkeypatch, chain)
    source, prior, e04, batch = chain
    key = batch.parent / 'must-not-read-key'
    original_read = Path.read_text

    def guarded(path, *args, **kwargs):
        if path == key:
            raise AssertionError('Preflight attempted credential access')
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded)
    before = {p: p.read_bytes() for p in batch.parent.rglob('*') if p.is_file()}
    assert review.main(['--api-key-file', str(key)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['credentials_read'] is False and summary['model_calls'] == 0
    assert summary['round_id'] == 'e05'
    manifest = summary['inheritance']
    assert manifest['source_calls'] == 19
    assert manifest['source_unresolved_reserved_cny'] == '0.160768'
    assert manifest['source_committed_upper_bound_cny'] == '0.226470'
    assert manifest['child_ceiling_cny'] == '4.612762'
    assert manifest['child_max_calls'] == 17
    assert manifest['trials'] == {'career-01': 4, 'career-02': 3, 'tender-03': 3}
    assert before == {p: p.read_bytes() for p in batch.parent.rglob('*') if p.is_file()}


@pytest.mark.parametrize('credential', [None, 'missing', '', '"synthetic-secret"', 'two tokens', 'short'])
def test_missing_or_invalid_key_makes_no_artifacts_or_calls(chain, monkeypatch, credential):
    configure_main(monkeypatch, chain)
    source, prior, e04, batch = chain
    args = ['--live']
    if credential is not None:
        key = batch.parent / 'synthetic-key'
        if credential != 'missing':
            key.write_text(credential, encoding='utf-8')
        args.extend(['--api-key-file', str(key)])
    with pytest.raises(SystemExit) as error:
        review.main(args)
    assert error.value.code == 2
    assert not batch.exists() and not review.marker_path(batch).exists()


def test_all_source_ledgers_and_markers_unchanged_and_authorization_accumulates(chain):
    source, prior, e04, batch = chain
    original = {p: p.read_bytes() for p in (source, prior / 'budget/budget.sqlite3',
        prior / 'inheritance.json', prior / 'budget-state.json', e04 / 'budget/budget.sqlite3',
        e04 / 'inheritance.json', e04 / 'budget-state.json', prior_round.marker_path(e04))}
    child = review.open_review_budget(source, prior, e04, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    state = child.snapshot()
    assert state['committed_cny'] == '0.000060'
    assert state['remaining_cny'] == '4.612702'
    assert state['inherited_source_calls'] == 19
    assert state['inherited_source_reserved_cny'] == '0.160768'
    assert state['inherited_source_committed_cny'] == '0.226470'
    assert state['total_ceiling_cny'] == '5.000000'
    assert all(path.read_bytes() == value for path, value in original.items())


def test_another_process_reopens_same_ledger_and_increments_cost(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.review_smoke import open_review_budget
from juli.core.models import ModelReply, ModelUsage
b = open_review_budget(*(Path(p) for p in sys.argv[1:]))
assert b.snapshot()['calls'] == 1
b.settle(b.reserve(4096), ModelReply(text='synthetic', usage=ModelUsage(input_tokens=20, output_tokens=10)), 4096)
print(b.snapshot()['committed_cny'])
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(prior), str(e04), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == '0.000180'
    _, summary = review.preflight_review(source=source, e03=prior, e04=e04, batch=batch, case_id='career-02')
    assert summary['existing_child_ledger']['calls'] == 2
    assert child.snapshot()['calls'] == 2


def test_unknown_child_stops_next_process_and_case_without_retry(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.unknown(child.reserve(4096))
    rejected(lambda: review.preflight_review(source=source, e03=prior, e04=e04, batch=batch), 'stopped')
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.review_smoke import open_review_budget
from juli.core.common import DomainError
try:
    open_review_budget(*(Path(p) for p in sys.argv[1:])).reserve(4096)
except DomainError as error:
    print(error.code)
else:
    raise AssertionError('Unknown call was bypassed')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(source), str(prior), str(e04), str(batch)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'probe_batch_stopped'
    assert child.snapshot()['calls'] == 1


def test_unknown_e03_stops_e05_before_initialization(chain):
    source, prior, e04, batch = chain
    old = previous.open_continuation_budget(source, prior)
    old.unknown(old.reserve(4096))
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'Historical E03 has an unresolved')
    assert not batch.exists() and not review.marker_path(batch).exists()


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'budget-state.json', 'inheritance.json', 'started-cases.json', 'marker'])
def test_missing_child_state_never_recreates_it(chain, member):
    source, prior, e04, batch = chain
    review.open_review_budget(source, prior, e04, batch)
    path = review.marker_path(batch) if member == 'marker' else batch / member
    path.unlink()
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'missing')
    assert not path.exists()


def test_removing_whole_batch_cannot_reset_budget_because_sibling_marker_remains(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    marker = review.marker_path(batch)
    marker_bytes = marker.read_bytes()
    shutil.rmtree(batch)  # Synthetic pytest-owned directory only.
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'marker or batch is missing')
    assert not batch.exists() and marker.read_bytes() == marker_bytes


def test_partial_initialization_is_fail_closed(chain, monkeypatch):
    source, prior, e04, batch = chain
    original_mkdir = Path.mkdir

    def interrupted(path, *args, **kwargs):
        if path == batch:
            raise OSError('synthetic interruption after marker')
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        review.open_review_budget(source, prior, e04, batch)
    assert json.loads(review.marker_path(batch).read_text())['status'] == 'initializing'
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'missing')


def test_commit_then_state_write_interruption_blocks_next_call(chain, monkeypatch):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    original_save = review.save_state

    def interrupted(*args):
        raise OSError('synthetic interruption after SQLite commit')

    monkeypatch.setattr(review, 'save_state', interrupted)
    with pytest.raises(OSError):
        child.reserve(4096)
    monkeypatch.setattr(review, 'save_state', original_save)
    assert review.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 1
    rejected(lambda: child.reserve(4096), 'outside the review runner')


def test_replacing_child_and_state_with_empty_ledger_does_not_bypass_external_marker(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    ledger = batch / 'budget/budget.sqlite3'
    ledger.unlink()
    BatchBudget(ledger.parent, max_cny='4.612762', max_calls=17)
    (batch / 'budget-state.json').write_text(json.dumps(review.read_ledger(ledger)))
    rejected(lambda: child.reserve(4096), 'external initialization marker')


@pytest.mark.parametrize('relative', ['inheritance.json', 'budget-state.json'])
def test_historical_json_byte_change_is_detected_even_when_semantically_equivalent(chain, relative):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    path = prior / relative
    path.write_bytes(path.read_bytes() + b'\n')
    rejected(lambda: child.reserve(4096), 'E04 inheritance manifest changed')


def test_live_pins_block_changed_e03_even_before_first_e05_write(chain):
    source, prior, e04, batch = chain
    approved = hashes(source, prior, e04)
    recent = previous.open_continuation_budget(source, prior)
    recent.settle(recent.reserve(4096), REPLY, 4096)
    rejected(lambda: review.open_review_budget(source, prior, e04, batch, approved_hashes=approved), 'Approved historical')
    assert not batch.exists() and not review.marker_path(batch).exists()


def test_changed_e02_or_e03_rejects_reserve_settle_and_unknown(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    call_id = child.reserve(4096)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE calls SET status='reserved' WHERE id=1")
    for operation in (lambda: child.reserve(4096), lambda: child.settle(call_id, REPLY, 4096),
                      lambda: child.unknown(call_id), child.snapshot):
        with pytest.raises(DomainError):
            operation()
    state = review.read_ledger(batch / 'budget/budget.sqlite3')
    assert state['calls'] == 1 and state['reserved_micros'] == 160768


@pytest.mark.parametrize('relative', ['budget/budget.sqlite3', 'inheritance.json', 'budget-state.json'])
def test_missing_prior_source_stops_without_new_state(chain, relative):
    source, prior, e04, batch = chain
    (prior / relative).unlink()
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'missing')
    assert not batch.exists() and not review.marker_path(batch).exists()


@pytest.mark.parametrize('artifact', ['career-01', 'career-01-model-trace'])
def test_existing_case_artifact_rejects_implicit_rerun(chain, artifact):
    source, prior, e04, batch = chain
    review.open_review_budget(source, prior, e04, batch)
    (batch / artifact).mkdir()
    rejected(lambda: review.preflight_review(source=source, e03=prior, e04=e04, batch=batch), 'already has artifacts')


def test_cumulative_call_limit_remains_thirty_six(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    for _ in range(17):
        child.settle(child.reserve(4096), REPLY, 4096)
    with pytest.raises(DomainError) as error:
        child.reserve(4096)
    assert error.value.code == 'probe_budget_exhausted'
    assert child.snapshot()['calls'] + child.snapshot()['inherited_source_calls'] == 36


def test_cumulative_money_limit_includes_unknown_and_all_completed_sources(tmp_path):
    old = BatchBudget(tmp_path / 'e02/budget', max_cny='5', max_calls=36)
    old.unknown(old.reserve(4096))
    e03, e04 = tmp_path / 'e03', tmp_path / 'e04'
    third = previous.open_continuation_budget(old.path, e03)
    worst = ModelReply(text='synthetic', usage=ModelUsage(input_tokens=64000, output_tokens=4096))
    for _ in range(29):
        third.settle(third.reserve(4096), worst, 4096)
    fourth = prior_round.open_revision_budget(old.path, e03, e04)
    fourth.settle(fourth.reserve(4096), worst, 4096)
    child = review.open_review_budget(old.path, e03, e04, tmp_path / 'e05')
    assert child.snapshot()['ceiling_cny'] == '0.016192'
    assert child.snapshot()['inherited_source_calls'] == 31
    with pytest.raises(DomainError) as error:
        child.reserve(4096)
    assert error.value.code == 'probe_budget_exhausted'
    assert child.snapshot()['calls'] == 0


def test_all_evidence_and_sibling_marker_can_be_copied_to_another_repo(chain, tmp_path):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.settle(child.reserve(4096), REPLY, 4096)
    relocated = tmp_path / 'independent'
    relocated.mkdir()
    shutil.copytree(source.parent.parent, relocated / 'e02')
    shutil.copytree(prior, relocated / 'e03')
    shutil.copytree(e04, relocated / 'e04')
    shutil.copyfile(prior_round.marker_path(e04), prior_round.marker_path(relocated / 'e04'))
    shutil.copytree(batch, relocated / 'e05')
    shutil.copyfile(review.marker_path(batch), review.marker_path(relocated / 'e05'))
    copied = review.open_review_budget(relocated / 'e02/budget/budget.sqlite3', relocated / 'e03', relocated / 'e04', relocated / 'e05')
    assert copied.snapshot()['calls'] == 1
    assert copied.snapshot()['committed_cny'] == '0.000060'
    copied.settle(copied.reserve(4096), REPLY, 4096)
    assert copied.snapshot()['committed_cny'] == '0.000120'
    assert child.snapshot()['calls'] == 1


def test_prior_sidecar_and_interrupted_locks_are_not_removed(chain):
    source, prior, e04, batch = chain
    sidecar = prior / 'budget/budget.sqlite3-wal'
    sidecar.write_bytes(b'synthetic sidecar')
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'sidecar')
    assert sidecar.exists()
    sidecar.unlink()
    review.open_review_budget(source, prior, e04, batch)
    lock = batch / '.budget-operation.lock'
    lock.write_text('synthetic interrupted operation')
    rejected(lambda: review.preflight_review(source=source, e03=prior, e04=e04, batch=batch), 'lock')
    assert lock.read_text() == 'synthetic interrupted operation'


def test_unknown_e04_stops_e05_before_initialization(chain):
    source, prior, e04, batch = chain
    old = prior_round.open_revision_budget(source, prior, e04)
    old.unknown(old.reserve(4096))
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'Historical E04 is missing or has an unresolved')
    assert not batch.exists() and not review.marker_path(batch).exists()


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'budget-state.json', 'inheritance.json', 'marker'])
def test_missing_e04_state_never_recreates_it(chain, member):
    source, prior, e04, batch = chain
    path = prior_round.marker_path(e04) if member == 'marker' else e04 / member
    path.unlink()
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'missing')
    assert not path.exists() and not batch.exists() and not review.marker_path(batch).exists()


@pytest.mark.parametrize('member', ['inheritance.json', 'budget-state.json', 'marker'])
def test_changed_e04_bytes_reject_all_child_operations(chain, member):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    call_id = child.reserve(4096)
    path = prior_round.marker_path(e04) if member == 'marker' else e04 / member
    path.write_bytes(path.read_bytes() + b'\n')
    for operation in (lambda: child.reserve(4096), lambda: child.settle(call_id, REPLY, 4096),
                      lambda: child.unknown(call_id), child.snapshot):
        rejected(operation, 'Fixed historical inheritance changed')
    assert review.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 1


def test_changed_pinned_e04_before_first_write_is_blocked(chain):
    source, prior, e04, batch = chain
    approved = hashes(source, prior, e04)
    old = prior_round.open_revision_budget(source, prior, e04)
    old.settle(old.reserve(4096), REPLY, 4096)
    rejected(lambda: review.open_review_budget(source, prior, e04, batch, approved_hashes=approved), 'Approved historical')
    assert not batch.exists() and not review.marker_path(batch).exists()


def test_partial_initialization_after_sqlite_leaves_marker_and_blocks_reopen(chain, monkeypatch):
    source, prior, e04, batch = chain
    original_save = review.save_state

    def interrupted(*args):
        raise OSError('synthetic interruption before state and ready marker')

    monkeypatch.setattr(review, 'save_state', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        review.open_review_budget(source, prior, e04, batch)
    monkeypatch.setattr(review, 'save_state', original_save)
    assert (batch / 'budget/budget.sqlite3').exists()
    assert review.read_json(review.marker_path(batch))['status'] == 'initializing'
    rejected(lambda: review.open_review_budget(source, prior, e04, batch), 'missing')


def test_interrupted_external_marker_update_blocks_next_call(chain, monkeypatch):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    original_replace = Path.replace
    marker_next = review.marker_path(batch).with_name(review.marker_path(batch).name + '.next')

    def interrupted(path, *args, **kwargs):
        if path == marker_next:
            raise OSError('synthetic interruption before external marker replace')
        return original_replace(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        child.reserve(4096)
    monkeypatch.setattr(Path, 'replace', original_replace)
    assert review.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 1
    assert marker_next.exists()
    rejected(lambda: child.reserve(4096), 'external initialization marker')


def test_mock_live_entrypoint_explicitly_enables_review_and_new_trial(chain, monkeypatch, capsys):
    configure_main(monkeypatch, chain)
    source, prior, e04, batch = chain
    key = batch.parent / 'synthetic-key'
    key.write_text('synthetic-credential-only', encoding='utf-8')
    captured = {}

    def provider(**kwargs):
        captured['provider'] = kwargs
        return object()

    def run(case, directory, instance, **kwargs):
        captured['run'] = kwargs
        assert case['id'] == 'career-02'
        assert directory == batch / 'career-02'
        return {'state': {'status': 'completed'}}, None

    monkeypatch.setattr(deepseek_probe, 'DeepSeekProbeProvider', provider)
    monkeypatch.setattr(review, 'run_case', run)
    assert review.main(['--live', '--case', 'career-02', '--api-key-file', str(key)]) == 0
    assert captured['run']['review_before_final'] is True
    assert captured['run']['profile'] == 'e05-reviewed-analysis-v1'
    assert captured['run']['trial_number'] == 3
    assert captured['run']['round_id'] == 'e05'
    assert captured['provider']['budget'].snapshot()['calls'] == 0
    assert 'synthetic-credential-only' not in capsys.readouterr().out


def test_claim_survives_removal_of_both_case_and_trace_artifacts(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.claim_case('career-01')
    # No trace was created at all: this also models cleanup of both empty dirs.
    assert not (batch / 'career-01').exists()
    assert not (batch / 'career-01-model-trace').exists()
    rejected(lambda: review.preflight_review(source=source, e03=prior, e04=e04, batch=batch), 'already claimed')
    rejected(lambda: child.claim_case('career-01'), 'already claimed')
    assert child.snapshot()['calls'] == 0
    assert review.read_json(review.marker_path(batch))['started_cases'] == ['career-01']


def test_trial_claim_cannot_be_cleared_without_external_marker_mismatch(chain):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    child.claim_case('career-01')
    (batch / review.TRIAL_STATE).write_text('[]', encoding='utf-8')
    rejected(lambda: child.claim_case('career-01'), 'external initialization marker')
    rejected(lambda: child.reserve(4096), 'external initialization marker')


def test_interrupted_claim_write_never_runs_provider_or_reclaims_trial(chain, monkeypatch):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    original_save = review.save_state

    def interrupted(*args):
        raise OSError('synthetic interruption after trial claim')

    monkeypatch.setattr(review, 'save_state', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        child.claim_case('career-01')
    monkeypatch.setattr(review, 'save_state', original_save)
    rejected(lambda: child.claim_case('career-01'), 'external initialization marker')
    assert review.read_started_cases(batch) == ['career-01']
    assert review.read_ledger(batch / 'budget/budget.sqlite3')['calls'] == 0


@pytest.mark.parametrize('value', [['career-01', 'career-01'], ['other'], 'career-01', [42]])
def test_invalid_trial_claim_list_is_rejected(chain, value):
    source, prior, e04, batch = chain
    child = review.open_review_budget(source, prior, e04, batch)
    (batch / review.TRIAL_STATE).write_text(json.dumps(value), encoding='utf-8')
    rejected(lambda: child.reserve(4096), 'trial claims are invalid')
