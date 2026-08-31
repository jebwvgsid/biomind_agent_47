# Batch 5 RecoveryContext Implementation

## Scope

Batch 5 keeps the existing graph, Supervisor, CompletionGate, Planner, Renderer,
and Installer boundaries. It extends the existing diagnostics/recovery models
instead of adding a second recovery manager.

Core files changed:

- `agent_core/diagnostics.py`
- `agent_core/recovery.py`
- `agent_core/middleware.py`
- `agent_core/state.py`
- `prompts/manager.py`
- `bio_agents.py`
- `execution/monitoring.py`

## Canonical Context

`RecoveryContext` schema 3 is the durable context shared by the active Execute
middleware, RecoveryJudge, RecoveryPolicy, and Generate. `DiagnosticContext` is
a compatibility alias to this class.

The bounded prompt projection contains:

- task, step, action, attempt, and failure identity;
- a canonical `CurrentStepContext`;
- ExecutionEvidence, ValidationEvidence, CompletionEvidence, and
  ProgressEvidence;
- runtime, manifest, artifact, and bounded log evidence;
- at most three attempts related by step, fingerprint, or action family;
- the current RecoveryDecision and allowed actions.

Full logs remain referenced by path. Secret-like environment keys and values are
redacted. A context ID is stable for the same task/step/action/attempt/fingerprint.

`CurrentStepContext` is serialized once in Generate. The copy inside
`RecoveryContext` is removed from the Generate prompt projection. TaskSpec and
the action contract no longer embed another step block.

Historical attempts retain full decisions on disk, but their prompt projection
contains only `recovery_action` and `recovery_decision_id`. This keeps the
current RecoveryDecision unique.

## Active Recovery Flow

The active `error_recovery_middleware` now:

1. reads structured ExecutionEvidence;
2. updates and persists the existing ProgressTracker;
3. evaluates a structured RecoveryTrigger;
4. builds and persists `recovery_context.json`;
5. skips RecoveryJudge for deterministic failures;
6. invokes RecoveryJudge at most once for one unchanged context ID;
7. applies RecoveryPolicy, including ProgressEvidence;
8. returns a code-free decision to the controller.

The middleware does not apply script patches or finish tasks.

Meaningful progress is based on manifest state, completed stages, artifacts,
verified repairs, and genuinely new evidence. A different `head`, `grep`, or
other probe command with no task-state change increments the no-progress streak.
Legacy records whose role was incorrectly set to `renderer_invocation` are still
recognized as probes when their structured purpose is `probe`.

## LLM Budget And Idempotence

- deterministic protocol/runtime/input/artifact failures: no RecoveryJudge;
- ambiguous failure: one RecoveryJudge result per RecoveryContext ID;
- one Generate result per RecoveryContext ID and recovery stage;
- one action-format repair per step;
- one Planner patch generation per cached plan-patch context;
- cached invalid Judge output causes deterministic internal escalation, not a
  second equivalent LLM call;
- checkpoint resume reuses persisted Judge/Generate results.

Format repair, Judge, diagnostic probe, and primary execution budgets remain
separate. Reusing a cached Generate result does not increment
`generate_action_count`.

## Prompt Compatibility

New structured recovery state does not inject `RECOVERY_TEMPLATE`. A legacy
checkpoint without RecoveryContext may still use it and emits a deprecation
warning.

Removal condition for `RECOVERY_TEMPLATE`: all supported checkpoints have been
migrated to schema 3 and no production resume audit reports a legacy-only state.

Structured evidence has higher priority than local messages. During recovery,
Generate keeps only the original user goal and explicit human answers; old
observations, Supervisor prose, installer logs, and old recovery instructions
are omitted.

## Dead Paths

These implementations remain unregistered and are marked deprecated:

- `_legacy_error_recovery_middleware`
- `_legacy_supervisor_controller`
- `_legacy_supervisor_final_audit`
- `execution.monitoring.ErrorRecoverySystem`

They are retained only for compatibility and tests. The active graph registers
`error_recovery_middleware` around Execute. Batch 5 does not reconnect or delete
the legacy paths.

## Installer Boundary

Installer still owns its separate installation loop and budget. Batch 5 does not
change installation behavior and does not claim Installer uses RecoveryContext.
A later migration can adapt Installer output into ExecutionEvidence without
moving its ten-turn budget into the main recovery policy.

## Replay Results

Offline replay used the durable state and seven execution records from
`session_1785398207`; it made no API call and did not mutate the old session.

- RecoveryContext digest: `recovery-context-87a896046c1294aa807d2562`
- digest stable on rebuild: yes
- current-step blocks: 1
- validation sources: 1
- current RecoveryDecision sources: 1
- RecoveryJudge required for deterministic manifest absence: 0
- manifest status: absent -> absent
- new/changed artifacts: none
- consecutive no-progress count: 6
- final progress classification: exhausted

The old session had accumulated 11 Judge calls, 9 Generate actions, and 12
renderer invocations. Under the new policy the same deterministic artifact
failure does not call RecoveryJudge, and exhausted progress is passed to
RecoveryPolicy instead of launching another probe loop.

Additional offline replay:

- explicit return code 127: deterministic route, zero Judge calls;
- plan-owned structural error: `PATCH_PLAN -> planner`;
- interrupted checkpoint, same attempt replayed: no extra progress decision;
- zero return code with required artifact missing: deterministic current-step
  recovery, CompletionGate remains authoritative.

## Verification

- 419 recovery, prompt, plan, Supervisor, CompletionGate, Renderer, TaskSpec,
  shell-safety, and memory tests passed.
- Python compilation passed for all seven core files.
- Full discovery ran 786 tests. Remaining failures are environment/dependency
  failures outside Batch 5: missing `pytest`, missing `networkx`, R/Seurat
  fixture incompatibility, and an R process killed by OOM.

## Rollback

Rollback should revert the seven core Batch 5 files and their matching tests as
one unit. Keep persisted `recovery_context.json`, progress, and LLM cache files:
older code ignores them, while deleting them would make resume less auditable.
Do not restore a legacy middleware registration or mix schema-3 prompt builders
with the old free-text recovery path.

## Acceptance

1. Generate stably receives ValidationEvidence: yes.
2. Current step is injected once: yes.
3. ProgressTracker is an active RecoveryPolicy input: yes.
4. One failed attempt invokes RecoveryJudge at most once: yes.
5. One recovery stage invokes Generate at most once: yes.
6. New sessions inject `RECOVERY_TEMPLATE`: no.
7. Legacy recovery text can override structured state: no.
