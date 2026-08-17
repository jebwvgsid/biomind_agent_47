# Recovery Progressive Disclosure Git Provenance

Status: read-only architecture provenance

Audit snapshot: `358c31d1b6ef8a159315e7f0ec01aa37345bd196` on branch `codex/save-current-work`, including the pre-existing dirty worktree on 2026-08-17. The production files cited here had no local diff at audit start.

## Verdict

The exact target protocol — small failure capsule, RecoveryJudge call, model-requested evidence category, targeted Controller expansion, and a second call — has never existed in the inspected Git history.

There was nevertheless a real historical category-level mechanism. Batch 9B assembled one of three semantic context levels before the only RecoveryJudge call. It was failure-class/conflict driven, not model-request driven, and was removed by the NodeContext v2 migration. The distinction matters:

- historical Batch 9B: deterministic **tiered pre-disclosure**;
- retained legacy middleware: retry-count-based **skill/RAG/external hint expansion**, but dead in the active graph;
- historical and current `prompt_view()`: size-driven **compression fallback**;
- requested target: iterative, evidence-driven **progressive disclosure**;
- current production: two one-shot NodeContext v2 Recovery entry paths, with the Execute path retaining `prompt_view()` only as a legacy capacity gate.

## Architecture evolution

| Commit | Date | Change and design intent | Effect on active Recovery path | Current status |
|---|---|---|---|---|
| `a724a8adec8e2312cf996fa5033deef65dbd21ce` | 2026-06-12 17:05 +08:00 | Added `_recovery_tier(retry_count)` behavior to the old observation-text middleware: tier 0 used local stdout/stderr; tier 1 added selected skill/RAG context; tier 2 added an external-search policy hint. | This expanded retrieval hints across separate failed workflow attempts. It did not expose TaskSpec/runtime/Plan sections on model request and did not perform a same-decision second call. | Function remains in source as `_legacy_error_recovery_middleware`, explicitly deprecated and not registered. |
| `21243aa25406f347d51bbcd8810ec20250b37c3f` | 2026-08-02 23:20 +08:00 | Introduced `RecoveryContext`, the 26,000-character `DiagnosticLimits`, filesystem snapshot, and `prompt_view()`. The original comment said it preserved section names while “progressively shrinking” nested values; it started broad and shrank values. The same commit introduced the scanner and basename-only internal-state denylist. | Broad collection and compression existed from the first committed `RecoveryContext`; no model response field requested a section and no Controller loop expanded it. | Core durable object/scanner remain; the original projection was rewritten. |
| `e8a2e60da0da9409af97f5ac60e462d253ac7b21` | 2026-08-02 23:40 +08:00 | Added the structured Execute recovery middleware, exact prompt snapshots, and decision lineage; renamed/marked the older path `DEPRECATED / NOT REGISTERED IN THE ACTIVE GRAPH`. | Active Execute built a broad context, selected one `prompt_view()`, and made at most one RecoveryJudge call. | Structured lineage remains; provider projection migrated to v2. Dead retry-tier code remains only for old imports/tests. |
| `e7b43f9b7881a9a2176ee63624dc0508ac805141` | 2026-08-03 16:09 +08:00 | Added durable execution/recovery lineage and bounded context infrastructure. | Strengthened replay/history inputs; it did not add a request-more protocol. | Durable history concepts remain in newer forms. |
| `687e224f65e342226b304e7358fa4c4c24f558df` | 2026-08-05 14:55 +08:00 | Batch 9B added `RecoveryLevel.LEVEL_1/2/3`, `initial_recovery_level()`, `assemble_recovery_context()`, block manifests, and 12k/24k/40k-token-equivalent budgets. L1 held current attempt/validation; L2 added TaskSpec, user facts, consultant handoff, skills, recovery history, and progress; L3 added provenance, runtime, dependencies, and state identity. | Failure anchor selected an initial level; plan/step/runtime/reference conflicts or recurrence could raise the effective level before the one Judge call. There was no provider feedback expansion. | Removed from current production by `21bfffee`; completion report remains historical evidence. |
| `f19a5ccd17c9b0f676fa05829dfa5bc85a418f96` | 2026-08-05 17:27 +08:00 | Batch 9C added a frozen adjudication envelope and one constrained RecoveryJudge proposal. Its schema included requested evidence types for a deferred/inconclusive decision. | Both entry points still made one call against the already assembled effective level. Requested evidence did not trigger a same-decision second provider call; probe/application work was explicitly deferred. | Batch 9C assembly/adjudication API was later removed/replaced. |
| `21bfffeed64eef9b3a723736576be512ad9bf8ca` | 2026-08-12 14:30 +08:00 | Introduced NodeContext v2 and current-step dependency closure. Removed Batch 9B/9C `RecoveryContextAssembly` from current code. Rewrote `prompt_view()` to preserve all semantic sections and compact every evidence category at four value limits. Its docstring explicitly marks it as a legacy diagnostic view. | Execute still calls `prompt_view()` as a capacity gate, then builds a separate NodeContext v2 provider payload. Post-review bypasses the legacy compact view and builds NodeContext directly. | Active architecture. |
| `b2ba4e4f884b7675a90d5d768f6ae1acf2a096f0` | 2026-08-13 11:56 +08:00 | Closed plan-local binding and approved-plan current-step convergence. | Refined current-step/artifact closure in NodeContext; did not add expansion levels or a second call. | Active architecture. |
| `34934de0c92d7cae268a101a3dce13606039c21e` | 2026-08-17 13:03 +08:00 | Reconciled managed artifact realizations. | Adjusted artifact behavior but did not restore progressive disclosure. | Active architecture before this documentation series. |

## What Git proves and does not prove

Git proves that Batch 9B briefly implemented semantic levels and failure-driven pre-call escalation. It also proves that the filesystem scanner was not a later bypass around an older small-first implementation: scanner, broad `RecoveryContext`, and compression appeared together in `21243aa`.

Searches across current code and history for `requested_context`, `requested_sections`, `need_more_context`, `missing_context`, and semantic equivalents found no Controller-owned same-incident expansion loop. `evidence_sufficient` and `missing_evidence` describe the decision's sufficiency; in the current prompt, insufficient evidence means `ESCALATE`, not “return a disclosure request.”

Therefore the most accurate classification is:

```text
True iterative progressive disclosure: A — never implemented.
Historical semantic tiering: B-like partial — implemented in Batch 9B, then removed.
Legacy source classification: C — retry-tier hint expansion still exists but active Execute does not call it.
Current topology: E — two one-shot Recovery paths coexist.
Current execution-failure behavior: MISSING, with current-step projection PARTIAL.
```

## Evidence commands

The audit used non-mutating `git log`, `git log -S`, `git log -G`, `git blame`, and `git show`. High-value historical symbols were:

```text
RecoveryLevel
initial_recovery_level
assemble_recovery_context
RecoveryContextAssembly
effective_level
prompt_context
prompt_view
_invoke_recovery_v2
```

No historical checkout, reset, worktree rewrite, or code/test change was performed.
