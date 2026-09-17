from pathlib import Path
import ast,datetime,hashlib,json
ROOT=Path(__file__).resolve().parents[4]
paths=['bio_agents.py','agent_core/installer.py','package_manager.py','agent_core/skill_manager.py','agent_core/workflow/early_nodes.py','agent_core/workflow/execution.py','agent_core/workflow/runtime_graph.py','agent_core/workflow/outcomes.py','agent_core/task_spec.py','agent_core/plan_tracker.py','agent_core/middleware.py','agent_core/state.py','prompts/manager.py','prompts/templates.py','agent_core/controller_audit.py']
names={'install_subagent','_mark_plan_current_done','_persist_plan_tracker','_runtime_install_disallowed_by_task_spec','_selected_skill_install_command_context','_record_installed_skill_environment','_redirect_install_command','_clip_prompt_text','_next_step_after_install_result','_is_low_friction_installer_command','_runtime_contract_violation','inst_execute','inst_router','_build_graph','register_environment','register_existing_tool','install_package','verify_environment','_save_registry','load_skill','_discover_skills','_rebuild_skill_lookup','route_install_outcome','route_execute_outcome','semantic_install_policy','advance_plan_after_verified_step','save_plan_state','_atomic_write_plan_state','review_code','get_installer_system_prompt'}
rows=[]
for rel in paths:
 p=ROOT/rel;raw=p.read_bytes();tree=ast.parse(raw)
 rows.append({'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'mtime_ns':p.stat().st_mtime_ns,'definitions':[{'name':n.name,'line':n.lineno,'end_line':n.end_lineno} for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]})
p=Path(__file__).with_suffix('.json');p.write_text(json.dumps({'captured_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'version_scope':'working tree at capture; production changed externally during audit','files':rows},ensure_ascii=False,indent=2)+'\n')
print(p)
