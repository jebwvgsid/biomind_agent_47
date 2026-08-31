# Batch 7 Manifest Stage Rewind

`REWIND_MANIFEST_STAGE` changes the stage cursor inside one Renderer-backed Plan step. It does not create a Plan step and does not route to Planner.

## Preconditions

- manifest, failed stage and target stage exist;
- failed and target stages belong to the same manifest;
- target is the failed stage or a real stage ancestor;
- the stage DAG is acyclic;
- referenced artifact IDs belong to the current task.

## Transition

- Plan `current_step_id` remains the composite Renderer step.
- Target stage becomes `retry_pending`.
- Target stage descendants become `invalidated`.
- `renderer_execution.completed_action_ids` drops target and descendant actions.
- `renderer_execution.current_action_id` and `current_stage_id` move to the target.
- Stage artifacts are marked stale; explicitly proven bad artifacts are invalid.
- `stage_rewind_count` is consumed once.

The next manifest action is resolved by the existing manifest runner from the updated `renderer_execution`; Generate does not invent a stage name.

## Safety

Cross-manifest and non-ancestor rewinds raise before state commit. The state update uses the same deep-copy/idempotent `apply_recovery_decision()` path as Plan-step rollback.

