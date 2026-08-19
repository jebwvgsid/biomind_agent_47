# Skill Planning Contract Retirement Audit

## 1. Executive Conclusion

```text
SKILL_PLANNING_CONTRACTS_FULL_RETIREMENT = YES
```

Audit boundary: repository `Biomind_v1`, branch `codex/save-current-work`, base
HEAD `151a6e720ff510cd8a2ead0038a879d340059a46`, incident
`session_1787112642`.

`agent_core/skill_planning_contracts.py` has no irreplaceable
`CLASS A — STRUCTURAL_MACHINE_CONTRACT`. It contains one natural-language
activation helper and one same-skill/dependency heuristic. Its only diagnostic,
`PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED`, is not emitted from a broken declared
artifact edge. It is inferred from:

```text
skill document contains four English phrases
+ producer and consumer use the same selected skill
+ consumer depends on producer
+ producer.outputs and consumer.inputs do not intersect
```

That inference treats an ordering edge as evidence of an undeclared dataflow
edge. It is `CLASS C — SEMANTIC_OR_HEURISTIC_PLANNING_RULE` and conflicts with
the target invariant:

> Dependency edges establish execution ordering. Structured artifact
> references establish dataflow.

The existing typed candidate admission path independently rejects unknown
artifact references, unknown/cyclic dependencies, invalid producer cardinality,
unreachable producers, origin conflicts, and invalid plan-local materialization
contracts. Retiring this file therefore does not retire the authoritative
machine checks for declared dataflow.

This audit does **not** implement retirement. The module, import, caller, error
code, and tests remain unchanged in this turn.

## 2. Current Responsibility

The module claims to validate selected-skill planning contracts over structured
Plan fields. Its actual responsibility is narrower and heuristic:

1. Normalize the full loaded `SKILL.md` text.
2. Recognize a governed skill only when four literal English phrases occur.
3. Visit every dependency edge whose two endpoint steps name the governed
   skill.
4. Require every such edge to share at least one exact string between
   `producer.outputs` and `consumer.inputs`.
5. Emit a blocking Planner-owned issue when no shared string exists.

It does not validate Plan schema, step-ID uniqueness, dependency existence,
DAG acyclicity, artifact declaration existence, artifact kind/lifecycle,
producer cardinality, producer reachability, materialization policy, output
ownership, renderer binding, file existence, file readability, or runtime
availability.

The incident demonstrates the consequence. The real snpEff dataflow edge was
correct:

```text
step-3.outputs -> plan-artifact-db -> step-4.inputs
```

The module instead emitted three blockers for validation/ordering edges
`step-1 -> step-2`, `step-1 -> step-3`, and `step-2 -> step-3`.

## 3. Full Call Graph

Production call graph:

```text
BioAnalystAgent._build_graph()
  -> registered plan_review node
     -> load_plan_state()
     -> normalize_plan_contracts()
        (canonical structural/output normalization runs first)
     -> SkillManager.load_skill() for every selected skill
     -> validate_selected_skill_planning_contracts()
        -> _declares_cross_step_artifact_contract()
        -> optional PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED issues
     -> merge issues into persisted_plan.validation_issues
     -> bind_plan_to_task_revision()
     -> save_plan_state()
     -> if heuristic issues exist:
          required_next_action = PLAN_CONTRACT_REPAIR
          failure_owner = PLAN
          next_step = planner
     -> plan_review_router()
     -> Planner re-entry
```

The only production import and direct caller are in `bio_agents.py`:

- module import near the top-level agent-core imports;
- one invocation in `plan_review` after `normalize_plan_contracts` and before
  approval reuse, ambiguity handling, conflict handling, Plan rendering, and
  the human approval prompt.

The only direct non-production caller is
`tests/test_snpeff_plan_dataflow_contract.py`.

There are no calls from Generate, Execute, Recovery, middleware, renderer code,
or plan snapshot loading.

## 4. Function Inventory

| Function | Visibility | Inputs | Behavior | Classification | Future disposition |
| --- | --- | --- | --- | --- | --- |
| `_declares_cross_step_artifact_contract(document)` | private | raw loaded skill document | whitespace-normalizes/lowercases text and requires four literal English phrases | CLASS C | retire with file |
| `validate_selected_skill_planning_contracts(plan_state, selected_skill_documents)` | public/exported | structured steps plus raw skill documents | infers that every same-skill dependency edge requires shared output/input identity | CLASS C | retire with file |

`__all__` exports only `validate_selected_skill_planning_contracts`.

No function in the file reads the filesystem, resolves a TaskSpec artifact,
uses an authoritative artifact ledger, checks a PlanSnapshot, or calls an
existing structural validator.

## 5. Rule Classification

| Rule/branch | Evidence used | Class | Reason |
| --- | --- | --- | --- |
| Skill document phrase activation | natural-language headings/sentences | C | machine behavior depends on exact prose wording |
| `governed_skills` selection | skill identity plus prose activation | C | skill identity selects a semantic workflow expectation |
| Consumer belongs to governed skill | `consumer.skill_ids` | C | skill membership is used to infer required dataflow |
| Producer and consumer share governed skill | both endpoint `skill_ids` | C | same skill does not prove producer/consumer semantics |
| Iterate every declared dependency | `dependencies` | C | a dependency is ordering, not necessarily dataflow |
| Accept when output/input strings intersect | structured strings | C as used here | the intersection is machine-readable, but the obligation to have one on every same-skill edge is a semantic inference |
| Emit blocker otherwise | preceding inferred obligation | C | the error does not identify a malformed declared artifact contract |
| Unknown dependency silently skipped | missing `by_id` entry | no validation rule | authoritative DAG validation owns and reports this case |

Classification totals:

```text
CLASS A = 0
CLASS B = 0 standalone rules
CLASS C = all emitted behavior
CLASS D = 0
```

The module contains structured set operations, but none constitutes an
independent CLASS A rule. Removing the semantic activation/obligation removes
the only reason those set operations run.

## 6. Error-Code Inventory

| Error code | Current rule | Classification | Other authoritative validator? | Future recommendation |
| --- | --- | --- | --- | --- |
| `PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED` | same governed skill + dependency + no output/input string intersection | CLASS C | Yes, for actual declared-graph failures: `ARTIFACT_REFERENCE_UNKNOWN`, `PLAN_ARTIFACT_UNAVAILABLE`, `PLAN_ARTIFACT_PRODUCER_CARDINALITY`, `PLAN_ARTIFACT_ORIGIN_CONFLICT`, plus DAG and allocation diagnostics | make unreachable when module/caller retire; do not replace with a renamed heuristic |

Repository-wide search found exactly one emitter: the target module. The
`bio_agents.py` occurrence only removes old copies before merging the latest
issues; the test occurrence only asserts the diagnostic.

## 7. Structural Validator Overlap

Authoritative existing owners already cover declared machine structure:

| Existing owner | Function | Relevant diagnostics/responsibility |
| --- | --- | --- |
| `agent_core/plan_contract.py` | `structured_plan_candidate_diagnostics` | Plan wire/schema, step fields, checklist, metadata, proposed-artifact container shape |
| `agent_core/plan_contract.py` | `candidate_artifact_ref_keys` | declaration uniqueness and exact input/output reference resolution; `ARTIFACT_REFERENCE_UNKNOWN` |
| `agent_core/plan_contract.py` | `validate_candidate_graph_and_flow` | step IDs, unknown/self/cyclic dependencies, producer cardinality, origin conflict, producer reachability/availability |
| `agent_core/plan_contract.py` | `prepare_allocation_intent` | registered object kind, lifecycle, and materialization policy |
| `agent_core/plan_contract.py` | `promote_allocation_intent` / `rewrite_candidate_local_refs` | one controller-owned identity allocation and consistent producer/consumer rewrite |
| `agent_core/plan_tracker.py` | `normalize_plan_contracts` | shared pre-review canonical normalization and aggregation of structural/output diagnostics |
| `agent_core/plan_tracker.py` | `normalize_task_output_materializations` | exact TaskSpec output reference and safe materialization path contract; `TASK_OUTPUT_REFERENCE_UNKNOWN` among its diagnostics |
| `agent_core/plan_tracker.py` | `validate_output_contracts` | required output coverage and single output producer ownership |
| `agent_core/managed_artifacts.py` | `build_approved_visible_artifact_set` / `build_managed_artifact_bindings` | approved current-step artifact closure and runtime-valid plan-local input visibility |
| `agent_core/managed_artifacts.py` | `materialize_managed_action` / `validate_materialized_artifacts` | Generate-to-controller handle/path materialization integrity |

The target module is not a duplicate implementation of these rules. It is an
extra semantic obligation layered after them. That is why it can reject a Plan
whose declared artifact graph is already structurally valid.

## 8. Artifact/Dataflow Validation Ownership

The following read-only probes were executed against current HEAD without
changing repository or run artifacts:

| Broken declared case | Authoritative result without the skill heuristic |
| --- | --- |
| `step.inputs = [missing-ref]` and no declaration | `candidate_artifact_ref_keys` raises `ARTIFACT_REFERENCE_UNKNOWN` |
| declared plan-local artifact consumed with no producer | `validate_candidate_graph_and_flow` emits `PLAN_ARTIFACT_UNAVAILABLE` |
| declared artifact has two producers | `validate_candidate_graph_and_flow` emits `PLAN_ARTIFACT_PRODUCER_CARDINALITY` |
| dependency target missing and producer unreachable | emits both `PLAN_DEPENDENCY_UNKNOWN` and `PLAN_ARTIFACT_UNAVAILABLE` (covered by `test_plan_contract_v121`) |
| invalid materialization policy | `prepare_allocation_intent` raises `ARTIFACT_POLICY_UNREGISTERED` |
| producer consumes and produces the same protected identity | `PLAN_ARTIFACT_ORIGIN_CONFLICT` |

Therefore a truly broken **declared** artifact handoff remains blocked if the
target module is retired.

There is an intentional boundary: if Planner declares no artifact/dataflow at
all and only implies hidden state in prose or a future shell command, a
structural Plan validator cannot infer the missing semantic edge. Preventing
that requires Planner prompt/skill guidance, human Plan review, and later
structured Generate/action validation—not a Controller guess based on skill ID
or step title.

## 9. Input/File Validation Ownership

The target module performs no input/file validation:

- no `Path`, `os`, `stat`, `open`, suffix, basename, filename, or MIME logic;
- no required input-role inference;
- no existence/readability/non-empty check;
- no runtime executable or environment check.

Current ownership is elsewhere:

- Consultant/input admission produces `input_audit`; canonical input facts are
  merged into TaskSpec by `merge_task_spec_input_audit`.
- TaskSpec owns declared input identities and paths.
- Candidate admission checks that Planner references declared identities, not
  whether a skill-specific task “should” have a VCF/FASTA/GTF.
- Approved binding construction and Execute-time artifact/runtime provenance
  own current machine availability for generated/intermediate artifacts.
- The execution/action path remains allowed to reject a missing/unreadable
  current path or let the process fail with typed execution evidence if the
  machine changed after Consultant audit.

Retirement does not mean “Consultant once observed a file, therefore Execute
must trust it forever.” It only removes a Planning-stage semantic re-derivation.
A complete audit of every physical pre-execution restat is outside this file's
retirement boundary.

## 10. Planner and Plan Review Routing Impact

Current ordering in `plan_review` is:

```text
load persisted Plan
-> normalize_plan_contracts
-> run skill_planning_contracts heuristic
-> persist merged issues
-> early-return Planner when heuristic issues exist
-> otherwise evaluate canonical blocking issues
-> approval reuse / ambiguity / conflict handling
-> render Plan and ask human for approval
```

The early return prevents the human from seeing an otherwise structurally valid
Plan. In `session_1787112642`, it routed the pending candidate back to Planner,
where a separate snapshot-state bug attempted to load that candidate as
approved.

Future retirement should remove only the skill-document load, validator call,
special issue replacement, and heuristic early-return branch. Canonical
`review_issues` routing must remain unchanged.

## 11. Test Inventory

Direct target-module tests in `tests/test_snpeff_plan_dataflow_contract.py`:

| Test | Disposition | Reason |
| --- | --- | --- |
| `test_split_plan_without_shared_artifact_requests_planner_repair` | RETIRE | asserts the same-skill/dependency heuristic and its error code |
| `test_split_plan_reuses_one_allocated_directory_identity` | UPDATE | keep typed candidate flow/allocation/rewrite assertions; remove the final target-validator assertion/import |
| `test_composite_plan_needs_no_cross_step_artifact` | RETIRE | tests a semantic exception to the heuristic, not a machine contract |
| `test_planner_prompt_and_plan_review_show_explicit_dataflow_rules` | KEEP | verifies guidance and review visibility; it does not require Controller semantic inference |
| `test_skill_contract_uses_controller_runtime_variable` | KEEP | verifies skill/runtime documentation, unrelated to the retiring validator |

Existing replacement/owner tests:

| Test area | Disposition | Coverage retained |
| --- | --- | --- |
| `tests/test_plan_contract_v121.py::PlannerWireContractRegressionTests` | KEEP | wire/schema and artifact declaration admission |
| `tests/test_plan_contract_v121.py::PlanContractV121FlowTests` | KEEP | explicit dataflow, origin conflict, dependency unknown, producer reachability, allocation scope |
| `tests/test_state_contract_incident_replay_v2.py` | KEEP | semantic-handle resolution and controller materialization identity |
| `tests/test_plan_local_preaction_convergence_v2.py` | KEEP | approved plan-local producer/consumer visibility and runtime binding |
| pending-candidate Plan Review round-trip regression added for the separate Track B fix | KEEP | true structured `TASK_OUTPUT_REFERENCE_UNKNOWN` routes back to Planner without entering approved lifecycle |

Focused current-HEAD result used by this audit:

```text
17 tests passed:
- PlannerWireContractRegressionTests
- PlanContractV121FlowTests
- test_snpeff_plan_dataflow_contract
```

The direct heuristic tests still pass because retirement was intentionally not
implemented; their future disposition is the recommendation above.

## 12. Git Provenance

`git blame`, `git log --follow`, `git log -L`, `git log -S`, and `git log -G`
all converge on one introducing commit:

```text
53920e35e978356f58b96e61377decb2b076a1fb
fix(plan): require explicit snpeff intermediate artifact dataflow
2026-08-16 23:11:11 +0800
```

That commit atomically:

- created `agent_core/skill_planning_contracts.py`;
- added its Plan Review import/call/early return;
- added the snpEff `## Planning Contract` prose;
- added general Planner prompt guidance that dependencies are ordering and
  artifacts are dataflow;
- added the five snpEff dataflow tests;
- added a human-facing cross-step artifact review checklist.

The commit message and diff prove its purpose was to force explicit snpEff
intermediate dataflow and prevent shell-local state from crossing managed
actions. They do not name a specific session or prove that every dependency
edge in that workflow is a dataflow edge. The implementation is therefore best
characterized, with evidence, as a heuristic fallback added to prevent an LLM
from omitting an expected snpEff DB carrier—not as an authoritative generic
artifact validator.

No later commit changed either function or the emitted diagnostic before this
audit.

## 13. Whole-File Retirement Feasibility

Feasibility is high and the answer is **YES** because:

1. The module has only two functions and one public export.
2. It has one production caller.
3. It emits one error code.
4. It owns no CLASS A rule.
5. Actual declared-artifact corruption is rejected before Plan commit by typed
   candidate admission.
6. Committed Plan Review retains canonical structural/output validation after
   the heuristic branch is removed.

Expected future deletion set:

```text
agent_core/skill_planning_contracts.py
bio_agents.py import
bio_agents.py Plan Review skill-document load/call/merge/early return
direct heuristic assertions/imports in test_snpeff_plan_dataflow_contract.py
PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED references
```

No replacement validator framework is needed.

## 14. Functions/Branches to Retain

Within `agent_core/skill_planning_contracts.py`:

```text
NONE
```

Outside the file, retain:

- general Planner prompt guidance that dependency is ordering and explicit
  references are dataflow;
- human-facing cross-step artifact review text unless a separate UX decision
  removes it;
- all existing typed candidate/DAG/artifact/allocation validators;
- canonical `normalize_plan_contracts` and `blocking_review_issues` routing;
- Execute-time runtime/artifact validation.

## 15. Functions/Branches to Retire

On a later user-approved retirement implementation:

- retire `_declares_cross_step_artifact_contract`;
- retire `validate_selected_skill_planning_contracts`;
- remove its `__all__` export with the file;
- remove the `bio_agents.py` import;
- remove `review_skill_documents` loading solely for this validator;
- remove special replacement of prior
  `PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED` rows;
- remove the heuristic `if planning_contract_issues` early return;
- retire/update the direct tests listed in Section 11;
- make `PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED` unreachable rather than aliasing
  it to another diagnostic.

## 16. Existing Owners to Reuse

Reuse the existing functions listed in Sections 7 and 8. In particular, the
future retirement patch should rely on the already-called candidate admission
sequence:

```text
structured_plan_candidate_diagnostics
-> candidate_artifact_ref_keys
-> validate_candidate_graph_and_flow
-> prepare_allocation_intent
-> resolve/rewrite handles
-> build_plan_state
-> normalize_plan_contracts
```

Do not add `PlanningValidatorV2`, `DependencyClassifier`, an artifact-flow
guessing layer, or any equivalent replacement framework.

## 17. Exact Future Modification Boundary

Recommended future implementation boundary:

1. Delete the target module.
2. Remove its single production import/call and heuristic-only state merge and
   route branch.
3. Update the one direct test module as classified above.
4. Add/retain regression cases proving:
   - ordering-only same-skill dependencies are accepted;
   - unknown artifact refs still fail in `candidate_artifact_ref_keys`;
   - declared artifact with no reachable producer still fails in
     `validate_candidate_graph_and_flow`;
   - Plan Review canonical blockers still route to Planner;
   - valid snpEff DB producer/consumer identity remains accepted.
5. Run the structural Plan, Plan Review, plan-local binding, and real graph
   suites.

This boundary deletes a heuristic truth source; it does not move semantic rules
into another Controller module.

## 18. Explicit No-Change Areas

This Track A audit makes and recommends no immediate change to:

- `agent_core/plan_contract.py`;
- `agent_core/plan_tracker.py` structural/approval loaders;
- `agent_core/managed_artifacts.py`;
- TaskSpec or Consultant input audit;
- Execute/recovery behavior;
- snpEff renderer/runtime implementation;
- the `151a6e7` open-world requirements migration;
- `SkillManager.validate_plan`, `_skill_plan_violations`, or
  `_plan_step_skill_violations` in this retirement patch.

The last three are adjacent Controller skill/text heuristics and may deserve a
separate architecture audit if the broader target is to remove **all**
Controller Skill Planning Semantics. They are not dependencies of this file and
do not justify retaining it.

## 19. Risks

1. **Loss of a semantic safety net:** Planner could omit a conceptually needed
   intermediate without declaring any reference. That omission will no longer
   be guessed from same-skill ordering. This is the intended ownership shift,
   but prompt/review coverage should remain.
2. **Human review dependence:** a semantically poor but structurally coherent
   Plan can reach approval. That is consistent with Planner/human semantic
   ownership.
3. **Adjacent heuristic layer remains:** retiring this file alone does not prove
   the repository has no other Controller skill/text heuristics.
4. **Compatibility expectations:** downstream dashboards or tests that count
   `PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED` must be updated when it becomes
   unreachable.
5. **Physical input drift:** removal must not be misread as removing
   Execute-time current-state validation. That boundary should remain explicit.

## 20. Recommendation

Approve a later, narrow whole-file retirement change.

```text
Recommendation: RETIRE ENTIRE skill_planning_contracts.py
Replacement framework: NONE
Authoritative owners: existing Plan candidate/DAG/artifact/allocation validators
```

The retirement implementation should be independent from the PlanSnapshot
re-entry fix. It should remove the heuristic rather than weaken, special-case,
or rename it.

## 21. Confidence

```text
Confidence = 98%
```

Confidence is based on complete function/error/caller search, direct source
inspection, repository-wide symbol search, Git provenance, current incident
state, existing unit tests, and read-only probes of each broken artifact class.
The residual 2% reflects adjacent skill/text heuristics outside this file and
the fact that a future retirement implementation/test run has intentionally not
been performed in Track A.
