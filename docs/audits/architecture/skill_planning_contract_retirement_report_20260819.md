# Skill Planning Contract Whole-File Retirement Report

Date: 2026-08-19

Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`

Branch: `codex/save-current-work`

Starting HEAD: `9ae9c7bbf201aea23f278331220e5352c13e8fcc`

Approved audit:
`docs/audits/architecture/skill_planning_contract_retirement_audit_20260819.md`

```text
SKILL_PLANNING_CONTRACTS_FULL_RETIREMENT_IMPLEMENTED = YES
REPLACEMENT_VALIDATOR_ADDED = NO
```

## Scope

This change implements only the whole-file retirement already approved by the
architecture audit. It does not redesign planning validation and does not move
the retired semantic inference into another Controller component.

The preserved ownership rule is:

> Dependency edges establish ordering. Structured artifact references
> establish dataflow.

## Deleted Production Call Path

The retired path was:

```text
bio_agents.py Plan Review
  -> load every selected SKILL.md
  -> validate_selected_skill_planning_contracts(...)
     -> _declares_cross_step_artifact_contract(...)
     -> infer dataflow from same-skill ordering edges
     -> emit PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED
  -> replace previous issues with the same heuristic code
  -> early-return to Planner with PLAN_CONTRACT_REPAIR
```

The implementation deletes all parts of that path:

1. Deleted `agent_core/skill_planning_contracts.py` in full.
2. Deleted its only production import from `bio_agents.py`.
3. Deleted Plan Review's validator-only selected-skill document loading.
4. Deleted the validator call.
5. Deleted special replacement of
   `PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED` issues.
6. Deleted the heuristic-only Planner early-return branch.

No alias, renamed diagnostic, compatibility shim, or replacement semantic
heuristic was added.

## Preserved Plan Review Path

The surrounding Plan Review path remains:

```text
load_plan_state
-> selected skill IDs for renderer records
-> normalize_plan_contracts
-> bind_plan_to_task_revision
-> save_plan_state
-> canonical blocking_review_issues routing
-> approval reuse / semantic conflict handling
-> rendered human Plan Review
-> normal approval routing
```

In particular, the implementation preserves:

- `normalize_plan_contracts(...)`;
- canonical `review_issues` and blocking-severity filtering;
- owner-based routing of canonical blockers;
- output-contract validation;
- normal human Plan Review and approval;
- the Planner candidate/retry lifecycle;
- the approved PlanSnapshot gate introduced by
  `2075849df5f512a5cf23df2b5679bcbdb5100e74`.

## Preserved Authoritative Structural Validators

| Existing validator | Preserved responsibility |
| --- | --- |
| `structured_plan_candidate_diagnostics` | Structured Planner candidate schema and wire fields |
| `candidate_artifact_ref_keys` | Exact artifact declaration/reference resolution and `ARTIFACT_REFERENCE_UNKNOWN` |
| `validate_candidate_graph_and_flow` | Step/dependency graph, cycle detection, producer cardinality, reachability, availability, and origin conflicts |
| `prepare_allocation_intent` | Registered object kind, lifecycle, and materialization policy |
| `promote_allocation_intent` | Controller-owned allocation of plan-local identities |
| `rewrite_candidate_local_refs` | Consistent producer/consumer identity rewrite |
| `normalize_plan_contracts` | Canonical pre-review normalization and validation issue derivation |
| `normalize_task_output_materializations` | Task-output reference and materialization validation |
| `validate_output_contracts` | Required output coverage and producer ownership |

No listed validator was modified.

## Test Retirement and Updates

`tests/test_snpeff_plan_dataflow_contract.py` was changed according to the
audit classification:

- retired `test_split_plan_without_shared_artifact_requests_planner_repair`;
- retired `test_composite_plan_needs_no_cross_step_artifact`;
- retained and adjusted
  `test_split_plan_reuses_one_allocated_directory_identity` to test only typed
  allocation/dataflow identity;
- retained Planner prompt and human review guidance coverage;
- retained the snpEff runtime-variable documentation contract;
- added direct positive and fail-closed structural cases.

`tests/test_consultant_first_graph_integration_v21.py` adds one real registered
Planner-to-Plan-Review graph regression for a snpEff same-skill ordering-only
dependency. Existing snapshot and canonical blocker graph tests are reused.

## Same-Skill Ordering-Only Proof

The positive candidate contains two steps using
`genomics/snpeff-annotation`:

```text
step-1 validates the admitted input and produces no artifact
step-2 depends on step-1 for ordering and writes the requested report
```

There is intentionally no `step-1.outputs -> step-2.inputs` dataflow edge.

Red evidence before production retirement:

```text
Plan Review result: next_step = planner
Assertion: 'planner' != 'generate'
```

That result came from the retired same-skill heuristic.

Green evidence after retirement:

```text
typed candidate diagnostics = []
typed graph/dataflow issues = []
Plan Review reached the human approval path
human approval succeeded
Plan Review result: next_step = generate
current_approval.status = approved
```

This proves that a structurally valid ordering-only dependency now passes both
the authoritative candidate checks and the real Plan Review node.

## Structural Fail-Closed Proof

| Case | Authoritative owner exercised | Observed result |
| --- | --- | --- |
| Unknown artifact reference | `candidate_artifact_ref_keys` | `ARTIFACT_REFERENCE_UNKNOWN` exception |
| Declared plan-local artifact without a producer | `validate_candidate_graph_and_flow` | `PLAN_ARTIFACT_UNAVAILABLE` |
| Two producers for one artifact | `validate_candidate_graph_and_flow` | `PLAN_ARTIFACT_PRODUCER_CARDINALITY` |
| Unknown dependency | `validate_candidate_graph_and_flow` | `PLAN_DEPENDENCY_UNKNOWN` |
| Cyclic dependency | `validate_candidate_graph_and_flow` | `PLAN_DEPENDENCY_CYCLE` |
| Unregistered materialization policy | `prepare_allocation_intent` | `ARTIFACT_POLICY_UNREGISTERED` exception |
| Valid plan-local directory handoff | allocation/promote/rewrite/graph flow | one shared controller-owned producer/consumer identity; no issues |
| Invalid Task output materialization in real Plan Review | `normalize_plan_contracts` / task output normalization | `TASK_OUTPUT_REFERENCE_UNKNOWN`, routed to Planner |

The negative cases are based on declared machine structure. None depends on
skill ID, step title, prose, or a replacement semantic classifier.

## Test Results

### Targeted structural regression

```text
python -m unittest \
  tests.test_plan_contract_v121.PlannerWireContractRegressionTests \
  tests.test_plan_contract_v121.PlanContractV121FlowTests \
  tests.test_snpeff_plan_dataflow_contract

Ran 21 tests in 0.019s
OK
```

The nine direct snpEff contract tests also pass independently:

```text
Ran 9 tests in 0.024s
OK
```

### Plan Review and PlanSnapshot graph regression

The targeted graph set contains:

- same-skill ordering-only Plan Review acceptance;
- pending candidate does not use the approved loader;
- approved snapshot still uses the approved loader;
- the approved loader remains strict for pending state;
- canonical unknown output reference routes Plan Review to Planner and re-enters
  the pending candidate lifecycle.

```text
Ran 5 tests in 6.998s
OK
```

### Static checks

```text
python -m py_compile \
  bio_agents.py \
  tests/test_snpeff_plan_dataflow_contract.py \
  tests/test_consultant_first_graph_integration_v21.py
PASS

git diff --check
PASS
```

## Git Diff

Expected final retirement paths:

```text
D  agent_core/skill_planning_contracts.py
M  bio_agents.py
M  tests/test_snpeff_plan_dataflow_contract.py
M  tests/test_consultant_first_graph_integration_v21.py
A  docs/audits/architecture/skill_planning_contract_retirement_report_20260819.md
```

The production diff is deletion-only:

```text
agent_core/skill_planning_contracts.py: deleted, 92 lines removed
bio_agents.py: import and heuristic Plan Review path removed, 39 lines removed
```

Repository search after retirement finds no production or Python-test import,
caller, emitter, or assertion for:

```text
skill_planning_contracts
validate_selected_skill_planning_contracts
PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED
```

## Explicit No-Change Verification

No diff was made to:

- `agent_core/plan_contract.py`;
- `agent_core/plan_tracker.py`;
- `agent_core/managed_artifacts.py`;
- Planner Repair;
- `agent_core/node_context.py`;
- `agent_core/task_spec.py`;
- Generate, Execute, or Recovery;
- `SkillManager.validate_plan`;
- `_skill_plan_violations`;
- `_plan_step_skill_violations`.

Git blame still attributes the Planner approved-snapshot gate to `2075849d`;
the retirement diff does not touch that branch. The open-world migration at
`151a6e7` is not reverted.

## Working Tree Safety

The initial working tree contained unrelated modified skill documents, many
modified tests, and numerous untracked documentation/test assets. They were
recorded before implementation and left in place.

Safety controls:

- no reset, restore, checkout, clean, or stash;
- no `git add .`, `git add -A`, or `git commit -am`;
- only the five retirement paths listed in the Git diff section are eligible
  for staging;
- unrelated dirty work remains unstaged and uncommitted.

## Acceptance

```text
whole-file heuristic retirement = complete
same-skill ordering-only dependency = accepted
declared structural corruption = fail closed
canonical Plan Review blockers = preserved
replacement validator/framework = none
approved PlanSnapshot state fix = preserved
```
