"""Current production component witness. No agent startup, API, science, or session writes.

Uses complete historical candidates and current pure production functions.
The acceptance gate is supplied synthetic execution/validation records: this is
NOT a substitute for CommitStep integration or scientific E2E evidence.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True
DENIED = REPO / '.env.api_keys'


def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).absolute()
        if path == DENIED or path.name in {'.env', '.env.api_keys'}:
            raise RuntimeError('offline witness denies all dotenv reads')
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('offline witness denies network')


sys.addaudithook(guard)
from agent_core import plan_bindings as pb
from agent_core.workspace_attestation import WorkspaceAttestationError
from agent_core.plan_tracker import _apply_upstream_step_rollback


def source_module(name, path):
    # Bypass package convenience initializers; load the exact unmodified file.
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


syntax = source_module('cross_audit_script_validation', 'execution/script_validation.py')
progress = source_module('cross_audit_candidate_repair', 'agent_core/workflow/candidate_repair.py')

samples = json.loads(Path(__file__).with_name('cross_samples.json').read_text())['samples']
historical_bad, historical_good = samples
producer = historical_good['executions'][0]['action_contract_binding_projection']
receipt = producer['binding_normalization_receipt']
raw = deepcopy(receipt['raw_candidate'])
rows = deepcopy(producer['formal_workspace_bindings'])
frozen = deepcopy(historical_good['steps'][0]['generate_binding_input_contract'])
frozen['operation'] = 'prepare_database'
scope = receipt['scope']
rule = frozen['binding_contract']['operations']['prepare_database']
results = {}


def normalize(candidate, catalog=None):
    return pb.normalize_public_action_bindings(candidate, frozen=frozen, scope=scope,
                                                workspace_bindings=rows if catalog is None else catalog)


def rejects(name, callback, expected):
    try:
        callback()
    except (pb.PlanBindingError, WorkspaceAttestationError) as error:
        assert error.code == expected, (name, error.to_dict())
        results[name] = {'expected': expected, 'actual_error': error.to_dict(), 'passed': True}
        return
    raise AssertionError(name + ' unexpectedly accepted')


normalized, normalized_receipt = normalize(raw)
assert 'binding_env' not in raw
assert normalized['binding_env'] == rule['required_binding_env']
assert normalized['produced_bindings'] == producer['produced_bindings']
results['legal_omission'] = {'passed': True, 'raw_binding_fragment': {
    k: raw[k] for k in ('operation', 'produced_bindings')},
    'derived_binding_env': normalized['binding_env']}
explicit = deepcopy(raw)
explicit['binding_env'] = deepcopy(rule['required_binding_env'])
explicit_result, _ = normalize(explicit)
assert explicit_result == normalized
results['legal_explicit'] = {'passed': True, 'binding_fragment': {
    k: explicit[k] for k in ('operation', 'produced_bindings', 'binding_env')},
    'provenance': 'isolated explicit variant of genuine historical candidate, not a claim that this raw variant was emitted historically'}
wrong = deepcopy(raw)
wrong['binding_env'] = {'GENOME_VERSION': raw['produced_bindings']['genome_version']['value']}
rejects('wrong_source_map', lambda: normalize(wrong), 'BINDING_ENV_CONTRACT_MISMATCH')
unknown = deepcopy(raw)
unknown['produced_bindings']['snpeff_db']['value']['handle'] = 'unknown-from-old-candidate'
rejects('unknown_handle', lambda: normalize(unknown), 'BINDING_HANDLE_UNKNOWN')
foreign_rows = deepcopy(rows)
foreign_rows[0]['operation_scope']['operation'] = 'different_operation'
rejects('cross_operation_handle', lambda: normalize(raw, foreign_rows), 'BINDING_HANDLE_UNKNOWN')
fresh, fresh_receipt = normalize(raw)
assert fresh == normalized and 'unknown-from-old-candidate' not in json.dumps(fresh_receipt)
results['corrected_candidate_no_old_handle'] = {'passed': True, 'scope': 'pure normalizer; integration cache not simulated'}

producer_action = {**normalized, 'formal_workspace_bindings': rows}
state = {**historical_good['plan_identity'], 'current_step_id': 'prepare_database', 'task_state': {}}
produced_resolution = pb.resolve_action_binding_environment(state, producer_action, binding_rule=rule)
assert produced_resolution == producer['generate_binding_resolution']
consumer_rule = historical_good['steps'][1]['generate_binding_input_contract']['binding_contract']['operations']['build_database']
consumer = {'operation': 'build_database', 'consumed_bindings': {
    name: {'ref': 'plan_binding:' + name} for name in consumer_rule['consumes_bindings']},
    'binding_env': deepcopy(consumer_rule['required_binding_env']), 'formal_workspace_bindings': rows}
rejects('unconfirmed_unavailable', lambda: pb.resolve_action_binding_environment(state, consumer), 'PLAN_BINDING_UNAVAILABLE')
exec_record = {'returncode': 0, 'step_id': 'prepare_database', 'action_id': 'isolated-action', 'attempt_id': 'isolated-attempt'}
assert not pb.accept_action_binding_candidates(state, producer_action, {**exec_record, 'returncode': 1}, {'passed': True})
assert not pb.accept_action_binding_candidates(state, producer_action, exec_record, {'passed': False})
assert not state['task_state']
accepted = pb.accept_action_binding_candidates(state, producer_action, exec_record, {'passed': True})
assert len(accepted) == 2
resolved = pb.resolve_action_binding_environment(state, consumer, binding_rule=consumer_rule)
assert resolved['environment'] == produced_resolution['environment']
results['acceptance_and_consumption'] = {'passed': True, 'accepted': [b.to_dict() for b in accepted],
    'consumer': consumer, 'resolved': resolved, 'limitation': 'synthetic gate inputs; true historical CommitStep separately verified'}
stale_consumer = deepcopy(consumer)
stale_consumer['formal_workspace_bindings'][0]['binding_revision'] += 1
rejects('stale_resource_revision', lambda: pb.resolve_action_binding_environment(state, stale_consumer), 'ARTIFACT_BACKED_BINDING_REF_STALE')
revised = deepcopy(state)
revised['plan_revision'] += 1
rejects('stale_plan_revision', lambda: pb.resolve_action_binding_environment(revised, consumer), 'PLAN_BINDING_UNAVAILABLE')
corrupted = deepcopy(state)
corrupted['task_state']['plan_binding_history'][0]['value'] = 'changed-after-commit'
rejects('hash_tamper', lambda: pb.accepted_plan_binding(corrupted, 'genome_version'), 'PLAN_BINDING_VALUE_HASH_MISMATCH')
changed = deepcopy(producer_action)
changed['produced_bindings']['genome_version']['value'] = 'different'
rejects('accepted_value_immutable', lambda: pb.accept_action_binding_candidates(state, changed, exec_record, {'passed': True}), 'PLAN_BINDING_IMMUTABLE')
invalidated = deepcopy(state)
assert pb.invalidate_producer_bindings(invalidated, producer_step_id='prepare_database', reason='isolated invalidation') == 2
rejects('invalidated_producer_unavailable', lambda: pb.resolve_action_binding_environment(invalidated, consumer), 'PLAN_BINDING_UNAVAILABLE')
literal_action = {'produced_bindings': {'opaque': {'type': 'string', 'value': '/tmp/looks-like-a-file/$UNEXPANDED'}},
                  'binding_env': {'OPAQUE': {'source': 'produced_binding', 'name': 'opaque'}}}
assert pb.resolve_action_binding_environment({}, literal_action)['environment']['OPAQUE'] == '/tmp/looks-like-a-file/$UNEXPANDED'
assert pb.binding_value_violation('$GENOME_VERSION', 'concrete_path_segment') == 'BINDING_CONCRETE_VALUE_REQUIRED'
assert pb.binding_value_violation('a/b', 'concrete_path_segment') == 'BINDING_PATH_SEGMENT_INVALID'
assert pb.resolve_action_binding_environment({}, {'operation': 'inspect'}) == {'environment': {}, 'records': []}
results['literal_and_observation'] = {'passed': True, 'literal_preserved': literal_action['produced_bindings']['opaque']['value'],
    'concrete_role_source': 'explicit rule; no path/string guessing', 'observation_requires_no_binding': True}

# A deliberately failing product invariant: rollback invalidates descendant
# steps/artifacts but currently only the target step's accepted bindings.
rollback_state = {'plan_id': 'isolated-rollback', 'plan_version': 'v1', 'plan_revision': 1,
    'steps': [{'step_id': 'a', 'status': 'passed', 'depends_on': []},
              {'step_id': 'b', 'status': 'passed', 'depends_on': ['a']},
              {'step_id': 'c', 'status': 'failed', 'depends_on': ['b']}], 'task_state': {}}
for step in ('a', 'b'):
    pb.accept_action_binding_candidates(rollback_state, {'produced_bindings': {step: {'type': 'string', 'value': step + '-old'}}},
        {'returncode': 0, 'step_id': step, 'action_id': step, 'attempt_id': step}, {'passed': True})
rollback_result = _apply_upstream_step_rollback(rollback_state, {'failed_step_id': 'c', 'rollback_to_step_id': 'a'})
assert rollback_state['steps'][1]['status'] == 'invalidated'
assert pb.accepted_plan_binding(rollback_state, 'b').value == 'b-old'
results['rollback_descendant_binding_defect'] = {'passed': True, 'meaning': 'DEFECT REPRODUCED; not a fix pass',
    'rollback_result': rollback_result, 'state_after': rollback_state,
    'stale_descendant_still_readable': pb.accepted_plan_binding(rollback_state, 'b').to_dict()}
rejects('rollback_descendant_new_value_blocked', lambda: pb.accept_action_binding_candidates(rollback_state,
    {'produced_bindings': {'b': {'type': 'string', 'value': 'b-new'}}},
    {'returncode': 0, 'step_id': 'b', 'action_id': 'b-new', 'attempt_id': 'b-new'}, {'passed': True}), 'PLAN_BINDING_IMMUTABLE')

# The actual historic A and B bodies are only compiled, never executed.
a, b = [c['full_public_candidate'] for c in historical_bad['candidates']]
check_a, check_b = syntax.check_executable_action(a), syntax.check_executable_action(b)
assert check_a['check_status'] == check_b['check_status'] == 'PASS'
assert check_a['body_sha256'] == check_b['body_sha256']
kwargs = {'representation_targets': ['json_object'], 'violations': [{'field_path': 'binding_env', 'code': 'INVALID'}],
          'contract_ref': {'operation': 'prepare_database'}}
first = progress.candidate_progress_assessment(candidate=a, binding_facts=[], prior_rows=[], check_records=[check_a], **kwargs)
second = progress.candidate_progress_assessment(candidate=b, binding_facts=['operation'],
    prior_rows=[{'repair_ordinal': 1, 'progress_assessment': first}], check_records=[check_b], **kwargs)
assert second['progress_proven'] and not second['repeat_proven']
results['historical_argv_only_regression_current'] = {'passed': True, 'a_syntax': check_a, 'b_syntax': check_b,
    'progress': second, 'decision': 'not invoked; graph/policy route is outside this component witness', 'execution_started': False}

paths = ['agent_core/plan_bindings.py', 'agent_core/plan_tracker.py', 'agent_core/workspace_attestation.py', 'agent_core/step_effects.py',
         'execution/script_validation.py', 'agent_core/workflow/candidate_repair.py', 'agent_core/recovery.py']
result = {'scope': __doc__, 'interpreter': sys.executable, 'python_version': sys.version,
          'config_or_dotenv_loaded': any(k == 'config' or k.startswith('config.') for k in sys.modules),
          'source_hashes': {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in paths}, 'results': results}
assert not result['config_or_dotenv_loaded']
output = Path(__file__).with_name('cross_witness_results.json')
output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'output': str(output), 'cases': len(results), 'all_passed': all(r['passed'] for r in results.values()),
                  'dotenv_loaded': result['config_or_dotenv_loaded']}))
