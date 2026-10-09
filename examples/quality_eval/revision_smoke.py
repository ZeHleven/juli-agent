"""Explicit E04 trials under the unchanged cumulative CNY 5 / 36-call cap.

Preflight is read-only. Live initialization writes an exclusive marker *outside*
the E04 directory before creating any child state. A crash after that write, or
loss of either marker or child files, requires operator review, never automatic
reinitialization. Each mutation commits SQLite, then child state, then marker;
a crash between those writes fails closed on their fingerprint mismatch. Copies
must include both historical batches, the E04 batch, and its sibling marker.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from juli.core.common import DomainError

from . import continue_smoke as previous
from .continue_smoke import ordinary, read_ledger, read_json, sha, money, locked
from .smoke import ROOT, CASES, preflight, run_case, wait_for_answer, emit, save_json

SOURCE = previous.SOURCE
PREVIOUS = previous.BATCH
BATCH = ROOT / '.local/quality-eval/v1/e04-deepseek-flash-20261002'
TOTAL_MICROS, TOTAL_CALLS = 5_000_000, 36
MANIFEST, STATE = 'inheritance.json', 'budget-state.json'
TRIALS = {'career-01': 3, 'career-02': 2, 'tender-03': 2}
# These four byte fingerprints are the completed, approved E02/E03 evidence.
# Synthetic tests pass their own ledger paths without this live-only pin set.
APPROVED_HASHES = {
    'e02/budget/budget.sqlite3': '95abeb9f97a2dd5a2e97d7f89fc85aaee7412eaad28e2f90e50f926b21f0c247',
    'e03/budget/budget.sqlite3': 'b3f6edb7cfb0f1119f95ac6d52cd9f1945dae381507edc98eb953150207d14bb',
    'e03/inheritance.json': '22d2a9c880b0e633b81d28ebd84db6e87f47837c6c51e2637f6ab46b3f7da2b4',
    'e03/budget-state.json': '638c6098d8e88a6a289ac960fa05450441433e4059b34e125b8c03ad3dd96d3f',
}


def fail(message):
    raise DomainError('revision_budget_invalid', message)


def marker_path(batch):
    batch = ordinary(batch, required=False)
    return ordinary(batch.parent / ('.' + batch.name + '.initialized.json'), required=False)


def inheritance(source, prior, *, approved_hashes=None):
    """Validate both historical rounds, then derive a separate E04 authorization."""
    source, prior = ordinary(source), ordinary(prior, required=False)
    paths = {'e02/budget/budget.sqlite3': source,
             'e03/budget/budget.sqlite3': prior / 'budget/budget.sqlite3',
             'e03/inheritance.json': prior / MANIFEST,
             'e03/budget-state.json': prior / STATE}
    before = {name: sha(ordinary(path)) for name, path in paths.items()}
    if approved_hashes is not None and before != approved_hashes:
        fail('Approved historical E02/E03 evidence changed; no new trial is permitted.')
    if (prior / '.budget-operation.lock').exists():
        fail('Historical E03 has an active or interrupted budget operation.')
    old = read_ledger(source)
    legacy_manifest = previous.inheritance(source)
    recent = previous.verify_existing(source, prior, legacy_manifest)
    if recent['stopped']:
        fail('Historical E03 has an unresolved call; further trials are stopped.')
    if before != {name: sha(ordinary(path)) for name, path in paths.items()}:
        fail('Historical evidence changed during inspection.')
    committed = old['committed_micros'] + recent['committed_micros']
    reserved = old['reserved_micros'] + recent['reserved_micros']
    calls = old['calls'] + recent['calls']
    remaining = TOTAL_MICROS - committed - reserved
    if remaining <= 0 or calls >= TOTAL_CALLS:
        fail('No cumulative authorization remains for E04.')
    return {'version': 1, 'round_id': 'e04', 'source_rounds': ['e02', 'e03'],
            'source_sha256': before,
            'total_ceiling_cny': money(TOTAL_MICROS), 'total_max_calls': TOTAL_CALLS,
            'source_committed_upper_bound_cny': money(committed),
            'source_unresolved_reserved_cny': money(reserved), 'source_calls': calls,
            'child_ceiling_cny': money(remaining), 'child_max_calls': TOTAL_CALLS - calls,
            'user_approved_new_trial': True, 'approved_cases': list(CASES), 'trials': TRIALS,
            'cost_basis': 'conservative_upper_bounds_not_actual_bill',
            'unresolved_source_calls_released': False}


def marker_value(manifest, state=None):
    return {'version': 1, 'round_id': 'e04',
            'status': 'ready' if state is not None else 'initializing',
            'inheritance': manifest, 'child_state': state}


def verify_existing(source, prior, batch, expected, *, approved_hashes=None):
    if inheritance(source, prior, approved_hashes=approved_hashes) != expected:
        fail('Fixed historical inheritance changed.')
    if read_json(batch / MANIFEST) != expected:
        fail('E04 inheritance manifest changed.')
    current = read_ledger(batch / 'budget/budget.sqlite3')
    if read_json(batch / STATE) != current:
        fail('E04 ledger changed outside the revision runner; no reset is allowed.')
    if read_json(marker_path(batch)) != marker_value(expected, current):
        fail('E04 external initialization marker is incomplete or changed.')
    if (money(current['ceiling_micros']) != expected['child_ceiling_cny']
            or current['max_calls'] != expected['child_max_calls']):
        fail('E04 child limits do not match the cumulative authorization.')
    return current


def save_state(batch, manifest):
    state = read_ledger(batch / 'budget/budget.sqlite3')
    temporary = batch / 'budget-state.next.json'
    save_json(temporary, state)
    temporary.replace(batch / STATE)
    marker = marker_path(batch)
    temporary_marker = marker.with_name(marker.name + '.next')
    save_json(temporary_marker, marker_value(manifest, state))
    temporary_marker.replace(marker)


class RevisionBudget:
    """Recheck immutable parents and all child fingerprints for every operation."""

    def __init__(self, source, prior, batch, manifest, approved_hashes=None):
        self.source, self.prior, self.batch = Path(source), Path(prior), Path(batch)
        self.manifest, self.approved_hashes = manifest, approved_hashes

    def _verify(self):
        return verify_existing(self.source, self.prior, self.batch, self.manifest,
                               approved_hashes=self.approved_hashes)

    def snapshot(self):
        with locked(self.batch):
            state = self._verify()
        source_committed = self.manifest['source_committed_upper_bound_cny']
        source_reserved = self.manifest['source_unresolved_reserved_cny']
        return {'ceiling_cny': money(state['ceiling_micros']), 'max_calls': state['max_calls'],
                'calls': state['calls'], 'committed_cny': money(state['committed_micros']),
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


def inspect_child(source, prior, batch, expected, *, approved_hashes=None):
    """No locks are created by read-only inspection; existing locks stop it."""
    marker = marker_path(batch)
    if batch.exists() != marker.exists():
        fail('E04 marker or batch is missing; do not recreate lost evaluation state.')
    if not batch.exists():
        return None
    if (batch / '.budget-operation.lock').exists():
        fail('Another or interrupted E04 operation holds the budget lock.')
    current = verify_existing(source, prior, batch, expected, approved_hashes=approved_hashes)
    if (batch / '.budget-operation.lock').exists():
        fail('E04 changed during read-only inspection.')
    return current


def open_revision_budget(source=SOURCE, prior=PREVIOUS, batch=BATCH, *, approved_hashes=None):
    source, prior, batch = (ordinary(source), ordinary(prior, required=False),
                            ordinary(batch, required=False))
    expected = inheritance(source, prior, approved_hashes=approved_hashes)
    inspect_child(source, prior, batch, expected, approved_hashes=approved_hashes)
    if batch.exists():
        with locked(batch):
            verify_existing(source, prior, batch, expected, approved_hashes=approved_hashes)
    else:
        # The historical rounds require their parent directory to already exist.
        # Exclusive marker creation serializes competing initializers. It is never
        # deleted on failure, including a failed batch mkdir or partial JSON write.
        save_json(marker_path(batch), marker_value(expected))
        batch.mkdir(parents=False, exist_ok=False)
        with locked(batch):
            save_json(batch / MANIFEST, expected)
            from .deepseek_probe import BatchBudget
            BatchBudget(batch / 'budget', max_cny=expected['child_ceiling_cny'],
                        max_calls=expected['child_max_calls'])
            save_state(batch, expected)
    return RevisionBudget(source, prior, batch, expected, approved_hashes)


def preflight_revision(*, repo=ROOT, source=SOURCE, prior=PREVIOUS, batch=BATCH,
                       case_id='career-01', approved_hashes=None):
    case = preflight(repo=Path(repo), case_id=case_id)
    batch = ordinary(batch, required=False)
    expected = inheritance(source, prior, approved_hashes=approved_hashes)
    current = inspect_child(source, prior, batch, expected, approved_hashes=approved_hashes)
    if (batch / case_id).exists() or (batch / (case_id + '-model-trace')).exists():
        fail('This E04 case already has artifacts; no implicit rerun is permitted.')
    if current and current['stopped']:
        fail('The E04 ledger has a reserved or unknown call; further cases are stopped.')
    return case, {'status': 'preflight_passed', 'round_id': 'e04', 'case_id': case_id,
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
    case, summary = preflight_revision(source=SOURCE, prior=PREVIOUS, batch=BATCH,
                                       case_id=args.case, approved_hashes=APPROVED_HASHES)
    if not args.live:
        emit(summary)
        return 0
    if args.api_key_file is None:
        parser.error('--live requires an explicitly supplied local --api-key-file')
    # Invalid/missing credentials create no E04 ledger, trace, or initialization marker.
    try:
        key = args.api_key_file.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        parser.error('The explicitly supplied credential file could not be read')
    if (len(key) < 8 or not key.isascii() or any(c.isspace() for c in key)
            or key.startswith(('"', "'"))):
        parser.error('The key file must contain one credential without whitespace or quotes')
    budget = open_revision_budget(SOURCE, PREVIOUS, BATCH, approved_hashes=APPROVED_HASHES)
    if budget.snapshot()['stopped']:
        fail('The E04 ledger is stopped; no provider was created.')
    from .deepseek_probe import DeepSeekProbeProvider
    trace = BATCH / (args.case + '-model-trace')
    trace.mkdir(parents=False, exist_ok=False)
    provider = DeepSeekProbeProvider(api_key=key, trace_directory=trace, budget=budget)
    try:
        outcome, error = run_case(case, BATCH / args.case, provider,
                                  answer_callback=wait_for_answer, profile='e04-native-analysis-v1',
                                  trial_number=TRIALS[args.case], round_id='e04')
    finally:
        emit({'event': 'batch_budget', 'budget': budget.snapshot()})
    return 0 if outcome['state']['status'] == 'completed' and error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
