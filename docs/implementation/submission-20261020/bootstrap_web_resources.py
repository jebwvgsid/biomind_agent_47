"""Candidate helper for the existing benchmark per-task bootstrap.
New scopes only; existing Pi guard and package manager own resource changes.
Does not copy auth/models/sessions/memory or publish/launch a benchmark.
"""
from pathlib import Path
import hashlib,importlib.util,json,os,subprocess

def prepare_agent_resources(*, core, python, node, agent_dir, project_dir, settings_source, package_source, package_sha256, web_config=None):
    core,agent_dir,project_dir,settings_source,package_source=map(Path,(core,agent_dir,project_dir,settings_source,package_source))
    if agent_dir.exists() or agent_dir.is_symlink():raise ValueError('Fresh per-task agentDir required')
    package=json.loads((package_source/'package.json').read_text())
    if (package.get('name'),package.get('version'))!=('pi-web-access','0.38.0'):raise ValueError('Pinned package version required')
    if hashlib.sha256((package_source/'dist/index.js').read_bytes()).hexdigest()!=package_sha256:raise ValueError('Reviewed package content changed')
    settings=json.loads(settings_source.read_text())
    # Preserve existing native PackageSource filters and resolve relative sources
    # against the source agentDir before relocation.
    def absolute(v):return str((settings_source.parent/v).resolve()) if v.startswith(('./','../')) else v
    settings['packages']=[dict(v,source=absolute(v['source'])) if isinstance(v,dict) else absolute(v) for v in settings.get('packages',[])]
    spec=importlib.util.spec_from_file_location('bootstrap_pi_guard',core/'agent_core/pi_plugin_guard.py');guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    request={'agentDir':str(agent_dir),'cwd':str(project_dir),'scopes':['user','project']};plan=guard.prepare(request);gates=[]
    try:
        for row in plan['scopes']:gates.append({'fd':os.open(row['gate'],os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'path':row['gate'],'mode':'exclusive'})
        guard.lock_fds({'files':gates});initialized=guard.initialize({**request,'gates':gates})
        if any(row['legacyUnverified'] for row in initialized['scopes']):raise ValueError('Project scope is legacy/unverified; do not adopt it implicitly')
        (agent_dir/'settings.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n')
        (agent_dir/'web-search.json').write_text(json.dumps(web_config or {'toolActivation':'auto','fetch':{'timeout':30}},ensure_ascii=False,indent=2)+'\n')
    finally:
        for row in gates:os.close(row['fd'])
    config=agent_dir.parent/'resource-bootstrap.runtime.json';config.write_text(json.dumps({'python':str(python),'agentDir':str(agent_dir),'installRoot':str(core)},indent=2)+'\n')
    command=[str(python),'-B',str(core/'bio_agents.py'),'install',str(package_source),'--json','--pi-config',str(config),'--project-dir',str(project_dir),'--pi-node',str(node)]
    env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1',PI_CODING_AGENT_DIR=str(agent_dir))
    result=subprocess.run(command,cwd=project_dir,env=env,capture_output=True,text=True,timeout=180)
    (agent_dir.parent/'resource-bootstrap.stdout').write_text(result.stdout);(agent_dir.parent/'resource-bootstrap.stderr').write_text(result.stderr)
    if result.returncode or json.loads(result.stdout).get('status')!='ok':raise RuntimeError('Native package install failed; see resource-bootstrap logs')
    receipt={'status':'PREPARED','core':str(core),'agentDir':str(agent_dir),'project':str(project_dir),'package':'pi-web-access@0.38.0','package_source':str(package_source),'package_dist_sha256':package_sha256,'settings_source_sha256':hashlib.sha256(settings_source.read_bytes()).hexdigest(),'credential_copy':False,'model_or_network_acceptance':'NOT_RUN','command':command}
    (agent_dir.parent/'resource-bootstrap.receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    return receipt
