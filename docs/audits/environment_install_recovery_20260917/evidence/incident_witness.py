"""Read-only incident audit and isolated witnesses; no application/config imports.

Only the historical approved script's two path constants are changed in-memory.
All scientific subprocesses and discovery calls are replaced in this process.
Current pure parser/display/validator functions are loaded from their actual AST,
avoiding application initialization and credential loading. No CommitStep runs.
"""
from __future__ import annotations

import ast
import collections
import contextlib
from copy import deepcopy
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
SESSION = ROOT.parent / "benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497"
OUT = Path(__file__).with_suffix(".json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def functions(path, names, namespace=None):
    """Compile exact named definitions, with annotations deferred, not imports."""
    tree = ast.parse(path.read_text())
    chosen = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    assert {n.name for n in chosen} == set(names)
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *chosen]
    ns = dict(namespace or {})
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), ns)
    return ns


def historical_summary():
    metrics = json.loads((SESSION / "run_metrics.json").read_text())
    plan = json.loads((SESSION / "plan_state.json").read_text())
    rows = []
    seen = set()
    for path in sorted(SESSION.glob("context_trace/*/manifest.json")):
        manifest = json.loads(path.read_text())
        call_id = manifest["call_id"]
        assert call_id not in seen
        seen.add(call_id)
        reqpath = path.with_name("request.json")
        request = json.loads(reqpath.read_text())
        response = json.loads(path.with_name("response.json").read_text())
        provider = manifest.get("annotations", {}).get("provider_attempt", {})
        rows.append({"call_id": call_id, "call_kind": manifest.get("call_kind"),
                     "request_sha256": digest(reqpath), "request_bytes": reqpath.stat().st_size,
                     "message_count": len(request.get("messages", [])),
                     "content_characters": sum(len(str(m.get("content", ""))) for m in request.get("messages", [])),
                     "provider_request_id": provider.get("provider_request_id"),
                     "provider_attempt_id": provider.get("provider_attempt_id"),
                     "provider_usage": provider.get("provider_usage"),
                     "total_elapsed_seconds": provider.get("total_elapsed_seconds"),
                     "finish_reason": provider.get("finish_reason"),
                     "tool_calls": [{"name": c.get("name"), "id": c.get("id")} for c in response.get("tool_calls", [])]})
    events = metrics["events"]
    llms = [r for r in events if r["type"] == "llm"]
    summary = metrics["summary"]
    assert len(llms) == summary["llm_calls"] == 20
    assert sum(r["total_tokens"] for r in llms) == summary["tokens"]["total_tokens"]
    terminal = next(r for r in rows if r["call_id"].startswith("terminal_summary-"))
    stop = plan["task_state"]["managed_run_stop"]
    return {
        "session_root": str(SESSION), "session_plan_sha256": digest(SESSION / "plan_state.json"),
        "run_metrics_summary": summary,
        "metrics_llm_event_nodes": dict(collections.Counter(r["node"] for r in llms)),
        "unique_archived_request_count": len(rows), "calls": rows,
        "terminal_summary_excluded_from_metrics_llm_events": not any(r["node"] == "terminal_summary" for r in llms),
        "metrics_plus_terminal_tokens": summary["tokens"]["total_tokens"] + terminal["provider_usage"]["total_tokens"],
        "metrics_plus_terminal_prompt_tokens": summary["tokens"]["prompt_tokens"] + terminal["provider_usage"]["input_tokens"],
        "metrics_plus_terminal_completion_tokens": summary["tokens"]["completion_tokens"] + terminal["provider_usage"]["output_tokens"],
        "sum_command_event_seconds": round(sum(e["duration_seconds"] for e in events if e["type"] == "command"), 3),
        "sum_confirmation_event_seconds": round(sum(e["duration_seconds"] for e in events if e["type"] == "confirmation"), 3),
        "task_status": plan["task_state"]["status"], "execution_status": plan["execution_status"],
        "runtime_state_status": plan["runtime_state"]["status"],
        "steps": [{k: s.get(k) for k in ("step_id", "status", "dependencies", "inputs", "outputs", "execution_capability_id", "success_criteria")} for s in plan["steps"]],
        "stop": {k: deepcopy(stop.get(k)) for k in ("stop_reason", "inspection_failures", "inspection_observations", "directive_reception", "prior_completed_work", "report_receipts", "required_output_status", "internal_diagnostic_delivery", "recovery_usage")},
        "runtime_counters": {k: plan["runtime_state"]["budgets"].get(k) for k in ("preaction_rejections", "payload_repairs", "execution_retry_count", "primary_execution_dispatches", "primary_execution_failures")},
    }


def partial_index_witness():
    source = SESSION / "attempts/attempt_51bd254a3abad5bfdfa0/approved.py"
    results = []
    gate = functions(ROOT / "agent_core/completion_gate.py", ["_contract_exists", "_contract_roots"], {"Path": Path})
    for scenario in ("only_fai_success", "all_commands_fail_but_partial_bwa_file"):
        with tempfile.TemporaryDirectory(prefix="incident-index-") as tmp:
            base = Path(tmp)
            ref = base / "input.fasta"
            ref.write_text(">audit_chr\nACGT\n")
            bundle = base / "bundle"
            tree = ast.parse(source.read_text())
            modified = []
            for n in tree.body:
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    key = n.targets[0].id
                    if key in {"REFERENCE", "OUTDIR"}:
                        n.value = ast.Constant(str(ref if key == "REFERENCE" else bundle))
                        modified.append(key)
            assert sorted(modified) == ["OUTDIR", "REFERENCE"]
            calls = []

            def command_stub(argv, **kwargs):
                assert isinstance(argv, list) and argv[0] in {"samtools", "bwa", "picard"}
                assert all(str(SESSION) not in str(a) for a in argv)
                rc = 1
                if argv[:2] == ["samtools", "faidx"] and scenario == "only_fai_success":
                    Path(argv[2] + ".fai").write_text("audit_chr\t4\t11\t4\t5\n")
                    rc = 0
                if argv[:2] == ["bwa", "index"] and scenario == "all_commands_fail_but_partial_bwa_file":
                    Path(argv[2] + ".amb").write_text("partial, not a valid BWA index\n")
                calls.append({"command": [str(a).replace(str(base), "<TMP>") for a in argv], "returncode": rc})
                return types.SimpleNamespace(returncode=rc, stdout="isolated fixture only\n")

            output = io.StringIO()
            status = 0
            with patch.object(subprocess, "run", command_stub), patch.object(shutil, "which", lambda name: "/fixture/bin/" + name), contextlib.redirect_stdout(output):
                try:
                    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), {})
                except SystemExit as exc:
                    status = exc.code
            line = next(l for l in output.getvalue().splitlines() if l.startswith("BIOMIND_EFFECT_EVIDENCE_JSON="))
            emission = json.loads(line.split("=", 1)[1])
            evidence = emission["explicit_side_effects"][0]
            directory_pass = gate["_contract_exists"]({"path": str(bundle), "validator": "directory_exists"}, [])
            assert status == 0 and evidence["status"] == "created" and directory_pass
            assert len(evidence["index_artifacts"]) == 1
            results.append({"scenario": scenario, "status": "PASS_current_behavior_witness",
                            "script_exit": status, "calls": calls,
                            "emission": json.loads(json.dumps(emission).replace(str(base), "<TMP>")),
                            "current_directory_exists_validator": directory_pass,
                            "actual_files": sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()),
                            "formal_commit_executed": False})
    return {"source": str(source), "source_sha256": digest(source), "ast_changes": ["REFERENCE constant", "OUTDIR constant"],
            "mocked_boundaries": ["shutil.which", "subprocess.run"],
            "real_logic": ["historical complete approved.py command-result and evidence branches", "current completion_gate._contract_exists directory predicate"],
            "limitation": "Demonstrates false created emission and generic directory predicate only; Supervisor/CommitStep not run and no scientific output accepted.",
            "cases": results}


def cli_witness(historical):
    source = ROOT / "agent_core/cli_lifecycle.py"
    ns = {"__name__": "incident_cli", "json": json}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    metrics = json.loads((SESSION / "run_metrics.json").read_text())
    facts = metrics["final_result"]["terminal_diagnostic"]
    text = ns["terminal_text"]("blocked", deepcopy(facts))
    archived = facts["internal_diagnostic_delivery"]["paths"]["report.md"]
    checks = {"specific_cause_still_displayed_unknown": "具体原因尚未定位" in text,
              "resolved_inspection_failure_still_current": "恢复取证问题：读取证据失败（INSPECTION_REF_KIND_MISMATCH）" in text,
              "archive_status_visible": "内部诊断归档=complete" in text,
              "archive_path_not_visible": archived not in text}
    assert all(checks.values())
    return {"status": "PASS_current_behavior_witness", "source": str(source), "source_sha256": digest(source),
            "input": str(SESSION / "run_metrics.json") + "#final_result.terminal_diagnostic",
            "checks": checks, "text": text, "writes": False}


def main():
    history = historical_summary()
    result = {"scope": "Current-behavior witnesses only. No production repair, software installation, model/API call, real WGS or task resume.",
              "historical_summary": history, "partial_index_witness": partial_index_witness(), "cli_witness": cli_witness(history)}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "PASS", "output": str(OUT), "witness_cases": 3,
                      "metrics_llm_calls": 20, "archived_unique_requests": history["unique_archived_request_count"],
                      "all_in_tokens": history["metrics_plus_terminal_tokens"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
