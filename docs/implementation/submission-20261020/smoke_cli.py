"""Load actual production CLI defaults; inspect resources and exit without a model request."""
import argparse,json,os,select,subprocess,time
from pathlib import Path
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';p=argparse.ArgumentParser();p.add_argument('--entry',required=True);p.add_argument('--expected',required=True);p.add_argument('--label',required=True);a=p.parse_args();run=O/'evidence'/a.label;run.mkdir();project=run/'project';project.mkdir();frames=[];buf=b''
cmd=[a.entry,'--project-dir',str(project),'--pi-mode','json','--session-dir',str(run/'sessions')]
err=(run/'stderr.log').open('w');proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,cwd=project,env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1'))
def wait(pred,seconds=90):
 global buf
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  for f in frames:
   if pred(f):return f
  if proc.poll() is not None:raise RuntimeError('Production entry exited: '+str(proc.returncode))
  if select.select([proc.stdout],[],[],.1)[0]:
   buf+=os.read(proc.stdout.fileno(),65536)
   while b'\n' in buf:
    line,buf=buf.split(b'\n',1)
    try:frames.append(json.loads(line))
    except ValueError:pass
 raise TimeoutError('Production CLI handshake')
def send(v):proc.stdin.write((json.dumps(v)+'\n').encode());proc.stdin.flush()
result={'command':cmd,'model_requests':0,'scientific_runs':0}
try:
 ready=wait(lambda f:f.get('type')=='ready');assert ready['loaded_build']['buildId']==a.expected
 result['ready']=ready
 for q in ['get_tools','get_resources']:
  send({'type':q,'id':q});result[q]=wait(lambda f:f.get('id')==q,20)
 send({'type':'shutdown','id':'close'});proc.stdin.close();proc.wait(30);assert proc.returncode==0
 result['status']='PASS'
except Exception as e:result.update(status='FAIL',error=str(e))
finally:
 if proc.poll() is None:proc.terminate();proc.wait(10)
 err.close();(run/'frames.jsonl').write_text(''.join(json.dumps(f,ensure_ascii=False)+'\n' for f in frames));(run/'receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':result['status'],'label':a.label,'error':result.get('error')}));raise SystemExit(result['status']!='PASS')
