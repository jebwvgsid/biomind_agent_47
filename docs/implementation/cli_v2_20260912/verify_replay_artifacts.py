"""Verify the committed synthetic replay bundle; no BioMind imports or state I/O."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
manifest = json.loads((ROOT / 'replay_manifest.json').read_text())
for relative, expected in manifest.items():
    path = ROOT / relative
    assert path.is_relative_to(ROOT / 'replay')
    assert path.stat().st_size == expected['bytes'], relative
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected['sha256'], relative

source_manifests = {side: json.loads((ROOT / f'replay/{side}-source-manifest.json').read_text()) for side in ('before', 'after')}
identity = json.loads((ROOT / 'replay_source_identity.json').read_text())
assert source_manifests['after']['source'] == identity['final_after_tree']
pairs = json.loads((ROOT / 'pty_comparison.json').read_text())
assert len(pairs) == 9
for row in pairs:
    before, after = [ROOT / row[f'{side}_directory'] for side in ('before', 'after')]
    metrics = [json.loads((directory / 'metrics.json').read_text()) for directory in (before, after)]
    render = [json.loads((directory / 'renderer.json').read_text()) for directory in (before, after)]
    assert all(item['returncode'] == 0 and item['captured'] for item in metrics)
    for side, metric in zip(('before', 'after'), metrics):
        assert metric['source'] == source_manifests[side]['source']
        assert metric['source_signature'] == source_manifests[side]['source_signature']
    assert row['after_commit'] == identity['final_after_commit']
    assert metrics[0]['counts'] == metrics[1]['counts']
    assert metrics[0]['replay_source_sha256'] == metrics[1]['replay_source_sha256']
    public = [[item['text'] for item in content['items'] if item['kind'] == 'model_public'] for content in render]
    assert public[0] == public[1]
    identities = [item['scope']['source_message_id'] for item in render[1]['items'] if item['kind'] == 'model_public']
    assert len(identities) == len(set(identities))
    assert render[0]['draft'] == render[1]['draft']
    assert render[0]['size'] == render[1]['size']
    for directory in (before, after):
        raw = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', (directory / 'terminal.ansi').read_text())
        assert 'SOURCE_UNCHANGED True' in raw
    if row['scenario'] == 'approval':
        assert (before / 'approval.json').read_bytes() == (after / 'approval.json').read_bytes()
    elif row['scenario'] == 'failure':
        assert (before / 'raw-receipt.json').read_bytes() == (after / 'raw-receipt.json').read_bytes()

compact = json.loads((ROOT / 'replay/after-normal-80/metrics.json').read_text())
verbose = json.loads((ROOT / 'replay/after-normal-80-verbose/metrics.json').read_text())
assert verbose['returncode'] == 0 and verbose['captured']
assert compact['counts'] == verbose['counts']
assert compact['permanent_logical_lines'] == 3 and verbose['permanent_logical_lines'] == 48
plain = json.loads((ROOT / 'plain_stage_validation.json').read_text())
assert plain['commit'] == identity['final_after_commit']
assert len(plain['reports']) == 3 and all(row['outcome'] == 'passed' for row in plain['reports'])
print(f'PASS: {len(manifest)} artifact hashes, 9 real PTY before/after pairs, and compact/verbose counts')
