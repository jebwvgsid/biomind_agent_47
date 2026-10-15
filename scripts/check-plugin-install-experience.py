"""Isolated CLI acceptance. Own fixtures only; no production entry/config changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

p = argparse.ArgumentParser()
p.add_argument('--root', type=Path, required=True)
p.add_argument('--node', type=Path, required=True)
p.add_argument('--python', type=Path, required=True)
args = p.parse_args()
repo = Path(__file__).resolve().parents[1]
base = args.root.resolve()
base.mkdir(parents=True, exist_ok=False)
project = base / 'project'; project.mkdir()
home = base / 'home'; home.mkdir()
agent = base / 'agent'
node, python = str(args.node.resolve()), str(args.python.resolve())
env = {'HOME': str(home), 'PATH': str(Path(node).parent) + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
       'BIOMIND_PI_NODE': node, 'BIOMIND_PI_PYTHON': python, 'BIOMIND_DISABLE_DOTENV': '1', 'PI_OFFLINE': '1',
       'PYTHONDONTWRITEBYTECODE': '1', 'PI_CODING_AGENT_DIR': str(agent), 'TMPDIR': str(base),
       'npm_config_cache': str(base / 'npm-cache'), 'npm_config_userconfig': str(base / 'npmrc'),
       'npm_config_globalconfig': str(base / 'global-npmrc'), 'npm_config_audit': 'false', 'npm_config_fund': 'false'}
(base/'npmrc').write_text(''); (base/'global-npmrc').write_text('')
runtime = repo / 'integrations/pi_runtime'
npm = Path(node).parent.parent / 'lib/node_modules/npm/bin/npm-cli.js'
pack = subprocess.run([node, str(npm), 'pack', '--ignore-scripts', '--json', '--pack-destination', str(base)],
                      cwd=runtime/'vendor/pi-output-limits', env=env, text=True, capture_output=True, timeout=90)
assert pack.returncode == 0, pack.stderr
metadata = json.loads(pack.stdout)[0]; archive = base/metadata['filename']
(base/'pack.json').write_text(json.dumps(metadata, indent=2))
assert not any('node_modules/' in f['path'] or '..' in f['path'].split('/') for f in metadata['files'])
assert any(f['path']=='dist/output-limits-extension.ts' for f in metadata['files'])
standalone = base/'standalone'; standalone.mkdir()
with tarfile.open(archive) as tar: tar.extractall(standalone, filter='data')
package = standalone/'package'
entry = (package/'dist/output-limits-extension.ts').read_text()
assert str(repo) not in entry and '../../dist' not in entry
config = {'python': python, 'installRoot': str(repo), 'agentDir': str(agent), 'dataRoot': str(base/'domain'),
          'registryPath': str(base/'registry.json'), 'preferencesPath': str(base/'preferences.md'),
          'historyEnabled': False, 'memoryMode': 'disabled', 'executionProfile': 'native'}
(base/'registry.json').write_text('{"environments":{},"tools":{}}\n')
(base/'preferences.md').write_text('Isolated plugin acceptance.\n')
(base/'runtime.json').write_text(json.dumps(config,indent=2))
command = [python, '-B', str(repo/'bio_agents.py')]
common = ['--pi-node',node,'--pi-config',str(base/'runtime.json'),'--project-dir',str(project)]
receipts = []
def cli(label, *arguments, expected=0):
    cmd = [*command, *arguments, *common]
    result = subprocess.run(cmd,cwd=project,env=env,text=True,capture_output=True,timeout=120)
    (base/(label+'.stdout')).write_text(result.stdout); (base/(label+'.stderr')).write_text(result.stderr)
    assert result.returncode == expected, (label,result.returncode,result.stdout,result.stderr)
    value = json.loads(result.stdout) if '--json' in arguments else None
    receipts.append({'label':label,'command':cmd,'exitCode':result.returncode,'status':'PASS'})
    return value, result.stdout
before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [runtime/'package.json', runtime/'package-lock.json',runtime/'node_modules/@earendil-works/pi-coding-agent/package.json']}
_, output = cli('human-install','install',str(package))
assert '目标：' in output and '正在安装' in output and '会话加载未核验' in output
assert '\x1b' not in output and '\r' not in output
listed, _ = cli('json-list','list','--json')
row = listed['packages'][0]
assert row['packageName']=='biomind-pi-output-limits' and row['installedPath']==str(package)
assert row['workerLoaded']=='unknown' and row['enablement']=='enabled'
assert listed['context']['agentDir']==str(agent)
again, _ = cli('repeat','install',str(package),'--json')
assert len(again['packages'])==1
cli('update-one','update',str(package),'--json')
cli('update-extensions','update','--extensions','--json')
refused, _ = cli('update-host-refused','update','--json',expected=2)
assert refused['code']=='PINNED_PI_SELF_UPDATE_REFUSED'
cli('remove-local','remove',str(package),'--json')
assert (package/'dist/output-limits-extension.ts').exists()
removed, _ = cli('removed-list','list','--json');assert removed['packages']==[]
cli('reinstall-for-runtime','install',str(package),'--json')
assert 'outputLimits' not in json.loads((agent/'settings.json').read_text()), 'verify adapter defaults, not prepared settings'
for file,digest in before.items():assert hashlib.sha256(Path(file).read_bytes()).hexdigest()==digest
(base/'acceptance.json').write_text(json.dumps({'status':'PASS','classification':'ISOLATED_CLI_REAL_NATIVE_PACKAGE_MANAGER',
    'packageArchive':str(archive),'archiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
    'packagePath':str(package),'config':config,'hostFilesUnchanged':before,'steps':receipts},indent=2))
print(json.dumps({'status':'PASS','candidate':str(base),'archive':str(archive)}))
