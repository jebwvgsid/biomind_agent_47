"""Read only explicitly authorized session artifacts; export bounded public facts.

This is a stdlib forensic reader, never a BioMind session loader. It never
executes source/scripts or reads scientific inputs referenced inside evidence.
"""
import ast
import difflib
import hashlib
import json
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
ROOT=REPO.parent/'benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789388849'
OUT=REPO/'docs/audits/execution_architecture_20260914_unified_a1/incident/evidence.json'
TAGS=('generate-0009-03fe76999849','generate-0011-f76f4373bd56','generate-0012-68a18c431aca')
FILES=['plan_state.json','executions/execution_records.jsonl',
       'state_contract/objects/53a0cb3458274a5b138813f8758fea8e6f5f1db08dbc4610e75ab4521b7b3673.json']
FILES += ['attempts/attempt_be39ca0c6335578331a1/'+x for x in ('approved.sh','executed.sh','exit_status.json','stdout.log','stderr.log','failure_evidence.json')]
FILES += ['context_trace/'+tag+'/'+name for tag in TAGS for name in ('request.json','response.json','manifest.json')]
def sha(x):return hashlib.sha256(x).hexdigest()
def read(name):
    assert name in FILES
    return (ROOT/name).read_bytes()
def obj(name):return json.loads(read(name))
def compact(x,keys):return {k:x[k] for k in keys if k in x}
state=obj('plan_state.json');ledger=state['task_state']['candidate_repair_state']
evidence={'incident_root':str(ROOT),'safety':{'business_load':False,'original_script_execution':False,'input_reads':False,'private_reasoning_export':False},
 'files':{name:{'bytes':len(read(name)),'sha256':sha(read(name))} for name in FILES},'calls':{},'rejections':{},'current_sources':{}}
evidence['durable_routing_and_selection']={'workflow_routing_schema_version':state['task_state'].get('workflow_routing_schema_version'),**compact(state,('selected_skill_ids','selected_skill_refs','approval_snapshot_ref','plan_snapshot_ref','plan_status','approval_status','execution_status'))}
evidence['managed_stop']=compact(state['task_state'].get('managed_run_stop',{}),('stop_reason','automatic_actions_stopped','technical_classification','execution','reports_status','report_receipts','status','stage','associated_attempts','current_candidate','causal_generation','causal_execution','primary_cause'))
for tag in TAGS:
    resp=obj('context_trace/'+tag+'/response.json');content=resp['content']
    # Only these three known public Generate completions are examined; private
    # messages/reasoning fields are neither selected nor copied.
    assert not any(token in content.lower() for token in ('<think>','<analysis>','<reasoning>'))
    req=obj('context_trace/'+tag+'/request.json');man=obj('context_trace/'+tag+'/manifest.json')
    try:parsed=json.loads(content);shape={'json_decodable':True,'top_keys':list(parsed),'action_type':parsed.get('action_type'),'code_present':bool(parsed.get('code'))}
    except ValueError:shape={'json_decodable':False,'wire':'bash fenced block followed by public prose'}
    contract=req['messages'][2]['content']
    evidence['calls'][tag]={'response_content_sha256':sha(content.encode()),'response_chars':len(content),'business_shape':shape,
       'response_completion':{'archive_status':man['status'],'finish_reason':'UNKNOWN (response archive lacks metadata)','content_integrity':'UNKNOWN'},
       'manifest':compact(man,('request_hash','response_hash','prompt_hash','runtime_identity','selected_skill_ids','skill_versions','generate_output_policy')),
       'provider_messages':[{'pointer':'/messages/'+str(i)+'/content','role':m['role'],'name':m.get('name'), 'chars':len(m['content']), 'sha256':sha(m['content'].encode()),
         'raw_json_instruction': 'Return exactly one raw JSON' in m['content'],'canonical_execute_example':'name="canonical_execute"' in m['content'],
         'action_json_phrase':'one action_json' in m['content'],'helper_block_instruction':'Return ONE complete executable block' in m['content']} for i,m in enumerate(req['messages'])],
       'helper_public_contract':json.loads(contract)}
    if tag.endswith('99849'):
       body=parsed['argv'][2]; evidence['calls'][tag]['script']={'pointer':'/content -> JSON /argv/2','sha256':sha(body.encode()),'marker_count':body.count('# BIOMIND_EFFECT_HELPER'),
          'helper_definition_present':'def emit_effect_evidence' in body,'emission_call':body.splitlines()[-2]}
    elif tag.endswith('bd56'):
       body=content.split('```bash\n',1)[1].split('\n```',1)[0];evidence['calls'][tag]['script']={'sha256':sha(body.encode()),'helper_definition_present':'def emit_effect_evidence' in body,'emission_call':body.splitlines()[-2]}
    else:
       body=parsed['action_json']['code'];evidence['calls'][tag]['script']={'pointer':'/content -> JSON /action_json/code','sha256':sha(body.encode()),'helper_definition_present':'def emit_effect_evidence' in body,'emission_call':body.splitlines()[-2]}
for key,row in ledger['events'].items():
    evidence['rejections'][key]=compact(row,('scope','candidate_fingerprint','error_fingerprint','repair_stage','repair_domain','repair_ordinal','repair_limit','progress_proven','public_valid_facts','validation_source','contract_ref','reason_code','status','terminal_reason_code','termination_reason','policy_receipt','receipt_ref'))
    evidence['rejections'][key]['violations']=[{**compact(v,('code','field_path','expected','check_source','message')),
       'parser':compact(v.get('protocol_error',{}),('stage','code','line','column','position','field_path','expected','actual','message')),
       'parser_excerpt_prefix':v.get('protocol_error',{}).get('raw_excerpt','')[:200]} for v in row['exact_violations']]
evidence['report_receipts']=[{'operation_id':k,**compact(v,('status','path','receipt'))} for k,v in state['task_state']['report_publications'].items()]
evidence['failure_packet_lifecycle']=state['task_state']['causal_failure_packet_lifecycle']
evidence['attempt']={'exit_status':obj('attempts/attempt_be39ca0c6335578331a1/exit_status.json'),
 'stderr':read('attempts/attempt_be39ca0c6335578331a1/stderr.log').decode(),
 'approved_selected_lines':[{'line':i,'text':line} for i,line in enumerate(read('attempts/attempt_be39ca0c6335578331a1/approved.sh').decode().splitlines(),1) if '# BIOMIND_EFFECT_HELPER' in line or 'emit_effect_evidence' in line],
 'wrapper_selected_lines':[{'line':i,'text':line} for i,line in enumerate(read('attempts/attempt_be39ca0c6335578331a1/executed.sh').decode().splitlines(),1) if i in (6,7,10)]}
identity=obj('state_contract/objects/53a0cb3458274a5b138813f8758fea8e6f5f1db08dbc4610e75ab4521b7b3673.json')['payload']
evidence['historical_identity']={**compact(identity,('capture_policy','loaded_digest_format','python','revision','dependencies','skill_versions')),
 'loaded_modules':{k:{**compact(v,('location','callable_code_digest')),'callables':{a:b for a,b in v['callables'].items() if any(s in a for s in ('materialize_effect_template','declared_effect_emission_contract','binding_input_progress_facts','get_generate_v2_prompt'))}} for k,v in identity['loaded_modules'].items() if k in ('agent_core.step_effects','agent_core.plan_bindings','prompts.manager')},
 'bio_agents_loaded_module':'NOT_CAPTURED: likely __main__ import name; source snapshot is not loaded callable proof'}
for name in ('bio_agents.py','agent_core/step_effects.py','agent_core/plan_bindings.py','prompts/manager.py','agent_core/managed_artifacts.py','agent_core/workflow/generate_handoff.py','agent_core/workflow/candidate_repair.py','agent_core/candidate_repair_policy.py','execution/script_validation.py','agent_core/controller_audit.py'):
    body=(REPO/name).read_bytes();old=identity['source_files'].get(name,{})
    evidence['current_sources'][name]={'sha256':sha(body),'bytes':len(body),'incident_capture_sha256':old.get('sha256','NOT_CAPTURED'),'matches_captured_source':sha(body)==old.get('sha256') if old else None}
OUT.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'output':str(OUT),'sha256':sha(OUT.read_bytes()),'original_scripts_executed':0,'provider_calls':0}))
