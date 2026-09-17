"""Current-source isolated witnesses; no config import, LLM, installation or real task.
AST loads unchanged production class bodies to avoid module-level config secret loading.
GraphCapture exposes the real nested execute/router; only transport/runner/reviewer replaced.
"""
import ast, asyncio, hashlib, json, logging, os, re, shlex, shutil, subprocess, sys, tempfile
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Literal, Optional, List, Mapping
ROOT=Path(__file__).resolve().parents[4]
OUT=Path(__file__).with_suffix('.json')
class Msg:
    def __init__(self,content): self.content=content
class HumanMessage(Msg): pass
class AIMessage(Msg): pass
class SystemMessage(Msg): pass
class GraphCapture:
    def __init__(self,*args): self.nodes={}; self.routes={}
    def add_node(self,k,v): self.nodes[k]=v
    def add_edge(self,*args): pass
    def add_conditional_edges(self,k,fn,*args): self.routes[k]=fn
    def compile(self,**kwargs): return self
sources={}
def source_class(path,name,ns):
    p=ROOT/path; raw=p.read_text(); tree=ast.parse(raw)
    target=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==name)
    sources[str(p)]={'sha256':hashlib.sha256(raw.encode()).hexdigest(),'class_line':target.lineno}
    exec(compile(ast.Module(body=[target],type_ignores=[]),str(p),'exec'),ns)
    return ns[name]
async def main():
    launched=[]
    async def fake_run(code): launched.append(code); return ('synthetic runner only','',0)
    ns=dict(globals(), StateGraph=GraphCapture, InstallerState=dict, START='START',END='END',
            LLM_STEP_CONTEXT_MESSAGES=6,LLM_STEP_CONTEXT_CHARS=12000,
            detect_script_language=lambda c:'Bash',normalize_script_code=lambda c:c.strip(),
            run_bash_script=fake_run,run_python_repl=fake_run,logger=logging.getLogger('audit'))
    Installer=source_class(Path('agent_core/installer.py'),'InstallerAgent',ns)
    PM=source_class(Path('package_manager.py'),'PackageManager',ns)
    results=[]
    with tempfile.TemporaryDirectory(prefix='biomind-install-audit-') as folder:
        p=Path(folder); pm=PM.__new__(PM); pm.registry={'tools':{},'environments':{}}
        pm.registry_path=p/'registry.json'; pm.conda_root=None; pm.conda_exe=None
        pm.source_root=p; pm.shim_dir=p/'shims'; pm.work_dir=p
        class Review:
            calls=0; response='approve'
            def review_code(self,code,language): self.calls+=1; return self.response,code
        review=Review(); agent=Installer.__new__(Installer); agent.pm=pm; agent.checkpointer=None; agent.middleware=review
        graph=agent._build_graph()
        # No earlier command or successful verification; original tag path writes actual isolated registry.
        state={'tool_name':'audit_missing','messages':[AIMessage('<register_environment env="audit_missing" language="bash" healthcheck="audit_missing --version" />')]}
        update=await graph.nodes['execute'](state); route=graph.routes['execute']({**state,'messages':state['messages']+update['messages']})
        assert route=='end_success' and not launched and pm.registry['environments']['audit_missing']['status']=='registered'
        results.append({'case':'registration_without_verification','status':'PASS_CURRENT_BEHAVIOR','runner_calls':0,'router':route,'registry_status':'registered','final':graph.nodes['end_success'](state)['final_result'],'interpretation':'Installer inner graph can claim complete from registration alone; parent recheck is a separate gate.'})
        # Candidate is data sent only to intercepted runner, never shell executed.
        command='conda run -n audit_missing python -m pip install audit_nonexistent_package'
        state={'tool_name':'audit_missing','messages':[AIMessage('```bash\n'+command+'\n```')]}
        await graph.nodes['execute'](state)
        assert review.calls==0 and len(launched)==1 and command in launched[-1]
        results.append({'case':'conda_run_install_bypasses_review','status':'PASS_CURRENT_BEHAVIOR','review_calls':review.calls,'runner_calls':len(launched),'command_data':command,'interpretation':'No strict contract in this fixture; prefix low-friction branch bypasses existing reviewer, runner replaced.'})
        launched.clear(); review.response='quit'
        state={'tool_name':'audit_missing','messages':[AIMessage('```bash\nconda install -n audit_missing audit_nonexistent_package\n```')]}
        update=await graph.nodes['execute'](state); route=graph.routes['execute']({**state,'messages':state['messages']+update['messages']})
        assert route=='generate' and not launched
        results.append({'case':'quit_returns_to_generate','status':'PASS_CURRENT_BEHAVIOR','runner_calls':0,'observation':update['messages'][0].content,'router':route,'interpretation':'Installer quit text is not terminal marker consumed by router.'})
    # Original classifier, exact production AST method; output projection is a plain fixture.
    raw=(ROOT/'agent_core/workflow/early_nodes.py').read_text(); tree=ast.parse(raw)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TypedInstallSubagent')
    fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='classify')
    cns=dict(globals(),task_status=lambda d:d.get('task_status',''),_outcome=lambda contract,kind,state,durable,payload,summary:{'type':kind,'payload':payload},_semantic_harness_error=lambda *a,**k:{'type':'harness_error'})
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(ROOT/'agent_core/workflow/early_nodes.py'),'exec'),cns)
    class Owner:contract=None
    state={'runtime_status':{'old_skill':{'ok':True},'target_skill':{'ok':False}}}
    observed=cns['classify'](Owner(),{},state,{'current_step_id':'target','steps':[{'step_id':'target','status':'running'}]})
    assert observed['type']=='capability_available'
    results.append({'case':'mixed_runtime_receipts','status':'PASS_CURRENT_BEHAVIOR','input':state,'output':observed,'interpretation':'Unscoped any(ok) precedes target failure; no matching target/attempt check in classifier.'})
    assert not any(k=='config' or k.startswith('config.') for k in sys.modules)
    OUT.write_text(json.dumps({'status':'current_behavior_witness_only','production_fix':False,'real_installation':False,'real_model':False,'real_task_resume':False,'config_module_imported':False,'source_hashes':sources,'results':results},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(OUT),'cases':len(results),'all':'PASS_CURRENT_BEHAVIOR'},ensure_ascii=False))
asyncio.run(main())
