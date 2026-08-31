# Current Recovery Flow

Audit date: 2026-07-31

Scope: current working tree after Renderer/TaskSpec/RecoveryPolicy/Supervisor
Batches 1-4. This is an implementation audit, not a target architecture.

## Ownership Summary

The active path separates five responsibilities:

| Responsibility | Current owner | Main evidence |
|---|---|---|
| Detect an invalid generated action | action parser and controller transition guard | `bio_agents.py:7668-7795`, `bio_agents.py:8132-8440` |
| Detect an execution failure | Execute creates `ExecutionRecord`; active middleware evaluates `RecoveryTrigger` | `agent_core/recovery.py:499`, `agent_core/middleware.py:1605-1811` |
| Validate latest execution | active `Supervisor.run()` creates `ValidationEvidence` | `supervisor.py:1229-1475` |
| Decide semantic recovery | deterministic `RecoveryPolicy`; `RecoveryJudge` only for ambiguous evidence | `agent_core/recovery.py:1499-1684`, `agent_core/recovery.py:1980-2410` |
| Generate the next repaired command/action | the ordinary Generate node, constrained by the committed decision | `bio_agents.py:10782-11790` |
| Validate step/task completion | `CompletionGate`; Controller commits the result to PlanTracker | `agent_core/completion_gate.py:1733-2170`, `bio_agents.py:12833-13096`, `bio_agents.py:13737-13824` |

`Supervisor` does not own routing in the active graph. It emits evidence. The
Controller applies `RecoveryPolicy`, and graph routers consume the committed
`RecoveryDecision` through `recovery_decision_from_state()` and
`recovery_route_from_state()` (`bio_agents.py:14429-14599`,
`agent_core/recovery.py:844-924`).

## Active Graph

```text
Generate
  -> action parser
  -> controller transition guard
     -> invalid protocol: RecoveryPolicy(FORMAT_REPAIR)
        -> Generate with format-repair feedback
     -> valid pending action
        -> Execute
           -> persist ExecutionRecord and full stream files
           -> active error_recovery_middleware
              -> RecoveryTrigger
              -> DiagnosticContext
              -> deterministic diagnosis, or RecoveryJudge if ambiguous
              -> committed RecoveryDecision
           -> Supervisor.run()
              -> ValidationEvidence
           -> Controller + CompletionGate + RecoveryPolicy
              -> complete step / repair current step / reconcile /
                 diagnostic / ask / block / PLAN_PATCH
```

The graph registration proving which implementations are active is at
`bio_agents.py:12823-12828` and `bio_agents.py:14422-14424`.

## Error-Type Flows

### Action Or Protocol Error

```text
raw Generate response
  -> _extract_structured_action()
  -> invalid or illegal for current action contract
  -> _record_action_format_failure()
  -> RecoveryPolicy commits FORMAT_REPAIR
  -> one new Generate call with ACTION_FORMAT_REPAIR feedback
  -> second invalid response: structured block/escalation, no third blind call
```

Key functions:

- Parser: `bio_agents.py:7668-7795`.
- Dynamic Renderer action protocol: `bio_agents.py:2670-2760`.
- Repair feedback: `bio_agents.py:2864-2900`.
- Durable failure record and budget: `bio_agents.py:4537-4616`.
- Pre-execution enforcement: `bio_agents.py:8132-8440`.

No separate diagnosis LLM is called for action-format repair. The same Generate
LLM receives a changed context. For a Renderer composite step with no manifest,
the legal actions are narrowed to `RENDERER_INVOCATION`, or evidence-backed
`BLOCKED`; ordinary `EXECUTE` and `STEP_COMPLETION_CANDIDATE` are rejected before
execution.

### Nonzero Return Code, Timeout, OOM, Missing Command, Permission

```text
Execute
  -> ExecutionRecord / persisted stdout, stderr, script and trace
  -> evaluate_recovery_trigger()
     -> obvious runtime/input/permission/OOM: deterministic decision
     -> ambiguous/conflicting: build_diagnostic_context() -> RecoveryJudge
  -> Controller routes the committed decision
  -> Generate creates a same-step repair when action is PATCH_SCRIPT
```

The active middleware reads structured `execution_evidence`. Text/emoji
observation parsing remains only as a migration fallback for old checkpoints
(`agent_core/middleware.py:1626-1646`).

`build_diagnostic_context()` includes TaskSpec, the current step, execution
source and streams, runtime/manifest evidence, artifact state, post-execution
reviews, up to three attempts and active-skill policy
(`agent_core/diagnostics.py:665-835`). Full streams stay in persisted files; the
LLM receives a bounded projection.

### Return Code 0 But Manifest Or Required Artifact Missing

```text
Execute returncode=0
  -> artifact/manifest evidence
  -> Supervisor ValidationEvidence
  -> current-step CompletionGate
  -> RecoveryPolicy
     -> unique contract drift: RECONCILE_AND_CONTINUE
     -> action/renderer defect: PATCH_SCRIPT
     -> true PLAN contract defect only: PLAN_PATCH
     -> unresolved evidence: diagnostic or escalation
```

Supervisor validates the latest action; CompletionGate validates the declared
step/task contract. They are not interchangeable. RecoveryPolicy is the only
semantic arbiter when their evidence conflicts.

### No-Progress Loop

`ProgressTracker` is implemented at `agent_core/diagnostics.py:1503-1680` and
can compare artifacts, command/action signatures, code hashes and contract
changes. However, the active structured middleware does not currently
instantiate or persist it. Its active trigger therefore relies mainly on
failure fingerprints, recent recovery history and any pre-existing
`state.recovery_progress` (`agent_core/recovery.py:1571-1650`).

This is a real integration gap: the sophisticated progress classification is
currently used by the legacy middleware, not by the active wrapper. Generate
can receive `recovery_progress`, but it is commonly empty.

### Completion And Final Audit Failure

`supervisor_final_audit()` runs active Supervisor validation, then
`CompletionGate.validate_final()`, then RecoveryPolicy
(`bio_agents.py:13737-13824`). A failed final gate cannot finish directly:

- repairable current-step/action failure -> Generate;
- unique artifact contract drift -> reconciliation then gate recheck;
- proven plan structural defect -> Planner through explicit `PLAN_PATCH`;
- missing user/scientific decision -> ask user;
- exhausted or unrecoverable internal failure -> block/escalate.

Runtime nodes are prevented from routing to Planner without a structured
PLAN-owned patch request (`bio_agents.py:8499-8522`).

## Active, Compatibility, And Dead Paths

| Path | Status | Notes |
|---|---|---|
| `agent_core/middleware.py:1605` structured middleware | Active | Trigger/context/judge/state only; does not apply arbitrary patches. |
| `agent_core/middleware.py:320` `_legacy_error_recovery_middleware` | Dead in current graph | Still contains observation/emoji detection, two LLM diagnosis calls, `RepairExecutor` and large message injection. |
| `bio_agents.py:12833-13096` Supervisor controller | Active | Validation -> policy -> gate -> controller transition. |
| `bio_agents.py:13098` `_legacy_supervisor_controller` | Dead in current graph | Retained implementation, not registered. |
| `bio_agents.py:13737` `supervisor_final_audit` | Active | Final ValidationEvidence + final gate + policy. |
| `bio_agents.py:13826` `_legacy_supervisor_final_audit` | Dead in current graph | Not registered. |
| `bio_agents.py:2359` post-review LLM adjudicator | Effectively dead | Current call sites are inside legacy Supervisor/final-audit functions. |
| `execution/monitoring.py:134` `ErrorRecoverySystem._llm_analyze_error` | Dead | No current construction/call site was found. |

## Priority Of Conflicting Information

The intended and mostly enforced priority is:

```text
ExecutionRecord / artifact facts
  > ValidationEvidence
  > CompletionGate result
  > committed RecoveryPolicy decision
  > RecoveryJudge recommendation
  > Generate narrative
```

RecoveryJudge cannot finish a task, bypass CompletionGate or directly apply a
patch. Generate cannot enter Planner unless the committed decision is
`PLAN_PATCH_REQUEST`. These hard constraints are present in current code.

## Real Session Evidence

`session_1785398207` is an old-runtime failure, not proof of current Batch 1-4
behavior. Its persisted metrics show 35 LLM calls:

| Node | Calls | Prompt tokens | Completion tokens |
|---|---:|---:|---:|
| Generate | 11 | 112,246 | 2,090 |
| Supervisor | 17 | 94,413 | 7,086 |
| Execute/old recovery path | 2 | 17,245 | 1,086 |
| Planner | 2 | 8,576 | 3,202 |
| Consultant | 2 | 6,314 | 67 |
| Triage | 1 | 964 | 94 |

The durable attempt classified help/usage probes as
`command_role=renderer_invocation`, returned 0, and produced no manifest. Its
12th attempt then received a `PATCH_SCRIPT` decision. That sequence explains
the historical loop. Under the current code it is reconstructed to stop at the
pre-action Renderer contract or use projected Renderer arguments; that current
result is a reconstruction, because the old checkpoint did not persist the
exact final message array.

