"""Resume the original idle Studio Main and verify the deployed core, without a model call."""
from pathlib import Path
import base64,datetime,hashlib,json,subprocess,urllib.request
from deploy_studio import B,O,E,SERVICE,CONFIG,host_preflight

def main():
 output=O/'studio-owner-verification.json';assert not output.exists()
 config=json.loads(CONFIG.read_text());host_preflight(config)
 build=json.loads((O/'build-result.json').read_text())
 backend=json.loads((O/'backend-result.json').read_text())
 assert config['cwd']==backend['root']
 for row in backend['sourceFiles']+backend['artifacts']:
  assert hashlib.sha256((Path(backend['root'])/row['path']).read_bytes()).hexdigest()==row['sha256']
 discovery=json.loads(subprocess.check_output(['python3','-B',str(SERVICE),'discover','--config',str(CONFIG)],text=True))
 assert discovery['status']=='ready'
 secret=json.loads(Path(config['environmentFile']).read_text());assert set(secret)=={'PI_WEB_PASSWORD'}
 auth='Basic '+base64.b64encode(('pi:'+secret['PI_WEB_PASSWORD']).encode()).decode();del secret
 class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*a,**k):raise RuntimeError('UNEXPECTED_REDIRECT')
 client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 def request(route,value=None):
  headers={'Authorization':auth};data=None
  if value is not None:data=json.dumps(value).encode();headers['Content-Type']='application/json'
  req=urllib.request.Request('http://127.0.0.1:'+str(discovery['port'])+route,data=data,headers=headers)
  with client.open(req,timeout=180) as response:return json.load(response)
 health=request('/_biomind_studio/health');assert health['instanceId']==discovery['instanceId']
 previous=json.loads((B/'BioMind_Production_LongTask_20261021/WINDOWS_GUI_RESULT.json').read_text())['main']
 sid=previous['sessionId'];project=previous['project'];route='/api/agent/'+sid
 prior=request(route)
 if prior.get('running') and prior.get('state',{}).get('loaded_build',{}).get('buildId')==build['releaseId']:
  state=prior['state'];resumed=False
 else:
  response=request(route,{'type':'resume_session','serverId':config['serverId'],'sessionId':sid,'project':project})
  assert response.get('success') is True
  state=response['data'];resumed=response.get('resumed')
 assert state['sessionId']==sid and state['project']==project
 assert state['sessionFile']==previous['sessionFile'],'Original history identity changed'
 assert state['loaded_build']['buildId']==build['releaseId']
 assert state['loaded_build']['releaseRoot']==build['root']
 assert not state['active'] and not state.get('admissions')
 scope={k:state[k] for k in ('serverId','sessionId','ownerGeneration','project')}
 reply=request(route,{'type':'get_tools',**scope})
 assert reply['success'] is True
 registered=sorted(t['name'] for t in reply['data']['data']['registered'])
 required=['run_command','inspect_run','wait_run','cancel_run','web_search','fetch_content','get_search_content']
 assert set(required)<=set(registered)
 result={'status':'PASS','atUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'serverId':config['serverId'],'instanceId':discovery['instanceId'],'port':discovery['port'],
  'sessionId':sid,'ownerGeneration':state['ownerGeneration'],'sessionFile':state['sessionFile'],'project':project,
  'loadedBuild':state['loaded_build'],'backendSourceCommit':backend['sourceCommit'],'backendBuildId':backend['buildId'],
  'backendArtifactsVerified':len(backend['artifacts']),'registeredTools':registered,'requiredTools':required,
  'originalOwnerAlreadyReconnectedBeforeProbe':prior.get('running',False),'resumedByProbe':resumed,
  'modelRequests':0,'tasksCancelled':0,'tasksReplayed':0,'newPersistentSessionsCreated':0,
  'testSessionCleanup':'No test session was created; the original idle Main is left loaded.'}
 with output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
 print(json.dumps({k:result[k] for k in ('status','serverId','instanceId','port','sessionId','ownerGeneration','modelRequests','originalOwnerAlreadyReconnectedBeforeProbe')},ensure_ascii=False))

if __name__=='__main__':main()
