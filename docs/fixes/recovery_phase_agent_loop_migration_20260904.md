# Schema-2 Recovery Phase-Local Agent Loop Migration

Date: 2026-09-04
Branch: `refactor/consultant-controller-authority`
Start HEAD: `f0d8477e3d8a95d243b1b5917eb594242bed33f4`

## Outcome

Schema-2 Recovery now follows one production architecture:

```text
explicit causal FailurePacket
  -> frozen RecoverySeed
  -> stable System + private append-only RecoveryAgentLoop
  -> scope-bound read-only inspection tools
  -> strict RecoveryDirective
  -> deterministic RecoveryRuntime
  -> RecoveryOutcome
  -> fixed LangGraph edge
```

Corrective Generate starts with a new verified `GenerateSeed`. It does not
inherit the Recovery transcript. Full Recovery episodes and large observations
are content-addressed archives that later phases can inspect explicitly.

## Authority boundaries

- RecoveryAgent: diagnose one causal failure and propose correction semantics.
- RecoveryRuntime: validate identity, scope, staleness, evidence subset, budget,
  duplicate/no-progress, idempotency and CAS.
- LangGraph: map `RecoveryOutcome.type` to a fixed edge.
- TaskSpec, Plan, selected Skills, artifact identities and graph routes remain
  owned outside Recovery.

No Controller-like resolver or legacy route interpretation was added.

## Context and cache behavior

- System prompt bytes are stable for one prompt version.
- Seed is constructed once and frozen per invocation.
- Provider history is exact append-only; transport retry reuses identical input.
- Seed target/hard limits are 16k/32k characters.
- Local soft/hard limits are 192k/256k characters.
- Inline tool limits are 24k single and 96k cumulative.
- Large new observations are externalized behind immutable refs.
- Hard overflow creates a deterministic `RecoveryCheckpoint` and closes the epoch.
- Provider usage preserves reasoning and cache hit/miss values; unreported cache
  data remains missing with `cache_metric_status=not_reported`.

## Compatibility decision

Schema-1 Recovery was not deleted. `agent_core/middleware.py` still contains
production-reachable `_invoke_recovery_v2` callers for sticky schema-1
checkpoints, and no explicit checkpoint migration/sunset policy has closed that
gate. Automated tests prove the Schema-2 handler and modern Recovery modules do
not call the legacy path.

## Commits

- `d484668` RCTX-01 phase-local context infrastructure
- `d4f70c2` RCTX-02 causal FailurePacket authority
- `a1490b3` RCTX-03 StepWorker archive
- `8ea87aa` RCTX-04 scoped Recovery tools
- `3ee96a8` RCTX-05 stable System and frozen Seed
- `c1afac4` RCTX-06 private Recovery agent loop
- `61ad878` RCTX-07 deterministic directive admission
- `4af63a4` RCTX-08 corrective Generate handoff
- `90ea50e` RCTX-09 context bounds/archive/checkpoint
- `27110ca` RCTX-10 provider cache metrics
- `7b55ad0` RCTX-11 schema isolation proof

## Verification boundary

Verification used only offline fake providers, sanitized fixtures, deterministic
tools and temporary directories. No real model/API, launcher, secret, Java,
snpEff, long BioMind workflow or benchmark run was used.

The selected final clean-HEAD regression run passed 197 of 198 tests. Its only
failure is a pre-existing Consultant static-prompt budget assertion
(`7343 > 6000`), reproduced unchanged at start HEAD `f0d8477`. Recovery-specific
tests pass; overall migration acceptance remains blocked by that unrelated
baseline regression under the Spec's strict all-tests gate.

Detailed current evidence is in
`docs/audits/architecture/RECOVERY_PHASE_CONTEXT_CURRENT.md`.
