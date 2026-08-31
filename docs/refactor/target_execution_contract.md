# Target Execution Contract

This is a migration target, not a request to add a parallel state machine.

## Ownership Model

```text
TaskSpec + InputAudit
  -> Planner (approved PlanStep contract)
  -> Materializer (Renderer or free-form Generate)
  -> existing deterministic pre-action validation
  -> Executor (ExecutionEvidence)
  -> action validation evidence
  -> CompletionGate (step/task contract)
  -> RecoveryPolicy when validation or completion fails
  -> Controller transition
  -> PlanTracker durable commit
```

## PlanStep Contract

Each approved step should durably contain:

```json
{
  "step_id": "step-1",
  "step_type": "renderer_composite|atomic|validation|report",
  "skill_ids": ["canonical/skill-id"],
  "renderer_id": "canonical.renderer.id",
  "inputs": [{"role": "input_role", "path": "/absolute/path"}],
  "outputs": [{"artifact_id": "result", "path": "/absolute/path"}],
  "success_criteria": ["structured criterion"],
  "dependencies": [],
  "status": "pending|running|passed|blocked",
  "renderer_execution": {}
}
```

Planner owns these fields. Generate cannot rewrite them. A required change is a
`PLAN_PATCH_REQUEST`; only the Planner and PlanTracker patch path can apply it.

## Materialized Action Contract

Reuse `pending_action` instead of adding a new ActionContract class:

```json
{
  "action_id": "...",
  "step_id": "step-1",
  "type": "execute",
  "action_source": "renderer|manifest_runner|generate|recovery",
  "command_role": "renderer_invocation|primary|verification|diagnostic|recovery",
  "code": "...",
  "renderer_id": "...",
  "manifest_path": "...",
  "manifest_action_id": "...",
  "expected_outputs": [],
  "success_checks": [],
  "runtime_bindings": {},
  "task_revision": 1,
  "plan_version": "plan_vN"
}
```

Provenance is assigned by the materializer, never inferred from arbitrary command
text.

## Allowed Actions By Step State

| Step state | Allowed materialization |
|---|---|
| Renderer composite, manifest absent | Canonical Renderer invocation only; contract BLOCKED or PLAN_PATCH_REQUEST if required facts are missing |
| Renderer composite, manifest registered | Controller selects the next manifest action; Generate is bypassed |
| Atomic/free-form | Generate may emit one structured EXECUTE/ASK/BLOCKED action allowed by the step contract |
| Recovery probe authorized | One bounded diagnostic action carrying RecoveryPolicy provenance |
| Step evidence already sufficient | STEP_COMPLETION_CANDIDATE; CompletionGate validates it |

## Validation Layers

### Existing pre-action validation

Validates schema, revision, step binding, action source, role, Renderer/manifest
provenance, required inputs and safety policy. It returns accept/reject facts and
does not select another scientific route.

### Action validation (narrowed Supervisor)

Validates the latest action only: return code, validator output, deterministic
tool-log errors, action success checks and action-produced artifacts. LLM review
is advisory evidence and cannot change deterministic facts.

### CompletionGate

Validates step/task completion only: all required manifest actions, step success
criteria, final artifact contracts, blocked evidence and final execution/review
evidence. It must not diagnose tools or choose retries.

## Recovery Decision

Reuse `RecoveryDecision`. RecoveryPolicy outputs a semantic result such as:

```text
CONTINUE
FORMAT_REPAIR
RETRY_CURRENT_ACTION
RUN_DIAGNOSTIC_PROBE
PLAN_PATCH_REQUEST
RECONCILE_AND_CONTINUE
BLOCKED
ESCALATE
```

RecoveryPolicy does not write a graph node name. The Controller maps the semantic
decision to one transition and records `decision_owner` and `decision_reason`.

## Durable State Rule

PlanTracker is the only durable writer for:

- step/task status;
- current step;
- retry, repair and probe budgets;
- Renderer manifest registration and completed action IDs;
- artifact snapshots and evidence relations.

Graph state may carry projections, but a later node must not overwrite persisted
truth with an older projection.
