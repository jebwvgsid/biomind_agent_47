# Batch 7 Completion Gate Migration

## Evidence APIs

CompletionGate now exposes three side-effect-free evidence builders:

- `validate_dependencies(plan_state, step_id, stage_id)`
- `validate_step_evidence(plan_state, step_id)`
- `validate_task_evidence(plan_state)`

They do not mutate a cursor, step/task status, retry count or route. RecoveryPolicy consumes dependency evidence; Controller applies the resulting decision through PlanTracker.

## Step Evidence

Step completion binds the current output heads to:

- producer attempt IDs;
- artifact version IDs;
- validation IDs;
- plan version.

Stale and invalid outputs fail. Renderer composite completion additionally requires all declared manifest stages to be complete.

## Task Evidence

`pending_retry`, `waiting_upstream`, `invalidated`, `running`, and other non-success statuses keep `task_complete=false`. Stale or invalid active artifact heads also keep the task incomplete even if old files remain on disk.

## Legacy Gate Compatibility

Existing semantic/file checks remain. Explicit stale/invalid provenance is rejected before filesystem checks; `expected` or merely `materialized` records continue through the existing reconciliation path so older plans preserve their diagnostic classifications. Hard-coded semantic checks return evidence only and do not own recovery routing.

