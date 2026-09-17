"""Actual loader + existing clip helper, exact request body provenance; read-only."""
import ast,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4]; sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode = True
from agent_core.skill_manager import SkillManager
sm=SkillManager(str(ROOT/'skills'))
raw=(ROOT/'bio_agents.py').read_text(); tree=ast.parse(raw)
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BioAnalystAgent')
clip=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_clip_prompt_text')
clip.decorator_list=[]; ns={};exec(compile(ast.Module(body=[clip],type_ignores=[]),str(ROOT/'bio_agents.py'),'exec'),ns)
needle='never guess an environment path, silently substitute tools, or install one.'
current=[]
for sid in ['genomics/wgs-short-read-mapping','genomics/wgs-gatk-snp-indel']:
 doc=sm.load_skills_for_prompt([sid]); cut=ns['_clip_prompt_text'](doc,4000)
 current.append({'skill_id':sid,'loaded_doc_chars':len(doc),'clipped_doc_chars':len(cut),'mapping_prohibition_in_clipped_install_handover':needle in cut,'prohibition_offset':doc.find(needle),'current_sha256':sm.get_skill_meta(sid)['_skill_content_hash']})
hist=[]
hroot=Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace')
for p in sorted(hroot.glob('*/request.json')):
 for i,m in enumerate(json.loads(p.read_text()).get('messages',[])):
  c=m.get('content','');
  if not isinstance(c,str):continue
  if c.startswith('<inherited_skill_material'):
   metadata=json.loads(c.splitlines()[1]); sid=metadata.get('skill_id','')
   if metadata.get('source_kind')!='skill_document':continue
   hist.append({'path':str(p),'pointer':f'/messages/{i}/content','skill_id':sid,'historical_declared_content_hash':metadata.get('content_hash'),'current_content_hash':sm.get_skill_meta(sid).get('_skill_content_hash'),'historical_and_current_hash_match':metadata.get('content_hash')==sm.get_skill_meta(sid).get('_skill_content_hash'),'mapping_prohibition_present':needle in c})
sid='genomics/wgs-short-read-mapping'
sm.runtime_registry.setdefault('skills',{})[sid]={'env':'audit_user_other_env','status':'verified'}
resolved=sm.get_runtime_requirements(sid)
assert resolved['env']=='bio_wgs'
override={'fixture_recorded_env':'audit_user_other_env','requirements_env':resolved['env'],'explanation':'Real get_runtime_requirements prefers declared default_env, so parent post-install verifier does not accept chosen target_env as input.'}
assert not any(k=='config' or k.startswith('config.') for k in sys.modules)
result={'status':'PASS_CURRENT_BEHAVIOR','production_changes':False,'config_imported':False,'bio_agents_sha256':hashlib.sha256(raw.encode()).hexdigest(),'clip_source_line':clip.lineno,'current_installer_selected_doc_projection':current,'historical_exact_material_versions':hist,'custom_environment_receipt':override}
p=Path(__file__).with_suffix('.json');p.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'output':str(p),'current':current,'historical_material_count':len(hist)},ensure_ascii=False))
