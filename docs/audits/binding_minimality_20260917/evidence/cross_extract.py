"""Read-only extraction of historical binding evidence; no science or API calls."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[4]
RUNS = REPO.parent / 'benchmarks/biomind_agent_benchmark/runs/task_outputs'
OUT = Path(__file__).with_name('cross_samples.json')
sources = {}


def read(path):
    data = path.read_bytes()
    sources[str(path)] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    return json.loads(data)


def project(mapping, keys):
    return {key: mapping[key] for key in keys if key in mapping}


def request_fragment(path):
    value = read(path)
    messages = value['messages']
    result = {'source': str(path), 'message_content_chars': sum(len(m['content']) for m in messages),
              'message_content_utf8_bytes': sum(len(m['content'].encode()) for m in messages),
              'messages': []}
    for index, message in enumerate(messages):
        content = message['content']
        row = {'index': index, 'role': message.get('role'), 'chars': len(content), 'utf8_bytes': len(content.encode())}
        if index == 0:
            row['full_content'] = content
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, dict) and ('feedback' in decoded or 'derived_field_policy' in decoded):
            row['full_json_content'] = decoded
        for tag, body in re.findall(r'<([a-z_]+)(?: [^>]*)?>\s*(.*?)\s*</\1>', content, re.S):
            try:
                decoded = json.loads(body)
            except (ValueError, TypeError):
                continue
            if tag == 'current_focus':
                runtime = decoded.get('runtime', {})
                row['current_focus_binding_projection'] = {
                    'current_step': decoded.get('current_step'),
                    'runtime': project(runtime, ['current_action_contract', 'current_action_capability',
                        'current_action_workspace_bindings', 'binding_input_requirements', 'accepted_plan_bindings',
                        'generate_output_contract'])}
            if tag == 'current_generate_output_protocol':
                row['full_output_protocol'] = decoded
        result['messages'].append(row)
    return result


def sample(session, prefixes):
    root = RUNS / session
    state = read(root / 'plan_state.json')
    result = {'session': session, 'historical_source_version': 'Not conclusively identified by trace; not asserted equal to current HEAD.',
        'plan_identity': project(state, ['plan_id', 'plan_version', 'plan_revision', 'schema_version']),
        'bindings': state['task_state'].get('plan_binding_history', []),
        'accepted_index': state['task_state'].get('accepted_plan_bindings', {}),
        'steps': [project(s, ['step_id', 'status', 'operation', 'step_effect', 'resource_uses', 'generate_binding_input_contract'])
                  for s in state['steps'] if s['step_id'] in ('prepare_database', 'build_database', 'annotate_variants')],
        'commits': [c['outcome'] for c in state['task_state'].get('typed_step_commit_receipts', {}).values()],
        'requests': [], 'candidates': []}
    for prefix in prefixes:
        directory = next((root / 'context_trace').glob(prefix + '*'))
        result['requests'].append(request_fragment(directory / 'request.json'))
        response = read(directory / 'response.json')
        result['candidates'].append({'source': str(directory / 'response.json'),
                                     'full_public_candidate': json.loads(response['content'])})
    path = root / 'executions/execution_records.jsonl'
    text = path.read_bytes()
    sources[str(path)] = {'sha256': hashlib.sha256(text).hexdigest(), 'bytes': len(text)}
    result['execution_count'] = len(text.splitlines())
    result['executions'] = []
    for raw in text.splitlines():
        execution = json.loads(raw)
        if execution.get('step_id') not in ('prepare_database', 'build_database', 'annotate_variants'):
            continue
        row = project(execution, ['execution_id', 'action_id', 'attempt_id', 'step_id', 'exit_code',
            'execution_status', 'process_status', 'started_at', 'finished_at', 'approved_script',
            'executed_script', 'effect_evidence', 'validated_artifacts', 'actual_environment'])
        row['action_contract_binding_projection'] = project(execution.get('action_contract', {}), [
            'operation', 'produced_bindings', 'consumed_bindings', 'binding_env', 'env',
            'formal_workspace_bindings', 'generate_binding_resolution', 'generate_binding_resolution_digest',
            'binding_contract_rule', 'binding_normalization_receipt'])
        row['stdout_binding_lines'] = [line for line in execution.get('raw_stdout', '').splitlines()
            if any(term in line for term in ('genome_version=', 'PREDICTOR_EXISTS=', 'BIOMIND_EFFECT_EVIDENCE_JSON='))]
        for kind in ('approved_script', 'executed_script'):
            file = Path(execution[kind]['path'])
            data = file.read_bytes()
            sources[str(file)] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
            row[kind + '_hash_verified'] = sources[str(file)]['sha256'] == execution[kind]['sha256']
        result['executions'].append(row)
    return result


samples = [sample('session_1789464978', ['generate-0013-', 'generate-0014-']),
           sample('session_1789563666', ['generate-0013-', 'generate-0016-'])]
recovery_root = RUNS / 'session_1789537621'
recovery_state = read(recovery_root / 'plan_state.json')
recovery_path = recovery_root / 'executions/execution_records.jsonl'
recovery_data = recovery_path.read_bytes()
sources[str(recovery_path)] = {'sha256': hashlib.sha256(recovery_data).hexdigest(), 'bytes': len(recovery_data)}
recovery_slice = {'session': recovery_root.name, 'bindings': recovery_state['task_state']['plan_binding_history'], 'build_attempts': []}
for raw in recovery_data.splitlines():
    execution = json.loads(raw)
    if execution.get('step_id') != 'build_database':
        continue
    recovery_slice['build_attempts'].append({**project(execution, ['attempt_id', 'action_id', 'step_id', 'exit_code', 'execution_status']),
        'action_binding_fields': project(execution.get('action_contract', {}), ['consumed_bindings', 'binding_env',
            'generate_binding_resolution', 'recovery_execution_envelope']),
        'full_approved_command': execution['code'],
        'stdout_observation': [line for line in execution.get('raw_stdout', '').splitlines() if 'BIOMIND_EFFECT_EVIDENCE_JSON=' in line]})
OUT.write_text(json.dumps({'scope': 'Historical read-only evidence; request fragments are explicitly projections; public candidates are complete.',
                          'samples': samples, 'recovery_slice': recovery_slice, 'sources': sources}, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'output': str(OUT), 'bytes': OUT.stat().st_size, 'sources': len(sources),
                  'samples': [{'session': s['session'], 'bindings': len(s['bindings']), 'executions': s['execution_count']} for s in samples]}))

def canonical_metrics(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'chars': len(payload), 'utf8_bytes': len(payload.encode())}


drafts = []
good = samples[1]
for index, request in enumerate(good['requests']):
    focus = next(m['current_focus_binding_projection'] for m in request['messages'] if 'current_focus_binding_projection' in m)
    runtime = focus['runtime']
    before_input = project(runtime, ['current_action_workspace_bindings', 'binding_input_requirements', 'accepted_plan_bindings'])
    rows = runtime['current_action_workspace_bindings']
    requirements = runtime['binding_input_requirements']
    after_input = {'operation': requirements['operation'],
        'resources': [project(row, ['plan_handle', 'resource_kind', 'formal_concrete_path', 'evidence_contract']) | {'status': 'current approved resource'} for row in rows],
        'binding_choices': {name: project(spec, ['type', 'value_policy', 'allowed_handles']) for name, spec in requirements.get('produced_bindings', {}).items()},
        'accepted_values': [project(row, ['name', 'value', 'value_type', 'producer_step']) for row in runtime.get('accepted_plan_bindings', [])],
        'process_variables': {key: value['name'] for key, value in requirements['binding_env']['declared_mapping'].items()},
        'rule': 'Supply only semantic values or chosen handles. Types, consumed refs and source mapping come from this approved operation. Values become downstream facts only after execution, validation and CommitStep.'}
    before_output = deepcopy(good['candidates'][index]['full_public_candidate'])
    after_output = deepcopy(before_output)
    for binding in after_output.get('produced_bindings', {}).values():
        binding.pop('type', None)
    after_output.pop('consumed_bindings', None)
    # Protocol marker and empty produced_bindings are left unchanged so their
    # already-optional omission is not counted as a new code improvement.
    before_binding = project(before_output, ['produced_bindings', 'consumed_bindings', 'binding_env'])
    after_binding = project(after_output, ['produced_bindings', 'consumed_bindings', 'binding_env'])
    drafts.append({'source_candidate': good['candidates'][index]['source'], 'operation': requirements['operation'],
        'before_input_binding_projection': before_input, 'after_input_binding_projection_DRAFT': after_input,
        'before_full_public_candidate': before_output, 'after_full_public_candidate_DRAFT': after_output,
        'input_projection_metrics': {'before': canonical_metrics(before_input), 'draft': canonical_metrics(after_input)},
        'output_binding_fragment_metrics': {'before': canonical_metrics(before_binding), 'draft': canonical_metrics(after_binding)},
        'mechanical_leaf_values_to_fill': {'before': 2, 'draft': 0, 'definition': 'producer: two declared type strings; consumer: two fixed plan_binding refs; operation and substantive script excluded'},
        'binding_env_new_savings': 0,
        'compatibility': 'DRAFT requires versioned public requirements/schema plus normalizer deriving missing known types and exact consumed refs before full admission; existing explicit fields remain validated, legacy_full_v1 remains strict. Current validators reject this reduced producer shape; no production change made.'})
Path(__file__).with_name('cross_draft.json').write_text(json.dumps({'status': 'proposal only',
    'measurement': 'Canonical compact JSON; binding projection only, not full request or tokens. Actual historical binding_env is already omitted and gets zero new savings.',
    'drafts': drafts}, ensure_ascii=False, indent=2) + '\n')
