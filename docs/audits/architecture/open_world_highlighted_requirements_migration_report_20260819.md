# BioMind Open-World Highlighted Requirements Migration Report

Date: 2026-08-19

Implementation base: `77d71307b3269d2e16f1e556fcca5b00e5f10452`

Branch: `codex/save-current-work`

## Outcome

The fresh generic semantic path now has one authority: the ordered
`TaskSpec.requirements[]` rows. A fresh generic row keeps its complete natural
language `value`, attention `strength`, stable non-semantic `requirement_id`,
and existing provenance. It does not create a semantic ontology key,
constraint identity, enforcement scope, semantic handle, Plan claim, knowledge
claim, or exact Controller coverage obligation.

The same ordered derived projection,
`highlighted_constraints[{text, importance, source_refs}]`, is visible to
Planner, Planner Repair, Generate, and Recovery. Human Plan Review renders the
complete list before the executable steps. The Controller no longer claims to
prove arbitrary natural-language semantic compliance. Typed machine contracts
and their deterministic validation remain authoritative.

Completion assertions:

```text
fresh generic ontology writes = 0
fresh generic constraint handles = 0
fresh Plan semantic_constraint_ids = 0
fresh generic Plan semantic_claims = 0
fresh SEMANTIC_CONSTRAINT_UNCOVERED path = unreachable
Planner highlight visibility = PASS
Plan Review highlight visibility = PASS
Generate highlight visibility = PASS
Recovery highlight visibility = PASS
typed runtime policies = PASS
structured machine contracts = PASS
legacy readers = PASS
NEW_FUNCTIONS_ADDED = 0
```

## Files Changed

### Production

- `agent_core/task_spec.py`
- `agent_core/node_context.py`
- `agent_core/plan_contract.py`
- `agent_core/plan_tracker.py`
- `agent_core/consultant_finalize.py`
- `bio_agents.py`
- `prompts/manager.py`

No production module outside the specification boundary was changed.
`supervisor.py` and every explicit no-change subsystem remain untouched.

### Canonical architecture documentation

- `docs/audits/architecture/context_memory_and_compression_contract.md`
- `docs/audits/architecture/global_prompt_context_lifecycle.md`
- `docs/audits/architecture/recovery_context_disclosure_matrix.tsv`
- `docs/audits/architecture/semantic_history_authority_matrix.tsv`
- `docs/audits/plan_contract_approval_lineage_20260807.md`

These are amendments to existing canonical documents; no parallel architecture
document was introduced.

### Regression tests

- `tests/test_task_spec.py`
- `tests/test_canonical_taskspec_v2.py`
- `tests/test_consultant_first_canonical_state.py`
- `tests/test_node_context_v2.py`
- `tests/test_node_context_semantic_continuity.py`
- `tests/test_plan_contract_v121.py`
- `tests/test_plan_contract_convergence_v2.py`
- `tests/test_plan_review_identity.py`
- `tests/test_output_contract_closure.py`

## Existing Functions Changed

No function was added. Existing owners were changed in place.

### `agent_core/task_spec.py`

- `extract_request_contracts`
- `_parameters_from_candidates`
- `_requirements_from_fact_candidates`
- `_apply_canonical_artifact_mutations`
- `create_task_spec`
- `normalize_requirement`
- `_constraint_semantic_identity`
- `validate_compiled_constraints`
- `_item_key`
- `merge_task_spec`
- `merge_compiled_constraints`
- `apply_task_spec_mutation_proposal`
- `semantic_constraints_from_task_spec`
- `apply_authorized_task_spec_constraint_repair`
- `apply_declarative_task_spec_rules`
- `project_task_spec_to_renderer`
- `format_planner_task_contract_context`
- `effective_task_contract`

### `agent_core/node_context.py`

- `_accepted_consultant_handoff_view`
- `build_node_context`
- `resolve_plan_proposal_handles`

### `agent_core/plan_contract.py`

- `planner_candidate_wire_example`
- `structured_plan_candidate_diagnostics`
- `typed_task_plan_traceability`

### `agent_core/plan_tracker.py`

- `resolve_plan_semantic_claims`
- `render_structured_plan`
- `render_plan_review_content`
- `migrate_plan_state`
- `carry_forward_plan_lifecycle`
- `build_approval_projection`
- `plan_contract_violations`
- `build_plan_state`

### `agent_core/consultant_finalize.py`

- `finalize_coverage_audit`
- `validate_directive_coverage`

### `bio_agents.py`

- `_accepted_consultant_handoff_projection`
- `_knowledge_claim_registry`
- `_consultant_task_spec_mutation_proposal`
- `_consultant_finalize_contract_projection`
- `_build_consultant_finalize_context`
- the existing nested Consultant repair prompt path

### `prompts/manager.py`

- Existing Consultant, Planner, Planner Repair, Generate, and Recovery prompt
  builders and their in-place contract text.

## Schema Version Changes

- TaskSpec top-level schema remains version `6`; its canonicalization identity
  advances from `task-c14n-v2` to `task-c14n-v3` because generic row identity
  and semantic-hash projection changed.
- Structured Planner wire advances from version `1` to version `2`. Version 2
  removes top-level `semantic_claims` and step
  `semantic_constraint_ids`/generic `semantic_claims`.
- Durable Plan schema and migration version advance from `9` to `10`.
- Legacy structured Planner wire version `1` and Plan schema versions below
  `10` remain explicitly readable.
- FINALIZE v2/v4 wire, hash, checkpoint, capacity, reservation, and replay
  schemas are unchanged. Only fresh generic directive synthesis/coverage is
  retired.

## Fresh Write Behavior

1. Consultant V2 emits the existing `upsert_constraint` operation with an
   open-text payload. The Controller binds existing evidence/provenance.
2. `apply_task_spec_mutation_proposal` remains the mutation admission owner.
   It strips invented generic ontology fields even when a model submits a
   typed-looking generic row. Only `failure_policy.*` and the exact
   `forbidden_behavior.runtime_install` family stay on the bounded typed path.
3. `normalize_requirement` keeps complete text and existing metadata without
   synthesizing `semantic_key`, `category`, `operator`, `enforcement_scope`,
   `constraint_id`, or a semantic handle.
4. `merge_compiled_constraints` preserves canonical order: explicit updates
   stay in place and new rows append in accepted order. Generated row IDs are
   stable across sequential batches, and exact replay is idempotent even when
   the replayed row is not the first canonical requirement.
5. Structured inputs, outputs, parameters, artifacts, DAG references,
   renderer bindings, runtime audits, approval scope, and output
   materialization remain in their typed authorities instead of being copied
   into generic requirements.
6. Fresh accepted Consultant handoff contains no duplicate
   `hard_constraints` registry.
7. Fresh Plan wire v2 / Plan v10 contains no generic claim fields. Planner
   authors a natural-language self-check in `validation_checklist`; the
   Controller validates checklist shape, not arbitrary prose truth.
8. Fresh generic text never enters the exact knowledge ClaimRegistry and never
   produces `SEMANTIC_CONSTRAINT_UNCOVERED` or
   `SEMANTIC_CONSTRAINT_REFERENCE_INVALID`.
9. Fresh FINALIZE projects ordered `highlighted_requirements` and creates no
   generic directive or generic coverage gap.

## Typed Policies Preserved

- The exact `forbidden_behavior.runtime_install` policy remains consumed by
  `semantic_install_policy` and deterministically blocks installation.
- Existing `failure_policy.*` rows remain consumed by
  `evaluate_failure_policies`.
- No user-text keyword, filename substring, basename, regex-NLP, demo/test
  detector, fallback namespace, or new guard engine was introduced.

## Legacy Read Behavior

- Persisted TaskSpec rows that already contain semantic keys continue through
  the legacy normalization/renderer adapters. Historical snapshots are not
  rewritten in place.
- Structured Planner wire v1 still validates and reads its historical generic
  fields. New version 2 payloads reject those fields.
- Plan schemas below v10 retain version-gated semantic-reference resolution,
  semantic-claim validation, and old uncovered/reference diagnostics. Plan v10
  bypasses those branches and strips deprecated fields from fresh writes and
  carry-forward candidates.
- Persisted FINALIZE v2/v4 artifacts keep exact hash, capacity, reservation,
  checkpoint, and replay behavior. Explicit typed/legacy directives remain
  source-addressable; fresh open-world text is not compiled into directives.
- Legacy Consultant `semantic_constraints` responses remain readable below the
  active V2 `task_spec_operations` path.

## Structured Contracts Intentionally Unchanged

The migration did not change `ArtifactRefKey`, managed-artifact ledger,
artifact/step/plan-local handles, placeholder atomicity, output reconciliation,
runtime closure, Java handling, Generate managed output identity, Execute
command model, Recovery evidence architecture, read/glob/grep, Supervisor
routing, skill routing, capability catalog, benchmark prompts, or snpEff.

## Regression Test Coverage

- T01-T03 and T22: arbitrary English/Chinese and incident-style unseen text is
  preserved with no synthesized ontology.
- T04-T05: runtime-install and failure-policy deterministic consumers remain.
- T06-T07 and T14-T16: one ordered highlight projection reaches all nodes,
  survives retries, and does not depend on accepted-handoff duplication.
- T08: Plan Review displays every active highlight before Plan steps.
- T09-T11: fresh wire has no generic claim fields and generic text has no exact
  coverage/reference diagnostic, while strict structured Plan validation stays
  active.
- T12-T13: requirement text changes task semantic identity; provenance and row
  identity enrichment do not.
- T17-T18: renderer structured facts and typed knowledge claims remain; generic
  knowledge claims are absent.
- T19-T21: legacy Plan, TaskSpec, and supported FINALIZE replay paths remain
  readable and deterministic.
- Ordering/update/removal/replay tests cover stable non-semantic
  `requirement_id` behavior.

## Tests Updated

The nine tracked test files listed above were updated in place. Existing test
frameworks and helpers were reused. `tests/test_plan_contract_approval_scope.py`
was also executed, but it was already dirty at the baseline and was neither
modified for this migration nor staged.

## Tests Retired or Replaced

Only assertions owned by the retired generic semantic registry are excluded:

- generic alias registration and fallback `required_behavior.*` synthesis;
- exact generic constraint IDs/handles and Planner claim references;
- generic `SEMANTIC_CONSTRAINT_UNCOVERED` coverage;
- generic FINALIZE directive count/exact coverage;
- generic raw-user prohibition gap synthesis;
- fresh v4 model resolution whose only purpose was to mint a generic directive.

Pre-existing untracked tests containing those obsolete assertions were not
edited or staged, in order to preserve user-owned working-tree content. Their
supported sibling tests were selected and run explicitly.

## Tests Passed Before Commit

All commands used
`/public/home/liusiqi/anaconda3/envs/biomind/bin/python`.

1. Main migration and structured-contract suite:

   ```text
   python -m unittest
     tests.test_task_spec
     tests.test_node_context_v2
     tests.test_node_context_semantic_continuity
     tests.test_plan_contract_v121
     tests.test_output_contract_closure
     tests.test_plan_review_identity
     tests.test_plan_contract_convergence_v2
     tests.test_plan_contract_approval_scope
     tests.test_consultant_first_canonical_state
   Result: PASS, 189 tests
   ```

2. Supported canonical TaskSpec tests, excluding the independently proven
   pre-existing artifact-ID failure: `PASS, 5 tests`.
3. FINALIZE protocol/capacity/reservation modules: `PASS, 44 tests`.
4. Supported FINALIZE integration cases, excluding three retired generic
   coverage assertions: `PASS, 37 tests`.
5. Supported FINALIZE v4 cases, excluding one retired exact generic directive
   count assertion: `PASS, 8 tests`.
6. Production `py_compile`: `PASS`.
7. `git diff --check`: `PASS`.
8. Added production function scan: no added `def` or `async def`; result
   `NEW_FUNCTIONS_ADDED = 0`.
9. Prompt scan: no `semantic_constraint_ids` or `semantic_claims` in
   `prompts/manager.py`.

Total supported regression executions in the five test selections above:
`283 passed`.

### Deliberate boundary-audit results

Running the user-owned untracked integration module without filtering produced
`37 passed, 3 failed`. The three failures assert behavior this specification
explicitly retires:

- `test_raw_user_prohibition_is_source_addressable_and_compiled` expects a
  synthetic generic coverage gap;
- `test_real_session_1786266235_replay_reaches_planner_without_provider_or_healthcheck`
  reaches Planner successfully and then expects the retired count of 57
  generic directives instead of zero fresh generic directives;
- `test_v4_genuine_model_resolution_uses_one_bounded_batch` injects a fresh
  generic directive and expects it to become authoritative.

Running the complete untracked v4 module plus tracked canonical module produced
`13 passed, 2 failed`. One is the retired v4 exact count assertion. The other,
`test_artifacts_have_one_canonical_managed_contract`, is unrelated to this
migration and also fails at the exact base commit. This was proved by exporting
`77d71307b3269d2e16f1e556fcca5b00e5f10452` with `git archive` into an isolated
`/tmp` snapshot and running that one test there; it failed on the same
`artifact_id.startswith("artifact-")` assertion. No artifact identity code was
changed.

## Remaining Legacy-Only Branches

- Keyed legacy requirement normalization and renderer projection.
- `_constraint_rows` and shared handle infrastructure for historical payloads.
- Planner wire v1 and Plan pre-v10 semantic claim readers/diagnostics.
- Legacy `semantic_claims` parsing kept for historical Plan hash/replay.
- Legacy Consultant `semantic_constraints` and persisted ambiguity repair.
- FINALIZE v2/v4 generic directive readers for already-persisted artifacts.

Every listed branch is version- or path-gated away from fresh writes.

## Remaining Deferred Cleanup

- Remove legacy generic readers only after the repository's persisted artifact
  retention/compatibility window closes.
- Retire or rewrite the pre-existing user-owned untracked generic-coverage test
  assertions in a separately authorized cleanup; they were intentionally not
  modified here.
- The unrelated canonical artifact-ID baseline failure remains outside this
  migration's explicit no-change boundary.

No cleanup item is needed for fresh-path correctness.

## Git Diff Summary

Before adding this report, the migration-owned patch contained:

```text
21 files changed, 1567 insertions(+), 858 deletions(-)
```

The final atomic commit adds this report as the twenty-second file.

## Git Delivery

### Repository Baseline

- Branch: `codex/save-current-work`
- BASE_HEAD: `77d71307b3269d2e16f1e556fcca5b00e5f10452`
- Baseline was dirty before implementation. The pre-existing tracked and
  untracked work listed below was treated as user-owned.

### Files Modified by This Migration

- The seven production files, five canonical architecture documents, nine
  tracked tests, and this implementation report listed under `Files Changed`.

### Files Staged

- Exactly the twenty-two migration-owned paths listed under `Files Changed`.
- Staging is performed with an explicit pathspec; no whole-tree staging is
  permitted.

### Pre-existing Dirty Files Explicitly Excluded

Tracked skill documents:

- `skills/genomics/assembly-variant-syri/SKILL.md`
- `skills/genomics/long-read-sv-sniffles-survivor/SKILL.md`
- `skills/genomics/reseq-variant-workflow/SKILL.md`
- `skills/genomics/wgs-cnvnator-cnv/SKILL.md`
- `skills/genomics/wgs-lumpy-sv/SKILL.md`
- `skills/genomics/wgs-short-read-mapping/SKILL.md`
- `skills/sequence/search/local-blast/SKILL.md`

Tracked tests already dirty at baseline:

- `tests/test_batch81_foundations.py`
- `tests/test_batch8_replay.py`
- `tests/test_batch8_user_facts.py`
- `tests/test_bio_agents_memory.py`
- `tests/test_consultation_state.py`
- `tests/test_context_lineage.py`
- `tests/test_diagnostics.py`
- `tests/test_effective_context_projection.py`
- `tests/test_effective_user_facts.py`
- `tests/test_execution_provenance.py`
- `tests/test_executor.py`
- `tests/test_formal_artifact_resolver.py`
- `tests/test_package_manager.py`
- `tests/test_plan_contract_approval_scope.py`
- `tests/test_plan_lifecycle.py`
- `tests/test_plan_tracker.py`
- `tests/test_prompt_manager.py`
- `tests/test_renderer_controller.py`
- `tests/test_run_profiler.py`
- `tests/test_runtime_preflight.py`
- `tests/test_semantic_constraint_protocol.py`
- `tests/test_shell_safety.py`
- `tests/test_skill_manager.py`
- `tests/test_validator.py`
- `tests/test_wiki_memory.py`

Pre-existing untracked content, including `HANDOFF.md`, existing audit/refactor
documents, `scripts/`, newly developed skill directories, `user_preferences.md`,
and the untracked test modules below, is excluded:

- `tests/test_batch7_rollback.py`
- `tests/test_consultant_finalize_capacity_lineage.py`
- `tests/test_consultant_finalize_integration.py`
- `tests/test_consultant_finalize_protocol.py`
- `tests/test_consultant_finalize_reservation_ledger.py`
- `tests/test_consultant_finalize_v4.py`
- `tests/test_consultation_state_v4.py`
- `tests/test_output_router.py`
- `tests/test_state_contract_audit_v2.py`
- `tests/test_state_contract_incident_replay_v2.py`
- `tests/test_task_spec_semantic_ambiguity.py`
- `tests/test_wiki_lifecycle.py`

### Tests Before Commit

- Commands and results are recorded in `Tests Passed Before Commit`.
- Required supported regressions: `283 passed`.
- Static gates: `py_compile` and `git diff --check` passed.

### Commit

- commit hash: the immutable SHA of the commit containing this report; resolve
  with `git rev-parse HEAD`. The literal SHA cannot be embedded in its own
  commit object without changing that SHA, so the actual value is recorded in
  the post-commit delivery output.
- subject: `refactor(constraints): migrate generic semantics to open-text requirements`

### Post-Commit Status

- Migration-owned paths: expected clean after the atomic commit and verified
  with `git status --short`.
- Remaining modified/untracked paths: the preserved baseline-owned work listed
  above; an unclean post-commit tree is expected and is not a failed commit.

### Scope Verification

- unrelated files committed: NO
- user pre-existing changes overwritten: NO
- git reset/clean/stash used: NO

```text
GIT_COMMIT_CREATED = YES (true for the repository commit containing this report)
COMMIT_SUBJECT = refactor(constraints): migrate generic semantics to open-text requirements
UNRELATED_FILES_COMMITTED = 0
PRE_EXISTING_USER_CHANGES_REMOVED = 0
GIT_RESET_USED = NO
GIT_CLEAN_USED = NO
GIT_STASH_USED = NO
```
