"""Offline runtime acceptance bootstrap for an isolated candidate checkout.

Run this tool with env-i and an already installed test Python. It provides only
startup/dotenv import exports, keeps real LangGraph/LangChain, and forbids
external commands, network connections and reads from the original workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from pathlib import Path

CANDIDATE = Path(
    os.environ.get("BIOMIND_OFFLINE_CANDIDATE")
    or Path(__file__).resolve().parents[1]
).resolve()
if "03.毕设" in CANDIDATE.parts:
    raise RuntimeError("Run offline acceptance from an isolated candidate checkout")
sys.dont_write_bytecode = True
os.chdir(CANDIDATE)
sys.path[:] = [str(CANDIDATE), *[p for p in sys.path if "03.毕设" not in p]]
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["LOG_LEVEL"] = "CRITICAL"
os.environ["BIOMIND_WIKI_ENABLED"] = "0"
os.environ["BIOMIND_STEP_WORKER_BACKEND"] = "legacy"
os.environ["BIOMIND_DISABLE_DOTENV"] = "1"


def safety(event, args):
    if event in {
        "socket.connect", "subprocess.Popen", "os.system", "os.exec",
        "os.posix_spawn",
    }:
        raise AssertionError("Offline test prohibited external operation: " + event)
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        name = Path(path).name
        if name == ".env" or name.startswith(".env.") or name in {
            "启动.sh", "startup.py", ".agent_history",
        }:
            raise AssertionError("Offline test prohibited sensitive/startup read")
        if "/03.毕设/" in path:
            raise AssertionError("Offline candidate must not read original workspace")


sys.addaudithook(safety)
dotenv = types.ModuleType("dotenv")
dotenv.dotenv_values = lambda *_a, **_k: {}
dotenv.load_dotenv = lambda *_a, **_k: False
dotenv.find_dotenv = lambda *_a, **_k: ""
sys.modules["dotenv"] = dotenv
startup = types.ModuleType("config.startup")
startup.StartupCheck = type("StartupCheck", (), {})
startup.ReadlineConfig = type("ReadlineConfig", (), {})
sys.modules["config.startup"] = startup


def candidate_source_identity():
    # This checkout contains only the explicitly exported safe source files.
    # Never include startup files, environment files, logs or runtime evidence.
    return {
        str(path.relative_to(CANDIDATE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(CANDIDATE.rglob("*.py"))
        if path.name != "startup.py" and "__pycache__" not in path.parts
    }


if __name__ == "__main__":
    before = candidate_source_identity()
    print("OFFLINE_CANDIDATE_SOURCE_IDENTITY " + json.dumps({
        "candidate_root": str(CANDIDATE), "python_executable": sys.executable,
        "python_version": sys.version, "sha256": before,
    }, sort_keys=True), flush=True)
    # HEAD conftest supplies minimal stubs unless real modules are loaded first.
    import langchain_core.messages
    import langchain_core.callbacks
    import pytest
    code = pytest.main(sys.argv[1:] or ["tests/test_runtime_evidence_transport.py", "-q"])
    origins = {}
    for name, module in sorted(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if not source:
            continue
        path = Path(source).resolve()
        if path.is_relative_to(CANDIDATE) and path.suffix == ".py":
            origins[name] = {
                "path": str(path),
                "sha256": before.get(str(path.relative_to(CANDIDATE)), ""),
            }
    changed = candidate_source_identity() != before
    print("OFFLINE_IMPORTED_CANDIDATE_MODULES " + json.dumps(origins, sort_keys=True), flush=True)
    print("OFFLINE_CANDIDATE_UNCHANGED=" + str(not changed), flush=True)
    raise SystemExit(2 if changed else code)
