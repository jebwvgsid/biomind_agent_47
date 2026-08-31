# Regression Test Matrix

## Baseline Tests Before Behavior Changes

| Area | Existing suite | Required addition |
|---|---|---|
| Renderer parsing/materialization | `tests/test_renderer_controller.py` | Capture raw -> parsed -> bound provenance and reject entrypoint-inspection probes. |
| Renderer manifest lifecycle | `tests/test_renderer_manifest.py`, `tests/test_plan_tracker.py` | Resume and plan-patch preservation for `renderer_execution`. |
| Generate lifecycle | `tests/test_plan_lifecycle.py`, `tests/test_prompt_manager.py` | Dynamic allowed actions and bounded format repair. |
| Recovery | `tests/test_recovery.py`, `tests/test_shell_safety.py` | Same decision from Execute and post-review entrances; budget ownership. |
| Supervisor | `tests/test_supervisor.py` | Action-only validation and no direct graph routing. |
| Completion | `tests/test_completion_gate.py` | Step/task-only scope and manifest action completion. |
| Task facts | `tests/test_task_spec.py`, `tests/test_task_spec_integration.py` | Explicit input/parameter/output projection and old-state migration. |

## Batch 1 Critical Cases

1. Renderer composite + no manifest + plain `head renderer.py` EXECUTE:
   rejected before Execute; remains diagnostic only if RecoveryPolicy authorized it.
2. Renderer composite + no manifest + `renderer.py --help`:
   rejected as Renderer invocation; no Renderer budget increment.
3. Plain command merely contains the Renderer entrypoint path:
   `_bind_pending_action()` must not add Renderer provenance.
4. Valid structured `RENDERER_INVOCATION`:
   materialized by the existing normalizer/materializer and accepted.
5. Invalid structured Renderer arguments:
   format repair exposes only the Renderer contract fields and does not offer
   ordinary EXECUTE.
6. Registered manifest:
   Controller selects `next_renderer_manifest_action()` and bypasses free-form
   Generate.
7. Probe success with no formal artifacts:
   records probe evidence but does not advance the composite step or run the
   formal artifact gate as if primary work succeeded.

## Ownership Tests

| Invariant | Assertion |
|---|---|
| Controller owns routing | Policy/validator outputs contain semantic decisions, not graph node names after migration. |
| PlanTracker owns budgets | Probe/repair/retry updates are persisted once and projected to graph state. |
| Planner owns plan contract | Generate cannot change step type, skill or Renderer ID. |
| Materializer owns provenance | Renderer role/source cannot be inferred from command substrings. |
| Supervisor scope | Reviews only latest action evidence and never closes a task. |
| CompletionGate scope | Validates step/task contracts and never proposes shell probes or tool changes. |
| User-question boundary | Internal protocol, runtime drift and manifest mismatch never become `ask_user`. |

## Session Replay: 1785398207

Replay checkpoints must demonstrate:

```text
raw action: ordinary EXECUTE probe
parsed action: execute
pre-action result: rejected for Renderer composite step
bound command_role: diagnostic only if explicitly authorized, otherwise absent
renderer invocation count: unchanged
manifest status: absent
next semantic decision: FORMAT_REPAIR or contract reconciliation
Planner re-entry: false unless failure_owner=PLAN
user question: false
```

The corrected replay must never execute the seven historical source/help probes as
`renderer_invocation` and must not terminate through
`action_format_repair_exhausted` with unchanged prompts.

## Suggested Commands

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest \
  tests.test_renderer_controller \
  tests.test_renderer_manifest \
  tests.test_plan_lifecycle \
  tests.test_plan_tracker

/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest \
  tests.test_recovery \
  tests.test_shell_safety \
  tests.test_supervisor \
  tests.test_completion_gate

/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest \
  tests.test_task_spec \
  tests.test_task_spec_integration \
  tests.test_prompt_manager
```

Run the first group before Batch 1, all groups before deleting any compatibility
logic, and a fresh snpEff plus GATK Renderer integration task before Batch 5.
