# Batch 6 Execution Provenance Model

## Scope

Batch 6 adds durable execution lineage without adding a second state machine. `PlanTracker` remains the only persistent control-plane truth. Full command streams remain in `executions/`; `plan_state.json` stores bounded records and references.

## Identity Chain

```text
plan_id
  -> step_id
    -> action_id
      -> attempt_id / execution_id
        -> artifact_version_id

manifest_id
  -> stage_id
    -> action_id
      -> attempt_id
```

- `plan_id` is stable for one task; `plan_version` identifies revisions.
- `action_id` identifies one bound, normalized action.
- `attempt_id` identifies one execution of that action. `execution_id` is attempt-scoped.
- `artifact_id` is a stable logical identity namespaced by task, producer step, logical role and normalized path.
- `artifact_version_id` identifies one materialization by one producer attempt.
- `manifest_id` derives from the Renderer manifest fingerprint.
- `stage_id` derives from `manifest_id + manifest_action_id` and is not a Plan step ID.

## Persistent State

Plan schema v4 stores `provenance_state` with:

```text
actions, attempts, artifacts, artifact_heads,
manifests, stages, validations,
attempt_counters, migration_log
```

`record_provenance_event()` is the sole mutation entry and accepts:

- `action_bound`
- `attempt_started`
- `execution_finished`
- `manifest_registered`
- `validation_recorded`

All writes are idempotent. Rebinding preserves the original raw action, while later normalized/executed representations are updated separately.

## Artifact State

An artifact starts as `expected`. A successful producer attempt may advance it to `materialized` only when:

1. the artifact is declared by the Plan or Renderer contract;
2. the executed record explicitly reports its path;
3. its post-execution signature differs from the pre-execution snapshot;
4. the action is not a probe, unless the contract explicitly permits probe production.

Associated ValidationEvidence advances the exact attempt version to `valid` or `invalid`. File existence alone never means valid.

## Eight Completion Questions

1. **Is the plan identity stable?** Yes. `plan_id` survives save/load and revisions use `plan_version`.
2. **Is there one provenance writer?** Yes. PlanTracker owns mutations through `record_provenance_event()`.
3. **Are actions and retries distinguishable?** Yes. One action may have multiple stable, independently recorded attempts.
4. **Are overwritten files versioned?** Yes. The logical ID remains stable and `artifact_heads` advances to a new attempt version.
5. **Can probes claim formal outputs?** No, unless `allow_probe_producer` is declared.
6. **Is validation attached to the correct producer?** Yes. Validation updates only artifact versions produced by its attempt.
7. **Are Renderer stages distinct from Plan steps?** Yes. Stage attempts carry both `step_id` and `stage_id`.
8. **Can old sessions be queried safely?** Yes. Migration is idempotent, preserves unknown producers, and corrects legacy probe role pollution without inventing manifests.

