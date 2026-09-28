"""Inspection production adapter tests; only the process/environment leaf is fake."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.runtime_evidence import digest, make_runtime_evidence
from agent_core.runtime_inspection import (
    BubblewrapRuntimeProbe, InspectionPolicyError, RuntimeInspectionAdapter,
    admitted_healthcheck,
)


def _verified_fixture_paths(detail: str) -> dict[str, str]:
    """Validate synthetic probe paths without constructing a legacy Agent."""
    resolved = {}
    for raw_line in str(detail or "").splitlines():
        match = re.fullmatch(r"\s*([A-Z][A-Z0-9_]{1,63})=(/[^\r\n]+?)\s*", raw_line)
        if not match:
            continue
        key, raw_path = match.groups()
        path = Path(raw_path).expanduser()
        try:
            valid = path.is_dir() or (path.is_file() and path.stat().st_size > 0)
        except OSError:
            valid = False
        if valid:
            resolved[key] = str(path.resolve())
    return resolved


class _Process:
    def __init__(self, stdout: str, stderr: str, code: int):
        self.returncode, self.pid = code, 999999999
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout.encode())
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr.encode())
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode


class _ProcessLeaf:
    def __init__(self):
        self.calls, self.stdout, self.stderr, self.code, self.error = [], "", "", 0, None

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return _Process(self.stdout, self.stderr, self.code)


class _SkillFixture:
    def __init__(self, root, command):
        self.skills_root = root / "skills"
        self.skills_root.mkdir()
        self.command, self.outputs = command, {}
        self.tools, self.required = ["widget"], ["runtime.widget.bin"]

    def get_runtime_requirements(self, _skill_id):
        return {"env": "testenv", "healthcheck": self.command, "tools": self.tools}

    def get_skill_meta(self, _skill_id):
        return {"runtime": {"inspection_outputs": self.outputs}}

    def get_execution_capability_registry(self, _skills):
        return {"test.inspect": {"required_runtime_binding_ids": self.required}}


class RuntimeInspectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-inspection-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "conda" / "envs" / "testenv"
        (self.prefix / "bin").mkdir(parents=True)
        self.binary = self.prefix / "bin" / "widget"
        self.binary.write_bytes(b"fixture-binary-is-never-executed")
        self.binary.chmod(0o700)
        self.bwrap = self.root / "bwrap"
        self.bwrap.write_bytes(b"fixture-bwrap-is-never-executed")
        self.bwrap.chmod(0o700)
        command = 'conda run -n testenv bash -lc \'widget --version >/dev/null; printf "RESOLVED_WIDGET_BIN=%s\\n" "$CONDA_PREFIX/bin/widget"\''
        self.skills = _SkillFixture(self.root, command)
        self.subject = {"skill_id": "test/widget", "skill_document_digest": digest("document"),
                        "skill_contract_digest": digest("contract"), "environment_id": "testenv",
                        "environment_fingerprint": digest("environment"), "host_id": "host-test",
                        "inspection_contract_digest": digest(command)}
        self.leaf = _ProcessLeaf()
        self.leaf.stdout = f"RESOLVED_WIDGET_BIN={self.binary}\n"
        self.probe = BubblewrapRuntimeProbe(process_factory=self.leaf, bwrap_path=str(self.bwrap))
        self.adapter = RuntimeInspectionAdapter(SimpleNamespace(conda_root=self.root / "conda"), self.skills,
            scope_provider=lambda _skill: deepcopy(self.subject),
            path_validator=_verified_fixture_paths,
            probe=self.probe, clock=lambda: "2026-09-07T10:00:00+00:00")

    def request(self):
        return {"scope": {"selected_skill_id": "test/widget", "subject": deepcopy(self.subject),
                           "required_binding_ids": list(self.skills.required)},
                "inspection_contract": self.adapter.describe("test/widget"), "operation": "inspect",
                "inspection_receipt_id": "persisted-receipt-1", "inspection_deadline_seconds": 4.0}

    async def test_nonjava_fact_runs_actual_adapter_and_sandbox_argv(self):
        request = self.request()
        self.assertTrue(request["inspection_contract"]["permitted"], request)
        result = await self.adapter.inspect(request)
        self.assertEqual(result["coverage"]["runtime.widget.bin"]["availability"], "AVAILABLE", result)
        record = make_runtime_evidence(result["audit"], task_id="task-1", skill_scope=self.subject,
            observed_at=result["observed_at"], producer="runtime_inspection", coverage=result["coverage"], inspection_status=result["inspection_status"])
        self.assertEqual(record["facts"]["runtime.widget.bin"]["availability"], "AVAILABLE")
        argv, kwargs = self.leaf.calls[0]
        for flag in ("--unshare-net", "--unshare-pid", "--unshare-user", "--die-with-parent", "--new-session"):
            self.assertIn(flag, argv)
        self.assertEqual(kwargs["env"], {})
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("--bind", argv)
        self.assertNotIn("-lc", argv)
        self.assertNotIn("--ro-bind-try", argv)
        self.assertEqual(argv[-5:-1], ("/bin/bash", "--noprofile", "--norc", "-c"))
        self.assertIn('printf "RESOLVED_WIDGET_BIN=%s\\n"', argv[-1])
        self.assertNotIn(str(Path.home()), argv)
        self.assertNotIn(str(self.root), argv)

    async def test_scope_or_method_change_does_not_launch(self):
        for field in self.subject:
            request = self.request()
            request["scope"]["subject"][field] += "changed"
            result = await self.adapter.inspect(request)
            self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_SCOPE_DRIFT")
        request = self.request()
        self.skills.command += "; exit 0"
        self.assertFalse(self.adapter.describe("test/widget")["permitted"])
        self.assertEqual((await self.adapter.inspect(request))["inspection_status"], "not_run")
        self.assertEqual(self.leaf.calls, [])

    async def test_contract_drift_and_self_dependency_scope_rejected(self):
        request = self.request()
        request["inspection_contract"]["contract_digest"] = digest("other")
        self.assertEqual((await self.adapter.inspect(request))["reason_code"], "RUNTIME_INSPECTION_CONTRACT_DRIFT")
        request = self.request()
        request["scope"]["required_binding_ids"] = ["runtime.other.bin"]
        self.assertEqual((await self.adapter.inspect(request))["reason_code"], "RUNTIME_INSPECTION_COVERAGE_UNDECLARED")
        self.assertEqual(self.leaf.calls, [])

    async def test_missing_sandbox_does_not_fallback(self):
        self.probe.bwrap_path = str(self.root / "does-not-exist")
        description = self.adapter.describe("test/widget")
        self.assertFalse(description["permitted"])
        self.assertEqual(description["reason_code"], "RUNTIME_INSPECTION_SANDBOX_UNAVAILABLE")
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "not_run")
        self.assertEqual(self.leaf.calls, [])

    async def test_nonzero_and_permission_are_not_absence(self):
        self.leaf.code = 1
        self.leaf.stderr = "bwrap: permission denied: SECRET=hidden"
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "execution_error")
        self.assertEqual(result["coverage"], {})
        self.assertNotIn("hidden", str(result))
        self.leaf.error = PermissionError("sensitive host failure")
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "permission_error")
        self.assertEqual(result["coverage"], {})
        self.assertNotIn("sensitive host", str(result))

    async def test_timeout_and_cancel_do_not_become_unavailable(self):
        self.leaf.error = asyncio.TimeoutError()
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "timeout")
        self.assertTrue(result["transient"])
        self.leaf.error = asyncio.CancelledError()
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "cancelled")

    async def test_output_limit_drops_all_untrusted_output(self):
        self.leaf.stdout = "x" * 65537
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_OUTPUT_LIMIT")
        self.assertEqual(result["audit"], {})

    async def test_ok_without_path_is_unknown(self):
        self.leaf.stdout = "OK\n"
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["inspection_status"], "succeeded")
        self.assertEqual(result["coverage"]["runtime.widget.bin"]["availability"], "UNKNOWN")

    async def test_two_required_facts_partial_success_stays_partial(self):
        self.skills.required += ["runtime.widget.jar"]
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["coverage"]["runtime.widget.bin"]["availability"], "AVAILABLE")
        self.assertEqual(result["coverage"]["runtime.widget.jar"]["availability"], "UNKNOWN")

    async def test_multiple_printf_outputs_after_literal_newline_are_materialized(self):
        helper = self.prefix / "bin" / "helper"
        helper.write_bytes(b"fixture-never-executed")
        helper.chmod(0o700)
        self.skills.required += ["runtime.helper.bin"]
        self.skills.command = 'conda run -n testenv bash -lc \'printf "RESOLVED_WIDGET_BIN=%s\\nRESOLVED_HELPER_BIN=%s\\n" "$CONDA_PREFIX/bin/widget" "$CONDA_PREFIX/bin/helper"\''
        self.subject["inspection_contract_digest"] = digest(self.skills.command)
        self.leaf.stdout += f"RESOLVED_HELPER_BIN={helper}\n"
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["coverage"]["runtime.widget.bin"]["availability"], "AVAILABLE", result)
        self.assertEqual(result["coverage"]["runtime.helper.bin"]["availability"], "AVAILABLE", result)

    async def test_out_of_scope_and_nonexecutable_paths_rejected(self):
        other = self.root / "other"
        other.write_bytes(b"unapproved")
        other.chmod(0o700)
        self.leaf.stdout = f"RESOLVED_WIDGET_BIN={other}\n"
        self.assertEqual((await self.adapter.inspect(self.request()))["reason_code"], "RUNTIME_INSPECTION_OUTPUT_OUT_OF_SCOPE")
        self.binary.chmod(0o600)
        self.leaf.stdout = f"RESOLVED_WIDGET_BIN={self.binary}\n"
        self.assertEqual((await self.adapter.inspect(self.request()))["reason_code"], "RUNTIME_INSPECTION_OUTPUT_OUT_OF_SCOPE")

    async def test_explicit_complete_negative_is_distinct_from_failed_probe(self):
        self.skills.outputs = {"RESOLVED_WIDGET_BIN": {"binding_id": "runtime.widget.bin", "value_kind": "executable_path",
            "negative_token": "ABSENT", "complete_search": True, "negative_reason_code": "approved_search_exhausted"}}
        self.leaf.stdout = "RESOLVED_WIDGET_BIN=ABSENT\n"
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["coverage"]["runtime.widget.bin"]["availability"], "UNAVAILABLE")
        record = make_runtime_evidence(result["audit"], task_id="task-1", skill_scope=self.subject,
            observed_at=result["observed_at"], producer="runtime_inspection", coverage=result["coverage"], inspection_status=result["inspection_status"])
        self.assertTrue(record["facts"]["runtime.widget.bin"]["conclusion_complete"])
        self.leaf.code = 1
        self.assertEqual((await self.adapter.inspect(self.request()))["coverage"], {})

    async def test_duplicate_or_sensitive_output_is_rejected_and_diagnostics_not_saved(self):
        self.leaf.stdout *= 2
        self.assertEqual((await self.adapter.inspect(self.request()))["reason_code"], "RUNTIME_INSPECTION_OUTPUT_INVALID")
        self.leaf.stdout = "RESOLVED_WIDGET_BIN=/tmp/.env.api_keys\n"
        result = await self.adapter.inspect(self.request())
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_OUTPUT_INVALID")
        self.assertNotIn(".env", str(result))

    def test_install_download_science_and_shell_eval_are_not_admitted(self):
        for script in ("conda install widget", "curl https://example.invalid/tool", "widget build", "widget -c evil --version", "eval 'widget --version'", "find / -type f", "printf X=y > /tmp/file", "widget --version &", "source /tmp/profile", "bash -c evil", "widget --version\nconda install widget\nwidget --version", "widget --version\ncurl url\nwidget --version"):
            with self.subTest(script=script), self.assertRaises(InspectionPolicyError):
                admitted_healthcheck("bash -lc " + __import__("shlex").quote(script), "testenv", {"widget"})

    def test_existing_multi_line_healthcheck_grammar_keeps_quoted_body(self):
        script = ('TOOL_HOME="${TOOL_HOME:-}"; if [ -z "$TOOL_HOME" ] && [ -n "${SOURCE_DIR:-}" ] && '
                  '[ -x "${SOURCE_DIR}/tool/bin/widget" ]; then TOOL_HOME="${SOURCE_DIR}/tool"; fi; '
                  'TOOL_JAR=$(find "$CONDA_PREFIX/share" -maxdepth 4 -type f -name tool.jar -print -quit); '
                  'test -s "$TOOL_JAR" || exit 1; if [ -n "$TOOL_HOME" ]; then test -x "$TOOL_HOME/bin/widget" || exit 1; '
                  '"$TOOL_HOME/bin/widget" -jar "$TOOL_JAR" -version >/dev/null || exit 1; '
                  'else command -v widget >/dev/null || exit 1; widget --version >/dev/null || exit 1; '
                  'TOOL_HOME=$(dirname "$(dirname "$(command -v widget)")"); fi; '
                  'printf "RESOLVED_WIDGET_JAR=%s\\nRESOLVED_WIDGET_HOME=%s\\n" "$TOOL_JAR" "$TOOL_HOME"')
        command = "conda run -n testenv bash -lc " + __import__("shlex").quote(script)
        self.assertEqual(admitted_healthcheck(command, "testenv", {"widget"})[-1], script)

    def test_roots_and_safe_environment_are_exact_contract_inputs(self):
        initial = self.request()["inspection_contract"]["contract_digest"]
        other = self.root / "toolroot"
        other.mkdir()
        self.adapter.allowed_runtime_roots = lambda _skill: [other]
        self.adapter.envsafevars = lambda _skill: {"WIDGET_HOME": str(other)}
        self.assertTrue(self.adapter.describe("test/widget")["permitted"])
        self.assertNotEqual(initial, self.adapter.describe("test/widget")["contract_digest"])
        self.adapter.envsafevars = lambda _skill: {"API_KEY": "do-not-pass"}
        self.assertEqual(self.adapter.describe("test/widget")["reason_code"], "RUNTIME_INSPECTION_ENVIRONMENT_UNSAFE")
        self.adapter.envsafevars = lambda _skill: {}
        self.adapter.allowed_runtime_roots = lambda _skill: [self.root]
        self.assertEqual(self.adapter.describe("test/widget")["reason_code"], "RUNTIME_INSPECTION_ROOT_TOO_BROAD")


if __name__ == "__main__":
    unittest.main()
