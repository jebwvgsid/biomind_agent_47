"""Atomically switch both supported CLI launchers; preserve prior release and user resources."""
from pathlib import Path
import argparse,ast,json,hashlib,os,shutil,datetime
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_LongTask_20261021'
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release',type=Path);p.add_argument('--apply',action='store_true');p.add_argument('--rollback',action='store_true');args=p.parse_args()
def sha(b):return hashlib.sha256(b).hexdigest()
def atomic(p,b,mode):
 t=p.with_name('.'+p.name+'.long-task-20261021.tmp');fd=os.open(t,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
 with os.fdopen(fd,'wb') as f:f.write(b);f.flush();os.fsync(f.fileno())
 os.replace(t,p)
entries=[Path('/public/home/liusiqi/.local/bin/biomind'),B/'BioMind/bin/biomind']
receipt=O/'cli-deployment.json'
if args.rollback:
 r=json.loads(receipt.read_text())
 for x in r['entries']:
  if sha(Path(x['path']).read_bytes())!=x['after_sha256']:raise RuntimeError('Launcher changed since deployment')
 for x in r['entries']:atomic(Path(x['path']),Path(x['backup']).read_bytes(),0o700)
 print(json.dumps({'status':'ROLLED_BACK','release':r['previous_release'],'resources_changed':False}));raise SystemExit
release=args.release.resolve(strict=True);identity=json.loads((release/'RELEASE.json').read_text());build=identity['releaseId'];manifest=json.loads((release/'integrations/pi_runtime/dist/build-manifest.json').read_text());assert manifest['buildId']==build
assert not receipt.exists(),'Deployment receipt already exists; do not overwrite it'
old=entries[0].read_text();values={}
for n in ast.parse(old).body:
 if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ['RELEASE','CONFIG','MODEL','EXPECTED']:values[n.targets[0].id]=ast.literal_eval(n.value)
assert values['EXPECTED']=='d0cf9799989657590c3ca3dc2d4fc49b5568a82d8aeae548d67ff1e1b412c5c7','Production changed; review new base'
for e in entries:assert e.is_file() and not e.is_symlink() and e.read_text()==old,'Launcher baseline differs'
config=json.loads(Path(values['CONFIG']).read_text());config['installRoot']=str(release)
delivery=B/'BioMind_Unified_Core_20261009/delivery/long-task-20261021'/build[:16];delivery.mkdir(parents=True,exist_ok=True);newconfig=delivery/'cli-shared-web-runtime.json'
configbytes=(json.dumps(config,ensure_ascii=False,indent=2)+'\n').encode()
new=old
for key,value in [('RELEASE',str(release)),('CONFIG',str(newconfig)),('EXPECTED',build)]:new=new.replace(key+'='+repr(values[key]),key+'='+repr(value),1)
compile(new,'biomind','exec')
(delivery/'biomind.candidate').write_text(new);(delivery/'cli-runtime.candidate.json').write_bytes(configbytes)
plan={'status':'PREPARED','release':str(release),'build_id':build,'source_commit':manifest['sourceCommit'],'previous_release':values['RELEASE'],'previous_config':values['CONFIG'],'config':str(newconfig),'entries':[],'settings_changed':False,'tasks_stopped':False}
backups=O/'backups';backups.mkdir(exist_ok=True)
for i,e in enumerate(entries):
 backup=backups/f'cli-{i}.before'
 if backup.exists():assert backup.read_bytes()==e.read_bytes(),'Prepared launcher baseline changed'
 else:backup.write_bytes(e.read_bytes());backup.chmod(0o700)
 plan['entries'].append({'path':str(e),'before_sha256':sha(e.read_bytes()),'after_sha256':sha(new.encode()),'backup':str(backup)})
(O/'cli-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
if not args.apply:print(json.dumps({'status':'PREPARED','plan':str(O/'cli-plan.json')}));raise SystemExit
for x in plan['entries']:assert sha(Path(x['path']).read_bytes())==x['before_sha256'],'Concurrent launcher change'
if newconfig.exists():assert newconfig.read_bytes()==configbytes,'Prepared runtime configuration changed'
else:atomic(newconfig,configbytes,0o600)
changed=[]
try:
 for x in plan['entries']:atomic(Path(x['path']),new.encode(),0o700);changed.append(x)
except BaseException:
 for x in changed:atomic(Path(x['path']),Path(x['backup']).read_bytes(),0o700)
 raise
plan.update(status='DEPLOYED',at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
receipt.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'status':plan['status'],'build_id':build,'launchers':len(entries)}))
