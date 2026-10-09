"""R13/R14 temporary-file acceptance of production report preservation."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_core.plan_tracker import load_plan_state, save_plan_state, approve_plan_state, build_plan_state
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow import runtime_recovery_reporting as reporting
from agent_core.workflow import report_preservation as preservation

NAMES = ['report.md', 'reproduce.sh', 'stage_status.tsv']


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Protected-path policy intentionally rejects credential-like test names.
    return tmp_path_factory.mktemp('report-fix')


def task(root, *, approved=True, authorized=True):
    output = root / 'reports'
    output.mkdir()
    paths = [output / name for name in NAMES]
    for path in paths:
        path.write_text('previous ' + path.name)
    state = load_plan_state(root, 'preserve')
    spec = create_task_spec('Preserve the three old reports with a date suffix then write new reports',
        task_id='preserve', base_dir=root, project_root=root,
        structured_source={'contract_source_type': 'user_explicit', 'expected_outputs': [
            {'role': role, 'path': str(path), 'artifact_kind': 'file', 'required': True}
            for role, path in zip(['report', 'reproduce', 'stage_status'], paths)]})
    from agent_core.step_effects import canonical_step_effect
    step = {'step_id': 'step.reports', 'title': 'Preserve and write reports',
        'action': 'execute', 'purpose': 'analysis', 'tool': 'bash', 'skill_ids': [],
        'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low',
        'produces_observations': [], 'inputs': [],
        'outputs': ['output.report', 'output.reproduce', 'output.stage_status'],
        'dependencies': [], 'operation': 'finalize_outputs',
        'execution_capability_id': 'framework.generic.execute',
        'success_criteria': ['Preserve all three old files then publish new reports.']}
    state = build_plan_state('preserve', json.dumps({'steps': [step], 'validation_checklist': [],
        'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}), strict=True)
    for current in state['steps']:
        current['step_effect'] = canonical_step_effect(current)
    state.update(workflow_routing_schema_version=2, plan_commit_requested=True)
    state['task_state']['workflow_routing_schema_version'] = 2
    state['current_task'] = {'task_spec': spec, 'task_revision': task_spec_revision(spec)}
    if authorized:
        state.setdefault('task_state', {})['report_preservation_authorization'] = {
            'operation': 'preserve_reports_date_suffix', 'source': 'user_explicit',
            'source_ref': 'user-event-U2', 'paths': list(map(str, paths)),
            'naming': 'date_suffix', 'date_suffix': '20260921'}
    save_plan_state(root, 'preserve', state, allow_canonical_task_write=True)
    if approved:
        state = load_plan_state(root, 'preserve')
        state = approve_plan_state(state, task_spec_revision(spec), review_event_id='explicit-test-plan-review')
        save_plan_state(root, 'preserve', state)
    return paths


def publish(root, target):
    return reporting.publish_report(root, 'preserve', target, 'new report', owner='approved-plan-test',
        authorization_ref='user-event-output', scope_guard=lambda state: state['task_id'] == 'preserve')


def test_r13_consult_readonly_and_real_publication_preserves_all_three(tmp_path):
    paths = task(tmp_path)
    state = load_plan_state(tmp_path, 'preserve')
    before = deepcopy(state)
    result = reporting.report_publication_preflight(state, tmp_path / 'task_outputs' / 'preserve')
    assert result['status'] == 'ready_with_prerequisites' and result['planning_allowed']
    assert not result['publication_ready'] and len(result['prerequisites']) == 3
    assert state == before and all(path.read_text().startswith('previous') for path in paths)
    receipt = publish(tmp_path, paths[0])
    assert receipt['path'] == str(paths[0]) and paths[0].read_text() == 'new report'
    assert all(path.with_name(path.name + '.20260921').read_text() == 'previous ' + path.name for path in paths)
    assert not paths[1].exists() and not paths[2].exists()
    assert all(row['status'] == 'completed' for row in load_plan_state(tmp_path, 'preserve')['task_state']['report_preservation_operations'].values())
    publish(tmp_path, paths[0])
    assert not list(paths[0].parent.glob('*.20260921.1'))


def test_r13_unapproved_plan_cannot_move_even_with_explicit_rename_authorization(tmp_path):
    paths = task(tmp_path, approved=False)
    with pytest.raises(reporting.RuntimeReportError, match='PLAN_APPROVAL_REQUIRED'):
        publish(tmp_path, paths[0])
    assert all(path.read_text().startswith('previous') for path in paths)


def test_r13_without_authorization_conflicts_remain_protected(tmp_path):
    paths = task(tmp_path, authorized=False)
    result = reporting.report_publication_preflight(load_plan_state(tmp_path, 'preserve'), tmp_path / 'task_outputs' / 'preserve')
    assert not result['planning_allowed'] and result['status'] == 'needs_user_decision'
    with pytest.raises(reporting.RuntimeReportError, match='EXISTING_FILE_PROTECTED'):
        publish(tmp_path, paths[0])


def test_r14_date_collision_chooses_unique_suffix_without_overwrite(tmp_path):
    paths = task(tmp_path)
    collision = paths[0].with_name('report.md.20260921')
    collision.write_text('unrelated backup')
    publish(tmp_path, paths[0])
    assert collision.read_text() == 'unrelated backup'
    assert paths[0].with_name('report.md.20260921.1').read_text() == 'previous report.md'


@pytest.mark.parametrize('failure_point', ['after_first_complete', 'after_move_before_finalize', 'after_prepare_before_move'])
def test_r14_partial_interruption_reenters_using_exact_identity_and_journal(tmp_path, failure_point):
    paths = task(tmp_path)
    if failure_point == 'after_first_complete':
        original = preservation._perform_one
        calls = []
        def interrupt(*args):
            if calls:
                raise RuntimeError('isolated crash')
            calls.append(1)
            return original(*args)
        target, replacement = '_perform_one', interrupt
    elif failure_point == 'after_move_before_finalize':
        target, replacement = 'finalize_mutation_attestation', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('isolated crash'))
    else:
        target, replacement = '_rename_noreplace', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('isolated crash'))
    with patch('agent_core.workflow.report_preservation.' + target, side_effect=replacement):
        with pytest.raises(RuntimeError, match='isolated crash'):
            publish(tmp_path, paths[0])
    # No new report may be written until the entire group has completed.
    assert not paths[0].exists() or paths[0].read_text() == 'previous report.md'
    publish(tmp_path, paths[0])
    assert paths[0].read_text() == 'new report'
    assert all(path.with_name(path.name + '.20260921').read_text() == 'previous ' + path.name for path in paths)
    assert not list(paths[0].parent.glob('*.20260921.1'))


def test_r14_equal_content_replacement_inode_is_not_accepted_as_completed_move(tmp_path):
    paths = task(tmp_path)
    with patch('agent_core.workflow.report_preservation.finalize_mutation_attestation', side_effect=RuntimeError('isolated crash')):
        with pytest.raises(RuntimeError):
            publish(tmp_path, paths[0])
    backup = paths[0].with_name('report.md.20260921')
    replacement = backup.with_name('unrelated-copy')
    replacement.write_bytes(backup.read_bytes())
    replacement.replace(backup)
    with pytest.raises(reporting.RuntimeReportError, match='TARGET_CHANGED'):
        publish(tmp_path, paths[0])
    assert not paths[0].exists()


def test_r14_late_collision_after_freeze_selects_unique_authorized_target(tmp_path):
    paths = task(tmp_path)
    original = preservation._perform_one
    injected = []
    def collide(*args):
        if not injected:
            injected.append(True)
            Path(args[2]['target']).write_text('late competitor')
        return original(*args)
    with patch.object(preservation, '_perform_one', side_effect=collide):
        publish(tmp_path, paths[0])
    assert paths[0].with_name('report.md.20260921').read_text() == 'late competitor'
    assert paths[0].with_name('report.md.20260921.1').read_text() == 'previous report.md'


def test_r14_source_changed_after_freeze_blocks_without_new_publication(tmp_path):
    paths = task(tmp_path)
    original = preservation._perform_one
    def change(*args):
        Path(args[2]['source']).write_text('changed by another writer')
        return original(*args)
    with patch.object(preservation, '_perform_one', side_effect=change):
        with pytest.raises(reporting.RuntimeReportError, match='SOURCE_CHANGED'):
            publish(tmp_path, paths[0])
    assert paths[0].read_text() == 'changed by another writer'
    assert not paths[0].with_name('report.md.20260921').exists()
