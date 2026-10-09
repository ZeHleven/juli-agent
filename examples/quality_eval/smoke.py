"""Opt-in E02 smoke runner. No grader parsing, automatic answers or model retries."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from juli.adapters.document_parsers import parse_document
from juli.adapters.scenario_files import load_scenario
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.application.model_session import SessionPolicy
from juli.bootstrap import create_durable_runner
from juli.core.common import DomainError
from juli.core.resources import ResourceBinding
from juli.core.tasks import TaskBudget
from tools.check_quality_suite import resolve_file, snapshots

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / 'examples/quality_eval/v1'
CASES = ('career-01', 'career-02', 'tender-03')
BATCH = ROOT / '.local/quality-eval/v1/e02-deepseek-flash-20261002'
BATCH_CEILING_CNY = '5'
TASK_BUDGET = TaskBudget(max_actions=24, max_input_tokens=750000, max_output_tokens=49152)


def save_json(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def preflight(repo=ROOT, case_id='career-01'):
    if case_id not in CASES:
        raise ValueError('This batch permits only the three approved smoke cases')
    suite = repo / 'examples/quality_eval/v1'
    freeze = json.loads((suite/'freeze.json').read_text(encoding='utf-8'))
    if snapshots(repo, suite) != freeze['files']:
        raise ValueError('Frozen suite changed; do not silently change inputs or answers')
    cases = json.loads((suite/'suite.json').read_text(encoding='utf-8'))['cases']
    case = next(c for c in cases if c['id'] == case_id)
    selected = {k: case[k] for k in ('id', 'question', 'as_of_date', 'documents', 'scenario_path')}
    for doc in selected['documents']:
        raw = resolve_file(suite, doc['path']).read_bytes()
        if parse_document(doc['display_name']+'.md', raw).text != raw.decode('utf-8'):
            raise ValueError('Text parser changed approved document bytes')
    return selected


def source_hashes(repo):
    paths = list((repo/'backend/src/juli').rglob('*.py'))
    paths += list((repo/'examples/quality_eval').glob('*.py'))
    paths += [repo/'tools/check_quality_suite.py']
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


class RecordingGateway:
    def __init__(self, gateway, directory):
        self.inner, self.directory, self.count = gateway, directory, 0

    def granted(self, package):
        return self.inner.granted(package)

    def execute(self, package, proposal, *, timeout_seconds):
        self.count += 1
        prefix = self.directory / f'tool-{self.count:03d}'
        save_json(str(prefix)+'-request.json', proposal.model_dump(mode='json'))
        started = time.monotonic()
        try:
            value = self.inner.execute(package, proposal, timeout_seconds=timeout_seconds)
        except BaseException as error:
            save_json(str(prefix)+'-error.json', {'code': error.code if isinstance(error, DomainError) else type(error).__name__})
            raise
        save_json(str(prefix)+'-response.json', {'elapsed_seconds': time.monotonic()-started, 'value': value})
        return value


def run_case(case, directory, provider, *, repo=ROOT, answer_callback=None,
             profile='e02-deepseek-nonthinking-v1', trial_number=1, round_id='e02',
             review_before_final=False):
    """Run one new task; model credentials and the generated owner token stay in memory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    database = directory/'synthetic.sqlite3'
    migrate(database)  # Explicit migration of a new dedicated synthetic database.
    store = SQLiteStore(database)
    owner = store.provision_principal('E02 synthetic evaluator')
    token = owner.token
    space = store.create_workspace(token, 'E02 synthetic workspace').workspace_id
    package = load_scenario(repo/case['scenario_path'])
    bindings, uploaded = [], []
    inputs = directory/'inputs'
    inputs.mkdir()
    for index, doc in enumerate(case['documents'], 1):
        raw = resolve_file(repo/'examples/quality_eval/v1', doc['path']).read_bytes()
        filename = doc['display_name']+'.md'
        result = store.save_file(token, space, filename=filename, data=raw)
        resource = result['resource']
        if resource['status'] != 'ready':
            raise ValueError('Approved input failed file parsing')
        bindings.append(ResourceBinding(role=doc['role'], version_id=resource['version_id']))
        (inputs/f'document-{index:02d}.md').write_bytes(raw)
        uploaded.append({'document_id': doc['id'], 'display_name': filename, 'role': doc['role'],
                         'version_id': resource['version_id'], 'sha256': hashlib.sha256(raw).hexdigest(),
                         'parse': result['parse']})
    conversation = store.create_conversation(token, space)
    question = case['question'] + '\n判断日期：' + case['as_of_date']
    task = store.create_task(token, space, conversation.conversation_id, question=question,
        package=package, bindings=tuple(bindings), request_key='e02-one-trial', budget=TASK_BUDGET)
    task_id = task['snapshot']['task_id']
    policy = SessionPolicy(allow_external_egress=provider.external_egress,
        max_model_calls=12, max_output_per_call=4096, max_elapsed_seconds=300,
        review_before_final=review_before_final)
    save_json(directory/'run.json', {'case_id': case['id'], 'trial': trial_number, 'round_id': round_id,
        'mode': 'real_model_smoke' if provider.external_egress else 'offline_harness_test',
        'question': question, 'documents': uploaded, 'task_snapshot': task['snapshot'],
        'scenario_fingerprint': package.fingerprint, 'scenario_path': case['scenario_path'],
        'suite_freeze_sha256': hashlib.sha256((repo/'examples/quality_eval/v1/freeze.json').read_bytes()).hexdigest(),
        'source_hashes': source_hashes(repo), 'policy': policy.model_dump(mode='json'),
        'quality_verdict': None, 'automatic_answers': False})
    runner = create_durable_runner(store, token, space, task_id, provider,
        profile=profile, policy=policy)
    runner.gateway = RecordingGateway(runner.gateway, directory)
    sequence, answers, error_code = 0, 0, None
    started = time.monotonic()
    try:
        while True:
            view = runner.view()
            sequence += 1
            save_json(directory/f'state-{sequence:03d}.json', view)
            status = view['state']['status']
            if status == 'pending':
                answers += 1
                if answers > 3 or answer_callback is None:
                    break
                text = answer_callback(view, directory, answers)
                if text is None:
                    break
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise ValueError('Operator answer must be bounded nonempty text')
                save_json(directory/f'answer-{answers:03d}-submitted.json', {'question': view['state'], 'text': text})
                runner.answer(expected_version=view['state']['version'], request_key=f'e02-answer-{answers}', text=text)
            elif status == 'running':
                runner.step(expected_actions=view['usage']['actions'])
            else:
                break
    except BaseException as error:
        error_code = error.code if isinstance(error, DomainError) else type(error).__name__
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            error_code = 'operator_interrupted'
    outcome = runner.view()
    save_json(directory/'outcome.json', {'runtime': outcome, 'error_code': error_code,
        'wall_seconds': time.monotonic()-started, 'clarifications': answers, 'quality_verdict': None,
        'note': 'Runtime completion is not a semantic quality verdict. Review the full trace against the frozen rubric.'})
    emit({'case_id': case['id'], 'status': outcome['state']['status'], 'error_code': error_code,
          'usage': outcome['usage'], 'directory': str(directory)})
    return outcome, error_code


def wait_for_answer(view, directory, number, *, timeout=600):
    path = directory/f'answer-{number:03d}.json'
    emit({'event': 'clarification_pending', 'state': view['state'], 'answer_file': str(path),
          'instruction': 'Operator may write a JSON object with only a text field after reviewing the actual question.'})
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 20000:
                raise ValueError('Invalid operator answer file')
            try:
                value = json.loads(path.read_text(encoding='utf-8-sig'))
            except (UnicodeError, json.JSONDecodeError):
                time.sleep(.25)  # Allow an editor to finish its atomic/manual save.
                continue
            if set(value) != {'text'}:
                raise ValueError('Answer file must contain only text')
            return value['text']
        time.sleep(.25)
    emit({'event': 'operator_answer_timeout', 'status': 'pending', 'model_retry': False,
          'limitation': 'Process-local answering ended; this entry has no resume command. Preserve this run; do not delete it to retry.'})
    return None


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, default='career-01')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--api-key-file', type=Path)
    args = parser.parse_args()
    case = preflight(case_id=args.case)
    if not args.live:
        emit({'status': 'preflight_passed', 'case_id': args.case, 'model_calls': 0,
              'credentials_read': False, 'approved_batch': list(CASES), 'max_batch_cny': BATCH_CEILING_CNY})
        return 0
    if args.api_key_file is None:
        parser.error('--live requires an explicitly supplied local --api-key-file')
    if (BATCH/args.case).exists():
        parser.error('This case already has a run directory; no implicit rerun or overwrite')
    from .deepseek_probe import BatchBudget, DeepSeekProbeProvider
    from juli.adapters.chat_completions import ChatModelConfig
    key = args.api_key_file.read_text(encoding='utf-8-sig').strip()
    if not key or any(c.isspace() for c in key):
        parser.error('The selected key file must contain one nonempty credential without quotes')
    budget = BatchBudget(BATCH/'budget', max_cny=BATCH_CEILING_CNY, max_calls=36)
    trace = BATCH/(args.case+'-model-trace')
    trace.mkdir(parents=True, exist_ok=False)
    provider = DeepSeekProbeProvider(
        ChatModelConfig(endpoint='https://api.deepseek.com/chat/completions',
                        model='deepseek-v4-flash', context_window=64000),
        api_key=key, trace_directory=trace, budget=budget)
    try:
        outcome, error = run_case(case, BATCH/args.case, provider, answer_callback=wait_for_answer)
    finally:
        emit({'event': 'batch_budget', 'budget': budget.snapshot()})
    return 0 if outcome['state']['status'] == 'completed' and error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
