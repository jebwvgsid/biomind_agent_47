# BioMind Runtime and Recovery Context Contract

Status: code-proven architecture baseline

Audit snapshot: `34934de0c92d7cae268a101a3dce13606039c21e` plus the pre-existing working tree on 2026-08-17

Scope: reusable runtime-propagation and Recovery-context facts; incident-specific values are intentionally excluded.

## Purpose

This file is the starting point for future runtime and Recovery audits. It records only behavior established by production code. `IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, and `MISSING` describe the current contract; they are not repair proposals.

## Runtime fact lifecycle

| Stage | Current contract | Status | Code evidence |
|---|---|---|---|
| Skill runtime discovery | Skill front matter declares an environment, tools, packages, capabilities, and a shell healthcheck. | IMPLEMENTED | `bio_agents.py:8810-8875`; skill-specific front matter |
| Healthcheck execution | The Controller runs the declared healthcheck and persists status, detail, and paths parsed from `RESOLVED_*` output rows. | IMPLEMENTED | `bio_agents.py:8810-8875` |
| Resolved-path persistence | Only existing paths in `runtime_audit[].resolved_runtime_paths` become Controller bindings. A `RESOLVED_X` row also creates alias `X`. | IMPLEMENTED | `bio_agents.py:5393-5418` |
| Executable identity persistence | A healthcheck may invoke an absolute executable, but the generic binding layer does not derive or persist an executable identity from a home directory. | MISSING | `bio_agents.py:5393-5418` |
| Generate projection | Generate receives a NodeContext `runtime` projection containing progress/current-action semantics. The generic builder call does not add `runtime_audit` or Controller runtime bindings. | PARTIALLY_IMPLEMENTED | `bio_agents.py:24675-24753` |
| Generate prompt contract | Generate is told that tools are supplied as Controller-managed environment variables and, for snpEff, to reference `$SNPEFF_JAR`. The prompt does not define an executable-resolution rule. | PARTIALLY_IMPLEMENTED | `prompts/manager.py:1807-1858` |
| Post-Generate binding | The Controller adds `runtime_bindings` after the model response is parsed and bound to the current task/step. | IMPLEMENTED | `bio_agents.py:4775-4867` |
| Execute materialization | For Bash, managed runtime values become conditional `export` statements. Action-owned variables are exported separately and conflicts are rejected. | IMPLEMENTED | `bio_agents.py:480-547` |
| PATH contract | Managed environment injection does not prepend runtime `bin` directories and does not otherwise control `PATH`. | MISSING | `bio_agents.py:480-547` |
| Shell contract | Structured argv may contain `bash -lc`; managed exports are placed outside that nested login shell. The login shell may read profiles and transform `PATH`. | PARTIALLY_IMPLEMENTED | `prompts/manager.py:1824-1829`; `bio_agents.py:480-547` |
| Healthcheck-to-execute closure | No generic assertion compares the executable invoked during healthcheck with the executable resolved immediately before Execute. | MISSING | runtime audit, binding, and materialization paths above |

### Runtime distinctions that audits must preserve

`JAVA_HOME` is a directory-valued environment variable. It does not alter shell command lookup by itself. `JAVA_EXECUTABLE` would be an executable identity, but the current generic runtime binding code does not create such a fact. Likewise, `RESOLVED_JAVA_HOME` is only an absolute directory path; it is not proof that a later bare `java` resolves below that directory.

The reusable provenance chain is:

```text
skill healthcheck
  -> status/detail/RESOLVED_* paths
  -> runtime_audit[].resolved_runtime_paths
  -> pending_action.runtime_bindings (after Generate)
  -> conditional environment exports
  -> shell and PATH command resolution
```

The chain preserves path strings but does not preserve executable identity end to end.

## Recovery evidence lifecycle

| Stage | Current contract | Status | Code evidence |
|---|---|---|---|
| Evidence enrichment | Reads persisted script/trace/log paths, expected runtime contract, actual selected environment facts, manifest summary, action contract, and failure details. | IMPLEMENTED | `agent_core/diagnostics.py:610-715` |
| Log reads | Each stdout/stderr file is read up to `max_text_bytes_per_file`; full text is retained only when the file fits, and a bounded tail is always derived from the bytes read. | IMPLEMENTED | `agent_core/diagnostics.py:519-529,623-632` |
| Bounded log excerpt | The combined stderr/stdout tail projection is clipped to 6,000 characters. | IMPLEMENTED | `agent_core/diagnostics.py:1325-1330` |
| Filesystem snapshot | Scans bounded roots with maximum file count, depth, total bytes, and per-file bytes. Large biological files are metadata-only. | IMPLEMENTED | `agent_core/diagnostics.py:826-890`; `DiagnosticLimits` |
| Internal-state suppression | Named internal state files and JSONL files are metadata-only in the filesystem snapshot. Other small JSON files, including provider trace artifacts, can receive previews. | PARTIALLY_IMPLEMENTED | `agent_core/diagnostics.py:507-510,863-879` |
| Raw RecoveryContext | Combines semantic state, execution/runtime evidence, filesystem snapshot, logs, skill policy, history, and deterministic diagnosis into a durable object. | IMPLEMENTED | `agent_core/diagnostics.py:1300-1340` |
| Legacy prompt projection | Serializes the entire context, preserves five semantic sections, and recursively compacts evidence at four `(text_limit, list_limit)` levels. | IMPLEMENTED | `agent_core/diagnostics.py:244-357` |
| Evidence dictionary breadth | String length and list length are reduced, but dictionary key count is not capped. | PARTIALLY_IMPLEMENTED | `agent_core/diagnostics.py:260-281` |
| Capacity diagnostic | After all candidates fail, `required_semantic_chars` measures identity plus semantic sections only. It does not include the smallest mandatory evidence projection or JSON wrapper overhead. | PARTIALLY_IMPLEMENTED | `agent_core/diagnostics.py:359-389` |
| Provider boundary | Recovery v2 reaches the provider only in `_invoke_recovery_v2`, after NodeContext and prompt construction, via one Human message. | IMPLEMENTED | `agent_core/middleware.py:83-128` |
| Capacity gate ordering | Middleware calls `prompt_view()` before provider invocation. A typed capacity error produces a deterministic `ESCALATE`; no partial projection is sent. | IMPLEMENTED | `agent_core/middleware.py:1602-1654` |
| Deterministic diagnosis precedence | Middleware checks capacity error before consuming the diagnosis included in the raw RecoveryContext. | IMPLEMENTED | `agent_core/middleware.py:1610-1661` |
| Failure precedence | Applying `ESCALATE` sets durable task status to failed; terminal transition guards then route subsequent non-blocked recovery states to finish. | IMPLEMENTED | `agent_core/plan_tracker.py:2087-2090`; `bio_agents.py:11923-11930` |

## Exact capacity formula

For the legacy RecoveryContext path, the budget applies to this local Python character count:

```text
len(json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str))
```

It is not a provider tokenizer result, provider message-envelope byte count, model context-window limit, or output-token reservation.

At each compaction level:

```text
candidate = identity
          + complete semantic sections
          + compacted values for every evidence-section key
          + local limits metadata
```

The final diagnostic reports only:

```text
required_semantic_chars = serialized(identity + semantic sections)
```

Consequently, `required_semantic_chars < max_context_chars` does not imply that any complete candidate fits. Mandatory compacted evidence values, JSON field names/separators, and limits metadata remain outside the reported numerator. This is a `DIAGNOSTIC_ACCOUNTING_INCONSISTENCY` whenever the two displayed numbers are interpreted as the actual pass/fail operands.

Provider system instructions, provider output schema, message-envelope overhead, and output reserve are not included in this local gate because the gate runs before provider prompt construction.

## Failure ownership and precedence

The durable sequence for an execution-triggered capacity failure is:

```text
primary execution failure
  -> RecoveryContext and deterministic diagnosis
  -> local prompt projection capacity failure
  -> deterministic RecoveryDecision ESCALATE / INTERNAL_PROTOCOL
  -> task status failed
  -> terminal transition guard
```

The primary execution evidence remains durable, but terminal user projection may show only the later Recovery capacity reason. A later advisory decision can be recorded without becoming executable because the already-durable failed state takes precedence in the transition guard.

## Invariant registry

| Invariant | State | Meaning |
|---|---|---|
| Runtime healthcheck result and resolved path are durable | IMPLEMENTED | The audit state records verified `RESOLVED_*` paths and readiness. |
| Runtime bindings are Controller-owned and cannot be overridden by action env | IMPLEMENTED | Materialization rejects key conflicts. |
| Generate receives the exact verified runtime executable | MISSING | Generic Generate projection does not include it. |
| Healthchecked executable equals executed executable | MISSING | No end-to-end identity assertion exists. |
| PATH belongs to the verified RuntimeContract | MISSING | PATH is neither derived from runtime paths nor closure-checked. |
| Nested login shell preserves verified executable selection | MISSING | Profiles may transform PATH; no closure assertion follows. |
| Raw Recovery evidence and provider payload are separately observable | PARTIALLY_IMPLEMENTED | Raw context and provider traces exist, but a provider trace exists only if invocation is reached. |
| Large biological files are prevented from entering Recovery text previews | IMPLEMENTED | They are metadata-only. |
| Internal PlanState files are prevented from entering filesystem previews | IMPLEMENTED | Named state files and JSONL are metadata-only. |
| All other context-trace JSON previews are excluded | MISSING | Small JSON provider traces may enter artifact previews. |
| stdout/stderr are bounded before Recovery projection | IMPLEMENTED | Per-file byte read, tail line count, and combined excerpt limits apply. |
| Capacity error numerator equals actual minimum candidate | MISSING | The diagnostic numerator omits evidence and structural overhead. |
| Capacity gate prevents a partial semantic request | IMPLEMENTED | The provider is not invoked with a partial semantic view. |
| Primary execution failure always wins terminal presentation | MISSING | A later capacity escalation can become the terminal detail. |

## Evidence checklist for future audits

Future audits can rely on this baseline and only re-measure incident facts:

1. Capture Git HEAD and dirty-tree boundary.
2. Compare the healthcheck command/executable with persisted `resolved_runtime_paths`.
3. Inspect the final Generate `request.json`, not only the prompt builder.
4. Locate the first bare executable in the raw Generate `response.json`.
5. Compare pending-action bindings, approved materialization, wrapper PATH/shell, and `actual_environment.resolved_executables`.
6. Measure RecoveryContext as persisted characters/bytes, canonical serialized characters/bytes, and per-section serialized characters.
7. Reproduce every prompt-view candidate with the exact serializer and compactor.
8. Count only provider trace directories and provider-response token records as actual LLM calls.
9. Separate RecoveryJudge calls from downstream Supervisor review calls.
10. Trace durable status and transition guards after any secondary Recovery failure.
