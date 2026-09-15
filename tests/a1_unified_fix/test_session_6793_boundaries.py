"""Affected direct boundaries; graph evidence lives in test_session_6793_chain."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from agent_core.step_effects import materialize_effect_action, EffectHelperError

MARKER = '# BIOMIND_EFFECT_HELPER'
HELPER = 'def emit_effect_evidence(value):\n    return value\n'
TEMPLATE = {'available': True, 'schema_version': 1, 'python_template': HELPER,
            'template_sha256': hashlib.sha256(HELPER.encode()).hexdigest()}


@pytest.mark.parametrize('case', ['undeclared', 'python', 'heredoc', 'argv_only', 'conflict',
    'language_conflict', 'zero', 'two', 'unavailable', 'outside'])
def test_carrier_measured_diagnostics(case):
    body = MARKER + '\nprint(emit_effect_evidence(False))\n'
    action = {'code': body, 'language': 'python'}
    template = deepcopy(TEMPLATE)
    if case == 'undeclared': action.pop('language')
    if case == 'zero': action['code'] = 'print("No named helper required")\n'
    if case in {'heredoc', 'two', 'outside'}:
        action = {'language': 'bash', 'code': "python3 - <<'PY'\n" + body + 'PY\n'}
        if case == 'two': action['code'] = MARKER + '\n' + action['code']
        if case == 'outside': action['code'] = MARKER + '\nprintf test\n'
    if case == 'argv_only': action = {'argv': ['python3', '-I', '-c', body]}
    if case == 'conflict': action['argv'] = ['python3', '-c', body + '# different\n']
    if case == 'language_conflict': action['argv'] = ['bash', '-c', body]
    if case == 'unavailable': template['available'] = False
    if case in {'python', 'heredoc', 'argv_only', 'zero'}:
        result = materialize_effect_action(action, template)
        if case != 'zero':
            assert HELPER in str(result) or HELPER.replace('\n', '\\n') in str(result)
        return
    with pytest.raises(EffectHelperError) as caught:
        materialize_effect_action(action, template)
    diagnostic = caught.value.to_dict()
    actual = diagnostic['actual']
    assert actual['template_available'] == (case != 'unavailable')
    assert actual['marker_count'] == (2 if case == 'two' else 1)
    assert diagnostic['message'] and diagnostic['expected']
    if case == 'undeclared':
        assert diagnostic['code'] == 'EFFECT_HELPER_CARRIER_UNDECLARED'
        assert actual['declared_language'] == 'UNKNOWN' and actual['fallback_language'] == 'bash'
    if case == 'two':
        assert actual['marker_positions'] == [
            {'source': 'code', 'line': 1, 'scope': 'outside_supported_python_scope'},
            {'source': 'code', 'line': 3, 'scope': 'python_comment'}]


def state():
    return {'task_id': 'new-task', 'plan_id': 'new-plan', 'plan_version': 'v3', 'plan_revision': 3,
        'current_step_id': 'build', 'task_state': {'status': 'running'},
        'steps': [{'step_id': 'prepare', 'status': 'passed'},
                  {'step_id': 'build', 'status': 'verification_pending', 'verification_attempt_id': 'causal-attempt'},
                  {'step_id': 'annotation', 'status': 'pending'}]}


def test_exact_step_ownership_and_stale_or_duplicate_registration():
    from agent_core.plan_tracker import record_preaction_contract_failure
    from agent_core.workflow.candidate_repair import candidate_repair_scope
    original = state()
    scope = candidate_repair_scope(original, task_id='new-task', step_id='build')
    kwargs = {'error_fingerprint': 'error', 'candidate_fingerprint': 'candidate',
              'termination_reason': 'repair_no_progress', 'step_id': 'build', 'expected_scope': scope}
    changed = deepcopy(original)
    record_preaction_contract_failure(changed, {'code': 'FORMAT_INVALID'}, **kwargs)
    assert [row['status'] for row in changed['steps']] == ['passed', 'failed', 'pending']
    assert changed['steps'][1]['verification_attempt_id'] == 'causal-attempt'
    once = deepcopy(changed)
    record_preaction_contract_failure(changed, {'code': 'FORMAT_INVALID'}, **kwargs)
    assert changed == once
    for change in [{'step_id': 'absent'}, {'step_id': 'annotation'},
                   {'expected_scope': {**scope, 'plan_revision': 2}},
                   {'expected_scope': {**scope, 'task_id': 'foreign'}}]:
        bad = deepcopy(original)
        with pytest.raises(ValueError, match='PREACTION_FAILURE_SCOPE_STALE'):
            record_preaction_contract_failure(bad, {'code': 'FORMAT_INVALID'}, **{**kwargs, **change})
        assert bad == original


@pytest.mark.parametrize('jar', [True, False])
@pytest.mark.parametrize('operation', ['build', 'ann'])
def test_selected_renderer_uses_explicit_subcommand_constructor(jar, operation):
    path = Path(__file__).resolve().parents[2] / 'skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py'
    spec = importlib.util.spec_from_file_location('synthetic_test_renderer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = SimpleNamespace(snpeff_jar='new synthetic.jar' if jar else None,
        snpeff_command='snpEff', java_options='-Xmx1g', config='new config',
        use_project_config=False, data_dir='new data')
    options = ['-gtf22', '-v', 'synthetic_genome'] if operation == 'build' else ['-v', 'synthetic_genome', 'new.vcf']
    calls = []
    original = module.snpeff_argv
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append((args, kwargs, result))
        return result
    module.snpeff_argv = observed
    argv = shlex.split(module.snpeff_cmd(args, [operation, *options]))
    assert len(calls) == 1 and calls[0][1]['operation'] == operation
    offset = argv.index('new synthetic.jar') + 1 if jar else 1
    assert argv[offset] == operation
    assert argv[offset + 1:offset + 3] == ['-c', 'new config']
    assert argv[-len(options):] == options
    probe = shlex.split(module.snpeff_cmd(args, ['-version']))
    assert probe[-1] == '-version' and 'ann' not in probe


@pytest.mark.parametrize('fault', ['none', 'changed', 'symlink', 'ancestor_symlink', 'escape', 'foreign_capability', 'unreliable'])
def test_failure_log_snapshot_has_no_arbitrary_path_authority(tmp_path, fault):
    from agent_core.workspace_attestation import capture_mutation_failure_logs, capture_bounded_workspace_snapshot
    root = tmp_path / 'approved-new-workspace'
    folder = root / 'data' / 'synthetic'
    folder.mkdir(parents=True)
    log = folder / 'build.log'
    log.write_text("SYNTHETIC ONLY\nCommand: 'ann'\nUnknown option '-gtf22'\n")
    snapshot = capture_bounded_workspace_snapshot(str(root))
    intent = {'operation': 'build_database', 'mutation': 'build_database', 'status': 'failed',
              'formal_concrete_path': str(root), 'after_evidence': snapshot,
              'resolved_postconditions': [{'kind': 'file_nonempty', 'relative_path': 'data/synthetic/snpEffectPredictor.bin'}]}
    capability = 'genomics.snpeff_annotation.build_database'
    if fault == 'changed': log.write_text('changed after attestation')
    if fault == 'symlink':
        other = tmp_path / 'new-unapproved-log'; other.write_text('must not be read')
        log.unlink(); log.symlink_to(other)
    if fault == 'ancestor_symlink':
        other = tmp_path / 'new-detached-folder'; folder.rename(other); folder.symlink_to(other, target_is_directory=True)
    if fault == 'escape': intent['resolved_postconditions'][0]['relative_path'] = 'data/../../snpEffectPredictor.bin'
    if fault == 'foreign_capability': capability = 'core.controlled_execution'
    if fault == 'unreliable': snapshot['reliable'] = False
    rows = capture_mutation_failure_logs({'intents': [intent]}, capability_id=capability)
    if fault in {'escape', 'foreign_capability', 'unreliable'}:
        assert rows == []
    elif fault == 'none':
        assert rows[0]['status'] == 'captured'
        assert "Unknown option '-gtf22'" in ''.join(rows[0]['text_chunks'])
    else:
        assert rows[0]['status'] == 'unavailable' and 'text_chunks' not in rows[0]
        assert rows[0]['unavailable_reason'] == ('FAILURE_LOG_SNAPSHOT_CHANGED' if fault == 'changed' else 'FAILURE_LOG_SYMLINK_DENIED')
