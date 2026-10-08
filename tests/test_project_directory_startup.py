"""Only temporary HOME/project fixtures; no model/domain creation in preflight."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_core.project_directory import ProjectDirectoryError, validate_project_directory
from agent_core.command_execution import CommandInspectionError, _directory


def test_shared_directory_policy_canonicalizes_home_alias_and_keeps_cwd_first(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    project = home / 'project'; project.mkdir()
    alias = tmp_path / 'home-alias'; alias.symlink_to(home)
    monkeypatch.setenv('HOME', str(home))
    for target in (home, alias, Path('/tmp')):
        with pytest.raises(ProjectDirectoryError, match='shared/home root'):
            validate_project_directory(target)
        with pytest.raises(CommandInspectionError, match='shared/home root') as error:
            _directory(target)
        assert error.value.code == 'PROJECT_DIRECTORY_INVALID'
    assert validate_project_directory(project) == project
    assert _directory(project) == project


def test_metadata_validation_is_structured_and_never_imports_domain_resources(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    project = home / 'project'; project.mkdir()
    code = '''import json,sys
from agent_core.pi_launch_metadata import main
result=main(['validate','--project',sys.argv[1]])
assert not any(name in sys.modules for name in ['agent_core.pi_model_config','agent_core.command_execution','memory.wiki_memory'])
raise SystemExit(result)
'''
    env = {**os.environ, 'HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
    root = Path(__file__).resolve().parents[1]
    for target, valid in ((home, False), (project, True), (home / 'missing', False)):
        result = subprocess.run([sys.executable, '-c', code, str(target)], cwd=root, env=env,
                                text=True, capture_output=True, check=True)
        data = json.loads(result.stdout)
        assert data['valid'] == valid
        assert not result.stderr
    assert list(home.iterdir()) == [project]
