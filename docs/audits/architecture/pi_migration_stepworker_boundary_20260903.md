# PI-MIG-01: replaceable StepWorker boundary

Date: 2026-09-03

## Implementation status

PI-MIG-01 is implemented as an explicit opt-in path. Pi is not installed or
called. The default production path remains `legacy`.

Repository snapshot:

- Branch: `refactor/consultant-controller-authority`
- Base HEAD: `6455bc712ecfda7359ce42dbfb6e47a252030ace`
- Base subject: `test(execute): cover format-repair reentry before command launch`
- Pre-existing tracked delta: the MIG-01 GraphBubbleUp fix in
  `bio_agents.py`, `agent_core/middleware.py`, and its regression in
  `tests/test_plan_local_preaction_convergence_v2.py`; isolated as commit
  `30aa0ce` (`fix(graph): preserve GraphBubbleUp control signals`).
- Pre-existing untracked delta: 30 `git status --short` entries, including the Phase 0/1 audit and
  route-authority matrix. None was deleted, cleaned, stashed, or overwritten.
- PI-MIG-01 boundary commit: this document is included in the isolated
  StepWorker commit; its final hash is recorded in the acceptance handoff.

## Before graph

Default and only path before this slice:

```text
Plan Review
  -> Generate
  -> Execute
  -> Supervisor
```

Runtime routing after Generate was authored through the legacy Controller
transition pipeline and its route fields.

## After graph

Default behavior is unchanged:

```text
BIOMIND_STEP_WORKER_BACKEND=legacy (default)

Plan Review -> Generate -> Execute -> Supervisor
```

The new opt-in path is:

```text
BIOMIND_STEP_WORKER_BACKEND=typed_stub

Plan Review
  -> step_worker
  -> StepOutcome.type
       completion_claimed -> supervisor
       needs_replan       -> planner
       blocked            -> END
       harness_error      -> END
       cancelled          -> END
```

`step_worker` is registered directly and deliberately does not pass through
`_profile_node()`. Therefore its result is not submitted to
`_apply_transition_guard()` or
`_resolve_and_commit_effective_transition()`. Its conditional edge calls only
`route_step_worker_outcome()` and never `_dispatch_effective_route()`.

## StepRequest contract

`StepRequest` is a frozen, versioned value object with a JSON-serializable
`to_dict()` representation. Nested mappings and sequences are recursively
frozen before they reach a backend.

Fields:

```text
schema_version
task_id
plan_id
plan_version
step_id
step_goal
completion_contract
selected_skill_ids
resolved_inputs
accepted_bindings
available_artifacts
workspace
attempt_ordinal
```

Authority sources:

- Plan and cursor identity: durable approved PlanState.
- Step semantics: immutable approved PlanSnapshot when available; otherwise
  `current_plan.plan_spec`, then the legacy approved PlanState compatibility
  representation.
- Task inputs: canonical current TaskSpec plus the approved per-step artifact
  visibility projection.
- Bindings: accepted PlanBinding records only.
- Workspace: BioMind's configured `data_path`; the backend cannot select it.
- Graph state is used only as a stale-scope rejection check. It cannot supply
  missing request semantics.

Missing approval, an empty/ambiguous/inactive cursor, or a graph/durable scope
mismatch raises `STEP_REQUEST_SCOPE_INVALID`; no step is guessed.

The contract contains no Controller route fields, recovery decision identity,
or mutable AgentState.

## StepOutcome contract

`StepOutcome` is also frozen, versioned, recursively immutable, and has a
JSON-serializable `to_dict()` representation.

Allowed types and semantics:

| Type | Meaning | BioMind destination |
|---|---|---|
| `completion_claimed` | Worker claims the attempt is complete; this is not verification PASS. | `supervisor` |
| `needs_replan` | The approved step/plan cannot continue. | `planner` |
| `blocked` | Scientific work cannot currently continue; a structured blocker is required. | `END` |
| `harness_error` | Worker/backend/runtime failed, not the scientific task. | `END` |
| `cancelled` | Controlled user/external termination. | `END` |

Unknown types, missing required fields, extra fields (including route fields),
and invalid blocker shapes are rejected as
`STEP_WORKER_OUTCOME_PROTOCOL_ERROR`. The production node converts that
boundary error into a typed terminal `harness_error`; it never falls back to
Generate, Execute, Recovery, or an old route field.

## StepWorker backend

The backend interface is:

```python
class StepWorkerBackend(Protocol):
    async def run_step(self, request: StepRequest) -> StepOutcome: ...
```

It receives only `StepRequest`, never AgentState or PlanState. Backend selection
uses `BIOMIND_STEP_WORKER_BACKEND`:

- `legacy`: default; no typed backend is installed and the existing graph runs.
- `typed_stub`: explicit offline-only path; tests may inject any conforming
  backend. Its uninjected safe default returns a terminal harness error.

No `pi` backend exists in this slice.

## Routing authority and compatibility

The StepWorker node writes only:

```text
step_worker_request
step_worker_outcome
step_worker_attempt_ordinal
step_worker_route_audit
```

The audit projection records:

```text
route_authority = STEP_WORKER_OUTCOME
legacy_route_fields_authoritative = false
```

Legacy fields remain in AgentState for old nodes and checkpoints. Tests corrupt
`next_step`, `proposed_route`, `effective_route`, `dispatch_route`,
`route_resolution.dispatch_route`, `required_next_action`, and
`pending_recovery_action` simultaneously. A `completion_claimed` outcome still
enters Supervisor in an actual offline LangGraph graph.

## Tests

New tests in `tests/test_step_worker_boundary.py` cover:

- authoritative StepRequest construction and JSON serialization;
- deterministic serialization for equivalent authoritative state;
- top-level and nested immutability;
- all five StepOutcome types;
- rejection of node names, every known route field, non-mapping payloads, and
  incomplete mappings returned by a backend;
- fail-closed stale step scope;
- actual LangGraph completion, replanning, and harness-error edges;
- simultaneous legacy-route corruption;
- default legacy selection and explicit typed opt-in;
- fail-closed rejection of an unknown backend configuration;
- production StepWorker handler bypass of all three Controller routing methods;
- zero model/provider calls;
- protocol-error and backend-exception conversion to terminal harness error;
- controlled cancellation remaining distinct from harness error;
- GraphBubbleUp identity-preserving re-raise through the typed worker node.

Focused regression command also includes the existing GraphInterrupt real graph
test and Generate pre-action corrective-loop tests. The focused set completed:

```text
Ran 16 tests in 18.168s
OK
```

`py_compile` passed for the new workflow module and every modified Python file.
No real model/API, Pi, snpEff, Java workflow, or benchmark was run.

## Known limitations

- `typed_stub` is an offline seam, not a useful scientific worker.
- `completion_claimed` reaches the existing Supervisor, whose downstream route
  is still legacy Controller territory. PI-MIG-01 transfers only the new
  StepWorker outcome edge.
- StepWorker attempts are checkpoint-visible in AgentState but do not yet have
  a dedicated append-only attempt ledger.
- Existing Generate, Execute, Recovery, Planner, bindings, skills, prompts, and
  managed-artifact semantics are intentionally unchanged.

## Next migration slice

PI-MIG-02: Pi RPC proof of concept. It should implement a fixed-version Pi RPC
backend behind this same contract, using a fake/custom terminating tool and
session/resume tests. It must not widen StepOutcome or return graph node names.
