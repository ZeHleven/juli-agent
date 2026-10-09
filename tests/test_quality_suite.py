"""The preparation checker validates fixtures, never agent quality."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('quality_checker', ROOT/'tools/check_quality_suite.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def copy_suite(tmp_path):
    relative = Path('examples/quality_eval/v1')
    shutil.copytree(ROOT/relative, tmp_path/relative)
    for name in ('career-fit','tender-review'):
        shutil.copytree(ROOT/'scenarios'/name, tmp_path/'scenarios'/name)
    return tmp_path, tmp_path/relative


def rewrite(path, change):
    value = json.loads(path.read_text(encoding='utf-8'))
    change(value)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def test_prepared_cases_have_valid_evidence_and_no_model_score(copy_suite):
    repo,suite = copy_suite
    index,report = checker.inspect(repo,suite)
    assert report['base_cases']==12 and report['paired_variants']==4
    assert report['model_runs']==0 and report['model_quality_score'] is None
    assert len(index['cases'])==16


def test_wrong_evidence_quote_is_rejected(copy_suite):
    repo,suite = copy_suite
    rewrite(suite/'grader/course-01.json',lambda g:g['required_findings'][0]['evidence'][0].update(quote='此句不在原始资料中'))
    with pytest.raises(AssertionError,match='quote must match once'):
        checker.inspect(repo,suite)


def test_answer_file_cannot_be_used_as_an_input(copy_suite):
    repo,suite = copy_suite
    def replace(cases):
        next(c for c in cases if c['id']=='course-01')['documents'][0]['path']='grader/course-01.json'
    rewrite(suite/'suite.json',lambda s:replace(s['cases']))
    rewrite(suite/'fragments/course.json',replace)
    with pytest.raises(AssertionError):
        checker.inspect(repo,suite)


def test_pair_question_must_be_held_constant(copy_suite):
    repo,suite = copy_suite
    def replace(cases):
        next(c for c in cases if c['id']=='career-05')['question']='请一定拒绝这个岗位。'
    rewrite(suite/'suite.json',lambda s:replace(s['cases']))
    rewrite(suite/'fragments/career.json',replace)
    with pytest.raises(AssertionError,match='Paired question changed'):
        checker.inspect(repo,suite)


def test_frozen_hashes_detect_input_changes(copy_suite):
    repo,suite = copy_suite
    before = checker.snapshots(repo,suite)
    path=suite/'inputs/course-01/background.md'
    path.write_text(path.read_text(encoding='utf-8')+'\n附加一段新资料。\n',encoding='utf-8')
    assert checker.snapshots(repo,suite)!=before
