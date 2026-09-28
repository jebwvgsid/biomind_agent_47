"""Product-entry tests; no legacy Agent or real model/config is imported."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
from types import SimpleNamespace

import pytest

from agent_core import pi_entry


ROOT = Path(__file__).resolve().parents[1]


def runtime_fixture(tmp_path, version="0.87.1"):
    runtime = tmp_path / "产品 安装"
    (runtime / "dist").mkdir(parents=True)
    (runtime / "dist" / "host.js").write_text("// synthetic command target only\n")
    package = runtime / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": version}))
    project = tmp_path / "科学 项目"
    project.mkdir()
    args = pi_entry.pi_parser().parse_args(["--project-dir", str(project), "--pi-node", sys.executable,
        "--pi-mode", "json", "--prompt", "中文 ; $(do-not-run) /tmp/input with spaces.tsv"])
    return runtime, project, args


def test_default_pi_and_explicit_legacy_restore_error(capsys, monkeypatch):
    argv = ["interactive", "--project-dir", "/项目 有空格"]
    assert pi_entry.select_engine(argv) == ("pi", argv)
    assert pi_entry.select_engine(["--engine=legacy", *argv]) == ("legacy", argv)
    monkeypatch.setattr(pi_entry, "launch_pi", lambda args: pytest.fail("legacy must not start Pi"))
    assert pi_entry.maybe_dispatch(["--engine", "legacy", *argv]) == 2
    assert "必须先恢复退役前代码" in capsys.readouterr().err
    assert "bio_agents" not in sys.modules
    assert "config.startup" not in sys.modules


@pytest.mark.parametrize("argv", [["--engine"], ["--engine=nope"], ["--engine=pi", "--engine=legacy"]])
def test_ambiguous_or_missing_engine_fails_closed(argv):
    assert pi_entry.maybe_dispatch(argv) == 2


def test_real_product_pi_failure_happens_before_legacy_config_import(tmp_path):
    site = tmp_path / "sitecustomize.py"
    site.write_text("import sys\nclass Block:\n def find_spec(self, name, *args):\n  if name == 'config' or name.startswith('config.'):\n   raise RuntimeError('LEGACY_CONFIG_IMPORT_FORBIDDEN')\nsys.meta_path.insert(0, Block())\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path),
           "BIOMIND_PI_NODE": str(tmp_path / "missing-node"), "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run([sys.executable, str(ROOT / "bio_agents.py"), "--engine", "pi",
        "--project-dir", str(tmp_path), "--pi-check"], env=env, cwd=tmp_path,
        text=True, capture_output=True, timeout=15)
    assert completed.returncode == 2
    assert "Node executable missing" in completed.stderr
    assert "LEGACY_CONFIG_IMPORT_FORBIDDEN" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_product_entry_only_imports_the_pi_dispatcher():
    tree = ast.parse((ROOT / "bio_agents.py").read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert imports == ["__future__", "agent_core.pi_entry"]
    assert not any(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
    entry = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    assert isinstance(entry.body[0], ast.Return)
    assert entry.body[0].value.func.id == "maybe_dispatch"


def test_pi_command_keeps_absolute_product_and_scientific_paths_and_argv(tmp_path):
    runtime, project, args = runtime_fixture(tmp_path)
    command, selected = pi_entry.build_pi_command(args, runtime=runtime,
        version_runner=lambda *a, **kw: SimpleNamespace(stdout="v22.19.0\n"))
    assert selected == project.resolve()
    assert command[:2] == [str(Path(sys.executable).resolve()), str(runtime / "dist" / "host.js")]
    assert command[-2:] == ["--prompt", "中文 ; $(do-not-run) /tmp/input with spaces.tsv"]
    assert command[command.index("--project-dir") + 1] == str(project)
    assert "tsx" not in " ".join(command)


def test_old_node_and_wrong_pi_package_are_rejected(tmp_path):
    runtime, _, args = runtime_fixture(tmp_path, version="0.84.4")
    with pytest.raises(pi_entry.PiEntryError, match="Node >=22.19.0"):
        pi_entry.build_pi_command(args, runtime=runtime,
            version_runner=lambda *a, **kw: SimpleNamespace(stdout="v20.19.0"))
    with pytest.raises(pi_entry.PiEntryError, match="requires 0.87.1"):
        pi_entry.build_pi_command(args, runtime=runtime,
            version_runner=lambda *a, **kw: SimpleNamespace(stdout="v22.19.0"))


def test_explicit_node_selection_does_not_fall_back(tmp_path):
    with pytest.raises(pi_entry.PiEntryError, match="Node executable missing"):
        pi_entry.node_executable(tmp_path / "missing", {"PATH": os.environ.get("PATH", "")}, tmp_path)


def version_install(tmp_path, version="7.8.9-dev.2"):
    root = tmp_path / "安装 空格"
    runtime = root / "integrations/pi_runtime"
    (runtime / "compat").mkdir(parents=True)
    (runtime / "package.json").write_text(json.dumps({"name": "biomind-pi-runtime", "version": version,
        "dependencies": {"@earendil-works/pi-coding-agent": "0.87.1"}}))
    (runtime / "compat/pi-0.87.1-observer.patch.json").write_text(json.dumps({
        "package": "@earendil-works/pi-coding-agent", "originalVersion": "0.87.1",
        "runtimeLabel": "0.87.1+biomind-observer.2"}))
    return root, runtime


def forbidden_version_side_effect(*args, **kwargs):
    raise AssertionError("version must not initialize providers, Node, domain or scan Git")


def test_version_uses_installation_without_initialization_or_project_state(tmp_path, monkeypatch, capsys):
    root, runtime = version_install(tmp_path)
    project = tmp_path / "scientific-project"; project.mkdir()
    (project / "package.json").write_text('{"version":"wrong-project-version"}')
    (tmp_path / ".git").mkdir()  # Export lies inside someone else's repository.
    monkeypatch.chdir(project); monkeypatch.setattr(pi_entry, "RUNTIME", runtime)
    for name in ("build_pi_command", "resolve_model_config", "node_executable", "prepare_deepseek_runtime_credentials"):
        monkeypatch.setattr(pi_entry, name, forbidden_version_side_effect)
    monkeypatch.setattr(pi_entry.subprocess, "run", forbidden_version_side_effect)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    assert pi_entry.maybe_dispatch(["--version", "--project-dir", str(project / "not-created")]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == ("BioMind 7.8.9-dev.2\nSource: unknown; worktree: not checked\n"
        "Pi declared: 0.87.1\nPi patch expected: 0.87.1+biomind-observer.2; verification: not checked\n")
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


def test_source_query_is_bounded_root_checked_metadata_only(tmp_path, monkeypatch):
    root, runtime = version_install(tmp_path); (root / ".git").mkdir()
    calls = []; sha = "a" * 40
    monkeypatch.setenv("GIT_DIR", "/unrelated-repository")
    def git(command, **kwargs):
        calls.append((command, kwargs))
        assert command == ["git", "-C", str(root), "rev-parse", "--show-toplevel", "--verify", "HEAD"]
        assert kwargs['timeout'] == 1 and kwargs['check'] is True
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        assert 'GIT_DIR' not in kwargs['env']
        return SimpleNamespace(stdout=f"{root}\n{sha}\n")
    monkeypatch.setattr(pi_entry.subprocess, "run", git)
    assert f"Source: {sha}; worktree: not checked" in pi_entry.product_version(runtime)
    assert len(calls) == 1
    assert os.environ['GIT_DIR'] == '/unrelated-repository'


@pytest.mark.parametrize("failure", ["missing-git", "timeout", "git-error", "wrong-root", "invalid-sha"])
def test_source_failures_are_unknown_without_failing_product_version(tmp_path, monkeypatch, failure):
    root, runtime = version_install(tmp_path); (root / ".git").touch()  # Worktree-style marker is allowed.
    def git(command, **kwargs):
        if failure == "missing-git": raise FileNotFoundError()
        if failure == "timeout": raise subprocess.TimeoutExpired(command, 1)
        if failure == "git-error": raise subprocess.CalledProcessError(128, command)
        return SimpleNamespace(stdout=f"{root.parent if failure == 'wrong-root' else root}\n"
            + ("not-a-commit" if failure == "invalid-sha" else "a" * 40))
    monkeypatch.setattr(pi_entry.subprocess, "run", git)
    assert "Source: unknown; worktree: not checked" in pi_entry.product_version(runtime)


@pytest.mark.parametrize("content", [None, "{broken", "[]", '{}',
    '{"name":"wrong","version":"1.0.0"}', '{"name":"biomind-pi-runtime","version":null}',
    '{"name":"biomind-pi-runtime","version":""}', '{"name":"biomind-pi-runtime","version":"secret\\nvalue"}'])
def test_invalid_product_manifest_fails_without_fabricating_version(tmp_path, monkeypatch, capsys, content):
    _, runtime = version_install(tmp_path); manifest = runtime / "package.json"
    if content is None: manifest.unlink()
    else: manifest.write_text(content)
    monkeypatch.setattr(pi_entry, "RUNTIME", runtime)
    monkeypatch.setattr(pi_entry.subprocess, "run", forbidden_version_side_effect)
    assert pi_entry.maybe_dispatch(["--version"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "manifest is missing or invalid" in out.err
    assert "secret" not in out.err


@pytest.mark.parametrize("content", [None, "[]", "{broken", '{"runtimeLabel":"unknown-provider"}'])
def test_missing_or_invalid_patch_metadata_is_unknown_not_verified(tmp_path, content):
    _, runtime = version_install(tmp_path); patch = runtime / "compat/pi-0.87.1-observer.patch.json"
    if content is None: patch.unlink()
    else: patch.write_text(content)
    out = pi_entry.product_version(runtime)
    assert "BioMind 7.8.9-dev.2" in out
    assert "Pi patch expected: unknown; verification: not checked" in out


def test_ordinary_parser_and_help_do_not_query_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pi_entry, "product_version", forbidden_version_side_effect)
    monkeypatch.setattr(pi_entry.subprocess, "run", forbidden_version_side_effect)
    args = pi_entry.pi_parser().parse_args(["--prompt=--version"])
    assert args.prompt == "--version" and args.version is False
    with pytest.raises(SystemExit) as result:
        pi_entry.maybe_dispatch(["--help"])
    assert result.value.code == 0 and "--version" in capsys.readouterr().out


def test_exported_entry_runs_without_site_packages_secrets_or_processes(tmp_path):
    root, runtime = version_install(tmp_path)
    (root / 'agent_core').mkdir()
    for name in ['bio_agents.py', 'agent_core/__init__.py', 'agent_core/pi_entry.py', 'agent_core/pi_model_config.py']:
        shutil.copy2(ROOT / name, root / name)
    project = tmp_path / 'project'; project.mkdir()
    (project / '.env').write_text('SYNTHETIC_MUST_NOT_OPEN=1')
    # Guard the actual entry in a fresh -S process, not pytest's dependency stubs.
    script = '''import sys, runpy, pathlib
root = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(root))
class Block:
 def find_spec(self, name, *args):
  if name.split('.')[0] in {'dotenv','config','langchain_core','domain_adapter','python_service'}:
   raise AssertionError('forbidden initialization')
sys.meta_path.insert(0, Block())
def guard(event, args):
 if event == 'open' and isinstance(args[0], (str, bytes)):
  name = pathlib.Path(args[0]).name
  if name.startswith('.env') or name in {'auth.json','credentials.json'}:
   raise AssertionError('secret file read')
 if event in {'subprocess.Popen','socket.connect','socket.getaddrinfo','os.mkdir'}:
  raise AssertionError('forbidden side effect')
sys.addaudithook(guard)
sys.argv = [str(root/'bio_agents.py'), '--version']
runpy.run_path(str(root/'bio_agents.py'), run_name='__main__')
'''
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = subprocess.run([sys.executable, '-S', '-B', '-c', script, str(root)], cwd=project,
        env={'PATH':'/nonexistent', 'HOME':str(tmp_path), 'LC_ALL':'C.UTF-8'}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "BioMind 7.8.9-dev.2" in result.stdout and "Source: unknown" in result.stdout
    assert result.stderr == ''
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize("override", [None, "8.0.0-dev.3"])
def test_candidate_archive_version_default_and_explicit_override(tmp_path, override):
    root, runtime = version_install(tmp_path)
    (root / 'scripts').mkdir(); script = root / 'scripts/generate_pi_package_candidates.py'
    shutil.copy2(ROOT / 'scripts/generate_pi_package_candidates.py', script)
    skill = root / 'skills/fixture/SKILL.md'; skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: fixture\ndescription: synthetic only\n---\nNo scientific execution.\n')
    output = tmp_path / 'candidate'
    command = [sys.executable, '-S', '-B', str(script), '--only', 'skills', '--output', str(output)]
    if override is not None: command += ['--version', override]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output/'biomind-bioinformatics-skills/package.json').read_text())
    assert manifest['version'] == (override or '7.8.9-dev.2') and manifest['private'] is True
    assert json.loads((runtime/'package.json').read_text())['version'] == '7.8.9-dev.2'


def test_product_and_lock_root_versions_agree():
    runtime = ROOT / 'integrations/pi_runtime'
    package = json.loads((runtime/'package.json').read_text())
    lock = json.loads((runtime/'package-lock.json').read_text())
    assert package['version'] == lock['version'] == lock['packages']['']['version']
    assert package['dependencies'] == lock['packages']['']['dependencies']
