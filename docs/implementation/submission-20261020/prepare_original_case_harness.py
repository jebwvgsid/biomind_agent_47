"""Bind the four revised original cases to the deployed core and isolated native resources."""
from pathlib import Path
import argparse,json,hashlib,importlib.util,shutil
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';S=B/'BioMind_Submission_Work_20261020/result';p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);a=p.parse_args();release=a.release.resolve(strict=True);root=O/'benchmark-harness';root.mkdir(mode=0o700)
spec=importlib.util.spec_from_file_location('bootstrap',S/'production-review/bootstrap_web_resources.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
target=json.loads((S/'production-review/targets.json').read_text());manifest=json.loads((B/'BioMind_Original_Cases_Revision_20261020/manifest.json').read_text());receipt=json.loads((release/'RELEASE.json').read_text());py=receipt['python']['path'];node=receipt['node']['path'];web=json.loads((S/'production-review/web-search.json.candidate').read_text());settings=B/'Biomind_v1/integrations/pi_runtime/.biomind/pi-agent/settings.json'
source=Path(target['package_source']);lock=source.parent.parent/'package-lock.json';lock_sha=hashlib.sha256(lock.read_bytes()).hexdigest()
assert lock_sha==json.loads((S/'network-cli/preparation.json').read_text())['source_lock_sha256'],'Pinned plugin dependency lock changed'
(O/'evidence/benchmark-plugin-identity.json').write_text(json.dumps({'package':'pi-web-access@0.38.0','source':str(source),'dist_sha256':target['package_dist_sha256'],'dependency_lock_sha256':lock_sha},indent=2)+'\n')
for i,t in enumerate(manifest['tasks'],1):
 rd=root/f'{i:02}';rd.mkdir();project=Path(t['project_dir']);agent=rd/'agent'
 assert hashlib.sha256(Path(t['prompt_source']).read_bytes()).hexdigest()==t['source_sha256']
 helper.prepare_agent_resources(core=release,python=py,node=node,agent_dir=agent,project_dir=project,settings_source=settings,package_source=target['package_source'],package_sha256=target['package_dist_sha256'],web_config=web)
 shutil.copy2(t['prompt_source'],rd/'prompt.txt');(rd/'preferences.md').write_text('Use only the current case formal inputs and input_revision 20261020-fullanalysis. Historical outputs, scoring and answer files are not task inputs.\n');(rd/'registry.json').write_text('{"environments":{},"tools":{}}\n')
 config={'python':py,'installRoot':str(release),'agentDir':str(agent),'dataRoot':str(rd/'state'),'sessionDir':str(rd/'sessions'),'registryPath':str(rd/'registry.json'),'preferencesPath':str(rd/'preferences.md'),'historyEnabled':False,'memoryMode':'disabled'};(rd/'runtime.json').write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n');t.update(runtime_config=str(rd/'runtime.json'),run_dir=str(rd),resource_status='PREPARED_NOT_SCIENTIFICALLY_RUN')
manifest.update(production_deployed=True,formal_runs_started=False,build_id=receipt['releaseId'],source_commit=receipt['sourceCommit'],entrypoint=str(root/'biomind-entry.py'),provider_credentials='No credentials copied to files. Existing confirmed DeepSeek credential is read into the child environment only; an explicitly supplied environment credential takes precedence.')
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
entry='''#!__PYTHON__
from pathlib import Path
import json,os,sys,hashlib
root=Path(__file__).resolve().parent
m=json.loads((root/'manifest.json').read_text());args=sys.argv[1:]
if '--project-dir' not in args:raise SystemExit('Pass --project-dir for one of the four revised original cases')
project=str(Path(args[args.index('--project-dir')+1]).resolve());t=next(t for t in m['tasks'] if t['project_dir']==project)
if any(v.split('=')[0] in ['--pi-config','--pi-model-record','--pi-node'] for v in args):raise SystemExit('Runtime and model record are fixed by this task adapter')
assert hashlib.sha256(Path(t['prompt_source']).read_bytes()).hexdigest()==t['source_sha256'],'Prompt changed since preparation'
assert hashlib.sha256(Path(t['input_manifest']).read_bytes()).hexdigest()==t['input_manifest_sha256'],'Input contract changed'
cmd=__BASE_COMMAND__+args+['--pi-config',t['runtime_config'],'--pi-model-record',__MODEL__]
env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1')
selection=json.loads(Path(__MODEL__).read_text())
if selection.get('provider')=='deepseek' and not env.get('DEEPSEEK_API_KEY'):
    assert selection.get('confirmed_active') is True and selection.get('endpoint_base_url')=='https://api.deepseek.com','Unconfirmed provider endpoint'
    credentials=json.loads(Path(__AUTH__).read_text()).get('deepseek',{})
    key=credentials.get('key')
    assert credentials.get('type')=='api_key' and isinstance(key,str) and key and not key.startswith('!'),'Literal provider credential required'
    env['DEEPSEEK_API_KEY']=key
    del credentials,key
os.execve(cmd[0],cmd,env)
'''.replace('__PYTHON__',py).replace('__BASE_COMMAND__',repr([py,'-B',str(release/'bio_agents.py'),'--pi-node',node])).replace('__MODEL__',repr(str(B/'BioMind_Unified_Core_20261009/production/v1.2.0/cli-model-selection.json')))
entry=entry.replace('__AUTH__',repr(str(B/'Biomind_v1/integrations/pi_runtime/.biomind/pi-agent/auth.json')))
(root/'biomind-entry.py').write_text(entry);(root/'biomind-entry.py').chmod(0o755)
print(json.dumps({'status':'PREPARED','tasks':len(manifest['tasks']),'core':receipt['releaseId'],'formal_runs':0}))
