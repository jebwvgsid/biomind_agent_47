# Renderer Failure Replay: session_1785398207

## Classification

The raw Generate output was an ordinary `EXECUTE` action. It was not a valid
`RENDERER_INVOCATION` that the parser lost. This is case **A**.

Persisted raw outputs include:

```text
<execute>
head -n 80 .../render_snpeff_annotation_scripts.py
</execute>
```

and later:

```text
<execute>
cd .../Biomind_v1 && python3 .../render_snpeff_annotation_scripts.py --help
</execute>
```

Evidence is preserved in the session memory archives and in:

- `task_outputs/session_1785398207/executions/execution_records.jsonl`
- `task_outputs/session_1785398207/step_7.sh`
- `task_outputs/session_1785398207/step_11.sh`
- subsequent `step_15.sh`, `step_19.sh`, `step_23.sh`, `step_27.sh`, `step_36.sh`

## Reconstructed Transition

| Layer | Observed value |
|---|---|
| Plan step | Renderer-backed composite step for `genomics/snpeff-annotation` |
| Raw Generate output | Ordinary XML `<execute>` probe (`head`, `grep`, `sed`, or `--help`) |
| Parsed action | `type=execute`, code preserved from XML |
| Normalized action | Still an ordinary execute action |
| Bound action | Changed to `command_role=renderer_invocation`, `action_source=renderer_compat_adapter`, `renderer_id=genomics.snpeff_annotation` |
| Binding trigger | `_bind_pending_action()` matched the Renderer entrypoint as a substring of the shell code (`bio_agents.py:2527-2560`) |
| Execution | Exit code 0; output was source/help text; no workflow manifest or target artifacts |
| Supervisor | RETRY because the planned Renderer workflow did not run |
| Recovery | Repeated probe/script-repair decisions; session ended as `action_format_repair_exhausted` |

Seven persisted executions were incorrectly labeled `renderer_invocation`. Their
actual commands were source inspection, help output, grep or sed. Most have an
empty `manifest_path`, which proves they were not materialized through the
canonical Renderer path.

## Direct Trigger

The direct bug is structural, not tool-specific:

```python
if renderer_entrypoint in code:
    command_role = "renderer_invocation"
```

This rule confuses "mentions/inspects the renderer" with "was produced by the
Renderer invocation materializer". The action's provenance is inferred from
command text after parsing instead of being established by the structured
action protocol.

## Prompt Contribution

The model was also given conflicting choices:

- Renderer instructions ask for `RENDERER_INVOCATION`.
- The generic Generate prompt still advertises ordinary `EXECUTE`.
- Action-format repair advertises both `EXECUTE` and `RENDERER_INVOCATION` at
  `bio_agents.py:10894-10920`.
- The current-step projection does not reliably pin the canonical Renderer ID,
  argument schema, manifest status and already verified capabilities ahead of
  generic skill text.

This explains why Generate chose probes, but it does not excuse executing them as
Renderer invocations. The deterministic pre-action boundary should have rejected
them.

## State/Contract Contribution

`create_task_spec()` currently preserves the original request as `goal` but starts
`inputs`, `parameters`, `output_contracts` and `requirements` empty
(`agent_core/task_spec.py:64-100`). Formal input discovery exists, but there is no
single generic projection that turns its verified roles into Renderer arguments.
Generate therefore tried to rediscover the Renderer CLI despite the original
request already containing VCF, FASTA, GTF, `-gtf22` and output directory facts.

## Minimal Fix Boundary

Batch 1 should change only existing protocol boundaries:

1. `_bind_pending_action()` must never infer Renderer provenance from substring
   matches. The compatibility adapter may recognize only a complete invocation
   with the declared runner/entrypoint and required argument contract, or be
   disabled for Renderer composite steps.
2. `_validate_pending_action_contract()` must reject free-form `EXECUTE` before
   execution when the current step is Renderer-backed and has no registered
   manifest.
3. The Generate action contract and format-repair action list must be dynamic.
   Before a manifest exists, the only materialization action is
   `RENDERER_INVOCATION` (plus a contract-level BLOCKED/PLAN_PATCH_REQUEST when
   justified). Ordinary probes require an explicit RecoveryPolicy authorization.
4. Existing `_normalize_renderer_invocation_arguments()` and
   `_materialize_renderer_invocation()` remain the only canonical Renderer path.

No snpEff-specific branch is needed.
