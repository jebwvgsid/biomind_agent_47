"""Read exact production owner identity/tools; no prompt or model request."""
from pathlib import Path
import argparse,base64,datetime,hashlib,json,subprocess,urllib.request
from deploy_studio import B,O,E,SERVICE,CONFIG,host_preflight

p=argparse.ArgumentParser();p.add_argument('--session',required=True);p.add_argument('--label',required=True);p.add_argument('--cleanup-test',action='store_true');a=p.parse_args()
assert a.label.replace('-','').isalnum()
c=json.loads(CONFIG.read_text());host_preflight(c)
d=json.loads(subprocess.check_output(['python3','-B',str(SERVICE),'discover','--config',str(CONFIG)],text=True));assert d['status']=='ready'
secret=json.loads(Path(c['environmentFile']).read_text());assert set(secret)=={'PI_WEB_PASSWORD'}
auth='Basic '+base64.b64encode(('pi:'+secret['PI_WEB_PASSWORD']).encode()).decode();del secret
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):raise RuntimeError('UNEXPECTED_REDIRECT')
client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def request(route,value=None,method=None):
    headers={'Authorization':auth};data=None
    if value is not None:data=json.dumps(value).encode();headers['Content-Type']='application/json'
    req=urllib.request.Request('http://127.0.0.1:'+str(d['port'])+route,data=data,headers=headers,method=method)
    with client.open(req,timeout=30) as response:return json.load(response)
owner=request('/api/agent/'+a.session);assert owner.get('running') is True,'OWNER_NOT_LOADED'
s=owner['state'];assert s['sessionId']==a.session
assert s['loaded_build']['buildId']==json.loads((O/'build-result.json').read_text())['releaseId']
assert not s['active'] and not s.get('admissions') and not s.get('isStreaming'),'OWNER_BUSY'
scope={k:s[k] for k in ('serverId','sessionId','ownerGeneration','project')}
t=request('/api/agent/'+a.session,{'type':'get_tools',**scope});assert t['success'] is True
registered=sorted(x['name'] for x in t['data']['data']['registered'])
assert set(['run_command','inspect_run','wait_run','cancel_run','web_search','fetch_content','get_search_content'])<=set(registered)
r={'status':'PASS','atUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':scope,'instanceId':d['instanceId'],'port':d['port'],
   'sessionFile':s['sessionFile'],'loadedBuild':s['loaded_build'],'registeredTools':registered,'modelRequests':0,'active':s['active'],'admissions':s.get('admissions',[])}
if a.cleanup_test:
    f=Path(s['sessionFile']);before=json.loads((E/'production-before.json').read_text());root=Path(before['historyRoot'])
    rel=str(f.relative_to(root));assert rel not in before['history'],'REFUSE_ORIGINAL_SESSION'
    lines=f.read_text().splitlines();header=json.loads(lines[0]);assert header['id']==a.session
    assert not any(json.loads(line).get('type')=='message' for line in lines),'REFUSE_NONEMPTY_SESSION'
    dest=O/'test-sessions-removed';dest.mkdir(exist_ok=True,mode=0o700)
    backup=dest/f.name;assert not backup.exists();backup.write_bytes(f.read_bytes());backup.chmod(0o600)
    r['cleanup']={'original':str(f),'backup':str(backup),'sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),'firstMessage':None}
    reply=request('/api/sessions/'+a.session,method='DELETE');assert reply.get('ok') is True
    assert not f.exists();r['cleanup']['status']='REMOVED_FROM_SIDEBAR_BACKUP_RETAINED'
out=E/(a.label+'.json');assert not out.exists();out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:r[k] for k in ('status','scope','instanceId','port','modelRequests')},ensure_ascii=False))
