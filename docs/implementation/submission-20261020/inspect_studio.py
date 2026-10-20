"""Read only the current service identity and running count, without recording auth."""
from pathlib import Path
import json,base64,urllib.request,subprocess
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';service=B/'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py';cfg=B/'BioMind_Studio_Workbench_20261009/deployment/private/service.json'
d=json.loads(subprocess.check_output(['python',str(service),'discover','--config',str(cfg)],text=True));config=json.loads(cfg.read_text());env=json.loads(Path(config['environmentFile']).read_text());password=env['PI_WEB_PASSWORD']
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise RuntimeError('Unexpected local service redirect')
client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect());result={'discovery':d}
for name in ['profile','running']:
 req=urllib.request.Request('http://127.0.0.1:'+str(d['port'])+'/api/agent/'+name,headers={'Authorization':'Basic '+base64.b64encode(('pi:'+password).encode()).decode()})
 with client.open(req,timeout=15) as r:result[name]=json.load(r)
(O/'evidence/studio-before.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'status':d['status'],'server_id':d['serverId'],'instance':d['instanceId'],'running':result['running'],'profile_fields':list(result['profile'])}))
