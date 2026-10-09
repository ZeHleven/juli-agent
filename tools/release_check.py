"""Check and export the explicit source distribution; never contacts a service."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT_FILES = frozenset({
    '.gitattributes', '.gitignore', 'AGENTS.md', 'README.md', 'LICENSE',
    'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md', 'SECURITY.md',
    'pyproject.toml', 'MANIFEST.in',
})
TREES = {
    'backend/src/juli': {'.py', '.html', '.css', '.js'},
    'docs': {'.md', '.json'}, 'scenarios': {'.md', '.json'},
    'docs/agent/dev-notes/assets': {'.svg'},
    'examples': {'.py', '.md', '.json'},
    'experiments': {'.py', '.md', '.json', '.txt'},
    'tests': {'.py'}, 'tools': {'.py'},
}
IGNORED = {'.git', '.local', '.venv', '__pycache__', '.pytest_cache',
           'build', 'dist', 'htmlcov', '.coverage'}
RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'model-key': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    'database-password': re.compile(r'\b(?:postgres(?:ql)?|mysql)://[^\s:@/]+:[^\s@<>]+@'),
    'author-local-path': re.compile(r'(?:[A-Za-z]:[/\\]Users[/\\][^\s]+|/h[o]me/[^/\s]+/)'),
}


class ReleaseError(ValueError):
    """The source candidate is not ready for export."""


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.lstat(), 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    )


def collect(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    findings: list[str] = []
    contents: dict[str, bytes] = {}
    local_links: list[tuple[str, Path]] = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        allowed_dirs = []
        for name in sorted(dirs):
            if name in IGNORED or name.endswith('.egg-info'):
                continue
            path = Path(folder) / name
            if _linked(path):
                findings.append(f'{path.relative_to(root).as_posix()}: linked-directory')
            else:
                allowed_dirs.append(name)
        dirs[:] = allowed_dirs
        for name in sorted(files):
            if name in IGNORED or name.endswith(('.pyc', '.pyo')):
                continue
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if _linked(path):
                findings.append(f'{relative}: linked-file')
                continue
            allowed = relative in ROOT_FILES or any(
                relative.startswith(prefix + '/') and path.suffix in extensions
                for prefix, extensions in TREES.items()
            )
            if not allowed:
                findings.append(f'{relative}: unexpected-file')
                continue
            data = path.read_bytes()
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                findings.append(f'{relative}: invalid-utf8')
                continue
            for rule, pattern in RULES.items():
                for match in pattern.finditer(text):
                    line = text.count('\n', 0, match.start()) + 1
                    findings.append(f'{relative}:{line}: {rule}')
            if path.suffix == '.py':
                try:
                    ast.parse(text, filename=relative)
                except SyntaxError as error:
                    findings.append(f'{relative}:{error.lineno}: python-syntax')
            if path.suffix == '.md':
                for link in re.findall(r'\]\(([^)]+)\)', text):
                    if '://' in link or link.startswith('#'):
                        continue
                    target = (path.parent / link.split('#')[0]).resolve()
                    if not target.is_relative_to(root) or not target.exists():
                        findings.append(f'{relative}: missing-or-external-local-link')
                    else:
                        local_links.append((relative, target))
            contents[relative] = data
    for relative, target in local_links:
        linked = target.relative_to(root).as_posix()
        if linked not in contents and not any(name.startswith(linked + '/') for name in contents):
            findings.append(f'{relative}: link-not-in-export')
    for required in sorted(ROOT_FILES - contents.keys()):
        findings.append(f'{required}: missing-required-file')
    if findings:
        raise ReleaseError('\n'.join(findings))
    return dict(sorted(contents.items()))


def manifest(contents: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}


def output_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    local = root.resolve() / '.local'
    if not path.is_relative_to(local) or path == local:
        raise ReleaseError('Outputs must stay inside the repository .local directory')
    if path.exists():
        raise ReleaseError('Output already exists; choose a new output name')
    return path


def write_archive(path: Path, contents: dict[str, bytes], version: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9._+-]+', version):
        raise ReleaseError('Invalid package version')
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, 'x', compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = ZipInfo(f'juli-agent-{version}/{name}', date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest')
    parser.add_argument('--archive')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        contents = collect(root)
        version = tomllib.loads(contents['pyproject.toml'].decode('utf-8'))['project']['version']
        manifest_path = output_path(root, args.manifest) if args.manifest else None
        archive_path = output_path(root, args.archive) if args.archive else None
        if manifest_path and manifest_path == archive_path:
            raise ReleaseError('Manifest and archive need distinct output paths')
        if archive_path:
            write_archive(archive_path, contents, version)
        if manifest_path:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with manifest_path.open('x', encoding='utf-8') as handle:
                json.dump(manifest(contents), handle, indent=2)
                handle.write('\n')
    except (ReleaseError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'passed', 'files': len(contents), 'version': version,
                      'scope': 'working-tree source files; not a full security audit'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
