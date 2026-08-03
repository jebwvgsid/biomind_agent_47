# Batch 8.1 Completion Report

## 1. Commit Closure

Batch 8.1 closes the previously broken import boundary around `context_budget`,
`recovery`, `root_cause`, and `knowledge_conflicts`, together with the matching
PlanTracker, execution, runtime, Renderer, configuration, and focused-test
dependencies. The commit identifier is reported by `git rev-parse HEAD` after
this report is committed; no `.env`, user preference, memory archive, task
output, or zip archive is part of the commit.

A Git index tree was archived into a new `/tmp` directory before commit. From
that isolated tree, `PromptManager`, `BioAnalystAgent`, and all four closure
modules imported successfully. The same tree passed 414 focused tests.

## 2. Script Source-of-Truth

`BioAnalystAgent._persist_script_version()` writes immutable script versions
atomically and records path, SHA-256, producer, creation time, parent reference,
and mutation reason. `_bind_pending_action()` persists the Generate candidate.
Execute then allocates an Attempt and persists separate approved and executed
versions. The executed version is a durable wrapper with per-Attempt stdout,
stderr, and atomic exit-status paths.

The normal lineage is:

```text
generated (producer=generate)
  -> approved (producer=hitl_approval, parent=generated)
  -> executed (producer=executor_wrapper, parent=approved)
  -> run_script_path(executed.path, expected_sha256=executed.sha256)
```

Executor rejects hash drift with exit code 126. Root-cause analysis receives the
actual executed wrapper path, while the approved script remains available as
the parent ActionContract evidence. Fixed `temp_script.sh` and `temp_script.R`
paths are no longer used.

## 3. Durable Attempt State Machine

PlanTracker is the only durable Attempt writer. Supported states are:

```text
CREATED -> SCRIPT_READY -> APPROVED -> STARTING -> RUNNING
        -> PROCESS_EXITED -> EVIDENCE_COMMITTED -> VALIDATED -> COMPLETED
```

`FAILED` and `UNKNOWN_AFTER_CRASH` are explicit terminal/reconciliation states.
Invalid transitions fail closed, and a step cannot allocate a second active
Attempt. Attempt IDs are stable per Action sequence and do not depend on message
count.

## 4. Resume Behavior

`classify_attempt_resume()` and `_resume_durable_attempt_update()` implement the
resume contract:

- a live PID is monitored instead of rerun;
- durable exit status resumes at Supervisor;
- committed ValidationEvidence resumes at the transition guard;
- missing process and exit evidence becomes `UNKNOWN_AFTER_CRASH`;
- old-plan unstarted Attempts are not executed after a plan-version change;
- terminal task state wins over stale Attempt residue.

The recovery test constructs a RUNNING Attempt using a live PID and confirms
`monitor_running_attempt`; another progresses through `PROCESS_EXITED` and
`VALIDATED` and confirms Supervisor then transition-guard routing without a new
Attempt.

## 5. Unified Recovery Decision Application

The active middleware creates incidents, evidence, and proposed decisions but
does not mutate PlanTracker. `_apply_transition_guard()` is the sole runtime
caller of `apply_recovery_decision()`. PlanTracker applies the decision,
increments any permitted budget once, and persists it atomically.

Each committed decision records `created`, `validated`, and `applied` audit
events. Stale plan versions and conflicting decisions for an already committed
incident record `rejected`. Replaying the same decision is idempotent and does
not consume another retry. `RECONSULT` routes to Consultant and `REVALIDATE`
routes to Supervisor.

## 6. Plan/Transition Invariants

`plan_id`, `plan_version`, and `plan_schema_version` are separate fields.
Generation Attempts and Recovery decisions bind task, plan, plan version, step,
Action, and Attempt identity. Normal validation advances through the transition
guard; only a PLAN-owned patch can route to Planner. Internal protocol failures
cannot become user questions.

## 7. Redaction

ContextLineage's shared redaction entry now covers API/Authorization values,
cookies, URL credentials and query secrets, password/token fields, cloud-style
credentials, environment assignments, and SSH private-key blocks. Recovery,
root-cause, diagnostic, conflict, and JSON persistence paths reuse this entry.
Redaction applies to persisted evidence and previews only; a regression test
confirms it never modifies the actual executed script.

## 8. Changed Files and Reused Components

Key implementation anchors:

- `bio_agents.py`: `_resolve_resume_node()` (line 1631),
  `_resume_durable_attempt_update()` (1765), `_persist_script_version()` (3114),
  `_bind_pending_action()` (3170), `_apply_transition_guard()` (8539).
- `agent_core/plan_tracker.py`: `active_attempt()` (552),
  `classify_attempt_resume()` (572), `record_provenance_event()` (633), and
  `apply_recovery_decision()` (1197).
- `execution/executor.py`: `run_script_path()` (644).
- `agent_core/recovery.py`: `ExecutionRecord` (489) and idempotent
  `save_execution_record()` (2791).
- `agent_core/context_lineage.py`: shared `redact_payload()` (159) and
  `redact_text()` (164).

The implementation reuses PlanTracker, ExecutionRecord, RecoveryPolicy,
RecoveryStateStore, Renderer manifest handling, and the existing transition
guard. It does not add a RecoveryManager, PlanController, or extra LLM call.

## 9. Tests and Results

Pre-commit working-tree checks:

```text
Focused Batch 8.1/executor/PlanTracker/Recovery tests: 66 passed
Clean index-tree import check: passed
Clean index-tree Batch 5-8 + Batch 8.1 regression: 414 passed
Python compile checks: passed
git diff --cached --check: passed
```

The clean-tree test excludes all unstaged files, proving that the committed
closure does not rely on accidental workspace state.

## 10. Remaining Risks Before Batch 9

- A crash between process creation and the first PID/PGID checkpoint can only be
  classified as unknown; the system intentionally refuses a blind rerun.
- Legacy records without plan-version or script hashes remain readable but are
  marked as legacy/unknown evidence rather than promoted to exact lineage.
- This batch does not implement Batch 9 Level 1-4 recovery reasoning.
- The repository still contains unrelated unstaged skill, benchmark, and
  documentation work. It is intentionally excluded from this commit and is not
  reverted.
