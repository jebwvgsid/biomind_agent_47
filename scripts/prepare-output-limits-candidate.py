"""Install the pinned local patch through BioMind's existing Pi package manager.

Only an explicit isolated candidate directory is accepted. No production copy,
credentials, trust changes, engine installation, or service operations.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
runtime = root / 'integrations/pi_runtime'
parser = argparse.ArgumentParser()
parser.add_argument('--candidate-root', type=Path, required=True)
parser.add_argument('--node', type=Path, required=True)
parser.add_argument('--python', type=Path, required=True)
args = parser.parse_args()
candidate = args.candidate_root.resolve()
candidate.mkdir(parents=True, exist_ok=True)
marker = candidate / 'OUTPUT-LIMITS-CANDIDATE.json'
agent = candidate / 'agent'
project = candidate / 'project'
if agent.exists() and not marker.is_file():
    raise SystemExit('Refusing an existing agent directory without this candidate receipt')
project.mkdir(exist_ok=True)
request = {'request': {'action': 'install', 'cwd': str(project), 'source': str(runtime / 'vendor/pi-output-limits')},
           'context': {'cwd': str(project), 'agentDir': str(agent), 'python': str(args.python.resolve())}}
# Let the existing resource guard initialize the new scope; do not forge a marker.
result = subprocess.run([str(args.node), str(runtime / 'dist/plugin-cli.js')], input=json.dumps(request),
                        text=True, capture_output=True, check=True)
receipt = json.loads(result.stdout)
if receipt['status'] != 'ok':
    raise SystemExit(receipt['code'])
settings = agent / 'settings.json'
original = json.loads(settings.read_text())
defaults = json.loads((runtime / 'output-limits-settings.json').read_text())
merged = {**original, 'outputLimits': {**original.get('outputLimits', {}), **defaults['outputLimits']}}
with settings.open('w') as stream:
    json.dump(merged, stream, indent=2, ensure_ascii=False)
    stream.write('\n')
config = {'python': str(args.python.resolve()), 'installRoot': str(root), 'agentDir': str(agent),
          'dataRoot': str(candidate / 'domain'), 'registryPath': str(candidate / 'registry.json'),
          'preferencesPath': str(candidate / 'preferences.md'), 'historyEnabled': False,
          'memoryMode': 'disabled', 'executionProfile': 'native'}
(candidate / 'runtime.json').write_text(json.dumps(config, indent=2) + '\n')
for filename, content in [('registry.json', '{"environments":{},"tools":{}}\n'), ('preferences.md', 'Synthetic output-limit acceptance.\n')]:
    if not (candidate / filename).exists():
        (candidate / filename).write_text(content)
receipt.update(candidateOnly=True, effectiveSettings=merged, runtimeConfig=str(candidate / 'runtime.json'),
               nodeVersion=subprocess.check_output([str(args.node), '--version'], text=True).strip())
marker.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + '\n')
os.chmod(marker, 0o600)
print(json.dumps({'status': 'installed_candidate', 'receipt': str(marker), 'agentDir': str(agent), 'project': str(project)}))
