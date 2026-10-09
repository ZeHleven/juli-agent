"""E06 immutable inheritance and last-eight-call ceiling; synthetic data only."""
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from examples.quality_eval import continue_smoke as e03_driver
from examples.quality_eval import revision_smoke as e04_driver
from examples.quality_eval import review_smoke as e05_driver
from examples.quality_eval import contract_smoke as contract
from examples.quality_eval import deepseek_probe
from examples.quality_eval.deepseek_probe import BatchBudget, DeepSeekProbeProvider
from juli.core.common import DomainError
from juli.core.models import ModelMessage, ModelReply, ModelRequest, ModelUsage


REPLY = ModelReply(text='Synthetic only', usage=ModelUsage(input_tokens=10, output_tokens=5))


def spend(budget, last_input):
    for _ in range(8):
        budget.settle(budget.reserve(4096), REPLY, 4096)
    last = ModelReply(text='Synthetic only', usage=ModelUsage(input_tokens=last_input, output_tokens=1))
    budget.settle(budget.reserve(4096), last, 4096)


@pytest.fixture
def chain(tmp_path):
    old = BatchBudget(tmp_path / 'e02/budget', max_cny='5', max_calls=36)
    old.unknown(old.reserve(4096))
    e03, e04, e05, e06 = (tmp_path / name for name in ('e03', 'e04', 'e05', 'e06'))
    spend(e03_driver.open_continuation_budget(old.path, e03), 59902)  # 0.120292
    spend(e04_driver.open_revision_budget(old.path, e03, e04), 52845)  # 0.106178
    fifth = e05_driver.open_review_budget(old.path, e03, e04, e05)
    spend(fifth, 57309)  # 0.115106
    for case in ('career-02', 'career-01', 'tender-03'):
        fifth.claim_case(case)
    return old.path, e03, e04, e05, e06


def hashes(chain):
    return contract.inheritance(*chain[:-1])['source_sha256']


def preflight(chain, case='career-01', **kwargs):
    source, e03, e04, e05, batch = chain
    return contract.preflight_contract(source=source, e03=e03, e04=e04,
                                      e05=e05, batch=batch, case_id=case, **kwargs)


def configure_main(monkeypatch, chain):
    for name, path in zip(('SOURCE', 'E03', 'E04', 'E05', 'BATCH'), chain):
        monkeypatch.setattr(contract, name, path)
    monkeypatch.setattr(contract, 'APPROVED_HASHES', hashes(chain))

    def forbidden(*args, **kwargs):
        raise AssertionError('Provider and task may not execute')

    monkeypatch.setattr(deepseek_probe, 'DeepSeekProbeProvider', forbidden)
    monkeypatch.setattr(contract, 'run_case', forbidden)


def rejected(call, match):
    with pytest.raises(DomainError, match=match):
        call()


def test_preflight_exact_remaining_authorization_without_reading_key_or_writing(chain, monkeypatch, capsys):
    configure_main(monkeypatch, chain)
    key = chain[-1].parent / 'must-not-read-key'
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        if path == key:
            raise AssertionError('No credential access during preflight')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded)
    before = {p: p.read_bytes() for p in chain[-1].parent.rglob('*') if p.is_file()}
    assert contract.main(['--api-key-file', str(key)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['credentials_read'] is False and summary['model_calls'] == 0
    manifest = summary['inheritance']
    assert manifest['source_calls'] == 28
    assert manifest['source_committed_upper_bound_cny'] == '0.341576'
    assert manifest['source_unresolved_reserved_cny'] == '0.160768'
    assert manifest['child_ceiling_cny'] == '4.497656'
    assert manifest['child_max_calls'] == 8
    assert len(manifest['source_sha256']) == 13
    assert manifest['review_before_final'] is False
    assert manifest['trials'] == {'career-01': 5, 'career-02': 4, 'tender-03': 4}
    assert before == {p: p.read_bytes() for p in chain[-1].parent.rglob('*') if p.is_file()}


@pytest.mark.parametrize('credential', [None, 'missing', '', 'short', 'two tokens', '"synthetic-key"'])
def test_invalid_credential_never_creates_e06_state(chain, monkeypatch, credential):
    configure_main(monkeypatch, chain)
    args = ['--live']
    if credential is not None:
        key = chain[-1].parent / 'synthetic-key'
        if credential != 'missing':
            key.write_text(credential, encoding='utf-8')
        args += ['--api-key-file', str(key)]
    with pytest.raises(SystemExit) as error:
        contract.main(args)
    assert error.value.code == 2
    assert not chain[-1].exists() and not contract.marker_path(chain[-1]).exists()


def test_main_uses_ordinary_mode_and_new_trial_without_logging_key(chain, monkeypatch, capsys):
    configure_main(monkeypatch, chain)
    key = chain[-1].parent / 'synthetic-key'
    key.write_text('synthetic-credential-only', encoding='utf-8')
    captured = {}

    def run(case, directory, provider, **kwargs):
        captured.update(kwargs)
        assert case['id'] == 'career-02'
        assert directory == chain[-1] / 'career-02'
        return {'state': {'status': 'completed'}}, None

    monkeypatch.setattr(deepseek_probe, 'DeepSeekProbeProvider', lambda **kwargs: object())
    monkeypatch.setattr(contract, 'run_case', run)
    assert contract.main(['--live', '--case', 'career-02', '--api-key-file', str(key)]) == 0
    assert captured['review_before_final'] is False
    assert captured['profile'] == 'e06-contract-analysis-v1'
    assert captured['trial_number'] == 4 and captured['round_id'] == 'e06'
    assert contract.read_started_cases(chain[-1]) == ['career-02']
    assert 'synthetic-credential-only' not in capsys.readouterr().out


def test_http_nine_rejected_before_transport_and_not_recorded_as_unknown(chain):
    budget = contract.open_contract_budget(*chain)
    sent = []

    def handler(wire):
        sent.append(wire)
        return httpx.Response(200, json={'model': 'synthetic', 'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': 'Synthetic answer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})

    provider = DeepSeekProbeProvider(api_key='synthetic-credential-only',
        trace_directory=chain[-1] / 'traces', budget=budget, transport=httpx.MockTransport(handler))
    request = ModelRequest(messages=(ModelMessage(role='user', content='Synthetic request'),),
                           tools=(), max_output_tokens=100)
    for _ in range(8):
        provider.complete(request)
    with pytest.raises(DomainError) as error:
        provider.complete(request)
    assert error.value.code == 'probe_budget_exhausted'
    state = budget.snapshot()
    assert len(sent) == 8 and state['calls'] == 8
    assert state['calls'] + state['inherited_source_calls'] == 36
    assert state['remaining_calls'] == 0 and state['call_limit_exhausted'] is True
    assert state['stopped'] is False and state['reserved_cny'] == '0.000000'
    assert not (chain[-1] / 'traces/call-0009').exists()
    assert not list((chain[-1] / 'traces').rglob('failure.json'))
    rejected(lambda: preflight(chain), 'exhausted')
    rejected(lambda: budget.claim_case('career-01'), 'exhausted')


def test_unknown_is_distinct_and_stops_every_subsequent_operation(chain):
    budget = contract.open_contract_budget(*chain)
    budget.unknown(budget.reserve(4096))
    state = budget.snapshot()
    assert state['calls'] == 1 and state['remaining_calls'] == 7
    assert state['call_limit_exhausted'] is False and state['stopped'] is True
    assert state['reserved_cny'] == '0.160768'
    rejected(lambda: budget.reserve(4096), 'reserved or unknown')
    rejected(lambda: preflight(chain), 'reserved or unknown')
    rejected(lambda: budget.claim_case('career-01'), 'stopped')


def test_parent_chain_bytes_never_change_and_other_process_reopens(chain):
    source, e03, e04, e05, batch = chain
    original = {p: p.read_bytes() for folder in (source.parent, e03, e04, e05)
                for p in folder.rglob('*') if p.is_file()}
    for marker in (e04_driver.marker_path(e04), e05_driver.marker_path(e05)):
        original[marker] = marker.read_bytes()
    budget = contract.open_contract_budget(*chain)
    budget.settle(budget.reserve(4096), REPLY, 4096)
    script = '''
from pathlib import Path
import sys
from examples.quality_eval.contract_smoke import open_contract_budget
from juli.core.models import ModelReply, ModelUsage
b = open_contract_budget(*(Path(p) for p in sys.argv[1:]))
assert b.snapshot()['calls'] == 1
b.settle(b.reserve(4096), ModelReply(text='Synthetic', usage=ModelUsage(input_tokens=20, output_tokens=10)), 4096)
print(b.snapshot()['committed_cny'])
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, *(str(p) for p in chain)],
                            capture_output=True, text=True, check=True, timeout=15)
    assert result.stdout.strip() == '0.000180'
    assert all(p.read_bytes() == data for p, data in original.items())
    assert budget.snapshot()['calls'] == 2


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'inheritance.json', 'budget-state.json',
                                   'started-cases.json', 'marker'])
def test_changed_pinned_e05_blocks_before_initialization(chain, member):
    approved = hashes(chain)
    e05 = chain[-2]
    path = e05_driver.marker_path(e05) if member == 'marker' else e05 / member
    path.write_bytes(path.read_bytes() + b'\n')
    rejected(lambda: contract.open_contract_budget(*chain, approved_hashes=approved), 'Approved historical')
    assert not chain[-1].exists() and not contract.marker_path(chain[-1]).exists()


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'inheritance.json', 'budget-state.json',
                                   'started-cases.json', 'marker'])
def test_missing_e05_state_is_never_recreated(chain, member):
    e05 = chain[-2]
    path = e05_driver.marker_path(e05) if member == 'marker' else e05 / member
    path.unlink()
    rejected(lambda: contract.open_contract_budget(*chain), 'missing')
    assert not path.exists() and not chain[-1].exists()


def test_e05_unknown_cannot_be_inherited(chain):
    fifth = e05_driver.open_review_budget(*chain[:-1])
    fifth.unknown(fifth.reserve(4096))
    rejected(lambda: contract.open_contract_budget(*chain), 'Historical E05 is missing or has an unresolved')
    assert not chain[-1].exists()


def test_parent_mutation_blocks_child_reserve_settle_unknown_snapshot(chain):
    budget = contract.open_contract_budget(*chain)
    call_id = budget.reserve(4096)
    path = chain[-2] / 'started-cases.json'
    path.write_bytes(path.read_bytes() + b'\n')
    for operation in (lambda: budget.reserve(4096), lambda: budget.settle(call_id, REPLY, 4096),
                      lambda: budget.unknown(call_id), budget.snapshot):
        rejected(operation, 'Fixed historical inheritance changed')
    assert contract.read_ledger(chain[-1] / 'budget/budget.sqlite3')['calls'] == 1


@pytest.mark.parametrize('member', ['budget/budget.sqlite3', 'inheritance.json', 'budget-state.json',
                                   'started-cases.json', 'marker'])
def test_missing_child_state_is_never_recreated(chain, member):
    contract.open_contract_budget(*chain)
    batch = chain[-1]
    path = contract.marker_path(batch) if member == 'marker' else batch / member
    path.unlink()
    rejected(lambda: contract.open_contract_budget(*chain), 'missing')
    assert not path.exists()


def test_partial_initialization_after_marker_blocks_reopening(chain, monkeypatch):
    original = Path.mkdir

    def interrupted(path, *args, **kwargs):
        if path == chain[-1]:
            raise OSError('Synthetic initialization interruption')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', interrupted)
    with pytest.raises(OSError):
        contract.open_contract_budget(*chain)
    monkeypatch.setattr(Path, 'mkdir', original)
    assert contract.read_json(contract.marker_path(chain[-1]))['status'] == 'initializing'
    rejected(lambda: contract.open_contract_budget(*chain), 'missing')


def test_sqlite_commit_without_state_update_blocks_next_request(chain, monkeypatch):
    budget = contract.open_contract_budget(*chain)
    original = contract.save_state

    def interrupted(*args):
        raise OSError('Synthetic post-commit interruption')

    monkeypatch.setattr(contract, 'save_state', interrupted)
    with pytest.raises(OSError):
        budget.reserve(4096)
    monkeypatch.setattr(contract, 'save_state', original)
    rejected(lambda: budget.reserve(4096), 'outside the contract runner')
    assert contract.read_ledger(chain[-1] / 'budget/budget.sqlite3')['calls'] == 1


def test_state_commit_without_external_marker_blocks_next_request(chain, monkeypatch):
    budget = contract.open_contract_budget(*chain)
    original = Path.replace
    marker = contract.marker_path(chain[-1])

    def interrupted(path, *args, **kwargs):
        if path == marker.with_name(marker.name + '.next'):
            raise OSError('Synthetic marker interruption')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(OSError):
        budget.reserve(4096)
    monkeypatch.setattr(Path, 'replace', original)
    rejected(lambda: budget.reserve(4096), 'external initialization marker')


def test_claim_survives_no_artifacts_and_cannot_be_reset(chain):
    budget = contract.open_contract_budget(*chain)
    budget.claim_case('career-02')
    assert not (chain[-1] / 'career-02').exists()
    rejected(lambda: preflight(chain, 'career-02'), 'already claimed')
    rejected(lambda: budget.claim_case('career-02'), 'already claimed')
    (chain[-1] / contract.TRIAL_STATE).write_text('[]', encoding='utf-8')
    rejected(lambda: budget.claim_case('career-02'), 'external initialization marker')
    rejected(lambda: budget.reserve(4096), 'external initialization marker')


def test_claim_interrupted_before_marker_is_not_reusable(chain, monkeypatch):
    budget = contract.open_contract_budget(*chain)
    original = contract.save_state

    def interrupted(*args):
        raise OSError('Synthetic interrupted claim')

    monkeypatch.setattr(contract, 'save_state', interrupted)
    with pytest.raises(OSError):
        budget.claim_case('career-02')
    monkeypatch.setattr(contract, 'save_state', original)
    rejected(lambda: budget.claim_case('career-02'), 'external initialization marker')
    assert contract.read_started_cases(chain[-1]) == ['career-02']
    assert contract.read_ledger(chain[-1] / 'budget/budget.sqlite3')['calls'] == 0


def test_interrupted_locks_and_parent_sqlite_sidecars_stop_without_cleanup(chain):
    sidecar = chain[-2] / 'budget/budget.sqlite3-wal'
    sidecar.write_bytes(b'Synthetic sidecar')
    rejected(lambda: contract.open_contract_budget(*chain), 'sidecar')
    assert sidecar.exists()
    sidecar.unlink()
    contract.open_contract_budget(*chain)
    lock = chain[-1] / '.budget-operation.lock'
    lock.write_text('Synthetic interrupted operation', encoding='utf-8')
    rejected(lambda: preflight(chain), 'lock')
    assert lock.exists()
