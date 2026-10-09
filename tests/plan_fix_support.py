import json,os
from pathlib import Path
from copy import deepcopy
from contextlib import contextmanager
from unittest.mock import patch
from agent_core.plan_tracker import load_plan_state,save_plan_state
E=Path(__file__).parent/'plan_fix_evidence';R=Path(os.environ['TMPDIR'])
def read(n):return json.loads((E/n).read_text())
def record(n,v):(R/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def wires(req):return [{'role':m.type,'name':getattr(m,'name',None),'content':m.content,'tool_calls':getattr(m,'tool_calls',[])} for m in req]
def mapped(v,pairs):
 if isinstance(v,dict):return {mapped(k,pairs):mapped(x,pairs) for k,x in v.items()}
 if isinstance(v,list):return [mapped(x,pairs) for x in v]
 if isinstance(v,str):
  import re
  replacements=dict(pairs)
  return re.sub('|'.join(re.escape(a) for a in sorted(replacements,key=len,reverse=True)),lambda m:replacements[m.group(0)],v)
 return v

def derivative(f):
 p=read('accepted_raw_plan.json');old=p['path_bindings']['OUT_ROOT'];pairs=[(old,str(f.root/'out')),('/public/home/liusiqi/03.毕设',str(f.root/'project')),('/public/home/liusiqi/anaconda3/envs/hicexplore',str(f.root/'registered'))]
 p=mapped(p,pairs);p['task_id']=f.task_id;p['path_bindings']['project_dir']=str(f.root);(f.root/'out').mkdir(exist_ok=True)
 return p

def final_obligations(f):
 s=load_plan_state(f.root,f.task_id);spec=s['current_task']['task_spec'];spec['output_contracts']=[{'artifact_id':'final_report','role':'report','canonical_path':str(f.root/'final-report.txt'),'artifact_kind':'file','required':True,'source':'user_explicit'}]
 s['current_task'].pop('task_semantic_hash',None);s['current_task'].pop('task_record_hash',None);save_plan_state(f.root,f.task_id,s,allow_canonical_task_write=True)

@contextmanager
def exact_incident_io(tmp_path):
 s=read('incident_state_projection.json');mapping={};status=s['steps'][0]['outputs'][0]['canonical_path'];local=tmp_path/'stage_status.tsv';local.write_bytes((E/'stage_status_original.tsv').read_bytes())
 receipt=read('attempt-1f3487e131f756cfa5eb37fd.json');st=receipt['after_files'][status];os.utime(local,ns=(st['mtime_ns'],st['mtime_ns']));mapping[status]=local
 for f in E.glob('attempt-*.json'):
  d=json.loads(f.read_text());mapping[d['receipt_ref']]=f
 original={n:getattr(Path,n) for n in ['read_text','read_bytes','open','stat','is_file']}
 def wrapper(n):
  def call(path,*a,**k):return original[n](mapping.get(str(path),path),*a,**k)
  return call
 patches=[patch.object(Path,n,wrapper(n)) for n in original]
 for p in patches:p.start()
 try:yield s
 finally:
  for p in reversed(patches):p.stop()

