# BioMind Planner Full-Replan Context Convergence Implementation Specification

**Status:** Implementation-ready specification; no production implementation in this turn

**Date:** 2026-08-20

**Repository:** `/public/home/liusiqi/03.毕设/Biomind_v1`

**Target area:** Planner candidate generation, deterministic admission, Planner re-entry context, and terminal projection

**Out of scope for this document turn:** production/test edits, real workflow execution, snpEff execution, session-artifact mutation, and Git commits

## 1. Executive Decision

### A. Can Planner semantic `PlanPatch` be retired safely?

**Decision: YES, after one prerequisite is implemented; it is not safe to delete first.**

Current-HEAD evidence:

- `build_plan_repair_request()`, `parse_plan_patch()`, `stamp_plan_patch_control_binding()`, and `apply_plan_patch()` are used by the production Planner path in `bio_agents.py`; no other production node applies this semantic patch object.
- Recovery emits and routes `PLAN_PATCH_REQUEST`, but that is a request to re-enter Planner, not a dependency on the `replace_steps` wire object or on `apply_plan_patch()`.
- No current deterministic violation code intrinsically requires a delta patch. A complete corrected `PLAN_CANDIDATE` can express step addition, removal, replacement, output materialization, DAG changes, and plan-local artifact changes more completely than the existing patch contract.
- The current patch is unable to repair some valid global failures. In particular, `REQUIRED_OUTPUT_UNCOVERED` can yield `allowed_step_ids=[]`, while `apply_plan_patch()` can only replace existing allowed steps. This is an unrepairable repair contract, not a reason to retain it.

The prerequisite is a deterministic **full-replan transition gate**. `carry_forward_plan_lifecycle()` currently preserves step status/evidence only when old and new semantic plan hashes are equal. On a real semantic replan it resets approval but does not protect already executed work. Before semantic PlanPatch is removed, the Controller must:

1. derive the immutable executed prefix and durable evidence from the previous PlanState;
2. reject a candidate that rewrites, removes, or reassigns identity for completed/running work in an unsafe way;
3. carry forward Controller-owned status, evidence, provenance, and runtime partitions;
4. allow the failed/current step and pending suffix to be replaced as a complete plan;
5. require normal review/approval for every semantic change.

There is therefore no patch-only violation exception. The safety exception is lifecycle state, and it is handled deterministically after a full candidate is returned—not by retaining an LLM delta protocol.

### B. Must structure-only repair remain after semantic PlanPatch retirement?

**Decision: keep the capability, remove the independent repair state machine.**

Malformed transport, invalid JSON, invalid outcome envelope, and typed schema errors still require correction. They must not be silently accepted or converted into semantic guesses. However, Current HEAD performs an immediate nested `planner_structure_repair` provider call with a separate prompt/stage. The target architecture records the structure diagnostics in the same durable `planning_failure`, routes to the next Planner graph attempt, and asks for one complete plan through the same System/Human contract.

Target semantics:

- `validation_source = "planner_structure"`;
- `current_focus.planning.mode = "correct_previous_plan"`;
- `instruction.return_complete_plan = true`;
- one provider call per Planner graph attempt;
- no structure patch object, no separate structure cache, and no second Planner prompt authority.

Provider/network retry behavior in the shared LLM transport remains unchanged; this decision concerns Planner candidate correction, not HTTP retry handling.

### C. Where is the unique durable owner of machine Planner failure?

**Decision: `plan_state.json -> task_state.planning_failure`.**

This is the only suitable owner because PlanState is already the durable CAS-backed task/plan state, and a Planner candidate can fail before it becomes a committed `current_plan` or `PlanSnapshot`.

- `planner_feedback` remains Consultant/human-authored typed plan guidance.
- `validation_issues` remains a derived result of validating a specific plan/candidate; it is not a re-entry lifecycle owner.
- `plan_validation_state` remains validation state for the current PlanSpec and cannot own a rejected pre-commit candidate.
- top-level `failure` remains the general execution/framework failure contract.
- graph `AgentState` fields are projections only.
- `run_metrics.json`, `blocked_status.json`, and terminal messages are derived terminal projections.

New writes to `planner_candidate_rejection` stop as soon as the migration lands. A compatibility reader converts an old unresolved value to `task_state.planning_failure`; both fields must never be independently writable.

### Complexity decision

| Concern | Current architecture | Target architecture |
| --- | --- | --- |
| Candidate protocols | full candidate + structure full candidate + semantic `PlanPatch` | one full `PLAN_CANDIDATE` protocol |
| Provider repair prompts | initial + structure repair + plan repair | one stable Planner System contract + one dynamic Human payload |
| Machine failure owners | `validation_issues`, `planner_candidate_rejection`, repair audit/cache, graph messages | one durable `task_state.planning_failure`; other values are projections |
| Repair scopes | `allowed_step_ids`, violation-derived scope, patch mutable fields | deterministic lifecycle transition policy derived from PlanState |
| Planner repair cache | raw `planner_patch:*` can be saved before acceptance | no Planner repair-result replay cache |
| Retry convergence | candidate-repair budget + Planner-call budget + no-op patch signature | PlanningEpoch call budget + current failure fingerprint/repeat count |

The target removes protocols and durable facts; it does not introduce a semantic reviewer, repair supervisor, second validator, event bus, or history database.

## 2. Current HEAD Boundary

The specification is bound to this exact checkout:

```text
HEAD:   516db73aeadd81a2bebe567b80b75b996237c757
Branch: codex/save-current-work
Commit: 516db73 refactor: stabilize execution recovery contracts
```

The checked `git log --oneline --decorate -20` begins:

```text
516db73 (HEAD -> codex/save-current-work) refactor: stabilize execution recovery contracts
8ce98f1 fix: make planner repair contract deterministic and convergent
0a82a03 fix: make generate repair preserve precise protocol errors
4d0dd6d refactor(plan): retire heuristic skill planning contracts
9ae9c7b docs(audit): record planner contract and snapshot findings
```

The working tree is dirty before this specification is added. Pre-existing modifications include skill documentation and multiple tests; pre-existing untracked files include `HANDOFF.md`, architecture audits, refactor documents, scripts, skills, and tests. They belong to the existing working tree and are not part of this specification change.

The decisive production files had no working-tree diff at audit time:

```text
bio_agents.py
agent_core/node_context.py
agent_core/plan_contract.py
agent_core/plan_tracker.py
agent_core/recovery.py
agent_core/context_lineage.py
agent_core/state.py
execution/profiling.py
prompts/manager.py
supervisor.py
```

Therefore no Current-HEAD code drift invalidates the findings in `docs/audits/architecture/planner_context_reentry_prompt_regression_audit_20260820.md`. This specification uses the repository's existing implementation-spec convention, `docs/refactor/`, rather than creating a parallel `docs/specs/` convention.

Line numbers are diagnostic anchors only. Function/class names and typed contracts are the implementation anchors because line positions will move during the repair.

## 3. Existing Failure Architecture

### 3.1 Current Planner attempt

The nested `planner()` node in `bio_agents.py` currently:

1. verifies the accepted Consultant FINALIZE contract;
2. resolves Canonical TaskSpec, selected skills, runtime audits, handoff, and other semantic context;
3. starts or reuses a `PlanningEpoch` keyed by TaskSpec/base-plan/planner-feedback identity;
4. increments `planner_calls_total`;
5. builds `NodeContext` with `node_type="planner"`;
6. renders `PromptManager.get_planner_v2_prompt()`;
7. sends the entire Planner policy and dynamic payload in one `HumanMessage`;
8. parses a typed Planner outcome;
9. runs deterministic candidate admission, allocation intent/promotion, plan normalization, output-contract validation, and skill checks;
10. enters one of two correction paths when validation fails.

### 3.2 Structure correction path

For malformed JSON/envelope/schema, the current node makes an immediate second provider call using `node_type="planner_repair"`, stage `planner_structure_repair`, and an instruction to return a corrected complete document. This path already demonstrates that structure correction does not require PlanPatch, but it creates a separate call path and does not establish durable cross-invocation failure continuity.

### 3.3 Semantic PlanPatch path

For semantic/contract violations, the current node:

1. builds a request with `build_plan_repair_request()`;
2. derives `allowed_step_ids` only from explicit affected-step metadata or a `steps[N]` field path;
3. renders `get_plan_repair_v2_prompt()`;
4. may load/save a `planner_patch:*` value through `RecoveryStateStore`;
5. saves the raw provider result before parse/authorization/apply/revalidation has succeeded;
6. parses and binds a `replace_steps` patch;
7. invokes `apply_plan_patch()`;
8. revalidates the patched full plan;
9. writes `plan_patches.jsonl` audit rows;
10. uses no-op patch counters/signatures for additional routing behavior.

`apply_plan_patch()` correctly protects Controller-owned fields for its own delta protocol, but its fundamental scope is limited: it cannot add/remove arbitrary steps and it cannot create a producer when the validator cannot identify an existing affected step.

### 3.4 Failure persistence and re-entry gap

If validation remains unresolved, the Planner preserves the previous plan and writes `task_state.planner_candidate_rejection`. That record includes a fingerprint, issue codes, candidate semantic hash, convergence data, and terminal diagnostic. The next initial Planner attempt, however, only reads pending `planner_feedback`; it does not project the durable rejection into `current_focus`.

Consequences:

- the next request can be semantically identical to the original initial request;
- a request hash can repeat because the actual correction objective is absent;
- the model can copy the same example/candidate again;
- the graph has durable evidence that the candidate failed but the Provider does not see it;
- `Plan Review` can route invalid plan state back to Planner with `validation_issues` but without a unified durable failure capsule.

This is a state/context assembly bug, not prompt truncation.

### 3.5 Example-copying source

`agent_core.plan_contract.planner_candidate_wire_example()` returns a complete validator-compatible candidate containing:

```text
local_ref: intermediate_summary
title: Create the intermediate summary
```

`validated_planner_candidate_wire_example()` validates it against the real candidate reader, and `PromptManager.get_planner_v2_prompt()` serializes the entire object into every Provider request. It is an internal contract fixture exposed as an executable semantic example.

### 3.6 Artifact identity and coverage inconsistency

`candidate_artifact_ref_keys()` currently constructs:

- authoritative typed keys for TaskSpec handles; and
- candidate-local typed keys indexed by bare `local_ref`.

It does not reject a local ref/role that shadows a TaskSpec input/output identity. `validate_output_contracts()` can count raw output text as a required output producer before allocation. Allocation later rewrites the candidate-local identity to `plan-artifact-*`, after which the same required output is uncovered. The same semantic candidate therefore receives inconsistent pre/post allocation ownership decisions.

### 3.7 Lifecycle safety limitation

`carry_forward_plan_lifecycle()`:

- carries task/evidence/runtime partitions;
- preserves step status/evidence for a semantic no-op;
- resets approval for a semantic change.

It does not by itself protect completed/running steps during a semantic full replan. `stable_plan_revision_delta()` reports added/changed/removed IDs, and `PlanRevisionManager` supports revision policy, but neither is currently an explicit executed-prefix admission gate for a full Planner candidate.

### 3.8 Terminal diagnostic loss

`_finalize_blocked_state()` derives `blocked_status.json` primarily from top-level `failure`; when the precise Planner rejection lives under `task_state.planner_candidate_rejection`, it can fall back to generic `TASK_BLOCKED`. `RunProfiler._durable_terminal_projection()` likewise does not read a Planner-specific failure owner. This loses the primary validation code and the final correction outcome.

## 4. Target Architecture

### 4.1 Authority hierarchy

The implementation must enforce this single hierarchy:

1. **Canonical TaskSpec** — current task semantic authority.
2. **Approved Plan/PlanSnapshot** — downstream execution-plan authority.
3. **Runtime and execution state** — machine-evidence authority.
4. **raw user turns and accepted Consultant handoff** — provenance/evidence, not a second TaskSpec.
5. **`current_focus`** — the current node objective derived from durable state.

If Consultant handoff and Canonical TaskSpec conflict, Planner follows TaskSpec or returns the existing typed `TASKSPEC_RECONCILIATION_REQUIRED` outcome. Planner must not merge the conflict by guessing.

### 4.2 Target state machine

```text
Consultant
  -> Canonical TaskSpec
  -> Planner attempt N (one provider call)
  -> parse complete PLAN_CANDIDATE
  -> deterministic structure/identity/DAG/dataflow validation
       PASS
         -> prepare allocation intent
         -> promote allocation
         -> revalidate typed ownership
         -> full-replan transition gate
         -> persist pending PlanSnapshot
         -> Plan Review / approval
       FAIL
         -> record/update task_state.planning_failure
         -> route Planner
         -> NodeContext.current_focus.planning
         -> Planner attempt N+1 returns a complete PLAN_CANDIDATE
```

Plan Review machine failures owned by Planner use the same `record_planning_failure()` transition. TaskSpec-owned failures route Consultant; external-input/user ambiguity keeps its existing typed route; internal Controller failures terminate as internal failures. The capsule is not used to relabel non-Planner owners.

Likewise, a PLAN-owned runtime Recovery decision is normalized into `planning_failure` before the Planner request is built. This replaces the current local-violation/PlanPatch shortcut: the next attempt sees the approved/base plan, typed Recovery evidence, and `return_complete_plan=true` in `current_focus`.

### 4.3 One-attempt invariant

One Planner graph invocation may dispatch at most one candidate-generation provider call. A returned candidate is parsed and validated once by the common admission pipeline. A failed attempt records a durable failure and returns a graph route. No immediate nested structure repair and no semantic patch call are allowed.

Shared provider retry/timeout middleware may retry transport according to its existing contract; those retries do not constitute a second candidate protocol.

### 4.4 Full-replan transition gate

Add a deterministic function in `agent_core/plan_tracker.py`, conceptually:

```python
validate_full_replan_transition(previous, candidate) -> list[diagnostic]
carry_forward_full_replan_lifecycle(previous, candidate) -> PlanState
```

It must operate on typed steps/status/evidence, not natural-language similarity.

Rules:

- Before execution starts, any valid full candidate may replace the previous pending/unapproved candidate.
- A `DONE`, `RUNNING`, or otherwise evidence-bearing executed step cannot be removed or semantically rewritten under the same stable `step_id`.
- Controller-owned status, evidence, provenance, attempts, materializations already realized, and runtime partitions are never accepted from the model.
- An executed step may be carried forward unchanged. A replacement may add dependencies only if they do not imply that already completed work depended on future work.
- The failed/current step and unstarted suffix may be changed, removed, split, or expanded, subject to DAG/dataflow and artifact identity validation.
- A candidate that needs to invalidate completed work must be rejected with a typed transition issue and routed through the existing Recovery/rollback authority; Planner cannot silently erase evidence.
- Every semantic change clears approval and creates a normal pending review snapshot.
- Exact semantic no-ops retain the existing approval/no-op lifecycle behavior but do not trigger a PlanPatch-specific Generate reroute.

Recommended deterministic issue codes:

```text
EXECUTED_STEP_REMOVED_BY_REPLAN
EXECUTED_STEP_SEMANTICS_CHANGED_BY_REPLAN
EXECUTED_EVIDENCE_IDENTITY_CHANGED
REPLAN_DEPENDENCY_REWRITES_COMPLETED_HISTORY
```

These codes compare typed fields and durable status/evidence only. They do not compare prose quality.

## 5. State Ownership

### 5.1 Durable `planning_failure` contract

The canonical owner is `PlanState.task_state.planning_failure` with this minimum schema:

```json
{
  "schema_version": 1,
  "failure_id": "planning-failure-...",
  "status": "unresolved|resolved|superseded|terminal",
  "owner": "planner",
  "planning_epoch_id": "planning-epoch-...",
  "attempt": 2,
  "task_spec_revision": 7,
  "task_semantic_hash": "controller-only",
  "candidate_id": "optional controller identity",
  "candidate_semantic_hash": "controller-only",
  "subject_kind": "rejected_candidate|pending_plan|approved_plan",
  "candidate_semantic_view": {
    "outcome_type": "PLAN_CANDIDATE",
    "proposed_artifacts": [],
    "steps": [],
    "task_output_materializations": []
  },
  "validation_source": "planner_structure|planner_candidate_admission|plan_contract|plan_review|recovery_plan_evidence|replan_transition",
  "validation_issues": [
    {
      "code": "REQUIRED_OUTPUT_UNCOVERED",
      "message": "...",
      "field_path": "$.steps",
      "affected_step_ids": [],
      "affected_artifact": {},
      "affected_output": {},
      "machine_evidence": {}
    }
  ],
  "failure_fingerprint": "controller-only",
  "repeat_count": 1,
  "previous_correction": {
    "attempt": 1,
    "status": "not_attempted|candidate_returned|parse_failed|rejected|accepted",
    "outcome_code": ""
  },
  "supersedes_failure_id": "",
  "superseded_by": "",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "resolved_at": "",
  "terminal_diagnostic": {}
}
```

Optional fields are omitted when absent. In particular, validators must not fabricate an affected step, artifact, or output merely to fill a schema slot.

`candidate_semantic_view` contains only model-owned semantic candidate fields. It never contains approval records, CAS tokens, allocation reservations, canonical paths, execution status, or evidence. Its size policy is defined in Section 7.

For a PLAN-owned runtime Recovery request there is no rejected candidate. Set `subject_kind="approved_plan"`, omit candidate-only identity fields, and store/project a bounded `base_plan_semantic_view` derived from the current approved PlanSpec. Do not relabel an approved plan as a rejected candidate.

For a transport/JSON/envelope failure there may be no semantic candidate to store. In that case omit `candidate_semantic_view` and store only a sanitized bounded `response_excerpt`, the exact structure diagnostic, and a Controller-only response identity. The excerpt is diagnostic context, not an alternate candidate parser: the Controller must not extract a JSON fragment or infer missing fields from it.

### 5.2 Mutation API

All new writes go through small PlanState helpers in `agent_core/plan_tracker.py`:

```text
current_planning_failure(state)
record_planning_failure(state, ...)
resolve_planning_failure(state, accepted_candidate_identity, ...)
supersede_planning_failure(state, reason, superseding_task_revision, ...)
terminalize_planning_failure(state, primary_code, secondary_cause, ...)
```

The helpers:

- normalize issues;
- compute Controller-only identity/fingerprint;
- enforce legal status transitions;
- update timestamps;
- preserve the original `created_at` for a repeated same-fingerprint failure;
- never write `planner_candidate_rejection`;
- return the new capsule for graph projection.

`save_plan_state()` remains the persistence boundary. No new database or event store is introduced.

A PLAN-owned Recovery decision that requests replanning must enter the same API before Planner dispatch. Normalize its typed violation/evidence into `validation_source="recovery_plan_evidence"`; record an unresolved capsule against the current approved/base plan; then consume/clear the Recovery routing projection according to the existing decision protocol. The Recovery decision authorizes and evidences re-entry, while `planning_failure` becomes the sole current Planner objective. Do not keep both as independently pending retry facts.

### 5.3 Other state fields

| Field | Decision | Target meaning |
| --- | --- | --- |
| `planner_feedback` | KEEP | pending Consultant/human typed plan instruction only; changes its revision and may start a new PlanningEpoch |
| `planner_candidate_rejection` | MIGRATE, then REMOVE from writes | legacy input to `planning_failure`; never a parallel source |
| `validation_issues` | KEEP with constrained semantics | derived validation result attached to candidate/current plan; not retry history or failure lifecycle owner |
| Planner `planner_patch:*` cache | REMOVE | no Planner candidate/repair replay cache |
| `allowed_step_ids` | REMOVE from Planner protocol | replaced by full candidate plus deterministic lifecycle transition gate |
| Planner `repair_scope` | REMOVE | correction objective comes from `planning_failure` projection |
| `candidate_fingerprint` | KEEP outside Planner | still used by Generate/pre-action/general Recovery cache; Planner uses Controller-only candidate semantic identity in the capsule |
| `PlanningEpoch` | KEEP, simplify | base TaskSpec/base plan identity and one Provider-call budget |
| `PlanningEpoch.candidate_repairs` | REMOVE | duplicates call/retry state |
| `max_candidate_repairs_per_epoch` | REMOVE | `max_planner_calls_per_epoch` is the only Planner-call limit |
| plan `validation_convergence_fingerprint` | KEEP as current-plan validation telemetry | does not own Planner retry continuity; current failure fingerprint is in the capsule |
| `repair_convergence` | CHANGE_SEMANTICS | plan validation result only; no PlanPatch lifecycle semantics |
| pending PlanSnapshot | KEEP | immutable review candidate after all deterministic admission passes |
| approved PlanSnapshot | KEEP | execution authority; never overwritten by a rejected candidate |
| `plan_patch_count` | MIGRATE | legacy Recovery route count maps to `plan_replan_count`; no semantic patch-apply count |
| `no_op_plan_patch_by_signature` | REMOVE from new writes | no PlanPatch-specific reroute/guard |
| top-level `failure` | KEEP | general execution/framework failure only; terminal Planner projection must reference the capsule rather than duplicate ownership |

## 6. Planner Failure Lifecycle

### 6.1 Legal transitions

```text
none
  -- candidate fails --> unresolved

unresolved
  -- same fingerprint fails again --> unresolved (repeat_count += 1)
  -- different Planner failure --> superseded, then new unresolved capsule
  -- candidate passes and pending snapshot commits --> resolved
  -- TaskSpec semantic revision changes --> superseded
  -- user/Consultant plan feedback changes objective --> superseded, new PlanningEpoch
  -- convergence budget exhausted --> terminal

resolved/superseded/terminal
  -- never reopened --> immutable terminal status for that failure identity
  -- later failure --> a new current capsule with supersedes_failure_id
```

Only one capsule is current in `task_state`. The PlanState audit/save history supplies historical trace; the implementation does not maintain a second current-failure list.

### 6.2 Attempt and convergence semantics

- `PlanningEpoch.planner_calls_total` increments exactly once before each candidate provider dispatch.
- `max_planner_calls_per_epoch` remains the hard bound, defaulting to the current value of 4 unless benchmark evidence justifies a later change.
- `planning_failure.attempt` records the call ordinal that produced the failure.
- `repeat_count` increments only when task revision, candidate semantic identity, validation source, and normalized issue set reproduce the same failure fingerprint.
- A different candidate that produces a different issue set supersedes the old failure and records a new capsule; it is progress even if still invalid.
- Terminalization occurs when the PlanningEpoch call budget is exhausted. An optional early non-progress terminal may occur only after two consecutive corrected full candidates reproduce the exact same failure fingerprint; it is not based on request text/hash alone.
- Request hashes stay in context lineage for observability. They never replace the capsule and never block a request that contains a changed correction objective.

### 6.3 Success transaction

A failure is marked resolved only in the same Controller transaction that:

1. parses the complete candidate;
2. passes deterministic admission;
3. promotes allocation successfully;
4. proves pre/post typed-output ownership equivalence;
5. passes the full-replan transition gate;
6. writes the pending PlanSnapshot/current pending plan.

Do not resolve on raw provider response, parse success, or pre-allocation validation alone.

Ordering is normative: first construct the admitted candidate, then run lifecycle carry-forward (which copies the prior `task_state`), then call `resolve_planning_failure()` on that resulting state, and finally persist the pending snapshot/state atomically. Resolving the source state before carry-forward is forbidden because the copied prior `task_state` could restore an unresolved capsule.

### 6.4 TaskSpec revision supersession

Before building Planner NodeContext, compare `planning_failure.task_spec_revision` and task semantic identity with current Canonical TaskSpec. If changed:

- call `supersede_planning_failure(..., reason="task_spec_revision_changed")`;
- do not project its violations as the current repair objective;
- open a new PlanningEpoch using the new TaskSpec identity;
- retain only a semantic provenance note that a prior objective was superseded.

This prevents a user correction from being forced through an obsolete Planner failure.

### 6.5 Plan Review integration

When `plan_review()` finds blocking issues:

- owner subset `{"planner"}`: persist/update `planning_failure` with `validation_source="plan_review"`, then route Planner;
- TaskSpec/external-input owner: use existing Consultant/user route and supersede any obsolete Planner capsule if task semantics change;
- internal owner: persist the existing internal failure contract and terminate;
- a Planner non-progress terminal: terminalize the current capsule before finishing.

Returning graph-only `validation_issues` is insufficient and is prohibited for the Planner route.

## 7. NodeContext Projection

### 7.1 Reuse the existing split

No new prompt-history subsystem is added.

```text
semantic_history = stable task, evidence, capability, and approved-plan semantics
current_focus    = the objective for this Planner invocation
```

`build_node_context()` gains an explicit planning-failure input/projection. Production Planner invocations use `node_type="planner"` for both initial and corrective attempts. `node_type="planner_repair"` becomes a legacy trace/read label and is not used by the new production path.

### 7.2 Provider-visible retry projection

When the capsule is unresolved, `LLMPayload.current_focus` must contain a semantic-safe structure equivalent to:

```json
{
  "planning": {
    "mode": "correct_previous_plan",
    "attempt": 2,
    "previous_candidate": {
      "candidate_label": "attempt-1",
      "plan": {
        "proposed_artifacts": [],
        "steps": [],
        "task_output_materializations": []
      }
    },
    "violations": [
      {
        "code": "REQUIRED_OUTPUT_UNCOVERED",
        "message": "Required output has no typed producer.",
        "field_path": "$.steps",
        "affected_output": {"handle": "output.annotated_vcf"},
        "machine_evidence": {"producer_step_ids": []}
      }
    ],
    "previous_correction": {
      "status": "rejected",
      "outcome_code": "REQUIRED_OUTPUT_UNCOVERED"
    },
    "instruction": {
      "return_complete_plan": true
    }
  }
}
```

For structure failures without a parsed plan, `previous_candidate` contains `candidate_label` plus the sanitized `response_excerpt` and `transport_status`; it does not fabricate a `plan` object. The violations remain the authoritative correction objective.

For `subject_kind="approved_plan"`, use `base_plan` rather than `previous_candidate` in the same `planning` object. It contains the approved plan's semantic view and typed Recovery evidence; approval/control identity remains outside Provider payload.

The durable capsule may contain hashes and stable IDs, but `node_context.py` already prohibits hash/control metadata and sha256-like values in Provider payloads. Therefore:

- candidate hash, task semantic hash, failure fingerprint, CAS and revision binding stay in `ControllerBinding`/trace metadata;
- Provider sees `candidate_label`, semantic plan view, typed violations, and attempt ordinal;
- no field ending in `_hash` is added to `LLMPayload`;
- `failure_id` and storage refs are not required for model reasoning and are omitted.

### 7.3 Candidate size policy

The projection must never drop the TaskSpec output contract or current violations to save space.

- Normally include the complete model-owned prior candidate.
- If it exceeds the Planner component budget, retain all step IDs, action/purpose/tool, typed inputs/outputs, dependencies, proposed artifact declarations, output materializations, and the full affected steps.
- Natural-language descriptions/success criteria outside affected steps may be clipped deterministically and listed in `omitted_descriptive_fields`.
- Never truncate artifact handles, issue codes, field paths, affected identities, dependencies, or TaskSpec output rows.
- Prompt packing must report the per-component character/token estimate; it must not silently project a retry as an initial attempt.

### 7.4 Re-entry invariants

`build_node_context()` must fail closed before provider dispatch if:

- an unresolved current Planner failure exists but `current_focus.planning` is absent;
- `mode` is not `correct_previous_plan` for an unresolved failure;
- current TaskSpec revision differs from the failure and supersession has not run;
- Provider projection contains Controller-only identity/fingerprint fields.

This is the primary guarantee against blind identical retry. Context-lineage request hash remains secondary observability.

## 8. Artifact Identity Contract

### 8.1 Model-facing namespace

New Planner candidates use exactly three semantic handle namespaces:

```text
input.<slug>   Canonical TaskSpec input
output.<slug>  Canonical TaskSpec output
plan.<slug>    Planner-declared plan-local artifact
```

New `proposed_artifacts[].local_ref` values must be `plan.<slug>`, and step `inputs`/`outputs` reference that exact handle. The Controller may allocate `plan-artifact-*` internally after admission. Internal `artifact-*`, canonical IDs, paths, reservation IDs, and CAS identities remain hidden from Planner.

Structured Planner wire schema advances from v2 to v3 for this namespace rule. The v2 reader may be retained only for non-replayed historical test/state migration input; new Provider writes and new fixtures emit v3.

### 8.2 Canonical collision keys

The collision gate is added before candidate-local keys are inserted in `candidate_artifact_ref_keys()`.

Authoritative keys come only from Canonical TaskSpec:

| Binding | Collision keys |
| --- | --- |
| input | semantic handle, `artifact_id`, legacy `input_id` when present, role |
| output | semantic handle, `artifact_id`, `output_id`, role |

Candidate-local keys are:

- exact `plan.<slug>` handle;
- suffix after `plan.`;
- `semantic_view.role` when present.

Use the existing deterministic field-key normalization used by TaskSpec for role/ID comparison. Do not use fuzzy matching, substring matching, filename guessing, or a hard-coded list of common output names.

Current `candidate_artifact_ref_keys()` receives the lossy `authoritative_handles: Mapping[handle, stable_identity]`, which is insufficient when role, `output_id`, and `artifact_id` differ. The v3 admission call must also receive the existing `SemanticHandleMap` binding view (or an equivalent immutable projection) containing `direction`, stable identity, and metadata for role plus input/output IDs. `_semantic_artifact_rows()` must add those IDs to Controller-side binding metadata. Build the collision index once from Canonical TaskSpec/handle bindings; do not reconstruct it by parsing Provider text or splitting hashed stable identities. A new v3 admission that lacks this binding index fails closed as an internal caller error.

Required behavior:

- a `plan.*` artifact whose normalized suffix/role shadows an authoritative output key is rejected with `TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL`;
- a shadowed input is rejected with `TASK_INPUT_SHADOWED_BY_PLAN_LOCAL`;
- duplicate `plan.*` declarations retain `LOCAL_REF_DUPLICATE`;
- a local handle outside `plan.*` is rejected with `PLAN_LOCAL_HANDLE_NAMESPACE_INVALID` for v3 candidates.

### 8.3 Capability and materialization namespaces

Capability output descriptors are capability metadata, not Task output identity authority. At context assembly:

- reconcile any descriptor that claims a Task output to an existing `output.*` binding;
- project the canonical output handle in capability context;
- keep unmatched descriptors as capability-local descriptive facts that cannot satisfy Task output coverage;
- treat a capability/TaskSpec identity contradiction as Controller configuration/reconciliation failure, not as a new Planner identity.

`task_output_materializations[].output_ref` must resolve to a typed authoritative `output.*` key. The relative `path` materializes the same identity; it never creates a fourth namespace.

### 8.4 Typed producer invariant

`validate_output_contracts()` must consume typed artifact usage/ownership, not raw output string coincidence.

The common admission pipeline produces `artifact_usage` rows from `ArtifactRefKey`:

```text
authoritative Task output + relation=produce -> counts as Task output producer
candidate-local plan artifact + relation=produce -> never counts as Task output producer
```

The exact invariant is:

> Given one semantic Planner candidate and one Canonical TaskSpec, required-output producer ownership is identical before allocation, after allocation, after normalization/reload, and in Plan Review.

Implementation requirements:

1. pre-allocation candidate validation computes typed usage;
2. allocation preserves the typed identity relation in durable plan artifact bindings;
3. post-allocation normalization reconstructs the same typed usage from bindings, not display strings;
4. `validate_output_contracts()` receives or derives that typed usage;
5. Plan Review calls the same typed coverage function;
6. a mismatch is an internal invariant error (`OUTPUT_OWNERSHIP_PROJECTION_DRIFT`), not a Planner repair objective.

### 8.5 TaskSpec output projection

`_semantic_artifact_rows()` currently omits fields that the Planner prompt claims to use. Every output row must project, when present:

```text
handle
output_id
role
artifact_kind
format
required
path_policy
root_ref
required_basename
producer_owner
producer_step_id (only when Canonical TaskSpec declares one)
alternative_group
user_visible
relocatable
validation_contract_id (only if Planner behavior depends on it)
description
placeholder
```

`producer_owner` is the current canonical materialization/producer ownership field. Do not invent a parallel `materialization_owner` if the Canonical TaskSpec already says `producer_owner`. `task_output_materializations` rules derive from `artifact_kind`, `path_policy`, `root_ref`, and `producer_owner`. When `root_ref` names another TaskSpec output identity, the Provider projection resolves it to that row's canonical `output.*` handle rather than exposing a fourth bare-reference convention. `alternative_group` preserves typed one-of deliverable semantics without asking Planner to infer them from prose.

Canonical TaskSpec is the sole output authority. `consultant_handoff.required_outputs` may remain provenance but cannot fill missing Planner output fields or override a TaskSpec row.

## 9. Deterministic Validation Boundary

### 9.1 Controller-owned machine checks

Keep or strengthen only checks whose truth is machine-deterministic:

- JSON transport and outcome envelope;
- typed candidate schema and prohibited Controller-owned fields;
- known semantic handles and namespace/collision rules;
- declared plan-local artifact contract enums;
- unique step IDs, known dependencies, acyclic DAG;
- explicit typed consume/produce dataflow and availability;
- required output logical producer existence/uniqueness;
- output materialization existence, safe relative path, `root_ref`, and `path_policy`;
- pre/post allocation identity consistency;
- TaskSpec revision/binding freshness;
- full-replan lifecycle transition safety;
- snapshot/approval state and CAS freshness;
- selected skill/capability ID membership.

Current `_skill_plan_violations()` may remain because `SkillManager.validate_plan()` now ignores prose and checks selected skill IDs against the catalog. `plan_review_telemetry()` remains non-authoritative telemetry.

### 9.2 Prohibited new validation

Do not add a blocking rule for:

- words such as `demo`, `placeholder`, `summary`, or scientific tool names;
- whether prose “looks real” or a title has a desired keyword;
- scientific workflow quality inferred from string patterns;
- model intent inferred from filenames alone;
- arbitrary natural-language compliance with highlighted requirements.

Planner owns semantic workflow design and correction. The Controller owns typed identity and machine-verifiable closure. `validation_checklist` remains Planner-authored semantic self-check content; the Controller validates its shape, not its truth by keyword.

## 10. Planner Repair Retirement and Compatibility Decision

### 10.1 Production switch

The production Planner switch is atomic within Phase 3:

- every Planner attempt returns a complete candidate;
- structure and semantic failures both record `planning_failure`;
- no production call reaches `get_plan_repair_v2_prompt()`, `build_plan_repair_request()`, `parse_plan_patch()`, `stamp_plan_patch_control_binding()`, or `apply_plan_patch()`;
- no `planner_patch:*` cache read/write occurs;
- no `plan_patches.jsonl` row is written;
- no PlanPatch no-op reroute occurs.

Do not ship a state where full-replan is active while semantic PlanPatch remains a fallback. That would preserve both authorities and defeat the simplification.

### 10.2 Recovery route compatibility

Recovery's current `PLAN_PATCH_REQUEST` is broader than the LLM PlanPatch wire and is persisted in existing checkpoints. Migrate it without breaking resume:

```text
canonical new decision: PLAN_REPLAN_REQUEST
canonical new action:   REPLAN_PLAN
legacy read aliases:    PLAN_PATCH_REQUEST, PLAN_PATCH, PATCH_PLAN
canonical route:        planner
required owner:         PLAN
```

New RecoveryPolicy writes only the new canonical values. `recovery_decision_from_state()` maps legacy values during read and records `migration_source`; routing remains unchanged. `_plan_patch_requested()` becomes `_plan_replan_requested()` and accepts legacy spellings only in the compatibility reader/input normalization layer.

Keep `_plan_replan_requested()` as a pure routing predicate. At `planner()` entry, before NodeContext construction, one dedicated normalization step must atomically convert the PLAN-owned decision's typed reason, affected identities, and machine evidence into the current `planning_failure`, then mark the decision consumed under the existing Recovery protocol. It must not merely route to a fresh Planner context, and it must not copy `allowed_patch_scope` into a model mutation scope.

`allowed_patch_scope` becomes legacy read-only metadata. It must not be converted to `allowed_step_ids`; full-replan transition safety is derived from durable lifecycle state.

`plan_patch_count` migrates to `plan_replan_count`. This counter bounds runtime Recovery authorizations to re-enter planning; `PlanningEpoch.planner_calls_total` separately bounds Provider calls. These are different facts and must not increment each other implicitly.

### 10.3 Cache decision

Delete/disable only the Planner `planner_patch:*` cache path. Keep `RecoveryStateStore.save_llm_result()/load_llm_result()` because Generate candidate repair, recovery judge, and diagnostic middleware use it.

The new full-replan path starts with **no Planner result replay cache**. Context lineage records requests/responses for audit only. A future accepted-candidate cache would require a separate reviewed design and may replay only a fully admitted/committed candidate; it is outside this repair.

Historical `plan_patches.jsonl` and cached files remain read-only evidence. Resume must never execute or replay them into the new Planner path.

## 11. Prompt Contract Changes

### 11.1 Stable System contract

Replace `get_planner_v2_prompt()` with two explicit builders:

```text
PromptManager.get_planner_v2_system_prompt()
PromptManager.get_planner_v2_user_prompt(llm_payload, capability_context, user_preferences)
```

The stable `SystemMessage` contains only:

- Planner responsibility and authority hierarchy;
- full-plan initial/correction protocol;
- `input.* / output.* / plan.*` namespace rules;
- typed field inventory and allowed enums;
- output materialization rules;
- Controller-owned/prohibited identity behavior;
- `TASKSPEC_RECONCILIATION_REQUIRED` alternative outcome contract;
- requirement to return exactly one JSON object.

The System schema representation must use field/type placeholders that cannot pass candidate admission as a real task plan, for example:

```text
"step_id": "<task-specific-stable-step-id>"
"inputs": ["<existing input.*|output.*|plan.* handle>"]
"title": "<task-specific-step-title>"
```

It must contain no concrete executable plan, scientific task, intermediate artifact, or validator-compatible semantic values.

### 11.2 Dynamic Human payload

The `HumanMessage` contains only per-invocation facts:

- NodeContext `semantic_history`;
- NodeContext `current_focus`;
- selected capability/operation cards;
- compact resolved runtime projection;
- user preferences relevant to planning.

Do not repeat the stable policy in Human content. Do not inject raw runtime audit history, a second Consultant output contract, or prior untyped error prose.

### 11.3 Internal fixture retirement from Provider context

`planner_candidate_wire_example()` and `validated_planner_candidate_wire_example()` may remain as internal test fixtures during migration because they synchronize reader/fixture behavior. Required changes:

- remove the fixture import/use from `prompts/manager.py`;
- remove the per-runtime Planner self-check in `bio_agents.py`;
- make docstrings explicitly say `internal test fixture; never provider-visible`;
- update the internal fixture to wire v3/`plan.*` if it remains;
- tests assert that Provider messages do not contain `Create the intermediate summary`, `intermediate_summary`, or any complete validator-compatible fixture.

If no test uses the helper after migration, delete it rather than preserving an unused runtime abstraction.

### 11.4 Invocation and temperature

The Planner caller sends:

```python
[
    SystemMessage(content=stable_policy),
    HumanMessage(content=dynamic_payload),
]
```

`_ainvoke_node_llm()` and `_ainvoke_llm()` already accept a per-call `temperature` and bind it when supported. Set Planner candidate calls to `temperature=0.1` (allowed range 0.0–0.2). Record it in trace telemetry.

Temperature is a stability tuning parameter only. It follows state continuity, identity correctness, deterministic admission, and full-replan semantics; it is not the root-cause fix.

## 12. Runtime Projection

### 12.1 Preserve durable evidence

Do not delete or rewrite `state.runtime_audit`, runtime registry evidence, healthcheck detail, install history, or Recovery disclosure. Those remain durable machine evidence for audit and recovery.

### 12.2 Add a Planner-specific whitelist projection

The current `_format_runtime_audit()` renders healthcheck commands, details, candidates, discovery hints, paths, and repeated metadata. Keep it for consumers that need full audit text, but do not feed it directly to Planner.

Add a compact Planner projection, conceptually `_planner_runtime_projection()`, containing only:

```text
selected skill/capability handle
runtime_ready
selected environment
resolved runner
resolved executable/jar paths required by the selected capability
install_required
blocking machine reason/decision
machine-critical runtime constraints
```

Exclude:

- repeated healthcheck commands and output;
- install/discovery history;
- registry timestamps;
- old task/action IDs;
- legacy renderer commands;
- duplicate `RESOLVED_*` strings;
- alternative environments after a canonical runtime has been selected;
- evidence-file inventories.

Each semantic fact appears once. NodeContext receives the structured projection under `runtime`; the prompt builder serializes it once under the dynamic Human payload. The full audit remains accessible to Recovery/on-demand diagnostics.

### 12.3 Runtime authority

The projection is derived from current typed runtime audit/registry state. Do not parse Consultant prose as a new runtime authority. Existing legacy sessions without typed runtime rows may omit Planner runtime facts or use a narrowly defined compatibility projector, but they must not promote an old handoff string over current runtime evidence.

Prompt budget telemetry must report at least:

```text
planner_system_chars/tokens
semantic_history_chars/tokens
current_focus_chars/tokens
capability_chars/tokens
runtime_projection_chars/tokens
user_preferences_chars/tokens
total and remaining headroom
```

## 13. Terminal Diagnostics

### 13.1 Durable terminal capsule

When Planner convergence terminates, `terminalize_planning_failure()` writes:

```json
{
  "terminal_class": "planner_failure",
  "primary_code": "PLANNER_VALIDATION_NO_PROGRESS",
  "primary_message": "Planner full replan did not converge within the bounded PlanningEpoch.",
  "last_validation_issue": {
    "code": "REQUIRED_OUTPUT_UNCOVERED",
    "message": "...",
    "field_path": "$.steps"
  },
  "secondary_cause": "FULL_REPLAN_REPEATED_FAILURE|PLANNING_EPOCH_EXHAUSTED|PLANNER_STRUCTURE_INVALID",
  "candidate_semantic_hash": "controller-only",
  "planning_attempt": 4,
  "failure_id": "planning-failure-...",
  "execution_started": false
}
```

There is no `PLAN_PATCH_REJECTED` secondary code after migration. Use the actual new correction outcome.

### 13.2 Terminal projections

`_finalize_blocked_state()` and `RunProfiler._durable_terminal_projection()` must read a terminal `task_state.planning_failure` before falling back to generic task status. They derive:

- `blocked_status.json.error_code` from `primary_code`;
- `blocked_status.json.error_owner = planner`;
- `run_metrics.json.final_result.terminal_diagnostic` from the terminal capsule;
- `run_metrics.json.failure` with primary code, owner, stage, and planning attempt;
- user-visible blocked text that includes primary and last-validation codes.

For non-Planner terminal failures, the current top-level `failure`/Consultant FINALIZE precedence remains unchanged. The profiler must only project an unresolved/terminal current planning capsule, never a resolved or superseded one.

Minimum incident result after this repair:

```text
terminal_class=planner_failure
primary_code=PLANNER_VALIDATION_NO_PROGRESS
last_validation_issue.code=REQUIRED_OUTPUT_UNCOVERED
secondary_cause=FULL_REPLAN_REPEATED_FAILURE (or PLANNING_EPOCH_EXHAUSTED)
planning_attempt=<exact attempt>
```

## 14. Exact File and Function Change Map

| File | Function / class | Current responsibility | Required change | Why |
| --- | --- | --- | --- | --- |
| `bio_agents.py` | nested `planner()` | initial candidate, Recovery/local repair shortcut, nested structure repair, semantic PlanPatch, cache, validation, commit | materialize any PLAN-owned Recovery request into `planning_failure` before context; make one provider call per invocation; load/supersede/project the capsule; admit complete candidate; run typed pre/post checks and full-replan transition gate; record/resolve/terminalize failure; remove semantic patch/cache/audit path | central production simplification and failure continuity |
| `bio_agents.py` | `reserve_planner_candidate_repair()` nested helper | separate candidate-repair and call budgets | remove; use one Planner-call reservation helper that increments only `planner_calls_total` | remove duplicate repair state |
| `bio_agents.py` | `_plan_patch_requested()` and graph recovery routing | authorize PLAN-owned patch route | rename to pure `_plan_replan_requested()`; recognize the canonical replan decision after compatibility normalization | preserve Recovery routing boundary without mutation inside a router |
| `bio_agents.py` | nested `plan_review()` | revalidates pending plan and routes failures | persist Planner-owned issues through `record_planning_failure()` before routing; resolve only after accepted commit | Plan Review must share failure continuity |
| `bio_agents.py` | `_format_runtime_audit()` call inside `planner()` | injects full runtime audit | add/use `_planner_runtime_projection()` for Planner; leave full formatter for other consumers | reduce duplicated dynamic context without deleting evidence |
| `bio_agents.py` | `_finalize_blocked_state()` | writes generic blocked artifacts | project terminal planning capsule when current | preserve precise diagnosis |
| `bio_agents.py` | Planner `_ainvoke_node_llm()` call site | dispatches one Human message at global/default temperature | dispatch System + Human and `temperature=0.1`; add accurate component telemetry | real role split and stable typed generation |
| `agent_core/state.py` | `AgentState` | graph projection type | add `planning_failure: dict` as a non-owning graph projection if returned by nodes | make graph updates typed without creating a second durable owner |
| `agent_core/node_context.py` | `_semantic_artifact_rows()` | maps TaskSpec artifacts to model handles | add canonical output fields listed in Section 8.5; add input/output IDs to Controller-side handle-binding metadata; preserve semantic handles and hide paths | make prompt claims match real output contract and supply lossless collision keys |
| `agent_core/node_context.py` | `build_node_context()` / current-focus construction | builds semantic history and repair focus | accept/project unresolved planning failure for `node_type="planner"`; enforce control-metadata stripping and fail-closed re-entry invariant | failure must reach next Provider request |
| `agent_core/plan_contract.py` | Planner wire constants/schema | defines structured candidate v2 | define v3 `plan.*` local-handle rule and compatibility reader policy | one model-facing artifact namespace |
| `agent_core/plan_contract.py` | `candidate_artifact_ref_keys()` | resolves authoritative and local refs from a lossy handle-to-ID map | consume the immutable semantic-handle binding view, reject Task input/output shadowing before key insertion, and return typed usage | close identity shadow before allocation without string inference |
| `agent_core/plan_contract.py` | `validate_planner_candidate_deterministically()` | shared candidate admission | surface collision/namespace diagnostics and retain typed usage for output coverage | one deterministic admission path |
| `agent_core/plan_contract.py` | `prepare_allocation_intent()` / promotion helpers | allocate plan-local artifacts | accept v3 `plan.*`, preserve typed relation, and assert allocation does not change semantic ownership | pre/post invariant |
| `agent_core/plan_contract.py` | `planner_candidate_wire_example()` / `validated_planner_candidate_wire_example()` | internal fixture currently exposed to prompt/runtime self-check | keep only as updated internal fixture or delete if unused; prohibit prompt/runtime use | remove copyable semantic example |
| `agent_core/plan_contract.py` | `start_planning_epoch()` / `planning_epoch_matches()` | epoch identity and dual counters | bump epoch schema, remove `candidate_repairs`, keep `planner_calls_total` and base identities | one attempt budget |
| `agent_core/plan_tracker.py` | PlanState defaults and `migrate_plan_state()` | schema/default migration | bump PlanState migration to v11; add `planning_failure`; migrate `planner_candidate_rejection`; migrate legacy budgets/decision names without dual writes | resume existing sessions safely |
| `agent_core/plan_tracker.py` | new planning-failure helpers | none | implement the sole mutation/lifecycle API in Section 5 | unique durable owner |
| `agent_core/plan_tracker.py` | `validate_output_contracts()` | required-output ownership, with raw-string compatibility | consume typed artifact usage/bindings for v3; confine raw-string behavior to explicitly legacy plan schemas | identical pre/post ownership |
| `agent_core/plan_tracker.py` | `carry_forward_plan_lifecycle()` / `stable_plan_revision_delta()` | no-op carry-forward and revision diff | add transition validator and explicit executed-state/evidence carry-forward before semantic full-plan commit | safely retire delta patch |
| `agent_core/plan_tracker.py` | PlanPatch constants/functions at current lines ~390 and ~4316–4585 | semantic delta protocol | delete after production switch and test migration | actual complexity reduction |
| `agent_core/recovery.py` | `RecoveryDecisionKind`, `RecoveryAction`, mapping/routing tables | PLAN patch request vocabulary | add canonical REPLAN vocabulary; normalize legacy names on read; new policies emit only canonical values | compatible resume without semantic patch authority |
| `agent_core/recovery.py` | `RecoveryDecision.allowed_patch_scope` | legacy patch-scope metadata | deprecate/read only; do not project to Planner or derive allowed steps | lifecycle state owns safe replan boundary |
| `agent_core/recovery.py` | Recovery budgets/policies using `plan_patch_count` | bounds PLAN repair routes | migrate to `plan_replan_count`; preserve limits and ownership checks | accurate semantics, unchanged safety boundary |
| `prompts/manager.py` | `get_planner_v2_prompt()` | mixed static/dynamic single prompt with full example | replace with System/User builders; placeholder schema only | role correctness and example removal |
| `prompts/manager.py` | `get_plan_repair_v2_prompt()` | semantic patch protocol | delete after production caller removal | retire second Planner protocol |
| `execution/profiling.py` | `RunProfiler._durable_terminal_projection()` | maps PlanState failures to final metrics | recognize terminal `task_state.planning_failure` and project primary/secondary codes | prevent generic `TASK_BLOCKED` loss |
| `execution/profiling.py` | `record_final_result()` / snapshot | stores terminal result | retain full bounded planning diagnostic fields in final result/failure projection | observable acceptance evidence |

### Explicit no-change decisions

| File/component | Decision | Reason |
| --- | --- | --- |
| `agent_core/context_lineage.py` | no production change required | request/response hashes remain observability; they must not become the correction source or hard retry authority |
| `supervisor.py` | no change required | Planner candidate admission/failure continuity is owned before Supervisor; terminal profiler already has the correct projection boundary |
| `agent_core/task_spec.py` | no canonical schema change required | output bindings already carry `output_id`, `artifact_kind`, `path_policy`, `root_ref`, and `producer_owner`; only Planner projection is incomplete |
| `agent_core/skill_manager.py` | no change required | current `validate_plan()` is catalog-ID based and explicitly non-lexical; prose scan is telemetry only |
| `agent_core/knowledge_conflicts.py` / `PlanRevisionManager` | no change preferred | run the new deterministic transition gate before the existing revision manager; do not expand legacy semantic string logic |
| general `RecoveryStateStore` cache methods | keep | Generate, Recovery judge, and middleware are real consumers; only Planner patch stages are retired |
| Generate/Execute managed artifact binding | no redesign | consume the same allocated PlanState after stronger Planner admission |

## 15. Deletion and Deprecation Map

### 15.1 DELETE after Phase 3 production switch

```text
PromptManager.get_plan_repair_v2_prompt
build_plan_repair_request
parse_plan_patch
stamp_plan_patch_control_binding
apply_plan_patch
PLAN_OPERATION_REPAIR
PLAN_OPERATION_LOCAL_REPLAN
PLAN_PATCH_OPERATION
the inline PlanPatch `replace_steps` wire schema (Current HEAD has no separate PlanPatch class)
Planner imports/call sites for all of the above
planner_patch:* load/save call sites
Planner write path for plan_patches.jsonl
reserve_planner_candidate_repair
PlanPatch-specific no-op Generate reroute
PlanPatch-specific review/audit telemetry fields
```

Delete or rewrite tests whose only asserted behavior is `replace_steps` patch parsing/application. Do not delete broader Plan lifecycle/identity tests merely because they currently use `apply_plan_patch()` as a fixture shortcut; rewrite them through complete candidate admission/transition.

### 15.2 DEPRECATE with compatibility reader

```text
planner_candidate_rejection
RecoveryDecisionKind.PLAN_PATCH_REQUEST
RecoveryAction.PATCH_PLAN
required_next_action=PLAN_PATCH|PATCH_PLAN
allowed_patch_scope
plan_patch_count
PlanningEpoch.candidate_repairs
max_candidate_repairs_per_epoch
bare candidate-local refs from structured_plan_v2
```

New writers must stop immediately in the phase that installs the reader. Compatibility normalization is read-only and records its migration source. Remove aliases only after supported pre-v11 checkpoints have passed the documented compatibility window.

### 15.3 KEEP TEMPORARILY / historical read-only

```text
old plan_patches.jsonl files as incident evidence
old Recovery cache files as incident evidence, never Planner replay
validated_planner_candidate_wire_example only as an internal test fixture if tests still need it
structured_plan_v2 reader only for explicit legacy input
pending and approved historical snapshots
```

### 15.4 KEEP

```text
PlanningEpoch base identity and planner_calls_total
PlanSnapshot / approval records
stable_plan_revision_delta
carry_forward task/evidence/runtime partitions
typed candidate admission and allocation intent
general RecoveryStateStore cache API
ContextLineage request/response telemetry
Generate -> Execute -> Recovery contracts
```

## 16. Migration Plan

### Phase 1 — Unique failure owner and context continuity

Implementation:

- bump PlanState migration v10 -> v11 and PlanningEpoch schema v1 -> v2;
- add planning-failure helpers and `AgentState` projection;
- migrate `planner_candidate_rejection` on read and stop new writes;
- make both Planner validation rejection and Plan Review Planner-owned failure persist the capsule;
- project unresolved failure through `current_focus` while the existing semantic patch mechanism is still the correction mechanism for this phase only;
- remove `candidate_repairs`/`max_candidate_repairs_per_epoch` and reserve one Planner call per dispatch.

No double authority: once the migrator is enabled, only `planning_failure` is written. The old field is an input alias, never mirrored output.

Acceptance:

- failure-continuity, no-blind-retry, Plan Review re-entry, and TaskSpec-supersession tests pass;
- unresolved failure cannot dispatch without `current_focus.planning`;
- old checkpoint with only `planner_candidate_rejection` resumes with one migrated capsule.

### Phase 2 — Typed identity and output-coverage consistency

Implementation:

- add a generic input/output shadow gate that accepts the currently active v2 bare local-ref syntax while computing candidate-local typed identity;
- retain typed usage through allocation and reload;
- change output coverage to typed ownership;
- complete TaskSpec output projection.

No double authority: even while v2 remains the active Provider wire in this phase, candidate-local refs never count as Task output producers through raw string coincidence. The namespace is still v2-only until the prompt/schema switch; Phase 2 does not accept two new-writer conventions.

Acceptance:

- shadow and typed producer tests pass;
- pre/post allocation ownership is identical;
- current Generate managed-binding tests remain green.

### Phase 3 — Full-replan transition and atomic production switch

Implementation:

- add full-replan transition/evidence carry-forward gate;
- merge structure correction into durable next-attempt flow;
- switch every Planner correction to complete `PLAN_CANDIDATE`;
- remove production calls to semantic PlanPatch and `planner_patch:*` cache in the same change;
- update Plan Review and approval flow for complete replacement.

No double authority: there is no runtime flag that can fall back from full-replan to semantic PlanPatch. Rollback is a Git revert of the whole phase, not dual live protocols.

Acceptance:

- complete-replan and executed-prefix safety tests pass;
- one provider call per Planner graph invocation;
- semantic patch/cache functions have zero production callers;
- accepted semantic changes always require current review/approval.

### Phase 4 — Prompt role/schema cleanup and legacy vocabulary normalization

Implementation:

- switch the new-writer candidate contract and Planner prompt atomically to structured plan v3/`plan.*`; keep v2 only as an explicit compatibility reader;
- install stable System/dynamic Human builders;
- remove Provider-visible complete example and runtime self-check;
- set Planner temperature to 0.1;
- canonicalize Recovery `PLAN_REPLAN_REQUEST`/`REPLAN_PLAN` and retain legacy read aliases;
- delete obsolete PlanPatch functions/constants/tests and stop PlanPatch audit writes.

No double authority: the same change switches both the prompt and validator to v3; PromptManager exposes one new-writer candidate protocol, and Recovery emits one canonical replan spelling.

Acceptance:

- System prefix is stable across attempts;
- Human payload changes when current failure changes;
- no Provider message contains the internal example;
- legacy recovery checkpoint routes to Planner after normalization.

### Phase 5 — Runtime projection, telemetry, and terminal diagnosis

Implementation:

- add compact Planner runtime projection and component telemetry;
- preserve durable runtime audit for Recovery;
- project terminal planning capsule into blocked artifacts/run metrics;
- remove obsolete Planner repair telemetry names.

No double authority: compact runtime context is a projection of durable runtime evidence, not a second runtime store; metrics are projections of the capsule.

Acceptance:

- prompt inventory contains each runtime fact once and current failure facts once;
- exact primary/last-validation/secondary codes survive to terminal artifacts;
- Generate/Execute/Recovery focused regressions pass.

## 17. Regression Test Matrix

### Test 1 — Failure continuity

Setup: Planner returns a structurally valid candidate that fails `REQUIRED_OUTPUT_UNCOVERED`.

Assert:

- `task_state.planning_failure.status == unresolved`;
- normalized issue and candidate semantic view are persisted;
- next Planner Human payload contains `mode=correct_previous_plan`, prior candidate, exact issue, previous outcome, and `return_complete_plan=true`;
- no hash/control field leaks to Provider.

### Test 2 — No blind identical retry

Setup: same TaskSpec, unresolved failure, next Planner attempt.

Assert:

- the corrective request is not semantically equal to an initial request because `current_focus` is present;
- request hash may be observed but is not the mechanism enforcing this;
- dispatch fails closed if the test removes the failure projection.

### Test 3 — Plan-local shadow

Setup: TaskSpec contains `output.annotated_vcf`; candidate declares `plan.annotated_vcf` or the same semantic role.

Assert before allocation:

```text
TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL
```

Repeat generically for an input role/handle. No name-specific rule is permitted.

### Test 4 — Typed producer consistency

Setup: one real `output.report` producer and one plan-local artifact with a similar display role.

Assert:

- only authoritative typed output production satisfies TaskSpec;
- ownership set is identical before allocation, after promotion, after save/reload, and during Plan Review;
- drift raises `OUTPUT_OWNERSHIP_PROJECTION_DRIFT` as internal failure.

### Test 5 — Complete replan

Setup: initial candidate fails a deterministic plan contract; next response adds/removes/rewires steps and returns a complete candidate.

Assert:

- no semantic PlanPatch prompt/parser/apply/cache is called;
- corrected candidate passes common admission and becomes pending review;
- prior approved plan remains execution authority until approval;
- old failure is resolved only after pending snapshot commit.

### Test 5A — Executed-prefix safety

Setup: previous plan has a completed step with evidence and pending suffix.

Assert:

- unchanged executed step/evidence are carried forward;
- changing/removing it is rejected with a typed transition code;
- pending suffix can be replaced by a complete plan;
- approval is reset for semantic change.

### Test 6 — Plan Review re-entry

Setup: Plan Review discovers a blocking Planner-owned typed issue.

Assert:

- issue is persisted through the same capsule API;
- next Planner payload sees prior pending plan and Plan Review issue;
- graph-only `validation_issues` are not the sole retry source.

### Test 7 — TaskSpec revision supersedes failure

Setup: unresolved Planner failure followed by a user/Consultant TaskSpec semantic mutation.

Assert:

- old capsule becomes superseded;
- new Planner attempt does not treat old violations as its correction objective;
- a new PlanningEpoch is based on the new TaskSpec identity.

### Test 8 — Output projection

Assert Provider `semantic_history.task.outputs[]` includes, when present:

```text
handle, output_id, role, artifact_kind, format, required,
path_policy, root_ref, producer_owner, required_basename,
alternative_group, user_visible, relocatable
```

Assert the same facts are not patched in from `consultant_handoff.required_outputs`.

### Test 9 — Example copying prevention

Assert Planner System/Human messages:

- contain no `Create the intermediate summary`;
- contain no `intermediate_summary` fixture;
- contain no complete validator-compatible semantic candidate example;
- contain placeholder schema/field rules;
- retain internal fixture validation tests if the fixture remains.

### Test 10 — Terminal diagnostic

Setup: full-replan repeats the same deterministic failure or exhausts the epoch.

Assert `plan_state.json`, `blocked_status.json`, and `run_metrics.json` preserve:

```text
terminal_class
primary_code
primary_message
last_validation_issue
secondary_cause
candidate_semantic_hash (durable/metrics only)
planning_attempt
```

Assert user-visible result is not only `TASK_BLOCKED`.

### Test 11 — Generate/Execute/Recovery regression

Keep passing focused suites for:

- managed output binding and placeholder atomicity;
- plan-local pre-action convergence;
- Generate candidate fingerprints/general Recovery cache;
- action environment binding;
- execution provenance/status evidence;
- Recovery decision routing and precise owner;
- context-lineage observability;
- Plan approval/snapshot freshness.

Add an explicit PLAN-owned Recovery re-entry case: a canonical or migrated replan decision must create `validation_source="recovery_plan_evidence"`, be consumed once, and appear in the next Planner `current_focus`; it must not invoke semantic PlanPatch or redispatch a context-free initial request.

### Recommended test-file changes

| Test file | Change |
| --- | --- |
| new `tests/test_planner_full_replan_context_convergence.py` | Tests 1, 2, 5, 5A, 6, 7, and one-call invariant |
| `tests/test_node_context_semantic_continuity.py` | failure current-focus, output projection, and control-metadata exclusion |
| `tests/test_plan_contract_v121.py` or current candidate-contract suite | v3 namespace, generic shadow gate, typed usage |
| `tests/test_planner_corrective_repair_contract.py` | replace PlanPatch-only cases with full-replan/structure continuity cases; rename/delete file when no old contract remains |
| `tests/test_plan_contract_convergence_v2.py` | remove patch parser/apply assertions; retain/rewrite convergence, allocation, and approval assertions |
| `tests/test_plan_tracker.py` | v11 migration, failure lifecycle, transition safety, typed output coverage |
| `tests/test_prompt_manager.py` | System/Human split, placeholder schema, no executable example |
| `tests/test_consultant_first_graph_integration_v21.py` or current graph integration suite | Plan Review/re-entry and TaskSpec supersession |
| Recovery decision tests | canonical REPLAN writes and legacy PATCH read normalization |
| `tests/test_run_profiler.py` | terminal planning-failure projection |
| `tests/test_generate_only_output_contract_v2.py` | stop using `apply_plan_patch()` as fixture setup; validate full candidate/binding path |
| `tests/test_managed_artifact_runtime_reconciliation_v3.py` | shared artifact binding regression |
| `tests/test_plan_local_preaction_convergence_v2.py` | general Recovery cache/candidate fingerprint regression |
| `tests/test_preaction_failure_terminal_v2.py` | non-Planner terminal diagnosis regression |

Exact filenames may follow current suite organization at implementation time, but every assertion above is mandatory. Do not retain a test merely to preserve retired PlanPatch behavior.

## 18. Acceptance Criteria

### 18.1 Functional acceptance

- A failed Planner candidate always creates/updates exactly one durable `task_state.planning_failure`.
- Every corrective Planner provider request contains the unresolved failure objective in `current_focus`.
- Every Planner graph attempt dispatches at most one candidate provider call.
- Structure and semantic failures use the same complete-candidate re-entry protocol.
- No production semantic PlanPatch call/cache/audit path remains.
- A complete semantic replan cannot rewrite durable executed history.
- TaskSpec outputs cannot be shadowed by plan-local artifacts.
- Required output producer ownership is identical pre/post allocation and after reload/review.
- Planner output rows contain the canonical TaskSpec fields required by prompt rules.
- Plan Review Planner-owned failures use the same failure owner/lifecycle.
- TaskSpec semantic mutation supersedes obsolete Planner failures.
- accepted semantic replans remain pending until review/approval.

### 18.2 Prompt acceptance

- Provider receives one stable `SystemMessage` plus one dynamic `HumanMessage`.
- Provider never receives the internal complete semantic example.
- Provider artifact handles are limited to `input.*`, `output.*`, and `plan.*`.
- unresolved failure facts cannot be compressed/projection-dropped.
- runtime projection contains only canonical current facts, once each.
- Planner call telemetry reports accurate component sizes and `temperature=0.1`.

### 18.3 State and terminal acceptance

- v10/legacy rejection checkpoints migrate on read without dual writes.
- v2/bare-local candidate compatibility is explicit, bounded, and never emitted by new writers.
- legacy Recovery PATCH decisions resume through canonical REPLAN normalization.
- rejected candidates never replace pending/approved snapshots.
- resolved/superseded failures are not projected as current objectives.
- terminal metrics preserve primary code, last validation issue, secondary cause, and attempt.

### 18.4 Negative acceptance

- no new lexical/scientific semantic validator;
- no new semantic reviewer/repair agent/supervisor;
- no second task-output authority;
- no Planner replay cache;
- no full-replan + PlanPatch fallback coexistence;
- no refactor of Generate/Execute/Recovery beyond the named compatibility/shared-helper changes.

### 18.5 Verification commands for the implementation turn

Run focused tests with the project environment, then the relevant broader suite. A representative command set is:

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest -v \
  tests.test_planner_full_replan_context_convergence \
  tests.test_node_context_semantic_continuity \
  tests.test_prompt_manager \
  tests.test_plan_tracker \
  tests.test_run_profiler

/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest -v \
  tests.test_generate_only_output_contract_v2 \
  tests.test_managed_artifact_runtime_reconciliation_v3 \
  tests.test_plan_local_preaction_convergence_v2 \
  tests.test_preaction_failure_terminal_v2 \
  tests.test_context_lineage

git diff --check
```

Use the actual importable test module names present after implementation. This specification does not authorize a real workflow or snpEff run.

## 19. Rollback and Compatibility

### 19.1 Schema compatibility

- PlanState migration v11 is reader-first: accept v10, derive `planning_failure` from legacy rejection when needed, then write only v11.
- PlanningEpoch v1 values migrate by dropping `candidate_repairs`; preserve base identities and `planner_calls_total`.
- Existing pending/approved snapshots remain authoritative and are not fabricated or rewritten by migration.
- Existing allocated plan-local artifacts retain internal identities; the `plan.*` rule applies to new model candidates, not by rewriting historical durable IDs.
- Existing `plan_patches.jsonl`/cache artifacts remain untouched on disk.

Legacy rejection status mapping is deterministic:

```text
planner_candidate_rejection.status=rejected
  -> planning_failure.status=unresolved

planner_candidate_rejection.status=blocked_non_convergent
  -> planning_failure.status=terminal

missing/unknown legacy status
  -> terminal only when its typed terminal diagnostic proves no-progress/exhaustion;
     otherwise unresolved
```

Copy existing issue/candidate fingerprints only into Controller-side fields, mark `migration_source="planner_candidate_rejection"`, and omit the legacy field from the next v11 write.

### 19.2 Existing session resume

On resume:

1. migrate PlanState before route/context selection;
2. if old `planner_candidate_rejection` is unresolved, construct one capsule with `migration_source="planner_candidate_rejection"`;
3. if current TaskSpec identity no longer matches, immediately supersede it;
4. normalize legacy Recovery patch decision to replan;
5. never replay an old Planner patch cache result;
6. preserve the approved snapshot as execution authority until a new full candidate is approved.

### 19.3 Code rollback

Each phase must be one reviewable change set with its tests. Phase 3 is rolled back atomically because it switches the production correction protocol. Do not add a long-lived runtime feature flag that permits both protocols. State readers may remain forward-compatible during rollback, but old writers must ignore unknown `planning_failure` safely rather than erase it.

### 19.4 Data rollback

The migration is additive before deletions:

- do not delete old fields/files during read migration;
- stop writing them;
- preserve audit history;
- if code rollback occurs, v11 readers/writers must document whether the old version can ignore the new field without overwriting task state.

## 20. Non-Goals

This repair does not:

- redesign Consultant FINALIZE or TaskSpec semantic compilation;
- add a semantic correctness validator to the Controller;
- redesign selected-skill discovery or capability execution;
- change Generate command/action protocols;
- change Execute process/evidence ownership;
- replace general Recovery cache/diagnostics;
- rework managed artifact realization or placeholder rules;
- redesign Plan Review user interaction;
- create a new global history database/event bus;
- change ContextLineage request hashing into a gate;
- tune model temperature as a substitute for state repair;
- run a real workflow or modify historical session artifacts.

## 21. Git Implementation Sequence

The future implementation should use this dependency order:

1. **Commit A — planning failure state and NodeContext continuity**

   PlanState v11 migration, capsule helpers, graph projection, Planner/Plan Review persistence, re-entry tests.

2. **Commit B — typed artifact identity and output projection**

   v2-compatible collision gate, typed producer equivalence, TaskSpec output projection, shared binding regressions.

3. **Commit C — full-replan lifecycle gate and production switch**

   executed-prefix protection, one-attempt orchestration, structure merge, complete candidate commit, semantic patch/cache caller removal.

4. **Commit D — prompt and Recovery compatibility cleanup**

   atomic v3/`plan.*` prompt+validator switch, System/Human split, example removal, Planner temperature, canonical REPLAN vocabulary, legacy readers, obsolete function/test deletion.

5. **Commit E — runtime/terminal observability**

   compact runtime projection, component metrics, precise blocked/run-metrics projection, focused downstream regressions.

Before each commit:

- inspect `git status --short` and preserve unrelated dirty-tree changes;
- stage only files named by that phase;
- run the phase-focused tests and `git diff --check`;
- confirm there is exactly one current state owner and one active Planner correction protocol.

This document does not authorize creating those commits in the current turn.

## 22. Definition of Done

The implementation is complete only when all of the following are true:

- semantic `PlanPatch` is absent from the production Planner call graph;
- structure correction remains available through the same next-attempt full-plan protocol;
- `task_state.planning_failure` is the unique durable machine Planner failure owner;
- unresolved failure is always present in corrective `current_focus`;
- full replans preserve executed history and require approval for semantic change;
- TaskSpec output/plan-local identity cannot collide or change ownership across allocation;
- Canonical TaskSpec output projection is complete and authoritative;
- Provider prompt contains no copyable complete semantic example;
- System/Human roles and per-node temperature are correct;
- Planner runtime context is a compact projection, not duplicated evidence;
- terminal artifacts retain the exact Planner failure chain;
- legacy checkpoints resume via read-only compatibility without replaying rejected patches;
- all focused Planner and Generate/Execute/Recovery regressions pass;
- no unrelated production boundary is refactored.
