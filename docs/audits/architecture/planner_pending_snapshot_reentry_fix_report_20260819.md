# Planner Pending PlanSnapshot Re-entry Fix Report

Date: 2026-08-19

Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`

Branch: `codex/save-current-work`

Starting HEAD: `151a6e720ff510cd8a2ead0038a879d340059a46`

Implementation commit: `2075849df5f512a5cf23df2b5679bcbdb5100e74`

Implementation commit subject: `fix(planner): gate approved snapshot loading on approval state`

```text
PLAN_SNAPSHOT_STATE_FIX = PASS
```

## Root Cause

The Planner re-entry path used the existence of
`current_plan.plan_snapshot_ref` as the only precondition for calling
`load_approved_plan_snapshot(...)`.

That condition conflated two different facts:

```text
a persisted PlanSnapshot exists
an approved PlanSnapshot authority exists
```

Planner candidate creation legitimately persists a snapshot while
`current_approval.status` is still `pending`. When Plan Review found a
structured blocker and routed back to Planner, the second Planner invocation
saw the candidate's snapshot reference and entered the approved-snapshot
loader. The loader correctly rejected that state with
`APPROVED_PLAN_SNAPSHOT_NOT_APPROVED`; the caller had selected the wrong
lifecycle before invoking it.

The first fault was therefore the Planner caller's state selection, not the
strict loader and not the Plan Review violation.

## Existing Function Modified

Only the existing nested Planner node handler was changed:

```text
bio_agents.py
BioAnalystAgent._build_graph.<locals>.planner
```

The change is immediately before the existing
`load_approved_plan_snapshot(...)` call. No new state field, exception mapping,
or fallback lifecycle was added.

## State Invariant Before

The effective pre-change invariant was:

```text
plan_snapshot_ref exists
=> snapshot has approval authority
=> approved loader may be called
```

This was invalid because candidate snapshots are persisted before approval.

## State Invariant After

The corrected invariant is:

```text
snapshot existence is not approval authority

plan_snapshot_ref exists
AND current_approval.status == "approved"
=> approved loader may be called
```

For a pending candidate, Planner continues through its existing working-plan,
feedback, retry, and repair path. For an approved snapshot, Planner still uses
the same approved loader and receives the same verified snapshot contract.

The canonical existing field `current_approval.status` is reused. No duplicate
approval flag was introduced.

## Implementation

Before:

```python
if base_snapshot_id:
    planner_approved_snapshot = load_approved_plan_snapshot(...)
```

After:

```python
if (
    base_snapshot_id
    and str(
        (planner_source_state.get("current_approval") or {}).get("status")
        or ""
    )
    == "approved"
):
    planner_approved_snapshot = load_approved_plan_snapshot(...)
```

This is a pre-call state decision. It does not catch or downgrade
`APPROVED_PLAN_SNAPSHOT_NOT_APPROVED`.

## Explicitly Preserved Boundaries

- `agent_core/plan_tracker.py` was not modified.
- `load_approved_plan_snapshot(...)` remains fail-closed for pending state.
- `APPROVED_PLAN_SNAPSHOT_NOT_APPROVED` remains reachable for invalid direct
  approved-loader use.
- No catch-all maps the strict-loader exception to retry.
- `agent_core/skill_planning_contracts.py` was not modified.
- The Track A Planning Validator audit did not become a production retirement
  change.

## Tests

Four state tests were added to the existing integration module
`tests/test_consultant_first_graph_integration_v21.py`.

### B-T01 Pending candidate

`test_pending_candidate_reentry_does_not_load_approved_snapshot`

- Creates a real Planner candidate snapshot.
- Confirms `current_approval.status == "pending"`.
- Re-enters the registered Planner handler.
- Asserts the approved loader is not called.
- Confirms routing continues to Plan Review.

### B-T02 Approved snapshot

`test_approved_snapshot_reentry_loads_approved_snapshot`

- Creates the same candidate state.
- Promotes it through the existing `approve_plan_state(...)` authority.
- Re-enters Planner.
- Asserts the real approved loader is called once and succeeds.

### B-T03 Approved loader remains strict

`test_approved_snapshot_loader_remains_strict_for_pending_candidate`

- Calls the unchanged approved loader directly with a pending candidate.
- Asserts `APPROVED_PLAN_SNAPSHOT_NOT_APPROVED` is still raised.

### B-T04 Real Plan Review round-trip

`test_plan_review_unknown_output_reference_reenters_pending_candidate`

- Creates a real structured Planner candidate.
- Persists an unknown `task_output_materializations.output_ref`.
- Confirms Plan Review emits `TASK_OUTPUT_REFERENCE_UNKNOWN` and routes to
  Planner.
- Re-enters Planner with a persisted snapshot and pending approval.
- Asserts the approved loader is not called, no crash occurs, and durable task
  state is not `failed`.

## Test Results

### Required red/green evidence

Before the production gate was changed, B-T01 and B-T04 failed at the strict
approved loader because pending candidate state was misrouted. B-T02 and B-T03
already demonstrated the valid approved path and strict-loader behavior.

After the fix:

```text
Ran 4 tests in 6.163s
OK
```

### Expanded passing regression coverage

```text
python -m py_compile bio_agents.py \
  tests/test_consultant_first_graph_integration_v21.py
PASS
```

```text
python -m unittest \
  tests.test_plan_contract_v121 \
  tests.test_node_context_semantic_continuity \
  tests.test_snpeff_plan_dataflow_contract

Ran 37 tests in 0.227s
OK
```

Track A also ran 17 focused Planner wire/flow/snpEff contract tests
successfully before implementation.

### Additional current-checkout observations

These suites were run for visibility but contain failures outside the modified
state invariant:

- Full `tests.test_consultant_first_graph_integration_v21`: 11 tests run, 8
  passed and 3 failed. Two failures stop in Consultant/Plan Review routing
  before the changed Planner approved-loader gate. The third reaches execution
  output reconciliation with `OUTPUT_MISSING` and never fails on snapshot
  authority. All four new Track B tests pass within this same module.
- `tests.test_plan_lifecycle`: 49 tests run, 48 passed and 1 failed on an
  unrelated renderer-manifest transition-guard string assertion.

No production or test changes were made to address those unrelated failures.
They do not invalidate B-T01 through B-T04, but they are recorded rather than
reported as a wholly green repository.

## Files Committed

Implementation commit `2075849df5f512a5cf23df2b5679bcbdb5100e74`
contains exactly:

```text
bio_agents.py
tests/test_consultant_first_graph_integration_v21.py
```

This implementation report and the Track A audit are committed separately as
documentation so that this report can name the immutable implementation SHA.

## Commit Hash

```text
2075849df5f512a5cf23df2b5679bcbdb5100e74
fix(planner): gate approved snapshot loading on approval state
```

## Pre-existing Dirty Files Preserved

The working tree was dirty before this task. The initial snapshot recorded
modified skill documents, numerous modified test modules, `HANDOFF.md`, and
multiple untracked audit/refactor/test assets.

Safety controls used:

- No reset, restore, checkout, clean, or stash command was used.
- No `git add .`, `git add -A`, or `git commit -am` was used.
- The implementation commit staged two explicit paths only.
- Unrelated modified and untracked files remain in the working tree.
- The strict loader and Planning Validator production module have zero diff in
  the implementation commit.

## Acceptance

```text
candidate snapshot + pending approval -> candidate repair lifecycle
candidate snapshot + approved authority -> strict approved loader
direct pending state into strict loader -> fail closed
real Plan Review blocker -> Planner re-entry without TASK_FAILED

PLAN_SNAPSHOT_STATE_FIX = PASS
```
