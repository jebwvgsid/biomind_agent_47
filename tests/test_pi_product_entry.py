"""Product-entry tests; no legacy Agent or real model/config is imported."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
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
