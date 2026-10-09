"""Explicit E06 trials under the unchanged cumulative CNY 5 / 36-call cap.

Preflight is read-only. Live initialization writes an exclusive marker *outside*
the E06 directory before creating any child state. A crash after that write, or
loss of either marker or child files, requires operator review, never automatic
reinitialization. Each mutation commits SQLite, then child state, then marker;
a crash between those writes fails closed on their fingerprint mismatch. Copies
must include all four historical batches, both E04/E05 sibling markers, the E06
batch, and its sibling marker. This is local accident detection, not protection
against an operator deleting or replacing the entire history and all markers.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from juli.core.common import DomainError

from . import review_smoke as previous
from .continue_smoke import ordinary, read_ledger, read_json, sha, money, locked
from .smoke import ROOT, CASES, preflight, run_case, wait_for_answer, emit, save_json

SOURCE = previous.SOURCE
E03 = previous.E03
E04 = previous.E04
E05 = previous.BATCH
BATCH = ROOT / '.local/quality-eval/v1/e06-deepseek-flash-20261003'
TOTAL_MICROS, TOTAL_CALLS = 5_000_000, 36
MANIFEST, STATE, TRIAL_STATE = 'inheritance.json', 'budget-state.json', 'started-cases.json'
TRIALS = {'career-01': 5, 'career-02': 4, 'tender-03': 4}
# Byte fingerprints cover the completed E02/E03/E04/E05 chain, trial claims, and sibling markers.
# Synthetic tests pass their own ledger paths without this live-only pin set.
APPROVED_HASHES = {
    'e02/budget/budget.sqlite3': '95abeb9f97a2dd5a2e97d7f89fc85aaee7412eaad28e2f90e50f926b21f0c247',
    'e03/budget/budget.sqlite3': 'b3f6edb7cfb0f1119f95ac6d52cd9f1945dae381507edc98eb953150207d14bb',
    'e03/inheritance.json': '22d2a9c880b0e633b81d28ebd84db6e87f47837c6c51e2637f6ab46b3f7da2b4',
    'e03/budget-state.json': '638c6098d8e88a6a289ac960fa05450441433e4059b34e125b8c03ad3dd96d3f',
    'e04/budget/budget.sqlite3': 'fc2b17949ed6b9ff24c860dcd3562e966d47f00193e9167da629bf8d666cb670',
    'e04/inheritance.json': '842c17ba929ce5a202e6e6c23cf5679af8cb410f988b2d56e7c0d9e5d2f65c50',
    'e04/budget-state.json': '1927067266448c4bfdf795219169b16f2e2e4f73b9a1fee533ec15c0a96a2465',
    'e04/initialized.json': '3b0c929726490ab644292be41140ffeda3e3e65a09bffef817534b22d3b6d36c',
    'e05/budget/budget.sqlite3': 'beecfaac0e98ceb7a1eb129bd19e6df856dd8af93172b8e904e976ba0a9714a9',
    'e05/inheritance.json': '303407ad9bf9b37d2f19a4a3c4601ee064a6ec36965f1239b9efd7082c00e111',
    'e05/budget-state.json': '5e72871451185c0b38b8a6ebeafcd10ff1ce7449dcbe6bfc1c850a4bf6b3f975',
    'e05/started-cases.json': '96e45c7ffd2715153a996a662c07a5ed358ab2186862050e7a2557472a3a7601',
    'e05/initialized.json': '0439934a065797083de91406328a3aaabd07ef71c876989376142177df65c36b',
}


def fail(message):
    raise DomainError('contract_budget_invalid', message)


def marker_path(batch):
    batch = ordinary(batch, required=False)
    return ordinary(batch.parent / ('.' + batch.name + '.initialized.json'), required=False)


def inheritance(source, e03, e04, e05, *, approved_hashes=None):
    """Verify all four immutable historical batches, including E05 trial claims."""
    source = ordinary(source)
    e03, e04, e05 = (ordinary(path, required=False) for path in (e03, e04, e05))
    paths = {'e02/budget/budget.sqlite3': source,
             'e03/budget/budget.sqlite3': e03 / 'budget/budget.sqlite3',
             'e03/inheritance.json': e03 / MANIFEST,
             'e03/budget-state.json': e03 / STATE,
             'e04/budget/budget.sqlite3': e04 / 'budget/budget.sqlite3',
             'e04/inheritance.json': e04 / MANIFEST,
             'e04/budget-state.json': e04 / STATE,
             'e04/initialized.json': previous.previous.marker_path(e04),
             'e05/budget/budget.sqlite3': e05 / 'budget/budget.sqlite3',
             'e05/inheritance.json': e05 / MANIFEST,
             'e05/budget-state.json': e05 / STATE,
             'e05/started-cases.json': e05 / TRIAL_STATE,
             'e05/initialized.json': previous.marker_path(e05)}
    before = {name: sha(ordinary(path)) for name, path in paths.items()}
    if approved_hashes is not None and before != approved_hashes:
        fail('Approved historical E02/E03/E04/E05 evidence changed; no new trial is permitted.')
    parent_manifest = previous.inheritance(source, e03, e04)
    recent = previous.inspect_child(source, e03, e04, e05, parent_manifest)
    if recent is None or recent['stopped']:
        fail('Historical E05 is missing or has an unresolved call; further trials are stopped.')
    if before != {name: sha(ordinary(path)) for name, path in paths.items()}:
        fail('Historical evidence changed during inspection.')
    states = [read_ledger(source), read_ledger(e03 / 'budget/budget.sqlite3'),
              read_ledger(e04 / 'budget/budget.sqlite3'), recent]
    if before != {name: sha(ordinary(path)) for name, path in paths.items()}:
        fail('Historical evidence changed during inspection.')
    committed = sum(item['committed_micros'] for item in states)
    reserved = sum(item['reserved_micros'] for item in states)
    calls = sum(item['calls'] for item in states)
    remaining = TOTAL_MICROS - committed - reserved
    if remaining <= 0 or calls >= TOTAL_CALLS:
        fail('No cumulative authorization remains for E06.')
    return {'version': 1, 'round_id': 'e06', 'source_rounds': ['e02', 'e03', 'e04', 'e05'],
            'source_sha256': before,
            'total_ceiling_cny': money(TOTAL_MICROS), 'total_max_calls': TOTAL_CALLS,
            'source_committed_upper_bound_cny': money(committed),
            'source_unresolved_reserved_cny': money(reserved), 'source_calls': calls,
            'child_ceiling_cny': money(remaining), 'child_max_calls': TOTAL_CALLS - calls,
            'user_approved_new_trial': True, 'approved_cases': list(CASES), 'trials': TRIALS,
            'review_before_final': False,
            'cost_basis': 'conservative_upper_bounds_not_actual_bill',
            'unresolved_source_calls_released': False}


def require_calls_available(state):
    if state['calls'] >= state['max_calls']:
        raise DomainError('probe_budget_exhausted',
                          'The E06 call budget is exhausted; no new request was sent.')


def marker_value(manifest, state=None, started_cases=None):
    return {'version': 1, 'round_id': 'e06',
            'status': 'ready' if state is not None else 'initializing',
            'inheritance': manifest, 'child_state': state,
            'started_cases': [] if started_cases is None else started_cases}


def read_started_cases(batch):
    cases = read_json(batch / TRIAL_STATE)
    if (not isinstance(cases, list) or any(type(case) is not str or case not in CASES for case in cases)
            or len(cases) != len(set(cases))):
        fail('E06 trial claims are invalid; do not reset consumed trials.')
    return cases


def verify_existing(source, e03, e04, e05, batch, expected, *, approved_hashes=None):
    if inheritance(source, e03, e04, e05, approved_hashes=approved_hashes) != expected:
        fail('Fixed historical inheritance changed.')
    if read_json(batch / MANIFEST) != expected:
        fail('E06 inheritance manifest changed.')
    current = read_ledger(batch / 'budget/budget.sqlite3')
    if read_json(batch / STATE) != current:
        fail('E06 ledger changed outside the contract runner; no reset is allowed.')
    if read_json(marker_path(batch)) != marker_value(expected, current, read_started_cases(batch)):
        fail('E06 external initialization marker is incomplete or changed.')
    if (money(current['ceiling_micros']) != expected['child_ceiling_cny']
            or current['max_calls'] != expected['child_max_calls']):
        fail('E06 child limits do not match the cumulative authorization.')
    return current


def save_state(batch, manifest):
    state = read_ledger(batch / 'budget/budget.sqlite3')
    temporary = batch / 'budget-state.next.json'
    save_json(temporary, state)
    temporary.replace(batch / STATE)
    marker = marker_path(batch)
    temporary_marker = marker.with_name(marker.name + '.next')
    save_json(temporary_marker, marker_value(manifest, state, read_started_cases(batch)))
    temporary_marker.replace(marker)


class ContractBudget:
    """Recheck immutable parents and all child fingerprints for every operation."""

    def __init__(self, source, e03, e04, e05, batch, manifest, approved_hashes=None):
        self.source, self.e03, self.e04, self.e05, self.batch = Path(source), Path(e03), Path(e04), Path(e05), Path(batch)
        self.manifest, self.approved_hashes = manifest, approved_hashes

    def _verify(self):
        return verify_existing(self.source, self.e03, self.e04, self.e05, self.batch, self.manifest,
                               approved_hashes=self.approved_hashes)

    def snapshot(self):
        with locked(self.batch):
            state = self._verify()
        source_committed = self.manifest['source_committed_upper_bound_cny']
        source_reserved = self.manifest['source_unresolved_reserved_cny']
        return {'ceiling_cny': money(state['ceiling_micros']), 'max_calls': state['max_calls'],
                'calls': state['calls'], 'committed_cny': money(state['committed_micros']),
                'remaining_calls': state['max_calls'] - state['calls'],
                'call_limit_exhausted': state['calls'] >= state['max_calls'],
                'reserved_cny': money(state['reserved_micros']),
                'remaining_cny': money(state['ceiling_micros'] - state['committed_micros'] - state['reserved_micros']),
                'stopped': state['stopped'], 'inherited_source_committed_cny': source_committed,
                'inherited_source_reserved_cny': source_reserved,
                'inherited_source_calls': self.manifest['source_calls'],
                'total_ceiling_cny': '5.000000', 'total_max_calls': TOTAL_CALLS,
                'cost_basis': self.manifest['cost_basis']}

    def _operate(self, operation, *args):
        from .deepseek_probe import BatchBudget
        with locked(self.batch):
            self._verify()
            budget = BatchBudget(self.batch / 'budget', max_cny=self.manifest['child_ceiling_cny'],
                                 max_calls=self.manifest['child_max_calls'])
            try:
                return getattr(budget, operation)(*args)
            finally:
                # If interrupted here, the durable mismatch blocks every later call.
                save_state(self.batch, self.manifest)

    def reserve(self, max_output_tokens):
        return self._operate('reserve', max_output_tokens)

    def settle(self, call_id, reply, max_output_tokens):
        return self._operate('settle', call_id, reply, max_output_tokens)

    def unknown(self, call_id):
        return self._operate('unknown', call_id)

    def claim_case(self, case_id):
        """Consume one trial before trace creation; cleanup cannot make it reusable."""
        with locked(self.batch):
            state = self._verify()
            if state['stopped']:
                fail('The E06 ledger is stopped; no new trial can be claimed.')
            require_calls_available(state)
            cases = read_started_cases(self.batch)
            if case_id not in CASES or case_id in cases:
                fail('This E06 case is invalid or already claimed; no retry is permitted.')
            if (self.batch / case_id).exists() or (self.batch / (case_id + '-model-trace')).exists():
                fail('This E06 case already has artifacts; no implicit rerun is permitted.')
            temporary = self.batch / (TRIAL_STATE + '.next')
            save_json(temporary, [*cases, case_id])
            temporary.replace(self.batch / TRIAL_STATE)
            # A crash between the claim and marker writes stops all later actions.
            save_state(self.batch, self.manifest)


def inspect_child(source, e03, e04, e05, batch, expected, *, approved_hashes=None):
    """No locks are created by read-only inspection; existing locks stop it."""
    marker = marker_path(batch)
    if batch.exists() != marker.exists():
        fail('E06 marker or batch is missing; do not recreate lost evaluation state.')
    if not batch.exists():
        return None
    if (batch / '.budget-operation.lock').exists():
        fail('Another or interrupted E06 operation holds the budget lock.')
    current = verify_existing(source, e03, e04, e05, batch, expected, approved_hashes=approved_hashes)
    if (batch / '.budget-operation.lock').exists():
        fail('E06 changed during read-only inspection.')
    return current


def open_contract_budget(source=SOURCE, e03=E03, e04=E04, e05=E05, batch=BATCH, *, approved_hashes=None):
    source, e03, e04, e05, batch = (ordinary(source), ordinary(e03, required=False), ordinary(e04, required=False), ordinary(e05, required=False),
                            ordinary(batch, required=False))
    expected = inheritance(source, e03, e04, e05, approved_hashes=approved_hashes)
    inspect_child(source, e03, e04, e05, batch, expected, approved_hashes=approved_hashes)
    if batch.exists():
        with locked(batch):
            verify_existing(source, e03, e04, e05, batch, expected, approved_hashes=approved_hashes)
    else:
        # The historical rounds require their parent directory to already exist.
        # Exclusive marker creation serializes competing initializers. It is never
        # deleted on failure, including a failed batch mkdir or partial JSON write.
        save_json(marker_path(batch), marker_value(expected))
        batch.mkdir(parents=False, exist_ok=False)
        with locked(batch):
            save_json(batch / MANIFEST, expected)
            save_json(batch / TRIAL_STATE, [])
            from .deepseek_probe import BatchBudget
            BatchBudget(batch / 'budget', max_cny=expected['child_ceiling_cny'],
                        max_calls=expected['child_max_calls'])
            save_state(batch, expected)
    return ContractBudget(source, e03, e04, e05, batch, expected, approved_hashes)


def preflight_contract(*, repo=ROOT, source=SOURCE, e03=E03, e04=E04, e05=E05, batch=BATCH,
                       case_id='career-01', approved_hashes=None):
    case = preflight(repo=Path(repo), case_id=case_id)
    batch = ordinary(batch, required=False)
    expected = inheritance(source, e03, e04, e05, approved_hashes=approved_hashes)
    current = inspect_child(source, e03, e04, e05, batch, expected, approved_hashes=approved_hashes)
    if current is not None and case_id in read_started_cases(batch):
        fail('This E06 case is already claimed; no implicit rerun is permitted.')
    if (batch / case_id).exists() or (batch / (case_id + '-model-trace')).exists():
        fail('This E06 case already has artifacts; no implicit rerun is permitted.')
    if current and current['stopped']:
        fail('The E06 ledger has a reserved or unknown call; further cases are stopped.')
    if current:
        require_calls_available(current)
    return case, {'status': 'preflight_passed', 'round_id': 'e06', 'case_id': case_id,
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
    case, summary = preflight_contract(source=SOURCE, e03=E03, e04=E04, e05=E05, batch=BATCH,
                                       case_id=args.case, approved_hashes=APPROVED_HASHES)
    if not args.live:
        emit(summary)
        return 0
    if args.api_key_file is None:
        parser.error('--live requires an explicitly supplied local --api-key-file')
    # Invalid/missing credentials create no E06 ledger, trace, or initialization marker.
    try:
        key = args.api_key_file.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        parser.error('The explicitly supplied credential file could not be read')
    if (len(key) < 8 or not key.isascii() or any(c.isspace() for c in key)
            or key.startswith(('"', "'"))):
        parser.error('The key file must contain one credential without whitespace or quotes')
    budget = open_contract_budget(SOURCE, E03, E04, E05, BATCH, approved_hashes=APPROVED_HASHES)
    if budget.snapshot()['stopped']:
        fail('The E06 ledger is stopped; no provider was created.')
    budget.claim_case(args.case)
    from .deepseek_probe import DeepSeekProbeProvider
    trace = BATCH / (args.case + '-model-trace')
    trace.mkdir(parents=False, exist_ok=False)
    provider = DeepSeekProbeProvider(api_key=key, trace_directory=trace, budget=budget)
    try:
        outcome, error = run_case(case, BATCH / args.case, provider,
                                  answer_callback=wait_for_answer, profile='e06-contract-analysis-v1',
                                  trial_number=TRIALS[args.case], round_id='e06',
                                  review_before_final=False)
    finally:
        emit({'event': 'batch_budget', 'budget': budget.snapshot()})
    return 0 if outcome['state']['status'] == 'completed' and error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
