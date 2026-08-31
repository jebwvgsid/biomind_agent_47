# Recovery Context Gap Analysis

Audit date: 2026-07-31

## Confirmed Gaps

### 1. ProgressTracker Is Not Wired Into The Active Middleware

`ProgressTracker` has useful artifact/action/code/contract comparison logic
(`agent_core/diagnostics.py:1503-1680`), but its construction and persistence
remain in `_legacy_error_recovery_middleware`. The active wrapper at
`agent_core/middleware.py:1605-1811` reads `state.recovery_progress` without
updating it.

Impact:

- meaningful progress can be empty even when artifacts changed;
- no-progress decisions fall back to failure fingerprints/history;
- Generate receives a `recovery_progress` field that is commonly empty;
- repeated probes with different command hashes can be harder to classify.

Design recommendation: call the existing ProgressTracker from the active
wrapper or Controller and persist its typed result in PlanTracker. Do not build
a second progress subsystem.

### 2. Full ValidationEvidence Is Not Reliably Projected To Generate

Active Supervisor persists structured `validation_evidence`, but Generate's
step-local message selector does not explicitly select `<validation_evidence>`.
The recovery block contains a compact decision/diagnostic summary, while
`last_supervisor_feedback` is minimal.

Impact: Generate can know “PATCH_SCRIPT” without seeing the exact failed
success criterion or missing artifact that caused it.

Design recommendation: add a compact, pinned `validation_summary` containing
validation ID, action/step status, contract status, missing required artifacts,
failed checks and evidence paths. Do not inject the full Supervisor transcript.

### 3. RecoveryJudge Schema Repair Omits The Invalid Raw Response

The first Judge parse failure is repaired with the original prompt plus the
exception text (`agent_core/middleware.py:1724-1730`). Unlike Generate action
repair, the invalid answer itself is not persisted/included.

Impact: the model is told that parsing failed but cannot inspect exactly which
field/value violated the schema.

Design recommendation: persist a redacted raw response and include a short
bounded excerpt or field-level parser report.

## Duplicate Or Conflicting Context

### Current-Step Duplication

The same current step appears in TaskSpec projection, current-step context,
`current_plan`, `plan_progress` and current action contract. This consumes
prompt budget without adding evidence.

Recommendation: keep one canonical structured `CurrentStepContext` and make
other blocks reference its ID/version.

### Legacy `RECOVERY_TEMPLATE` Plus Structured Recovery

The conditional template can coexist with RecoveryDecision/DiagnosticContext.
It is not authoritative and has a brittle “last message contains emoji”
trigger. Remove it after old-checkpoint migration tests prove that structured
recovery is always available.

### Scientific Advisory Prompt Residue

`supervisor.py:512-576` contains several domain examples in a generic advisory
prompt. They are not routing authority, but they increase tokens and may bias
unrelated analyses.

Recommendation: replace domain examples with generic evidence categories in a
later prompt-cleanup batch.

## Dead Recovery Implementations

The following definitions remain in the repository but are inactive:

- `_legacy_error_recovery_middleware` and its two LLM calls
  (`agent_core/middleware.py:320,884,926`);
- `_legacy_supervisor_controller` (`bio_agents.py:13098`);
- `_legacy_supervisor_final_audit` (`bio_agents.py:13826`);
- post-review LLM adjudication whose callers are legacy-only
  (`bio_agents.py:2359`);
- `ErrorRecoverySystem._llm_analyze_error`
  (`execution/monitoring.py:134`).

They do not currently determine routes, but they make audits and maintenance
harder and create a reactivation risk. Mark/deprecate them explicitly, then
remove only after checkpoint compatibility coverage exists.

## Context That Is Deliberately Bounded

These are not bugs by themselves:

- DiagnosticContext uses bounded tails and source paths rather than complete
  logs.
- Generate receives current step, not full Plan.
- Generate receives selected step-local messages, not full graph history.
- Supervisor scientific advisory receives only a narrow execution projection.
- RAG/memory blocks are optional and droppable.

The correct improvement is source-addressable structured evidence, not larger
prompts.

## Real Replay: `session_1785398207`

### Original Persisted Evidence

Task root:

```text
benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1785398207
```

Confirmed facts:

- seven persisted scripts and seven `ExecutionRecord` entries;
- two invalid action response records;
- latest action was `step-1-attempt-12`;
- return code was 0;
- role/source were `renderer_invocation` /
  `renderer_compat_adapter`;
- manifest path was empty;
- no target artifacts were produced;
- old durable plan ended blocked;
- old counters included 11 Generate calls, 17 Supervisor calls and 12 Renderer
  invocations for step 1.

The old RecoveryDecision correctly recognized that usage/help output with no
manifest was not successful execution, but only after repeated misclassified
attempts. The deterministic DiagnosticContext still classified the root cause
as insufficient evidence, while RecoveryJudge emitted a confident Renderer
usage diagnosis. This is a concrete example of two diagnosis layers
disagreeing.

### Current-Code Reconstruction

The exact final Generate `messages` array was not persisted in this old
checkpoint. Therefore the current result is **reconstructed**, not an original
prompt replay.

With the audited current code:

1. a read-only `head/grep/sed/--help` action is classified as a probe;
2. a Renderer composite step without a manifest rejects ordinary `EXECUTE`
   before execution;
3. TaskSpec/formal input projection can materialize the canonical Renderer
   invocation;
4. the old `renderer_compat_adapter` substring upgrade is no longer the normal
   source of authority;
5. one format-repair Generate turn is allowed; a second invalid action does
   not receive a third identical call.

This indicates that the historical probe-to-Renderer loop is addressed by the
current control path, while the progress and validation-context gaps remain.

## Top Three Context Problems

1. Active no-progress classification does not use the existing
   `ProgressTracker`, so recovery history is less informative than the codebase
   appears to promise.
2. Generate lacks a guaranteed compact projection of full
   `ValidationEvidence` and exact failed contract criteria.
3. Duplicate current-step blocks and an unreliable legacy recovery template
   spend bounded prompt space that should carry authoritative evidence.

## Minimal Design Recommendations

1. Define one `RecoveryContext` view over existing structures, not a new
   persisted state system. It should reference TaskSpec/current step,
   ExecutionRecord, ValidationEvidence, GateResult, RecoveryDecision,
   ProgressDecision and evidence paths.
2. Keep RecoveryPolicy deterministic and authoritative. Use RecoveryJudge only
   for declared ambiguity.
3. Give Generate only the current step, legal actions, compact failure facts,
   committed decision, relevant skill policy, latest attempt and evidence
   paths. Exclude old plans, old diagnoses, installer transcripts and full
   logs.
4. Persist a redacted prompt-composition manifest per LLM call: block names,
   character counts, hashes, source IDs and dropped/truncated status. Do not
   persist secrets or complete environment dumps.
5. Add prompt-composition tests that assert required blocks, forbidden legacy
   blocks, limits and dynamic allowed actions for every recovery class.

## Responsibility Conformance

Current code is close to:

```text
Supervisor validates
RecoveryPolicy decides
Generate proposes the next action
Controller commits transitions
CompletionGate validates completion
```

The remaining violations are mainly residual/dead implementations and
incomplete context wiring, not active Supervisor ownership of routing.

