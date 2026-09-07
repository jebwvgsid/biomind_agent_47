# Runtime evidence interfaces — RT-01

Production: `_consultant_runtime_seed_evidence` calls scoped `_consultant_runtime_preflight`; only a real observation is sealed by `make_runtime_evidence`. The actual select_skill, update_taskspec and submit_planning_handoff returns carry the internally produced audit into `_commit_consultant_update` → `commit_consultant_candidate_state` → existing `save_plan_state` CAS. An uninitialized semantic task uses the same evidence acceptance helper through `commit_runtime_evidence_state`.

The single durable truth is `task_state.runtime_evidence.records`; `state.runtime_audit` is a derived compatibility view. Planner, resume, Generate, action binding and Execute read `_runtime_binding_source` / `_runtime_audit_projection`. Runtime evidence does not approve an action, advance a step or replace Execute manifests/prelaunch.

Records retain immutable observation identity, trusted producer/source, Skill document and contract digests, environment prefix/runner/host scope, method digest, original observed_at, expiry, fact coverage/status and task acceptance revision. Raw stdout, environment dumps and internal approval data are not model feedback. Consultant feedback uses an explicit semantic allowlist.

`evaluate_runtime_evidence` preserves UNKNOWN / AVAILABLE / UNAVAILABLE / AMBIGUOUS separately from inspection status. Explicit negative/conflict facts override materialized paths generically, including existing derivations. Runtime schema-2 consumption filters current scope; an absent graph schema field cannot select legacy authority. Old summary-only state remains UNKNOWN. Schema-1 decoding and the old projection remain supported.

RT-02/03 are not delivered by this commit: typed acquisition/resume, durable episodes/budgets and runtime-specific Recovery/reporting remain subsequent work. The resolver still has its prior terminal missing-binding control path until RT-02.
