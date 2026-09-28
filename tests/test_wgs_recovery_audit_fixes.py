"""Offline regressions for the recorded WGS Generate/Recovery failure chain."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from agent_core.controller_audit import ScriptBlockError, parse_executable_script_block
from agent_core.generate_protocol import SCRIPT_OUTPUT_PROTOCOL
from agent_core.skill_resource_binding import parse_generate_script_candidate, bind_action_skill_resources
from agent_core.controller_audit import adapt_script_block_to_action
from agent_core.skill_script_invocation import build_skill_script_shell
from agent_core.step_effects import materialize_effect_template
from execution.resource_evidence import ProcessResourceEvidence
from execution.profiling import RunProfiler
from test_skill_script_invocation import _fixture, _receipt, HANDLE, STEP_ID


def test_recovery_rebuild_keeps_complete_shared_output_contract():
    from tests.test_generate_context_episode import _basis, _episode, _verified_generate_seed
    from agent_core.workflow.generate_handoff import start_generate_after_recovery, freeze_generate_provider_request
    seed = _verified_generate_seed()
    result = start_generate_after_recovery(_episode(),
        basis=_basis(execution_identity="repaired", recovery_seed_id=seed.seed_id),
        verified_generate_seed=seed, node_context={}, inherited_materials=[])
    wire = json.dumps(freeze_generate_provider_request(result).provider_payloads())
    for text in ('skill_script', 'CDATA', 'public_message', 'blocked', 'schema'):
        assert text in wire
    from agent_core.step_effects import generate_output_examples
    assert generate_output_examples({}, script_protocol=True)['wire_contract']['instructions'] == SCRIPT_OUTPUT_PROTOCOL


def test_format_feedback_reports_all_outer_issues_together():
    with pytest.raises(ScriptBlockError) as caught:
        parse_executable_script_block('Explanation\n<shell language="bash"><![CDATA[echo ok]]></shell>')
    fields = {row['field_path'] for row in caught.value.to_dict()['format_issues']}
    assert fields == {'$.prefix', '$.opening_tag.language', '$.body.CDATA'}
    assert 'Remove' in str(caught.value)


def test_cdata_policy_is_explicit_in_both_validation_modes():
    raw = '<shell><![CDATA[printf ok\n]]></shell>'
    with pytest.raises(ScriptBlockError) as caught:
        parse_executable_script_block(raw, strict=True)
    assert caught.value.to_dict()['format_issues'][0]['field_path'] == '$.body.CDATA'
    assert parse_executable_script_block(raw, strict=False).body == 'printf ok\n'




def test_registered_helper_request_builds_bound_interpreter_before_approval(tmp_path):
    service, access, _, _ = _fixture(tmp_path)
    value = 'input with spaces; $(not executed).fq'
    raw = json.dumps({'type': 'skill_script', 'handle': HANDLE,
        'arguments': ['--path', value, '--observation-id', 'observation']})
    block = parse_generate_script_candidate(raw, service, access, step_id=STEP_ID, cwd=tmp_path, strict=True)
    resource = _receipt(service, access, tmp_path)
    assert block.body.startswith(resource['invocation']['shell_prefix'])
    action = bind_action_skill_resources(adapt_script_block_to_action(block), service, access,
        step_id=STEP_ID, cwd=tmp_path, enforce_invocation=True)
    assert action['code'] == block.body
    assert action['formal_skill_resources'][0]['handle'] == HANDLE


def test_structured_helper_late_binding_cannot_be_forged():
    resource = {'invocation': {'argv_prefix': ['/python', '/helper.py'], 'arguments': {
        '--evidence': {'nargs': 1, 'required': True, 'runtime_environment_variable': 'APPROVED_EVIDENCE'}}}}
    body = build_skill_script_shell(resource, ['--evidence', {'runtime_binding': 'APPROVED_EVIDENCE'}])
    assert body == '/python /helper.py --evidence "$APPROVED_EVIDENCE"'
    for value in ('/invented/path', {'runtime_binding': 'HOME'}):
        with pytest.raises(ValueError):
            build_skill_script_shell(resource, ['--evidence', value])




def test_unified_reader_uses_registered_reader():
    from tests.test_recovery_tools import RecoveryToolsTests
    case = RecoveryToolsTests()
    case.setUp()
    try:
        read = case.dispatcher.dispatch('inspect_ref', {'ref_id': 'validation-1', 'start_line': 1, 'end_line': 2})
        assert read['resolved_reader'] == 'evidence.read'
        assert 'line one' in json.dumps(read)
    finally:
        case.doCleanups()


def test_modified_copied_helper_cannot_duplicate_frozen_serializer():
    template_code = 'def emit_effect_evidence(facts):\n    return facts\n'
    template = {'available': True, 'python_template': template_code,
                'template_sha256': hashlib.sha256(template_code.encode()).hexdigest(), 'declarations': {}}
    body = '# BIOMIND_EFFECT_HELPER\nimport sys\ndef emit_effect_evidence(facts):\n    "copied and edited"\n    return facts\n'
    assert materialize_effect_template(body, template, language='python') == body
    with pytest.raises(ValueError, match='EFFECT_HELPER_ALREADY_EXPANDED_WITH_MARKER'):
        materialize_effect_template(body, template, language='python', protocol_version=1)


def test_resource_evidence_tracks_actual_membership_and_counters(tmp_path):
    proc = tmp_path / 'proc'
    group = tmp_path / 'cgroup'
    job = group / 'job'
    job.mkdir(parents=True)
    for pid, rss, child in ((123, 10, '124'), (124, 20, '')):
        base = proc / str(pid)
        (base / 'task' / str(pid)).mkdir(parents=True)
        (base / 'status').write_text(f'Name:\tfixture\nVmRSS:\t{rss} kB\nVmHWM:\t{rss+1} kB\n')
        (base / 'task' / str(pid) / 'children').write_text(child)
    (proc / '123' / 'limits').write_text('Max address space unlimited unlimited bytes\n')
    (proc / '123' / 'cgroup').write_text('0::/job\n')
    (proc / '123' / 'mountinfo').write_text(f'1 0 0:1 / {group} rw - cgroup2 cgroup rw\n')
    (job / 'memory.max').write_text('4096')
    (job / 'memory.events').write_text('oom 0\noom_kill 0\n')
    monitor = ProcessResourceEvidence(123, proc_root=proc, environment={'SLURM_JOB_ID': 'job-1', 'API_KEY': 'excluded'})
    (job / 'memory.events').write_text('oom 1\noom_kill 1\n')
    result = monitor.finish()
    assert result['sampled_peak_process_tree_rss_kib'] == 30
    assert result['cgroups'][0]['event_deltas']['memory.events']['oom_kill'] == 1
    assert result['root_cause_status'] == 'UNCONFIRMED'
    assert result['job_identifiers'] == {'SLURM_JOB_ID': 'job-1'}
    assert result['read_errors']  # Missing files are explicit, never unlimited.


def test_candidate_metrics_are_distinct_and_deduplicated(tmp_path):
    profiler = RunProfiler(tmp_path)
    ledger = {'events': {str(i): {'reason_code': 'GENERATE_SCRIPT_BLOCK_INVALID' if i < 5 else 'SKILL_INVOCATION_UNVERIFIABLE'} for i in range(8)}}
    profiler.record_candidate_rejections('task', ledger)
    profiler.record_candidate_rejections('task', ledger)
    snapshot = profiler.snapshot('task')
    assert snapshot['summary']['candidate_rejection_count'] == 8
    assert snapshot['summary']['candidate_format_rejection_count'] == 5
    assert snapshot['summary']['candidate_contract_rejection_count'] == 3
    assert 'Candidate rejections: 8' in profiler.render_markdown(snapshot)


def test_repair_basis_never_silently_recopies_expanded_execution_code():
    from agent_core.generate_protocol import repair_source_view
    draft = '# BIOMIND_EFFECT_HELPER\nprint("observation")\n'
    expanded = 'def emit_effect_evidence(facts):\n    return facts\nprint("observation")\n'
    current = repair_source_view({'model_authored_code': draft, 'generated_code': expanded})
    assert current['model_authored_code'] == draft
    assert current['code_basis'] == 'model_draft_before_framework_materialization'
    legacy = repair_source_view({'generated_code': expanded})
    assert legacy['model_authored_code'] == ''
    assert legacy['code_basis'] == 'draft_unavailable_inspect_original_generate_response'


def test_helper_argument_feedback_collects_independent_errors():
    from agent_core.skill_access import SkillAccessError
    resource = {'invocation': {'argv_prefix': ['/python', '/helper.py'], 'arguments': {
        '--input': {'nargs': 1, 'required': True}, '--output': {'nargs': 1, 'required': True}}}}
    with pytest.raises(SkillAccessError) as caught:
        build_skill_script_shell(resource, ['--wrong', 'value', '--input'])
    issues = caught.value.diagnostic['argument_issues']
    assert any('Unknown' in item['detail'] for item in issues)
    assert any('--input needs' in item['detail'] for item in issues)
    assert any('Missing required arguments: --output' in item['detail'] for item in issues)


def test_helper_public_message_projection_remains_a_single_candidate(tmp_path):
    from agent_core.public_response import project_generate_response
    service, access, _, _ = _fixture(tmp_path)
    raw = json.dumps({'type': 'skill_script', 'handle': HANDLE,
        'public_message': '参数已整理，等待检查与审批。',
        'arguments': ['--path', 'input.fq', '--observation-id', 'observation']})
    projected = project_generate_response(raw, script_protocol=True, strict=True)
    assert projected.error == ''
    assert projected.public_message == '参数已整理，等待检查与审批。'
    block = parse_generate_script_candidate(projected.payload, service, access,
        step_id=STEP_ID, cwd=tmp_path, strict=True)
    assert block.skill_resource_refs == (HANDLE,)


def test_terminal_separates_execution_candidate_and_stop_causes():
    from agent_core.cli_lifecycle import terminal_text
    facts = {'origin': {'error_code': 'COMMAND_FAILED', 'returncode': 137},
        'causal_execution': {'returncode': 137},
        'latest_failure': {'error_code': 'GENERATE_SCRIPT_BLOCK_INVALID'},
        'current_candidate': {'execution_started': False},
        'stop_reason': {'error_code': 'GENERATE_REPAIR_BUDGET_EXHAUSTED'},
        'prior_completed_work': [{'step_id': 'input_check'}, {'step_id': 'prepare_reference'}]}
    text = terminal_text('blocked', facts)
    assert '原始执行返回码：137' in text
    assert '当前修复障碍：GENERATE_SCRIPT_BLOCK_INVALID；候选尚未执行' in text
    assert '候选修正次数已耗尽' in text
    assert '已有 2 步正式提交' in text
    assert 'OOM' not in text


def test_executor_observes_real_process_without_classifying_oom(tmp_path):
    import execution.executor as executor
    from execution.executor import get_last_execution_metadata
    from tests.react_v3.support import actual_micro_runner
    with actual_micro_runner([]):
        stdout, stderr, rc = asyncio.run(executor.run_command('sleep 0.6; printf audit-ok', profile=False, cwd=tmp_path, env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}))
    assert (stdout, stderr, rc) == ('audit-ok', '', 0)
    metadata = get_last_execution_metadata()
    resources = metadata['resource_evidence']
    assert resources['launcher_pid'] == metadata['pid']
    assert resources['root_cause_status'] == 'UNCONFIRMED'
    assert resources['sample_count'] >= 2
    assert resources['launch_limits'] or resources['read_errors']
