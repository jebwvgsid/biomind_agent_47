"""Real disk + existing validators; candidate acceptance is not Execute admission."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from agent_core.draft_files import DraftFileSession
from agent_core.workspace_files import WorkspaceFileService
from agent_core.plan_contract import parse_planner_outcome_envelope, PlannerEnvelopeError, validated_planner_candidate_wire_example
from agent_core.plan_tracker import build_plan_state
from execution.script_validation import check_executable_action


def session(tmp_path, stage, validator, *, task_id='a'):
    root = tmp_path / 'runs'
    state_file = root / 'task_outputs' / task_id / 'plan_state.json'
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if not state_file.exists():
        state_file.write_text(json.dumps({'task_id': task_id, 'current_task': {'task_revision': 1}, 'plan_revision': 1}))
    def load():
        return json.loads(state_file.read_text())
    def save(value):
        target = state_file.with_suffix('.tmp')
        target.write_text(json.dumps(value))
        target.replace(state_file)
    commands = SimpleNamespace(data_path=root, deny_paths=(), _task_dir=lambda tid: root / 'task_outputs' / tid / 'command_inspection')
    return DraftFileSession(WorkspaceFileService(commands), task_id=task_id, stage=stage,
                            load_state=load, persist_state=save, validate_submit=validator)


def planner_validate(content, reference):
    try:
        envelope = parse_planner_outcome_envelope(content, allow_legacy_plan=False)
        plan = build_plan_state('a', json.dumps(envelope.plan_candidate_body()), strict=True)
        return {'accepted': True, 'plan': plan}
    except PlannerEnvelopeError as exc:
        return {'accepted': False, 'errors': [{'code': exc.code, **exc.diagnostic}]}


def test_planner_two_local_patches_restart_and_complete_current_file(tmp_path):
    async def run():
        wire = json.dumps(validated_planner_candidate_wire_example(), ensure_ascii=False)
        # Two independent defects force two actual validator observations.
        wrong = wire.replace('"outcome_type": "PLAN_CANDIDATE"', '"outcome_type": "INVALID"')
        assert wrong != wire
        wrong = wrong[:-1] + ',}'
        draft = session(tmp_path, 'planner', planner_validate)
        first = await draft.dispatch('create_draft_file', {'content': wrong})
        assert first['status'] == 'editing'
        rejected = await draft.dispatch('submit_draft_file', {})
        assert rejected['accepted'] is False
        assert rejected['validation_issues'][0]['line'] >= 1
        assert 'content' not in rejected
        # A fresh process-level session locates the file and precise rejection.
        draft = session(tmp_path, 'planner', planner_validate)
        assert draft.reference['validation_issues'] == rejected['validation_issues']
        patch1 = {'old_text': wrong[-50:], 'new_text': wrong[-50:-2] + '}'}
        assert (await draft.dispatch('patch_draft_file', patch1))['status'] == 'editing'
        rejected2 = await draft.dispatch('submit_draft_file', {})
        assert rejected2['accepted'] is False
        assert rejected2['validation_issues'][0]['field_path'] == '$.outcome_type'
        patch2 = {'old_text': '"outcome_type": "INVALID"', 'new_text': '"outcome_type": "PLAN_CANDIDATE"'}
        assert (await draft.dispatch('patch_draft_file', patch2))['status'] == 'editing'
        accepted = await draft.dispatch('submit_draft_file', {})
        assert accepted['accepted'] is True, accepted
        assert draft.accepted_content == wire
        assert draft.accepted_result['plan']['steps']
        assert (Path(accepted['path']).parent / 'plan.md').is_file()
        # Provider correction actions never contain a replacement full Plan.
        assert len(json.dumps(patch1)) + len(json.dumps(patch2)) < len(wire)
        assert len(json.dumps(accepted)) < len(wire)
        read = await draft.dispatch('read_draft_file', {'start_line': 1, 'max_lines': 1})
        values = {'first_body': wrong, 'first_patch': json.dumps(patch1), 'second_patch': json.dumps(patch2),
                  'read_receipt': json.dumps(read), 'submit_arguments': '{}', 'submit_receipt': json.dumps(accepted)}
        metrics = {key: {'chars': len(value), 'utf8_bytes': len(value.encode()), 'reference_tokens_chars_div_4': (len(value)+3)//4} for key, value in values.items()}
        (tmp_path/'planner_patch_lengths.json').write_text(json.dumps({'measurement': 'offline reference only; not provider usage or verified API savings', 'messages': metrics}, indent=2))
    asyncio.run(run())


def test_generate_patch_current_file_then_run_immutable_accepted_content(tmp_path):
    checked = []
    def validate(content, reference):
        check = check_executable_action({'code': content, 'language': 'bash'})
        checked.append(content)
        return {'accepted': check['check_status'] == 'PASS', 'errors': [] if check['check_status'] == 'PASS' else [check]}
    async def run():
        draft = session(tmp_path, 'generate', validate)
        bad = 'if true; then\n    printf "accepted-version\\n"\n'
        created = await draft.dispatch('create_draft_file', {'name': '01.print.sh', 'content': bad})
        reject = await draft.dispatch('submit_draft_file', {})
        assert reject['accepted'] is False
        assert reject['validation_issues'][0]['detail']
        await draft.dispatch('patch_draft_file', {'old_text': bad[-20:], 'new_text': bad[-20:] + 'fi\n'})
        assert (await draft.dispatch('submit_draft_file', {}))['accepted'] is True
        assert checked == [bad, bad + 'fi\n']
        ref = draft.accepted_reference
        Path(created['path']).write_text('raise Exception("unapproved working copy")\n')
        frozen = draft.store.read_object(ref['content_ref'], expected_type='draft_candidate_content')
        sealed = tmp_path / 'accepted.sh'
        sealed.write_text(frozen['content'])
        bootstrap = sys.modules['__main__']
        allowed = getattr(bootstrap, 'OFFLINE_ALLOWED_SCRIPT_PATHS', set())
        allowed.add(str(sealed))
        try:
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(sealed)], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'})
        finally:
            allowed.discard(str(sealed))
        assert result.returncode == 0 and result.stdout.strip() == 'accepted-version'
        assert 'FILE_CHANGED' in (await draft.dispatch('submit_draft_file', {}))['error']
        assert draft.accepted_content is None
    asyncio.run(run())


def test_conflicts_scope_isolation_and_no_guessing(tmp_path):
    async def run():
        accept = lambda content, reference: {'accepted': True}
        a = session(tmp_path, 'generate', accept)
        b = session(tmp_path, 'generate', accept, task_id='b')
        await a.dispatch('create_draft_file', {'content': 'repeat\nrepeat\nend\n'})
        await b.dispatch('create_draft_file', {'content': 'other session\n'})
        assert a.reference['path'] != b.reference['path']
        assert 'PATCH_TARGET_NOT_UNIQUE' in (await a.dispatch('patch_draft_file', {'old_text': 'repeat', 'new_text': 'x'}))['error']
        assert 'PATCH_TARGET_NOT_FOUND' in (await a.dispatch('patch_draft_file', {'old_text': 'missing', 'new_text': 'x'}))['error']
        path = Path(a.reference['path'])
        path.write_text('external\n')
        assert 'FILE_CHANGED' in (await a.dispatch('patch_draft_file', {'old_text': 'external', 'new_text': 'changed'}))['error']
        assert (await a.dispatch('read_draft_file', {'max_lines': 1}))['content'] == 'external\n'
        assert (await a.dispatch('patch_draft_file', {'old_text': 'external', 'new_text': 'changed'}))['status'] == 'editing'
        state = a.load_state()
        state['plan_revision'] += 1
        a.persist_state(state)
        assert 'DRAFT_SCOPE_CHANGED' in (await a.dispatch('submit_draft_file', {}))['error']
        assert Path(b.reference['path']).read_text() == 'other session\n'
        plan = session(tmp_path/'revision', 'planner', accept)
        await plan.dispatch('create_draft_file', {'content': '{"steps":[{"title":"before"}]}'})
        state = plan.load_state(); state['current_task']['task_revision'] += 1; plan.persist_state(state)
        revised = session(tmp_path/'revision', 'planner', accept)
        await revised.dispatch('create_draft_file', {'name': 'plan.revision2.draft.json', 'content': '{"steps":[{"title":"after"}]}'})
        assert 'after' in (revised.directory/'plan.md').read_text()
        assert 'before' in (revised.directory/'plan.draft.json').read_text()
    asyncio.run(run())


def test_unsafe_paths_and_orphan_cannot_be_submitted(tmp_path):
    async def run():
        a = session(tmp_path, 'generate', lambda c, r: {'accepted': True})
        for name in ('../victim.py', '.env.api_keys', '/tmp/victim.py'):
            assert (await a.dispatch('create_draft_file', {'name': name, 'content': 'pass'}))['status'] == 'not_run'
        orphan = a.directory / '01.step.sh'
        orphan.write_text('unrecorded interrupted create')
        assert 'DRAFT_MISSING' in (await a.dispatch('submit_draft_file', {}))['error']
        assert (await a.dispatch('create_draft_file', {'content': 'overwrite'}))['status'] == 'not_run'
        assert orphan.read_text() == 'unrecorded interrupted create'
    asyncio.run(run())


def test_correction_history_keeps_raw_archive_and_short_reference(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from agent_core.draft_files import draft_response_for_history
    from agent_core.planner_context import save_correction_context
    from agent_core.state_contract_store import StateContractStore
    raw = '{"outcome_type":"PLAN_CANDIDATE","explanation":"' + 'long candidate ' * 2000 + '"}'
    ref = {'path': str(tmp_path / 'plan.draft.json'), 'status': 'rejected'}
    message = AIMessage(content=raw, response_metadata={'draft_file': ref})
    projected = draft_response_for_history(message)
    assert len(projected.content) < 500 and message.content == raw
    assert draft_response_for_history(AIMessage(content='ordinary')).content == 'ordinary'
    state = {'task_state': {'planner_loop': {'draft_file': ref}, 'planning_failure': {'validation_issues': [{'code': 'TEST_INVALID', 'field_path': '$.steps', 'message': 'steps required'}]}}}
    feedback = save_correction_context(str(tmp_path), 'a', state, task_spec={}, raw=raw,
        candidate={}, provider_messages=[SystemMessage(content='rules'), HumanMessage(content='task')],
        fixed_prefix_count=2, capabilities={})
    record = StateContractStore(tmp_path, 'a').read_object(state['task_state']['planning_failure']['context_ref'], expected_type='planner_correction_context')
    assert record['raw_candidate'] == raw
    assert len(record['messages'][-2]['data']['content']) < 500
    assert feedback['draft_file']['path'] == ref['path']
    assert state['task_state']['planner_loop']['draft_file']['status'] == 'rejected'
    assert state['task_state']['planner_loop']['draft_file']['validation_issues'][0]['field_path'] == '$.steps'




