# Batch 6 Provenance Test Matrix

## Focused Coverage

| Area | Assertions |
| --- | --- |
| Plan DAG | Linear, branch, join, non-numeric order, cycle errors |
| Artifact lineage | Producer, consumers, current head, multiple versions |
| Attempt lifecycle | Stable IDs, idempotent checkpoint replay, JSONL upsert |
| Ownership | Changed declared outputs only; unchanged files rejected |
| Probe isolation | Probe outputs excluded; legacy role pollution corrected |
| Validation | Materialized version becomes valid/invalid for the same attempt |
| Renderer | Plan step differs from stage; stage DAG and producer stage retained |
| Migration | Safe defaults, unknown producers, formal legacy output backfill |
| Persistence | `plan_id`, attempts and heads survive save/load |
| Observability | Compact debug dump and one execution-lineage log per attempt |

## Commands

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m unittest \
  tests.test_execution_provenance \
  tests.test_plan_tracker \
  tests.test_renderer_manifest \
  tests.test_renderer_controller
```

Result: 66 tests passed.

The broader recovery, lifecycle, Supervisor, CompletionGate, shell-safety and context regression selection passed 392 valid tests. Two initially requested module names did not exist; the actual modules `tests.test_recovery_policy_ownership` and `tests.test_supervisor_validation_evidence` were then run and their 27 tests passed. A further 42 TaskSpec, integration and prompt tests passed.

`py_compile` passes for the modified Python modules. Startup messages about a missing API key are test-import diagnostics; these tests and the session replay are offline and do not call the LLM API.
