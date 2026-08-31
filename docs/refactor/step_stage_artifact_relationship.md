# Step, Stage, Attempt, And Artifact Relationships

## Plan DAG

Plan dependencies use explicit `depends_on`. `dependencies` remains a compatibility alias. Artifact-derived dependencies are accepted only when an input exactly matches a declared logical artifact ID or normalized path; their source is recorded as `artifact_contract_inferred`.

Queries never infer order from names such as `step-1` and `step-2`:

- `get_step_dependencies()` returns direct explicit/data dependencies and structured cycle errors.
- `get_step_descendants()` traverses reverse dependencies.
- `get_step_input_producers()` resolves the current head of each input artifact.

## Renderer DAG

Renderer manifest v3 remains unchanged on disk. PlanTracker normalizes each manifest action into an internal ManifestStage:

```text
PlanStep(renderer_composite)
  -> Manifest
    -> ManifestStage
      -> Action
        -> Attempt
          -> ArtifactVersion
```

The internal `stages` view records manifest dependency IDs and canonical input/output artifact IDs. `get_manifest_stage_dependencies()` detects stage cycles and returns a structured error rather than a partial order.

## Producer And Consumer Rules

- Producer identity is stored on the artifact version: Plan step, Renderer stage, action and attempt.
- Consumers are stored on the logical artifact lineage as Plan step and Renderer stage IDs.
- `get_artifact_producer()` resolves the current head and returns `producer_unknown` when migration cannot prove ownership.
- `get_artifact_consumers()` returns declared consumers.
- `get_attempt_artifacts()` returns only versions actually attributed to an attempt.

The same physical path may have many versions. A rerun cannot claim an unchanged pre-existing file. A diagnostic probe is retained as an attempt for auditability but is isolated from formal artifacts.

