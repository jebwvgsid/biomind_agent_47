"""Temporary-files-only checks for the command and shared-directory installer."""
from pathlib import Path
import importlib.util

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_user_launcher', ROOT/'scripts/install_user_launcher.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def fixture(tmp_path):
    repo, home = tmp_path/'源码 空格', tmp_path/'user'
    (repo/'bin').mkdir(parents=True)
    launcher = repo/'bin/biomind'
    launcher.write_text('#!/bin/sh\nexit 0\n')
    launcher.chmod(0o755)
    home.mkdir()
    return repo, home


def test_shared_directory_reuse_preserves_key_and_lock_location(tmp_path, monkeypatch):
    repo, home = fixture(tmp_path)
    old = repo/'integrations/pi_runtime/.biomind/pi-agent'
    old.mkdir(parents=True)
    key = old/'auth.json'
    key.write_text('synthetic sentinel: never open this file')
    metadata = key.stat()
    original_read = Path.read_bytes

    def read_without_keys(path):
        assert path.name != 'auth.json', 'installer must never read credentials'
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', read_without_keys)
    before = b'# user-owned settings\nexport CUSTOM_LAUNCHER_FIXTURE=1\n'
    (home/'.bashrc').write_bytes(before)
    result = installer.install(repo, home, home/'.pi/agent', apply=True)
    shared = home/'.pi/agent'
    assert shared.is_symlink() and shared.resolve() == old
    assert (shared/'auth.json').stat().st_ino == metadata.st_ino
    (shared/'auth.json.lock').mkdir()
    assert (old/'auth.json.lock').is_dir()
    assert (home/'.bashrc').read_bytes() == before + installer.PATH_BLOCK
    assert (home/'.local/bin/biomind').resolve() == repo/'bin/biomind'
    second = installer.install(repo, home, home/'.pi/agent', apply=True)
    assert not any(x['path_block_added'] for x in second['shell_changes'])
    assert (home/'.bashrc').read_bytes() == before + installer.PATH_BLOCK
    assert result['credential_contents_read'] is False


def test_existing_personal_directory_is_not_replaced(tmp_path):
    repo, home = fixture(tmp_path)
    shared = home/'.pi/agent'
    shared.mkdir(parents=True)
    old = repo/'integrations/pi_runtime/.biomind/pi-agent'
    old.mkdir(parents=True)
    marker = shared/'keep-settings.txt'
    marker.write_text('personal fixture')
    result = installer.install(repo, home, home/'.pi/agent', apply=True)
    assert not shared.is_symlink()
    assert marker.read_text() == 'personal fixture'
    assert not result['existing_agent_directory_reused']


def test_command_conflict_refuses_before_any_change(tmp_path):
    repo, home = fixture(tmp_path)
    command = home/'.local/bin/biomind'
    command.parent.mkdir(parents=True)
    command.write_text('existing user command')
    with pytest.raises(ValueError, match='existing biomind command'):
        installer.install(repo, home, home/'.pi/agent', apply=True)
    assert command.read_text() == 'existing user command'
    assert not (home/'.pi').exists()
    assert not (home/'.bashrc').exists()


def test_check_only_does_not_create_user_paths(tmp_path):
    repo, home = fixture(tmp_path)
    assert installer.install(repo, home, home/'.pi/agent', apply=False)['mode'] == 'CHECK_ONLY'
    assert list(home.iterdir()) == []
