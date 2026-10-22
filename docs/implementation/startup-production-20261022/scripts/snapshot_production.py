"""Record nonsecret production identity and history preservation evidence."""
from pathlib import Path
import argparse,base64,datetime,hashlib,json,subprocess,urllib.request,urllib.error
from deploy_studio import B,O,E,SERVICE,CONFIG,host_preflight

def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
 return h.hexdigest()

def main():
 p=argparse.ArgumentParser();p.add_argument('--label',required=True);args=p.parse_args()
 assert args.label in ('before','after')
 output=E/f'production-{args.label}.json';assert not output.exists()
 config=json.loads(CONFIG.read_text());host_preflight(config)
 runtime_path=Path(config['environment']['BIOMIND_RUNTIME_CONFIG'])
 runtime=json.loads(runtime_path.read_text())
 discovery=json.loads(subprocess.check_output(['python3','-B',str(SERVICE),'discover','--config',str(CONFIG)],text=True))
 secret=json.loads(Path(config['environmentFile']).read_text());assert set(secret)=={'PI_WEB_PASSWORD'}
 auth='Basic '+base64.b64encode(('pi:'+secret['PI_WEB_PASSWORD']).encode()).decode();del secret
 class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*a,**k):raise RuntimeError('UNEXPECTED_REDIRECT')
 client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 def get(route):
  req=urllib.request.Request('http://127.0.0.1:'+str(discovery['port'])+route,headers={'Authorization':auth})
  try:
   with client.open(req,timeout=20) as response:return json.load(response)
  except urllib.error.HTTPError as error:return {'httpStatus':error.code}
 health=get('/_biomind_studio/health');running=get('/api/agent/running')
 sid='01a145a0-3a8e-703c-913f-a6867ee491f9';owner=get('/api/agent/'+sid)
 selected={k:owner.get(k) for k in ('serverId','sessionId','ownerGeneration','project','running','ownerStatus','resumable','resumeReason')}
 state=owner.get('state') or {}
 selected['state']={k:state.get(k) for k in ('active','loaded_build','sessionFile','isStreaming','steeringMessages','followUpMessages','admissions')}
 root=Path(config['environment']['BIOMIND_SESSION_ROOT']).resolve()
 history={str(f.relative_to(root)):{'sha256':digest(f),'size':f.stat().st_size} for f in sorted(root.rglob('*.jsonl')) if f.is_file()}
 agent=Path(runtime['agentDir']);shared={}
 for f in sorted(agent.iterdir()):
  if f.name=='.biomind-resource-locks' or f.name=='sessions':continue
  s=f.lstat();shared[f.name]={'inode':s.st_ino,'size':s.st_size,'mode':s.st_mode,'mtimeNs':s.st_mtime_ns}
 result={'atUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),'health':health,'discovery':discovery,
  'running':running,'originalOwner':selected,'historyRoot':str(root),'history':history,'agentDir':str(agent),'sharedMetadata':shared,
  'serviceConfigSha256':digest(CONFIG),'runtimeConfig':str(runtime_path),'runtimeConfigSha256':digest(runtime_path),
  'backendRoot':config['cwd'],'entrypoint':config['environment']['BIOMIND_ENTRYPOINT'],
  'launchers':{str(f):digest(f) for f in [Path('/public/home/liusiqi/.local/bin/biomind'),B/'BioMind/bin/biomind']}}
 if args.label=='before':
  (O/'backups/service.before.json').write_bytes(CONFIG.read_bytes())
  (O/'backups/runtime.before.json').write_bytes(runtime_path.read_bytes())
  for f in (O/'backups').glob('*.json'):f.chmod(0o600)
 else:
  before=json.loads((E/'production-before.json').read_text())
  changes=[name for name,row in before['history'].items() if history.get(name)!=row]
  result['preservation']={'originalHistories':len(before['history']),'changedOrMissingOriginalHistories':changes,
   'sharedMetadataUnchanged':before['sharedMetadata']==shared,'sameAgentDir':before['agentDir']==str(agent),
   'sameHistoryRoot':before['historyRoot']==str(root),'newHistoryFiles':sorted(set(history)-set(before['history']))}
 with output.open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n')
 print(json.dumps({'status':'RECORDED','label':args.label,'serviceStatus':health.get('status'),'instanceId':discovery.get('instanceId'),
  'port':discovery.get('port'),'runningCount':len(running.get('runningSessionIds',[])) if isinstance(running,dict) else len(running),'historyFiles':len(history),'preservation':result.get('preservation')},ensure_ascii=False))

if __name__=='__main__':main()
