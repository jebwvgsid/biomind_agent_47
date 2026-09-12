"""Persistent fatal diagnostics with original guarded capture and 180s limit."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from cli_model_progress_support import launch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--selftest', action='store_true')
    args, tests = parser.parse_known_args()
    if args.results.exists():
        raise SystemExit('Refusing to overwrite existing evidence')
    if args.selftest:
        if tests:
            raise SystemExit('Self-test accepts no extra tests')
        tests = ['-q', 'tests/test_cli_fatal_outlet.py']
    else:
        tests = tests or ['-q', 'tests/test_plan_results_name.py',
                         'tests/test_plan_results_evidence.py', 'tests/test_plan_results_managed.py']
    if '-s' in tests or any(t.startswith('--capture') for t in tests):
        raise SystemExit('This diagnostic keeps the original default capture')
    manifest = json.loads((args.export / 'export_manifest.json').read_text())
    start = time.monotonic(); code = None; error = None
    try:
        code = launch(args.export, args.results, tests, fatal_diagnostics=True,
                      fatal_selftest=args.selftest)
    except subprocess.TimeoutExpired as exc:
        error = {'type': type(exc).__name__, 'timeout': exc.timeout,
                 'status': 'child killed by original runner timeout; tests incomplete'}
    rows_path = args.results / 'pytest-results.json'
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    fatal_path = args.results / 'fatal.txt'
    fatal = fatal_path.read_text() if fatal_path.exists() else ''
    receipt = {'returncode': code, 'error': error, 'selftest': args.selftest,
               'elapsed_seconds': time.monotonic()-start, 'source': manifest['source'],
               'source_signature': manifest['source_signature'],
               'call_passed': sum(r.get('phase') == 'call' and r.get('outcome') == 'passed' for r in rows),
               'call_failed': sum(r.get('phase') == 'call' and r.get('outcome') == 'failed' for r in rows),
               'last_events': rows[-4:], 'fatal_stack_present': 'Fatal Python error:' in fatal,
               'fatal_sha256': hashlib.sha256(fatal_path.read_bytes()).hexdigest() if fatal_path.exists() else None,
               'parent_source_drift': [p for p,h in manifest['files'].items()
                                       if hashlib.sha256((args.export/p).read_bytes()).hexdigest()!=h]}
    (args.results/'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))
    if args.selftest:
        assert code == -11 and receipt['fatal_stack_present']
        assert 'test_synthetic_fatal_outlet' in fatal
    elif error or code:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
