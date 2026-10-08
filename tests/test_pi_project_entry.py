"""Launch argument boundary: synthetic paths only, no provider/config clients."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from agent_core import pi_entry


def command(tmp_path, argv, monkeypatch):
    runtime = tmp_path / 'runtime'
    (runtime / 'dist').mkdir(parents=True)
    (runtime / 'dist/host.js').write_text('// fixture')
    package = runtime / 'node_modules/@earendil-works/pi-coding-agent/package.json'
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"0.87.1"}')
    monkeypatch.chdir(tmp_path)
    args = pi_entry.pi_parser().parse_args([*argv, '--pi-node', sys.executable])
    return pi_entry.build_pi_command(args, environment={'BIOMIND_DISABLE_DOTENV': '1'}, runtime=runtime,
        version_runner=lambda *args, **kwargs: SimpleNamespace(stdout='v22.19.0'))


def test_implicit_project_is_distinct_and_session_uses_launch_directory(tmp_path, monkeypatch):
    argv, cwd = command(tmp_path, ['--session', './history/file.jsonl', '--session-dir', './sessions'], monkeypatch)
    assert '--project-dir' not in argv
    assert argv[argv.index('--launch-cwd') + 1] == str(tmp_path)
    assert argv[argv.index('--session') + 1] == './history/file.jsonl'
    assert argv[argv.index('--session-dir') + 1] == 'sessions'
    assert '--resolve-entry-model' in argv


@pytest.mark.parametrize('action', ['list', 'create'])
def test_directory_commands_do_not_resolve_models(tmp_path, monkeypatch, action):
    monkeypatch.setattr(pi_entry, 'resolve_model_config', lambda **kwargs: pytest.fail('model resolution before project'))
    args = ['project', action] + (["新 项目 ' $(literal)"] if action == 'create' else [])
    argv, cwd = command(tmp_path, args, monkeypatch)
    assert argv[argv.index('--project-command') + 1] == action
    assert '--resolve-entry-model' not in argv
    if action == 'create':
        assert argv[argv.index('--project-target') + 1] == str(tmp_path / "新 项目 ' $(literal)")


def test_project_open_explicit_root_and_model_record_survive(tmp_path, monkeypatch):
    target = tmp_path / 'B 空格'; target.mkdir()
    argv, cwd = command(tmp_path, ['project', 'open', str(target), '--pi-model-record', 'selection.json'], monkeypatch)
    assert argv[argv.index('--project-dir') + 1] == str(target)
    assert argv[argv.index('--model-record') + 1] == str(tmp_path / 'selection.json')
    assert '--project-command' not in argv
    assert '--project-open' in argv
    assert '--continue' not in argv


def test_conflicting_explicit_open_fails_before_node(tmp_path, monkeypatch):
    target = tmp_path / 'B'; target.mkdir()
    with pytest.raises(pi_entry.PiEntryError, match='conflicts'):
        command(tmp_path, ['project', 'open', str(target), '--project-dir', str(tmp_path)], monkeypatch)


@pytest.mark.parametrize('flag', ['--resume', '-r'])
@pytest.mark.parametrize('scope', ['interactive', 'project_menu', 'project_open'])
def test_resume_selector_is_forwarded_for_interactive_project_flows(tmp_path, monkeypatch, flag, scope):
    prefix = {'interactive': [], 'project_menu': ['project'],
              'project_open': ['project', 'open', str(tmp_path)]}[scope]
    argv, cwd = command(tmp_path, [*prefix, flag, '--session-dir', './history'], monkeypatch)
    assert argv.count('--resume') == 1
    assert '-r' not in argv
    assert argv[argv.index('--session-dir') + 1] == 'history'
    assert '--resolve-entry-model' in argv
    if scope == 'project_menu':
        assert argv[argv.index('--project-command') + 1] == 'menu'
    if scope == 'project_open':
        assert argv[argv.index('--project-dir') + 1] == str(tmp_path)


@pytest.mark.parametrize('other', [['--session', 'saved-id'], ['--continue'], ['--no-session']])
def test_resume_selector_conflicts_with_other_session_modes(other):
    with pytest.raises(SystemExit) as failure:
        pi_entry.pi_parser().parse_args(['--resume', *other])
    assert failure.value.code == 2


@pytest.mark.parametrize('argv, error', [
    (['--resume', '--pi-mode', 'json'], 'requires --pi-mode interactive'),
    (['project', 'list', '--resume'], 'cannot be used with project list/create'),
    (['project', 'create', 'new-project', '--resume'], 'cannot be used with project list/create'),
])
def test_resume_rejects_noninteractive_commands_before_node(tmp_path, monkeypatch, argv, error):
    monkeypatch.setattr(pi_entry, 'node_executable', lambda *args: pytest.fail('Node selected for invalid command'))
    with pytest.raises(pi_entry.PiEntryError, match=error):
        command(tmp_path, argv, monkeypatch)


def test_resume_selector_cannot_replace_exact_session_for_profile_migration(tmp_path, monkeypatch):
    with pytest.raises(pi_entry.PiEntryError, match='Migration requires --session and --execution-profile'):
        command(tmp_path, ['--resume', '--execution-profile', 'protected', '--migrate-execution-profile'], monkeypatch)
