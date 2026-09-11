"""Offline launcher and timing helpers for the CLI production-wiring fixtures.

Run this file directly with --export. The tested process receives a synthetic
environment and the existing audited bootstrap injects inert configuration.
The launcher never imports BioMind. All real state access is confined to newly
created scratch roots by the process audit guards.
"""
from __future__ import annotations

import argparse
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

PYTHON = "/public/home/liusiqi/anaconda3/envs/biomind/bin/python"
DEPS = "/tmp/biomind-cli-deps"


class TimedWriter(io.StringIO):
    """Real text writer with monotonic write/flush receipts, never pixel claims."""

    def __init__(self):
        super().__init__()
        self.events = []

    def write(self, value):
        self.events.append({"event": "write", "at_ns": time.monotonic_ns(), "text": value})
        return super().write(value)

    def flush(self):
        self.events.append({"event": "flush", "at_ns": time.monotonic_ns()})
        return super().flush()


def launch(export_root: Path, results: Path, tests: list[str]) -> int:
    export_root = export_root.resolve()
    results = results.resolve()
    if not export_root.is_relative_to(Path("/tmp")) or not results.is_relative_to(Path("/tmp")):
        raise ValueError("Export and result directories must be below /tmp")
    if not (export_root / "export_manifest.json").is_file():
        raise ValueError("An explicit allowlisted export is required")
    manifest = json.loads((export_root / "export_manifest.json").read_text())
    drift = [name for name, expected in manifest["files"].items()
             if hashlib.sha256((export_root / name).read_bytes()).hexdigest() != expected]
    if drift:
        raise ValueError("Export changed after its manifest was written: " + ", ".join(drift))
    results.mkdir(parents=True, exist_ok=True)
    scratch = results / "scratch"
    scratch.mkdir(exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONPATH": DEPS,
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TMPDIR": str(scratch), "BIOMIND_WIKI_ENABLED": "0", "LOG_LEVEL": "CRITICAL",
        "BIOMIND_OFFLINE_RESULTS": str(results / "pytest-results.json"),
        "BIOMIND_GER_ARTIFACTS": str(results),
        "BIOMIND_CLI_PROGRESS_ARTIFACTS": str(results),
        "BIOMIND_EXEC_LOG_DIR": str(scratch / "executor-logs"),
    }
    command = [PYTHON, str(export_root / "tests" / "cli_model_progress_support.py"),
               "--guarded-child", str(export_root), *tests]
    (results / "invocation.json").write_text(json.dumps({"command": command, "environment": environment}, indent=2))
    with (results / "stdout.txt").open("w", encoding="utf-8") as log:
        process = subprocess.run(command, env=environment, cwd=export_root, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=180)
    print(json.dumps({"returncode": process.returncode, "results": str(results)}))
    return process.returncode


def guarded_child():
    # Only the synthetic environment supplied by launch is visible here.
    import os
    import runpy
    import types
    root = Path(sys.argv[2]).resolve()
    interpreter_root = Path(PYTHON).parents[1]
    results_root = Path(os.environ["BIOMIND_CLI_PROGRESS_ARTIFACTS"]).resolve()
    allowed_roots = (root, results_root, Path(os.environ["TMPDIR"]).resolve(), interpreter_root,
                     Path(DEPS), Path("/usr/lib"), Path("/usr/share"), Path("/usr/local/lib"))
    allowed_exact = {Path("/dev/null"), Path("/dev/urandom"), Path("/etc/localtime"),
                     Path("/etc/mime.types"), Path("/etc/ssl/certs/ca-certificates.crt")}

    def guard(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            raw = Path(os.fsdecode(args[0]))
            path = (Path.cwd() / raw).resolve() if not raw.is_absolute() else raw.resolve()
            if path.is_relative_to(Path("/proc")):
                # CPU/lease metrics may degrade to unknown; never inspect host
                # process tables merely to run a display integration fixture.
                raise PermissionError("CLI_OFFLINE_PROCESS_METRICS_UNAVAILABLE")
            if path.name.startswith(".env") or path.name in {"启动.sh", ".netrc", ".gitconfig"}:
                raise RuntimeError("CLI_OFFLINE_PROTECTED_READ")
            flags = args[2] or 0
            if flags & getattr(os, "O_DIRECTORY", 0) and any(base.is_relative_to(path) for base in allowed_roots):
                return  # Descriptor traversal/fsync of an allowed root's ancestors.
            if not (path in allowed_exact or any(path.is_relative_to(base) for base in allowed_roots)):
                raise RuntimeError("CLI_OFFLINE_READ_OUTSIDE_EXPORT: " + str(path))
        if event == "sqlite3.connect" and args[0] != ":memory:":
            if not Path(str(args[0])).resolve().is_relative_to(Path(os.environ["TMPDIR"]).resolve()):
                raise RuntimeError("CLI_OFFLINE_ORIGINAL_DATABASE_DENIED")

    sys.addaudithook(guard)
    token_stub = types.ModuleType("tiktoken")
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Offline fixture uses the existing character estimator")
    token_stub.get_encoding = unavailable
    token_stub.encoding_for_model = unavailable
    sys.modules["tiktoken"] = token_stub
    sys.argv = [str(root / "tests/offline_managed_bootstrap.py"), str(root), *sys.argv[3:]]
    runpy.run_path(sys.argv[0], run_name="__main__")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--guarded-child":
        guarded_child()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", dest="export_root", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    args, tests = parser.parse_known_args()
    raise SystemExit(launch(args.export_root, args.results, tests or ["-q", "tests/test_cli_model_progress_integration.py"]))


if __name__ == "__main__":
    main()
