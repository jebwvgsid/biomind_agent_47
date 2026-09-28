"""Later exact user preparation changes policy, never Installer command approval."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from agent_core.task_spec import create_task_spec, merge_compiled_constraints, semantic_install_policy
from agent_core.installation_policy import grant_scoped_installation_preparation


def fixture(tmp_path):
    spec = merge_compiled_constraints(create_task_spec('Do not install missing dependencies.'), [{
        'constraint_id': 'original-install-block', 'semantic_key': 'forbidden_behavior.runtime_install',
        'operator': 'forbid', 'value': True, 'source': 'user_explicit', 'source_type': 'user_explicit',
        'source_ref': 'U1', 'source_id': 'U1', 'source_text': 'Do not install missing dependencies.'}])
    request = {'request_id': 'request-current', 'tool': 'cooler', 'target_env': 'science',
               'target_prefix': str(tmp_path / 'science')}
    event = {'action': 'prepare_installation', 'question_id': 'question-current',
             'answer_turn_ref': 'real-user-U2', 'request_id': 'request-current'}
    return spec, request, event


def test_later_exact_user_preparation_supersedes_only_matching_old_block(tmp_path):
    spec, request, event = fixture(tmp_path)
    assert not semantic_install_policy(spec, dependency_key='cooler')['install_allowed']
    updated = grant_scoped_installation_preparation(spec, request, event)
    restored = json.loads(json.dumps(updated))
    outcome = semantic_install_policy(restored, dependency_key='cooler', target_env='science', target_prefix=request['target_prefix'])
    assert outcome['install_allowed'] and outcome['proposal_preparation_only']
    assert not outcome['constraint']['value']['command_execution_authorized']
    assert outcome['constraint']['source_ref'] == 'real-user-U2'
    assert not semantic_install_policy(restored, dependency_key='different', target_env='science', target_prefix=request['target_prefix'])['install_allowed']
    assert not semantic_install_policy(restored, dependency_key='cooler', target_env='other', target_prefix=request['target_prefix'])['install_allowed']
    assert not semantic_install_policy(restored, dependency_key='cooler')['install_allowed']
    assert grant_scoped_installation_preparation(restored, request, event) == restored


@pytest.mark.parametrize('answer', ['continue', '', 'install_as_skill_recommends'])
def test_vague_or_skill_answer_cannot_replace_blocked_policy(tmp_path, answer):
    spec, request, event = fixture(tmp_path)
    with pytest.raises(ValueError, match='INSTALLATION_POLICY_EXPLICIT_SCOPE_REQUIRED'):
        grant_scoped_installation_preparation(spec, request, {**event, 'action': answer})
    assert not semantic_install_policy(spec, dependency_key='cooler')['install_allowed']


def test_model_or_skill_decision_has_no_policy_authority(tmp_path):
    spec, request, event = fixture(tmp_path)
    updated = grant_scoped_installation_preparation(spec, request, event)
    updated['confirmed_decisions'][0]['source_type'] = 'skill_default'
    assert not semantic_install_policy(updated, dependency_key='cooler', target_env='science', target_prefix=request['target_prefix'])['install_allowed']


def test_new_user_block_is_not_overruled_by_old_preparation(tmp_path):
    spec, request, event = fixture(tmp_path)
    updated = grant_scoped_installation_preparation(spec, request, event)
    later = merge_compiled_constraints(updated, [{
        'constraint_id': 'new-user-block', 'semantic_key': 'failure_policy.cooler_missing',
        'operator': 'on_condition', 'value': 'block', 'condition': 'cooler_missing',
        'source': 'user_explicit', 'source_type': 'user_explicit', 'source_ref': 'U3',
        'source_id': 'U3', 'source_text': 'Now block; do not prepare cooler installation.'}])
    assert not semantic_install_policy(later, dependency_key='cooler', target_env='science', target_prefix=request['target_prefix'])['install_allowed']




