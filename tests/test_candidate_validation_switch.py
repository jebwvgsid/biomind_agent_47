"""Trial mode reaches resource prelaunch without changing the script body."""

import asyncio
from pathlib import Path

import pytest

from agent_core.candidate_validation import (
    CANDIDATE_VALIDATION_ENV,
    candidate_validation_enabled,
)
from agent_core.controller_audit import adapt_script_block_to_action, parse_executable_script_block
from agent_core.public_response import project_generate_response
from agent_core.skill_access import SkillAccessError
from agent_core.skill_resource_binding import bind_action_skill_resources, validate_action_skill_resources
from execution.script_validation import check_executable_action
from test_skill_script_invocation import HANDLE, STEP_ID, _fixture, _receipt


def test_trial_is_default_and_can_be_reenabled(monkeypatch):
    monkeypatch.delenv(CANDIDATE_VALIDATION_ENV, raising=False)
    assert candidate_validation_enabled() is False
    monkeypatch.setenv(CANDIDATE_VALIDATION_ENV, "1")
    assert candidate_validation_enabled() is True
    monkeypatch.setenv(CANDIDATE_VALIDATION_ENV, "0")
    assert candidate_validation_enabled() is False


def test_trial_decode_bind_and_prelaunch_keep_exact_code(tmp_path, monkeypatch):
    monkeypatch.delenv(CANDIDATE_VALIDATION_ENV, raising=False)
    service, access, _, _ = _fixture(tmp_path)
    resource = _receipt(service, access, tmp_path)
    body = ('\nSOURCE="input.fastq"\nif [ -r "$SOURCE" ]; then\n'
            + resource['invocation']['shell_prefix']
            + ' --path "$SOURCE" --observation-id validation_observation\nfi\n')
    raw = f'候选说明。\n<shell language="bash" skill_resources="{HANDLE}"><![CDATA[{body}]]></shell>'
    projection = project_generate_response(raw, script_protocol=True,
        strict=candidate_validation_enabled())
    assert not projection.error
    action = adapt_script_block_to_action(parse_executable_script_block(
        projection.payload, strict=candidate_validation_enabled()))
    assert action['code'] == body
    assert check_executable_action(action)['check_status'] == 'PASS'
    bound = bind_action_skill_resources(action, service, access, step_id=STEP_ID,
        cwd=tmp_path, enforce_invocation=candidate_validation_enabled())
    assert bound['code'] == body
    assert bound['argv'] == action['argv']
    validate_action_skill_resources(bound, service, access, step_id=STEP_ID,
        cwd=tmp_path, enforce_invocation=candidate_validation_enabled())

    # Re-enabling the same check restores its previous rejection behavior.
    monkeypatch.setenv(CANDIDATE_VALIDATION_ENV, '1')
    with pytest.raises(SkillAccessError, match='SKILL_INVOCATION_UNVERIFIABLE'):
        validate_action_skill_resources(bound, service, access, step_id=STEP_ID,
            cwd=tmp_path, enforce_invocation=candidate_validation_enabled())


def test_trial_still_detects_resource_changed_after_binding(tmp_path, monkeypatch):
    monkeypatch.setenv(CANDIDATE_VALIDATION_ENV, '0')
    service, access, _, _ = _fixture(tmp_path)
    action = {'type': 'execute', 'language': 'bash', 'code': 'echo candidate',
              'skill_resource_refs': [HANDLE]}
    bound = bind_action_skill_resources(action, service, access, step_id=STEP_ID,
        cwd=tmp_path, enforce_invocation=candidate_validation_enabled())
    Path(bound['formal_skill_resources'][0]['canonical_path']).write_text('changed after approval\n')
    with pytest.raises(SkillAccessError):
        validate_action_skill_resources(bound, service, access, step_id=STEP_ID,
            cwd=tmp_path, enforce_invocation=candidate_validation_enabled())


