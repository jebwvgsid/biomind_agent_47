"""Verify saved before-images + A/B/C patches reproduce changed effective sources.

This is a temporary reconstruction only; it never resets or edits the working tree.
It is not a clean-SHA build claim or a restoration of installed dependencies.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]
DOC = Path(__file__).resolve().parent
BASE = '4721cc8e737991b7d6571d5bebfdf47e808b6ba1'

if __name__ == '__main__':
    bundle = json.loads((DOC/'baseline-recovery.json').read_text())
    assert hashlib.sha256(Path(bundle['path']).read_bytes()).hexdigest() == bundle['sha256']
    plans = [json.loads((DOC/'deltas'/f'{batch}-staging.json').read_text()) for batch in 'ABC']
    paths = sorted({row['file'] for plan in plans for row in plan})
    with tempfile.TemporaryDirectory(prefix='biomind-delta-reconstruction-') as temporary:
        target = Path(temporary)
        with tarfile.open(bundle['path']) as archive:
            names = set(archive.getnames())
            from_head = []
            for name in paths:
                assert not Path(name).is_absolute() and '..' not in Path(name).parts
                assert name not in {'.env', '.env.api_keys'}
                entry = next((prefix+name for prefix in ('before/', 'untracked/') if prefix+name in names), None)
                raw = archive.extractfile(entry).read() if entry else None
                if raw is None:
                    original = subprocess.run(['git', 'show', BASE+':'+name], cwd=ROOT, capture_output=True)
                    if original.returncode == 0:
                        raw = original.stdout
                        from_head.append(name)
                if raw is not None:
                    path = target/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
            if from_head:
                # Recover relevant old tracked dirty hunks before this task's deltas.
                subprocess.run(['git', 'apply', '--unsafe-paths', '--directory='+str(target),
                                *['--include='+str(target/name) for name in from_head]],
                               input=archive.extractfile('preexisting.patch').read(),
                               cwd=ROOT, check=True, capture_output=True)
        applied = []
        for plan in plans:
            for row in plan:
                patch = ROOT/row['patch']
                # Absolute --directory points only to this fresh temporary tree.
                subprocess.run(['git', 'apply', '--unsafe-paths', '--directory='+str(target), str(patch)],
                               cwd=ROOT, check=True, capture_output=True)
                applied.append(row['patch'])
        facts = []
        for name in paths:
            actual, expected = (target/name).read_bytes(), (ROOT/name).read_bytes()
            facts.append({'file': name, 'matches_effective_tree': actual == expected,
                          'sha256': hashlib.sha256(actual).hexdigest()})
        assert all(row['matches_effective_tree'] for row in facts), 'delta chain differs from effective tree'
    output = {'status': 'PASS', 'base_commit': BASE, 'patches': len(applied), 'files': facts,
              'scope': 'changed-file reconstruction from saved original before-images and exact A/B/C patches; no execution/build of a clean checkout'}
    (DOC/'delta-reconstruction.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'status': output['status'], 'patches': len(applied), 'files': len(facts)}))
