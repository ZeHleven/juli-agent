"""Validate synthetic eval inputs and grader evidence without running an agent."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from juli.adapters.document_parsers import parse_document
from juli.adapters.scenario_files import load_scenario
from juli.bootstrap import tool_contracts
from juli.core.scenarios import ScenarioRegistry


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def resolve_file(root, relative):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise ValueError('Non-canonical relative path')
    if any(part in ('', '.', '..') for part in relative.split('/')):
        raise ValueError('Non-canonical relative path')
    path = root / relative
    if any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
           for part in (path, *path.parents) if part != root.parent):
        raise ValueError('Linked evaluation path')
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError('Missing or external evaluation file: ' + relative)
    return resolved


def inspect(repo, suite):
    config = read_json(suite/'suite.json')
    cases = config['cases']
    fragment_cases = []
    for domain in ('career', 'tender', 'course'):
        fragment_cases.extend(read_json(suite/'fragments'/f'{domain}.json'))
    assert sorted(fragment_cases,key=lambda c:c['id']) == sorted(cases,key=lambda c:c['id']), 'Case fragments differ from suite'
    if config['status'] != 'prepared_not_run':
        raise ValueError('Preparation manifest must not claim a model run')
    ids = [case['id'] for case in cases]
    assert len(ids) == len(set(ids)) == 16, 'Expected 12 base cases and 4 paired variants'
    base_conditions = ('sufficient', 'missing', 'conflicting', 'long_context')
    assert Counter((c['domain'], c['condition']) for c in cases if 'pair' not in c) == Counter(
        (domain, condition) for domain in ('career', 'tender', 'course') for condition in base_conditions)
    assert sum(c['split']=='reserved_validation' for c in cases) == 4
    assert all(c['split']=='reserved_validation' for c in cases if c['domain']=='course')
    case_map = {c['id']: c for c in cases}
    evidence_index = {}
    manifest = {}
    long_lengths = {}
    registry = ScenarioRegistry(tool_contracts())
    packages = {}
    for case in cases:
        cid = case['id']
        assert case['as_of_date'] == '2026-10-02'
        assert isinstance(case['question'], str) and case['question'].strip()
        scenario = resolve_file(repo, case['scenario_path']+'/scenario.json').parent
        if case['scenario_path'] not in packages:
            package = load_scenario(scenario)
            registry.register(package)
            packages[case['scenario_path']] = package
        package = packages[case['scenario_path']]
        docs = case['documents']
        assert len({d['id'] for d in docs}) == len(docs), cid
        counts = Counter(d['role'] for d in docs)
        roles = {r.name: r for r in package.manifest.resource_roles}
        assert set(counts) <= set(roles), cid
        assert all(r.min_count <= counts[name] <= r.max_count for name,r in roles.items()), cid
        assert len(docs) <= package.manifest.max_resources
        texts = {}
        for doc in docs:
            assert doc['path'].startswith('inputs/'+cid+'/'), cid
            path = resolve_file(suite, doc['path'])
            assert path.suffix == '.md', 'Round 1 uses text fixtures only'
            raw = path.read_bytes()
            text = raw.decode('utf-8')
            assert parse_document(path.name, raw).text == text, 'Parser changed input text'
            texts[doc['id']] = text
        gold = read_json(resolve_file(suite, case['gold_path']))
        assert case['gold_path'] == 'grader/'+cid+'.json'
        assert gold['case_id']==cid
        assert gold['allowed_conclusions'] and gold['forbidden_conclusions']
        points = gold['required_findings']
        assert 4 <= len(points) <= 10, cid
        assert len({point['id'] for point in points}) == len(points)
        clar = gold['clarification']
        assert type(clar['required']) is bool
        assert clar['after_answer_expectation']
        if clar['required']:
            assert clar['topics'] and clar['answer_facts'], cid
        assert all(a['source']=='user_statement' for a in clar['answer_facts'])
        index = []
        for point in points:
            assert type(point['critical']) is bool and point['statement']
            refs = []
            for source in point['evidence']:
                text = texts[source['document_id']]
                quote = source['quote']
                assert quote and text.count(quote)==1, (cid, point['id'], 'quote must match once')
                start = text.index(quote)
                doc = next(d for d in docs if d['id']==source['document_id'])
                refs.append({'document_id':doc['id'],'path':doc['path'],
                    'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'char_start':start,'char_end':start+len(quote),
                    'line_start':text.count('\n',0,start)+1,
                    'line_end':text.count('\n',0,start+len(quote))+1,'quote':quote})
            index.append({'finding_id':point['id'],'sources':refs,
                          'no_direct_quote':not bool(refs)})
        evidence_index[cid] = index
        if case['condition']=='long_context':
            longest = max(len(text) for text in texts.values())
            assert longest > 4500, cid
            assert any(ref['char_start'] > len(texts[ref['document_id']])//2
                       for point in index for ref in point['sources']), (cid,'no late evidence')
            long_lengths[cid] = longest
        if 'pair' in case:
            pair = case['pair']
            base = case_map[pair['base_case']]
            assert base['condition']=='sufficient' and base['domain']==case['domain']
            assert pair['relation'] in ('changes_conclusion','preserves_conclusion')
            assert pair['description']
            assert case['question']==base['question'], 'Paired question changed'
            assert case['scenario_path']==base['scenario_path'], 'Paired scenario changed'
            assert set(d['id'] for d in docs)==set(d['id'] for d in base['documents'])
            changed = []
            for doc in docs:
                old = next(d for d in base['documents'] if d['id']==doc['id'])
                assert doc['role']==old['role'] and doc['display_name']==old['display_name']
                if resolve_file(suite,doc['path']).read_bytes()!=resolve_file(suite,old['path']).read_bytes():
                    changed.append(doc['id'])
            assert set(changed) <= {pair['changed_document_id']}, (cid,changed)
            assert changed or [d['id'] for d in docs]!=[d['id'] for d in base['documents']], 'Pair has no change'
    manifest['cases'] = evidence_index
    manifest['scenario_fingerprints'] = {path:p.fingerprint for path,p in packages.items()}
    manifest['positions'] = 'UTF-8 decoded text; zero-based Unicode character offsets; one-based lines; not runtime evidence IDs'
    report = {'status':'fixture_checks_passed','base_cases':12,'paired_variants':4,
        'input_documents':sum(len(c['documents']) for c in cases),
        'grading_findings':sum(len(v) for v in evidence_index.values()),
        'located_evidence_quotes':sum(len(p['sources']) for v in evidence_index.values() for p in v),
        'long_documents_characters':long_lengths,'scenario_packages':len(packages),
        'model_runs':0,'model_quality_score':None,'scope':'fixture structure, exact source quotes, text parsing, scenario compatibility; not model quality'}
    return manifest, report


def snapshots(repo, suite):
    files = [p for p in suite.rglob('*') if p.is_file() and p.name not in {'freeze.json','fixture-checks.json'}]
    for relative in ('scenarios/career-fit', 'scenarios/tender-review'):
        files += [p for p in (repo/relative).iterdir() if p.is_file()]
    return {p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true', help='Create initial review index and hashes; refuses to replace an existing freeze')
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    suite=repo/'examples/quality_eval/v1'
    try:
        index, report=inspect(repo,suite)
        if args.freeze:
            assert not (suite/'freeze.json').exists(), 'Already frozen; create a new suite version for revisions'
            (suite/'grader/evidence-index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            (suite/'fixture-checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            frozen={'date':'2026-10-02','source_commit':'2c54675e2d0325ea91676b3adfb9d83b987a80f1','files':snapshots(repo,suite)}
            (suite/'freeze.json').write_text(json.dumps(frozen,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        else:
            assert index==read_json(suite/'grader/evidence-index.json'), 'Source index changed'
            assert report==read_json(suite/'fixture-checks.json'), 'Fixture report changed'
            assert snapshots(repo,suite)==read_json(suite/'freeze.json')['files'], 'Frozen files changed'
    except (AssertionError, KeyError, ValueError, OSError) as error:
        print(str(error),file=sys.stderr)
        return 1
    print(json.dumps(report,ensure_ascii=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
