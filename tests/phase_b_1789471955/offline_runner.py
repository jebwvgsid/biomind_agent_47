"""Explicit offline test bootstrap. Run with the existing Python interpreter."""
from pathlib import Path
import builtins
import io
import os
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["SKIP_STARTUP_CHECK"] = "true"
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["BIOMIND_OFFLINE"] = "1"


def forbidden_path(path):
    if isinstance(path, (str, bytes, os.PathLike)):
        name = os.fsdecode(path)
        return Path(name).name.startswith(".env") or Path(name).name == "启动.sh"
    return False


def guard_open(original):
    def guarded(path, *args, **kwargs):
        if forbidden_path(path):
            raise AssertionError("OFFLINE_CREDENTIAL_OR_STARTUP_READ_FORBIDDEN")
        return original(path, *args, **kwargs)
    return guarded


builtins.open = guard_open(builtins.open)
io.open = guard_open(io.open)
os.open = guard_open(os.open)


def deny_network(*args, **kwargs):
    raise AssertionError("OFFLINE_NETWORK_FORBIDDEN")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network
# Prevent the optional automatic dotenv loader, before any BioMind import.
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
dotenv.dotenv_values = lambda *a, **k: {}
import dotenv.main
dotenv.main.load_dotenv = dotenv.load_dotenv
dotenv.main.dotenv_values = dotenv.dotenv_values

if "--baseline" in sys.argv:
    sys.argv.remove("--baseline")
    import json, subprocess, importlib.abc, importlib.util
    # These targets were clean at actual BASE. Other preexisting dirty modules
    # stay byte-identical in the shared workspace, as recorded by protection.json.
    before = {}
    for relative in ("agent_core/evidence_projection.py", "agent_core/semantic_completion.py",
                     "agent_core/workflow/commit_step.py", "agent_core/workflow/context_compaction.py",
                     "agent_core/workflow/inspection_evidence.py", "agent_core/workflow/recovery_agent.py",
                     "agent_core/workflow/recovery_context.py",
                     "agent_core/workflow/recovery_node.py", "agent_core/workflow/recovery_runtime.py",
                     "agent_core/workflow/recovery_tools.py", "agent_core/workflow/verifier.py",
                     "prompts/context_compaction.py", "bio_agents.py", "supervisor.py", "agent_core/plan_tracker.py"):
        before[relative] = subprocess.check_output(["git", "show", "de7037f7a522e5a7350c07b6349b80965b8f49ed:" + relative], cwd=ROOT, text=True)
    sources = {path[:-3].replace("/", "."): source for path, source in before.items()}
    class BaselineLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            return importlib.util.spec_from_loader(fullname, self) if fullname in sources else None
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            module.__file__ = str(ROOT / (module.__name__.replace(".", "/") + ".py"))
            exec(compile(sources[module.__name__], module.__file__, "exec"), module.__dict__)
    sys.meta_path.insert(0, BaselineLoader())


import pytest
raise SystemExit(pytest.main(["--noconftest", "-q", "-p", "no:cacheprovider", *sys.argv[1:]]))
