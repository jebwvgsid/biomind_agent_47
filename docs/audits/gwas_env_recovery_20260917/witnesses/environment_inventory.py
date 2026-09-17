"""Bounded formal-index inventory and read-only help/package probes.

No recursive disk search: configured env root direct children, Conda's recorded
environment list, registry named/source paths and configured source root only.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO=Path('/public/home/liusiqi/03.毕设/Biomind_v1')
OUT=REPO/'docs/audits/gwas_env_recovery_20260917/evidence/environment_inventory.json'
CONDA=Path('/public/home/liusiqi/anaconda3')
SHIM=Path('/public/home/liusiqi/03.毕设/agent_shim')
SOURCE=Path('/public/home/liusiqi/03.毕设/source_tools')
TOOLS=['bcftools','plink','plink2','emmax','emmax-intel64','emmax-kin','emmax-kin-intel64','Rscript','bedtools','LDBlockShow','ShowLDSVG','beagle','java','python']
def sha(raw):return hashlib.sha256(raw).hexdigest()
def guard(event,args):
 if event in {'socket.connect','socket.getaddrinfo','os.system'}:raise PermissionError('NETWORK_OR_OS_SYSTEM_DENIED')
 if event=='open' and isinstance(args[0],(str,bytes)):
  p=Path(os.fsdecode(args[0]))
  if p.name=='.env.api_keys':raise PermissionError('API_KEYS_READ_DENIED')
  mode,flags=args[1] or '',args[2] or 0
  if any(c in str(mode) for c in 'wax+') or flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT):
   if not(str(p.absolute()).startswith('/tmp/') or p.absolute()==OUT):raise PermissionError('WRITE_SCOPE_DENIED')
sys.addaudithook(guard)
config={}
for p in [REPO/'.env',REPO.parent/'.env']:
 if not p.is_file():continue
 fields={}
 for line in p.read_text().splitlines():
  k,sep,v=line.partition('=');k=k.strip().removeprefix('export ').strip()
  if sep and k in {'ANACONDA_ROOT','BIO_SOFTWARE_ROOT','BIO_SOURCE_INSTALL_DIR','BIOMIND_RUNTIME_REGISTRY_FILES','BIOMIND_SKILLS_DIR'}:
   fields[k]=v.strip().strip('"\'')
 config[str(p)]=fields
registry=json.loads((SHIM/'software_registry.json').read_text())
prefixes={str(CONDA/'envs'/name):['software_registry'] for name in registry.get('environments',{})}
for p in (CONDA/'envs').iterdir():
 if p.is_dir():prefixes.setdefault(str(p),[]).append('configured_conda_root_direct_child')
index=Path('/public/home/liusiqi/.conda/environments.txt')
conda_index={'path':str(index),'exists':index.is_file()}
if index.is_file():
 raw=index.read_bytes();conda_index['sha256']=sha(raw)
 for line in raw.decode().splitlines():
  if line.startswith('/'):prefixes.setdefault(line,[]).append('conda_environments_index')
hits=[]
for prefix,origins in prefixes.items():
 for name in TOOLS:
  p=Path(prefix)/'bin'/name
  if p.exists():hits.append({'tool':name,'path':str(p),'realpath':str(p.resolve()),'executable':os.access(p,os.X_OK),'origins':origins})
for name,record in registry.get('tools',{}).items():
 if name not in TOOLS:continue
 for key in ['shim_path','source_path','source_executable']:
  value=record.get(key)
  if not value:continue
  p=Path(value)
  if key=='source_path' and p.is_dir():p=p/name
  if p.exists():hits.append({'tool':name,'path':str(p),'realpath':str(p.resolve()),'executable':os.access(p,os.X_OK),'origins':['registered_tools.'+key]})
for name in TOOLS:
 for p in [SOURCE/name,SOURCE/'bin'/name,SHIM/'bin_shims'/name]:
  if p.is_file():hits.append({'tool':name,'path':str(p),'realpath':str(p.resolve()),'executable':os.access(p,os.X_OK),'origins':['configured_source_or_shim_exact_name']})
setup=SHIM/'tools_setup.yaml'
setup_fact={'path':str(setup),'exists':setup.is_file()}
if setup.is_file():
 import yaml
 raw=setup.read_bytes();setup_fact['sha256']=sha(raw)
 parsed=yaml.safe_load(raw) or {}
 setup_fact['top_level_keys']=list(parsed) if isinstance(parsed,dict) else []
 setup_fact['entries_count']=len(parsed)
probes=[]
with tempfile.TemporaryDirectory(prefix='biomind-env-inventory-') as tmp:
 env={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':tmp,'TMPDIR':tmp}
 commands=[]
 # Exact candidates from bounded indexes; no EMMAX scientific computation.
 for name in ['plink','plink2','emmax','emmax-intel64','bedtools','LDBlockShow','ShowLDSVG']:
  candidates=[h for h in hits if h['tool']==name and h['executable']]
  for hit in candidates[:1]:
   suffix=['--version'] if name in {'plink','plink2','bedtools'} else ['-h']
   commands.append((name,[hit['path'],*suffix]))
 # Representative R envs; package status is environment-specific, not global.
 rhits=[h for h in hits if h['tool']=='Rscript' and h['executable']]
 rhits.sort(key=lambda h:(not('/bio_gatk/' in h['path']),h['path']))
 for hit in rhits[:3]:
  expr='for(p in c("CMplot","qqman")){cat(p,"=",requireNamespace(p,quietly=TRUE),"\\n",sep="")}'
  commands.append(('Rscript_packages',[hit['path'],'--vanilla','-e',expr]))
 for tool,argv in commands:
  try:
   proc=subprocess.run(argv,cwd=tmp,env=env,capture_output=True,text=True,timeout=10)
   probes.append({'tool':tool,'argv':argv,'returncode':proc.returncode,'stdout':proc.stdout[:12000],'stderr':proc.stderr[:3000],
                  'stdout_sha256':sha(proc.stdout.encode()),'status':'completed'})
  except subprocess.TimeoutExpired as exc:probes.append({'tool':tool,'argv':argv,'status':'timeout','seconds':10})
report={'captured_utc':datetime.now(timezone.utc).isoformat(),'interpreter':sys.executable,'witness_sha256':sha(Path(__file__).read_bytes()),
        'config_whitelist':config,'conda_index':conda_index,'tools_setup':setup_fact,'prefixes':prefixes,'tool_candidates':hits,'probes':probes,
        'limits':'No recursion, no arbitrary user PATH scan, no install, no original session write; indexed candidate absence does not establish server-wide absence; R package probe covers only shown prefixes.'}
OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'result':str(OUT),'prefixes':len(prefixes),'tool_candidates':len(hits),'probes':[(p['tool'],p.get('returncode'),p['status']) for p in probes]}))
