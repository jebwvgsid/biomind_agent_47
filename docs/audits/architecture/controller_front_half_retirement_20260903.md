# CTRL-03 — Front-Half Typed Routing and Controller Retirement

Date: 2026-09-03

Branch: `refactor/consultant-controller-authority`

Starting HEAD: `28fcfdb1a4fea19628892acc57f902926118744c`

## Result

Routing schema 2 is now the complete production graph for newly created tasks:

```text
START
  -> Triage
     |-> DirectResponse -> END
     |-> Consultant -> Planner -> PlanReview
     |                              |-> Generate
     |                              `-> StepWorker (configured external backend)
     |-> semantic ResumeState dispatch

Generate -> Execute -> Supervisor -> CommitStep -> Generate
                         |               `-> FinalAudit -> END
                         `-> Recovery

Recovery -> Generate | Planner | Consultant | Supervisor | AskUser | END
Execute  -> AskUser | InstallSubagent | Recovery | ...
AskUser  -> Consultant | Planner | Generate | Supervisor | Recovery
InstallSubagent -> Generate | Recovery | END
```

Every outcome-to-node table is in
`agent_core/workflow/runtime_graph.py::build_schema2_production_graph`.
Registered schema-2 nodes do not return `Command(goto=...)` or a graph-node
destination. `Command(resume=...)` remains limited to LangGraph interrupt
continuation.

## Front-half contracts

All contracts are frozen, versioned, recursively route-field rejecting, and
JSON serializable:

| Node | Contract | Semantic outcomes |
|---|---|---|
| Triage | `TriageOutcome.v1` | direct response ready, consultation ready, step execution ready, resume classified, harness error |
| Consultant | `ConsultantOutcome.v1` | planning ready, consultation continues, user input needed, stale result, terminal, harness error |
| Planner | `PlannerOutcome.v1` | plan-review ready, consultation required, planning continues, stale result, terminal, harness error |
| PlanReview | `PlanReviewOutcome.v1` | approved for execution, revision required, reconsultation required, user input needed, approval pending, terminal, harness error |
| AskUser | `AskUserOutcome.v1` | answer recorded, question missing, harness error |
| InstallSubagent | `InstallSubagentOutcome.v1` | capability available, installation failed, terminal, harness error |

The live legacy implementations remain available only to schema 1. Schema 2
invokes them behind `agent_core/workflow/early_nodes.py`, recursively removes
legacy route projections from both input and output, and publishes only the
node-specific outcome plus route-blind business state.

## ResumeState contract

`ResumeState.v1` supports these live semantic states:

- `awaiting_user_input`
- `consultation_ready`
- `planning_ready`
- `plan_review_pending`
- `step_execution_ready`
- `action_execution_ready`
- `verification_pending`
- `recovery_pending`
- `installation_pending`
- `final_audit_pending`
- `terminal`

`_resolve_schema2_resume_state` reads durable task, plan, step, approval,
waiting, recovery lifecycle, and pending-action facts. It does not call
`_resolve_resume_node`, consume a legacy recovery transition, or inspect
`resume_route_hint`, `next_step`, `dispatch_route`, `effective_route`, or
`RecoveryDecision.target_route/next_route`. Triage carries the semantic state
to the graph; only the graph maps it to a node.

## Schema selection and checkpoint behavior

- A genuinely new task is explicitly bound to schema 2 in both PlanState root
  and `task_state` before graph execution.
- An existing checkpoint with an explicit schema remains sticky.
- An existing checkpoint with no schema version resolves to schema 1.
- A missing-version historical task is not stamped schema 2 when a later user
  submission reopens it.
- No checkpoint is automatically migrated and no legacy route-to-outcome
  adapter exists.
- `BioAnalystAgent._app_for_task` selects the compiled legacy or schema-2 app
  from the durable binding for run, resume, interrupt, debug, and terminal
  snapshot reads.

## Poisoning and Controller authority proof

Before every typed node invocation, `route_blind_state_projection` recursively
removes:

```text
next_step
next_route
proposed_route
effective_route
dispatch_route
route_resolution.dispatch_route
node_recommended_next_step
required_next_action
pending_recovery_action
RecoveryDecision.target_route
RecoveryDecision.next_route
resume_route_hint
resolved_resume_node
Controller transition metadata
```

The complete compiled graph test runs the same nine-node happy path with and
without all of those poison values. The visited sequence remains:

```text
Triage -> Consultant -> Planner -> PlanReview -> Generate -> Execute
       -> Supervisor -> CommitStep -> FinalAudit
```

The three patched Controller routing functions have call counts `0 / 0 / 0`
for schema-2 front-half adapters and for the compiled schema-2 graph:

- `_apply_transition_guard`
- `_resolve_and_commit_effective_transition`
- `_dispatch_effective_route`

## Preserved properties

- `profile_typed_node` still rethrows the identical `GraphBubbleUp` and
  `asyncio.CancelledError` object.
- AskUser/installer interrupts still use `Command(resume=...)`; the acceptance
  fixture proves one resumed answer produces one post-interrupt side effect.
- CommitStep and FinalAudit retain PlanState CAS and idempotent receipts.
- CTRL-02 typed Supervisor, CommitStep, Recovery receipts/budgets, and final
  audit contracts are unchanged.
- Schema-1 nodes, routers, resume resolver, Controller guards, and route fields
  remain intact for historical checkpoint compatibility.

## Controller responsibility classification

### A. Schema-1 compatibility only

- `_apply_transition_guard`
- `_resolve_and_commit_effective_transition`
- `_dispatch_effective_route`
- `_resolve_resume_node`
- `_consume_pending_recovery_on_resume`
- legacy `triage_router`, `consultant_router`, `ask_user_router`,
  `planner_router`, `plan_review_router`, and runtime dispatch routers
- legacy route projections and recovery target routes

### B. Route-blind runtime service

- task/plan canonicalization and PlanState persistence
- provider invocation and prompt assembly
- Generate/Execute domain work behind typed adapters
- Supervisor verification
- CommitStep CAS transaction
- typed Recovery diagnosis, budget, and receipt persistence
- FinalAudit CAS transaction
- memory, profiling, human I/O, and package/runtime services

### C. Dead for schema 2

- interpreting any legacy route field as a schema-2 transition
- node-owned graph destination returns
- Controller rewrite/dispatch after a schema-2 outcome
- durable node-name resume hints

These paths are intentionally retained rather than physically deleted because
they remain live inside the schema-1 compatibility boundary.

## Verification

No provider request, scientific workflow, Pi process, Node installation, or
Native Agent Loop was run.

- Typed contracts, full graph, poisoning, resume, interrupt, StepWorker, and
  CTRL-02 suites: `56 passed, 139 subtests passed`.
- Recovery context/policy/lifecycle, CompletionGate, and cross-step state:
  `120 passed, 3 subtests passed`.
- Real Agent construction and legacy graph integration reached and passed the
  first integration case. A later legacy Planner fixture failed with the
  previously known `ACTION_CAPABILITY_SCOPE_MISSING` contract gap; it does not
  execute the schema-2 graph and was not weakened for this task.

## Readiness decision

Schema 2 is safe to be the default for genuinely NEW tasks: **YES**. Historical
and missing-version checkpoints remain schema 1.

WORKER-01 Native Agent Loop prerequisite status: **READY**. This means the
single-control-plane and typed StepWorker boundaries are ready for that later
task; the Native Agent Loop itself was not started here, and Pi remains paused.
