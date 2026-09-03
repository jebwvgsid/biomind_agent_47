# Current Recovery Context Baseline

Implementation baseline commit: `43119e4750d17e0dad70e10c337e7058006dfb02`

Audit date: 2026-09-03 (Asia/Shanghai)

Incident shape: `session_1788401254`

## Current verdict

The Recovery identity, atomicity, scope-isolation, provider-claim, and opaque
cross-step binding changes are implemented. The current authority chain is:

```text
semantic RecoveryDecisionDraft
  -> Controller RecoverySourceScope
  -> RecoveryPolicy effective decision
  -> finalize_recovery_decision (one identity assignment)
  -> matching RecoveryExecutionEnvelope
  -> RecoveryCommitBundle
  -> one PlanState CAS
  -> strict active-state validation
  -> target_generation_id claim
  -> provider completion
  -> exactly-once local action adoption
```

`PlanState.task_state.recovery_state.authoritative_commit` is the active truth.
Recovery sidecars remain replay and audit material only.

## Scope audit

Validation, completion, supervisor, and recovery evidence now use scoped
history plus an active reference. Provider-facing Recovery projection requires
an explicit expected tuple:

```text
task_id + plan_id + plan_version + step_id + action_id + execution_attempt_id
```

An absent or mismatched tuple projects `ABSENT_CURRENT_EVIDENCE`; it does not
fall back to a prior step's `VALID`, `rc=0`, or completion `PASS`. Advancing a
step clears active references but retains immutable history.

## Binding audit

The generic Controller stores `PlanBinding` values opaquely. It accepts a
candidate only after producer return code and validation both pass, preserves
the exact value and type, resolves only explicit `plan_binding:<name>` refs,
and marks producer bindings stale on rewind without deleting history. It does
not parse executable source or synthesize domain values.

The snpEff Skill is the first consumer. Its typed operation contract declares:

- `prepare_database` produces `genome_version` and `snpeff_db`;
- `build_database` consumes both bindings;
- `annotate_variants` consumes both bindings.

The incident value `biomind_bm_remain_annot_007` is transported exactly. The
Controller neither creates it nor rewrites it to
`biomind_remaining_annot_007`.

## Terminal causal chain

Internal Recovery protocol termination preserves four independent objects:

1. `primary_execution_failure`
2. `recovery_decision_summary`
3. `recovery_transition_failure`
4. `terminal_failure`

This prevents the final `blocked` label from replacing the original execution
failure or the Recovery decision that attempted to repair it.

## Offline acceptance matrix

| Tests | Coverage |
|---|---|
| T1-T2 | semantic draft versus machine authority; spoofed identity rejection |
| T3-T5 | R1, R2, and deterministic paths use the Controller finalizer |
| T6-T9 | cache/idempotency, restart claims, legacy identity, mismatch fail-closed before provider |
| T10-T13 | exact evidence scope, cross-step isolation, retained history, terminal causal chain |
| T14-T22 | success-only binding adoption, exact projection/ref resolution, no synthesis or shell parsing, immutability, rewind |
| T23-T27 | snpEff producer/build/annotation operations, typed corrective route, no downstream semantic regeneration |

The incident-shaped replay is
`tests/fixtures/session_1788401254_recovery_binding_replay.json`; it uses only
fake Generate/Execute/Recovery objects.

Final focused result:

```text
333 passed, 33 subtests passed
```

No real LLM/API call, Java process, snpEff process, or benchmark was used.

## Current source blobs

| Path | Blob |
|---|---|
| `agent_core/recovery.py` | `c924299a4fb058e42f16e5dadfdaa4f682832f9b` |
| `agent_core/middleware.py` | `fde105a612ddd39850d723a37f1b479186487abf` |
| `agent_core/plan_tracker.py` | `06a226da23f110dd059446f5b53df0cc7889fe83` |
| `agent_core/diagnostics.py` | `8c07de1bc2dc5eebe6108c3c3ea3b0dc76f309ef` |
| `agent_core/plan_bindings.py` | `be753bf54438ce03c953f6730226ad6584d64ed0` |
| `agent_core/node_context.py` | `613c2fd67c709cadcf867481fe501f07c75e2ae5` |
| `bio_agents.py` | `6c83c1d1d075dad500b98e9765c191a3b4b0b9ce` |
| `skills/genomics/snpeff-annotation/SKILL.md` | `9d8a2203e7b6f70e17829e58d7a558587d98fa51` |

## Re-audit triggers

Re-audit this baseline when any of these change:

- Recovery finalization, decision identity, envelope, or commit bundle schema;
- PlanState CAS, active recovery, transition, budget, or generation claim lifecycle;
- validation/completion/supervisor/recovery scope keys or active references;
- Generate provider request/adoption handling;
- generic PlanBinding persistence, projection, resolution, or invalidation;
- a Skill binding operation contract or typed action schema;
- Recovery provider-facing NodeContext construction.
