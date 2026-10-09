"""Explicit Python-only export and current-business offline CLI test launcher."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess

PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'


def export(root, destination):
    module = runpy.run_path(str(root / 'tests/export_cli_model_progress_fixture.py'))
    manifest = module['export'](root, destination)
    for path in sorted((root / 'tests').rglob('*.py')):
        if path.is_symlink() or any(p.startswith('.') for p in path.relative_to(root).parts):
            continue
        name = str(path.relative_to(root))
        data = path.read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    manifest['source_signature'] = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    # Both existing bootstraps verify loaded bytes: one uses files, the current
    # long-command bootstrap uses a flat allowlist. Supply both without relaxing either.
    manifest.update(manifest['files'])
    (destination / 'export_manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--create', action='store_true')
    args, tests = parser.parse_known_args()
    if args.create:
        export(Path(__file__).resolve().parents[1], args.export)
    for path in (args.export, args.results):
        if not path.resolve().is_relative_to(Path('/tmp')):
            raise ValueError('Fixture paths must be below /tmp')
    manifest = json.loads((args.export / 'export_manifest.json').read_text())
    assert all(hashlib.sha256((args.export / n).read_bytes()).hexdigest() == sha for n, sha in manifest['files'].items())
    args.results.mkdir(parents=True, exist_ok=True)
    scratch = args.results / 'scratch'
    scratch.mkdir(exist_ok=True)
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'TMPDIR': str(scratch),
        'BIOMIND_OFFLINE_RESULTS': str(args.results / 'pytest-results.json'),
        'BIOMIND_CLI_PROGRESS_ARTIFACTS': str(args.results), 'BIOMIND_GER_ARTIFACTS': str(args.results),
        'BIOMIND_EXEC_LOG_DIR': str(scratch / 'executor-logs')}
    # Host suppresses socketpair wakeups; the existing test-only pipe driver is
    # event-driven. Production timers and policies are untouched.
    code = ('import sys,runpy; root=sys.argv[1]; sys.path.insert(0,root); '
            'from tests.cli_repair_support import install_pipe_wakeup; install_pipe_wakeup(); '
            'sys.argv=[root+"/tests/longrun_offline_bootstrap.py",*sys.argv[1:]]; '
            'runpy.run_path(sys.argv[0],run_name="__main__")')
    command = [PYTHON, '-c', code, str(args.export), *tests]
    (args.results / 'invocation.json').write_text(json.dumps({'command': command, 'environment': environment}, indent=2))
    with (args.results / 'stdout.txt').open('w') as output:
        result = subprocess.run(command, cwd=args.export, env=environment, stdout=output,
                                stderr=subprocess.STDOUT, timeout=360)
    print(json.dumps({'returncode': result.returncode, 'results': str(args.results)}))
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
