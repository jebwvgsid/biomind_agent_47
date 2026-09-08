"""Runtime identity travels through the real provider and immutable trace seams.

Only the provider and counted I/O faults are substituted. Source exports,
TaskSpec/trace/object storage and every test artifact live in tmp_path or the
explicit /tmp candidate. No Git command is needed by these exported fixtures.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import marshal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_core.context_lineage import ContextLineageStore
from agent_core.provider_attempt import PROVIDER_CALLER_CANCELLED
from agent_core.runtime_identity import SOURCE_PATHS, DEPENDENCIES, capture_runtime_identity
from agent_core.state_contract_store import StateContractStore
from bio_agents import BioAnalystAgent
from tests.test_provider_attempt_reliability import _agent


class RecordedIdentityProvider:
    def __init__(self, root, task_id, mode="success"):
        self.root, self.task_id, self.mode = root, task_id, mode
        self.entered = asyncio.Event()
        self.requests, self.pre_dispatch_snapshots = [], []
        self.cancelled = 0

    async def ainvoke(self, messages, config=None):
        self.requests.append(deepcopy(list(messages)))
        snapshots = ContextLineageStore(self.root, self.task_id).list_snapshots()
        assert len(snapshots) == 1, snapshots
        assert snapshots[0]["status"] == "started"
        self.pre_dispatch_snapshots.append(deepcopy(snapshots[0]))
        self.entered.set()
        if self.mode == "error":
            raise ValueError("OFFLINE_RECORDED_PROVIDER_FAILURE")
        if self.mode == "cancel":
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
        return AIMessage(content="RECORDED_IDENTITY_SUCCESS", response_metadata={"finish_reason": "stop"})


def _fixture_agent(root, task_id, mode="success"):
    model = RecordedIdentityProvider(root, task_id, mode)
    agent = _agent(model, data_path=str(root), attempts=1)
    agent.llm_max_retries = 0
    return agent, model


def _assert_captured(root, task_id, snapshot):
    identity = snapshot["runtime_identity"]
    assert identity["status"] == "CAPTURED", identity
    assert identity["task_id"] == task_id
    assert identity["stage"] == "recovery"
    assert identity["revision"]["loaded_revision"] == "UNKNOWN"
    assert identity["object_ref"].startswith("object:sha256:")
    body = StateContractStore(str(root), task_id).read_object(
        identity["object_ref"], expected_type="runtime.identity")
    assert body["capture_policy"] == "explicit_implementation_sources_v1"
    assert body["loaded_digest_format"] == "marshal_v2"
    assert set(body["source_files"]) == set(SOURCE_PATHS)
    assert set(body["dependencies"]) == set(DEPENDENCIES)
    source = body["source_files"]["bio_agents.py"]
    assert hashlib.sha256(source["text"].encode()).hexdigest() == source["sha256"]
    assert "async def _ainvoke_llm(" in source["text"]
    assert body["loaded_modules"]["bio_agents"]["callables"]["BioAnalystAgent._ainvoke_llm"] == hashlib.sha256(
        marshal.dumps(BioAnalystAgent._ainvoke_llm.__code__, 2)).hexdigest()
    assert body["revision"] == identity["revision"]
    assert body["revision"]["loaded_revision"] == "UNKNOWN"
    return identity, body


@pytest.mark.parametrize("mode", ["success", "error", "cancel"])
def test_real_provider_trace_keeps_pre_dispatch_runtime_identity_for_every_exit(tmp_path, mode):
    async def scenario():
        task_id = "identity-" + mode
        agent, model = _fixture_agent(tmp_path, task_id, mode)
        wire = [HumanMessage(content="Identity metadata must never become a model instruction.")]
        forged = {"schema_version": 1, "status": "CAPTURED", "object_ref": "object:forged",
                  "source_identity": "CALLER_MUST_NOT_SIGN_RUNTIME_IDENTITY", "revision": {"loaded_revision": "forged"}}
        invocation = agent._ainvoke_llm(wire, stage="recovery", task_id=task_id,
            trace_metadata={"runtime_identity": forged})
        if mode == "cancel":
            task = asyncio.create_task(invocation)
            try:
                await asyncio.wait_for(model.entered.wait(), timeout=2)
                task.cancel("DETERMINISTIC_OFFLINE_CALLER_CANCEL")
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            assert model.cancelled == 1
        elif mode == "error":
            with pytest.raises(ValueError, match="OFFLINE_RECORDED_PROVIDER_FAILURE"):
                await invocation
        else:
            response = await invocation
            assert response.content == "RECORDED_IDENTITY_SUCCESS"
        assert len(model.requests) == 1
        assert model.requests[0] == wire
        assert "CALLER_MUST_NOT_SIGN_RUNTIME_IDENTITY" not in str(model.requests[0])
        snapshots = ContextLineageStore(tmp_path, task_id).list_snapshots()
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot["status"] == {"success": "completed", "error": "failed", "cancel": "cancelled"}[mode]
        identity, body = _assert_captured(tmp_path, task_id, snapshot)
        assert identity == model.pre_dispatch_snapshots[0]["runtime_identity"]
        assert identity != forged
        assert "forged" not in json.dumps(identity)
        if mode == "cancel":
            assert snapshot["annotations"]["provider_attempt"]["failure_type"] == PROVIDER_CALLER_CANCELLED
        request = json.loads(Path(snapshot["request_payload_ref"]).read_text())
        assert "runtime_identity" not in request
        assert request["messages"][0]["content"] == wire[0].content
        (tmp_path / "runtime-identity-provider-receipt.json").write_text(json.dumps({
            "mode": mode, "provider_calls": len(model.requests), "pre_dispatch_snapshot": model.pre_dispatch_snapshots[0],
            "final_snapshot": snapshot, "loaded_module": body["loaded_modules"]["bio_agents"],
        }, indent=2))
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["source_read", "object_store"])
def test_runtime_identity_capture_failure_preserves_actual_provider_continuity(tmp_path, monkeypatch, fault):
    async def scenario():
        task_id = "identity-unavailable-" + fault
        agent, model = _fixture_agent(tmp_path, task_id)
        hits = []
        if fault == "object_store":
            real_put = StateContractStore.put_object
            def fail_identity(self, kind, payload):
                if kind == "runtime.identity":
                    hits.append(kind)
                    raise PermissionError("COUNTED_RUNTIME_IDENTITY_STORAGE_FAULT")
                return real_put(self, kind, payload)
            monkeypatch.setattr(StateContractStore, "put_object", fail_identity)
        else:
            import agent_core.runtime_identity as module
            target = Path(module.__file__).resolve().parents[1] / "agent_core" / "inspection_readers.py"
            real_open = Path.open
            def fail_source(self, *args, **kwargs):
                if self == target:
                    hits.append(str(self))
                    raise PermissionError("COUNTED_RUNTIME_IDENTITY_SOURCE_FAULT")
                return real_open(self, *args, **kwargs)
            monkeypatch.setattr(Path, "open", fail_source)
        response = await agent._ainvoke_llm([HumanMessage(content="Optional capture failure must preserve this call.")],
                                           stage="recovery", task_id=task_id)
        assert response.content == "RECORDED_IDENTITY_SUCCESS"
        assert len(hits) == 1 and len(model.requests) == 1
        snapshot = ContextLineageStore(tmp_path, task_id).list_snapshots()[0]
        identity = snapshot["runtime_identity"]
        assert snapshot["status"] == "completed"
        assert identity == model.pre_dispatch_snapshots[0]["runtime_identity"]
        assert identity == {"schema_version": 1, "status": "UNAVAILABLE", "task_id": task_id,
                            "stage": "recovery", "error_code": "RUNTIME_IDENTITY_CAPTURE_FAILED",
                            "error_type": "PermissionError"}
        assert not identity.get("object_ref") and not identity.get("loaded_code_identity")
        (tmp_path / "runtime-identity-fault-receipt.json").write_text(json.dumps({
            "fault": fault, "hits": hits, "provider_calls": len(model.requests), "snapshot": snapshot,
        }, indent=2))
    asyncio.run(scenario())


def test_allowlist_capture_denies_sensitive_and_symlink_sources_and_separates_loaded_from_disk(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    assert not (root / ".git").exists()
    # These are inert sentinel files, never original configuration or inputs.
    sensitive = [root / ".env.api_keys", root / "启动.sh", root / "config" / "config.py"]
    # The shared guard forbids even opening startup/dotenv names; these two
    # remain absent. An actual inert config sentinel proves exclusion too.
    sensitive[2].parent.mkdir(parents=True, exist_ok=True)
    sensitive[2].write_text("NEVER_READ_OR_ARCHIVE_SENSITIVE_SENTINEL")
    for name in SOURCE_PATHS:
        assert "config" not in Path(name).parts
        assert not Path(name).name.startswith(".env")
        assert Path(name).name not in {"启动.sh", "startup.py"}
    disk_source = root / "bio_agents.py"
    disk_source.write_text("SYNTHETIC_DISK_REVISION = 'one'\n")
    (root / "supervisor.py").symlink_to(sensitive[2])
    outside = tmp_path / "not-approved-source"
    outside.mkdir()
    outside_file = outside / "executor.py"
    outside_file.write_text("NEVER_READ_OUTSIDE_SOURCE_SENTINEL")
    (root / "execution").symlink_to(outside, target_is_directory=True)
    manifest = root / "candidate_manifest.json"
    manifest.write_text(json.dumps({"head": "fixture-disk-base-revision", "mode": "synthetic-test-export"}))
    real_open, reads = Path.open, []
    denied = {p.resolve() for p in [*sensitive, outside_file]}
    def guarded_open(self, *args, **kwargs):
        if self.resolve() in denied:
            raise AssertionError("A sensitive or escaped source was opened")
        if self.is_relative_to(root):
            reads.append(str(self.relative_to(root)))
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    first = capture_runtime_identity(data_path=str(tmp_path / "state"), task_id="identity-source",
        stage="recovery", skill_versions={"fixture/selected": "sha256:selected-skill"}, root=root)
    first_body = StateContractStore(str(tmp_path / "state"), "identity-source").read_object(
        first["object_ref"], expected_type="runtime.identity")
    assert first_body["source_files"]["supervisor.py"]["status"] == "UNAVAILABLE"
    assert first_body["source_files"]["execution/executor.py"]["status"] == "UNAVAILABLE"
    assert "NEVER_READ_OR_ARCHIVE_SENSITIVE_SENTINEL" not in json.dumps(first_body)
    assert "NEVER_READ_OUTSIDE_SOURCE_SENTINEL" not in json.dumps(first_body)
    assert set(reads) <= {"bio_agents.py", "candidate_manifest.json"}
    assert first["revision"]["candidate_base_revision"] == "fixture-disk-base-revision"
    assert first["revision"]["loaded_revision"] == "UNKNOWN"
    assert first_body["loaded_modules"]["bio_agents"]["location"] != str(disk_source)
    # Changing disk does not relabel the already-loaded callable or first receipt.
    disk_source.write_text("SYNTHETIC_DISK_REVISION = 'two'\n")
    second = capture_runtime_identity(data_path=str(tmp_path / "state"), task_id="identity-source",
        stage="recovery", skill_versions={"fixture/selected": "sha256:selected-skill"}, root=root)
    assert first["source_identity"] != second["source_identity"]
    assert first["loaded_code_identity"] == second["loaded_code_identity"]
    assert first["object_ref"] != second["object_ref"]
    assert second["revision"]["loaded_revision"] == "UNKNOWN"
    saved_first = StateContractStore(str(tmp_path / "state"), "identity-source").read_object(
        first["object_ref"], expected_type="runtime.identity")
    assert saved_first == first_body
    assert saved_first["source_files"]["bio_agents.py"]["text"] == "SYNTHETIC_DISK_REVISION = 'one'\n"


def test_loaded_digest_is_stable_while_existing_code_constants_gain_live_references(tmp_path, monkeypatch):
    # Default marshal >=3 can change flags when a running function holds this
    # unchanged constant. Production identity must use its declared stable v2.
    import bio_agents
    namespace = {"__name__": "bio_agents"}
    exec("def _identity_constant_fixture():\n    return (1, 2, 'fixture constant containing spaces')\n", namespace)
    fn = namespace["_identity_constant_fixture"]
    monkeypatch.setattr(bio_agents, "_identity_constant_fixture", fn, raising=False)
    source = tmp_path / "sources"
    source.mkdir()
    (source / "bio_agents.py").write_text("# Inert disk source distinct from loaded executable identity.\n")
    first = capture_runtime_identity(data_path=str(tmp_path / "state"), task_id="identity-constants",
                                     stage="recovery", root=source)
    held_constant = fn()
    second = capture_runtime_identity(data_path=str(tmp_path / "state"), task_id="identity-constants",
                                      stage="recovery", root=source)
    assert held_constant is fn.__code__.co_consts[1]
    store = StateContractStore(str(tmp_path / "state"), "identity-constants")
    a = store.read_object(first["object_ref"], expected_type="runtime.identity")
    b = store.read_object(second["object_ref"], expected_type="runtime.identity")
    assert a["loaded_digest_format"] == b["loaded_digest_format"] == "marshal_v2"
    before = a["loaded_modules"]["bio_agents"]["callables"]["_identity_constant_fixture"]
    after = b["loaded_modules"]["bio_agents"]["callables"]["_identity_constant_fixture"]
    assert before == after == hashlib.sha256(marshal.dumps(fn.__code__, 2)).hexdigest()
    assert first["loaded_code_identity"] == second["loaded_code_identity"]
