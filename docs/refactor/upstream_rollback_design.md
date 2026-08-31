# Batch 7 Upstream Rollback Design

## Authority

`RecoveryPolicy` selects a recovery kind from structured dependency evidence. It does not mutate state. `PlanTracker.apply_recovery_decision()` is the only durable rollback writer. Controller and middleware project the committed `current_step_id` and `current_stage_id` into graph state; they do not select another target.

The implementation is in:

- `agent_core/recovery.py`: `RecoveryDecision`, `RecoveryPolicy.decide()`.
- `agent_core/plan_tracker.py`: `_apply_upstream_step_rollback()` and `apply_recovery_decision()`.
- `bio_agents.py`: the pre-execution DependencyGate.

## Deterministic Selection

`DependencyEvidence.rollback_candidates` is emitted only when a stale or invalid required artifact has a different producer step. RecoveryPolicy selects `ROLLBACK_UPSTREAM_STEP`. Missing or unknown producers select `PLAN_PATCH_REQUEST`; an invalid dependency in the same manifest selects `REWIND_MANIFEST_STAGE`.

## Atomic Transition

For `producer -> failed consumer -> descendants`, one deep-copy transaction:

1. validates task, plan, plan version, ancestor/provenance relation, cycle freedom and artifact ownership;
2. consumes `upstream_rollback_count`;
3. sets producer to `retry_pending`;
4. sets failed consumer to `waiting_upstream`;
5. sets affected descendants to `invalidated` without touching unrelated DAG branches;
6. invalidates or stales associated artifact versions and producer attempts;
7. moves the real plan cursor to the producer;
8. appends one recovery audit event;
9. swaps the complete updated mapping into the caller state.

Files are retained for audit. Their presence does not restore validity.

## Resume

`decision_id`/`idempotency_key` is stored in `applied_decision_ids`. Replaying a checkpoint returns `duplicate=true`, does not move the cursor again, and does not consume budget again. When a repaired upstream step passes, only directly ready descendants are released; deeper invalidated descendants wait until their own dependencies pass.

