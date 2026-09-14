"""Bounded audit-only diagnostic launcher; unchanged offline guard, new paths."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'


def child():
    import faulthandler
    import os
    source = Path(sys.argv[2]).resolve()
    assert source.is_relative_to(Path('/tmp'))
    handle = open(Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / 'early-stack.txt', 'w')
    faulthandler.enable(file=handle)
    faulthandler.dump_traceback_later(30, file=handle, repeat=True)
    sys.path.insert(0, str(source / 'tests'))
    from cli_model_progress_support import guarded_child
    sys.argv = [str(source / 'tests/cli_model_progress_support.py'), '--guarded-child', str(source), *sys.argv[3:]]
    guarded_child()


def main():
    if sys.argv[1:2] == ['--child']:
        child()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('export', type=Path)
    parser.add_argument('results', type=Path)
    args, tests = parser.parse_known_args()
    source = args.export.resolve()
    results = args.results.resolve()
    assert source.is_relative_to(Path('/tmp')) and results.is_relative_to(Path('/tmp'))
    assert not results.exists()
    manifest = json.loads((source / 'export_manifest.json').read_text())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest, name
    results.mkdir(parents=True)
    scratch = results / 'scratch'
    scratch.mkdir()
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'TMPDIR': str(scratch), 'BIOMIND_WIKI_ENABLED': '0', 'LOG_LEVEL': 'CRITICAL',
        'BIOMIND_OFFLINE_RESULTS': str(results / 'pytest-results.json'), 'BIOMIND_GER_ARTIFACTS': str(results),
        'BIOMIND_CLI_PROGRESS_ARTIFACTS': str(results), 'BIOMIND_EXEC_LOG_DIR': str(scratch / 'executor-logs')}
    command = [PYTHON, str(Path(__file__).resolve()), '--child', str(source), *tests]
    record = {'command': command, 'environment': environment, 'guard': 'unchanged cli_model_progress_support.guarded_child + offline_managed_bootstrap', 'timeout_seconds': 120}
    with (results / 'stdout.txt').open('w') as output:
        try:
            record['returncode'] = subprocess.run(command, cwd=source, env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=120).returncode
        except subprocess.TimeoutExpired:
            record['timeout'] = True
    (results / 'invocation.json').write_text(json.dumps(record, indent=2))
    print(json.dumps(record), flush=True)
    raise SystemExit(record.get('returncode', 124))


if __name__ == '__main__':
    main()
