"""Declared output protection; real graph/approval with an explicit process stub."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.action_runtime import (ActionRuntimeContractError, capture_output_write_guard,
    validate_output_write_guard, preserve_approved_output_files)


def contract(path):
    return [{'artifact_id': 'report', 'expected_path': str(path), 'artifact_kind': 'file'}]


@pytest.mark.parametrize('decision', ['approve', '', 'y'])
def test_existing_file_requires_explicit_backup_choice(tmp_path, decision):
    target = tmp_path / '中文 ann.vcf'; target.write_bytes(b'old result')
    guard = capture_output_write_guard(contract(target))
    with pytest.raises(ActionRuntimeContractError, match='OUTPUT_REPLACEMENT_NOT_APPROVED'):
        preserve_approved_output_files(guard, decision=decision, approval_identity={})
    assert target.read_bytes() == b'old result'
    assert not list(tmp_path.glob('.biomind-backup-*'))


def test_backup_is_independent_copy_and_exclusive(tmp_path):
    target = tmp_path / 'ann.vcf'; target.write_bytes(b'old result')
    guard = capture_output_write_guard(contract(target))
    receipt = preserve_approved_output_files(guard, decision='approve_backup_replace',
                                            approval_identity={'approval_identity_hash': 'approved'})
    backup = Path(receipt['backups'][0]['backup_path'])
    target.write_bytes(b'new result')
    assert backup.read_bytes() == b'old result'
    assert backup.stat().st_ino != target.stat().st_ino
    assert receipt['backups'][0]['sha256'] == hashlib.sha256(b'old result').hexdigest()
    # Neither a changed target nor a preexisting backup is ever overwritten.
    with pytest.raises(ActionRuntimeContractError):
        validate_output_write_guard(guard)


@pytest.mark.parametrize('change', ['appeared', 'changed', 'symlink', 'parent_symlink', 'scope', 'tampered'])
def test_target_changes_revoke_authority(tmp_path, change):
    target = tmp_path / 'ann.vcf'
    if change != 'appeared': target.write_text('old')
    guard = capture_output_write_guard(contract(target))
    if change in {'appeared', 'changed'}: target.write_text('different')
    elif change == 'symlink':
        target.unlink(); target.symlink_to(tmp_path / 'missing')
    elif change == 'parent_symlink':
        parent = tmp_path / 'dir'; parent.mkdir(); target = parent / 'ann.vcf'
        guard = capture_output_write_guard(contract(target))
        parent.rmdir(); parent.symlink_to(tmp_path, target_is_directory=True)
    elif change == 'tampered': guard['targets'] = []
    with pytest.raises(ActionRuntimeContractError):
        validate_output_write_guard(guard, contract(tmp_path / 'other') if change == 'scope' else None)


def test_backup_collision_preserves_both_files(tmp_path):
    target = tmp_path / 'ann.vcf'; target.write_text('original')
    guard = capture_output_write_guard(contract(target))
    backup = Path(guard['targets'][0]['backup_path']); backup.write_text('unrelated')
    with pytest.raises(FileExistsError):
        preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
    assert backup.read_text() == 'unrelated' and target.read_text() == 'original'


def test_backup_parent_race_does_not_write_through_symlink(tmp_path, monkeypatch):
    parent = tmp_path / 'outputs'; parent.mkdir()
    other = tmp_path / 'unapproved'; other.mkdir()
    target = parent / 'ann.vcf'; target.write_text('original')
    guard = capture_output_write_guard(contract(target))
    original_open = os.open
    switched = False
    def raced_open(path, flags, *args, **kwargs):
        nonlocal switched
        fd = original_open(path, flags, *args, **kwargs)
        if path == 'ann.vcf' and kwargs.get('dir_fd') is not None and not switched:
            switched = True
            parent.rename(tmp_path / 'moved')
            parent.symlink_to(other, target_is_directory=True)
        return fd
    monkeypatch.setattr(os, 'open', raced_open)
    with pytest.raises(ActionRuntimeContractError):
        preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
    assert switched and not list(other.iterdir())
    assert (tmp_path / 'moved' / 'ann.vcf').read_text() == 'original'


@pytest.mark.parametrize('answer,expected', [('', 'approve_backup_replace'), ('y', 'approve_backup_replace'), ('backup', 'approve_backup_replace'), ('n', 'reject')])
def test_existing_review_cannot_auto_approve_or_accept_enter(tmp_path, answer, expected):
    import agent_core.middleware as middleware
    target = tmp_path / 'ann.vcf'; target.write_text('old')
    provenance = {'output_write_guard': capture_output_write_guard(contract(target))}
    answers = [answer, 'backup'] if answer in {'', 'y'} else [answer]
    with patch.object(middleware, 'interrupt', side_effect=answers) as ask:
        result = middleware.HITLMiddleware(auto_approve_safe=True).review_code('true', 'Bash', provenance=provenance)
    assert ask.call_count == len(answers) and result[0] == expected
    payload = ask.call_args.args[0]
    assert payload['output_conflicts'][0]['path'] == str(target)
    assert 'backup' in payload['msg'] and '已有文件' in payload['content']
    if len(answers) == 2:
        assert ask.call_args_list[0].args[0]['content'] == ask.call_args_list[1].args[0]['content']


def test_optional_output_is_protected_without_becoming_required(tmp_path):
    from agent_core.managed_artifacts import ManagedArtifactBinding, validate_concrete_action_artifacts
    target = tmp_path / 'optional.tsv'; target.write_text('old optional result')
    binding = ManagedArtifactBinding(artifact_id='optional', semantic_handle='output.optional',
        direction='output', format='tsv', path_policy='managed_exact', canonical_path=str(target),
        required_basename=target.name, required=False, artifact_kind='file')
    action, errors = validate_concrete_action_artifacts({'type': 'execute',
        'code': 'true', 'argv': ['bash', '-c', 'true'], 'output_guard_contracts': []},
        {'output.optional': binding})
    assert not errors
    assert action['controller_output_bindings'] == []
    assert action['output_guard_contracts'][0]['required'] is False
    guard = capture_output_write_guard(action['output_guard_contracts'])
    with pytest.raises(ActionRuntimeContractError, match='OUTPUT_REPLACEMENT_NOT_APPROVED'):
        preserve_approved_output_files(guard, decision='approve', approval_identity={})
    assert target.read_text() == 'old optional result'


@pytest.mark.parametrize('variant', ['new', 'backup', 'ordinary_approval', 'changed_after_approval'])
def test_guard_actual_generate_approval_execute_graph(tmp_path, variant):
    async def run():
        import bio_agents
        from langchain_core.messages import AIMessage
        from agent_core.plan_tracker import load_plan_state
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.ger_fixture_support import configure_protocol
        case = PlanLocalRealGraphTests(methodName='test_graph_a_plan_local_directory_happy_path')
        await case.asyncSetUp(); configure_protocol(case)
        target = case.root / 'ann.vcf'; task = 'output-write-' + variant
        old_consultant = case._consultant_payload
        case._configure_one_step_no_output_plan(); case._consultant_payload = old_consultant
        old_plan = case._planner_payload
        def plan():
            value = old_plan(); step = value['steps'][0]
            step['outputs'] = ['output.report']; step.pop('produces_observations', None)
            step['success_criteria'] = ['The actual output is nonempty.']
            return value
        case._planner_payload = plan
        if variant != 'new': target.write_text('old result')
        requests, reviews, executions, events = [], [], [], []
        async def provider(messages, *, stage, **kwargs):
            requests.append({'stage': stage, 'messages': [str(m.content) for m in messages]})
            if stage == 'consultant':
                if variant == 'backup' and len([r for r in requests if r['stage'] == 'consultant']) == 1:
                    value = {'action_type': 'inspect_resource', 'resource': str(target), 'role': 'output'}
                else:
                    value = case._consultant_payload(target)
                    if variant == 'backup':
                        value['summary'] = '指定目标已经存在。保留文件名；需要先明确批准备份后替换，旧文件不能直接覆盖。'
            elif stage in {'planner', 'planner_repair', 'planner_structure_repair'}: value = plan()
            elif stage == 'generate':
                assert len([r for r in requests if r['stage'] == 'generate']) == 1
                return AIMessage(content='<shell>\nprintf "new result" > ' + repr(str(target)) + '\n</shell>')
            else: raise AssertionError('Unexpected provider: ' + stage)
            return AIMessage(content=json.dumps(value))
        case.agent._ainvoke_node_llm = provider
        def review(code, language, **kwargs):
            reviews.append(deepcopy(kwargs['provenance']))
            if variant == 'changed_after_approval': target.write_text('external update')
            return ('approve_backup_replace' if variant in {'backup', 'changed_after_approval'} else 'approve'), code
        case.agent.middleware.review_code = review
        async def process(script, **kwargs):
            executions.append(script)
            if variant == 'backup':
                assert Path(reviews[-1]['output_write_guard']['targets'][0]['backup_path']).read_text() == 'old result'
            target.write_text('new result')
            if kwargs.get('lifecycle_callback'):
                await kwargs['lifecycle_callback']('PROCESS_STARTED', {'pid': 1, 'pgid': 1})
                await kwargs['lifecycle_callback']('PROCESS_EXITED', {'returncode': 0})
            return ('BIOMIND_EFFECT_EVIDENCE_JSON=' + json.dumps({'schema_version': 1,
                'produces_artifacts': [{'identity': 'report', 'path': str(target)}]}) + '\n', '', 0)
        try:
            runner = case._runner_state(task); app = case.agent._app_for_task(task, runner)
            with patch.object(bio_agents, 'run_script_path', new=process):
                stream = app.astream(runner, config={'configurable': {'thread_id': task}, 'recursion_limit': 20}, stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if 'execute' in event: break
                finally: await stream.aclose()
            assert reviews, events
            assert reviews[0]['output_write_guard']['targets'][0]['path'] == str(target)
            assert len(executions) == (1 if variant in {'new', 'backup'} else 0), events
            assert target.read_text() == ('new result' if executions else 'external update' if variant == 'changed_after_approval' else 'old result')
            for stage in ['consultant', 'planner', 'generate']:
                assert any('Existing-file protection (mandatory)' in '\n'.join(r['messages']) for r in requests if r['stage'] == stage)
            if variant == 'backup':
                planner_request = next(r for r in requests if r['stage'] == 'planner')
                assert '指定目标已经存在' in '\n'.join(planner_request['messages'])
                assert any('consultant' in event for event in events)
            receipts = list((case.root / 'task_outputs' / task).glob('attempts/*/output_write_approval.json'))
            assert len(receipts) == len(executions)
            if receipts:
                receipt = json.loads(receipts[0].read_text())
                assert receipt['approval_identity_hash'] and receipt['guard_hash']
            if variant == 'backup': assert len(json.loads(receipts[0].read_text())['backups']) == 1
            (tmp_path / (variant + '.json')).write_text(json.dumps({'requests': requests, 'reviews': reviews,
                'executions': executions, 'events': events}, default=str, ensure_ascii=False))
        finally: await case.asyncTearDown()
    asyncio.run(run())
