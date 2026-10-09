#!/usr/bin/env python3
"""Build/verify immutable BioMind candidates and select a default for NEW workers.

This tool never edits an existing release, user settings, sessions, plugins, or
the installed user launcher. Select/rollback only changes the supplied link.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REL = Path('integrations/pi_runtime')


def digest(file: Path) -> str:
    result = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def local(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('RELEASE_INVALID_PATH')
    result = root / rel
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('RELEASE_PATH_ESCAPES_ROOT')
    return result


def inventory(root: Path) -> list[dict]:
    rows = []
    for file in sorted(root.rglob('*')):
        if file.is_symlink():
            if not file.resolve().is_relative_to(root.resolve()):
                raise ValueError('DEPENDENCY_SYMLINK_ESCAPES_RELEASE')
            rows.append({'path': file.relative_to(root).as_posix(), 'symlink': os.readlink(file)})
        elif file.is_file():
            rows.append({'path': file.relative_to(root).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)})
    return rows


def read_manifest(root: Path) -> dict:
    manifest = json.loads((root / RUNTIME_REL / 'dist/build-manifest.json').read_text())
    if manifest.get('schemaVersion') != 1 or not isinstance(manifest.get('sources'), list):
        raise ValueError('RELEASE_MANIFEST_INVALID')
    return manifest


def verify(root: Path) -> dict:
    root = root.resolve(strict=True)
    manifest = read_manifest(root)
    receipt = json.loads((root / 'RELEASE.json').read_text())
    runtime = root / RUNTIME_REL
    if receipt['releaseId'] != manifest['buildId'] or digest(runtime / 'dist/build-manifest.json') != receipt['manifestSha256']:
        raise ValueError('RELEASE_IDENTITY_MISMATCH')
    for rows, base in [(manifest['sources'], root), (manifest['artifacts'], runtime)]:
        for item in rows:
            if digest(local(base, item['path'])) != item['sha256']:
                raise ValueError('RELEASE_CONTENT_MISMATCH: ' + item['path'])
    deps = runtime / 'node_modules'
    saved_inventory = json.loads((root / 'DEPENDENCIES.json').read_text())
    if digest(root / 'DEPENDENCIES.json') != receipt['dependenciesInventorySha256'] or inventory(deps) != saved_inventory:
        raise ValueError('RELEASE_DEPENDENCIES_MISMATCH')
    return {'result': 'PASS', 'releaseId': manifest['buildId'], 'root': str(root),
            'sourceFingerprint': manifest['sourceFingerprint'], 'sourceCommit': manifest.get('sourceCommit'),
            'sourceFiles': len(manifest['sources']), 'artifactFiles': len(manifest['artifacts']),
            'dependencyEntries': len(saved_inventory), 'modelStarted': False}


def build(output: Path, node: Path, python: Path, skip_compile: bool = False) -> dict:
    runtime = ROOT / RUNTIME_REL
    node = node.resolve(strict=True)
    python = python.resolve(strict=True)
    env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get('PATH', ''),
               GOMAXPROCS='2', UV_THREADPOOL_SIZE='2')
    def run(args):
        subprocess.run([str(node), *args], cwd=runtime, env=env, check=True)
    if not skip_compile:
        guard = runtime / 'compat/apply-plugin-guard.mjs'
        if guard.exists():
            run(['compat/apply-plugin-guard.mjs', '--check'])
        else:
            run(['compat/apply-pi-observer.mjs', '--check'])
        run(['--max-old-space-size=1024', 'node_modules/typescript/bin/tsc', '-p', 'tsconfig.json'])
        run(['build-native-extension.mjs'])
        run(['build-manifest.mjs'])
    manifest = read_manifest(ROOT)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ('biomind-' + manifest['buildId'][:16])
    if destination.exists():
        result = verify(destination)
        return {**result, 'reused': True}
    # An interrupted build leaves a clearly unpublished directory for inspection.
    stage = output / ('.incomplete-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    for rows, source_base, target_base in [(manifest['sources'], ROOT, stage),
                                          (manifest['artifacts'], runtime, stage / RUNTIME_REL)]:
        for item in rows:
            source = local(source_base, item['path'])
            if digest(source) != item['sha256']:
                raise ValueError('BUILD_SOURCE_CHANGED: ' + item['path'])
            target = local(target_base, item['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    target_runtime = stage / RUNTIME_REL
    shutil.copy2(runtime / 'dist/build-manifest.json', target_runtime / 'dist/build-manifest.json')
    shutil.copytree(runtime / 'node_modules', target_runtime / 'node_modules', symlinks=True)
    dependencies = inventory(target_runtime / 'node_modules')
    (stage / 'DEPENDENCIES.json').write_text(json.dumps(dependencies, indent=2) + '\n')
    node_version = subprocess.check_output([str(node), '--version'], text=True).strip()
    python_version = subprocess.check_output([str(python), '--version'], text=True).strip()
    receipt = {'schema': 'BioMindCoreReleaseV1', 'releaseId': manifest['buildId'],
               'builtAtUTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'sourceCommit': manifest.get('sourceCommit'), 'sourceFingerprint': manifest['sourceFingerprint'],
               'manifestSha256': digest(target_runtime / 'dist/build-manifest.json'),
               'dependenciesInventorySha256': digest(stage / 'DEPENDENCIES.json'),
               'node': {'path': str(node), 'version': node_version, 'sha256': digest(node)},
               'python': {'path': str(python), 'version': python_version},
               'externalDependencyBoundary': 'Node/npm and Python/conda environments; BioMind business code is contained in this release.',
               'engineUpgrade': False, 'sessionsIncluded': False, 'credentialsIncluded': False}
    (stage / 'RELEASE.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    verify(stage)
    # core is immutable to these tools; selecting a different release never
    # rewrites it. User resources remain outside and are not made read-only.
    for file in stage.rglob('*'):
        if not file.is_symlink() and file.is_file():
            file.chmod(file.stat().st_mode & ~0o222)
    stage.rename(destination)
    return {'result': 'PASS', 'root': str(destination), 'releaseId': manifest['buildId'],
            'sourceFingerprint': manifest['sourceFingerprint'], 'reused': False,
            'productionSelected': False, 'windowsAcceptance': 'NOT_RUN'}


def select(link: Path, release: Path) -> dict:
    release = release.resolve(strict=True)
    result = verify(release)
    link = link.absolute()
    if link.exists() and not link.is_symlink():
        raise ValueError('DEFAULT_SELECTOR_IS_NOT_A_SYMLINK')
    previous = str(link.resolve()) if link.is_symlink() else None
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name('.' + link.name + '.' + uuid.uuid4().hex)
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, link)
    return {**result, 'selector': str(link), 'previousRelease': previous,
            'affects': 'Only workers started through this selector after selection',
            'existingWorkersStopped': False, 'settingsOrPluginsRestored': False}


def run_release(selection: Path, args: list[str]) -> None:
    # Resolve once. Neither the Python entry nor later scripts resolve current
    # again, so a new default cannot change the code of this worker.
    root = selection.resolve(strict=True)
    receipt = json.loads((root / 'RELEASE.json').read_text())
    manifest = read_manifest(root)
    if receipt['releaseId'] != manifest['buildId']:
        raise ValueError('RELEASE_IDENTITY_MISMATCH')
    env = dict(os.environ, BIOMIND_PI_NODE=receipt['node']['path'],
               BIOMIND_PI_PYTHON=receipt['python']['path'])
    os.execve(receipt['python']['path'], [receipt['python']['path'], '-B', str(root / 'bio_agents.py'), *args], env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('build');p.add_argument('--output', type=Path, required=True);p.add_argument('--node', type=Path, required=True);p.add_argument('--python', type=Path, default=Path(sys.executable));p.add_argument('--skip-compile', action='store_true', help='Use an already built manifest; all listed source/artifact hashes must match')
    p = sub.add_parser('verify');p.add_argument('release', type=Path)
    p = sub.add_parser('select');p.add_argument('--link', type=Path, required=True);p.add_argument('--release', type=Path, required=True)
    p = sub.add_parser('status');p.add_argument('selection', type=Path)
    p = sub.add_parser('run');p.add_argument('selection', type=Path);p.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'build': result = build(args.output, args.node, args.python, args.skip_compile)
    elif args.command == 'verify': result = verify(args.release)
    elif args.command == 'select': result = select(args.link, args.release)
    elif args.command == 'run': run_release(args.selection, args.args[1:] if args.args[:1] == ['--'] else args.args); return 0
    else:
        root = args.selection.resolve(strict=True); result = {**json.loads((root / 'RELEASE.json').read_text()), 'root': str(root), 'scope': 'selected release; not evidence that existing workers changed'}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'result': 'FAIL', 'error': str(error)}), file=sys.stderr)
        raise SystemExit(2)
