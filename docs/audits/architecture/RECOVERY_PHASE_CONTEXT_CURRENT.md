# Current Schema-2 Recovery Phase Context

Audit date: 2026-09-04 (Asia/Shanghai)
Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`
Branch: `refactor/consultant-controller-authority`
Implementation through: `7b55ad0`

## Current truth

Schema-2 Recovery is now a bounded phase-local LLM agent. Its production path
is `Schema2RecoveryNode -> RecoveryAgentLoop -> RecoveryRuntime ->
RecoveryOutcome`. The agent owns diagnosis and a semantic directive; the
runtime owns validation, budget, receipts, idempotency, no-progress and CAS;
LangGraph alone maps the typed outcome to an edge.

Schema-1 Recovery remains a compatibility-only path because sticky schema-1
checkpoints still call `_invoke_recovery_v2` from `agent_core/middleware.py`.
Schema-2 does not import or call that provider path. The deletion/sunset gate is
therefore not satisfied.

## Current schema-2 call graph

```text
typed Execute / Verify / Completion / Commit / Install / resume failure
  -> CausalFailurePacketBinder
  -> immutable FailurePacket + active_failure_packet_ref
  -> Schema2RecoveryNode
  -> project_recovery_seed()
  -> stable Recovery System + frozen provider-safe RecoverySeed
  -> private append-only RecoveryAgentLoop
       <-> failure.inspect / step.inspect
       <-> artifact.inspect / artifact.read
       <-> skill.* / evidence.*
  -> strict semantic RecoveryDirective
  -> RecoveryRuntime
       schema / identity / scope / stale / evidence subset
       budget / duplicate / no-progress / CAS / idempotency
  -> RecoveryOutcome
  -> route_recovery_outcome() fixed edge
  -> fresh corrective GenerateSeed (directive + refs only)
```

## Slice evidence

### RCTX-01 COMMIT

status: PASS
commit: `d484668`
files: phase archive, phase inspection, phase subgraph, scoped context, stage contracts and focused tests
tests: phase archive/inspection/subgraph, scoped context and stage-contract offline suites

### RCTX-02 COMMIT

status: PASS
commit: `d4f70c2`
files: recovery projection/runtime, state/stage contracts, production binding and causal packet tests
tests: Execute/Verify/Completion/Commit/Install/resume, stale/cross-step/wrong-plan/replay cases

### RCTX-03 COMMIT

status: PASS
commit: `a1490b3`
files: StepWorker archive adapter, recovery projection and production StepWorker integration
tests: immutable archive, distinct worker session/archive identities and recovery reference projection

### RCTX-04 COMMIT

status: PASS
commit: `8ea87aa`
files: scoped Recovery tool registry/dispatcher and phase inspection integration
tests: 33 focused tool scope, opaque-ref, bound and rejection tests

### RCTX-05 COMMIT

status: PASS
commit: `3ee96a8`
files: stable Recovery System, provider-safe frozen Seed projector and Seed tests
tests: byte stability, scope changes, forbidden fields, normal/hard capacity

### RCTX-06 COMMIT

status: PASS
commit: `c1afac4`
files: LangGraph phase-local Recovery tool loop and loop tests
tests: append-only history, transport retry identity, repeat/no-progress and observation bounds

### RCTX-07 COMMIT

status: PASS
commit: `61ad878`
files: Recovery node/runtime, production handler binding and directive admission tests
tests: 58 focused schema/scope/stale/evidence/budget/idempotency/no-progress/CAS/node tests

### RCTX-08 COMMIT

status: PASS
commit: `4af63a4`
files: GenerateSeed, verified Recovery-to-Generate projector, TypedGenerate handoff and tests
tests: 38 focused handoff/runtime/node/contract/outcome tests

### RCTX-09 COMMIT

status: PASS
commit: `90ea50e`
files: Recovery observation externalization, deterministic checkpoint, durable phase archive and prefix tests
tests: 83 focused Recovery context/archive/agent/runtime/tool/projection suites

### RCTX-10 COMMIT

status: PASS
commit: `27110ca`
files: lossless provider usage normalization and profiler tests
tests: 12 profiler tests; missing cache values remain `None`, with explicit status

### RCTX-11 COMMIT

status: PASS; legacy retained
commit: `7b55ad0`
files: schema isolation test
tests: production binding, modern-call-chain AST isolation, live typed edge and legacy caller inventory

## Contract results

### FAILUREPACKET CAUSAL AUTHORITY

before: retained outcome fields were priority-scanned and Install/resume had source gaps.
after: each Recovery entry is bound to one producer-owned immutable packet and active ref; stale retained outcomes cannot override it.

### INSTALL FAILURE RESULT

`InstallSubagentOutcome.installation_failed` produces a causal packet before the Recovery edge.

### RECOVERY_PENDING RESUME RESULT

Resume requires a valid active packet; a missing/stale/wrong-scope packet fails closed.

### STEPWORKER ARCHIVE RESULT

Every StepWorker attempt can produce an immutable content-addressed phase archive inspected only through a scoped ref.

### WORKER_SESSION_REF VS ARCHIVE_REF

`worker_session_ref` identifies the external worker transport session;
`archive_ref` identifies immutable BioMind-owned phase evidence. They are never interchangeable.

### RECOVERYSEED RESULT

One invocation gets one immutable `RecoverySeed`; no global messages, old phase transcript, raw full logs/scripts or route fields enter its provider view.

### RECOVERYSEED NORMAL FIXTURE CHARS

1451 chars; target `<16000`: PASS.

### RECOVERYSEED HARD BOUND RESULT

Provider Seed above 32000 chars fails closed with `RECOVERY_SEED_CAPACITY_EXCEEDED`.

### RECOVERY SYSTEM STABILITY RESULT

`RECOVERY_SYSTEM_PROMPT` is a literal, task-independent string versioned by
`recovery-agent-system-v1`; task/failure data is only in the frozen Seed.

### RECOVERY APPEND-ONLY RESULT

The loop uses fixed System/Seed plus `AppendOnlyProviderMessages`; tool results append and never rebuild prior messages.

### PREFIX TEST

call1 messages: 2 (`System`, `Seed`)
call2 messages: 4 (`System`, `Seed`, `AI tool call`, `Tool result`)
prefix equality: PASS; also verified through four provider calls with lengths `2,4,6,8`.

### FAILURE.INSPECT RESULT

PASS; reads only the current causal FailurePacket through its registered opaque ref.

### STEP.INSPECT RESULT

PASS; current/direct-dependency archives only, with typed sections, filters and pagination.

### ARTIFACT.INSPECT RESULT

PASS; metadata-only view of an approved registered artifact ref.

### ARTIFACT.READ RESULT

PASS; bounded text ranges, binary denied, no raw arbitrary path access.

### SKILL TOOL RESULT

PASS; only already-selected snapshot-bound Skills/resources are readable with bounded ranges.

### EVIDENCE TOOL RESULT

PASS; evidence read/glob use registered opaque scope and bounded output.

### RECOVERYDIRECTIVE RESULT

PASS; strict semantic JSON with allowed actions only and no executable payload.

### FORBIDDEN ROUTE FIELD TEST

PASS; recursive route/controller fields fail contract validation.

### RECOVERYRUNTIME RESULT

PASS; consumes only the strict directive and causal durable facts; it does not judge diagnosis text or select a graph node.

### BUDGET RESULT

PASS; limits are central config: 12 LLM turns, 24 tools, exact repeat 2, no-progress 2, observation and context bounds.

### DUPLICATE/NO-PROGRESS RESULT

PASS; repeated requests/directives and non-progressing repairs fail closed deterministically.

### CAS RESULT

PASS; durable write requires unchanged state generation and rejects conflicting concurrent mutation.

### RECOVERYOUTCOME ROUTING RESULT

PASS; `route_recovery_outcome()` is the sole semantic-outcome to fixed-edge mapping.

### CORRECTIVE GENERATE HANDOFF RESULT

PASS; Generate receives a fresh verified `GenerateSeed` containing only directive, evidence/archive refs and canonical failure identity.

### RECOVERY ARCHIVE RESULT

PASS; successful and hard-failed invocations archive provider-visible messages,
prompt version/ref, Seed digest, tool messages, directive/error, validator feedback field and terminal state. Hidden reasoning is not stored.

### CONTEXT SOFT BOUND RESULT

PASS; at 192000 chars, new large observations are externalized to immutable storage and only ref + bounded excerpt is appended.

### CONTEXT HARD BOUND RESULT

PASS; at 256000 chars, the epoch closes with deterministic
`RecoveryCheckpoint`; no free-form model summary is generated. Overflow epoch is capped at 2.

### CACHE METRICS STATUS

PASS; prompt/completion/total/reasoning/cache-hit/cache-miss values are preserved across common provider shapes. Missing cache metrics remain `None` and status is `not_reported`, never synthetic zero.

## Isolation and safety

### SCHEMA2 CALLS LEGACY RECOVERY

NO.

### SCHEMA1 COMPATIBILITY STATUS

RETAINED / PRODUCTION-REACHABLE FOR STICKY SCHEMA-1 CHECKPOINTS.

### CONTROLLER-LIKE AUTHORITY INTRODUCED

NO. Recovery emits a semantic directive; Runtime validates; LangGraph owns fixed edges.

### REAL MODEL/API RUN

NO.

### LAUNCHER CONTENT READ

NO.

### SECRETS INSPECTED

NO.

## Known limitations and removal readiness

- Automatic continuation into a linked overflow epoch is policy-gated and not enabled; hard overflow archives/checkpoints and fails closed.
- Validator feedback is archived when present; the current strict directive parser fails closed rather than running an open-ended validator repair dialogue.
- Real-provider cache reuse and a real Recovery smoke run remain unverified by this offline migration.
- The selected clean-HEAD regression run passed 197/198 tests. The sole failure,
  `ConsultantContextBudgetTests.test_static_prompt_is_compact_and_has_the_ask_and_preload_gates`,
  is also present at start HEAD `f0d8477` (`7343 > 6000`) and is outside this
  Recovery migration's allowed scope.
- Schema-1 `_invoke_recovery_v2`, legacy Recovery context/NodeContext/LLMPayload,
  decision cache and Controller-era disposition helpers remain removal candidates.

LEGACY RECOVERY REMOVAL READY: NO. Sticky schema-1 checkpoint callers and an explicit migration/sunset policy remain.

READY FOR REAL RECOVERY SMOKE TEST: NO under the Spec's all-regressions gate;
Recovery-specific offline suites pass, but the pre-existing Consultant prompt
budget regression must be resolved separately first.
