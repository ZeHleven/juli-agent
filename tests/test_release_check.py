"""Publication boundary tests use synthetic files, never local credentials."""
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location('release_check', Path(__file__).resolve().parents[1] / 'tools/release_check.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def candidate(tmp_path):
    for name in release.ROOT_FILES:
        (tmp_path / name).write_text('synthetic\n', encoding='utf-8')
    return tmp_path


def test_private_generated_data_is_excluded(candidate):
    local = candidate / '.local'
    local.mkdir()
    (local / '.env').write_text('sk-' + 'a' * 36, encoding='utf-8')
    (local / 'personal.sqlite3').write_bytes(b'synthetic database')
    contents = release.collect(candidate)
    assert set(contents) == release.ROOT_FILES


@pytest.mark.parametrize('relative', ['personal.sqlite3', '.env', 'docs/notes.pdf'])
def test_unexpected_files_block_export(candidate, relative):
    path = candidate / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic')
    with pytest.raises(release.ReleaseError, match='unexpected-file'):
        release.collect(candidate)


def test_credential_diagnostics_are_redacted(candidate):
    secret = 'sk-' + 'a' * 36
    (candidate / 'README.md').write_text('example\n' + secret, encoding='utf-8')
    with pytest.raises(release.ReleaseError) as error:
        release.collect(candidate)
    assert 'README.md:2: model-key' in str(error.value)
    assert secret not in str(error.value)


def test_archive_matches_validated_bytes_and_is_repeatable(candidate):
    (candidate / 'tools').mkdir()
    (candidate / 'tools/example.py').write_text('pass\n', encoding='utf-8')
    (candidate / 'README.md').write_text('[tools](tools)', encoding='utf-8')
    contents = release.collect(candidate)
    first, second = candidate / '.local/first.zip', candidate / '.local/second.zip'
    release.write_archive(first, contents, '0.1.0.dev7')
    release.write_archive(second, contents, '0.1.0.dev7')
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert {name.removeprefix('juli-agent-0.1.0.dev7/'): archive.read(name)
                for name in archive.namelist()} == contents
    with pytest.raises(FileExistsError):
        release.write_archive(first, contents, '0.1.0.dev7')


def test_output_cannot_escape_or_overwrite_source(candidate):
    for value in ['README.md', '../outside.zip', '.local/../../outside.zip']:
        with pytest.raises(release.ReleaseError):
            release.output_path(candidate, value)


def test_missing_policy_and_broken_links_block_export(candidate):
    (candidate / 'SECURITY.md').unlink()
    (candidate / 'README.md').write_text('[guide](missing.md)', encoding='utf-8')
    with pytest.raises(release.ReleaseError) as error:
        release.collect(candidate)
    assert 'SECURITY.md: missing-required-file' in str(error.value)
    assert 'missing-or-external-local-link' in str(error.value)
