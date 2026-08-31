# Incremental Migration Plan

Each batch is independently testable and reversible. Production logic is not
deleted until the relevant replay tests pass.

## Batch 0: Observability And Baseline

**Goal:** make raw, parsed, normalized and bound actions traceable without storing
secrets or unbounded prompts.

**Not included:** behavior changes or routing changes.

**Core files (3-5):** `bio_agents.py`, `execution/executor.py`,
`agent_core/plan_tracker.py`, `tests/test_renderer_controller.py`,
`tests/test_plan_lifecycle.py`.

**Reuse:** existing profiler, execution records, RecoveryStateStore and action
hashing.

**Data changes:** add bounded audit events for `raw_action_hash/excerpt`,
`parsed_action`, `normalized_action`, `bound_action`, `allowed_actions`, step and
budget snapshots. Store large raw content by path.

**Compatibility:** fields are optional for old checkpoints.

**Tests:** assert three action layers are distinguishable and no API key or full
environment dump is persisted.

**Replay:** session `1785398207` must show ordinary EXECUTE before compatibility
binding.

**Success:** one event chain explains every action transformation.

**Rollback:** remove optional audit fields/log calls.

## Batch 1: Close The Renderer Protocol Hole

**Goal:** stop probes and free-form commands from becoming Renderer invocations.

**Not included:** TaskSpec extraction, Recovery/Supervisor refactor.

**Core files (4-6):** `bio_agents.py`, `prompts/templates.py`,
`prompts/manager.py`, `tests/test_renderer_controller.py`,
`tests/test_plan_lifecycle.py`, optionally `tests/test_prompt_manager.py`.

**Reuse:** `_extract_structured_action()`,
`_normalize_renderer_invocation_arguments()`,
`_materialize_renderer_invocation()`, `_validate_pending_action_contract()`.

**Changes:** remove substring-only provenance inference; derive dynamic allowed
actions from current step and manifest state; reject ordinary EXECUTE before a
manifest-absent Renderer step; require RecoveryPolicy provenance for probes;
prevent probe success from running the formal artifact gate.

**Compatibility:** a narrowly validated legacy invocation may pass only when it
contains the declared entrypoint and satisfies the full declared invocation
schema. It must be logged as legacy.

**Unit tests:** probe mentions Renderer; `--help`; full legacy invocation;
structured invocation; format repair; manifest-present action selection.

**Integration/replay:** session `1785398207` stops before Step 7 is executed and
requests a structured Renderer action instead.

**Success:** only `_materialize_renderer_invocation()` can create
`action_source=renderer` and `command_role=renderer_invocation` for new sessions.

**Rollback:** restore compatibility adapter behind a temporary feature flag.

## Batch 2: Structure Task Inputs And Renderer Projection

**Goal:** project verified user inputs into Renderer arguments without CLI probes.

**Not included:** changing scientific intent selection or adding domain-specific
parsers.

**Core files (4-6):** `agent_core/task_spec.py`,
`agent_core/formal_artifact_resolver.py`, `agent_core/skill_manager.py`,
`bio_agents.py`, `tests/test_task_spec_integration.py`,
`tests/test_renderer_controller.py`.

**Reuse:** TaskSpec requirements, formal input audit, SkillManager Renderer
invocation metadata and Renderer argument normalizer.

**Changes:** create a generic explicit-fact projection (role/path/parameter/output
contract); merge verified resolver evidence into TaskSpec through a controlled
patch; map roles to declared Renderer argument fields; preserve ambiguity rather
than guessing.

**Compatibility:** old TaskSpecs retain `unknown`; original request remains the
fallback source but is not reparsed repeatedly.

**Tests:** absolute/relative paths, output directory, leading-dash parameter
values, ambiguous roles, missing required argument and old checkpoint migration.

**Replay:** the snpEff task projects VCF/FASTA/GTF/output directory and format
intent without reading Renderer source/help.

**Success:** Renderer materialization is deterministic when required facts are
present.

**Rollback:** disable structured projection and fall back to explicit contract
reconciliation, never free-form probing.

## Batch 3: One RecoveryPolicy

**Goal:** remove duplicated recovery adjudication while retaining existing
diagnostics and decision schemas.

**Not included:** deleting Supervisor or CompletionGate.

**Core files (4-6):** `agent_core/recovery.py`, `agent_core/middleware.py`,
`bio_agents.py`, `agent_core/plan_tracker.py`, `tests/test_recovery.py`,
`tests/test_shell_safety.py`.

**Reuse:** `evaluate_recovery_trigger()`, `build_diagnostic_context()`,
`deterministic_recovery_decision()`, `constrain_recovery_decision()` and
`recovery_action_route()`.

**Changes:** extract one shared recovery-adjudication function; make active
middleware an exception/evidence boundary only; replace
`_adjudicate_post_review_recovery()` internals with the shared policy; Controller
alone maps decision to graph node; PlanTracker owns all budget increments.

**Compatibility:** retain old state fields as read-only aliases for one migration
period. Keep `_legacy_error_recovery_middleware` unused, then delete it after
replay coverage passes.

**Tests:** same evidence through Execute and post-review entrances yields the same
decision; probes do not consume execution retry; only PLAN ownership reaches
Planner; internal protocol errors never ask the user.

**Success:** exactly one function produces each semantic recovery decision.

**Rollback:** route both callers back to the old wrappers while keeping the new
audit fields.

## Batch 4: Narrow Supervisor And CompletionGate

**Goal:** separate action validation from step/task completion.

**Not included:** physical node removal or graph rewrite.

**Core files (4-6):** `supervisor.py`, `agent_core/completion_gate.py`,
`bio_agents.py`, `tests/test_supervisor.py`, `tests/test_completion_gate.py`,
`tests/test_plan_lifecycle.py`.

**Reuse:** RuleEngine, structured ExecutionEvidence, manifest helpers and GateResult.

**Changes:** Supervisor returns action validation evidence without `next_step` or
budget writes; remove global/final artifact ownership from Supervisor;
CompletionGate consumes normalized action evidence but performs only step/task
contract checks; RecoveryPolicy handles every failed result.

**Compatibility:** temporarily derive legacy `supervisor_verdict` from the new
validation result.

**Tests:** action PASS with step gate fail; action WARN with final gate pass;
nonzero exit cannot be overridden by LLM; Gate never recommends a tool or retry.

**Success:** Supervisor and CompletionGate can disagree only because they validate
different scopes, and the disagreement has a structured classification.

**Rollback:** re-enable legacy verdict-to-route adapter.

## Batch 5: Simplify Graph And State

**Goal:** remove obsolete wrappers and duplicate transient fields after all replay
tests pass.

**Not included:** new features or large naming changes.

**Core files (3-6):** `bio_agents.py`, `agent_core/middleware.py`,
`agent_core/state.py`, `agent_core/plan_tracker.py`, graph/lifecycle tests.

**Changes:** reduce routers to one transition table; delete dead legacy recovery
middleware; consider inlining action validation as a hook; remove stale graph
fields after checkpoint migration; keep CompletionGate as the final contract
boundary.

**Success:** no production component except Controller writes `next_step`; no
component except PlanTracker durably writes budgets/status.

**Rollback:** retain schema adapters and the prior graph definition for one release.

## Recommendations To Defer

- Do not create new physical PreAction/PostExecution node classes in Batch 1.
- Do not merge Supervisor, RecoveryJudge and CompletionGate into one class.
- Do not make Planner responsible for runtime graph routing.
- Do not delete middleware tests before replacing their covered boundary.
- Do not rename broad state schemas while changing behavior.
- Do not add snpEff-specific routing or argument code.
