# Batch 7 Test Matrix

## Focused Scenarios

`tests/test_batch7_rollback.py` covers:

| Scenario | Assertion |
|---|---|
| Linear rollback | real cursor moves upstream; consumer waits; descendant invalidates |
| Branch DAG | unrelated branch remains passed |
| Multi-upstream | only the invalid producer branch rolls back |
| Invalid target/version/artifact | rejected atomically |
| Idempotency/checkpoint | cursor and budget change once |
| Layered resume | only dependency-ready descendants resume |
| Manifest rewind | Plan step retained; target/descendant stage statuses updated |
| Cross-manifest rewind | rejected before commit |
| Deterministic policy | stage rewind is not converted to Plan patch |
| Stale existing file | DependencyGate and Step Gate reject it |
| Missing producer | RecoveryPolicy emits `PLAN_PATCH_REQUEST` |
| Completion evidence | read-only and bound to attempt/version/validation |
| New attempt | new head created; old version retained as stale audit history |
| Invalidated final step | task completion remains false despite existing file |

## Regression Runs

- Batch 7 + provenance + PlanTracker + CompletionGate: 101 tests passed.
- Recovery + recovery context/policy + Renderer Controller/manifest + Plan lifecycle + Supervisor: 180 tests passed.
- Full discovery: 818 tests ran; 812 passed. Six unrelated environment-dependent failures remained: missing `pytest` (two imports), missing `networkx` (one wiki test), and three scRNA R smoke failures (two exit 137 kills and one Seurat `pbmc_small` export mismatch).

## Historical Replay

An isolated temporary copy of `session_1785398207` was loaded through current migration code without executing commands or calling an LLM:

```text
schema_version: 4
actions: 7
attempts: 7
roles: diagnostic x7
formal producer attempts: 0
manifests: 0
stages: 0
task_complete: false
```

Thus old `head/grep/sed/--help` probes remain diagnostic evidence. They do not create a manifest, stage, producer or completion PASS. The original session directory was not modified.

## Integration Cases

- A, upstream false completion: stale producer head selects `ROLLBACK_UPSTREAM_STEP`.
- B, early Renderer stage false completion: invalid stage dependency selects `REWIND_MANIFEST_STAGE`.
- C, omitted producer step: missing required producer selects `PLAN_PATCH_REQUEST` with no fabricated rollback target.
