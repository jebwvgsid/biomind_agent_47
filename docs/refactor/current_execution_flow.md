# Current Execution Flow

Audit date: 2026-07-30

This document describes the current implementation, not the desired design. Line
numbers refer to the working tree audited on the date above.

## Managed Task Flow

```text
Consultant
  -> Planner
  -> PlanTracker normalization
  -> Generate prompt assembly
  -> LLM raw response
  -> action parser
  -> renderer materialization or pending-action binding
  -> controller transition guard
  -> Execute
  -> active error-recovery middleware (may reroute here)
  -> Supervisor
  -> CompletionGate
  -> post-review recovery adjudication (when needed)
  -> router
```

## Concrete Call Chain

| Stage | Current implementation | State/control effects |
|---|---|---|
| Planner | `bio_agents.py:9330` | Builds or locally repairs the plan; calls `collapse_renderer_composite_steps()` and may call `apply_plan_patch()` around `bio_agents.py:9626-9666`. |
| Plan normalization | `agent_core/plan_tracker.py:909`, `agent_core/plan_tracker.py:1798` | Normalizes steps and collapses Renderer internals into a composite step. |
| Generate context | `bio_agents.py:3267`, `bio_agents.py:3424`, `bio_agents.py:10125` | Builds action contract/current-step context and invokes the LLM. Generic and Renderer action protocols are both currently visible. |
| Action parsing | `bio_agents.py:7277`, `bio_agents.py:10495-10540` | Parses JSON/XML actions and chooses a generated action category. |
| Renderer materialization | `bio_agents.py:2634`, `bio_agents.py:2770` | Normalizes declared Renderer arguments and converts a valid Renderer action into one executable pending action. |
| Pending-action binding | `bio_agents.py:2516` | Adds action, task revision, step, purpose, role, runtime bindings and expected evidence. It also contains the unsafe Renderer compatibility inference at `bio_agents.py:2527-2560`. |
| Pre-execution contract check | `bio_agents.py:3023`, transition handling near `bio_agents.py:7748` and Execute entry near `bio_agents.py:11180` | Rejects stale or invalid pending actions, but currently does not reject every free-form action for a manifest-absent Renderer composite step. |
| Execute | nested `execute_logic` in `bio_agents.py`, principal action handling around `bio_agents.py:11180-12145` | Runs the command, validates the immediate result, persists logs/ExecutionEvidence and also mutates retry/probe budgets. |
| Execute recovery middleware | `agent_core/middleware.py:1602`, applied at `bio_agents.py:12198` | Re-evaluates failure, builds diagnostics, invokes RecoveryJudge when needed and can write `next_step` before Supervisor. |
| Supervisor | `supervisor.py:1046`, graph wrapper at `bio_agents.py:12208` | Produces PASS/WARN/RETRY/ESCALATE, requirements/artifact checks, retry context and a direct `next_step`. |
| CompletionGate | `agent_core/completion_gate.py:1879`, `agent_core/completion_gate.py:2085`; called from the Supervisor controller around `bio_agents.py:12528` and `bio_agents.py:12642` | Validates step and task completion, including Renderer manifest/action evidence and final outputs. |
| Post-review recovery | `bio_agents.py:2344` | Repeats trigger/context/judge/route work after Supervisor or CompletionGate conflicts. |
| Graph routing | `bio_agents.py:13429`, `bio_agents.py:13445`, `bio_agents.py:13468` | Converts state fields such as `next_step`, verdict and recovery fields into graph edges. |

## State Ownership Today

`PlanTracker` persists plan steps, Renderer execution state and budgets, but it is
not yet the sole writer. Generate, Execute, Supervisor, middleware and the
controller all write overlapping transient fields in `AgentState` (see
`agent_core/state.py:35-118`). The same semantic fact can therefore exist as:

- `retry_count` in graph state;
- a budget under persisted `task_state.budgets`;
- `supervisor_verdict` plus `required_next_action`;
- `recovery_decision` plus `pending_recovery_action`;
- `next_step` written by more than one module.

This is the main source of stale RETRY/PASS state and non-convergent routing.

## Existing Components To Reuse

- Renderer argument normalization: `bio_agents.py:2634`.
- Renderer materialization: `bio_agents.py:2770`.
- Structured action parser: `bio_agents.py:7277`.
- Renderer manifest normalization/registration/progression:
  `agent_core/plan_tracker.py:530`, `agent_core/plan_tracker.py:579`,
  `agent_core/plan_tracker.py:615`.
- Step/task completion validation: `agent_core/completion_gate.py:1746`,
  `agent_core/completion_gate.py:1879`, `agent_core/completion_gate.py:2085`.
- Recovery trigger and bounded decision policy: `agent_core/recovery.py:1105-1433`.
- Structured diagnostic context: `agent_core/diagnostics.py`.

## Immediate Architectural Constraint

Do not introduce new `PreActionGuard`, `PostExecutionGuard`, `RecoveryManager` or
parallel action-contract classes yet. First make existing functions obey one
ownership model; physical extraction or renaming can happen only after replay
tests pass.
