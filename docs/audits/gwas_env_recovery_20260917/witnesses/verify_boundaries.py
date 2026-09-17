#!/usr/bin/env python3
"""Capture/verify protected sources and incident artifacts without importing BioMind."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

AUDIT = Path(__file__).resolve().parents[1]
REPO = AUDIT.parents[2]
PROJECT = REPO.parent
EVIDENCE = AUDIT / 'evidence'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def capture():
    paths = []
    source = json.loads((EVIDENCE / 'version_comparison.json').read_text())
    paths.extend(REPO / row['path'] for row in source['rows'])
    session = PROJECT / 'benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505'
    paths.extend(p for p in session.rglob('*') if p.is_file() and not p.is_symlink())
    output = PROJECT / 'benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-GWAS-008/medium'
    paths.extend(output / name for name in ['report.md', 'reproduce.sh', 'stage_status.tsv'])
    registry = PROJECT / 'agent_shim/software_registry.json'
    paths.append(registry)
    hashes = {str(p): sha(p) if p.is_file() else None for p in sorted(set(paths))}
    return {'files': hashes,
            'staged': subprocess.check_output(['git', 'diff', '--cached', '--name-only'], cwd=REPO).decode().splitlines(),
            'boundary': '905 delivered source/test files, current GWAS session files, three original reports, software registry; no credentials read'}


def main():
    path = EVIDENCE / 'protected_before.json'
    current = capture()
    if '--capture' in sys.argv:
        if path.exists(): raise SystemExit('baseline already exists; refusing overwrite')
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        print('captured', len(current['files']), 'protected files')
        return
    before = json.loads(path.read_text())
    all_paths = sorted(set(before['files']) | set(current['files']))
    changed = [p for p in all_paths if before['files'].get(p) != current['files'].get(p)]
    result = {'protected_count_before': len(before['files']), 'protected_count_after': len(current['files']),
              'changed_paths': changed, 'staged_paths': current['staged'],
              'staged_outside_audit': [p for p in current['staged'] if not p.startswith('docs/audits/gwas_env_recovery_20260917/')],
              'status': 'PASS' if not changed else 'FAIL'}
    (EVIDENCE / 'protected_after.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    if changed or result['staged_outside_audit']: raise SystemExit(1)


if __name__ == '__main__': main()
