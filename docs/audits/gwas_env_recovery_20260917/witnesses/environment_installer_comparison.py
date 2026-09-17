"""AST-preserved old installer candidate block versus current target selection.

No installer graph, approval, package manager initialization or real installation.
The extracted statement bodies are unmodified. Wrappers only expose local results.
"""
import ast
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types

BASE=Path('/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/prerequisite_source')
CURRENT=Path('/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source')
OUT=Path('/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/environment_installer_comparison.json')
def find(root,path,name):
 raw=(root/path).read_text();tree=ast.parse(raw)
 n=next(n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
 text='\n'.join(raw.splitlines()[n.lineno-1:n.end_lineno])
 return n,{'file':str(root/path),'line':n.lineno,'file_sha256':hashlib.sha256(raw.encode()).hexdigest(),'method_sha256':hashlib.sha256(text.encode()).hexdigest()}

async def main():
 old,old_source=find(BASE,'bio_agents.py','install_subagent')
 candidate=next(n for n in old.body if isinstance(n,ast.If) and 'find_matching_environment' in ast.unparse(n.test)+ast.unparse(n))
 # Locate the dedicated reuse_match block, excluding earlier unrelated ifs.
 candidate=next(n for n in old.body if isinstance(n,ast.If) and 'runtime_defaults.get' in ast.unparse(n.test) and 'find_matching_environment' in ast.unparse(n))
 wrapper=ast.parse('async def witness():\n context_summary=""\n default_env="declared_missing"\n return locals()').body[0]
 wrapper.body=[*wrapper.body[:-1],deepcopy(candidate),wrapper.body[-1]]
 calls=[]
 fixture={'env':'registered_candidate','match_type':'partial','missing_packages':['plink'],'missing_capabilities':['emmax-association']}
 ns={'self':types.SimpleNamespace(pm=types.SimpleNamespace(find_matching_environment=lambda req:(calls.append(deepcopy(req)) or deepcopy(fixture)))),
     'runtime_defaults':{'language':'bash','packages':['bcftools','plink'],'capabilities':['emmax-association']},
     'explicit_runtime_repair':False,'context_summary':'','default_env':'declared_missing'}
 exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(BASE/'bio_agents.py'),'exec'),ns)
 observed=await ns['witness']()
 assert len(calls)==1 and observed['default_env']=='registered_candidate'
 new,new_source=find(CURRENT,'bio_agents.py','_install_subagent_impl')
 selected=next(n for n in new.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='target_env' for t in n.targets))
 inputs={'raw':{},'evidence':{},'defaults':{'env':'declared_missing'}}
 exec(compile(ast.fix_missing_locations(ast.Module(body=[deepcopy(selected)],type_ignores=[])),str(CURRENT/'bio_agents.py'),'exec'),inputs)
 assert inputs['target_env']=='declared_missing'
 all_calls=[ast.unparse(n.func) for n in ast.walk(new) if isinstance(n,ast.Call)]
 assert not any('find_matching_environment' in c or '_reuse_registered_runtime_for_install' in c for c in all_calls)
 env_cases=[]
 for root in [BASE,CURRENT]:
  fn,source=find(root,'agent_core/skill_manager.py','get_runtime_env')
  ns={};exec(compile(ast.fix_missing_locations(ast.Module(body=[deepcopy(fn)],type_ignores=[])),str(root/'agent_core/skill_manager.py'),'exec'),ns)
  for status in ['verified','broken','registered']:
   owner=types.SimpleNamespace(get_skill_meta=lambda sid:{'runtime':{'default_env':'declared_missing'}},
                              get_runtime_record=lambda sid:{'env':'registered_candidate','status':status})
   env_cases.append({'snapshot':str(root),'status':status,'selected':ns['get_runtime_env'](owner,'fixture'),'source':source})
 result={'old_partial_match':{'source':old_source,'statement_line':candidate.lineno,'calls':calls,'selected_default':observed['default_env'],'candidate_message':observed['context_summary']},
         'current_selection':{'source':new_source,'statement_line':selected.lineno,'selected_target':inputs['target_env'],'candidate_or_registered_reuse_calls':[]},
         'get_runtime_env_same_condition':env_cases,
         'classification':'Installer candidate discovery caller removed without equivalent automatic scoped-workorder replacement; not reached in GWAS incident',
         'boundary':'AST real statements plus leaf candidate fixture, not full Installer/HITL graph; existing function library remains, production scoped caller does not invoke it',
         'witness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
 OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(str(OUT))
asyncio.run(main())
