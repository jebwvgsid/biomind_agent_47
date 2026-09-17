"""Read-only incident capture; never import BioMind or rewrite the session.

Usage: python failure_capture.py SESSION SNAPSHOT_DIR EVIDENCE_JSON
The isolated witness consumes an exact JSON copy in SNAPSHOT_DIR; the small
durable evidence records hashes and the fields supporting this audit.
"""
import hashlib
import json
from pathlib import Path
import stat
import sys


def metadata(path):
    info = path.lstat()
    return {"path": str(path), "size": info.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "device": info.st_dev, "inode": info.st_ino,
            "regular_file": stat.S_ISREG(info.st_mode)}


def capture(session, snapshot, destination):
    snapshot.mkdir(parents=True, exist_ok=False)
    sources = [session / "plan_state.json", session / "executions/execution_records.jsonl"]
    state = json.loads(sources[0].read_text())
    task = state["task_state"]
    stop = task["managed_run_stop"]
    attempt = state["provenance_state"]["attempts"][stop["scope"]["attempt_id"]]
    failure_path = session / "attempts" / attempt["attempt_id"] / "failure_evidence.json"
    sources.extend([failure_path, session / "attempts" / attempt["attempt_id"] / "exit_status.json"])
    for source in sources:
        (snapshot / source.name).write_bytes(source.read_bytes())
    record = json.loads(sources[1].read_text().splitlines()[0])
    failure = json.loads(failure_path.read_text())
    packet = task["causal_failure_packets"][task["active_failure_packet_ref"]]
    reports = []
    for name, receipt in stop["report_receipts"].items():
        path = Path(receipt["path"])
        actual = metadata(path)
        publication = task["report_publications"][receipt["operation_id"]]
        body_ref = publication["body_ref"]
        body_path = session / "state_contract/objects" / (body_ref.split(":")[-1] + ".json")
        body_object = json.loads(body_path.read_text())
        reports.append({"name": name, "actual": actual, "receipt": receipt,
            "publication": publication, "body_object": metadata(body_path),
            "body_object_type": body_object["object_type"],
            "hash_matches": actual["sha256"] == receipt["sha256"] == publication["sha256"],
            "ownership_matches": actual["device"] == receipt["ownership"]["device"] and actual["inode"] == receipt["ownership"]["inode"],
            "inode_matches": actual["inode"] == receipt["ownership"]["inode"],
            "device_matches": actual["device"] == receipt["ownership"]["device"],
            "durable_ownership_chain_consistent": publication["receipt"]["ownership"] == publication["prepared"] == receipt["ownership"] and publication["receipt"]["operation_id"] == receipt["operation_id"],
            "owner_matches": publication["owner"] == stop["stop_id"] and receipt["task_id"] == state["task_id"],
            "proof_file_exists": path.with_name(receipt["ownership"]["proof_name"]).exists(),
            "body_matches_frozen": path.read_text() == stop["frozen_report_bodies"][name]})
    empty = session / "plan_artifacts/00.input_check/vcf.samples.txt"
    baseline = {str(p): metadata(p) for p in sorted(session.rglob("*")) if p.is_file() and not p.is_symlink()}
    baseline.update({r["actual"]["path"]: r["actual"] for r in reports})
    (snapshot / "protected_hashes.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n")
    evidence = {"session": str(session), "snapshot": str(snapshot), "source_files": [metadata(p) for p in sources],
        "task_id": state["task_id"], "plan_id": state["plan_id"], "plan_version": state["plan_version"],
        "current_step": state["current_step_id"], "steps": [{k: row.get(k) for k in ("step_id", "title", "status")} for row in state["steps"]],
        "commit_receipt_count": len(task.get("typed_step_commit_receipts") or {}),
        "attempt": {k: attempt.get(k) for k in ("attempt_id", "action_id", "status", "returncode", "execution_record_id", "managed_output_contracts")},
        "failure_evidence": failure,
        "execution_record": {k: record.get(k) for k in ("execution_id", "attempt_id", "exit_code", "process_status", "execution_status", "failure_code", "runtime_output_reconciliation")},
        "empty_sample_file": metadata(empty), "failure_packet": packet["failure_packet"], "failure_source": packet["failure_source"],
        "stop": {k: stop.get(k) for k in ("status", "scope", "origin", "stop_reason", "recovery_usage", "prior_completed_work", "delivery_status", "required_output_status", "scientific_completion", "internal_diagnostic_delivery", "summary_llm_calls")},
        "report_files": reports, "protected_file_count": len(baseline)}
    evidence["report_ownership_limit"] = ("Compare device and inode separately. Matching files and internally linked published receipts establish delivery; "
        "a device mismatch does not establish current writer re-entry safety. No original writer re-entry is attempted.")
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    capture(*(Path(arg).resolve() for arg in sys.argv[1:]))
