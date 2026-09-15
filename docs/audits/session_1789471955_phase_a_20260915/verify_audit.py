"""Read-only historical consistency checks; no BioMind imports or model calls.

Run with system Python. This script reads allowlisted historical receipts and
hashes; it never executes saved scripts, touches biological data, or opens .env.
"""
from pathlib import Path
import ast
import collections
import csv
import datetime
import hashlib
import json
import subprocess


AUDIT = Path(__file__).resolve().parent
REPO = AUDIT.parents[2]
SESSION = REPO.parent / "benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955"


def read_json(path):
    return json.loads(path.read_text())


def main():
    checks = {}
    metrics = read_json(SESSION / "run_metrics.json")
    llm = [e for e in metrics["events"] if e["type"] == "llm"]
    totals = {k: sum(e.get(k) or 0 for e in llm) for k in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    )}
    expected = dict(prompt_tokens=2959152, completion_tokens=111886,
                    total_tokens=3071038, prompt_cache_hit_tokens=1310976,
                    prompt_cache_miss_tokens=1648176)
    assert len(llm) == 77 and totals == expected, (len(llm), totals)
    assert totals["total_tokens"] == totals["prompt_tokens"] + totals["completion_tokens"]
    assert totals["prompt_tokens"] == totals["prompt_cache_hit_tokens"] + totals["prompt_cache_miss_tokens"]
    checks["completed_usage_arithmetic"] = {"status": "PASS", "rows": len(llm), **totals}

    calls = []
    for p in (SESSION / "context_trace").glob("*/manifest.json"):
        d = read_json(p)
        if d.get("call_id"):
            calls.append(d)
    assert len(calls) == 79
    statuses = collections.Counter(d.get("status") for d in calls)
    assert statuses["completed"] == 77, statuses
    checks["context_trace_inventory"] = {"status": "PASS", "calls": len(calls), "statuses": dict(statuses),
        "boundary": "Trace requests are observable application calls; hidden SDK retries are not counted as known physical requests."}

    executions = [json.loads(line) for line in (SESSION / "executions/execution_records.jsonl").read_text().splitlines() if line.strip()]
    counts = collections.Counter(e["step_id"] for e in executions)
    assert counts == {"check_inputs_readable": 1, "check_contig_compatibility": 1, "prepare_database": 4}
    assert all(e["exit_code"] == 0 for e in executions)
    checks["execution_inventory"] = {"status": "PASS", "by_step": dict(counts),
        "duration_seconds": round(sum(e["duration_seconds"] for e in executions), 3),
        "boundary": "Exit code zero does not assert postconditions, Commit, database build, or scientific completion."}

    state = read_json(SESSION / "plan_state.json")
    assert state["runtime_state"]["status"] == "cancelled"
    assert metrics["status"] == "cancelled" and metrics["failure"]["stage"] == "user_cancellation"
    checks["terminal_receipt"] = {"status": "PASS", "runtime_status": state["runtime_state"]["status"],
        "top_level_execution_status": state["execution_status"], "metrics_status": metrics["status"]}

    with (AUDIT / "request_ledger.tsv").open() as handle:
        ledger = list(csv.DictReader(handle, delimiter="\t"))
    assert len(ledger) == 79
    known = [row for row in ledger if row["total_tokens"] != ""]
    assert len(known) == 77 and sum(int(row["total_tokens"]) for row in known) == 3071038
    assert len({row["provider_attempt_id"] for row in ledger if row["provider_attempt_id"]}) == 72
    checks["exported_request_ledger"] = {"status": "PASS", "rows": 79, "known_usage_rows": 77, "unique_observed_attempt_ids": 72}
    science = read_json(AUDIT / "evidence_science.json")["assertions"]
    assert science["status"] == "PASS" and science["scientific_validity_verified"] is False
    compaction = read_json(AUDIT / "compaction_evidence.json")
    assert compaction["applied_evidence"]["activated_transactions"] == 0
    replay = compaction["pure_schema_replay"]
    assert [item["replayed_error"]["error_code"] for item in replay if "replayed_error" in item] == [
        "CONTEXT_SCHEMA_UNKNOWN_FIELD", "CONTEXT_FIELD_REQUIRED", "CONTEXT_FIELD_REQUIRED"]
    assert any(item.get("asserted_error_condition") == "COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE" for item in replay)
    exit_cases = read_json(AUDIT / "evidence_wait.json")["offline_exit_witness"]["cases"]
    assert [case["matched_existing_exit_predicate"] for case in exit_cases] == [True, False, False, False]
    checks["specialist_offline_witness_receipts"] = {"status": "PASS", "science": science,
        "compaction": "3 actual pure-parser failures and 1 selected/mode predicate match historical rejection",
        "exit": "existing AST condition reproduces trailing LF/CRLF failure; not a repaired behavior test"}

    changed = []
    manifest = read_json(AUDIT / "source_manifest.json")
    for item in manifest["files"]:
        path = Path(item["path"])
        st = path.stat()
        if (st.st_size != item["size"] or st.st_mtime_ns != item["mtime_ns"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]):
            changed.append(str(path))
    assert not changed, changed
    checks["original_evidence_unchanged"] = {"status": "PASS", "files": len(manifest["files"]), "checks": "bytes, size, mtime_ns; not atime"}

    before = read_json(AUDIT / "protection_before.json")
    changed = []
    for name, digest in before["protected_python_hashes"].items():
        if hashlib.sha256((REPO / name).read_bytes()).hexdigest() != digest:
            changed.append(name)
    assert not changed, changed
    index = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=REPO)
    # Before staging this audit, the whole index must match; after staging or
    # committing, audit paths may differ, so check all non-audit metadata instead.
    status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=REPO).decode()
    audit_relative = str(AUDIT.relative_to(REPO))
    initial_status = [s for s in before["status_porcelain"].splitlines() if audit_relative not in s]
    current_status = [s for s in status.splitlines() if audit_relative not in s]
    assert initial_status == current_status, "Existing worktree/index status changed outside audit"
    checks["existing_worktree_preserved"] = {"status": "PASS", "python_files": len(before["protected_python_hashes"]),
        "non_audit_status_unchanged": True, "whole_index_equals_pre_audit": hashlib.sha256(index).hexdigest() == before["index_sha256"]}

    scripts = sorted(AUDIT.glob("*.py"))
    for script in scripts:
        ast.parse(script.read_text(), filename=str(script))
    checks["audit_script_syntax"] = {"status": "PASS", "files": [s.name for s in scripts]}
    result = {"status": "PASS", "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "verification_kind": "historical_receipt_and_protection_checks_only", "checks": checks,
              "real_model_calls": 0, "real_scientific_calls": 0, "production_tests_run": False}
    (AUDIT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
