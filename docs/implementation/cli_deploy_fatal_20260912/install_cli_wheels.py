"""Two verified local wheels, target interpreter only, no network/config reads.

Run with biomind Python -I -S, PIP_CONFIG_FILE=/dev/null and a synthetic env.
Dry-run and install are separate explicit invocations; retain both pip reports.
"""
import base64
import csv
import datetime
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import sysconfig
import zipfile

EXPECTED={
 'prompt_toolkit-3.0.52-py3-none-any.whl':'9aac639a3bbd33284347de5ad8d68ecc044b91a762dc39b7c21095fcd6a19955',
 'wcwidth-0.2.13-py2.py3-none-any.whl':'3da69048e4540d84af32131829ff948f1e022c1c6bdb8d6102117aac784f6859',
}
TARGET='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
assert sys.executable==TARGET and sys.flags.isolated and sys.flags.no_site
sys.dont_write_bytecode=True
mode,wheel_dir,result_dir=sys.argv[1:];assert mode in {'dry-run','install'}
wheel_dir=Path(wheel_dir).resolve();result_dir=Path(result_dir).resolve();result_dir.mkdir(exist_ok=True)
site=Path(sysconfig.get_path('purelib'));sys.path.insert(0,str(site))
assert os.environ.get('PIP_CONFIG_FILE')==os.devnull

def guard(event,args):
 if event in {'socket.connect','socket.getaddrinfo'}:raise RuntimeError('Offline wheel install forbids network')
 if event=='open' and isinstance(args[0],(str,bytes)):
  p=Path(os.fsdecode(args[0])).resolve()
  if p.name.startswith('.env') or p.name in {'启动.sh','.netrc','pip.conf','pip.ini','.gitconfig'}:
   raise RuntimeError('Sensitive configuration read denied')
sys.addaudithook(guard)
from pip._vendor.packaging.requirements import Requirement
from pip._internal.cli.main import main as pip_main

def inventory():
 out={}
 for d in importlib.metadata.distributions(path=[str(site)]):
  name=d.metadata.get('Name','').lower().replace('_','-')
  out[name]={'version':d.version,'location':str(d.locate_file(''))}
 return out
before=inventory();consumers=[]
for d in importlib.metadata.distributions(path=[str(site)]):
 for raw in d.requires or []:
  req=Requirement(raw)
  if req.name.lower().replace('_','-') in {'prompt-toolkit','wcwidth'}:
   consumers.append({'consumer':d.metadata['Name'],'requirement':raw})
for name in EXPECTED:
 path=wheel_dir/name;assert not path.is_symlink()
 assert hashlib.sha256(path.read_bytes()).hexdigest()==EXPECTED[name]
 with zipfile.ZipFile(path) as z:
  names=z.namelist();assert all(not PurePosixPath(p).is_absolute() and '..' not in PurePosixPath(p).parts for p in names)
  assert not any('.data/scripts/' in p or p.endswith(('.so','.dll','.exe')) for p in names)
  meta=z.read(next(p for p in names if p.endswith('.dist-info/METADATA'))).decode()
  wheel=z.read(next(p for p in names if p.endswith('.dist-info/WHEEL'))).decode();assert 'Root-Is-Purelib: true' in wheel
  for line in meta.splitlines():
   if not line.startswith('Requires-Dist: '):continue
   req=Requirement(line.split(': ',1)[1])
   if req.marker is None or req.marker.evaluate({'python_version':'3.12','python_full_version':'3.12.12','extra':''}):
    assert req.name.lower()=='wcwidth' and req.specifier.contains('0.2.13'),str(req)
  rec=next(p for p in names if p.endswith('.dist-info/RECORD'))
  for p,h,size in csv.reader(io.StringIO(z.read(rec).decode())):
   if not h:assert p==rec;continue
   algorithm,expected=h.split('=',1);assert algorithm=='sha256';body=z.read(p)
   assert base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b'=').decode()==expected and len(body)==int(size)
if mode=='dry-run':
 assert 'prompt-toolkit' not in before and 'wcwidth' not in before
 (result_dir/'before.json').write_text(json.dumps(before,indent=2));(result_dir/'existing-consumers.json').write_text(json.dumps(consumers,indent=2))
else:
 assert before==json.loads((result_dir/'before.json').read_text()),'Environment changed after dry-run'
 report=json.loads((result_dir/'dry-run-report.json').read_text())
 planned={r['metadata']['name'].lower().replace('_','-'):r['metadata']['version'] for r in report['install']}
 assert planned=={'prompt-toolkit':'3.0.52','wcwidth':'0.2.13'},planned
report=result_dir/(mode+'-report.json');assert not report.exists()
args=['--isolated','--disable-pip-version-check','--no-cache-dir','install','--no-index','--only-binary=:all:','--no-compile',
      '--report',str(report),*[str(wheel_dir/name) for name in EXPECTED]]
if mode=='dry-run':args.append('--dry-run')
(result_dir/(mode+'-invocation.json')).write_text(json.dumps({'executable':sys.executable,'pip_args':args,'pip_version':before['pip']['version'],
 'network':'forbidden by audit hook','config':'PIP_CONFIG_FILE=/dev/null; pip config reads denied',
 'began_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'expected_wheels':EXPECTED},indent=2))
code=pip_main(args)
after=inventory();changed={k:v for k,v in after.items() if before.get(k)!=v};removed=sorted(set(before)-set(after))
receipt={'mode':mode,'returncode':code,'changed':changed,'removed':removed,'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
(result_dir/(mode+'-receipt.json')).write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))
assert code==0
if mode=='dry-run':assert not changed and not removed
else:
 assert not removed and {k:v['version'] for k,v in changed.items()}=={'prompt-toolkit':'3.0.52','wcwidth':'0.2.13'}
 (result_dir/'after.json').write_text(json.dumps(after,indent=2))
