"""Run one bounded final-candidate group with the existing offline guard.

The parent imports only safe test support. Each child gets the existing explicit
environment, a new scratch tree and an external 180-second watchdog. This file
adds receipts, not production outcomes or synthetic success state.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', dest='source', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args, targets = parser.parse_known_args()
    source, results = args.source.resolve(), args.results.resolve()
    assert source.is_relative_to(Path('/tmp')) and results.is_relative_to(Path('/tmp'))
    assert not results.exists(), 'new results only'
    manifest = json.loads((source / 'export_manifest.json').read_text())
    def verify():
        return [name for name, value in manifest['files'].items()
                if hashlib.sha256((source / name).read_bytes()).hexdigest() != value]
    assert not verify(), 'export changed before launch'
    sys.path.insert(0, str(source / 'tests'))
    from cli_model_progress_support import launch
    receipt = {'source_signature': manifest['source_signature'],
               'loaded_source_manifest': str(source / 'export_manifest.json'),
               'test_targets': targets, 'timeout_seconds': 180,
               'watchdog_is_not_production_termination': True}
    try:
        receipt['returncode'] = launch(source, results, targets, installed_cli=True)
    except subprocess.TimeoutExpired:
        receipt.update(returncode=124, timeout=True, classification='TEST_WATCHDOG_NOT_PASS')
    receipt['source_changes_after_run'] = verify()
    if receipt['source_changes_after_run']:
        receipt['returncode'] = 125
    (results / 'run_exit.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)
    raise SystemExit(receipt['returncode'])


if __name__ == '__main__':
    main()
