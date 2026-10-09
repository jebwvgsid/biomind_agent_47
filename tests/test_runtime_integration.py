"""Isolated fixtures and injected errno only: no real HOME, SSH, Orca or relay."""
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agent_core import pi_entry
from agent_core import runtime_integration as integration

ROOT = Path(__file__).resolve().parents[1]


def fake_node(path, version='22.22.0'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/python3\nimport os,sys\n'
                    'assert not any(k in os.environ for k in ("NODE_OPTIONS","OPENAI_API_KEY","SECRET_TOKEN"))\n'
                    'print(' + repr('v' + version) + ' if sys.argv[1] == "--version" else "10.9.4")\n')
    path.chmod(0o755)
    return path


def fake_npm(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/env node\n// fixture handled by fake node\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "安装 中文 ' space"
    runtime = repo / 'integrations/pi_runtime'
    node = fake_node(runtime / '.runtime/node/bin/node')
    npm = fake_npm(node.parent / 'npm')
    home = tmp_path / 'home'; home.mkdir()
    work = tmp_path / 'not-a-project'; work.mkdir()
    tools = tmp_path / 'isolated-path'; tools.mkdir()
    for name in ('readlink', 'dirname', 'python3'):
        (tools / name).symlink_to(shutil.which(name))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(tools))
    monkeypatch.setenv('NODE_OPTIONS', '--require /must-not-run')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-not-inherited')
    monkeypatch.chdir(work)
    env = {'PATH': str(tools), 'NODE_OPTIONS': '--require /must-not-run', 'SECRET_TOKEN': 'synthetic'}
    bin_dir = home / '.local/bin'
    refresh = lambda: integration.diagnose(runtime, home, bin_dir, env)
    return runtime, home, bin_dir, env, refresh


def test_doctor_private_available_path_missing_no_persistent_writes(setup):
    runtime, home, bin_dir, env, refresh = setup
    report = refresh()
    assert report['internal_node']['compatible']
    assert report['npm']['via_node']['stdout'] == '10.9.4'
    assert report['npm']['paired_path']['status'] == 'OK'
    assert report['npm']['current_path']['status'] == 'FAILED'
    assert 'node' in report['npm']['current_path']['stderr']
    assert report['current_path']['node']['status'] == 'MISSING'
    assert report['status'] == 'READY_TO_APPLY'
    assert report['orca_probe'] == report['relay'] == report['normal_ssh'] == 'NOT_RUN'
    assert report['bin_directory']['writability'] == 'NOT_VERIFIED'
    assert list(home.iterdir()) == []
    assert not (runtime / 'dist').exists()


def test_missing_nonexecutable_and_invalid_pi_do_not_block_diagnostic(setup):
    runtime, home, bin_dir, env, refresh = setup
    node = runtime / '.runtime/node/bin/node'
    node.chmod(0o644)
    assert refresh()['internal_node']['probe']['status'] == 'MISSING_OR_UNEXECUTABLE'
    node.unlink()
    assert refresh()['status'] == 'RUNTIME_UNAVAILABLE'
    assert list(home.iterdir()) == []


def test_compatible_external_split_layout_keeps_private_selection(setup, tmp_path):
    runtime, home, bin_dir, env, refresh = setup
    node = fake_node(tmp_path / 'external-node/node', '18.20.0')
    npm = fake_npm(tmp_path / 'external-npm/npm')
    env['PATH'] = f'{node.parent}:{npm.parent}:/usr/bin:/bin'
    report = refresh()
    assert report['status'] == 'NO_CHANGES_NEEDED'
    assert report['current_path']['compatible']
    assert report['internal_node']['probe']['version'] == [22, 22, 0]
    assert integration.apply_links(report, refresh)['status'] == 'NO_CHANGES_NEEDED'
    assert not bin_dir.exists()
    env['BIOMIND_PI_NODE'] = str(node)
    report = refresh()
    assert not report['internal_node']['compatible'] and report['current_path']['compatible']


def test_lower_priority_external_manager_is_not_shadowed(setup, tmp_path):
    runtime, home, bin_dir, env, refresh = setup
    old = fake_node(tmp_path / 'manager/node', '16.0.0')
    env['PATH'] = f'{bin_dir}:{old.parent}:/usr/bin:/bin'
    report = refresh()
    assert report['status'] == 'SHADOW_RISK'
    assert integration.apply_links(report, refresh)['status'] == 'REFUSED'
    assert not bin_dir.exists()


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink', 'dangling'])
def test_all_destination_conflicts_stop_before_first_write(setup, kind, tmp_path):
    _, home, bin_dir, _, refresh = setup
    bin_dir.mkdir(parents=True)
    conflict = bin_dir / 'npm'
    if kind == 'file': conflict.write_text('keep')
    if kind == 'directory': conflict.mkdir()
    if kind == 'symlink': conflict.symlink_to(sys.executable)
    if kind == 'dangling': conflict.symlink_to(tmp_path / 'missing')
    before = integration.identity(conflict)
    report = refresh()
    assert report['status'] == 'CONFLICT'
    assert integration.apply_links(report, refresh)['status'] == 'REFUSED'
    assert integration.identity(conflict) == before
    assert not (bin_dir / 'node').exists()
    assert not (home / '.local/state').exists()


def apply(setup):
    refresh = setup[-1]
    return integration.apply_links(refresh(), refresh)


def test_apply_two_links_receipt_repeat_and_undo(setup):
    runtime, home, bin_dir, env, refresh = setup
    result = apply(setup)
    assert result['status'] == 'APPLIED', result
    assert {p.name for p in bin_dir.iterdir()} == {'node', 'npm'}
    assert len(result['created']) == 2
    env['PATH'] = str(bin_dir) + ':/usr/bin:/bin'
    report = refresh()
    assert report['status'] == 'NO_CHANGES_NEEDED'
    assert apply(setup)['status'] == 'NO_CHANGES_NEEDED'
    removed = integration.undo_links(report, integration.undo_plan(report))
    assert removed['status'] == 'UNDONE'
    assert list(bin_dir.iterdir()) == []
    assert (runtime / '.runtime/node/bin/node').is_file()
    assert refresh()['receipt']['phase'] == 'UNDONE'
    assert apply(setup)['status'] == 'APPLIED'


def test_same_target_unowned_link_is_never_adopted(setup):
    runtime, _, bin_dir, _, refresh = setup
    bin_dir.mkdir(parents=True)
    (bin_dir / 'node').symlink_to(runtime / '.runtime/node/bin/node')
    result = apply(setup)
    assert result['status'] == 'APPLIED'
    assert [Path(i['destination']).name for i in result['created']] == ['npm']
    report = refresh()
    assert integration.undo_links(report, integration.undo_plan(report))['status'] == 'UNDONE'
    assert (bin_dir / 'node').is_symlink()


@pytest.mark.parametrize('code', [errno.EROFS, errno.EACCES, errno.ENOSPC, errno.EDQUOT])
def test_first_link_errno_is_preserved_and_second_not_attempted(setup, monkeypatch, code):
    _, _, bin_dir, _, refresh = setup
    original = os.symlink
    attempts = []
    def symlink(src, dest, *args, **kwargs):
        if Path(dest).parent == bin_dir:
            attempts.append(str(dest))
            raise OSError(code, 'injected', str(dest))
        return original(src, dest, *args, **kwargs)
    monkeypatch.setattr(os, 'symlink', symlink)
    result = apply(setup)
    assert result['status'] == 'FAILED'
    assert result['error']['errno'] == code and result['error']['phase'] == 'symlink'
    assert attempts == [str(bin_dir / 'node')]
    assert result['residual'] == []
    assert not os.path.lexists(bin_dir / 'node')


def test_race_creation_no_overwrite_and_first_link_rolls_back(setup, monkeypatch):
    _, _, bin_dir, _, _ = setup
    original = os.symlink
    def symlink(src, dest, *args, **kwargs):
        if Path(dest) == bin_dir / 'npm':
            Path(dest).write_text('concurrent user file')
        return original(src, dest, *args, **kwargs)
    monkeypatch.setattr(os, 'symlink', symlink)
    result = apply(setup)
    assert result['error']['errno'] == errno.EEXIST
    assert result['status'] == 'FAILED'
    assert (bin_dir / 'npm').read_text() == 'concurrent user file'
    assert not os.path.lexists(bin_dir / 'node')


def test_second_link_failure_and_failed_rollback_reports_partial(setup, monkeypatch):
    _, _, bin_dir, _, refresh = setup
    original_link, original_unlink = os.symlink, Path.unlink
    def symlink(src, dest, *args, **kwargs):
        if Path(dest) == bin_dir / 'npm': raise OSError(errno.EACCES, 'injected')
        return original_link(src, dest, *args, **kwargs)
    def unlink(path, *args, **kwargs):
        if path == bin_dir / 'node': raise OSError(errno.EROFS, 'injected')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'symlink', symlink)
    monkeypatch.setattr(Path, 'unlink', unlink)
    result = apply(setup)
    assert result['status'] == 'PARTIAL'
    assert result['residual'] == [str(bin_dir / 'node')]
    assert result['rollback_errors'][0]['errno'] == errno.EROFS
    assert refresh()['status'] == 'RECEIPT_NEEDS_ATTENTION'


@pytest.mark.parametrize('replacement', ['file', 'other-link', 'same-link'])
def test_undo_never_deletes_replacements_even_same_target(setup, replacement):
    runtime, _, bin_dir, _, refresh = setup
    assert apply(setup)['status'] == 'APPLIED'
    path = bin_dir / 'node'
    # Rename preserves the old inode, ensuring a new link cannot reuse it.
    path.rename(bin_dir / 'saved-old-link')
    if replacement == 'file': path.write_text('user file')
    else: path.symlink_to(runtime / '.runtime/node/bin/node' if replacement == 'same-link' else sys.executable)
    before = integration.identity(path)
    report = refresh()
    result = integration.undo_links(report, integration.undo_plan(report))
    assert result['status'] == 'PARTIAL'
    assert integration.identity(path) == before
    assert not os.path.lexists(bin_dir / 'npm')


@pytest.mark.parametrize('when', ['first', 'after-first-link'])
def test_receipt_failure_stops_or_rolls_back(setup, monkeypatch, when):
    _, _, bin_dir, _, refresh = setup
    original = integration.write_receipt
    def write(path, data):
        if when == 'first' or (data['phase'] == 'APPLYING' and data['created']):
            raise OSError(errno.ENOSPC, 'injected', str(path))
        return original(path, data)
    monkeypatch.setattr(integration, 'write_receipt', write)
    result = apply(setup)
    assert result['status'] == 'FAILED'
    assert result['error']['phase'] == 'receipt'
    assert not os.path.lexists(bin_dir / 'node')
    assert not os.path.lexists(bin_dir / 'npm')


def test_interruption_pending_receipt_only_reports_then_safe_undo(setup, monkeypatch):
    _, _, bin_dir, _, refresh = setup
    original = os.symlink
    def interrupted(src, dest, *args, **kwargs):
        if Path(dest) == bin_dir / 'npm': raise KeyboardInterrupt()
        return original(src, dest, *args, **kwargs)
    monkeypatch.setattr(os, 'symlink', interrupted)
    with pytest.raises(KeyboardInterrupt): apply(setup)
    report = refresh()
    assert report['status'] == 'RECEIPT_NEEDS_ATTENTION'
    assert (bin_dir / 'node').is_symlink()
    assert report['receipt']['phase'] == 'APPLYING'
    assert integration.undo_links(report, integration.undo_plan(report))['status'] == 'UNDONE'


def test_runtime_removed_detected_and_owned_dangling_links_can_be_undone(setup):
    runtime, _, bin_dir, _, refresh = setup
    assert apply(setup)['status'] == 'APPLIED'
    (runtime / '.runtime/node/bin/node').unlink()
    assert refresh()['status'] in {'CONFLICT', 'RUNTIME_UNAVAILABLE'}
    report = refresh()
    assert integration.undo_links(report, integration.undo_plan(report))['status'] == 'UNDONE'
    assert list(bin_dir.iterdir()) == []


def test_timeout_and_nonzero_are_not_missing(setup, monkeypatch):
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(subprocess, 'run', timeout)
    assert integration.probe(['/fake/node', '--version'], '/usr/bin')['status'] == 'TIMEOUT'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 9, '', 'broken loader'))
    result = integration.probe(['/fake/node', '--version'], '/usr/bin')
    assert result['status'] == 'FAILED' and result['rc'] == 9 and result['stderr'] == 'broken loader'


def test_probe_env_is_allowlisted_with_empty_config_and_temp_cwd(setup, monkeypatch):
    visited = []
    def run(argv, **kw):
        env = kw['env']; root = Path(env['HOME']); visited.append(root)
        assert kw['cwd'] == str(root) and root.parent == Path('/tmp')
        assert not {'NODE_OPTIONS', 'OPENAI_API_KEY', 'SECRET_TOKEN', 'LD_PRELOAD'} & env.keys()
        assert Path(env['NPM_CONFIG_USERCONFIG']).read_text() == ''
        assert Path(env['NPM_CONFIG_GLOBALCONFIG']).read_text() == ''
        assert kw['timeout'] == 10
        return subprocess.CompletedProcess(argv, 0, '10.9.4', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert integration.probe(['/fake/npm', '--version'], '/usr/bin')['status'] == 'OK'
    assert all(not path.exists() for path in visited)


@pytest.mark.parametrize('answer', ['', 'no', 'EOF', 'INTERRUPT', 'noninteractive'])
def test_confirmation_denial_creates_nothing(setup, monkeypatch, capsys, answer):
    runtime, home, _, _, _ = setup
    class Input(io.StringIO):
        def isatty(self): return answer != 'noninteractive'
    monkeypatch.setattr(sys, 'stdin', Input())
    def reply(*args):
        if answer == 'EOF': raise EOFError()
        if answer == 'INTERRUPT': raise KeyboardInterrupt()
        return answer
    monkeypatch.setattr('builtins.input', reply)
    assert integration.main(['integrate', 'orca', '--apply'], runtime=runtime) == 2
    assert 'REFUSED' in capsys.readouterr().out
    assert list(home.iterdir()) == []


def test_cli_early_dispatch_without_models_project_or_host(setup, monkeypatch, capsys):
    runtime, home, _, _, _ = setup
    monkeypatch.setattr(pi_entry, 'RUNTIME', runtime)
    def forbidden(*args, **kw): raise AssertionError('model/project/host must not be used')
    monkeypatch.setattr(pi_entry, 'build_pi_command', forbidden)
    monkeypatch.setattr(pi_entry, 'resolve_model_config', forbidden)
    assert pi_entry.maybe_dispatch(['doctor', '--integration', 'orca', '--json']) == 1
    assert json.loads(capsys.readouterr().out)['internal_node']['compatible']
    assert pi_entry.maybe_dispatch(['integrate', 'orca']) == 0
    assert list(home.iterdir()) == []


def test_export_quoted_paths_no_deployment_no_overwrite(setup, tmp_path):
    _, home, bin_dir, _, refresh = setup
    report = refresh()
    out = tmp_path / "handoff 中文 ' space"
    result = integration.export_handoff(out, report, None, bin_dir)
    assert result['status'] == 'EXPORTED'
    script = (out / 'apply.sh').read_text()
    import shlex
    words = shlex.split(script.split('exec ', 1)[1])
    assert words[0] == str(Path(report['install_root']) / 'bin/biomind')
    assert words[1:3] == ['integrate', 'orca'] and words[-1] == '--apply'
    assert 'ln ' not in script and 'yes' not in script
    assert list(home.iterdir()) == []
    with pytest.raises(FileExistsError): integration.export_handoff(out, report, None, bin_dir)


def test_source_changes_after_preview_requires_new_review(setup, tmp_path):
    _, _, bin_dir, env, refresh = setup
    report = refresh()
    env['BIOMIND_PI_NODE'] = str(fake_node(tmp_path / 'changed/node'))
    result = integration.apply_links(report, refresh)
    assert result['status'] == 'REFUSED'
    assert not bin_dir.exists()


def test_launcher_without_private_python_fallback_and_explicit_override(setup):
    _, home, _, source_env, _ = setup
    for override in (None, sys.executable):
        env = {'HOME': str(home), 'PATH': source_env['PATH'], 'BIOMIND_PI_NODE': str(home / 'missing'),
               'PYTHONDONTWRITEBYTECODE': '1'}
        if override: env['BIOMIND_PI_PYTHON'] = override
        result = subprocess.run([str(ROOT / 'bin/biomind'), 'doctor', '--integration', 'orca', '--json'],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 1, result.stderr
        assert json.loads(result.stdout)['status'] == 'RUNTIME_UNAVAILABLE'
    assert list(home.iterdir()) == []


@pytest.mark.parametrize('session', [[], ['--session', 'exact-id'], ['--continue'], ['--no-session']])
def test_original_pi_check_session_project_contract(setup, monkeypatch, capsys, session):
    runtime, home, _, _, _ = setup
    (runtime / 'dist').mkdir(); (runtime / 'dist/host.js').write_text('// fixture')
    pkg = runtime / 'node_modules/@earendil-works/pi-coding-agent/package.json'
    pkg.parent.mkdir(parents=True); pkg.write_text('{"version":"0.87.1"}')
    monkeypatch.setenv('BIOMIND_DISABLE_DOTENV', '1')
    # Existing version-only check inherits env by its old contract; use /bin/sh fixture.
    node = runtime / '.runtime/node/bin/node'
    node.write_text('#!/bin/sh\nprintf "v22.22.0\\n"\n')
    original = pi_entry.build_pi_command
    captured = []
    def build(args):
        command, project = original(args, runtime=runtime)
        captured.extend(command)
        return command, project
    monkeypatch.setattr(pi_entry, 'build_pi_command', build)
    assert pi_entry.maybe_dispatch(['interactive', '--project-dir', str(home), '--pi-node', str(node), '--pi-check', *session]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['runtime_ready'] and not result['provider_started']
    assert result['project_dir'] == str(home)
    assert 'model_selection_status' in result and result['engine'] == 'pi'
    if session: assert session[0] in captured


def test_changed_version_after_preview_is_not_silently_approved(setup):
    runtime, _, bin_dir, _, refresh = setup
    report = refresh()
    fake_node(runtime / '.runtime/node/bin/node', '24.0.0')
    result = integration.apply_links(report, refresh)
    assert result['status'] == 'REFUSED' and result['reason'] == 'RUNTIME_CHANGED_REVIEW_AGAIN'
    assert not bin_dir.exists()


def test_preview_never_claims_user_link_ownership_without_receipt(setup):
    runtime, _, bin_dir, _, refresh = setup
    bin_dir.mkdir(parents=True)
    for name in ('node', 'npm'):
        (bin_dir / name).symlink_to(runtime / '.runtime/node/bin' / name)
    report = refresh()
    assert all(link['action'] == 'EXISTING_UNOWNED' for link in report['links'])
    assert integration.undo_plan(report)['status'] == 'NO_OWNED_LINKS'


def test_pending_unrecorded_link_after_interruption_is_preserved(setup, monkeypatch):
    _, _, bin_dir, _, refresh = setup
    original = os.symlink
    def interrupted(src, dest, *args, **kwargs):
        result = original(src, dest, *args, **kwargs)
        if Path(dest) == bin_dir / 'node': raise KeyboardInterrupt()
        return result
    monkeypatch.setattr(os, 'symlink', interrupted)
    with pytest.raises(KeyboardInterrupt): apply(setup)
    report = refresh()
    assert report['receipt']['created'] == []
    assert report['receipt']['pending_destination'] == str(bin_dir / 'node')
    result = integration.undo_links(report, integration.undo_plan(report))
    assert result['status'] == 'PARTIAL' and (bin_dir / 'node').is_symlink()
    assert result['errors'][0]['error'] == 'UNRECORDED_IDENTITY_PRESERVED'


def test_undo_receipt_failure_reports_actual_removals(setup, monkeypatch):
    _, _, bin_dir, _, refresh = setup
    assert apply(setup)['status'] == 'APPLIED'
    report = refresh()
    def fail(*args): raise OSError(errno.ENOSPC, 'injected')
    monkeypatch.setattr(integration, 'write_receipt', fail)
    result = integration.undo_links(report, integration.undo_plan(report))
    assert result['status'] == 'FAILED' and len(result['removed']) == 2
    assert list(bin_dir.iterdir()) == []
    assert result['receipt_phase'] == 'CHECK_REQUIRED'


def test_atomic_receipt_update_failure_keeps_previous_valid_checkpoint(setup, monkeypatch):
    _, home, _, _, _ = setup
    path = home / 'test-receipt.json'
    integration.write_receipt(path, {'phase': 'APPLYING'})
    original = path.read_bytes()
    def fail(*args): raise OSError(errno.ENOSPC, 'injected')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError): integration.write_receipt(path, {'phase': 'APPLIED'})
    assert path.read_bytes() == original
    assert not list(home.glob('.orca-receipt-*'))


def test_ro_hint_export_works_and_checks_exact_target_submount(setup, monkeypatch, tmp_path):
    _, home, bin_dir, _, refresh = setup
    bin_dir.mkdir(parents=True)
    original = os.statvfs
    visited = []
    from types import SimpleNamespace
    def vfs(path):
        visited.append(Path(path))
        return SimpleNamespace(f_flag=0 if Path(path) == bin_dir else os.ST_RDONLY)
    monkeypatch.setattr(os, 'statvfs', vfs)
    report = refresh()
    assert bin_dir in visited
    assert not report['bin_directory']['readonly_hint']
    assert report['receipt_directory']['readonly_hint']
    assert report['status'] == 'READONLY_HINT'
    assert integration.export_handoff(tmp_path / 'export', report, None, bin_dir)['status'] == 'EXPORTED'
    assert list(bin_dir.iterdir()) == []


def test_different_homes_install_roots_custom_bin_are_independent(setup, tmp_path):
    runtime, _, _, _, refresh = setup
    assert apply(setup)['status'] == 'APPLIED'
    other_runtime = tmp_path / 'second install/integrations/pi_runtime'
    node = fake_node(other_runtime / '.runtime/node/bin/node', '24.0.0')
    fake_npm(node.parent / 'npm')
    home = tmp_path / 'second home'; home.mkdir()
    bin_dir = home / 'chosen user bin'
    other = lambda: integration.diagnose(other_runtime, home, bin_dir, {'PATH': '/usr/bin:/bin'})
    report = other()
    assert not report['bin_in_path']
    assert integration.apply_links(report, other)['status'] == 'APPLIED'
    assert other()['orca_probe'] == 'NOT_RUN'
    assert (bin_dir / 'node').resolve() == node
    assert refresh()['internal_node']['probe']['version'] == [22, 22, 0]


def test_no_credentials_files_read_fresh_python_without_site_packages(setup):
    runtime, home, _, source_env, _ = setup
    script = '''import sys, pathlib
sys.path.insert(0, sys.argv[1])
def guard(event, args):
 if event == 'open' and isinstance(args[0], (str, bytes)):
  name = pathlib.Path(args[0]).name
  if name.startswith('.env') or name in {'auth.json', 'credentials.json', '.npmrc', '启动.sh'}:
   raise AssertionError('forbidden configuration read')
 if event in {'socket.connect', 'socket.getaddrinfo'}:
  raise AssertionError('network forbidden')
sys.addaudithook(guard)
from agent_core import pi_entry
pi_entry.RUNTIME = pathlib.Path(sys.argv[2])
raise SystemExit(pi_entry.maybe_dispatch(['doctor','--integration','orca','--json']))
'''
    result = subprocess.run([sys.executable, '-S', '-B', '-c', script, str(ROOT), str(runtime)],
                            env={'HOME': str(home), 'PATH': source_env['PATH'], 'NODE_OPTIONS': '--require forbidden'},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)['internal_node']['compatible']
    assert result.stderr == '' and list(home.iterdir()) == []


def test_corrupt_symlink_receipt_blocks_without_following(setup, tmp_path):
    _, home, _, _, refresh = setup
    path = home / '.local/state/biomind/orca-links.json'
    path.parent.mkdir(parents=True)
    trap = tmp_path / 'do-not-read'; trap.write_text('not a receipt')
    path.symlink_to(trap)
    report = refresh()
    assert report['status'] == 'RECEIPT_NEEDS_ATTENTION'
    assert report['receipt_error']['errno'] == errno.ELOOP
    assert integration.undo_plan(report)['status'] == 'RECEIPT_NEEDS_ATTENTION'


def test_apply_confirmation_cli_and_noninteractive_shell_fixture(setup, monkeypatch, capsys):
    runtime, home, bin_dir, _, _ = setup
    class Input(io.StringIO):
        def isatty(self): return True
    monkeypatch.setattr(sys, 'stdin', Input())
    monkeypatch.setattr('builtins.input', lambda *args: 'yes')
    assert integration.main(['integrate', 'orca', '--apply'], runtime=runtime) == 0
    assert 'APPLIED' in capsys.readouterr().out
    # Isolated bash startup fixture. This is NOT a real SSH/client validation.
    (home / '.bashrc').write_text('export PATH="$HOME/.local/bin:$PATH"\ncase $- in *i*) ;; *) return ;; esac\n')
    command = '. "$HOME/.bashrc"; command -v node; command -v npm; node --version; npm --version'
    completed = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command],
                               env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [str(bin_dir / 'node'), str(bin_dir / 'npm'), 'v22.22.0', '10.9.4']


def test_relative_equivalent_links_and_repeat_outside_path_need_no_changes(setup):
    runtime, _, bin_dir, _, refresh = setup
    bin_dir.mkdir(parents=True)
    for name in ('node', 'npm'):
        (bin_dir / name).symlink_to(os.path.relpath(runtime / '.runtime/node/bin' / name, bin_dir))
    report = refresh()
    assert report['status'] == 'NO_CHANGES_NEEDED'
    assert not report['bin_in_path']
    assert integration.apply_links(report, refresh)['status'] == 'NO_CHANGES_NEEDED'
    assert report['receipt'] is None


def test_named_explicit_node_with_split_npm_can_be_wired(setup, tmp_path):
    runtime, home, bin_dir, env, _ = setup
    renamed = fake_node(tmp_path / 'custom/node-custom')
    # A PATH npm alone is intentionally protected by SHADOW_RISK; test selection
    # and actual paired version compatibility without adopting that manager.
    npm = fake_npm(tmp_path / 'tools/npm')
    env['PATH'] = f'{npm.parent}:/usr/bin:/bin'
    report = integration.diagnose(runtime, home, bin_dir, env, renamed)
    assert report['internal_node']['source'] == 'explicit'
    assert report['npm']['paired_path']['status'] == 'OK'
    assert report['npm']['paired_path']['node_binding'] == str(renamed)


def test_postcreation_verification_failure_rolls_back_both(setup, monkeypatch):
    _, _, bin_dir, _, _ = setup
    original = integration.probe
    def probe(argv, path, selected_node=None):
        if argv[0] == str(bin_dir / 'npm'):
            return {'status': 'FAILED', 'rc': 7, 'stderr': 'synthetic verification error'}
        return original(argv, path, selected_node)
    monkeypatch.setattr(integration, 'probe', probe)
    result = apply(setup)
    assert result['status'] == 'FAILED' and result['error']['phase'] == 'verify'
    assert result['verification']['npm']['rc'] == 7
    assert list(bin_dir.iterdir()) == []


def test_rollback_does_not_remove_concurrently_replaced_first_link(setup, monkeypatch):
    _, _, bin_dir, _, _ = setup
    original = os.symlink
    def symlink(src, dest, *args, **kwargs):
        if Path(dest) == bin_dir / 'npm':
            (bin_dir / 'node').rename(bin_dir / 'old')
            (bin_dir / 'node').write_text('user replacement')
            raise OSError(errno.EACCES, 'injected')
        return original(src, dest, *args, **kwargs)
    monkeypatch.setattr(os, 'symlink', symlink)
    result = apply(setup)
    assert result['status'] == 'PARTIAL'
    assert (bin_dir / 'node').read_text() == 'user replacement'
    assert result['rollback_errors'][0]['errno'] == errno.EEXIST


def test_export_uses_live_implementation_and_rechecks_new_version(setup, monkeypatch, tmp_path):
    runtime, home, bin_dir, source_env, refresh = setup
    repo = runtime.parent.parent
    (repo / 'bin').mkdir(); shutil.copy2(ROOT / 'bin/biomind', repo / 'bin/biomind')
    (repo / 'agent_core').mkdir()
    for name in ('__init__.py', 'pi_entry.py', 'pi_model_config.py', 'runtime_integration.py'):
        shutil.copy2(ROOT / 'agent_core' / name, repo / 'agent_core' / name)
    shutil.copy2(ROOT / 'bio_agents.py', repo / 'bio_agents.py')
    out = tmp_path / "export ' 中文"
    integration.export_handoff(out, refresh(), None, bin_dir)
    fake_node(runtime / '.runtime/node/bin/node', '24.0.0')
    result = subprocess.run(['/bin/sh', str(out / 'apply.sh')],
                            env={'HOME': str(home), 'PATH': source_env['PATH']},
                            input='', capture_output=True, text=True, timeout=20)
    assert result.returncode == 2 and 'REFUSED' in result.stdout
    assert 'v24.0.0' in result.stdout and 'v22.22.0' not in result.stdout
    assert list(home.iterdir()) == []


def test_real_private_runtime_pair_in_isolated_home(tmp_path):
    runtime = ROOT / 'integrations/pi_runtime'
    node = runtime / '.runtime/node/bin/node'
    if not node.is_file(): pytest.skip('optional installed private runtime is unavailable')
    home = tmp_path / 'isolated actual versions'; home.mkdir()
    empty_path = tmp_path / 'no-node-on-path'; empty_path.mkdir()
    report = integration.diagnose(runtime, home, home / '.local/bin', {'PATH': str(empty_path)})
    assert report['internal_node']['compatible'], report['internal_node']
    assert report['npm']['via_node']['status'] == 'OK'
    assert report['npm']['paired_path']['status'] == 'OK'
    assert report['npm']['current_path']['status'] == 'FAILED'
    assert list(home.iterdir()) == []
