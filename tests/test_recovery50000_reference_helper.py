"""Stable reference preparation: actual parser, controlled OS-leaf failures.

Opt-in real-tools case executes installed binaries via the production immutable
script runner. It uses an isolated synthetic reference, never benchmark inputs.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys

import pytest
import yaml
from agent_core.genomics_reference_bundle import validate_reference_bundle_files
from agent_core.plan_tracker import (approve_plan_state, build_plan_state, load_plan_state,
    mark_step_verification_pending, record_provenance_event, save_plan_state)
from agent_core.step_effects import canonical_step_effect, extract_structured_effect_evidence
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.skill_script_invocation import (build_skill_script_argv, resolve_script_invocation,
                                               validate_skill_script_invocations)
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.verifier import TypedSupervisor
from execution.executor import run_script_path
from supervisor import SupervisorNode
from tests.test_unified_binding_rollback import _Reviewer
from tests.test_unified_reference_bundle import REAL_BWA_FIXTURE

SKILL = Path(__file__).resolve().parents[1] / 'skills/genomics/wgs-short-read-mapping'
_spec = importlib.util.spec_from_file_location('reference_helper_under_test', SKILL / 'scripts/prepare_reference.py')
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


def _request(tmp_path):
    root = tmp_path / '参考 空格 % #'
    root.mkdir()
    source = root / '源.fa'
    source.write_bytes(base64.b64decode(REAL_BWA_FIXTURE['reference.fa']))
    tools = {}
    for name in ('bwa', 'samtools', 'picard', 'java'):
        path = root / name
        path.write_text('#!/bin/sh\nexit 99\n')
        path.chmod(0o700)
        tools[name] = str(path)
    return dict(source=source, output=root / '正式 bundle', work_dir=root / '控制记录', tools=tools,
                reference_ref='input.reference_fasta', artifact_ref='output.reference_indexes',
                evidence_artifact_ref='output.reference_preparation_evidence',
                environment={'BIOMIND_TASK_ID': 'reference-small', 'BIOMIND_STEP_ID': 'prepare',
                    'BIOMIND_ACTION_ID': 'action-1', 'BIOMIND_ATTEMPT_ID': 'attempt-1',
                    'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'})


def _fake_command(calls, fail=None):
    def run(argv, *, cwd, env, stdout_path, stderr_path):
        component = 'bwa' if argv[1] == 'index' else ('fai' if argv[1] == 'faidx' else 'dictionary')
        calls.append(component)
        stdout_path.write_text('')
        stderr_path.write_text('Killed\n' if fail == (component, -9) else 'tool diagnostic\n')
        if fail and component == fail[0]:
            return fail[1]
        names = helper._COMPONENTS[component]
        for name in names:
            body = base64.b64decode(REAL_BWA_FIXTURE[name])
            if component == 'dictionary':
                uri = next(arg[4:] for arg in argv if arg.startswith('URI='))
                body = b'\n'.join(row.split(b'\tUR:')[0] + b'\tUR:' + uri.encode('ascii')
                    if row.startswith(b'@SQ') else row for row in body.split(b'\n'))
            (cwd / name).write_bytes(body)
        return 0
    return run


def test_picard_error_keeps_native_rc_and_never_publishes(tmp_path, monkeypatch):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls, ('dictionary', 1)))
    with pytest.raises(helper.PreparationError) as failure:
        helper.prepare_reference(**req)
    assert failure.value.returncode == 1
    assert calls == ['bwa', 'fai', 'dictionary']
    assert not req['output'].exists()
    journal = json.loads((req['work_dir'] / 'publication.json').read_text())
    assert journal['attempts'][0]['commands'][-1]['returncode'] == 1
    assert Path(journal['attempts'][0]['commands'][-1]['stderr_path']).is_file()


def test_cli_required_subprocess_failure_is_nonzero(tmp_path, monkeypatch, capsys):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls, ('dictionary', 1)))
    for key, value in req['environment'].items(): monkeypatch.setenv(key, value)
    argv = []
    for key, value in [('source', req['source']), ('output', req['output']), ('work-dir', req['work_dir']),
            ('reference-ref', req['reference_ref']), ('artifact-ref', req['artifact_ref']),
            ('evidence-artifact-ref', req['evidence_artifact_ref']), *req['tools'].items()]:
        argv += ['--' + key, str(value)]
    assert helper.main(argv) == 1
    captured = capsys.readouterr()
    assert not captured.out and json.loads(captured.err)['child_returncode'] == 1
    assert not req['output'].exists()


@pytest.mark.parametrize('component,code,expected', [('bwa', -9, ['bwa', 'fai', 'dictionary']),
    ('dictionary', 1, ['dictionary'])])
def test_owned_failed_residue_reuses_only_independently_valid_components(tmp_path, monkeypatch, component, code, expected):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls, (component, code)))
    with pytest.raises(helper.PreparationError): helper.prepare_reference(**req)
    journal = json.loads((req['work_dir'] / 'publication.json').read_text())
    assert journal['attempts'][0]['commands'][-1]['returncode'] == code
    assert 'OOM' not in json.dumps(journal)
    req['environment']['BIOMIND_ATTEMPT_ID'] = 'attempt-2'
    calls.clear(); monkeypatch.setattr(helper, '_command', _fake_command(calls))
    result = helper.prepare_reference(**req)
    assert calls == expected
    assert result['status'] == 'published_uncommitted'
    assert validate_reference_bundle_files(req['source'], req['output'],
        expected_final_reference=req['output'] / 'reference.fa', require_final_uri=True)['value']


def test_changed_owned_component_is_rebuilt_not_trusted_by_file_count(tmp_path, monkeypatch):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls, ('dictionary', 1)))
    with pytest.raises(helper.PreparationError): helper.prepare_reference(**req)
    journal = json.loads((req['work_dir'] / 'publication.json').read_text())
    (Path(journal['attempts'][0]['staging']) / 'reference.fa.sa').write_bytes(b'bad')
    calls.clear(); monkeypatch.setattr(helper, '_command', _fake_command(calls))
    helper.prepare_reference(**req)
    assert calls == ['bwa', 'dictionary']


@pytest.mark.parametrize('conflict', ['unknown_output', 'foreign_work', 'foreign_task', 'symlink'])
def test_unknown_or_other_task_targets_preserved(tmp_path, monkeypatch, conflict):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls))
    if conflict == 'unknown_output':
        req['output'].mkdir(); (req['output'] / 'keep').write_text('other task')
    elif conflict == 'foreign_work':
        req['work_dir'].mkdir(); (req['work_dir'] / 'keep').write_text('unknown owner')
    elif conflict == 'foreign_task':
        helper.prepare_reference(**req); calls.clear()
        req['environment']['BIOMIND_TASK_ID'] = 'different-task'
    else:
        req['output'].symlink_to(req['source'])
    before = {str(p): p.read_bytes() for p in req['output'].parent.rglob('*') if p.is_file()}
    with pytest.raises(helper.PreparationError): helper.prepare_reference(**req)
    assert not calls
    assert all(Path(p).read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize('when', ['before_rename', 'after_rename'])
def test_publish_interruption_revalidates_without_rebuilding(tmp_path, monkeypatch, when):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls))
    original = helper._publish_noreplace
    def interrupt(source, target):
        if when == 'after_rename': original(source, target)
        raise KeyboardInterrupt('simulated process death before Commit')
    monkeypatch.setattr(helper, '_publish_noreplace', interrupt)
    with pytest.raises(KeyboardInterrupt): helper.prepare_reference(**req)
    assert calls == ['bwa', 'fai', 'dictionary']
    req['environment']['BIOMIND_ATTEMPT_ID'] = 'attempt-2'; calls.clear()
    monkeypatch.setattr(helper, '_publish_noreplace', original)
    result = helper.prepare_reference(**req)
    assert not calls
    assert result['reused_published'] is (when == 'after_rename')
    assert result['status'] == 'published_uncommitted'


def test_noncooperating_publisher_cannot_be_overwritten(tmp_path, monkeypatch):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls))
    original = helper._publish_noreplace
    def race(source, target):
        target.mkdir(); (target / 'keep').write_text('other publisher')
        original(source, target)
    monkeypatch.setattr(helper, '_publish_noreplace', race)
    with pytest.raises(helper.PreparationError, match='REFERENCE_OUTPUT_CONFLICT'):
        helper.prepare_reference(**req)
    assert (req['output'] / 'keep').read_text() == 'other publisher'


def test_published_change_blocks_reentry(tmp_path, monkeypatch):
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls))
    helper.prepare_reference(**req); calls.clear()
    (req['output'] / 'reference.fa.sa').write_bytes(b'changed')
    with pytest.raises(Exception, match='REFERENCE_BWA_'):
        helper.prepare_reference(**req)
    assert not calls


def _formal_setup(root, req):
    metadata = yaml.safe_load((SKILL / 'SKILL.md').read_text().split('---', 2)[1])
    capability = deepcopy(next(row for row in metadata['generate']['execution_capabilities'] if row['operation'] == 'prepare_reference'))
    capability.update(schema_version=1, selected_skill_id='genomics/wgs-short-read-mapping')
    task_id = req['environment']['BIOMIND_TASK_ID']
    task = create_task_spec('Prepare isolated small reference bundle.', task_id=task_id, base_dir=root, project_root=root,
        structured_source={'input_files': [{'role': 'reference_fasta', 'path': str(req['source']), 'format': 'fasta'}],
            'expected_outputs': [{'role': 'reference_indexes', 'path': str(req['output']), 'format': 'directory',
                'object_kind': 'directory', 'artifact_kind': 'directory', 'validator': 'directory_exists'},
                {'role': 'reference_preparation_evidence', 'path': str(req['work_dir']), 'format': 'directory',
                 'object_kind': 'directory', 'artifact_kind': 'directory', 'validator': 'directory_exists'}]})
    step = dict(step_id='prepare', title='Prepare reference', action='execute', purpose='analysis', tool='bash',
        skill_ids=['genomics/wgs-short-read-mapping'], dependencies=[], inputs=['input.reference_fasta'],
        outputs=['output.reference_indexes', 'output.reference_preparation_evidence'], operation='prepare_reference',
        execution_capability_id=capability['capability_id'], workflow_stage_ids=[], batch_id='', risk_level='low',
        success_criteria=['Complete content-validated bundle with declared preparation evidence.'],
        produces_observations=capability['produces_observations'], explicit_side_effects=capability['explicit_side_effects'])
    plan = build_plan_state(task_id, json.dumps({'steps': [step], 'validation_checklist': [],
        'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}), strict=True)
    plan['steps'][0]['step_effect'] = canonical_step_effect(plan['steps'][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
                current_task={'task_spec': task, 'task_revision': task_spec_revision(task)})
    plan['task_state']['workflow_routing_schema_version'] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True,
                    step_effect_capability_registry={capability['capability_id']: capability})
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source='isolated infrastructure test authorization')
    save_plan_state(root, task_id, plan)
    return task_id, capability


@pytest.mark.skipif(os.environ.get('BIOMIND_REFERENCE_REAL_TOOLS') != '1', reason='Real installed tools require explicit isolated acceptance run')
def test_real_tools_production_runner_actual_skill_gate_and_commit(tmp_path):
    req = _request(tmp_path)
    prefix = Path('/public/home/liusiqi/anaconda3/envs/bio_wgs/bin')
    req['tools'] = {name: str(prefix / name) for name in ('bwa', 'samtools', 'picard', 'java')}
    root = req['source'].parent
    task_id, capability = _formal_setup(root, req)
    plan = load_plan_state(root, task_id); step = plan['steps'][0]
    resource = {'handle': 'genomics/wgs-short-read-mapping#scripts/prepare_reference.py',
                'canonical_path': str(SKILL / 'scripts/prepare_reference.py'),
                'invocation': resolve_script_invocation((SKILL / 'SKILL.md').read_text(),
                    'scripts/prepare_reference.py', SKILL / 'scripts/prepare_reference.py')}
    arguments = []
    for flag, value in [('source', req['source']), ('output', req['output']), ('work-dir', req['work_dir']),
            ('reference-ref', req['reference_ref']), ('artifact-ref', req['artifact_ref']),
            ('evidence-artifact-ref', req['evidence_artifact_ref']), *req['tools'].items()]:
        arguments += ['--' + flag, str(value)]
    argv = build_skill_script_argv(resource, arguments)
    code = 'set -euo pipefail\n' + shlex.join(argv) + '\n'
    action = {'action_id': 'action-1', 'step_id': 'prepare', 'type': 'execute', 'language': 'bash', 'plan_id': plan['plan_id'],
        'plan_version': plan['plan_version'], 'purpose': 'analysis', 'command_role': 'analysis_execution',
        'code': code, 'outputs': [str(req['output']), str(req['work_dir'])], 'expected_step_effect': canonical_step_effect(step)}
    validate_skill_script_invocations(action, [resource])
    record_provenance_event(plan, 'action_bound', {'action': action})
    attempt = record_provenance_event(plan, 'attempt_started', {'action_id': action['action_id'], 'step_id': 'prepare'})
    req['environment']['BIOMIND_ATTEMPT_ID'] = attempt['attempt_id']
    save_plan_state(root, task_id, plan)
    script = root / 'approved-infrastructure.sh'; script.write_text(code)
    env = req['environment']
    main = sys.modules.get('__main__')
    if hasattr(main, 'OFFLINE_ALLOWED_SCRIPT_PATHS'):
        main.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(script))
        main.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(script)] = env
    stdout, stderr, rc = asyncio.run(run_script_path(script, expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        interpreter='bash', timeout=60, profile=False, env=env, exit_status_path=root / 'runner-exit.json',
        managed_log_paths={'stdout': str(root / 'runner.stdout'), 'stderr': str(root / 'runner.stderr')}))
    assert rc == 0, (stdout, stderr, rc)
    effects = extract_structured_effect_evidence(stdout)
    execution = {'task_id': task_id, 'execution_id': 'execution-real-small-reference', 'step_id': 'prepare',
        'action_id': action['action_id'], 'attempt_id': attempt['attempt_id'], 'plan_id': plan['plan_id'],
        'plan_version': plan['plan_version'], 'command_role': 'analysis_execution', 'purpose': 'analysis',
        'returncode': rc, 'exit_code': rc, 'execution_status': 'succeeded', 'raw_stdout': stdout, 'raw_stderr': stderr,
        'declared_outputs': [str(req['output']), str(req['work_dir'])],
        'produced_artifacts': [str(req['output']), str(req['work_dir'])],
        'step_effect': canonical_step_effect(step), 'effect_evidence': effects}
    record_provenance_event(plan, 'execution_finished', {'attempt_id': attempt['attempt_id'], 'execution_record': execution})
    mark_step_verification_pending(plan, step_id='prepare', attempt_id=attempt['attempt_id'])
    save_plan_state(root, task_id, plan)
    result = helper.prepare_reference(**req)
    assert result['reused_published'] and not result['commands']
    state = {'task_id': task_id, 'workflow_routing_schema_version': 2, 'validation_level': 'scientific',
             'pending_action': action, 'execution_evidence': execution}
    verifier = TypedSupervisor(data_path=str(root), verifier=SupervisorNode(_Reviewer(), output_dir=str(root / 'task_outputs')).run)
    state.update(asyncio.run(verifier.run(state)))
    (root / 'real-verification.json').write_text(json.dumps(state, indent=2, default=str))
    assert state['verification_outcome']['type'] in {'verified', 'verified_with_warnings'}, state
    committed = asyncio.run(CommitStep(data_path=str(root)).run(state))
    assert committed['commit_step_outcome']['type'] == 'plan_complete', committed
    assert load_plan_state(root, task_id)['steps'][0]['status'] == 'passed'
    assert '%20' in result['expected_final_uri'] and '%25' in result['expected_final_uri'] and '%23' in result['expected_final_uri']
    receipt = {'scope': 'independent synthetic infrastructure validation, not WGS benchmark',
        'tools': req['tools'], 'capability': capability, 'execution': execution,
        'verification': state['verification_outcome'], 'commit': committed['commit_step_outcome'], 'reentry': result}
    (root / 'real-tool-acceptance.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False))


def test_scoped_live_lock_prevents_second_build(tmp_path, monkeypatch):
    import fcntl
    req = _request(tmp_path); calls = []
    monkeypatch.setattr(helper, '_command', _fake_command(calls, ('bwa', -9)))
    with pytest.raises(helper.PreparationError): helper.prepare_reference(**req)
    calls.clear()
    with (req['work_dir'] / '.publication.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(helper.PreparationError, match='REFERENCE_PREPARATION_ALREADY_RUNNING'):
            helper.prepare_reference(**req)
    assert not calls
