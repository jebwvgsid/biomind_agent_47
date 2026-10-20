"""Start each actual new task adapter, inspect loaded resources, then shut down.
No prompt is sent, no model credentials are read, no scientific task executes.
"""
from pathlib import Path
import json, subprocess, os, time, select
R=Path('/public/home/liusiqi/03.毕设/BioMind_Production_20261020');root=R/'benchmark-harness';manifest=json.loads((root/'manifest.json').read_text());results=[]
for t in manifest['tasks']:
    frames=[];buf=b'';rd=Path(t['run_dir']);env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1')
    command=[str(root/'biomind-entry.py'),'--project-dir',t['project_dir'],'--pi-mode','json'];err=(rd/'load-only.stderr').open('w');p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,env=env)
    def wait(predicate,timeout=90):
        global buf
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            for f in frames:
                if predicate(f):return f
            if p.poll() is not None:raise RuntimeError('Entry exited before response')
            if select.select([p.stdout],[],[],.1)[0]:
                buf+=os.read(p.stdout.fileno(),65536)
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1)
                    if line:
                        try:frames.append(json.loads(line))
                        except ValueError:pass
        raise TimeoutError('Entry response timeout')
    def send(x):p.stdin.write((json.dumps(x)+'\n').encode());p.stdin.flush()
    try:
        ready=wait(lambda f:f.get('type')=='ready');assert ready['build_id']==manifest['build_id']
        for q in ['get_tools','get_resources']:
            send({'type':q,'id':q});wait(lambda f:f.get('id')==q)
        send({'type':'shutdown','id':'close'});p.stdin.close();p.wait(30)
        result={'case_id':t['case_id'],'status':'PASS_LOAD_ONLY','command':command,'ready':ready,'model_calls':0,'science_calls':0}
    finally:
        if p.poll() is None:p.terminate();p.wait(10)
        err.close();(rd/'load-only.frames.jsonl').write_text(''.join(json.dumps(f,ensure_ascii=False)+'\n' for f in frames))
    tools=next(f for f in frames if f.get('id')=='get_tools');result['tools_response']=tools
    assert all(name in json.dumps(tools) for name in ['fetch_content','web_search','get_search_content']), 'Native web tools missing'
    results.append(result);print(t['case_id'],'PASS_LOAD_ONLY',flush=True)
(R/'evidence/original-case-harness-load-only.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
