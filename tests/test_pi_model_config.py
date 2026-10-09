"""Non-secret active selection bridge; no credentials or model calls."""
import json
from types import SimpleNamespace

import pytest

from agent_core.pi_model_config import launch_environment_config, resolve_model_config, validate_config, project_launch_environment
from agent_core import pi_entry


def test_legacy_active_environment_preserves_exact_model_and_endpoint_path():
    config = launch_environment_config({'LLM_DEFAULT_MODEL': 'deepseek-v4-flash', 'LLM_BASE_URL': 'https://api.deepseek.com/v1'})
    assert config['model'] == 'deepseek-v4-flash'
    assert config['endpoint_base_url'] == 'https://api.deepseek.com/v1'
    assert config['thinking'] == 'off'
    assert config['origin'] == 'active-launch-environment'
    assert config['confirmed_active'] is True


def test_only_named_nonsecret_variables_are_consulted():
    class Whitelist(dict):
        def get(self, key, default=None):
            assert key in {'BIOMIND_PI_MODEL_RECORD', 'LLM_DEFAULT_MODEL', 'LLM_BASE_URL', 'BIO_SOFTWARE_ROOT'}
            return super().get(key, default)
    assert resolve_model_config(environment=Whitelist())[0] is None


@pytest.mark.parametrize('endpoint', ['https://user:pass@api.deepseek.com', 'https://api.deepseek.com?token=x',
    'https://elsewhere.invalid/v1', 'https://api.deepseek.com/private', 'http://api.deepseek.com'])
def test_endpoint_never_silently_replaced_or_leaked(endpoint):
    with pytest.raises(ValueError) as error:
        launch_environment_config({'LLM_DEFAULT_MODEL': 'deepseek-v4-flash', 'LLM_BASE_URL': endpoint})
    assert endpoint not in str(error.value)


def test_partial_environment_is_not_a_current_config():
    with pytest.raises(ValueError, match='INCOMPLETE'):
        launch_environment_config({'LLM_DEFAULT_MODEL': 'deepseek-v4-flash'})


def test_v2_endpoint_is_bound_to_hostname_and_rejects_credentials():
    config = launch_environment_config({'LLM_DEFAULT_MODEL': 'deepseek-v4-flash', 'LLM_BASE_URL': 'https://api.deepseek.com/v1'})
    for endpoint in ['https://user:pass@api.deepseek.com', 'https://api.deepseek.com?token=x', 'https://wrong.invalid/v1']:
        with pytest.raises(ValueError, match='ENDPOINT'):
            validate_config({**config, 'endpoint_base_url': endpoint})


def test_original_product_command_defers_active_selection_until_final_project(tmp_path):
    runtime = tmp_path/'runtime'; (runtime/'dist').mkdir(parents=True)
    (runtime/'dist/host.js').write_text('// fixture')
    package = runtime/'node_modules/@earendil-works/pi-coding-agent/package.json'
    package.parent.mkdir(parents=True); package.write_text('{"version":"0.87.1"}')
    import sys
    args = pi_entry.pi_parser().parse_args(['--project-dir', str(tmp_path), '--pi-node', sys.executable])
    env = {'LLM_DEFAULT_MODEL': 'deepseek-v4-flash', 'LLM_BASE_URL': 'https://api.deepseek.com/v1'}
    command, _ = pi_entry.build_pi_command(args, environment=env, runtime=runtime,
        version_runner=lambda *args, **kwargs: SimpleNamespace(stdout='v22.22.0'))
    assert '--model-selection' not in command
    assert '--resolve-entry-model' in command
    assert command[command.index('--entry-python') + 1] == str(__import__('pathlib').Path(sys.executable).resolve())


def test_runtime_only_credential_mapping_preserves_legacy_precedence_without_output(capsys):
    env = {'QWEN_API_KEY': 'synthetic-qwen', 'OPENAI_API_KEY': 'synthetic-openai'}
    assert pi_entry.prepare_deepseek_runtime_credentials(env) is None
    assert env['DEEPSEEK_API_KEY'] == 'synthetic-qwen'
    assert capsys.readouterr().out == ''
    env['DEEPSEEK_API_KEY'] = 'synthetic-existing'
    pi_entry.prepare_deepseek_runtime_credentials(env)
    assert env['DEEPSEEK_API_KEY'] == 'synthetic-existing'


def test_original_dotenv_order_and_empty_value_semantics(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();project=tmp_path/'project';project.mkdir()
    (repo/'.env').write_text('LLM_DEFAULT_MODEL=repo-model\nLLM_BASE_URL=https://api.deepseek.com\nBIO_SOFTWARE_ROOT=/software/repo\n')
    (project/'.env').write_text('LLM_DEFAULT_MODEL="deepseek-v4-flash"\nLLM_BASE_URL=\nBIO_SOFTWARE_ROOT=/software/project\n')
    selection, source=resolve_model_config(environment={'LLM_DEFAULT_MODEL':'exported-model'},repo=repo,project=project)
    assert selection['model']=='deepseek-v4-flash'
    assert selection['endpoint_base_url']=='https://api.deepseek.com'
    assert source['software_root']=='/software/project'
    assert len(source['dotenv_sources'])==2


def test_dotenv_only_allowlisted_assignments_reach_parser(tmp_path, monkeypatch):
    import dotenv
    original=dotenv.dotenv_values
    def guarded(*args, **kwargs):
        text=kwargs['stream'].getvalue()
        assert 'synthetic-secret' not in text and 'API_KEY' not in text
        assert kwargs['interpolate'] is False
        return original(*args, **kwargs)
    monkeypatch.setattr(dotenv,'dotenv_values',guarded)
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=synthetic-secret\nLLM_DEFAULT_MODEL=deepseek-v4-flash\nLLM_BASE_URL=https://api.deepseek.com/v1\n')
    selection, _=resolve_model_config(environment={},repo=tmp_path,project=tmp_path)
    assert selection['endpoint_base_url']=='https://api.deepseek.com/v1'


def test_offline_disablement_prevents_all_dotenv_filesystem_operations(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):raise AssertionError('OFFLINE_DOTENV_FILE_ACCESS')
    monkeypatch.setattr('agent_core.pi_model_config.os.open',forbidden)
    values, source=project_launch_environment({'BIOMIND_DISABLE_DOTENV':'1'},repo=tmp_path,project=tmp_path)
    assert source['dotenv_projection']=='DISABLED'
    assert values['LLM_DEFAULT_MODEL']==''


def test_symlink_dotenv_rejected_without_opening_target(tmp_path, monkeypatch):
    target=tmp_path/'sensitive';target.write_text('must not be read')
    (tmp_path/'.env').symlink_to(target)
    def forbidden(*args, **kwargs):raise AssertionError('SYMLINK_TARGET_READ')
    monkeypatch.setattr('agent_core.pi_model_config.os.open',forbidden)
    with pytest.raises(ValueError,match='SYMLINK'):
        project_launch_environment({},repo=tmp_path,project=tmp_path)


def test_unselected_interpolation_cannot_read_credential_environment(tmp_path):
    (tmp_path/'.env').write_text('LLM_DEFAULT_MODEL=${DEEPSEEK_API_KEY}\n')
    with pytest.raises(ValueError,match='OUTSIDE_ALLOWLIST'):
        project_launch_environment({'DEEPSEEK_API_KEY':'synthetic-secret'},repo=tmp_path,project=tmp_path)
