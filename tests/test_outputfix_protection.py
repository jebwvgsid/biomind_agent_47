"""Real native executions protect old bytes; no production acceptance stub."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from agent_core.action_runtime import execute_formal_command, ActionRuntimeContractError
from agent_core.plan_tracker import load_plan_state, save_execution_plan, save_plan_state
from tests.contracts_support import contract_fixture
from tests.react_v3.support import actual_micro_runner


def command(f, target, text='new', rc=0):
    code = ('from pathlib import Path; import sys; '
            f'p=Path({str(target)!r}); p.parent.mkdir(parents=True,exist_ok=True); '
            f'p.write_text({text!r}); sys.exit({rc})')
    return {'argv': [sys.executable, '-c', code], 'workdir': str(f.root)}


def setup(f):
    target = f.root / 'output' / 'result.txt'
    save_execution_plan(f.root, f.task_id, {'steps': [{'step_id': 'work',
        'description': 'Write the declared result from this isolated engineering task.',
        'outputs': [str(target)]}]})
    return target


@pytest.mark.parametrize('kind', ['foreign', 'symlink', 'parent_symlink'])
def test_native_foreign_outputs_rejected_before_any_launch(kind):
    async def run():
        async with contract_fixture('protect-' + kind) as f:
            target = setup(f); target.parent.mkdir()
            if kind == 'foreign':
                target.write_text('old original')
            elif kind == 'symlink':
                old = f.root / 'old.txt'; old.write_text('old original'); target.symlink_to(old)
            else:
                other = f.root / 'actual'; other.mkdir(); (other / target.name).write_text('old original')
                target.parent.rmdir(); target.parent.symlink_to(other, target_is_directory=True)
            launches = []
            with actual_micro_runner(launches), pytest.raises((ActionRuntimeContractError, ValueError)):
                await execute_formal_command(f.agent.command_service, command(f, target),
                    task_id=f.task_id, call_id='first')
            assert launches == [] and target.read_text() == 'old original'
            assert not load_plan_state(f.root, f.task_id).get('execution_evidence')
            assert not list(f.root.rglob('.biomind-backup-*'))
    asyncio.run(run())


def test_owned_retry_and_receipt_replay_preserve_single_process_identity():
    async def run():
        async with contract_fixture('protect-owned') as f:
            target = setup(f); launches = []
            first_args = command(f, target, 'partial', 1)
            with actual_micro_runner(launches):
                first = await execute_formal_command(f.agent.command_service, first_args,
                    task_id=f.task_id, call_id='first')
                original = Path(first['receipt_ref']).read_bytes()
                replay = await execute_formal_command(f.agent.command_service, first_args,
                    task_id=f.task_id, call_id='first')
                second = await execute_formal_command(f.agent.command_service, command(f, target, 'finished'),
                    task_id=f.task_id, call_id='second')
                stale = await execute_formal_command(f.agent.command_service, first_args,
                    task_id=f.task_id, call_id='first')
            from agent_core.execution_repair import execution_repair_status
            current = load_plan_state(f.root, f.task_id)
            assert stale['replayed'] and current['execution_evidence']['attempt_id'] == second['attempt_id']
            assert execution_repair_status(current, stale) == {}
            assert len(launches) == 2 and replay['replayed'] and second['exit_code'] == 0
            assert target.read_text() == 'finished'
            assert Path(first['receipt_ref']).read_bytes() == original
            assert second['output_write_protection']['owned_targets'][str(target)]['producer_attempt_id'] == first['attempt_id']
            assert not list(f.root.rglob('.biomind-backup-*'))
    asyncio.run(run())


@pytest.mark.parametrize('change', ['replace_same_bytes', 'edit', 'tamper_receipt'])
def test_external_changes_cannot_borrow_task_ownership(change):
    async def run():
        async with contract_fixture('protect-change-' + change) as f:
            target = setup(f); launches = []
            with actual_micro_runner(launches):
                first = await execute_formal_command(f.agent.command_service, command(f, target),
                    task_id=f.task_id, call_id='first')
                if change == 'replace_same_bytes':
                    target.unlink(); target.write_text('new')
                elif change == 'edit':
                    target.write_text('external edit')
                else:
                    Path(first['receipt_ref']).write_text('{}')
                before = target.read_bytes()
                with pytest.raises(ActionRuntimeContractError, match='OUTPUT_REPLACEMENT_NOT_APPROVED'):
                    await execute_formal_command(f.agent.command_service, command(f, target, 'overwritten'),
                        task_id=f.task_id, call_id='second')
            assert len(launches) == 1 and target.read_bytes() == before
    asyncio.run(run())


def test_output_changed_during_preparation_is_not_dispatched(monkeypatch):
    async def run():
        async with contract_fixture('protect-race') as f:
            target = setup(f); launches = []
            import agent_core.work_output_protection as protection
            original = protection.prepare_output_protection
            def race(*args, **kwargs):
                result = original(*args, **kwargs)
                target.parent.mkdir(exist_ok=True); target.write_text('concurrent writer')
                return result
            monkeypatch.setattr(protection, 'prepare_output_protection', race)
            with actual_micro_runner(launches), pytest.raises(ActionRuntimeContractError, match='OUTPUT_TARGET_CHANGED_SINCE_REVIEW'):
                await execute_formal_command(f.agent.command_service, command(f, target),
                    task_id=f.task_id, call_id='first')
            assert launches == [] and target.read_text() == 'concurrent writer'
            state = load_plan_state(f.root, f.task_id)
            assert not (state.get('provenance_state') or {}).get('attempts')
            assert not (state.get('task_state') or {}).get('budgets', {}).get('primary_execution_dispatches')
    asyncio.run(run())




@pytest.mark.parametrize('prior_backup', ['complete', 'partial', 'symlink'])
def test_backup_component_reentry_preserves_completed_copy_only(tmp_path, prior_backup):
    from agent_core.action_runtime import capture_output_write_guard, preserve_approved_output_files
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.write_bytes(b'original first'); second.write_bytes(b'original second')
    guard = capture_output_write_guard([{'expected_path': str(first)}, {'expected_path': str(second)}])
    backup = Path(guard['targets'][0]['backup_path'])
    if prior_backup == 'symlink': backup.symlink_to(first)
    else: backup.write_bytes(first.read_bytes() if prior_backup == 'complete' else b'partial')
    if prior_backup == 'complete':
        receipt = preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
        assert len(receipt['backups']) == 2
        for row in receipt['backups']:
            assert Path(row['backup_path']).read_bytes() == Path(row['original_path']).read_bytes()
    else:
        with pytest.raises((ActionRuntimeContractError, OSError)):
            preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
        assert not Path(guard['targets'][1]['backup_path']).exists()
    assert first.read_bytes() == b'original first' and second.read_bytes() == b'original second'


@pytest.mark.parametrize('change_source', [False, True])
def test_script_freeze_reentry_after_predispatch_failure(monkeypatch, change_source):
    async def run():
        async with contract_fixture('protect-source-' + str(change_source)) as f:
            target = setup(f)
            source = f.root / 'writer.py'
            source.write_text('from pathlib import Path\np=Path('+repr(str(target))+');p.parent.mkdir(exist_ok=True);p.write_text("new")\n')
            args = {'argv': [sys.executable, str(source)], 'script_path': str(source), 'workdir': str(f.root)}
            import agent_core.work_output_protection as protection
            original = protection.prepare_output_protection
            calls = []
            def fail_once(*a, **kw):
                calls.append(True)
                if len(calls) == 1: raise PermissionError('isolated failure after source freeze')
                return original(*a, **kw)
            monkeypatch.setattr(protection, 'prepare_output_protection', fail_once)
            launches = []
            with actual_micro_runner(launches):
                with pytest.raises(PermissionError, match='isolated failure'):
                    await execute_formal_command(f.agent.command_service, args, task_id=f.task_id, call_id='script')
                assert not launches
                frozen = next((f.root/'task_outputs'/f.task_id/'attempts').glob('*/source/writer.py'))
                before = frozen.read_bytes()
                if change_source:
                    source.write_text('raise RuntimeError("changed source")\n')
                    with pytest.raises(ValueError, match='FROZEN_SOURCE_VERSION_CHANGED'):
                        await execute_formal_command(f.agent.command_service, args, task_id=f.task_id, call_id='script')
                    assert not launches and not target.exists()
                else:
                    result = await execute_formal_command(f.agent.command_service, args, task_id=f.task_id, call_id='script')
                    assert result['exit_code'] == 0 and len(launches) == 1 and target.read_text() == 'new'
                assert frozen.read_bytes() == before
    asyncio.run(run())
