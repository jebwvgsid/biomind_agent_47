"""Small product entry with scoped nonsecret configuration bridging.

Pi is the default and sole installed engine after the authorized retirement.
Module import stays stdlib-only: dispatch precedes legacy imports, model clients
and database construction. Authorized .env projection lazily uses its parser.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable, Mapping, Sequence

from agent_core.pi_model_config import resolve_model_config


DEFAULT_ENGINE = "pi"
MINIMUM_NODE = (22, 19, 0)
REPOSITORY = Path(__file__).resolve().parents[1]
RUNTIME = REPOSITORY / "integrations" / "pi_runtime"


class PiEntryError(RuntimeError):
    """An explicit Pi request cannot start; never fall back to legacy."""


def _metadata_token(value: object) -> str | None:
    """A bounded display token, not a second version parser or authority."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9.+-]{1,128}", value) else None


def _version_metadata(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = json.loads(stream.read(65537))
    if not isinstance(value, dict):
        raise ValueError("Expected manifest object")
    return value


def _source_commit(repository: Path) -> str:
    # An exported installation can be inside an unrelated project repository.
    # Require its own marker before consulting Git, and verify the returned root.
    if not (repository / ".git").exists():
        return "unknown"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel", "--verify", "HEAD"],
            capture_output=True, text=True, check=True, timeout=1, env=environment,
        )
        lines = result.stdout.strip().splitlines()
        if (len(lines) == 2 and Path(lines[0]).resolve() == repository
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", lines[1])):
            return lines[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "unknown"


def product_version(runtime: Path | None = None) -> str:
    """Read declared identity offline; do not inspect worktrees or installed Pi."""
    runtime = (runtime if runtime is not None else RUNTIME).resolve()
    try:
        package = _version_metadata(runtime / "package.json")
        version = _metadata_token(package.get("version"))
        if package.get("name") != "biomind-pi-runtime" or version is None:
            raise ValueError("Invalid product manifest")
    except (OSError, ValueError):
        raise PiEntryError("Product version manifest is missing or invalid") from None
    dependencies = package.get("dependencies")
    declared = (_metadata_token(dependencies.get("@earendil-works/pi-coding-agent"))
                if isinstance(dependencies, dict) else None) or "unknown"
    expected = "unknown"
    try:
        patch = _version_metadata(runtime / "compat" / "pi-0.87.1-observer.patch.json")
        if patch.get("package") == "@earendil-works/pi-coding-agent" and patch.get("originalVersion") == declared:
            expected = _metadata_token(patch.get("runtimeLabel")) or "unknown"
    except (OSError, ValueError):
        pass
    return (f"BioMind {version}\n"
            f"Source: {_source_commit(runtime.parent.parent)}; worktree: not checked\n"
            f"Pi declared: {declared}\n"
            f"Pi patch expected: {expected}; verification: not checked")


def select_engine(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Remove just --engine, leaving legacy's argument parsing unchanged."""
    remaining: list[str] = []
    selected: str | None = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--":
            remaining.extend(argv[index:])
            break
        if value == "--engine" or value.startswith("--engine="):
            if selected is not None:
                raise PiEntryError("--engine may be specified only once")
            if value == "--engine":
                index += 1
                if index >= len(argv):
                    raise PiEntryError("--engine requires pi or legacy")
                selected = argv[index]
            else:
                selected = value.partition("=")[2]
            if selected not in {"pi", "legacy"}:
                raise PiEntryError("--engine requires pi or legacy")
        else:
            remaining.append(value)
        index += 1
    return selected or DEFAULT_ENGINE, remaining


def pi_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BioMind — Pi runtime",
        epilog="Shell: biomind project list/create/open; biomind --resume to choose history.")
    parser.add_argument("--version", action="store_true", help="Show declared product/source identity offline; no worktree or patch verification")
    parser.add_argument("command", nargs="?", choices=("interactive", "project"), default="interactive")
    parser.add_argument("project_action", nargs="?", choices=("list", "create", "open"))
    parser.add_argument("project_target", nargs="?", type=Path)
    parser.add_argument("--project-dir", type=Path, help="Explicit project root; otherwise keep launch cwd or the selected session cwd")
    parser.add_argument("--pi-mode", choices=("interactive", "json"), default="interactive")
    parser.add_argument("--pi-config", type=Path, help="Explicit non-secret Pi runtime settings; no dotenv loading")
    parser.add_argument("--pi-model-record", type=Path, help="Confirmed non-secret model selection shared with offline acceptance")
    parser.add_argument("--pi-node", type=Path, help="Explicit Node >=22.19.0 executable")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--session", help="Resume a Pi session by exact ID or JSONL path")
    sessions.add_argument("--resume", "-r", action="store_true", help="Choose a saved Pi session interactively")
    sessions.add_argument("--continue", dest="continue_session", action="store_true", help="Explicitly continue the most recent Pi session in this project")
    sessions.add_argument("--no-session", action="store_true", help="Use an explicit temporary Pi session")
    parser.add_argument("--session-dir", type=Path, help="Native Pi session directory override")
    parser.add_argument("--prompt", help="Initial prompt passed as one argv value")
    parser.add_argument("--pi-check", action="store_true", help="Check candidate runtime without starting a model or service")
    return parser


def node_executable(explicit: Path | None, environment: Mapping[str, str], runtime: Path) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
    elif environment.get("BIOMIND_PI_NODE"):
        candidate = Path(environment["BIOMIND_PI_NODE"]).expanduser().resolve()
    else:
        bundled = runtime / ".runtime" / "node" / "bin" / "node"
        candidate = bundled if bundled.is_file() else Path(shutil.which("node", path=environment.get("PATH")) or "/nonexistent/biomind-node")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise PiEntryError("Pi runtime unavailable: Node executable missing; install the pinned private runtime first")
    return candidate.resolve()


def build_pi_command(args: argparse.Namespace, *, environment: Mapping[str, str] | None = None,
                     runtime: Path = RUNTIME, version_runner: Callable = subprocess.run) -> tuple[list[str], Path]:
    environment = os.environ if environment is None else environment
    launch_cwd = Path.cwd().resolve()
    project_action = getattr(args, "project_action", None)
    project_target = getattr(args, "project_target", None)
    if args.command != "project" and (project_action or project_target):
        raise PiEntryError("Project actions require: biomind project list/create/open")
    if args.command == "project":
        if project_action in {"create", "open"} and project_target is None:
            raise PiEntryError(f"project {project_action} requires a directory")
        if project_action not in {"create", "open"} and project_target is not None:
            raise PiEntryError("Only project create/open accepts a directory")
    if getattr(args, "resume", False):
        if args.pi_mode != "interactive":
            raise PiEntryError("--resume requires --pi-mode interactive")
        if args.command == "project" and project_action in {"list", "create"}:
            raise PiEntryError("--resume cannot be used with project list/create")
    explicit_project = args.project_dir is not None or project_action == "open"
    project = (project_target if project_action == "open" else args.project_dir or launch_cwd).expanduser().resolve()
    if project_action == "open" and args.project_dir and project != args.project_dir.expanduser().resolve():
        raise PiEntryError("project open conflicts with --project-dir")
    if not project.is_dir():
        raise PiEntryError(f"Project directory does not exist: {project}")
    runtime = runtime.resolve()
    node = node_executable(args.pi_node, environment, runtime)
    # Version-only execution cannot initialize the SDK, credentials, or project.
    try:
        version = version_runner([str(node), "--version"], check=True, capture_output=True,
                                 text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise PiEntryError("Cannot determine the selected Node version") from error
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < MINIMUM_NODE:
        raise PiEntryError("Pi requires Node >=22.19.0; selected executable is unsupported")
    host = runtime / "dist" / "host.js"
    package = runtime / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
    if not host.is_file() or not package.is_file():
        raise PiEntryError("Pi runtime unavailable: built host or pinned coding-agent dependency missing; run the private build first")
    try:
        installed = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PiEntryError("Cannot verify the installed Pi package manifest") from error
    if installed.get("version") != "0.87.1":
        raise PiEntryError("Pi package mismatch: this candidate requires 0.87.1")
    command = [str(node), str(host), "--mode", args.pi_mode,
               "--launch-cwd", str(launch_cwd), "--entry-python", str(Path(sys.executable).resolve())]
    if explicit_project:
        command.extend(["--project-dir", str(project)])
    if project_action == "open":
        command.append("--project-open")
    lightweight = args.command == "project" and project_action != "open"
    if lightweight:
        command.extend(["--project-command", project_action or "menu"])
        if project_target is not None:
            command.extend(["--project-target", str(project_target.expanduser().absolute())])
    if args.pi_config:
        config = args.pi_config.expanduser().resolve()
        if not config.is_file():
            raise PiEntryError(f"Explicit Pi settings file does not exist: {config}")
        command.extend(["--config", str(config)])
    # The host resolves nonsecret model fields after final project/session/menu
    # selection. Project list/create never read project .env or model records.
    if (not lightweight or project_action is None) and (args.pi_model_record or environment.get("BIOMIND_PI_MODEL_RECORD") or not args.pi_config):
        command.append("--resolve-entry-model")
        record = args.pi_model_record or environment.get("BIOMIND_PI_MODEL_RECORD")
        if record:
            command.extend(["--model-record", str(Path(record).expanduser().absolute())])
    if getattr(args, "session_dir", None):
        command.extend(["--session-dir", str(args.session_dir.expanduser())])
    if getattr(args, "session", None):
        command.extend(["--session", args.session])
    if getattr(args, "resume", False):
        command.append("--resume")
    if getattr(args, "continue_session", False):
        command.append("--continue")
    if getattr(args, "no_session", False):
        command.append("--no-session")
    if args.prompt is not None:
        command.extend(["--prompt", args.prompt])
    return command, project


def launch_pi(argv: Sequence[str], *, execute: Callable = os.execv) -> int:
    args = pi_parser().parse_args(list(argv))
    try:
        if args.version:
            print(product_version())
            return 0
        command, project = build_pi_command(args)
        selection = (json.loads(command[command.index("--model-selection") + 1])
                     if "--model-selection" in command else None)
        if args.pi_check:
            print(json.dumps({"engine": "pi", "default_engine": DEFAULT_ENGINE,
                              "runtime_ready": True, "project_dir": str(project),
                              "node": command[0], "host": command[1],
                              "registry_path": (command[command.index("--registry-path") + 1]
                                                if "--registry-path" in command else None),
                              "provider_started": False, "model_selection": selection,
                              "model_selection_status": "RESOLVED" if selection else "NOT_RESOLVED_BY_ENTRY"}, ensure_ascii=False))
            return 0
        if selection and selection["provider"] == "deepseek":
            prepare_deepseek_runtime_credentials(os.environ)
        # Host receives the scientific cwd explicitly. No chdir into the
        # software installation tree and no shell quoting or shell execution.
        execute(command[0], command)
        return 0
    except (PiEntryError, OSError) as error:
        print(f"BioMind Pi: {error}", file=sys.stderr)
        return 2


def prepare_deepseek_runtime_credentials(environment) -> None:
    """Preserve legacy's existing environment-key precedence at exec only.

    Never called by --pi-check, model resolution, export or P10. This maps an
    already-loaded credential within the product process; it opens no file,
    returns/logs no value and does not change the parent shell environment.
    """
    for name in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        value = environment.get(name)
        if value:
            environment["DEEPSEEK_API_KEY"] = value
            return


def maybe_dispatch(argv: Sequence[str] | None = None) -> int:
    """Dispatch Pi only; an explicit retired engine never silently falls back."""
    original = list(sys.argv[1:] if argv is None else argv)
    try:
        engine, remaining = select_engine(original)
    except PiEntryError as error:
        print(f"BioMind: {error}", file=sys.stderr)
        return 2
    if engine == "legacy":
        print("BioMind: legacy 旧运行时已物理退役，必须先恢复退役前代码，不能只切换运行参数。"
              "恢复说明：BioMind_Pi_migration_implementation_20260926/RECOVERY_USER_MANAGED.md", file=sys.stderr)
        return 2
    return launch_pi(remaining)


if __name__ == "__main__":
    raise SystemExit(maybe_dispatch())
