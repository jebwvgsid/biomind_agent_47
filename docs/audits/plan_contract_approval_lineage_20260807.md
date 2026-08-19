# Plan Contract and Approval Lineage Audit

Date: 2026-08-07

## Starting State

- Branch: `codex/save-current-work`
- Start commit: `d4a74a2d8baf08b85a1f8fe966ad745ccf5d5486`
- The worktree already contained unrelated modified skills, installer tests, and audit documents. They were not reverted.

## Active Path Findings

1. `TaskSpec` was authoritative for task inputs and outputs, but duplicate bindings retained only one scalar source. Benchmark question and controls also reached the agent as one flattened prompt.
2. `PlanTracker` stored one effective plan plus revision history. Approval bound only plan semantic hash, plan revision, and task revision.
3. `render_plan_review_content()` rendered machine plan text, but the exact displayed projection was not hashed into approval.
4. `_bind_pending_action()` bound task revision, plan version, and step ID. It did not bind the complete approval scope.
5. `_pending_action_stale_reason()` and `_validate_pending_action_contract()` were the existing Generate/resume/Execute guards and were reused.
6. Renderer capability metadata could add/remove output ownership before an invocation contract existed.
7. Local filesystem validation written as SEARCH was silently rewritten during normalization. The invalid Planner contract was therefore not visible to typed PlanPatch.
8. `_ainvoke_llm()` logged chars and elapsed time but did not emit common token/headroom/duplicate telemetry for every node.

## Implemented Contract

Current amendment (2026-08-19): generic natural-language requirements are
reviewed through the full ordered Highlighted Constraint List. Fresh Planner
wire v2 / Plan schema v10 no longer persist generic semantic claims or
constraint references. `render_plan_review_content()` shows all highlights
before the steps, and changing requirement text still invalidates the Task
semantic/approval scope. Provenance-only enrichment does not. The Controller
does not claim to prove arbitrary prose compliance; all structured approval,
artifact, renderer, and action-lineage checks below remain deterministic.

- RequestEnvelope separates canonical request text, raw submission text, benchmark source, and controls.
- Input, requirement, and output bindings preserve a `provenance[]` list while retaining legacy scalar source fields.
- `output_contracts` is authoritative; `expected_outputs` is a compatibility projection.
- Plan state has orthogonal `revision_status`, `approval_status`, and `execution_status` fields.
- ApprovalProjection is built deterministically from TaskSpec and canonical Plan steps.
- ApprovalScope hashes task, Plan, TaskSpec, inputs, selected skills, renderer projection, and displayed projection.
- Pending actions carry `generated_from_plan_hash`, `generated_from_approval_scope_hash`, and `action_hash`.
- New approvals are checked at Generate/resume/Execute boundaries through the existing stale-action guard.
- Renderer outputs become owned only after a deterministic RendererInvocationContract is projected from TaskSpec.
- SEARCH remains KB retrieval. Local file validation produces `INVALID_ACTION_FOR_STEP_PURPOSE` before review.
- Prompt telemetry reports estimated input tokens, context limit, headroom, component chars, and exact duplicate message chars.
- Planner persists first-pass schema/contract validity, repair reasons, semantic change, and call counts.

## Canary

Task ID: `canary_snpeff_plan_contract_20260807`

- Runner dry-run resolved benchmark index 56 and the expected strict output directory.
- Live LLM execution was not started because no API key was available in the test environment.
- Deterministic canary `tests/test_plan_contract_canary.py` verifies A-E lifecycle semantics: structured request contract, first-plan violations, repaired Renderer invocation ownership, exact approval, stale mutation rejection, and action lineage.

## Tests

- Focused contract and renderer set: 128 tests passed.
- Additional canary/ownership set: 13 tests passed.
- Python compile checks passed.
- `git diff --check` passed before commit preparation.
- `pytest` was unavailable in the `biomind` environment.
- Repository-wide unittest invocation is not a valid discovery mode because `tests/` is not a package; the explicit all-file invocation is interrupted by CLI-oriented test modules. Focused active-path suites are the authoritative result.

## Known Limits

- Legacy approval records without an ApprovalScope remain readable for checkpoint compatibility. Every newly approved plan uses the strict scope.
- Token estimates use the provider-neutral `ceil(chars/4)` approximation because the configured DeepSeek tokenizer is not locally available.
- A live A-E benchmark requires a configured API key and human approval interaction.
- CompletionGate has a pre-existing test isolation issue that may scan unrelated shared `/tmp` status files.
