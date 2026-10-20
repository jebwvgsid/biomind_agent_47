"""Use the existing native idle-stop and managed-service rebind commands."""
from pathlib import Path
import json,subprocess,time,sys
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';E=O/'evidence';result=json.loads((O/'build-result.json').read_text());release=Path(result['root']);service=B/'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py';config=B/'BioMind_Studio_Workbench_20261009/deployment/private/service.json';root=service.parent

def run(label,args,timeout=120):
 r=subprocess.run([sys.executable,'-B',*map(str,args)],capture_output=True,text=True,timeout=timeout);(E/(label+'.stdout')).write_text(r.stdout);(E/(label+'.stderr')).write_text(r.stderr)
 if r.returncode:raise RuntimeError(label+' failed; see saved logs')
 return json.loads(r.stdout)
current=run('studio-discover-before',[service,'discover','--config',config]);assert current['status']=='ready';(O/'studio-before-config.json').write_bytes(config.read_bytes());(O/'studio-before-config.json').chmod(0o600)
stop=run('studio-native-idle-stop',[service,'stop','--config',config,'--instance',current['instanceId']]);assert stop['status']=='stopped' and stop.get('nativeIdleShutdown') is True
runtime_output=B/'BioMind_Unified_Core_20261009/delivery/submission-20261020'/result['releaseId'][:16]/'workbench-runtime.json'
try:
 rebind=run('studio-rebind',[root/'rebind-managed-services.py','--service-config',config,'--release',release,'--backend-root',B/'BioMind_Unified_Core_20261009/studio/backend/pi-studio','--runtime-output',runtime_output,'--receipt',O/'studio-rebind.json','--apply'])
except BaseException:
 # The rebinder performs its own compare-and-swap and retains backups. If it
 # refuses before applying, restore availability with the still-current config.
 run('studio-ensure-after-rebind-error',[service,'ensure','--config',config]);raise
started=run('studio-ensure',[service,'ensure','--config',config]);deadline=time.monotonic()+120
while time.monotonic()<deadline:
 current=run('studio-discover-after',[service,'discover','--config',config])
 if current['status']=='ready':break
 if current['status'] in ['failed','stopped']:raise RuntimeError('New Studio service did not become ready')
 time.sleep(2)
assert current['status']=='ready'
receipt={'status':'DEPLOYED','release':str(release),'build_id':result['releaseId'],'source_commit':result['sourceCommit'],'discovery':current,'native_idle_shutdown':True,'user_tasks_stopped':False,'credentials_migrated':False,'rebind_receipt':str(O/'studio-rebind.json')};(O/'studio-deployment.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');print(json.dumps(receipt,ensure_ascii=False))
