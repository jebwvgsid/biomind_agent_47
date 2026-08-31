# BioMind Plan Contract Specification 1.2.1 — Contract Closure

Status: Proposed 1.2.1 Contract Closure

Incident reference: `session_1786516456`

Normative target pipeline:

`LLM Request-Intent Decision -> Controller Admission -> Consultant -> canonical TaskSpec -> Planner Candidate -> Deterministic Plan Validation -> Plan-local Artifact Preparation -> Atomic PlanSnapshot Commit -> Human Plan Review/Approval -> Generate/Execute`

Primary implementation scope: `agent_core/node_context.py`,
`agent_core/plan_tracker.py`, `agent_core/managed_artifacts.py`,
`agent_core/state.py`, `prompts/manager.py`, `bio_agents.py`, state
storage, and focused unit, integration, and incident-replay tests.

Independent TaskSpec coherence scope: `agent_core/task_spec.py`,
`agent_core/consultant_finalize.py`, `prompts/manager.py`,
`bio_agents.py`, and separately versioned tests and commits.

Normative relationship: this complete specification supersedes Proposed 1.2
Simplified and amends the Plan Contract and
artifact-handle portions of
`docs/refactor/state_contract_architecture_v2_spec.md`. Where that document
assigns step-level usage meaning to artifact `direction`,
`allowed_usage`, or handle spelling, this specification takes precedence.
Task-boundary metadata may remain, but it MUST NOT determine whether a Plan
step consumes or produces an artifact.

Version 1.2.1 closes ordering, atomicity, convergence, artifact-origin,
authority, I/O classification, candidate-scope, review-traceability, reader
compatibility, and staged-acceptance gaps in 1.2. It does not add an Agent,
Reviewer, Supervisor, or LLM gate.

This document is design-only. It does not authorize code implementation,
session repair, benchmark execution, or Git commit.

## 1. Executive Decision

BioMind 1.2.1 keeps the 1.2 removal of the extra model-based Plan
semantic-review subsystem specified in 1.1. Consultant remains the sole
TaskSpec semantic writer. Plan semantics before execution have exactly two
authorities:

1. Planner designs the scientific workflow and returns an explicit Plan
   Candidate.
2. Human Plan Review is the final semantic approval gate before Generate or
   Execute.

The Controller remains a deterministic contract and state machine. It does
not become a scientific reviewer after removal of the extra model stage.

Before this pipeline may create its first executable TaskSpec, an LLM-owned
request-intent decision MUST distinguish the user's interaction as exactly one
of `question`, `plan`, or `execute`, with a typed execution scope. Controller
validates only the closed schema, enum values, confidence policy, and field
coherence. It MUST NOT reclassify the request from TaskSpec content, prose,
filenames, or action-word/phrase lists. A missing, invalid, incoherent, or
low-confidence managed decision fails closed to a non-executable route.

In particular, `question` and advisory `plan` decisions do not authorize an
executable TaskSpec. Initial TaskSpec promotion requires the current typed
decision `request_kind=execute`, `execution_scope=managed`, and
`managed_intent_explicit=true`. Resuming an already-admitted durable managed
task is not a new intent upgrade and uses the persisted admission lineage.

The Plan Contract is:

> An artifact handle identifies an artifact. The Plan field containing the
> handle defines step-level usage. `step.inputs` means consume and
> `step.outputs` means produce. Artifact availability follows from explicit
> references, committed state, and the dependency DAG, never from handle
> spelling.

The compatibility names `input.*` and `output.*` MAY remain as display
aliases. Their prefixes are non-normative. An `output.*` handle can be
consumed by a later step because it appears in that later step's
`step.inputs`, not because the Controller has an allowlist exception.

Identity may be opaque; meaning must not be opaque to the Planner. Every
model-visible artifact is therefore represented by:

- a stable `handle`, used only as an identity reference; and
- a `semantic_view`, used by Planner to understand the artifact.

The Controller MUST NOT translate `semantic_view.role`,
`semantic_view.format`, descriptions, filenames, extensions, step titles,
or path substrings back into scientific workflow rules.

Planner MAY propose plan-local intermediate artifacts that are not present in
TaskSpec. Planner owns their semantic need and typed materialization request;
Controller owns authoritative identity, canonical path, binding revision,
content version, allocation, and transactionality.

A deterministically valid PlanSnapshot is not necessarily scientifically
approved. It may be committed and presented with a pending approval record,
but Generate and Execute remain ineligible until a matching human approval is
current.

TaskSpec output-path loss, required-input weakening, and ambiguity coherence
remain an independent Workstream T. They MUST NOT be represented as the cause
of the Planner handle crash and MUST NOT be hidden inside Workstream P.

## 2. Contract Defects Confirmed

All twelve closure findings are confirmed. “Confirmed” here means the 1.2
text is incomplete or contradictory, not that the proposed 1.2.1 mechanism is
already implemented.

### 2.1 P0 closure defects

| ID | Finding | Repository/spec evidence | 1.2.1 decision |
|---|---|---|---|
| P0-1 | Staleness must precede outcome side effects | Current Planner checks `evaluate_node_staleness()` before ordinary Plan JSON/strict parsing, but has no top-level outcome parser. The 1.2 state diagram routed reconciliation before staleness. | Parse only the envelope, then prove invocation currency before interpreting or routing any outcome. |
| P0-2 | PlanSnapshot and pending approval initialization need one commit | Current `save_plan_state()` stores approval objects/audit events before atomically replacing `plan_state.json`; current Planner has no immutable PlanSnapshot or pending ApprovalRecord. The 1.2 diagram separated snapshot commit from approval creation while prose required one transaction. | One atomic Plan commit covers artifact promotion, snapshot, pending approval, pointers, audit index, and one generation advance. |
| P0-3 | Stale behavior must respect epoch semantic base | `_NODE_RELEVANT_PRECONDITIONS["planner"]` currently checks task semantic hash; no PlanningEpoch exists. 1.2 said every stale redispatch remains in one epoch despite fixed base revisions. | Non-semantic stale may remain in epoch; semantic-base stale ends it and starts a child epoch. |
| P0-4 | Reconciliation needs cross-epoch convergence | No Planner/Consultant reconciliation fingerprint or non-progress ledger exists. Existing no-progress counters belong to other Consultant/Recovery protocols. | Add a structured `ReconciliationFingerprint` and one terminal `TASKSPEC_RECONCILIATION_NONPROGRESS` status. |
| P0-5 | V1 no-in-place artifact mutation was omitted | Current code has no `availability_origin`, `verified_task_input`, `admitted_resume_version`, or `PLAN_ARTIFACT_ORIGIN_CONFLICT` contract. | An already-available logical artifact cannot also be a current-Plan output; transformations create a new logical artifact. |
| P0-6 | Task artifact replacement authority was too broad | 1.2 allowed identity-operation choice from “Consultant/Planner proposal”; canonical TaskSpec write is already restricted to Consultant commit paths. | Only Consultant typed TaskSpec mutation may update/replace Task-owned artifact identity or binding. Planner may create plan-local artifacts only. |
| P0-7 | Transient classification is not closed at every I/O boundary | Finalize has typed/status-based handling, but generic `is_transient_llm_failure()` uses exception text markers; Plan state/object storage has broad `OSError`/`Exception` paths rather than one closed infrastructure error policy. | Classify typed I/O failure first at every boundary; validate state only after a successful read. Unknown exceptions are implementation errors unless an adapter certifies retry safety. |
| P0-8 | `candidate_scope_id` ownership/lifecycle was unspecified | No candidate-scope or proposed-artifact implementation exists. | Controller creates a model-invisible scope per initial candidate lineage; repair rules and allocation mapping bind to that scope explicitly. |

### 2.2 Optimization closure defects

| ID | Finding | Repository/spec evidence | 1.2.1 decision |
|---|---|---|---|
| O1 | “semantic diff” invites text inference | Current approval projection is typed, but 1.2 called its review output a task/plan semantic diff. | Human Review receives a typed TaskSpec-to-Plan traceability projection built only from IDs/contracts. |
| O2 | Unscoped review text invalidates too early | Current `plan_review` appends durable evidence, then calls `reset_plan_for_replanning()` before Consultant classifies the text. | Preserve snapshot/approval during classification; invalidate only after a typed Task mutation or PlannerFeedback exists. |
| O3 | New snapshots need a minimum-reader gate | Current state uses schema version 8 but has no `minimum_reader_version`; `migrate_plan_state()` does not fail closed merely because source schema is newer and rewrites version fields to current values. | Persist Plan Contract/minimum-reader versions and fail closed before migration when unsupported. |
| O4 | One acceptance gate would hide incident closure | Current work spans resolver, orchestration, artifact allocation, persistence, and approval. | Split acceptance into Milestone A incident closure, B planning hardening, and C state-model cleanup. |

The 1.2 removals remain normative: there is no Automated Plan Semantic
Review, Semantic Reviewer invocation, `PLAN_SEMANTIC_REVIEW_REJECTED`,
semantic-review repair branch, or replacement LLM quality gate. Consultant's
separate TaskSpec/constraint work is not that removed subsystem.

## 3. Repository Audit Delta Relative to 1.2

Only facts that correct or extend the 1.2 audit are listed here. Previously
recorded direction/prefix consumers remain confirmed.

### 3.1 Response ordering and staleness

- Current Planner remote-call handling reloads authoritative state and calls
  `evaluate_node_staleness()` before ordinary Plan JSON resolution and strict
  Plan parsing. Therefore the repository is safer than the 1.2 diagram for the
  one recognized Plan shape.
- There is no outer Planner outcome envelope parser. The prompt advertises
  `TASK_SEMANTICS_INCOMPLETE`, but the node treats every current response as a
  Plan document; that outcome can enter structure repair.
- Planner staleness currently treats task semantic hash as relevant and state
  generation/task record hash as ignorable revalidation changes. It does not
  bind a persisted PlanningEpoch or a base PlanSnapshot.

### 3.2 Prefix and closure delta

- There is no runtime `startswith("local.")` consumer in the audited Python
  checkout; candidate-local scope is not implemented at all.
- The two active Plan-usage prefix sites remain
  `_select_current_step_closure()` (`startswith("output.")`) and
  `validate_plan_managed_artifacts()` (legacy output-handle discovery).
- `ManagedArtifactBinding` is currently compiled from TaskSpec boundary
  `input`/`output` rows and carries direction, canonical path, policy, and an
  action placeholder. `materialize_managed_action()` restricts placeholders to
  complete structured argv/env values. That action-level adapter may remain,
  but its direction/path contract must be downstream of an already-resolved
  Plan step usage; current Plan validation still reuses output direction and
  prefix assumptions and therefore needs migration.
- `build_node_context()` applies `_select_current_step_closure()` to Generate,
  Recovery, and Supervisor. CompletionGate primarily reads normalized step
  artifact contracts, provenance heads/versions, renderer manifests, and
  dependency IDs; it has no direct input/output-prefix closure, but upstream
  normalization can still feed it inferred path/producer relations.

### 3.3 Persistence and atomicity delta

- `current_plan` is a projection embedded in mutable `plan_state.json`, not a
  separately persisted immutable PlanSnapshot.
- `approval_record` is embedded in mutable Plan state. A content-addressed
  `approval_snapshot` is created only on approval; no pending ApprovalRecord is
  initialized when Planner commits.
- `_atomic_write_plan_state()` holds an OS file lock, checks generation, and
  atomically replaces `plan_state.json`. Before replacement it may write a
  content-addressed approval object and append an immutable audit event. Those
  unreachable objects are tolerated after a failed pointer commit. This is a
  valid append-only technique, but it is not the full multi-object atomic Plan
  commit required by 1.2.1.
- There is no plan-local artifact promotion transaction to include in that
  critical section today.

### 3.4 Review and Consultant protocol delta

- Every unapproved free-text Human Review response is currently treated as
  rejection: it is appended as `U(n)`, then the Plan/approval state is reset,
  then Consultant classifies it. A mere explanation question therefore loses
  the current review state too early.
- Consultant's current typed response can contain TaskSpec operations,
  `planner_feedback`, both, or neither. Tests cover the “both” route. The
  protocol therefore can support classification-first behavior without a new
  LLM node.

### 3.5 Convergence, artifact origin, and candidate scope delta

- No reconciliation-specific stable fingerprint, evidence revision, or
  non-progress count exists.
- Existing validation convergence fingerprints concern Plan violations;
  existing Consultant/Recovery no-progress counters do not close the
  Planner-to-Consultant reconciliation loop.
- Current provenance has logical IDs, producers, consumers, heads, and
  materialized versions, but no typed `availability_origin` and no explicit V1
  no-in-place invariant.
- No `candidate_scope_id`, `ArtifactRefKey` union, `proposed_artifacts`, or
  AllocationIntent exists in runtime code.

### 3.6 Error and reader-compatibility delta

- Finalize provider handling already demonstrates typed exception/status and
  adapter-certified retry safety. Generic Planner LLM handling still calls a
  text-marker classifier, so the desired closed policy is not system-wide.
- Plan state has schema/migration version 8 and legacy migration logic, but no
  Plan Contract version or minimum-reader compatibility field. A newer
  persisted schema is not explicitly rejected before migration, so an old
  reader could reinterpret or rewrite data it does not understand.

## 4. Scope, Goals, and Non-goals

### 4.1 Workstream P goals

Workstream P MUST:

1. make artifact identity independent of step usage;
2. give Planner enough artifact meaning through `semantic_view`;
3. support typed plan-local artifacts without Planner-selected authoritative
   IDs or paths;
4. validate references, DAG, availability, bindings, revisions, and state
   deterministically;
5. handle every candidate-owned protocol defect inside a bounded Planner
   candidate boundary;
6. distinguish candidate, reconciliation, stale, invariant,
   implementation, and transient failures;
7. commit one immutable PlanSnapshot and its promoted plan-local bindings in
   one transaction/single-writer boundary;
8. make Human Plan Review the only execution-before-semantic-approval gate;
9. route feedback without Python natural-language classification;
10. migrate Generate, Recovery, managed-artifact validation, and
    CompletionGate projections away from Plan handle-prefix semantics.

### 4.2 Workstream T goals

Workstream T independently addresses:

- canonical output-directory and output-path preservation;
- required input strength/coherence;
- ambiguity and confirmed-decision coherence;
- historical user evidence preservation across Consultant/user turns.

### 4.3 Non-goals

This specification does not:

- implement a special permission for `output.*` under inputs;
- rename all handles;
- require `inputs/outputs` to be renamed immediately;
- make Controller judge scientific appropriateness;
- make Planner authoritative over TaskSpec, paths, IDs, hashes, approvals, or
  runtime status;
- make Human Plan Review mutate an existing snapshot;
- replace existing CAS/content-addressed state storage;
- merge TaskSpec coherence work into the Planner crash fix;
- change Consultant's separate semantic-constraint compilation architecture;
- authorize code changes in this documentation-only turn.

## 5. Normative Terminology

### 5.1 ArtifactHandle

An `ArtifactHandle` is a model-visible stable identity alias within its
declared scope. Its text is opaque to Controller execution semantics.

The handle:

- MUST resolve uniquely;
- MUST NOT encode step consume/produce authority;
- MUST NOT be parsed by prefix, filename, extension, or substring to decide
  scientific usage;
- MAY keep legacy `input.*` or `output.*` spelling during migration.

### 5.2 semantic_view

`semantic_view` is the model-facing description required for scientific
planning. It is not an authoritative execution contract.

Minimum fields:

```json
{
  "handle": "A17",
  "semantic_view": {
    "role": "variant_vcf",
    "format": "vcf",
    "description": "variant calls to annotate",
    "source_class": "user_supplied",
    "required": true
  }
}
```

Planner uses this meaning to choose workflow structure. Controller may verify
the projection was produced from a canonical record, but MUST NOT use its
natural-language values to invent step edges or scientific compatibility.

### 5.3 artifact_contract

`artifact_contract` is the typed, machine-readable contract Controller may
validate and materialize. It is separate from `semantic_view`.

```json
{
  "object_kind": "directory",
  "materialization_policy_id": "managed_intermediate_v1",
  "lifecycle": "plan_local"
}
```

Allowed values and policy IDs are registry-defined. Controller validates exact
enum/schema membership and policy availability. It does not infer them from
role, description, suffix, or filename.

### 5.4 ArtifactRefKey

Before allocation, every artifact reference MUST be represented internally as
one closed union:

```text
ArtifactRefKey =
    AuthoritativeArtifactRefKey(logical_artifact_id)
    OR
    CandidateLocalArtifactRefKey(candidate_scope_id, local_ref)
```

Producer maps, consumer maps, availability checks, dependency closure, and
diagnostics operate on `ArtifactRefKey`. They MUST NOT contain
`ref.startswith("local.")` or equivalent semantic branching.

After commit, every candidate-local key maps to an authoritative key. The
mapping is immutable evidence for that PlanSnapshot.

#### 5.4.1 Controller-owned candidate scope

`candidate_scope_id` is Controller-owned control metadata. Controller creates
it when registering the initial Planner invocation. It is:

- model-invisible and absent from Planner response schemas;
- bound to one PlanningEpoch and one candidate lineage;
- included by Controller when converting returned `local_ref` values into
  `CandidateLocalArtifactRefKey` values;
- required on AllocationIntent, provisional reservation, allocation mapping,
  diagnostics, and commit evidence;
- never inferred from `local_ref` spelling.

A bounded repair of the same candidate lineage inherits the same
`candidate_scope_id`; this preserves local-ref identity across an allowed
PlanPatch. A full replacement candidate, semantic-base redispatch, or new
PlanningEpoch receives a new scope. Controller MUST reject any model payload
that returns, selects, or overrides `candidate_scope_id`.

### 5.5 Identity layers

- `logical_artifact_id`: stable logical identity.
- `artifact_binding_revision`: revision of canonical binding/path/contract.
- `artifact_version_id`: one materialized content/version identity.
- `handle`: model-facing alias resolving to a logical identity.
- `local_ref`: candidate-scoped identity before promotion.

Controller does not decide whether meaning “changed” by comparing text.
Identity continuation is expressed by typed operations:

- `update_artifact_binding` -> same `logical_artifact_id`, new binding
  revision when the binding changes;
- `replace_logical_artifact` -> new `logical_artifact_id`.

Authority depends on ownership:

- for a TaskSpec-owned authoritative artifact, `update_artifact_binding` and
  `replace_logical_artifact` may originate only from Consultant typed TaskSpec
  mutation, including an explicit user decision canonicalized by Consultant;
- Planner may reference a Task-owned artifact and may consume/produce it only
  as permitted by the canonical Task/Plan contract, but MUST NOT replace its
  identity, rebind its path, or redefine its Task semantic role;
- Planner may propose a new plan-local artifact. Controller allocates its new
  identity only after deterministic validation and as part of atomic Plan
  commit;
- Controller validates operation legality and performs allocation/commit. It
  never selects the semantic replacement operation by comparing text.

#### 5.5.1 Availability origin and V1 immutability

Every authoritative artifact available before the current Plan has a typed
`availability_origin`. V1 includes at least:

- `verified_task_input`;
- `admitted_resume_version`;
- `current_plan_produced`;
- `not_yet_available`.

V1 MUST NOT support in-place mutation of an available logical artifact. If an
artifact is available from `verified_task_input` or
`admitted_resume_version`, no current Plan step may list the same
`logical_artifact_id` under `step.outputs`. A current-Plan output has exactly
one producing step in V1; after that step makes it available, later steps may
consume it but may not produce that same identity again. No step may list the
same logical identity as both input and output. Every transformation `A -> B`
requires `A.logical_artifact_id != B.logical_artifact_id`.

Append-only artifacts, mutable workspaces, and explicit in-place versioning
require a future independent contract and are outside V1.

### 5.6 Plan Candidate, PlanSnapshot, and PlanApprovalRecord

- `PlanCandidate` is uncommitted Planner output.
- `PlanSnapshot` is an immutable, deterministically valid committed Plan
  plus resolved artifact references and allocation mapping.
- `PlanApprovalRecord` is a separate record referring to exactly one
  PlanSnapshot.

Rejected candidates and their repair attempts are audit records, never
PlanSnapshots.

Every persisted PlanSnapshot MUST declare:

```json
{
  "plan_contract_version": "1.2.1",
  "minimum_reader_version": "1.2.1"
}
```

The concrete reader-version comparison scheme MAY use ordered integers or a
semantic-version registry, but it MUST be deterministic. A reader that does
not satisfy `minimum_reader_version` fails closed before migration,
normalization, approval evaluation, prefix interpretation, or pointer update.

The authoritative root Plan-pointer envelope MUST repeat these compatibility
fields so a reader can reject the record before dereferencing or migrating the
snapshot. Because a binary that predates this rule cannot be made safe by data
alone, migration uses a reader-first deployment barrier:

1. deploy the fail-closed root-envelope check to every active reader/writer;
2. fence or drain binaries that do not advertise that reader capability;
3. only then allow a 1.2.1 writer to promote a new root Plan pointer.

A mixed-version writer lease/capability registry enforces this barrier. Merely
adding `minimum_reader_version` inside a new snapshot while allowing an older
process to open the mutable root state does not satisfy this contract.

### 5.7 PlanningEpoch

A `PlanningEpoch` bounds one pre-commit planning attempt. It starts on
Planner dispatch and ends on PlanSnapshot commit, TaskSpec reconciliation
route, terminal exhaustion, or unrecoverable internal failure.

Human Plan Review always occurs after the epoch that created the snapshot has
ended.

## 6. Authority and Decision Ownership

| Decision/state | Authority | Permitted proposal source | Forbidden inference/write |
|---|---|---|---|
| question vs advisory plan vs execution request | typed request-intent admission | LLM semantic classifier | Controller keyword/prose/TaskSpec reclassification; implicit upgrade to managed execution |
| user goal, constraints, required inputs/outputs, confirmed decisions, ambiguities | canonical TaskSpec | Consultant | Planner/Controller direct mutation |
| scientific workflow, step ordering, artifact consumption, intermediates, success criteria, capability composition | PlanSnapshot | Planner Candidate then deterministic commit | Python prefix/keyword rules |
| Task-owned artifact semantic meaning and replacement | canonical TaskSpec | Consultant typed mutation | Planner replacement/rebinding; Controller natural-language interpretation |
| plan-local artifact semantic proposal | Plan Candidate then PlanSnapshot | Planner | TaskSpec mutation or authoritative ID/path selection |
| object kind, lifecycle, materialization policy | typed artifact contract | Planner for candidate-local artifact; registry constrains values | inference from description/suffix |
| logical ID, path, binding revision, content version | artifact registry/controller | Consultant-owned Task operation or validated plan-local allocation request | LLM-selected authority values |
| graph/reference/availability validity | validation state | none | LLM override |
| Plan semantic approval | PlanApprovalRecord | Human Plan Review | Planner self-approval or Controller heuristics |
| current execution step, attempts, budgets | RuntimeState | deterministic policy | TaskSpec/PlanSnapshot mutation |
| TaskSpec reconciliation | canonical TaskSpec revision | Consultant after typed Planner outcome | Planner direct TaskSpec edit |

## 7. Planner Wire Contracts

### 7.1 Authoritative artifact catalog entry

Planner receives a deterministic semantic projection:

```json
{
  "handle": "output.reproduce_script",
  "semantic_view": {
    "role": "reproduce_script",
    "format": "shell",
    "description": "script that reproduces the approved workflow",
    "source_class": "task_required_output",
    "required": true
  }
}
```

The ControllerBinding privately resolves that handle to logical identity,
binding revision, canonical path, and policy. Those control fields do not
need to be returned by Planner.

### 7.2 Plan Candidate success shape

```json
{
  "outcome_type": "PLAN_CANDIDATE",
  "proposed_artifacts": [],
  "steps": [
    {
      "step_id": "step-1",
      "title": "Generate the reproducibility script",
      "action": "execute",
      "purpose": "analysis",
      "tool": "registered workflow capability",
      "inputs": ["input.variant_vcf"],
      "outputs": ["output.reproduce_script"],
      "dependencies": [],
      "success_criteria": ["The declared script is materialized"],
      "skill_ids": [],
      "workflow_stage_ids": [],
      "batch_id": "",
      "risk_level": "low",
      "semantic_constraint_ids": []
    },
    {
      "step_id": "step-2",
      "title": "Generate the task report",
      "action": "execute",
      "purpose": "analysis",
      "tool": "registered workflow capability",
      "inputs": ["output.reproduce_script"],
      "outputs": ["output.report"],
      "dependencies": ["step-1"],
      "success_criteria": ["The declared report is materialized"],
      "skill_ids": [],
      "workflow_stage_ids": [],
      "batch_id": "",
      "risk_level": "low",
      "semantic_constraint_ids": []
    }
  ],
  "semantic_claims": [],
  "validation_checklist": [
    {
      "check_id": "check-1",
      "description": "Verify explicit artifact references and dependencies"
    }
  ],
  "metadata": {
    "schema_version": 1,
    "planner_contract": "structured_plan_v1"
  }
}
```

The executable prompt example and the strict reader use the same shared wire
definition in `agent_core.plan_contract`; this documentation mirrors that
contract and is not an independent compatibility schema.

`outcome_type` is the only field the top-level envelope parser needs before
staleness. At that stage Controller MUST NOT resolve handles, validate missing
facts, allocate artifacts, append semantic events, or route another node.
`candidate_scope_id` is deliberately absent; Controller attaches it from the
invocation registry only after the response passes staleness.

Normative meaning:

- the first step produces the referenced script;
- the second step consumes the same artifact;
- no direction mismatch exists;
- the second step may run only after the first producer makes the artifact
  available and its dependency requirements are satisfied.

### 7.3 Plan-local artifact declaration

```json
{
  "local_ref": "local.snpeff_database",
  "semantic_view": {
    "role": "snpeff_custom_database",
    "description": "custom snpEff database"
  },
  "artifact_contract": {
    "object_kind": "directory",
    "materialization_policy_id": "managed_intermediate_v1",
    "lifecycle": "plan_local"
  }
}
```

Planner MAY choose `local_ref`, `semantic_view`, and a contract using
registered typed values.

Planner MUST NOT provide:

- `logical_artifact_id`;
- canonical or absolute path;
- `artifact_binding_revision`;
- `artifact_version_id`;
- CAS token, hash, object ref, or reservation ID.

`local_ref` MUST be unique inside `candidate_scope_id` and MUST be declared
in `proposed_artifacts`. JSON serialization order is irrelevant: references
may appear textually before or after the declaration.

Planner returns only `local_ref`. The wire schema MUST reject a returned
`candidate_scope_id`, even if its value happens to match Controller state.

### 7.4 TaskSpec reconciliation outcome

Planner may return this instead of a Plan Candidate:

```json
{
  "outcome_type": "TASKSPEC_RECONCILIATION_REQUIRED",
  "missing_semantic_facts": [
    {
      "fact_key": "reference_database_version",
      "reason": "database construction cannot be planned reproducibly without it",
      "affected_operation_id": "operation.build_custom_annotation_database",
      "relevant_confirmed_decision_ids": [],
      "durable_evidence_may_resolve": true
    }
  ]
}
```

This is not a malformed Plan and not a Planner repair request. Controller
routes the typed outcome to Consultant only after the Planner invocation
passes staleness and the reconciliation convergence guard. Planner MUST NOT
mutate TaskSpec or ask the user directly through an executable Plan step for a
TaskSpec-owned semantic gap.

Consultant then decides:

- durable evidence resolves it -> revise/complete TaskSpec without asking;
- information is genuinely user-owned and absent -> ask the user;
- request contradicts canonical evidence -> reconcile the contradiction;
- request is invalid or non-progressing at the same TaskSpec revision ->
  typed internal/contract handling, not infinite Planner repair.

### 7.5 Top-level envelope parser

The envelope parser recognizes only registered `outcome_type` values and the
minimum syntactic shape needed to locate that discriminator. It MUST be:

- pure and side-effect-free;
- independent of TaskSpec semantics and artifact resolution;
- unable to append audit/feedback, create a candidate scope, or route nodes;
- strict enough to accept only raw JSON or one complete whole-response
  `json` fence as transport-equivalent input;
- strict enough to reject surrounding prose, incomplete/multiple fences,
  fragment extraction, and multiple objects;
- followed immediately by invocation staleness evaluation.

Fence removal is transport normalization only. It MUST NOT alter the JSON
body, fill fields, reinterpret artifacts, or consume a Planner repair attempt.

An unrecognized or malformed envelope is not allowed to bypass the staleness
gate. Controller may retain raw diagnostic evidence, but it classifies the
response as candidate-invalid only after proving the invocation current. A
stale malformed response is discarded as stale without Planner repair.

## 8. Deterministic Controller Invariants

### 8.1 Candidate schema and reference invariants

Before allocation, Controller MUST verify:

- response is exactly one recognized outcome type;
- Plan Candidate fields have correct types and closed-schema values;
- step IDs are unique;
- `local_ref` values are unique within candidate scope;
- every candidate-local reference is declared in `proposed_artifacts`;
- every authoritative handle resolves uniquely in the dispatch binding;
- every artifact contract uses a registered object kind, lifecycle, and
  materialization policy;
- Planner did not submit Controller-owned identity/path/revision/hash fields;
- duplicate declarations and conflicting contracts are rejected.

All candidate-local declaration/schema defects MUST be found here, before any
allocation intent or persistent reservation.

### 8.2 DAG invariants

Controller MUST verify:

- every dependency step exists;
- no step depends on itself;
- the dependency graph is acyclic;
- every producer referenced by availability logic is reachable before the
  consumer;
- topological order is derived from explicit dependencies, not list position
  alone.

### 8.3 Artifact-flow invariants

For each `ArtifactRefKey`, Controller builds explicit producer and consumer
maps from step fields:

```text
for ref in step.inputs:  consume(step, ref)
for ref in step.outputs: produce(step, ref)
```

It MUST verify:

- a produced artifact has an allowed producer cardinality under its typed
  contract, which is exactly one producer in V1;
- a consumed artifact is available before the consuming step;
- an initial authoritative artifact is available according to canonical
  state;
- a produced artifact is available only after its producer succeeds and
  required validation state is satisfied;
- no required consumer depends on an unreachable producer;
- an artifact is not silently rebound to a different identity/path;
- an artifact with availability origin `verified_task_input` or
  `admitted_resume_version` has no current-Plan producer;
- no step consumes and produces the same logical identity, and no later step
  produces an identity already made available by a current-Plan producer;
- every transformation output has a logical identity distinct from every
  consumed available input it transforms.

Violation of the last three rules yields the stable candidate diagnostic
`PLAN_ARTIFACT_ORIGIN_CONFLICT`, including the step ID, logical artifact ID,
availability origin, and conflicting input/output fields. It is a candidate
contract defect when the candidate explicitly creates the conflict. If the
same conflict appears only after a previously valid PlanSnapshot commit, it is
an `INTERNAL_CONTROLLER_INVARIANT_ERROR`.

No rule may inspect the handle's prefix to build these maps.

### 8.4 Canonical binding and state invariants

Controller MUST verify:

- TaskSpec and Plan base revisions match the dispatch binding;
- authoritative artifact identity and binding revision are current;
- canonical path and path policy agree with the registry;
- Plan Candidate does not overwrite authoritative binding data;
- allocation intent is owned by the current candidate and PlanningEpoch;
- CAS/generation is rechecked immediately before commit;
- `candidate_scope_id` on every candidate-local key, AllocationIntent, and
  mapping equals the Controller invocation scope;
- Plan Contract and minimum-reader compatibility have passed before snapshot
  interpretation;
- existing PlanSnapshot, ApprovalRecord, RuntimeState, or artifact versions
  are not mutated by a rejected candidate;
- content versions are not fabricated before materialization.

### 8.5 What deterministic validation may and may not prove

Deterministic validation may prove:

- an explicit reference exists;
- a declared enum or contract ID is registered;
- a dependency edge is valid;
- an explicit constraint reference points to a canonical TaskSpec constraint;
- required structural references are present;
- an artifact is available under the state machine.

It does not prove:

- that a VCF is scientifically appropriate for a particular analysis;
- that a custom database should or should not be built;
- that a filename “looks like” a reference genome;
- that a step title implies validation;
- that a skill mention in prose makes a workflow scientifically complete;
- that an extension determines artifact role or object kind.

### 8.6 I/O-before-invariant classification

Every provider, object-store, database, lock, filesystem, and remote registry
boundary follows this sequence:

```text
perform I/O
  +-- adapter emits recognized typed retryable infrastructure error
  |     -> TRANSIENT_SYSTEM_ERROR
  +-- adapter emits recognized typed non-retryable domain/integrity error
  |     -> classify by the closed domain policy
  +-- successful read/write
  |     -> validate authoritative state invariants
  +-- unknown exception
        -> INTERNAL_CONTROLLER_IMPLEMENTATION_ERROR
```

Examples of registered transient adapter outcomes include
`ObjectStoreTimeout`, `DatabaseConnectionReset`, `TypedLockTimeout`, and
`TemporaryProviderUnavailable`. Names here denote types/codes registered by
the adapter; Controller MUST NOT search exception message text for these
words. A successful read that proves a committed object absent, corrupt, or
contradictory may then be an `INTERNAL_CONTROLLER_INVARIANT_ERROR`.

Retry safety is explicit metadata supplied by the closed adapter policy. An
unknown exception is never assumed retry-safe.

## 9. Forbidden Semantic Heuristics

Controller, validators, and routers MUST NOT decide Plan scientific meaning
from:

- `input.*` or `output.*` prefixes;
- filename or extension;
- role or description text;
- path substring or basename;
- step title, purpose prose, or success-criteria prose;
- keyword lists;
- natural-language similarity;
- presence/absence of a skill name in rendered Plan text.

Compatibility parsers MAY recognize legacy syntax to construct typed fields,
but:

1. the compatibility result must be marked as migrated/derived;
2. it must not silently override an explicit typed field;
3. new candidates MUST use the typed schema;
4. scientific acceptance cannot depend on the heuristic;
5. migration ambiguity must become a typed migration error or require
   replanning.

Task-boundary `direction` and action-level read/write placeholders are not
automatically forbidden. They may classify an already-resolved operation, but
they cannot determine Plan step usage. The committed step contract is the
source of consume/produce truth.

## 10. Candidate Processing and Atomic Commit

### 10.1 Normative state machine

```text
START PlanningEpoch
  |
DISPATCH Planner with ControllerBinding + candidate_scope_id
  |
RECEIVE RAW PLANNER RESPONSE
  |
PARSE TOP-LEVEL OUTCOME ENVELOPE ONLY
  |
CHECK INVOCATION STALENESS
  |
  +-- semantic-base stale
  |      -> discard entire response; no semantic/state side effects
  |      -> END old epoch(status=stale_semantic_base)
  |      -> START child epoch(parent=old, new semantic base)
  |      -> Planner redispatch under new candidate_scope_id
  |
  +-- non-semantic stale
  |      -> discard entire response; no semantic/state side effects
  |      -> fresh binding; bounded same-epoch Planner redispatch
  |
  +-- current
         |
         +-- TASKSPEC_RECONCILIATION_REQUIRED
         |      -> compute/check ReconciliationFingerprint
         |      +-- terminal ledger already at configured limit
         |      |      -> END epoch
         |      |      -> TASKSPEC_RECONCILIATION_NONPROGRESS
         |      +-- allowed
         |             -> persist typed reconciliation record
         |             -> END epoch(status=reconciliation_required)
         |             -> Consultant
         |             +-- TaskSpec semantic mutation
         |             |      -> commit TaskSpec revision
         |             |      -> START new epoch
         |             +-- genuine user-owned ambiguity
         |             |      -> ask_user
         |             +-- no progress/same fingerprint
         |                    -> increment durable nonprogress ledger
         |                    +-- below limit
         |                    |      -> START bounded successor epoch
         |                    |         (same semantic base, new scope)
         |                    +-- at limit
         |                           -> TASKSPEC_RECONCILIATION_NONPROGRESS
         |
         +-- PLAN_CANDIDATE
                |
PARSE / RESOLVE TO ArtifactRefKey using Controller candidate scope
  |
  +-- candidate invalid
  |      -> bounded Planner candidate repair in same PlanningEpoch
  |
  +-- internal / transient
  |      -> respective non-Planner handling
  |
VALIDATE DAG / ARTIFACT FLOW / AVAILABILITY / CANONICAL BINDINGS
  |
PREPARE_ALLOCATION
  |  compute AllocationIntent only; no authoritative artifact/path
  |
RECHECK STALENESS / CAS
  |
ATOMIC PLAN COMMIT
  |  recheck TaskSpec / base PlanSnapshot / artifact bindings / CAS
  |  allocate/promote plan-local logical identities
  |  allocate managed bindings and paths
  |  create local_ref -> authoritative mapping
  |  rewrite typed Plan references
  |  create immutable PlanSnapshot
  |  create pending PlanApprovalRecord
  |  update current Plan pointer
  |  update current approval pointer
  |  append authoritative audit index/events
  |  advance generation exactly once
  |
END PlanningEpoch
  |
HUMAN PLAN REVIEW
  |
  +-- approve -> approval record approved -> Generate/Execute eligible
  +-- explicit plan feedback -> durable U(n) -> changes_requested
  |      -> Planner -> new PlanningEpoch
  +-- explicit task feedback -> durable U(n) -> Consultant
  |      -> TaskSpec revision -> Planner -> new PlanningEpoch
  +-- unscoped free text -> durable U(n) -> Consultant classification
         +-- typed change/feedback -> appropriate route/new epoch
         +-- explanation only -> preserve snapshot and approval
```

No LLM result may create semantic or state side effects before Controller
proves that the result is still based on the current authoritative planning
base. In particular, `TASKSPEC_RECONCILIATION_REQUIRED` is not exempt from the
staleness gate. Raw-response observability may write an invocation-scoped,
unreachable diagnostic blob, but MUST NOT advance authoritative audit,
semantic, approval, artifact, or routing state.

### 10.2 Candidate repair boundary

The following are candidate-owned and repairable:

- malformed JSON/closed-schema violation;
- missing required candidate field;
- unknown authoritative handle returned by Planner;
- undeclared or duplicate `local_ref`;
- invalid typed plan-local artifact contract;
- dependency to a missing step;
- DAG cycle;
- duplicate/forbidden producer under an explicit contract;
- consumed artifact unavailable before the step;
- Planner attempt to write Controller-owned identity/path fields;
- PlanPatch outside its allowed candidate scope.

These map to `PLANNER_CANDIDATE_INVALID` and enter bounded repair with:

- the same canonical TaskSpec semantic closure as initial planning;
- the same artifact catalog including `semantic_view`;
- the rejected candidate or allowed patch scope;
- complete actionable diagnostics grouped for the model;
- no committed-state mutation.

The repair invocation belongs to the same candidate lineage and inherits the
Controller-held `candidate_scope_id`; the repair model still does not see or
return it. A full redispatch that is not an allowed repair, including any
semantic-base stale redispatch, creates a new candidate scope. A repair MUST
NOT use a newly allocated scope to evade duplicate-local-ref or allocation
identity checks.

### 10.3 Allocation preparation

`PREPARE_ALLOCATION` computes an `AllocationIntent`:

- candidate and PlanningEpoch owner;
- exact Controller-owned `candidate_scope_id`;
- candidate-local keys;
- requested typed contracts;
- selected registered materialization policies;
- proposed parent/output root binding, if allowed by policy;
- collision and capacity checks;
- deterministic allocation inputs.

It MUST NOT:

- create a committed logical artifact;
- publish a canonical path;
- create an unowned authoritative directory;
- update the current Plan pointer;
- change approval/runtime state.

If allocation preparation discovers an invalid candidate-local schema or
contract, the pre-allocation validator is incomplete. The immediate candidate
may still receive one stable candidate diagnostic during migration, but this
condition MUST also be recorded as an internal validator defect and MUST NOT
remain a normal long-term repair branch.

### 10.4 Atomic commit

Within one transaction or equivalent single-writer critical section,
Controller MUST:

1. re-read current TaskSpec, Plan pointer, artifact bindings, and generation;
2. verify dispatch and allocation preconditions;
3. allocate logical artifact IDs;
4. allocate managed bindings and canonical paths;
5. create candidate-local to authoritative mapping;
6. rewrite all Plan references by typed key;
7. create one immutable PlanSnapshot;
8. create a separate pending PlanApprovalRecord;
9. update current Plan and approval pointers;
10. append audit events;
11. advance generation exactly once.

PlanSnapshot, pending PlanApprovalRecord, artifact promotion, current Plan
pointer, and current approval pointer become authoritative together. Any
failure before the one commit point leaves all authoritative pointers and the
visible generation unchanged. It is forbidden for a current Plan pointer to
identify a new snapshot while its matching pending approval pointer is absent.

Content-addressed objects MAY be physically prewritten because they are
immutable, but before the pointer transaction they are unreachable provisional
objects, not a committed PlanSnapshot, ApprovalRecord, or artifact binding.

### 10.5 Provisional fallback

If the storage layer cannot perform step 3-11 atomically, it MUST use a
provisional reservation with:

- reservation ID;
- owner `candidate_scope_id`;
- owner `planning_epoch_id`;
- base generation/CAS;
- provisional identities and paths;
- provisional PlanSnapshot object/ref;
- provisional pending PlanApprovalRecord object/ref;
- provisional current-pointer promotion set;
- provisional audit-index/event set;
- expiration/TTL;
- explicit status `reserved|promoted|released|expired`;
- idempotent promotion in the PlanSnapshot commit;
- rollback/reaper logic.

A provisional reservation covers allocation, snapshot, approval
initialization, audit-index update, and pointer promotion as one promotion
unit. It is not available to Human Review, Generate, or Execute and is not a
formal artifact binding. No promoted artifact/path, current snapshot, or
current approval may exist without all other members of the committed unit.

## 11. PlanningEpoch

The persisted minimum shape is:

```json
{
  "planning_epoch_id": "planning-epoch-...",
  "parent_epoch_id": null,
  "start_reason": "initial_planning",
  "base_task_revision": 2,
  "base_task_semantic_identity": "task-semantic-...",
  "base_plan_revision": 0,
  "base_plan_snapshot_id": null,
  "planner_calls_total": 2,
  "candidate_repairs": 1,
  "stale_redispatches": 0,
  "status": "active"
}
```

Required budgets:

- `MAX_PLANNER_CALLS_PER_EPOCH`;
- `MAX_CANDIDATE_REPAIRS_PER_EPOCH`.

`planner_calls_total` includes initial Planner, candidate repair, and stale
redispatch model calls. `candidate_repairs` counts only calls caused by
candidate-owned invalidity. Stale redispatch is separately observable but
still consumes the total Planner call budget.

Transient retries remain governed by the shared runtime/provider policy.
1.2.1 MUST NOT create a duplicate Planner-only transient retry framework.

Lifecycle rules:

- pre-commit candidate repair stays in the same epoch;
- a non-semantic stale redispatch MAY stay in the same epoch after a fresh
  binding and consumes both `planner_calls_total` and `stale_redispatches`;
- a semantic-base stale response ends the old epoch and starts a child epoch
  with `parent_epoch_id`, `start_reason=semantic_base_stale`, and new base
  task/Plan identities;
- TaskSpec reconciliation ends the epoch;
- a Consultant no-progress result below the convergence limit may start one
  bounded successor epoch with the same semantic base and a new candidate
  scope; the durable cross-epoch ledger is preserved;
- PlanSnapshot commit ends the epoch;
- plan-level Human Review feedback always starts a new epoch;
- task-level feedback reaches Planner only after Consultant reconciliation and
  starts a new epoch;
- there is no “continue old epoch after review” mode.

A PlanningEpoch may continue only while its semantic planning base remains
the same. It is invalid for an epoch persisted with `base_task_revision=2` to
dispatch or accept a Planner call bound to TaskSpec semantic revision 3.
Generation movement, audit append, runtime telemetry, or record-only changes
are non-semantic only when the current task semantic identity and base
PlanSnapshot semantic identity remain equal and the affected store adapter
certifies revalidation safe.

Budget exhaustion yields a typed terminal planning failure with the last
candidate diagnostics and counters. It MUST NOT be converted to ask_user
unless a separate Consultant-owned semantic question genuinely exists.

### 11.1 ReconciliationFingerprint and cross-epoch convergence

`ReconciliationFingerprint` is a stable digest of a canonical structured
payload, never a natural-language response hash:

```json
{
  "fingerprint_schema_version": 1,
  "task_semantic_identity": "task-semantic-...",
  "missing_fact_keys": ["reference_database_version"],
  "affected_operation_ids": ["operation.build_custom_annotation_database"],
  "relevant_confirmed_decision_ids": []
}
```

Before hashing, Controller sorts and deduplicates each identifier list,
normalizes registered identifiers by their schema rules, rejects unknown
fields, and excludes `reason`, explanation prose, timestamps, invocation IDs,
epoch IDs, and model formatting.

The durable convergence ledger contains at least:

```json
{
  "last_reconciliation_fingerprint": "sha256:...",
  "last_reconciliation_task_semantic_revision": 4,
  "last_reconciliation_task_semantic_identity": "task-semantic-...",
  "last_reconciliation_evidence_revision": 9,
  "reconciliation_nonprogress_count": 1,
  "last_consultant_outcome": "no_semantic_change"
}
```

The relevant evidence revision advances only for durable `U(n)` or typed
evidence linked to at least one missing fact, affected operation, or confirmed
decision in the fingerprint. Unrelated audit/runtime writes cannot reset the
guard.

The outcome is non-progress when all are true:

- the reconciliation fingerprint is unchanged;
- TaskSpec semantic identity is unchanged;
- no new relevant durable evidence exists;
- Consultant produced no TaskSpec semantic change and no proven genuine
  user-owned missing decision.

The first completed no-progress cycle increments the counter. Repetition at
the configured closed limit (V1 default: 2 completed identical cycles) ends
the route with the unique typed terminal status
`TASKSPEC_RECONCILIATION_NONPROGRESS`. Starting a fresh PlanningEpoch does not
reset the ledger and cannot evade the guard. This status is not a top-level
error taxonomy class, candidate invalidity, user ambiguity, or storage
corruption.

Only Consultant may establish a genuine user-owned ambiguity through its
typed ownership/requiredness contract. In that case `ask_user` is allowed and
the question/evidence lineage is persisted. Controller never converts
reconciliation non-progress into `ask_user` automatically.

## 12. Human Plan Review and Approval

### 12.1 Separate immutable objects

`PlanSnapshot` contains:

- snapshot ID and schema version;
- exact TaskSpec semantic identity/revision used for planning;
- immutable steps and explicit inputs/outputs/dependencies;
- resolved authoritative artifact references;
- plan-local allocation mapping;
- capability/renderer contract bindings;
- success criteria and approval projection;
- TaskSpec-to-Plan typed traceability projection;
- deterministic validation identity/result reference;
- parent snapshot/revision relation.

It MUST NOT contain mutable execution progress or a mutable approval status.

`PlanApprovalRecord` contains:

```json
{
  "plan_snapshot_id": "plan-snapshot-...",
  "status": "pending",
  "decision_source": "human_plan_review",
  "review_event_id": null,
  "approved_task_revision": null,
  "created_at": "...",
  "decided_at": null
}
```

Allowed status values:

- `pending`;
- `approved`;
- `changes_requested`;
- `rejected`;
- `superseded`.

Decision events create a new immutable approval record or immutable event plus
new current record; they do not mutate the PlanSnapshot.

### 12.2 Execution eligibility

Generate/Execute is eligible only when all are true:

- current Plan pointer identifies the reviewed PlanSnapshot;
- deterministic validation is valid for that exact snapshot;
- current TaskSpec semantic identity/revision matches the snapshot;
- current artifact binding revisions satisfy the snapshot;
- current PlanApprovalRecord refers to the same snapshot;
- approval status is `approved`;
- approval scope and execution contract bindings are current;
- RuntimeState has no terminal/stale gate.

Pending, changes-requested, rejected, superseded, missing, stale, or
unverifiable approval blocks Generate and Execute.

### 12.3 Feedback routing

Every Human Review response is appended first as durable `U(n)` evidence.
Routing never drops earlier turns.

If the review UI/API carries an explicit scope:

- `feedback_scope=plan` -> persist PlanApprovalRecord
  `changes_requested`; create durable PlannerFeedback; dispatch Planner in a
  new epoch without changing TaskSpec;
- `feedback_scope=task` -> persist `changes_requested`; dispatch Consultant;
  Consultant proposes typed TaskSpec operations; after commit dispatch Planner
  in a new epoch.

If free-text feedback has no explicit scope, Controller MUST NOT classify it
with keywords. Appending `U(n)` and routing Consultant for classification do
not themselves change the current PlanSnapshot, PlanApprovalRecord, approval
applicability, or current pointers. Consultant may return:

- TaskSpec mutation only;
- PlannerFeedback only;
- both;
- neither/clarification.

The typed classification is then applied:

- PlannerFeedback only -> create `changes_requested`, persist feedback, then
  Planner starts a new epoch;
- TaskSpec mutation only -> atomically commit the TaskSpec mutation and mark
  the prior approval `superseded` or `changes_requested` according to the
  decision event, then Planner starts a new epoch;
- both -> commit the TaskSpec revision and durable PlannerFeedback, supersede
  the prior approval, then Planner starts a new epoch from the new TaskSpec;
- neither/explanation request -> preserve PlanSnapshot, approval record, and
  pointers; answer or display the explanation and return to review without
  replanning.

No state invalidation may occur between durable `U(n)` append and typed
classification, except that Generate/Execute remain blocked while a pending
classification could alter a not-yet-approved review. Python keywords do not
select among these outcomes.

Plan example: “step-2 should run annotation directly, not execute
reproduce.sh” -> PlannerFeedback.

Task example: “the GTF path was wrong; use this other GTF” -> Consultant and a
TaskSpec revision.

### 12.4 TaskSpec-to-Plan typed traceability projection

Human Review receives an explicit projection built from typed IDs and
contracts, for example:

```text
Task required output O3 -> produced by Plan step-4
Task required input I2 -> consumed by Plan step-1
Task constraint C7 -> explicitly referenced by Plan step-2
Task confirmed decision D4 -> Plan binding X
```

Each edge records source ID, target step/binding ID, relation enum, and
validation status. The projection MUST NOT compare descriptions, filenames,
step titles, or other prose to guess coverage. Missing explicit traceability
is visible to Planner/Human Review or a typed deterministic reference check;
Controller does not repair it through NLP.

## 13. Error Taxonomy and Routing

The top-level taxonomy is closed:

### 13.1 PLANNER_CANDIDATE_INVALID

Owner: Planner candidate.

Route: bounded candidate repair in the same PlanningEpoch.

Examples: schema errors, unknown references, undeclared local refs, invalid
dependency graph, unavailable consumption, forbidden authoritative fields.

### 13.2 TASKSPEC_RECONCILIATION_REQUIRED

Owner: Task semantic reconciliation.

Route: Consultant. It is a valid Planner outcome, not an exception and not a
candidate repair.

### 13.3 STALE_PLANNER_INVOCATION

Owner: concurrency/state transition.

Route:

- semantic-base stale -> discard the full response, end the old epoch, and
  start a child epoch from the new semantic base;
- non-semantic stale -> discard the full response, refresh the binding, and
  optionally redispatch in the same epoch within stale and total budgets.

Never interpret the outcome payload, route reconciliation, or ask Planner to
edit a stale candidate.

### 13.4 INTERNAL_CONTROLLER_INVARIANT_ERROR

Owner: authoritative state/controller contract.

Examples: committed binding missing, duplicate authoritative identity,
impossible producer index after a valid commit, PlanSnapshot mapping
inconsistent with artifact registry.

Route: internal block/diagnostic. Never Planner repair.

### 13.5 INTERNAL_CONTROLLER_IMPLEMENTATION_ERROR

Owner: implementation defect.

Examples: unexpected exception, wrong resolver call contract, parser bug,
non-exhaustive union handling, allocation code reaching a supposedly
prevalidated candidate error.

Route: internal error handling with traceback/correlation evidence. Never
Planner repair and never user ambiguity.

### 13.6 TRANSIENT_SYSTEM_ERROR

Owner: provider/storage/runtime availability.

Route: shared bounded transient policy when retry-safe; otherwise truthful
internal/provider block. It does not consume candidate-repair count.

This class is available only when the relevant adapter returns a registered
typed error/code together with retry-safety metadata. Exception-message,
substring, regex, filename, provider prose, and generic `OSError` matching do
not establish this class. At every I/O boundary, Controller first classifies
the I/O result; only a successful read may proceed to authoritative invariant
validation. An unregistered exception is
`INTERNAL_CONTROLLER_IMPLEMENTATION_ERROR`.

### 13.7 Classification precedence

For each Planner response, apply this order exactly:

1. parse only the top-level outcome envelope, without outcome side effects;
2. read the invocation binding and current authoritative state through typed
   I/O adapters; classify an I/O failure before attempting invariants;
3. after successful reads, verify binding/state integrity and evaluate
   invocation staleness;
4. if stale, discard the entire response using the semantic/non-semantic
   route in 13.3; do not interpret the outcome body;
5. if current and reconciliation, validate its typed body and apply the
   convergence guard before persisting/routing it;
6. if current and Plan Candidate, parse/resolve it, validate candidate-owned
   invariants, and prepare allocation;
7. at every later I/O boundary, repeat the typed I/O-first rule, then validate
   the successfully read state and recheck CAS/staleness before commit.

A known candidate error MUST NOT escape the graph as an uncaught
`SemanticHandleError`. Conversely, an internal invariant or implementation
failure MUST NOT be wrapped as `PLANNER_CANDIDATE_INVALID`.

### 13.8 Reconciliation non-progress terminal status

`TASKSPEC_RECONCILIATION_NONPROGRESS` is the single normative expression for
the cross-epoch convergence stop defined in Section 11.1. It is a typed
terminal planning status outside the six-class error taxonomy. It MUST NOT
also be emitted as a seventh error class, candidate invalidity, Consultant
ambiguity, or transient failure, and MUST NOT auto-route to `ask_user`.

## 14. Prompt and Context Contracts

### 14.1 Planner

Planner sees:

- canonical TaskSpec semantic projection;
- all active hard/required constraints needed for planning;
- artifact `handle + semantic_view`;
- registered typed artifact-contract options;
- selected capability/renderer semantic contracts;
- prior PlanSnapshot summary when replanning;
- durable PlannerFeedback, if any;
- Plan Candidate and reconciliation outcome schemas.

Planner does not see or return:

- canonical paths hidden by managed policy;
- logical IDs, binding revisions, version IDs;
- state generations, CAS tokens, hashes, object refs;
- approval IDs or mutable runtime state;
- `candidate_scope_id` or any other Controller candidate-lineage identifier;
- raw full audit history.

### 14.2 Candidate repair

Candidate repair sees the same relevant TaskSpec/artifact semantic closure as
initial Planner, plus:

- rejected candidate or allowed PlanPatch scope;
- exact field paths and stable error codes;
- authoritative-handle catalog and candidate-local declarations;
- explicit statement that step fields define consume/produce.

Controller reattaches the same model-invisible `candidate_scope_id` for an
allowed repair of the same lineage. A full replacement dispatch receives a
new scope; neither scope is included in the repair prompt or response schema.

Diagnostics are fully persisted. The LLM view may group repeated diagnostics,
but MUST preserve every unique error class, field path, artifact ref, and
affected step. It MUST NOT tail-truncate the semantic closure.

### 14.3 Consultant reconciliation

Consultant sees:

- typed reconciliation facts from Planner;
- canonical TaskSpec;
- relevant durable user turns/evidence;
- available read-only evidence operations;
- explicit rule to ask only for genuinely missing user-owned information.

For reconciliation, Consultant also sees the structured fingerprint payload,
relevant evidence revision/excerpts, previous typed Consultant outcome, and
non-progress count. It does not receive unrelated audit text merely to change
the fingerprint.

Consultant owns TaskSpec mutation. Controller only validates typed operations
and CAS-commits them.

### 14.4 Generate

Generate sees:

- one committed, approved current-step closure;
- explicit step inputs and outputs;
- dependency-produced artifact references selected from dependency step
  outputs, not handle prefixes;
- semantic views and action-level managed placeholders;
- capability/renderer action contract;
- relevant runtime evidence.

Generate cannot add a plan-local artifact, change PlanSnapshot, or reinterpret
an unapproved plan.

### 14.5 Recovery and Completion

Recovery sees the committed step/action/failure contract and relevant evidence
without re-deriving usage from handle spelling. A Plan-binding defect routes
to a new Planner epoch; a Task semantic gap routes to Consultant; action/runtime
failures follow their typed owners.

CompletionGate consumes committed output contracts, provenance, materialized
versions, validation evidence, and approval lineage. Task-boundary output
classification may be used to determine required final outputs, but not to
infer which Plan step produced or consumed them.

### 14.6 Human Plan Review

Human Plan Review is not an LLM node in this contract. It receives a
deterministic rendering of the immutable PlanSnapshot, validation summary,
artifact mapping, typed TaskSpec-to-Plan traceability projection, and approval
impact. The projection is assembled from canonical IDs, relation enums, and
typed contracts; no prose similarity or filename/keyword matching is used.

## 15. Persistence and Audit

Persist, with bounded projections where appropriate:

- PlanningEpoch and counters;
- parent epoch, semantic base identities, and stale classification;
- Controller-owned candidate scope and candidate-lineage binding;
- Planner invocation/outcome record;
- raw Planner response ref/hash;
- rejected PlanCandidate ref/hash;
- complete candidate diagnostics;
- repair response and resulting candidate;
- TaskSpec reconciliation request and Consultant outcome;
- structured ReconciliationFingerprint, relevant-evidence revision, and
  durable cross-epoch non-progress ledger;
- AllocationIntent or provisional reservation;
- local-to-authoritative allocation mapping;
- immutable PlanSnapshot ref with `plan_contract_version` and
  `minimum_reader_version`;
- root Plan-pointer envelope repeating the contract/minimum-reader versions;
- typed TaskSpec-to-Plan traceability projection;
- separate pending/decision PlanApprovalRecord ref;
- current Plan and current approval pointers as one Plan commit unit;
- Human Review event and durable `U(n)`;
- pending unscoped-review classification and its typed result;
- staleness/CAS decision;
- internal/transient failure evidence.

Rejected candidates MUST NOT write:

- current Plan pointer;
- authoritative artifact catalog;
- canonical paths;
- approval state;
- RuntimeState progress;
- TaskSpec.

Audit data is not a default prompt payload. Nodes receive only the semantic
facts and evidence excerpts required by their contract.

The authoritative Plan commit record groups the promoted artifact mappings,
PlanSnapshot, pending ApprovalRecord, both current pointers, audit index/event
set, and exactly one generation transition. Content-addressed objects written
before pointer promotion remain unreachable provisional evidence. Recovery
may reap or reuse them idempotently, but MUST NOT interpret any member as
committed independently.

Before reading or migrating a PlanSnapshot, the reader validates
`minimum_reader_version`. An unsupported future record is retained unchanged
and fails closed; it is never normalized into the old schema, used for
approval, or rewritten by an older process.

Writer capability/lease evidence proves the reader-first rollout barrier was
satisfied before the first new root pointer promotion. A process lacking that
capability cannot acquire the Plan-state writer lease.

The following persistence fields from 1.1 are removed with no migration into
the active 1.2.1 state: `semantic_review_calls`, `semantic_repairs`, reviewer
response records, and reviewer retry/convergence state. Old audit records may
remain readable as historical evidence but cannot affect 1.2.1 routing or
gates.

## 16. Workstream P Implementation Map

Implementation MUST be split into reviewable stages. Exact function names may
change, but authority boundaries may not.

### P0. Lock audit fixtures

Add characterization tests for:

- `resolve_plan_proposal_handles()` direction mismatch;
- Generate/Recovery dependency closure prefix filtering;
- managed artifact prefix/direction checks;
- Plan Review free-text route;
- approval snapshot/current Plan storage;
- prompt-only TaskSpec reconciliation;
- existing intermediate artifact/provenance behavior.

No behavioral change in P0.

### P1. Introduce identity and proposal types

Primary files: `agent_core/node_context.py`,
`agent_core/managed_artifacts.py`, `agent_core/plan_tracker.py`.

Add:

- `AuthoritativeArtifactRefKey`;
- `CandidateLocalArtifactRefKey`;
- parser/resolver returning the closed `ArtifactRefKey` union;
- `semantic_view` projection;
- `ProposedArtifact` and typed `artifact_contract`;
- Controller-owned candidate-scope identity and invocation registry;
- typed `availability_origin` and the V1 no-in-place rule.

Reuse existing managed-artifact and provenance stores.

### P2. Extend Planner outcomes and bounded candidate repair

Primary files: `prompts/manager.py`, `bio_agents.py`,
`agent_core/plan_tracker.py`.

Add a top-level outcome parser before strict Plan parsing:

- Plan Candidate;
- `TASKSPEC_RECONCILIATION_REQUIRED`.

The parser reads only the discriminator. Invocation staleness then runs before
strict body parsing or routing. Add semantic/non-semantic stale transitions,
PlanningEpoch totals, structured reconciliation fingerprints, and the durable
cross-epoch non-progress ledger. Wrap all known candidate
parser/resolver/validator errors as `PLANNER_CANDIDATE_INVALID` while keeping
internal and typed transient errors distinguishable.

### P3. Replace prefix-based Plan validation

Primary files: `agent_core/node_context.py`,
`agent_core/managed_artifacts.py`, `agent_core/plan_tracker.py`,
`bio_agents.py`.

Change:

- Plan resolver to use the containing field for usage;
- dependency closure to read explicit dependency outputs;
- managed Plan validation to resolve identity first;
- availability and producer maps to use `ArtifactRefKey`;
- already-available Task/resume artifacts to reject a current-Plan producer
  with `PLAN_ARTIFACT_ORIGIN_CONFLICT`;
- skill/workflow checks to use typed IDs/contracts instead of rendered-text
  presence;
- free-text purpose/filename inference to migration-only or non-authoritative
  display behavior.

Action-level placeholder syntax may remain behind a typed adapter.

### P4. Add prepare/allocation transaction

Extend the existing state store/single-writer CAS boundary:

- compute AllocationIntent without side effects;
- recheck current Task/Plan/artifact generation;
- allocate/promote plan-local artifacts, create PlanSnapshot and pending
  PlanApprovalRecord, promote both pointers/audit index, and advance generation
  once as one atomic Plan commit;
- otherwise implement provisional reservation, TTL, release, and idempotent
  promotion for that entire unit.

### P5. Separate PlanSnapshot and PlanApprovalRecord

Persist PlanSnapshot as an immutable object and make current Plan a pointer.
Persist pending/decision ApprovalRecords separately. Migrate approval gate
lookups to exact snapshot identity while preserving old ApprovalSnapshot
readability. Add `plan_contract_version`/`minimum_reader_version`, perform
root-envelope reader compatibility checks before migration, fence incompatible
writers, and fail closed for future records.

### P6. Route Human Review feedback and TaskSpec reconciliation

Add explicit feedback scope to interactive/API review payloads. Preserve
Consultant classification for unscoped text. Every feedback route appends
durable evidence first. An unscoped append/classification alone preserves the
snapshot, approval, and pointers; only typed Task mutation or PlannerFeedback
invalidates/routes replanning. Explanation-only text returns to review.

Implement Consultant routing for typed Planner reconciliation outcomes,
including evidence-first resolution and genuine ask_user behavior.

### P7. Migrate downstream consumers

Update Generate, Recovery, managed action materialization, Renderer bindings,
Supervisor, and CompletionGate projections to consume committed explicit
artifact usage and snapshot identity.

Human Plan Review receives the typed TaskSpec-to-Plan traceability projection,
not a natural-language semantic diff.

Remove all Plan step-usage dependence on handle prefix. Keep legacy aliases
only in a documented adapter with telemetry.

### P8. Remove obsolete 1.1 subsystem artifacts

Delete unused model-reviewer prompt/node/parser/state/counters/tests if any
appear during implementation. Do not delete Consultant constraint-repair code
merely because its name contains “semantic repair”.

Replace generic exception-message transient detection at Planner/storage I/O
boundaries with the shared closed typed adapter policy. Unknown exceptions
remain implementation errors.

## 17. Workstream T: Independent TaskSpec Coherence

Workstream T is separately implemented, tested, committed, and reversible.

### T1. Output path closure

Canonical TaskSpec MUST preserve:

- user-selected output directory;
- required output relative names or canonical paths;
- path source/provenance;
- output ownership and managed policy.

Planner/Generate projections refer to handles; Controller materializes the
canonical binding.

### T2. Required input coherence

An input named in the durable user request and resolved to a canonical
artifact MUST not be weakened or dropped by later summary/projection
construction. Requiredness is a typed TaskSpec property, not inferred from
filename or latest message.

### T3. Ambiguity coherence

`needs_clarification`, ambiguity rows, and confirmed decisions MUST agree.
Previously resolved durable facts remain available across ask/resume turns.
Only a contradictory/new user turn or invalidated evidence reopens the
ambiguity.

### T4. Independence rule

P-only acceptance uses a fixture with a coherent TaskSpec and reproduces the
Plan handle crash. T-only acceptance validates TaskSpec state without relying
on Planner changes. A combined fixture verifies integration but is not the
sole proof for either workstream.

## 18. Test Specification

### 18.1 Static and contract tests

- no Plan resolver call supplies expected direction based on
  `step.inputs/outputs`;
- no Generate/Recovery closure uses `startswith("input.")` or
  `startswith("output.")`;
- no candidate routing uses filename, role, description, title, or keyword
  heuristics for scientific acceptance;
- envelope parsing is side-effect-free and precedes outcome-body parsing;
- Planner wire schemas reject `candidate_scope_id`;
- no transient classifier inspects exception-message text at migrated I/O
  boundaries;
- model-reviewer node, prompt, counters, and error type from 1.1 are absent;
- Consultant semantic-constraint repair remains intact.

### 18.2 Artifact identity and flow

- the same handle may be produced in one step and consumed in a later step;
- an authoritative initial artifact consumed without a producer is valid when
  canonical state marks it available;
- a produced artifact consumed before an unreachable producer is invalid;
- missing dependency, cycle, duplicate producer, and unavailable artifact
  yield stable candidate diagnostics;
- handle spelling changes do not change flow behavior;
- semantic_view changes alone do not cause Controller to invent graph edges;
- a verified Task input listed as a current-Plan output yields
  `PLAN_ARTIFACT_ORIGIN_CONFLICT`;
- transforming an available artifact into a distinct output identity is valid.

### 18.3 Plan-local artifacts

- valid local file and directory contracts prepare and commit;
- declaration may appear after references in JSON serialization;
- undeclared/duplicate local refs fail before allocation;
- Planner-supplied ID/path/hash/revision fields fail as candidate invalid;
- allocation maps every local key exactly once;
- allowed repair keeps the same hidden candidate scope and local-key identity;
- a full replacement/new epoch gets a new hidden candidate scope;
- a spoofed model-supplied candidate scope is rejected;
- rejected candidates leave no artifact/path/pointer;
- CAS failure after preparation leaves no promoted orphan;
- provisional reservations expire/release and cannot be executed;
- retry/idempotent commit does not allocate a second artifact.

### 18.4 Error boundary

- known `SemanticHandleError`-class candidate defects become
  `PLANNER_CANDIDATE_INVALID` and do not escape LangGraph;
- stale candidate is not repaired;
- stale reconciliation is discarded before Consultant/audit/routing effects;
- authoritative registry inconsistency is internal invariant error;
- unexpected resolver/allocation exception is implementation error;
- a registered provider/storage timeout follows shared typed transient policy;
- an exception whose message merely contains “timeout” is an implementation
  error unless its adapter emits the registered typed transient code;
- a successful read followed by missing committed state is invariant error,
  not transient;
- internal errors are never rendered as Planner feedback or ask_user.

### 18.5 PlanningEpoch

- initial plus candidate repairs monotonically increments totals;
- candidate repair does not create a new epoch;
- PlanSnapshot commit ends the epoch;
- both kinds of Human Review feedback start a new epoch;
- stale redispatch consumes total Planner budget;
- non-semantic stale may remain in the same epoch;
- semantic-base stale ends the epoch and starts a child with new base/scope;
- transient retries do not increment candidate repairs;
- exhaustion is terminal and preserves last diagnostics/counters.

### 18.6 Reconciliation and feedback

- typed reconciliation bypasses strict Plan parser and candidate repair;
- Consultant resolves from durable evidence without asking when possible;
- genuine missing user-owned fact asks once and preserves all prior turns;
- same unresolved reconciliation fingerprint at unchanged TaskSpec revision
  cannot loop Planner/Consultant indefinitely;
- a new epoch without TaskSpec semantic/relevant-evidence progress does not
  reset reconciliation convergence;
- the configured repeated cycle ends only as
  `TASKSPEC_RECONCILIATION_NONPROGRESS`, never automatic `ask_user`;
- explicit plan feedback reaches Planner without TaskSpec mutation;
- explicit task feedback reaches Consultant and increments TaskSpec revision
  only on semantic change;
- unscoped feedback is classified by Consultant, not Python keywords;
- unscoped explanation-only text preserves snapshot/approval/pointers;
- mixed feedback commits TaskSpec and PlannerFeedback coherently;
- no review invalidation occurs between `U(n)` append and typed
  classification.

### 18.7 Snapshot and approval

- PlanSnapshot is content-addressed/immutable;
- pending ApprovalRecord is separate but initialized in the same atomic Plan
  commit;
- snapshot, pending approval, both current pointers, promoted artifacts, audit
  index, and one generation transition commit or fail together;
- injected ApprovalRecord initialization failure exposes none of the new unit;
- pending/changes_requested/rejected/superseded block Generate;
- approval for a different snapshot is inapplicable;
- task, plan, artifact binding, or execution contract semantic change makes
  approval stale;
- runtime/audit-only changes do not rewrite PlanSnapshot or approval decision;
- review feedback never mutates an approved snapshot;
- an older reader fails closed on an unsupported `minimum_reader_version`
  before migration, approval evaluation, or writeback.

### 18.8 Downstream integration

- Generate current-step closure includes dependency outputs by explicit field;
- Recovery receives the same artifact identity after failure;
- managed placeholders resolve only within the committed step's allowed
  operation set;
- CompletionGate verifies the materialized artifact version and lineage;
- legacy `input.*`/`output.*` aliases behave identically to opaque aliases;
- Human Review traceability uses typed source/target IDs and relation enums,
  and does not derive coverage from prose.

### 18.9 Incident fixtures

P-only fixture:

- coherent TaskSpec;
- step 1 outputs `output.reproduce_script`;
- step 2 inputs the same handle and depends on step 1;
- candidate reaches pending Human Review without a direction mismatch.

P+T fixture:

- original VCF/FASTA/GTF and output path persist through Consultant/user turns;
- Planner uses the explicit artifact fields;
- Plan commits pending approval;
- approval gates Generate;
- no repeated question is caused by lost historical state.

## 19. State-machine Proof

The following traces are normative proof obligations. An implementation that
reaches a different authoritative state does not conform, even if it returns
a superficially helpful user message.

### Case A: produced alias consumed by a later step

Given:

```text
step-1.outputs = ["output.reproduce_script"]
step-2.inputs  = ["output.reproduce_script"]
step-2.dependencies = ["step-1"]
```

Controller resolves both occurrences to one `ArtifactRefKey`. It records
`produce(step-1, ref)` from the containing outputs field and
`consume(step-2, ref)` from the containing inputs field. The DAG proves
step-1 reachable before step-2. No `direction` or `output.*` prefix check is
performed. With all remaining deterministic invariants valid, the atomic Plan
commit creates the snapshot and pending approval together. The route ends at
pending Human Plan Review; `SemanticHandleError`/direction mismatch is not a
legal result.

### Case B: stale reconciliation after TaskSpec semantic change

Planner invocation `P1` is bound to TaskSpec semantic identity `T2`. While it
runs, Consultant commits `T3`. `P1` returns a syntactically recognizable
`TASKSPEC_RECONCILIATION_REQUIRED` envelope. Controller parses only that
discriminator, successfully reads current state, and classifies `P1` as
semantic-base stale before validating or persisting the reconciliation body.
It discards the whole response, creates no Consultant/user/audit-semantic side
effect, ends the old epoch, and starts a child epoch bound to `T3` with a new
candidate scope.

### Case C: repeated reconciliation without progress

Cycle one produces fingerprint `F` at TaskSpec semantic identity `T4` and
relevant evidence revision `E9`. Consultant returns typed
`no_semantic_change`; the ledger records one completed non-progress cycle.
The next Planner/Consultant route, even in a new epoch, produces the same `F`,
same `T4`, same `E9`, and no genuine user-owned ambiguity. At the V1 limit of
two completed identical cycles, Controller emits only the terminal status
`TASKSPEC_RECONCILIATION_NONPROGRESS`, persists the ledger, and stops. It does
not issue candidate repair, start another epoch to reset the counter, or
convert the condition into `ask_user`.

### Case D: AllocationIntent followed by CAS stale

Controller computes AllocationIntent `I` without publishing IDs, paths, or
pointers. The pre-commit re-read shows a stale CAS/base. The commit is not
entered. `I` is discarded, or its complete provisional reservation is
released/expired. The old generation and current pointers remain unchanged;
no authoritative artifact, binding, path, snapshot, approval, or audit-index
promotion is visible. Semantic-base staleness starts a child epoch;
non-semantic staleness may use bounded same-epoch redispatch.

### Case E: ApprovalRecord initialization fails during Plan commit

The new PlanSnapshot object may have been physically prewritten, but it is
unreachable provisional content. If pending ApprovalRecord initialization or
its storage adapter fails before the single promotion point, a real
transaction rolls back the complete unit; the fallback protocol leaves the
reservation unpromoted and later releases/reaps it. Neither current pointer is
advanced and generation is unchanged. Therefore no authoritative state can
contain the new current PlanSnapshot without its matching pending approval.
The storage failure is transient only when its adapter emits a registered
retry-safe typed error; otherwise it is an implementation error.

### Case F: unscoped explanation question in Human Review

The user asks “为什么这里需要两步？”. Controller first appends the exact turn
as durable `U(n)` and routes it to the existing Consultant classification
protocol. It does not set `changes_requested`, supersede approval, clear the
snapshot, or move either pointer before classification. If Consultant returns
neither TaskSpec mutation nor PlannerFeedback, the system answers/displays the
explanation and returns to the same review state. Replanning occurs only if a
typed result actually contains Task mutation or PlannerFeedback.

### Case G: verified input reused as a current-Plan output

Artifact `A` is available with `availability_origin=verified_task_input`.
Planner lists `A` in a current step's outputs. Explicit producer construction
therefore conflicts with the origin contract and deterministic validation
returns `PLAN_ARTIFACT_ORIGIN_CONFLICT` as candidate invalid before allocation.
The bounded repair asks Planner to declare a distinct output identity `B` for
the transformation. No in-place version/path is allocated and no state is
committed for the rejected candidate.

## 20. Migration Milestones

Milestones are ordered and independently reviewable. Each milestone has its
own commits, tests, acceptance evidence, and rollback switch. A later
milestone may depend on an earlier schema, but it MUST NOT rewrite the earlier
commit history or hide Workstream T changes inside Workstream P.

### 20.1 Milestone A: Incident Closure

Scope:

- characterize all current `direction`/`allowed_usage` and Plan prefix
  consumers;
- make Plan step usage derive from `step.inputs`/`step.outputs`;
- remove direction mismatch from `resolve_plan_proposal_handles()`;
- migrate the Generate/Recovery step closure and managed Plan validator for
  the incident path to explicit producer/dependency relations;
- keep legacy aliases as opaque identities;
- contain known candidate resolver/validation errors inside the existing
  bounded Planner path rather than allowing an uncaught crash.

Tests:

- characterization and static prefix-consumer tests;
- Case A P-only incident regression with coherent TaskSpec;
- opaque-handle spelling equivalence;
- missing ref/cycle/unavailable producer stays a stable candidate diagnostic;
- Generate and Recovery select the dependency artifact by explicit relation.

Acceptance:

- the exact `output.reproduce_script` producer/consumer fixture reaches a
  deterministically valid Plan and pending review through the then-current
  storage path;
- no allowlist special case for `output.* in inputs` exists;
- no new TaskSpec behavior, reviewer, or plan-local allocation is activated.

Rollback:

- revert only Milestone A behavior commits behind the resolver/closure feature
  flag while retaining characterization tests and audit evidence;
- rollback restores the pre-A resolver but does not mutate existing sessions
  or introduce a semantic reviewer.

Commit boundary:

1. A1 characterization/static tests only;
2. A2 explicit step-usage resolver and validator;
3. A3 downstream incident closure plus bounded error adapter;
4. A4 incident replay evidence.

### 20.2 Milestone B: Planning Hardening

Scope:

- add the closed `ArtifactRefKey` union, semantic views, typed plan-local
  declarations, hidden candidate scopes, and AllocationIntent preparation;
- add the top-level envelope parser and enforce staleness before outcome
  handling;
- add semantic/non-semantic stale epoch transitions;
- add reconciliation fingerprints and the durable cross-epoch non-progress
  guard;
- add typed `availability_origin` and V1 no-in-place validation;
- migrate Planner/provider/storage boundaries in scope to the typed transient
  policy;
- generate typed TaskSpec-to-Plan traceability.

Until Milestone C activates the complete atomic Plan commit, plan-local
promotion MUST remain shadow-only/disabled. B may validate and record an
AllocationIntent, but cannot expose a plan-local authoritative artifact via a
partial legacy write.

Tests:

- Cases B, C, and G;
- stale malformed/reconciliation envelopes have no semantic side effects;
- candidate-scope spoofing, repair inheritance, and full-replacement rotation;
- typed transient versus message-lookalike exception classification;
- successful-read invariant failures are not transient;
- traceability edges depend only on typed IDs/relation enums.

Acceptance:

- all recognized Planner outcomes pass the staleness gate before routing;
- semantic stale starts a child epoch and non-semantic stale stays bounded;
- repeated reconciliation terminates under the one named status;
- the V1 origin invariant rejects in-place output;
- candidate repair and transient budgets remain separate;
- no model-visible candidate scope or NLP semantic diff exists.

Rollback:

- disable B routing/types and return to the accepted A path;
- B shadow AllocationIntents/responses are retained only as unreachable audit
  evidence and cannot be promoted after rollback;
- convergence and epoch records are append-only evidence, not interpreted by
  the A reader.

Commit boundary:

1. B1 typed refs, scopes, origins, and shadow validators;
2. B2 envelope/staleness/epoch routing;
3. B3 reconciliation convergence ledger;
4. B4 typed I/O policy migration;
5. B5 traceability projection and B acceptance fixtures.

### 20.3 Milestone C: State Model Cleanup

Scope:

- persist immutable PlanSnapshot and separate PlanApprovalRecord objects;
- implement the complete atomic Plan commit or complete provisional protocol,
  including artifact promotion, pending approval, pointers, audit index, and
  one generation advance;
- add `plan_contract_version` and `minimum_reader_version` with fail-closed
  root-envelope reader behavior and a reader-first writer-fencing barrier;
- activate plan-local artifact promotion;
- migrate Generate/Recovery/Supervisor/CompletionGate and approval eligibility
  to exact committed snapshot/artifact identity;
- change unscoped Human Review to classification-first behavior;
- remove obsolete compatibility writes and any 1.1 reviewer remnants after
  legacy-read fixtures pass.

Tests:

- Cases D, E, and F;
- transaction fault injection at every promotion member and pointer boundary;
- idempotent provisional retry/reaper and no orphan authority;
- old-reader/new-snapshot rejection before migration/writeback;
- incompatible-process writer-lease denial before new-pointer activation;
- pending approval exact-snapshot gating;
- explanation-only, PlannerFeedback-only, Task mutation-only, and both review
  outcomes;
- full Section 18 and P+T integration fixtures.

Acceptance:

- snapshot, pending approval, artifact mapping, pointers, audit index, and
  generation are all-or-none;
- unsupported readers fail closed without mutation;
- the first 1.2.1 pointer is not promoted until every active writer satisfies
  the reader capability barrier;
- plan-local artifacts become executable only after the complete commit;
- unscoped explanation text preserves review state;
- Human Plan Review remains the sole semantic approval gate;
- all active downstream Plan consumers are prefix-independent.

Rollback:

- stop new C writes, expire/release all unpromoted reservations, and pin the
  writer to a compatible version;
- a legacy reader may resume only from a proven compatible legacy pointer; it
  must invalidate approval when exact semantic equivalence cannot be proven;
- committed content-addressed snapshots, approvals, audit events, and artifact
  versions remain append-only and are not deleted;
- rollback never promotes a C reservation with an older writer and never
  restores a model-based semantic reviewer.

Commit boundary:

1. C1 immutable object schemas and fail-closed readers;
2. C2 atomic/provisional Plan commit with fault injection;
3. C3 exact approval and downstream snapshot consumers;
4. C4 classification-first Human Review;
5. C5 compatibility cleanup and end-to-end acceptance evidence.

### 20.4 Independent Workstream T commits

Workstream T retains separate commits for output paths, required inputs,
ambiguity/decision coherence, and history-preserving tests. It has its own
feature/rollback boundary and acceptance evidence. No A/B/C commit may alter
TaskSpec output or required-input semantics merely to make an incident replay
pass; only the final combined fixture depends on both workstreams.

## 21. Global Rollback Invariants

- A rollback changes active readers/writers/routes, not immutable historical
  evidence.
- An unsupported reader always fails closed; rollback is not permission to
  reinterpret a future snapshot.
- No reservation is promoted across an implementation-version boundary unless
  the promoting writer proves full contract compatibility.
- No rollback can make an approval apply to a different snapshot or TaskSpec
  semantic identity.
- Workstream T can roll back independently of P.
- No rollback restores a model-based Plan semantic reviewer as a hidden
  fallback.

## 22. Acceptance Criteria

Workstream P is accepted only when:

1. Plan step usage is determined exclusively by explicit step fields.
2. Artifact handles resolve by identity without prefix direction checks.
3. Planner sees `semantic_view` for every relevant artifact, but no
   Controller-owned candidate scope.
4. Every Planner response passes envelope-only parsing and staleness before
   outcome effects.
5. Semantic and non-semantic stale routes obey their epoch contracts.
6. Valid plan-local artifacts can be proposed and atomically promoted.
7. No rejected/stale/internal-failure candidate creates an authoritative
   artifact, path, PlanSnapshot, ApprovalRecord, pointer, or generation move.
8. Snapshot, pending approval, promoted artifacts, pointers, audit index, and
   one generation transition form one Plan commit.
9. Candidate-local schema defects are detected before allocation; allowed
   repairs preserve their hidden scope.
10. Planner candidate errors are bounded and remain inside LangGraph.
11. Stale, invariant, implementation, and typed transient failures retain
    distinct routes and budgets without exception-message matching.
12. Typed TaskSpec reconciliation routes to Consultant only after staleness
    and stops under the cross-epoch convergence guard.
13. `TASKSPEC_RECONCILIATION_NONPROGRESS` is only a terminal status outside the
    six top-level error classes and never auto-asks the user.
14. V1 rejects a current producer for verified/admitted available artifacts.
15. PlanSnapshot commit ends the PlanningEpoch.
16. Human Plan Review is the sole execution-before-semantic-approval gate.
17. PlanSnapshot and PlanApprovalRecord are separate immutable records with
    exact snapshot applicability.
18. New snapshots carry contract/minimum-reader versions and unsupported
    readers fail closed before migration or writeback.
19. Every review feedback route first appends durable `U(n)`; unscoped text
    invalidates only after typed classification warrants it.
20. Human Review uses typed traceability, not an NLP semantic diff.
21. Generate, Recovery, managed artifacts, Supervisor, and CompletionGate no
    longer infer Plan usage from handle spelling.
22. 1.1 model-review prompts, routes, counters, errors, persistence, and tests
    are absent from the active design/implementation.
23. Cases A-G and the milestone-specific acceptance suites pass.
24. Only an LLM typed request-intent decision can admit new managed execution;
    Controller cannot upgrade prose or a TaskSpec into executable intent.
25. Question/advisory-plan, invalid, incoherent, and low-confidence intent
    decisions cannot create the first executable canonical TaskSpec.

Workstream T is accepted separately when output paths, required inputs,
ambiguities, confirmed decisions, and earlier user turns remain coherent
through Consultant/ask/resume/replan cycles.

## 23. Definition of Done

This specification is implemented only when all of the following are true:

- the target state machine is represented by typed outcomes and durable state;
- authority boundaries match the table in Section 6;
- managed execution admission is LLM-semantic and typed, while Controller is
  validation-only and fails closed when explicit managed intent is absent;
- `ArtifactRefKey` is used before allocation and no semantic prefix branching
  remains in Plan flow;
- `semantic_view` and `artifact_contract` are separate;
- the top-level envelope parser is side-effect-free and staleness precedes all
  recognized Planner outcome effects;
- candidate scope is Controller-owned, model-invisible, and lifecycle-tested;
- allocation, PlanSnapshot, pending approval, pointers, audit index, and one
  generation transition are atomic or use the complete provisional protocol;
- V1 origin/no-in-place and typed I/O-first invariants are enforced;
- Planner candidate repair and PlanningEpoch limits are enforced;
- TaskSpec reconciliation is functional, cross-epoch bounded, and does not
  manufacture user ambiguity;
- Human Plan Review approval is required and snapshot-specific;
- feedback routing preserves all durable user turns and explanation-only text
  does not invalidate current review state;
- Human Review traceability contains only typed ID/contract relations;
- current/legacy state migrations are idempotent, minimum-reader guarded, and
  tested;
- all tests in Section 18 and proofs in Section 19 pass;
- audit evidence can reconstruct envelope/staleness, candidate scope,
  validation, allocation, atomic commit, approval, reconciliation convergence,
  and feedback decisions;
- Milestones A, B, and C each have acceptance/rollback evidence and their
  stated commit boundaries;
- Workstream P and Workstream T have independent commits and acceptance
  evidence;
- no code or session was changed merely by approving this specification.
