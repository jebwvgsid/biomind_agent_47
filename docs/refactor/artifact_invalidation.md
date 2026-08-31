# Batch 7 Artifact Invalidation

Batch 7 reuses Batch 6's stable logical artifact IDs and versioned artifact records.

## States

- `valid`: linked validation accepted the active producer attempt.
- `materialized`: a file exists but validation is not sufficient yet.
- `stale`: the file may still exist, but its producer attempt or upstream chain was invalidated.
- `invalid`: evidence proves the artifact does not satisfy its contract.

## Propagation

Rollback computes affected steps from the explicit DAG. Artifact versions produced by the rollback target or affected descendants become stale, unless their logical IDs are explicitly listed as invalid. Producer attempts for those versions become `invalidated`.

Unrelated branch artifacts remain unchanged. No file is deleted by rollback. A repaired attempt creates a new artifact version and updates `artifact_heads`; the old version remains stale history.

## Consumer Rules

`CompletionGate.validate_dependencies()` checks the active artifact head, producer identity and producer-attempt status. A nonempty file cannot satisfy a dependency when its active version is stale/invalid or its attempt is no longer successful. Probe outputs remain excluded from formal producer registration by the Batch 6 provenance writer.

