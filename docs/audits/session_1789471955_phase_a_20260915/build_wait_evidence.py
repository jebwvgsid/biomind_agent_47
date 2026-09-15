"""Read fixed historical audit evidence; no application import or network call."""
from pathlib import Path
import ast
import collections
import csv
import datetime as dt
import hashlib
import json
import re

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]
ROOT = REPO.parent
BASE = ROOT / "benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955"
LOG = REPO / "logs/biomind_1030526.log"
TZ = dt.timezone(dt.timedelta(hours=8))


def time(value):
    result = dt.datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=TZ)


def json_file(path):
    return json.loads(path.read_text())


def json_lines(path):
    return [(i, json.loads(s)) for i, s in enumerate(path.read_text().splitlines(), 1)]


events = json_lines(BASE / "context_trace/events.jsonl")
metrics = json_file(BASE / "run_metrics.json")
state = json_file(BASE / "plan_state.json")
log_lines = LOG.read_text().splitlines()
attempts, approval_events, log_points = {}, collections.defaultdict(dict), []
for line, content in enumerate(log_lines, 1):
    if re.match(r"^2026-", content):
        stamp = dt.datetime.strptime(content[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=TZ)
        log_points.append((stamp, line, content[26:160]))
    if "PROVIDER_ATTEMPT_EVENT " in content:
        payload = json.loads(content.split("PROVIDER_ATTEMPT_EVENT ", 1)[1])
        attempts[payload["attempt_id"]] = (line, payload)
    if "approval_ui " in content:
        payload = json.loads(content.split("approval_ui ", 1)[1])
        approval_events[payload["request_id"]][payload["phase"]] = (stamp, line, payload)

rows = []


def add(kind, label, start, end, seconds=None, source="", detail=""):
    start, end = time(start) if isinstance(start, str) else start, time(end) if isinstance(end, str) else end
    rows.append(dict(kind=kind, label=label, start=start.astimezone(TZ).isoformat(),
                     end=end.astimezone(TZ).isoformat(), seconds=round(seconds if seconds is not None else (end-start).total_seconds(), 6),
                     source=source, detail=detail))


provider_rows = []
for line, payload in attempts.values():
    keep = ["attempt_id", "call_id", "node_name", "request_started_at", "attempt_completed_at", "state",
            "time_to_first_event_seconds", "time_to_first_content_seconds", "max_payload_gap_seconds", "total_elapsed_seconds",
            "content_character_count", "reasoning_character_count", "observed_payload_callback_count", "tool_call_fragment_count",
            "late_chunk_count", "usage_status", "failure_type", "cancel_requested_at", "cancel_completed_at", "cleanup_result",
            "stream_close_supported", "stream_close_requested_at", "stream_close_completed_at", "completion_marker_seen", "timeout_policy"]
    record = {key: payload.get(key) for key in keep}
    record["log_line"] = line
    provider_rows.append(record)
    add("provider_attempt", payload["call_id"], payload["request_started_at"], payload["attempt_completed_at"],
        source=f"logs/biomind_1030526.log:{line}",
        detail=f"node={payload['node_name']};state={payload['state']};first_callback={payload.get('time_to_first_event_seconds')};max_payload_gap={payload.get('max_payload_gap_seconds')}")

compaction_groups = collections.defaultdict(list)
tool_starts = {}
tools = []
for line, event in events:
    payload = event["payload"]
    if event["event_kind"].startswith("recovery_compaction_"):
        compaction_groups[payload["compaction_request_id"]].append((line, event))
    if event["event_kind"] == "recovery_tool_started":
        tool_starts[(payload["seed_id"], payload["tool_request_id"])] = (line, event)
    if event["event_kind"] == "recovery_tool_completed":
        before_line, before = tool_starts[(payload["seed_id"], payload["tool_request_id"])]
        add("recovery_tool", payload["tool"], before["timestamp"], event["timestamp"],
            source=f"context_trace/events.jsonl:{before_line}-{line}", detail=payload["tool_request_id"])
        tools.append(rows[-1])

groups = []
for key, group in compaction_groups.items():
    first_line, first = group[0]
    last_line, last = group[-1]
    started = sum(event["event_kind"] == "recovery_compaction_started" for _, event in group)
    completed = sum(event["event_kind"] == "recovery_compaction_completed" for _, event in group)
    cancelled = sum(event["event_kind"] == "recovery_compaction_cancelled" for _, event in group)
    add("compaction_envelope_nested", key, first["timestamp"], last["timestamp"],
        source=f"context_trace/events.jsonl:{first_line}-{last_line}",
        detail=f"started={started};completed={completed};cancelled={cancelled};contains provider intervals; do not add twice")
    groups.append({**rows[-1], "started": started, "completed": completed, "cancelled": cancelled})

for request_id, phases in approval_events.items():
    if "input_ready" not in phases or "decision_received" not in phases:
        continue
    start, line1, payload = phases["input_ready"]
    end, line2, _ = phases["decision_received"]
    add("human_input", f"request_{request_id}:{payload['stage']}", start, end,
        source=f"logs/biomind_1030526.log:{line1}-{line2}",
        detail="input_ready to decision_received; request 1 predates task; requests 9/10 follow task cancellation")

for line, execution in json_lines(BASE / "executions/execution_records.jsonl"):
    add("command", execution["step_id"], execution["started_at"], execution["finished_at"], execution["duration_seconds"],
        source=f"executions/execution_records.jsonl:{line}",
        detail="timestamps only second precision; seconds column uses recorded monotonic duration; " + execution["execution_id"])

major_gaps = []
for before, after in zip(log_points, log_points[1:]):
    seconds = (after[0]-before[0]).total_seconds()
    if seconds < 10 or before[0] < time(metrics["started_at"]):
        continue
    overlap = [p["call_id"] for p in provider_rows if time(p["request_started_at"]) <= before[0] and time(p["attempt_completed_at"]) >= after[0]]
    human = [str(k) for k, p in approval_events.items() if "input_ready" in p and "decision_received" in p
             and p["input_ready"][0] <= before[0] and p["decision_received"][0] >= after[0]]
    kind = "log_gap_provider_active" if overlap else "log_gap_human_wait" if human else "log_gap_unattributed_local"
    add(kind, f"log_lines_{before[1]}_{after[1]}", before[0], after[0],
        source=f"logs/biomind_1030526.log:{before[1]}-{after[1]}", detail=";".join(overlap or human) or "no in-flight provider evidenced; cause not observable")
    major_gaps.append(rows[-1])

cancel = next({"line": line, **event} for line, event in events if event["event_kind"] == "task_user_cancelled")
router_dir = BASE / "context_trace/task_router-0079-fe0d6a64b302"
router = json_file(router_dir / "manifest.json")
submission_ref = "state_contract/objects/b3a4853b067ae72a0799eb4fe28f769270e2fa3a53de23427b0c6ee4d23399f7.json"
submission = json_file(BASE / submission_ref)["payload"]["text"]

# Evaluate only the exact exit-condition AST extracted from source. Do not import BioMind.
source = (REPO / "bio_agents.py").read_text()
tree = ast.parse(source)
predicate = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and "user_input.strip().lower() in {'/exit'" in ast.unparse(node.test))
compiled = compile(ast.Expression(predicate.test), "offline_exit_predicate", "eval")
witness = [{"input": text, "matched_existing_exit_predicate": eval(compiled, {"__builtins__": {}}, {"user_input": text})}
           for text in ["/exit", "/exit\n", "/exit\r\n", "task text\n/exit"]]
assert witness[0]["matched_existing_exit_predicate"] is True
assert all(row["matched_existing_exit_predicate"] is False for row in witness[1:])
assert submission == "/exit\n"

summary_keys = ["total_wall_seconds", "agent_active_seconds_excluding_human_wait", "human_confirmation_wait_seconds",
                "command_execution_seconds", "llm_call_seconds", "analysis_planning_overhead_seconds", "commands", "llm_calls", "manual_confirmations"]
evidence = {
    "schema_version": 1, "task_id": "session_1789471955", "timezone": "Asia/Shanghai (+08:00)",
    "scope": "historical read-only evidence; no application imports, model calls, or scientific execution",
    "metrics_summary": {k: metrics["summary"][k] for k in summary_keys},
    "metrics_residual_wall_seconds": round(metrics["summary"]["total_wall_seconds"]-metrics["summary"]["human_confirmation_wait_seconds"]-metrics["summary"]["command_execution_seconds"]-metrics["summary"]["llm_call_seconds"], 6),
    "accounting_note": "Metrics durations are the existing profiler convention, not a mutually exclusive CPU/IO partition. Provider attempts, compaction envelopes, node durations and log gaps overlap. Do not add interval rows together.",
    "provider_attempts": provider_rows, "compaction_groups": groups, "recovery_tools": tools, "major_log_gaps": major_gaps,
    "cancel_event": cancel,
    "durable_state": {"updated_at": state["updated_at"], "state_generation": state["state_generation"],
                      "plan_status": state["plan_status"], "execution_status": state["execution_status"],
                      "runtime_status": state["runtime_state"]["status"], "current_step_id": state["current_step_id"]},
    "post_cancel_router": {"call_id": router["call_id"], "timestamp": router["timestamp"], "completed_at": router["completed_at"],
                           "status": router["status"], "response": json_file(router_dir / "response.json"),
                           "raw_submission_ref": submission_ref, "raw_submission": submission,
                           "actual_request_message_count": len(json_file(router_dir / "request.json")["messages"]),
                           "request_assembly": "triage joins all durable user turns with two newlines; original task plus second turn /exit\\n"},
    "offline_exit_witness": {"source": "bio_agents.py", "line": predicate.lineno, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                             "predicate": ast.unparse(predicate.test), "cases": witness},
    "limits": ["No OS exit status, shell return marker, terminal-close receipt or process-tree cleanup receipt was located.",
               "No provider attempt identity/usage accompanies the post-cancel router; HTTP 200 timing alone is not a provider-id join.",
               "CLEANUP_CONFIRMED proves the recorded provider adapter cleanup result; stream_close_supported=false prevents claiming remote generation stopped.",
               "Context-trace events are selective; absence in that stream alone is not a wait or deadlock."]
}
(OUT / "evidence_wait.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
with (OUT / "wait_intervals.tsv").open("w", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda row: row["start"]))
print(json.dumps({"interval_count": len(rows), "provider_attempts": len(provider_rows), "compaction_groups": groups,
                  "tool_total_seconds": round(sum(t["seconds"] for t in tools), 6), "tool_max_seconds": max(t["seconds"] for t in tools),
                  "provider_max_seconds": max(p["total_elapsed_seconds"] for p in provider_rows),
                  "provider_compaction_seconds": round(sum(p["total_elapsed_seconds"] for p in provider_rows if p["node_name"] == "context_compaction"), 6),
                  "exit_witness": witness}, ensure_ascii=False))
