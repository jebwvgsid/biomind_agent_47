# CTRL-02 — Controller Runtime Migration

> Historical stage report. CTRL-03 subsequently activated the complete
> schema-2 graph for new tasks; see
> `controller_front_half_retirement_20260903.md`. Schema-1 compatibility
> statements below still apply to historical checkpoints.

Date: 2026-09-03

Branch: `refactor/consultant-controller-authority`

Starting HEAD: `803442c7fb4314ebe8341cec1118b3a32127fc2d`

## Result

CTRL-02 adds an isolated, integration-testable routing-schema-2 runtime segment:

```text
Generate -> Execute -> Supervisor -> CommitStep -> FinalAudit
               |             |             |
               +---------- Recovery <------+
```

Runtime nodes report strict semantic outcomes. Conditional edges in
`agent_core/workflow/runtime_graph.py` alone select destinations. The existing
production entry graph remains routing schema 1; global schema-2 activation is
deferred to CTRL-03.

## Contracts and wrapper

`agent_core/workflow/outcomes.py` defines frozen JSON contracts for:

- `GenerateOutcome.v1`
- `ExecuteOutcome.v1`
- `VerificationOutcome.v1`
- `CommitStepOutcome.v1`
- `RecoveryOutcome.v1`
- `FinalAuditOutcome.v1`

Unknown schema versions, outcome types, top-level fields, missing required
payload fields, non-JSON values, and structural route authority fail closed.
Route-field rejection is recursive.

`profile_typed_node` records profiler latency and validated outcome type. It
does not mutate PlanState or select destinations. `GraphBubbleUp` and
`asyncio.CancelledError` retain object identity; bounded ordinary exceptions
may become the node-specific `harness_error`.

## Routing schema semantics

`workflow_routing_schema_version` is sticky:

- schema 1: historical/missing checkpoints and the current production graph;
- schema 2: typed runtime outcomes and LangGraph conditional edges.

There is no automatic checkpoint migration and no `next_step`-to-outcome
adapter. Schema 2 does not read legacy route fields as fallback.

## Typed runtime graph

`build_schema2_runtime_graph` constructs the isolated segment and four explicit
CTRL-03 boundary nodes (`planner`, `consultant`, `ask_user`, and
`install_subagent`). Boundary nodes terminate the isolated graph. Conditional
routers are async wrappers around strict route tables, avoiding the deployed
LangGraph executor-shutdown deadlock observed with synchronous conditions in a
cyclic graph.

Generate has one bounded self-edge. It requires a repairable rejection,
changed candidate/error fingerprints, a monotonic ordinal, and remaining
budget. Execute has no generic self-edge. Recovery back-edges are guarded by a
durable incident budget and exactly-once decision receipts.

## CommitStep transaction

`CommitStep` validates the current task, Plan identity/version, current step,
state generation, action/attempt/validation identity, non-probe role,
CompletionGate result, artifact ownership, and binding ownership before one
PlanState CAS write. That write accepts heads, records validation, advances one
cursor, activates at most one successor, expires step-scoped state, and stores
an immutable idempotency receipt. Duplicate replay returns the stored outcome;
CAS conflict has no partial mutation.

## Recovery and final completion

`TypedRecovery` consumes only typed failure outcomes or an explicit semantic
`recovery_fact`. It preserves diagnosis, owner, source scope, fingerprints,
evidence references, repair instruction, and budget receipt. It neither creates
nor applies a legacy `RecoveryDecision`. `complete_step` and `complete_task`
dispositions are rejected as internal protocol violations. One route-free
outcome receipt is committed per decision ID; duplicate crash/resume replay
does not consume budget twice.

`TypedFinalAudit` requires a durable `CommitStepOutcome.plan_complete` receipt,
current successful execution evidence, matching current verification evidence,
all Plan steps complete, a newly scoped final validation, and a current final
CompletionGate pass. Only then does one CAS transaction set the durable task to
completed and store the terminal receipt. Stale cross-plan, cross-step, action,
attempt, or commit evidence is rejected before the auditor runs.

## Legacy compatibility and authority boundary

Legacy Controller routing remains intact for schema 1. In the schema-2 runtime
handlers and wrappers, these methods have zero routing authority:

- `_apply_transition_guard`
- `_resolve_and_commit_effective_transition`
- `_dispatch_effective_route`

Legacy `next_step`, Controller transition metadata,
`RecoveryDecision.target_route`, `required_next_action`, and
`pending_recovery_action` can remain in schema-1 checkpoints but cannot alter a
schema-2 runtime edge. Generate and Execute currently use narrow adapters over
their live semantic implementations; every legacy destination field is
discarded at the typed boundary.

## Test evidence

No provider API, real scientific workflow, Node, Pi, or installation command
was run.

- Typed Recovery/FinalAudit and Supervisor/CommitStep: 21 passed, 4 subtests.
- Typed Generate/Execute, contracts, and StepWorker: 30 passed, 93 subtests.
- Recovery identity/context/policy/lifecycle: 111 passed, 9 subtests.
- CompletionGate, Plan advancement, cross-step state, binding, and recovery
  scope set: 108 passed; one unchanged pre-existing assertion failed in
  `test_action_scoped_runtime_bindings.py` because the live provider projection
  emits a concrete executable path while the historical test expects
  `$TOOL_BIN`.
- Stage-C focused Generate/Execute regressions: 47 passed, 12 subtests.
- Stage-C outcome contract aggregate: 29 passed, 68 subtests.

The broader Generate/Planner/runtime-preflight run also exposed 11 historical
failures. Representative failures were reproduced from the committed Stage-B
snapshot, establishing that they predate Stage C and are outside CTRL-02's
change surface.

## Commits

1. `67e2b00 refactor(workflow): add typed routing foundation`
2. `835d0a8 refactor(verifier): separate verification from step commit`
3. `e7578a5 refactor(execution): move generate and execute routing to typed outcomes`
4. `refactor(recovery): remove runtime route authority from typed graph`

## Known limitations and CTRL-03 readiness

CTRL-02 does not activate schema 2 globally. Triage, Consultant, Planner,
PlanReview, AskUser, installer return, and resume/controller entry authority
remain schema-1 work for CTRL-03. The isolated graph exposes explicit boundary
nodes so CTRL-03 can replace them incrementally without weakening the runtime
contracts.

Pi migration remains paused and is not a dependency of this control-plane work.
