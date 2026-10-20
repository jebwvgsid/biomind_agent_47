#!/public/home/liusiqi/anaconda3/envs/biomind/bin/python3.12
from pathlib import Path
import json,os,sys,hashlib
root=Path(__file__).resolve().parent
m=json.loads((root/'manifest.json').read_text());args=sys.argv[1:]
if '--project-dir' not in args:raise SystemExit('Pass --project-dir for one of the four revised original cases')
project=str(Path(args[args.index('--project-dir')+1]).resolve());t=next(t for t in m['tasks'] if t['project_dir']==project)
if any(v.split('=')[0] in ['--pi-config','--pi-model-record','--pi-node'] for v in args):raise SystemExit('Runtime and model record are fixed by this task adapter')
assert hashlib.sha256(Path(t['prompt_source']).read_bytes()).hexdigest()==t['source_sha256'],'Prompt changed since preparation'
assert hashlib.sha256(Path(t['input_manifest']).read_bytes()).hexdigest()==t['input_manifest_sha256'],'Input contract changed'
cmd=['/public/home/liusiqi/anaconda3/envs/biomind/bin/python3.12', '-B', '/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/local-core-releases/biomind-d0cf979998965759/bio_agents.py', '--pi-node', '/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node']+args+['--pi-config',t['runtime_config'],'--pi-model-record','/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/production/v1.2.0/cli-model-selection.json']
env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1')
selection=json.loads(Path('/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/production/v1.2.0/cli-model-selection.json').read_text())
if selection.get('provider')=='deepseek' and not env.get('DEEPSEEK_API_KEY'):
    assert selection.get('confirmed_active') is True and selection.get('endpoint_base_url')=='https://api.deepseek.com','Unconfirmed provider endpoint'
    credentials=json.loads(Path('/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.biomind/pi-agent/auth.json').read_text()).get('deepseek',{})
    key=credentials.get('key')
    assert credentials.get('type')=='api_key' and isinstance(key,str) and key and not key.startswith('!'),'Literal provider credential required'
    env['DEEPSEEK_API_KEY']=key
    del credentials,key
os.execve(cmd[0],cmd,env)
