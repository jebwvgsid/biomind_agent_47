"""Stop only the exact, empty Windows canary relay; never touch user owners."""
from pathlib import Path
import base64,json,os,signal,time,urllib.request
from deploy_studio import O,E,CONFIG
x=json.loads((E/'new-owner.json').read_text());scope=x['scope'];gui=json.loads((E/'windows-new-owner.json').read_text())
assert scope['sessionId']==gui['session'] and scope['ownerGeneration']==gui['ownerGeneration']
assert not Path(x['sessionFile']).exists(),'PERSISTED_SESSION_REFUSED'
before=json.loads((E/'production-before.json').read_text());assert not any(scope['sessionId'] in f for f in before['history'])
diagnostics=json.loads((E/'startup-diagnostics.json').read_text())
matches=[g for g in diagnostics['attempts'] if any(r.get('sessionId')==scope['sessionId'] and r.get('ownerGeneration')==scope['ownerGeneration'] for r in g['records'])]
assert len(matches)==1
rows=matches[0]['records'];worker=next(r for r in rows if r['role']=='worker' and r['stage']=='ready');server=next(r for r in rows if r['role']=='server' and r['stage']=='spawn')
relay_pid=worker['ppid'];worker_pid=worker['pid'];relay=Path('/proc')/str(relay_pid)
def ident(pid):
    p=Path('/proc')/str(pid)
    try:
        fields=(p/'stat').read_text().rsplit(')',1)[1].split()
        return {'pid':pid,'state':fields[0],'ppid':int(fields[1]),'startTicks':fields[19]}
    except FileNotFoundError:return None
prior=ident(relay_pid);assert prior and prior['ppid']==server['pid'];assert ident(worker_pid)['ppid']==relay_pid
assert relay.stat().st_uid==os.getuid()
argv=(relay/'cmdline').read_bytes().split(b'\0');root=x['loadedBuild']['releaseRoot']
assert (root+'/integrations/pi_runtime/testing/posix-pipe-relay.py').encode() in argv
assert (root+'/bio_agents.py').encode() in argv
c=json.loads(CONFIG.read_text());state=Path(c['stateRoot']);d=json.loads((state/'service.json').read_text());assert d['instanceId']==x['instanceId']
secret=json.loads(Path(c['environmentFile']).read_text());assert set(secret)=={'PI_WEB_PASSWORD'}
auth='Basic '+base64.b64encode(('pi:'+secret['PI_WEB_PASSWORD']).encode()).decode();del secret
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):raise RuntimeError('UNEXPECTED_REDIRECT')
client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def get_owner():
    req=urllib.request.Request('http://127.0.0.1:'+str(x['port'])+'/api/agent/'+scope['sessionId'],headers={'Authorization':auth})
    with client.open(req,timeout=20) as r:return json.load(r)
s=get_owner()['state'];assert all(s[k]==v for k,v in scope.items())
assert not s['active'] and not s.get('isStreaming') and not s.get('admissions')
assert ident(relay_pid)==prior
owned={}
def remember(pid):
    row=ident(pid)
    if row is None:return
    owned[pid]=row
    children=Path('/proc')/str(pid)/'task'/str(pid)/'children'
    if children.exists():
        for child in children.read_text().split():remember(int(child))
remember(relay_pid)
started=time.monotonic();os.kill(relay_pid,signal.SIGTERM)
# Closing only this relay's pipe gives its idle worker EOF. No worker or server signal.
deadline=time.monotonic()+35
while time.monotonic()<deadline:
    alive=[i for pid,old in owned.items() if (i:=ident(pid)) and i['startTicks']==old['startTicks'] and i['state']!='Z']
    if not alive:break
    time.sleep(.25)
assert not alive,'CANARY_EXIT_NOT_CONFIRMED_NO_FURTHER_SIGNALS'
after=get_owner();assert after.get('running') is False
r={'status':'PASS','scope':scope,'relayBefore':prior,'workerPid':worker_pid,'elapsedSeconds':time.monotonic()-started,
   'signals':[{'pid':relay_pid,'signal':'SIGTERM','scope':'only this turn empty canary relay'}],'workerSignals':0,'productionServiceSignals':0,'userOwnerSignals':0,
   'persistedHistoryCreated':False,'historyFilesDeleted':0,'ownerRunningAfter':False,'ownedProcessTree':list(owned.values())}
(E/'empty-canary-cleanup.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False))
