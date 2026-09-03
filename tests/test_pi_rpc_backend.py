from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from agent_core.state import AgentState
from agent_core.workflow.pi_rpc import (
    PI_FAKE_MODEL_NAME,
    PI_FAKE_PROVIDER_NAME,
    PI_PACKAGE_NAME,
    PI_PINNED_VERSION,
    PI_RPC_BACKEND_NAME,
    PI_TERMINAL_PROTOCOL,
    PI_TERMINAL_TOOL_NAME,
    PiRpcConfig,
    PiRpcError,
    PiRpcProcess,
    PiRpcRunConfig,
    PiSessionBinding,
    PiStepWorkerBackend,
    PiTerminalEventCollector,
    PiTerminalOutcomePayload,
    canonical_step_request_json,
    decode_jsonl_record,
    encode_jsonl_record,
    step_request_sha256,
)
from agent_core.workflow.step_worker import (
    StepOutcome,
    StepRequest,
    build_step_request,
    get_step_worker_backend,
    plan_review_destination,
    resolve_step_worker_backend_name,
    route_step_worker_outcome,
)
from tests.test_step_worker_boundary import FORBIDDEN_ROUTE_FIELDS, _approved_plan_state


def _terminal_details(outcome_type: str = "completion_claimed") -> dict[str, Any]:
    blocker = None
    if outcome_type == "blocked":
        blocker = {"code": "PI_RPC_POC_BLOCKED"}
    elif outcome_type == "harness_error":
        blocker = {"code": "PI_RPC_POC_HARNESS_ERROR"}
    return {
        "protocol": PI_TERMINAL_PROTOCOL,
        "schema_version": 1,
        "outcome": {
            "schema_version": 1,
            "type": outcome_type,
            "summary": f"deterministic {outcome_type}",
            "evidence_refs": [],
            "artifact_refs": [],
            "binding_refs": [],
            "blocker": blocker,
        },
    }


class _FakePiProcess:
    def __init__(
        self,
        run_config: PiRpcRunConfig,
        *,
        details: Mapping[str, Any],
        failure: BaseException | None = None,
    ) -> None:
        self.run_config = run_config
        self.details = details
        self.failure = failure
        self.returncode: int | None = None
        self.started = False
        self.closed = False
        self.aborted = False
        self.prompts: list[str] = []
        self.session_id = "pi-session-fixture"
        self.session_file = (
            run_config.session_path
            or str(Path(run_config.session_dir) / "pi-session-fixture.jsonl")
        )

    async def start(self) -> None:
        self.started = True
        session_file = Path(self.session_file)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.touch(exist_ok=True)

    async def get_state(self) -> Mapping[str, Any]:
        return {
            "sessionId": self.session_id,
            "sessionFile": self.session_file,
            "messageCount": 3 if self.run_config.session_path else 0,
        }

    async def get_messages(self) -> list[Any]:
        return [
            {"role": "user", "content": "stored request"},
            {"role": "assistant", "content": [{"type": "toolCall"}]},
            {"role": "toolResult", "toolName": PI_TERMINAL_TOOL_NAME},
        ]

    async def prompt(self, message: str) -> None:
        self.prompts.append(message)

    async def wait_for_terminal(self) -> tuple[PiTerminalOutcomePayload, str]:
        if self.failure is not None:
            raise self.failure
        return PiTerminalOutcomePayload.from_details(self.details), "pi-tool-call-fixture"

    async def abort_and_close(self) -> None:
        self.aborted = True
        await self.close()

    async def close(self) -> None:
        self.closed = True
        self.returncode = 0


class _FakePiFactory:
    def __init__(
        self,
        details: Mapping[str, Any] | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.details = dict(details or _terminal_details())
        self.failure = failure
        self.instances: list[_FakePiProcess] = []

    def __call__(self, run_config: PiRpcRunConfig) -> _FakePiProcess:
        process = _FakePiProcess(
            run_config,
            details=self.details,
            failure=self.failure,
        )
        self.instances.append(process)
        return process


class _LoopbackWriter:
    def __init__(self, reader: asyncio.StreamReader, response_mode: str = "ok") -> None:
        self.reader = reader
        self.response_mode = response_mode
        self.buffer = bytearray()
        self.closing = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        request = json.loads(data.decode("utf-8"))
        self.reader.feed_data(encode_jsonl_record({"type": "agent_start"}))
        response_id = request["id"] if self.response_mode == "ok" else "wrong-id"
        self.reader.feed_data(encode_jsonl_record({
            "id": response_id,
            "type": "response",
            "command": request["type"],
            "success": True,
            "data": {
                "sessionId": "loopback-session",
                "sessionFile": "/tmp/loopback-session.jsonl",
            },
        }))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None


class _LowLevelProcess:
    def __init__(self, reader: asyncio.StreamReader, writer: _LoopbackWriter) -> None:
        self.stdout = reader
        self.stdin = writer
        self.stderr = None
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 17
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class PiRpcContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="biomind-pi-rpc-unit-")
        self.addCleanup(self.temp.cleanup)
        self.plan_state = _approved_plan_state(self.temp.name)
        self.request = build_step_request(
            self.plan_state,
            workspace=self.temp.name,
            graph_scope={
                "task_id": self.plan_state["task_id"],
                "plan_version": self.plan_state["plan_version"],
                "current_step_id": self.plan_state["current_step_id"],
            },
            attempt_ordinal=2,
        )
        integration = Path(__file__).resolve().parents[1] / "integrations" / "pi_step_worker"
        self.config = PiRpcConfig(
            executable=str(integration / "node_modules" / ".bin" / "pi"),
            extension_path=str(integration / "biomind_pi_rpc_poc.ts"),
            runtime_root=str(Path(self.temp.name) / "runtime"),
            startup_timeout_seconds=0.2,
            response_timeout_seconds=0.05,
            settlement_timeout_seconds=0.2,
            shutdown_timeout_seconds=0.05,
        )

    async def asyncTearDown(self) -> None:
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None

    def test_manifest_is_exactly_pinned_and_backend_selection_is_explicit(self) -> None:
        integration = Path(self.config.extension_path).parent
        package = json.loads((integration / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["dependencies"][PI_PACKAGE_NAME], PI_PINNED_VERSION)
        self.assertNotIn("latest", json.dumps(package))
        self.assertEqual(package["engines"]["node"], ">=22.19.0")
        npmrc = (integration / ".npmrc").read_text(encoding="utf-8")
        self.assertIn("ignore-scripts=true", npmrc)
        self.assertEqual(resolve_step_worker_backend_name(environ={}), "legacy")
        self.assertEqual(plan_review_destination("generate", "legacy"), "generate")
        self.assertEqual(plan_review_destination("generate", "typed_stub"), "step_worker")
        self.assertEqual(plan_review_destination("generate", PI_RPC_BACKEND_NAME), "step_worker")
        self.assertIsNotNone(get_step_worker_backend("typed_stub"))
        self.assertIsInstance(get_step_worker_backend(PI_RPC_BACKEND_NAME), PiStepWorkerBackend)
        with self.assertRaisesRegex(Exception, "STEP_WORKER_BACKEND_UNSUPPORTED"):
            resolve_step_worker_backend_name("pi")

    def test_extension_is_one_offline_non_scientific_terminating_tool(self) -> None:
        source = Path(self.config.extension_path).read_text(encoding="utf-8")
        self.assertEqual(source.count("pi.registerTool("), 1)
        self.assertIn(f'const TOOL = "{PI_TERMINAL_TOOL_NAME}"', source)
        self.assertIn("terminate: true", source)
        self.assertIn("streamSimple", source)
        for forbidden in (
            "fetch(",
            "node:child_process",
            "node:fs",
            "bun.spawn",
            "deno.command",
            "biomind_run_shell",
            "biomind_run_python",
            "biomind_run_r",
            "snpeff",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source.lower())

    def test_canonical_request_hash_is_deterministic_and_route_free(self) -> None:
        encoded = canonical_step_request_json(self.request)
        self.assertEqual(encoded, canonical_step_request_json(self.request))
        self.assertEqual(
            step_request_sha256(self.request),
            step_request_sha256(self.request),
        )
        for field in FORBIDDEN_ROUTE_FIELDS:
            self.assertNotIn(f'"{field}":', encoded)

    def test_strict_jsonl_keeps_unicode_separators_inside_one_lf_frame(self) -> None:
        payload = {"type": "prompt", "message": "before\u2028middle\u2029after"}
        encoded = encode_jsonl_record(payload)
        self.assertEqual(encoded.count(b"\n"), 1)
        decoded = decode_jsonl_record(encoded[:-1])
        self.assertEqual(decoded, payload)
        self.assertEqual(decode_jsonl_record(encoded[:-1] + b"\r"), payload)
        for malformed in (b"", b"[]", b"{bad json}", b"{}\n{}"):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(PiRpcError, "PI_RPC_PROTOCOL_ERROR"):
                    decode_jsonl_record(malformed)

    def test_terminal_collector_requires_one_tool_result_and_agent_settled(self) -> None:
        def valid_events(collector: PiTerminalEventCollector) -> None:
            collector.observe({
                "type": "tool_execution_start",
                "toolName": PI_TERMINAL_TOOL_NAME,
                "toolCallId": "call-1",
            })
            collector.observe({
                "type": "tool_execution_end",
                "toolName": PI_TERMINAL_TOOL_NAME,
                "toolCallId": "call-1",
                "isError": False,
                "result": {"details": _terminal_details()},
            })

        collector = PiTerminalEventCollector(self.config.extension_path)
        valid_events(collector)
        collector.observe({"type": "agent_end", "willRetry": False})
        with self.assertRaisesRegex(PiRpcError, "agent_settled"):
            collector.finish()
        collector.observe({"type": "agent_settled"})
        self.assertEqual(collector.finish().outcome["type"], "completion_claimed")

        missing = PiTerminalEventCollector(self.config.extension_path)
        missing.observe({"type": "agent_settled"})
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_MISSING_TERMINAL_OUTCOME"):
            missing.finish()

        duplicate = PiTerminalEventCollector(self.config.extension_path)
        valid_events(duplicate)
        duplicate.observe({
            "type": "tool_execution_start",
            "toolName": PI_TERMINAL_TOOL_NAME,
            "toolCallId": "call-2",
        })
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_MULTIPLE_TERMINAL_OUTCOMES"):
            duplicate.observe({
                "type": "tool_execution_end",
                "toolName": PI_TERMINAL_TOOL_NAME,
                "toolCallId": "call-2",
                "isError": False,
                "result": {"details": _terminal_details()},
            })

        extension_error = PiTerminalEventCollector(self.config.extension_path)
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_EXTENSION_ERROR"):
            extension_error.observe({
                "type": "extension_error",
                "extensionPath": self.config.extension_path,
                "error": "fixture extension failed",
            })

        unexpected = PiTerminalEventCollector(self.config.extension_path)
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_PROTOCOL_ERROR"):
            unexpected.observe({
                "type": "tool_execution_start",
                "toolName": "bash",
                "toolCallId": "call-bash",
            })

    async def test_low_level_client_correlates_responses_and_forbids_bash(self) -> None:
        run_config = self.config.for_run(self.request)
        client = PiRpcProcess(run_config)
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_COMMAND_FORBIDDEN"):
            await client._command({"type": "bash", "command": "true"})
        reader = asyncio.StreamReader(limit=1024 * 1024)
        writer = _LoopbackWriter(reader)
        client.process = _LowLevelProcess(reader, writer)  # type: ignore[assignment]
        state = await client.get_state()
        self.assertEqual(state["sessionId"], "loopback-session")
        self.assertEqual(client._events[0]["type"], "agent_start")
        written = bytes(writer.buffer)
        self.assertNotIn(b'"type":"bash"', written)
        self.assertIn(b'"id":"biomind-', written)

        bad_reader = asyncio.StreamReader(limit=1024 * 1024)
        bad_writer = _LoopbackWriter(bad_reader, response_mode="wrong-id")
        bad_client = PiRpcProcess(self.config.for_run(self.request))
        bad_client.process = _LowLevelProcess(bad_reader, bad_writer)  # type: ignore[assignment]
        with self.assertRaisesRegex(PiRpcError, "uncorrelated"):
            await bad_client.get_state()

    async def test_low_level_malformed_exit_and_timeout_fail_closed(self) -> None:
        malformed_reader = asyncio.StreamReader(limit=1024 * 1024)
        malformed_writer = _LoopbackWriter(malformed_reader)
        malformed_client = PiRpcProcess(self.config.for_run(self.request))
        malformed_client.process = _LowLevelProcess(  # type: ignore[assignment]
            malformed_reader, malformed_writer
        )
        malformed_reader.feed_data(b"{not-json}\n")
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_PROTOCOL_ERROR"):
            await malformed_client._read_record(0.05)

        exited_reader = asyncio.StreamReader(limit=1024 * 1024)
        exited_writer = _LoopbackWriter(exited_reader)
        exited_client = PiRpcProcess(self.config.for_run(self.request))
        exited_client.process = _LowLevelProcess(exited_reader, exited_writer)  # type: ignore[assignment]
        exited_reader.feed_eof()
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_PROCESS_EXITED"):
            await exited_client._read_record(0.05)

        timeout_reader = asyncio.StreamReader(limit=1024 * 1024)
        timeout_writer = _LoopbackWriter(timeout_reader)
        timeout_client = PiRpcProcess(self.config.for_run(self.request))
        timeout_client.process = _LowLevelProcess(timeout_reader, timeout_writer)  # type: ignore[assignment]
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_TIMEOUT"):
            await timeout_client._read_record(0.01)

    def test_process_profile_is_isolated_and_credential_free(self) -> None:
        run_config = self.config.for_run(self.request)
        process = PiRpcProcess(run_config)
        with patch.dict(os.environ, {
            "PATH": "/controlled/bin",
            "OPENAI_API_KEY": "forbidden",
            "DEEPSEEK_API_KEY": "forbidden",
            "ANTHROPIC_API_KEY": "forbidden",
            "GOOGLE_API_KEY": "forbidden",
            "AWS_SECRET_ACCESS_KEY": "forbidden",
        }, clear=True):
            environment = process._environment()
        self.assertEqual(environment["HOME"], run_config.fake_home)
        self.assertEqual(environment["PI_CODING_AGENT_DIR"], run_config.agent_dir)
        self.assertEqual(environment["PI_CODING_AGENT_SESSION_DIR"], run_config.session_dir)
        self.assertEqual(environment["PI_OFFLINE"], "1")
        self.assertFalse(any("API_KEY" in key or "SECRET" in key for key in environment))
        arguments = process._arguments()
        self.assertIn("--no-builtin-tools", arguments)
        self.assertIn("--no-extensions", arguments)
        self.assertIn("--no-skills", arguments)
        self.assertIn("--no-prompt-templates", arguments)
        self.assertIn("--no-context-files", arguments)
        self.assertEqual(arguments[arguments.index("--tools") + 1], PI_TERMINAL_TOOL_NAME)
        self.assertEqual(arguments[arguments.index("--provider") + 1], PI_FAKE_PROVIDER_NAME)
        self.assertEqual(arguments[arguments.index("--model") + 1], PI_FAKE_MODEL_NAME)

    async def test_mocked_process_converts_all_existing_outcomes_and_records_session(self) -> None:
        for outcome_type, destination in (
            ("completion_claimed", "supervisor"),
            ("needs_replan", "planner"),
            ("blocked", "finish"),
            ("harness_error", "finish"),
            ("cancelled", "finish"),
        ):
            with self.subTest(outcome_type=outcome_type):
                factory = _FakePiFactory(_terminal_details(outcome_type))
                backend = PiStepWorkerBackend(self.config, process_factory=factory)
                outcome = await backend.run_step(self.request)
                self.assertEqual(outcome.type, outcome_type)
                self.assertEqual(outcome.worker_session_ref, "pi-session-fixture")
                self.assertEqual(
                    route_step_worker_outcome({"step_worker_outcome": outcome.to_dict()}),
                    destination,
                )
                self.assertTrue(factory.instances[0].closed)
                prompt = factory.instances[0].prompts[0]
                envelope = json.loads(prompt.split("\n", 1)[1])
                self.assertEqual(envelope["step_request_sha256"], step_request_sha256(self.request))
                for field in FORBIDDEN_ROUTE_FIELDS:
                    self.assertNotIn(f'"{field}":', prompt)
                binding = backend.last_session_binding
                self.assertIsNotNone(binding)
                assert binding is not None
                binding_path = Path(binding.session_dir) / f"{binding.session_id}.biomind-binding.json"
                self.assertTrue(binding_path.is_file())

    async def test_protocol_payload_injection_and_transport_failure_become_harness_error(self) -> None:
        for injected in ("planner", "generate", "execute"):
            details = _terminal_details("completion_claimed")
            details["outcome"]["type"] = injected
            backend = PiStepWorkerBackend(self.config, process_factory=_FakePiFactory(details))
            outcome = await backend.run_step(self.request)
            self.assertEqual(outcome.type, "harness_error")
            self.assertEqual(outcome.blocker["code"], "PI_RPC_PROTOCOL_ERROR")

        for route_field in FORBIDDEN_ROUTE_FIELDS:
            details = _terminal_details("completion_claimed")
            details["outcome"][route_field] = "planner"
            backend = PiStepWorkerBackend(self.config, process_factory=_FakePiFactory(details))
            outcome = await backend.run_step(self.request)
            self.assertEqual(outcome.type, "harness_error")
            self.assertEqual(outcome.blocker["code"], "PI_RPC_PROTOCOL_ERROR")

        failure = PiRpcError("PI_RPC_TIMEOUT", "fixture settlement timeout")
        factory = _FakePiFactory(failure=failure)
        backend = PiStepWorkerBackend(self.config, process_factory=factory)
        outcome = await backend.run_step(self.request)
        self.assertEqual(outcome.type, "harness_error")
        self.assertEqual(outcome.blocker["code"], "PI_RPC_TIMEOUT")
        self.assertTrue(factory.instances[0].closed)

    async def test_cancellation_and_graphbubble_preserve_control_semantics(self) -> None:
        cancellation_factory = _FakePiFactory(failure=asyncio.CancelledError())
        backend = PiStepWorkerBackend(self.config, process_factory=cancellation_factory)
        with self.assertRaises(asyncio.CancelledError):
            await backend.run_step(self.request)
        self.assertTrue(cancellation_factory.instances[0].aborted)
        self.assertEqual(backend.last_audit["transport_error_code"], "PI_RPC_CANCELLED")

        bubble = GraphBubbleUp()
        bubble_factory = _FakePiFactory(failure=bubble)
        backend = PiStepWorkerBackend(self.config, process_factory=bubble_factory)
        with self.assertRaises(GraphBubbleUp) as raised:
            await backend.run_step(self.request)
        self.assertIs(raised.exception, bubble)
        self.assertTrue(bubble_factory.instances[0].closed)

    async def test_mocked_cross_process_resume_and_scope_mismatch(self) -> None:
        factory = _FakePiFactory()
        backend = PiStepWorkerBackend(self.config, process_factory=factory)
        outcome = await backend.run_step(self.request)
        self.assertEqual(outcome.type, "completion_claimed")
        binding = backend.last_session_binding
        self.assertIsNotNone(binding)
        assert binding is not None
        resumed = await backend.reopen_session_for_poc(self.request, binding)
        self.assertEqual(resumed["session_id"], binding.session_id)
        self.assertEqual(resumed["session_file"], binding.session_file)
        self.assertGreater(resumed["message_count"], 0)
        self.assertEqual(len(factory.instances), 2)
        self.assertTrue(factory.instances[0].closed)
        self.assertTrue(factory.instances[1].closed)

        mismatched = replace(self.request, attempt_ordinal=self.request.attempt_ordinal + 1)
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_SESSION_MISMATCH"):
            await backend.reopen_session_for_poc(mismatched, binding)

        escaped = replace(binding, session_file="/tmp/foreign-session.jsonl")
        with self.assertRaisesRegex(PiRpcError, "PI_RPC_SESSION_MISMATCH"):
            await backend.reopen_session_for_poc(self.request, escaped)


def _real_pi_available() -> bool:
    config = PiRpcConfig.default()
    executable = Path(config.executable)
    return executable.is_file() and os.access(executable, os.X_OK)


@unittest.skipUnless(
    _real_pi_available(),
    "project-local @earendil-works/pi-coding-agent@0.84.4 is not installed",
)
class RealPiRpcAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pi_process_resume_and_compiled_langgraph_edge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="biomind-real-pi-rpc-") as root:
            plan_state = _approved_plan_state(root)
            request = build_step_request(
                plan_state,
                workspace=root,
                graph_scope={
                    "task_id": plan_state["task_id"],
                    "plan_version": plan_state["plan_version"],
                    "current_step_id": plan_state["current_step_id"],
                },
                attempt_ordinal=1,
            )
            base = PiRpcConfig.default()
            config = replace(base, runtime_root=str(Path(root) / "runtime"))
            backend = PiStepWorkerBackend(config)
            visited: list[str] = []
            graph = StateGraph(AgentState)

            async def worker(_state: AgentState) -> AgentState:
                outcome = await backend.run_step(request)
                return {"step_worker_outcome": outcome.to_dict()}

            async def supervisor(_state: AgentState) -> AgentState:
                visited.append("supervisor")
                return {}

            async def planner(_state: AgentState) -> AgentState:
                visited.append("planner")
                return {}

            def router(state: AgentState) -> str:
                destination = route_step_worker_outcome(state)
                return END if destination == "finish" else destination

            graph.add_node("step_worker", worker)
            graph.add_node("supervisor", supervisor)
            graph.add_node("planner", planner)
            graph.add_edge(START, "step_worker")
            graph.add_conditional_edges(
                "step_worker", router, ["supervisor", "planner", END]
            )
            graph.add_edge("supervisor", END)
            graph.add_edge("planner", END)
            polluted: AgentState = {
                "next_step": "planner",
                "proposed_route": "finish",
                "effective_route": "generate",
                "dispatch_route": "execute",
                "route_resolution": {"dispatch_route": "finish"},
                "required_next_action": "REPLAN",
                "pending_recovery_action": {"type": "INTERNAL_PROTOCOL_ERROR"},
            }
            await graph.compile().ainvoke(polluted)
            self.assertEqual(visited, ["supervisor"])
            binding = backend.last_session_binding
            self.assertIsNotNone(binding)
            assert binding is not None
            resumed = await backend.reopen_session_for_poc(request, binding)
            self.assertEqual(resumed["session_id"], binding.session_id)
            self.assertGreater(resumed["message_count"], 0)


if __name__ == "__main__":
    unittest.main()
