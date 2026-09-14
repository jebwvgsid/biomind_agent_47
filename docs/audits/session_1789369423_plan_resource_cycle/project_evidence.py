"""Static allowlisted JSON projection. Never imports BioMind or resumes a session."""
import hashlib
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SESSION = REPO.parent / 'benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789369423'

def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()

def read(path):
    return json.loads(path.read_text())

def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def main():
    state = read(SESSION / 'plan_state.json')
    traces = sorted((SESSION / 'context_trace').glob('planner*/manifest.json'))
    objects = {}
    for p in (SESSION / 'state_contract/objects').glob('*.json'):
        value = read(p)
        if value.get('object_type') == 'planner_correction_context':
            objects[value['payload']['occurrence_receipt']['call_id']] = (p.name, value['payload'])
    rows = []
    for i, p in enumerate(traces):
        manifest = read(p); response = read(p.parent / 'response.json')
        row = {k: manifest.get(k) for k in ['call_id', 'attempt_id', 'semantic_invocation_id', 'status', 'timestamp', 'completed_at', 'planner_observation', 'planner_budget_decision', 'planner_correction_escalation', 'retry_budget_before', 'retry_budget_after', 'sdk_max_retries', 'runtime_identity']}
        row['request_sha256'] = digest((p.parent / 'request.json').read_bytes())
        row['actual_controller_binding'] = manifest.get('controller_binding')
        raw = response.get('content', '')
        if not raw:
            row['cancellation'] = response; rows.append(row); continue
        candidate = json.loads(raw)
        row['raw_candidate_sha256'] = digest(raw.encode())
        row['public_message_sha256'] = digest(candidate.pop('public_message', '').encode())
        row['formal_candidate_sha256'] = digest(candidate)
        name = f'candidate_{i+1:02d}.json'; write(name, candidate); row['candidate_file'] = name
        projection = {k: candidate[k] for k in ['proposed_artifacts', 'steps']}
        for step in projection['steps']:
            for key in ['title', 'description', 'success_criteria']:
                step.pop(key, None)
        row['resource_structure_sha256'] = digest(projection)
        receipt_name, context = objects[row['call_id']]
        assert digest(context['raw_candidate'].encode()) == row['raw_candidate_sha256']
        feedback = context['feedback']
        row['receipt_object'] = receipt_name
        row['occurrence_receipt'] = context.get('occurrence_receipt')
        row['candidate_scope_id'] = context['occurrence_receipt']['scope']['candidate_scope_id']
        row['planning_epoch_id'] = context.get('planning_epoch_id')
        row['validation_issues'] = feedback.get('validation_issues')
        row['correction_budget'] = (context.get('provider_feedback') or feedback).get('correction_budget')
        row['capability_digest'] = context.get('capability_digest')
        row['validator_candidate'] = {k:v for k,v in context.get('candidate', {}).items() if k in ['steps','proposed_artifacts','metadata','plan_document','task_output_materializations','plan_resource_bindings','artifact_usage','allocation_intent','compiled_dataflow','plan_compilation_receipt','plan_dataflow_receipt','validation_issues']}
        row['feedback_sha256'] = digest(feedback)
        row['saved_provider_feedback_sha256'] = digest(context.get('provider_feedback'))
        # Match the actual runtime observation, not an inferred reconstructed request.
        next_request = read(traces[i+1].parent / 'request.json')['messages']
        matches = []
        for index, message in enumerate(next_request):
            if message.get('name') != 'runtime_observation': continue
            try: observed = json.loads(message['content'])
            except (ValueError, TypeError): continue
            if observed.get('code') == 'PLANNER_CANDIDATE_REJECTED':
                matches.append({'message_index': index, 'message_id': message.get('id'), 'content_sha256': digest(message['content'].encode()), 'feedback': observed})
        row['next_request_latest_feedback'] = matches[-1] if matches else None
        row['saved_feedback_equals_next_request'] = bool(matches and digest(matches[-1]['feedback']) == row['saved_provider_feedback_sha256'])
        rows.append(row)
    runtime = read(SESSION / 'state_contract/objects/e1b1e0f173cdf0cf92ae3e7f0fad3fea1014156529bab629b40f3132bf5142f8.json')['payload']
    safe_files = {}
    for name, captured in runtime['source_files'].items():
        # Explicit source receipt only. Never inspect configuration/startup or external locations.
        if name.startswith(('agent_core/', 'prompts/')) or name in ['bio_agents.py', 'skill_manager.py']:
            path = REPO / name
            safe_files[name] = {'captured_sha256': captured['sha256'], 'current_sha256': digest(path.read_bytes()) if path.is_file() else None}
    snap = next(iter(state['skill_access_state']['snapshots'].values()))
    skill = REPO / 'skills/genomics/snpeff-annotation/SKILL.md'
    skill_identity = {k: v for k, v in snap.items() if k not in ['exact_text', 'heading_index']}
    skill_identity.update(current_sha256=digest(skill.read_bytes()), captured_text_sha256=digest(snap['exact_text'].encode()))
    metrics = read(SESSION / 'run_metrics.json')
    # Event names/IDs and bounded machine cancellation receipts only; no tool stdout.
    events = [json.loads(line) for line in (SESSION / 'context_trace/events.jsonl').read_text().splitlines()]
    consultant=[]
    for p in sorted((SESSION/'context_trace').glob('consultant*/response.json')):
        value=read(p); body=json.loads(value.get('content') or '{}')
        consultant.append({'call_id':p.parent.name,'message_id':value.get('id'),'response_sha256':digest(value.get('content','').encode()),'action_type':body.get('action_type'),'public_message_sha256':digest(body.get('public_message','').encode()),'selected_skills':body.get('selected_skills')})
    write('evidence.json', {'session_id': state['task_id'], 'original_session_read_mode': 'static JSON only',
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'candidates': rows, 'skill_identity': skill_identity, 'source_identity': safe_files,
        'loaded_modules': runtime['loaded_modules'], 'historical_revision': runtime['revision'],
        'budget': state['task_state']['budgets'], 'planning_epoch': state['task_state']['planning_epoch'],
        'planner_loop': state['task_state']['planner_loop'], 'summary': metrics['summary'],
        'final_state': {k: state.get(k) for k in ['plan_status', 'approval_status', 'execution_status', 'can_request_approval', 'can_generate', 'steps', 'plan_artifacts', 'current_approval']},
        'runtime_status': state['runtime_state']['status'],
        'consultant_calls':consultant,
        'cancellation_receipt':state['task_state'].get('cancellation_receipt'),
        'trace_event_index': [{'event_id':e['event_id'],'event_kind':e['event_kind'],'timestamp':e['timestamp'],'payload':e['payload']} for e in events]})
    fields = ['ordinal','call_id','attempt_id','candidate_scope_id','raw_candidate_sha256','resource_structure_sha256','codes','next_message_index']
    lines = ['\t'.join(fields)]
    for i, row in enumerate(rows):
        lines.append('\t'.join(str(v or '') for v in [i+1,row['call_id'],row['attempt_id'],row.get('candidate_scope_id'),row.get('raw_candidate_sha256'),row.get('resource_structure_sha256'),';'.join(x['code'] for x in row.get('validation_issues') or []),(row.get('next_request_latest_feedback') or {}).get('message_index')]))
    (OUT / 'candidate_matrix.tsv').write_text('\n'.join(lines)+'\n')

if __name__ == '__main__':
    main()
