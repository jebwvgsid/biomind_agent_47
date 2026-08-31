# Batch 6 Provenance Migration

## Plan Migration

`migrate_plan_state()` upgrades old plans to schema v4 in place and records applied migrations. It:

- creates one stable `plan_id` when absent;
- maps `dependencies` to `depends_on`;
- initializes the bounded `provenance_state`;
- normalizes Plan output contracts into expected logical artifacts;
- infers data dependencies only from exact logical IDs or normalized paths.

Missing producer evidence is represented as `producer_unknown`; migration does not guess ownership from step order or filenames.

## Execution Record Migration

`load_plan_state()` reads legacy `executions/execution_records.jsonl` and calls `migrate_execution_provenance()`. The migration fingerprint makes replay idempotent.

Legacy command classification gives explicit `purpose=probe` priority. It also recognizes bounded read-only inspection patterns such as `head`, `grep`, `rg`, `sed`, `cat`, `tail`, `awk`, pagers and help flags. A polluted legacy `renderer_invocation` label is therefore corrected to `diagnostic` when command evidence proves it was a probe.

Only non-probe records may backfill a producer, and only when their declared `produced_artifacts` path exactly matches a formal Plan artifact. Legacy Renderer evidence does not create a manifest or stage that never existed.

## Replay Result

An isolated copy of `session_1785398207` migrates to:

```text
schema_version: 4
actions: 7
attempts: 7
roles: diagnostic x7
step_ids: step-1
formal producer attempts: 0
manifests: 0
stages: 0
```

The original task output is not modified during replay.

