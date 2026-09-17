#!/usr/bin/env python3
"""Recheck saved A/B/C source identities; no checkout, imports, or writes."""
from pathlib import Path
import hashlib
import json

AUDIT = Path(__file__).resolve().parents[1]
REPO = AUDIT.parents[2]


def main():
    comparison = json.loads((AUDIT / 'evidence/version_comparison.json').read_text())
    frozen = json.loads((AUDIT / 'evidence/baseline_current.json').read_text())
    old_manifest = json.loads((REPO / 'docs/implementation/environment_binding_unified_20260917/evidence/baseline.json').read_text())
    old = {r['path']: r['sha256'] for r in old_manifest['source_manifest']}
    release = json.loads((REPO / 'docs/implementation/environment_binding_unified_20260917/evidence/final_source_manifest.json').read_text())
    errors = []
    checks = 0
    for row in comparison['rows']:
        name = row['path']
        candidates = [(REPO / name, row['current_sha256']),
                      (Path(comparison['release_source_root']) / name, release[name]['sha256'])]
        if name in old:
            candidates.append((Path(comparison['old_source_root']) / name, old[name]))
        for path, expected in candidates:
            observed = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            checks += 1
            if observed != expected: errors.append(str(path))
    for row in frozen['source_manifest']:
        path = Path(frozen['source_root']) / row['path']
        checks += 1
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            errors.append(str(path))
    print(json.dumps({'checks': checks, 'mismatches': errors, 'status': 'PASS' if not errors else 'FAIL'}))
    if errors: raise SystemExit(1)


if __name__ == '__main__': main()
