"""Verify committed synthetic evidence with stdlib only; no BioMind imports."""
from pathlib import Path
import hashlib
import json

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'evidence_manifest.json').read_text())
for name, entry in manifest.items():
    assert Path(name).name == name
    data = (root / 'evidence' / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry['sha256'], name
    assert len(data) == entry['bytes'], name
observations = []
for path in sorted((root / 'evidence').glob('consultant-handoff-*.json')):
    data = json.loads(path.read_text())
    result = data['closures'][0]['skill_decision_result']
    assert data['closures'][0]['consultation_state']['tool_observations'][-1]['observation'] == result
    decoded = []
    for message in data['provider_requests'][1]:
        try:
            decoded.append(json.loads(message['content']))
        except (ValueError, TypeError):
            pass
    assert result in decoded, path.name  # Whole actual result, not a keyword match.
    assert data['provider_calls'] == 2 and data['execution_calls'] == 0
    observations.append({'file': path.name, 'next_provider_contains_exact_tool_result': True,
                         'calls': data['provider_calls'], 'service_calls': data['service_calls']})
resumes = []
for path in sorted((root / 'evidence').glob('consultant-production-continue-*.json')):
    data = json.loads(path.read_text())
    assert data['history_preserved']
    assert data['historical_component_calls'] == {'consultant': 1, 'executions': 0}
    assert data['calls']['consultant'] == 2 and data['calls']['executions'] == 0
    if data['new_stop']:
        assert data['new_stop']['stop_id'] != data['old_stop']['stop_id']
        assert data['new_stop']['stop_reason']['error_code'] == 'CONSULTANT_PROVIDER_FAILED'
        assert 'SYNTHETIC_NEW_PROVIDER_FAILURE' in data['new_stop']['stop_reason']['detail']
    else:
        assert data['calls']['summary'] == 0
    resumes.append({'file': path.name, 'calls': data['calls']})
assert len(observations) == 4 and len(resumes) == 2
print(json.dumps({'sha256_verified_files': len(manifest), 'handoffs': observations, 'resumes': resumes}, indent=2))
