"""New tiny inputs + real batch/executor and production Recovery/Generate.

No original session or original script is an executable fixture. Run only via
the established safe export, two audit guards and persistent fatal outlet.
"""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from execution.executor import run_command
from agent_core.step_effects import extract_structured_effect_evidence
from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from tests.cli_model_progress_support import TimedWriter

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py'
PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}


def save(name, value):
    root = Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])
    (root / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def digest_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


async def approved_tiny(path, code):
    path.write_text('#!/bin/bash\n' + code + '\n')
    sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
    return await run_command('bash ' + shlex.quote(str(path)), timeout=20, profile=False, env=ENV)


def batch_args(directory, paths, *, joined=False, observation='audit-prefix'):
    argv = [PYTHON, str(BATCH), '--observation-id', observation,
            '--evidence-dir', str(directory), '--item-timeout-seconds', '5']
    for label, path in paths:
        argv += ['--item', label + ' ' + str(path)] if joined else ['--item', label, str(path)]
    return argv


def test_real_batch_parameter_directory_and_fasta_counterexamples(tmp_path):
    async def scenario():
        inputs = []
        for i in range(4):
            p = tmp_path / ('input %d.fq.gz' % i)
            p.write_bytes(gzip.compress(b'@synthetic\nACGT\n+\nIIII\n', mtime=0))
            inputs.append(('S%d' % i, p))
        fasta = tmp_path / 'synthetic-long.fasta'
        fasta.write_text('>synthetic\n' + 'A' * 17000 + '\n')
        short = tmp_path / 'synthetic-short.fasta'; short.write_text('>synthetic\nACGT\n')
        cases = [
            ('joined_item', True, True, inputs + [('reference_fasta', fasta)], 2, 'expected 2 arguments'),
            ('precreated_leaf', False, True, inputs, 1, 'FileExistsError'),
            ('renamed_but_precreated', False, True, inputs, 1, 'FileExistsError'),
            ('old_leaf_without_mkdir', False, False, inputs, 1, 'FileExistsError'),
            ('new_leaf', False, False, inputs, 0, None),
            ('next_attempt_new_leaf', False, False, inputs, 0, None),
            ('third_error_long_fasta', False, False, inputs + [('reference_fasta', fasta)], 2, None),
            ('short_fasta_is_not_fastq', False, False, inputs + [('reference_fasta', short)], 0, None),
        ]
        previous = None; old_evidence = {}; results = []
        for index, (name, joined, mkdir, paths, expected, error) in enumerate(cases):
            leaf = tmp_path / name
            if name == 'old_leaf_without_mkdir':
                leaf = tmp_path / 'precreated_leaf'
            argv = batch_args(leaf, paths, joined=joined)
            code = ('mkdir -p ' + shlex.quote(str(leaf)) + '\n' if mkdir else '') + 'exec ' + shlex.join(argv)
            out, err, rc = await approved_tiny(tmp_path / (name + '.sh'), code)
            save('batch-attempt-' + name, {'argv': argv, 'code': code, 'stdout': out, 'stderr': err, 'returncode': rc})
            assert rc == expected, (name, rc, err)
            if error: assert error in err
            observations = extract_structured_effect_evidence(out).get('produces_observations', [])
            row = {'case': name, 'argv': argv, 'returncode': rc, 'stdout': out, 'stderr': err,
                   'observation_count': len(observations), 'model_calls': 0}
            if index < 4:
                assert not observations and not (leaf / 'checks.json').exists()
                assert 'BIOMIND_PREFLIGHT_PROGRESS_JSON=' not in err
            else:
                assert len(observations) == 1 and observations[0]['identity'] == 'audit-prefix'
                value = observations[0]['value']; row['value'] = value
                checks = json.loads((leaf / 'checks.json').read_text()); row['checks'] = checks
                assert value['full_file_validated'] is False and value['passed'] == 4
                if name == 'third_error_long_fasta':
                    assert value['status'] == 'incomplete' and value['returned'] == 5
                    last = value['checks'][-1]; assert last['returncode'] == 2
                    raw = Path(last['raw']['stderr']).read_text(); row['nested_stderr'] = raw
                    assert json.loads(raw)['error']['code'] == 'READ_LINE_LIMIT'
                elif name == 'short_fasta_is_not_fastq':
                    assert value['status'] == 'returned' and not value['checks'][-1]['passed']
                else:
                    assert value['status'] == 'returned' and value['returned'] == 4
                    if previous: assert digest_tree(previous) == old_evidence
                    previous = leaf; old_evidence = digest_tree(leaf)
            results.append(row)
        save('batch-counterexamples', {'cases': results, 'wrapper_calls': len(cases),
             'nested_reads': 18, 'real_models': 0, 'old_evidence_preserved': True,
             'scope': 'FASTQ prefixes only; new tiny synthetic files, never original inputs'})
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['compact', 'off'])
def test_actual_recovery_admission_generate_and_format_correction(tmp_path, mode):
    async def scenario():
        import bio_agents
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.ger_fixture_support import configure_protocol
        from agent_core.workflow.recovery_node import Schema2RecoveryNode
        from agent_core.plan_tracker import load_plan_state
        case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp(); configure_protocol(case); case._configure_one_step_no_output_plan()
        original_plan = case._planner_payload
        def plan_payload():
            value = original_plan()
            value['steps'][0]['produces_observations'] = [{'identity': 'approved_command_succeeded',
                'evidence_contract': {'required_fields': ['value']}}]
            return value
        case._planner_payload = plan_payload
        task = 'synthetic-preflight-handoff-' + mode
        data = case.root / 'tiny.fq.gz'; data.write_bytes(gzip.compress(b'@new\nACGT\n+\nIIII\n', mtime=0))
        old_leaf, new_leaf = case.root / 'old-evidence', case.root / 'new-evidence'
        instruction = ('Use --item LABEL BOUND_PATH as two separate arguments. Keep the bound input. '
                       'Do not mkdir the evidence leaf. Use a fresh nonexisting evidence leaf within the approved log root. '
                       'Preserve FASTQ-only prefix scope and the observation identity.')
        correction = '<shell>\nexec ' + shlex.join(batch_args(new_leaf, [('sample', data)], observation='approved_command_succeeded')) + '\n</shell>'
        requests, recovery_requests, events, approvals, executions, receptions = [], [], [], [], [], []
        counts = {'consultant': 0, 'planner': 0, 'generate': 0}
        writer = TimedWriter(); owner = PlainTerminal(writer, enabled=mode != 'off')
        previous_owner = bind_terminal(owner)
        class RecoveryModel:
            async def ainvoke(self, messages, **kwargs):
                recovery_requests.append([{'type': m.type, 'content': deepcopy(m.content)} for m in messages])
                directive = {'schema_version': 1, 'directive_id': 'synthetic-repair', 'task_id': task,
                    'action': 'retry_step', 'target_step_id': 'step-direct', 'summary': '合成预检失败，修正参数和目录后重新审批。',
                    'reason_code': 'SCRIPT_BUG', 'instruction': instruction}
                if len(recovery_requests) == 1:
                    assert 'expected 2 arguments' in str(recovery_requests[-1])
                    directive.pop('schema_version')
                    content = 'Synthetic diagnostic text; no private reasoning. ' * 80
                else:
                    assert len(recovery_requests) == 2 and 'STAGE_FIELD_REQUIRED' in str(recovery_requests[-1])
                    content = '合成修正保持不变，补齐协议字段。'
                return AIMessage(content=content, tool_calls=[{'name': 'submit_recovery_directive',
                    'id': 'submit-%d' % len(recovery_requests), 'args': directive}])
        async def sink(event, payload):
            if event == 'recovery_response_received': receptions.append(deepcopy(payload))
            case.agent._schema2_recovery_event_sink(event, payload)
        recovery = Schema2RecoveryNode(data_path=str(case.root), model=RecoveryModel(),
            phase_store=case.agent.phase_archive_store, skill_service=case.agent.skill_access_service,
            event_sink=sink, provider_invoke=case.agent._schema2_recovery_provider_invoke)
        case.agent._schema2_runtime_node_handlers['recovery'] = recovery.run
        case.agent._schema2_node_handlers['recovery'] = recovery.run
        case.agent.schema2_app = bio_agents.build_schema2_production_graph(
            case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
        async def provider(messages, *, stage, node_context=None, **kwargs):
            assert stage in counts; counts[stage] += 1
            if stage == 'consultant': return AIMessage(content=json.dumps(case._consultant_payload(case.root / 'unused.md')))
            if stage == 'planner': return AIMessage(content=json.dumps(case._planner_payload()))
            wire = [{'type': m.type, 'content': deepcopy(m.content)} for m in messages]; requests.append(wire)
            if counts['generate'] == 1:
                return AIMessage(content='<shell>\nmkdir -p ' + shlex.quote(str(old_leaf)) + '\nexec ' +
                    shlex.join(batch_args(old_leaf, [('sample', data)], joined=True, observation='approved_command_succeeded')) + '\n</shell>')
            assert instruction in str(wire), 'Real Generate messages lost the admitted repair'
            durable = load_plan_state(case.root, task)
            receipt = durable['task_state']['typed_recovery_receipts']['synthetic-repair']
            assert receipt['reception']['admission']['status'] == 'admitted'
            assert receipt['outcome']['type'] == 'candidate_repair_required'
            assert 'expected 2 arguments' in str(wire)
            if counts['generate'] == 2:
                return AIMessage(content='Synthetic public prose outside the block.\n' + correction)
            assert counts['generate'] == 3 and 'GENERATE_SCRIPT_BLOCK_INVALID' in str(wire)
            assert wire[:len(requests[1])] == requests[1], 'Format correction changed the prior message prefix'
            return AIMessage(content=correction)
        case.agent._ainvoke_node_llm = provider
        def approve(code, language, **kwargs):
            approvals.append(hashlib.sha256(code.encode()).hexdigest()); return 'approve', code
        case.agent.middleware.review_code = approve
        async def dispatch(script_path, *, expected_sha256, lifecycle_callback=None, **kwargs):
            from execution.executor import run_script_path
            raw = Path(script_path).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == expected_sha256
            assert str(case.root).encode() in raw
            approved_path = Path(script_path).with_name('approved.sh')
            approved_raw = approved_path.read_bytes()
            assert str(approved_path).encode() in raw
            assert hashlib.sha256(approved_raw).hexdigest() == approvals[len(executions)]
            allowed = sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
            allowed.add(str(script_path))
            try:
                result = await run_script_path(script_path, expected_sha256=expected_sha256,
                    lifecycle_callback=lifecycle_callback, profile=False, env=ENV,
                    managed_log_paths=kwargs.get('managed_log_paths'))
            finally:
                allowed.discard(str(script_path))
            executions.append({'approved_sha256': hashlib.sha256(approved_raw).hexdigest(),
                               'executed_wrapper_sha256': expected_sha256, 'approved_code': approved_raw.decode(),
                               'stdout': result[0], 'stderr': result[1], 'returncode': result[2]})
            return result
        try:
            runner = case._runner_state(task)
            with patch.object(bio_agents, 'run_script_path', new=dispatch):
                stream = case.agent._app_for_task(task, runner).astream(runner,
                    config={'configurable': {'thread_id': task}, 'recursion_limit': 30}, stream_mode='updates')
                try:
                    async for event in stream:
                        events.append({k: {f: v[f] for f in v if f.endswith('_outcome')} for k,v in event.items()})
                        if 'execute' in event and len(executions) == 2: break
                finally: await stream.aclose()
            assert counts == {'consultant': 1, 'planner': 1, 'generate': 3}
            assert len(recovery_requests) == 2 and len(approvals) == len(executions) == 2
            assert [e['returncode'] for e in executions] == [2, 0]
            assert approvals == [e['approved_sha256'] for e in executions]
            assert len(receptions[0]['explanation']) > 3000 and receptions[0]['status'] == 'not_accepted'
            assert len(receptions[1]['explanation']) < 100 and receptions[1]['status'] == 'candidate_validated'
            if mode == 'off': assert writer.getvalue() == ''
            save('handoff-' + mode, {'synthetic_root': str(case.root), 'counts': counts, 'recovery_calls': len(recovery_requests),
                'provider_requests': requests, 'admission_and_routes': events,
                'recovery_request_hashes': [hashlib.sha256(json.dumps(x,sort_keys=True).encode()).hexdigest() for x in recovery_requests],
                'receptions': [{k: x.get(k) for k in ['source_message_id','status','error_code']} |
                               {'public_chars': len(x['explanation'])} for x in receptions],
                'approvals': approvals, 'executions': executions,
                'format_prefix_preserved': True, 'original_session_loaded': False})
        finally:
            save('handoff-progress-' + mode, {'counts': counts, 'recovery_calls': len(recovery_requests),
                'provider_requests': requests, 'admission_and_routes': events,
                'approvals': approvals, 'executions': executions})
            bind_terminal(previous_owner); owner.close(); await case.asyncTearDown()
    asyncio.run(scenario())
