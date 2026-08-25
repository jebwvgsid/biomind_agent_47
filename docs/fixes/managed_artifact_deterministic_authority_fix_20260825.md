# Managed-Artifact Deterministic Authority Fix

Date: 2026-08-25

Incident: `session_1787622048`

Scope: managed-artifact pre-execution validation and repair routing

## 1. Executive Summary

The pre-execution boundary now treats typed action state as the validation authority. Managed-artifact identity, direction, placeholder shape, canonical path, and output coverage remain strict in structured `argv`/`env` fields. Lexical matches in raw script or description text are retained as high-signal audit diagnostics, but they cannot invalidate an otherwise materializable action, consume a repair budget, or cause a terminal no-progress result.

The recovery policy no longer infers `ACTION_FORMAT_REPAIR` from `valid=false` plus `repairable=true`. Every pre-action decision carries an explicit repair domain. Representation failures use the existing single format-repair budget; deterministic generated-action semantic failures use the existing bounded per-step Generate candidate budget; source diagnostics continue; dependency, internal-protocol, and runtime/output failures remain recovery-owned.

## 2. Files Changed

Production:

- `agent_core/managed_artifacts.py`
- `agent_core/controller_audit.py`
- `agent_core/recovery.py`
- `agent_core/plan_tracker.py`
- `bio_agents.py`

Tests:

- `tests/test_managed_artifact_contract_v2.py`
- `tests/test_generate_script_artifact_repair_convergence.py`
- `tests/test_plan_local_preaction_convergence_v2.py`
- `tests/test_recovery_policy_ownership.py`
- `tests/test_generate_corrective_repair_phase1.py`
- `tests/test_session_controller_recovery_contract.py`

The existing `tests/test_managed_action_env_v2.py` was not changed, but was included in the targeted regression run.

## 3. Exact Behavior Before

- `materialize_managed_action` scanned whole raw `code` strings for managed canonical paths, basenames, and placeholders.
- A basename in a comment, log line, variable description, fallback expression, or heredoc could become `MANAGED_ARTIFACT_HANDLE_REQUIRED`, `ACTION_OUTPUT_BINDING_MISMATCH`, or a free-shell placeholder violation.
- Raw Controller script-audit findings were marked authoritative even though they were based on lexical evidence rather than structured command semantics.
- A generic invalid and repairable pre-action result fell through to `FORMAT_REPAIR`, even when the action representation was well formed and the actual problem was a managed-artifact semantic mismatch.
- Consequently, a repeated diagnostic/source match could consume `action_format_repair_count` and terminate as format-repair exhaustion or no progress before Execute.

## 4. Exact Behavior After

- Typed `argv` and `env` values remain deterministic validation inputs and are materialized exactly as before.
- For the `script_block_v1` adapter, `argv[2]` is recognized as a byte-for-byte transport copy of raw `code`, not a second typed command scalar.
- Raw `code` and `description` matches are emitted under `source_diagnostics` with `authoritative=false` and `effect=diagnostic_only`; they never become `ArtifactViolation` objects.
- Generate and Execute block Controller audit findings only when both `severity=HIGH` and `authoritative=true`.
- Structured semantic violations route to bounded corrective Generate without consuming the action-format budget.
- Controller projection loss and managed environment injection failures remain fail-closed internal-protocol failures. Runtime-unavailable inputs remain dependency recovery.

## 5. Evidence Authority Model

Authoritative machine contract:

- approved semantic handle and binding identity;
- approved snapshot/current-step visibility;
- structured `argv` and `env` field shape;
- atomic typed placeholder use;
- canonical managed path materialization;
- input/output direction and explicit output coverage;
- dependency availability and structured runtime/output evidence.

Non-authoritative diagnostic evidence:

- free script source;
- comments, logging strings, examples, descriptions, and heredoc contents;
- raw lexical basename, path, placeholder, or environment-reference matches;
- Controller raw-source audit observations without a structured command fact.

No shell AST, quote parser, or heredoc parser was introduced.

## 6. Repair Routing Changes

The explicit pre-action domains and final routes are:

```text
representation
-> ACTION_FORMAT_REPAIR

generated semantic
-> CORRECTIVE_GENERATE

source diagnostic
-> continue

dependency
-> recovery

internal protocol
-> internal_protocol

runtime/output evidence
-> recovery
```

Missing or unsupported domain information is fail-closed and budget-neutral; it cannot silently become format repair.

## 7. Format Budget Changes

`action_format_repair_count` and `action_format_repair_by_step` increment only for a committed `FORMAT_REPAIR` decision in the `representation` domain. Examples include malformed script-block framing, an unparseable action envelope, non-list `argv`, non-object `env`, invalid environment keys, and non-string environment values.

Generated semantic correction consumes `generate_attempts_by_step[step_id]`, bounded by the existing `max_candidate_repairs_per_epoch` value (default 2). It does not change the scalar or per-step format budget. Identical invalid corrective candidates terminate as bounded no progress without an extra budget increment. Source diagnostics, dependency recovery, internal-protocol failures, and runtime/output recovery consume no action-format budget.

## 8. Diagnostic Behavior

Raw-source diagnostics retain code, severity, source field, location, observed token, optional artifact handle, and an explicit non-authoritative/effect marker. They remain available on the materialized action and Controller action state for audit and observability. The same diagnostic fingerprint does not invalidate the action and cannot cause terminal no progress.

The implementation intentionally does not attempt to prove whether a raw token occurs in a shell argument, expansion fallback, comment, quote, or heredoc. It records the occurrence and defers authority to typed state.

## 9. Regression Tests Added/Updated

- Both minimized `session_1787622048` candidates use canonical `BIOMIND_INPUT_REFERENCE_FASTA`, `BIOMIND_INPUT_ANNOTATION_GTF`, and output environment bindings while retaining `Custom genome from provided genome.fasta and genome.gtf` in raw source.
- Comment, `echo`, `printf`, heredoc, description variable, direct literal, raw placeholder, and shell fallback cases produce diagnostics without hard violations.
- Typed canonical path, typed basename, structured environment literals, atomic/embedded placeholder, unknown handle, wrong direction, and output-coverage checks remain strict.
- Representation failures still commit `ACTION_FORMAT_REPAIR` and increment only its budget.
- Generated semantic failures commit bounded corrective Generate and increment only the per-step candidate budget.
- Source diagnostics continue budget-neutral; missing domain, dependency, and internal-protocol failures do not use the format budget.
- The real graph/controller replay proves validation pass, pending action persistence, Execute eligibility, and completion without terminal failure.
- Raw Controller audit findings are non-authoritative, while structured command identity findings remain authoritative.

## 10. Test Results

The repository's `tests/conftest.py` compatibility stub conflicts with the installed `langgraph` unless the real `langchain_core.messages` and `langchain_core.callbacks` modules are imported before pytest. The targeted suite was therefore executed through the project Python environment with those modules preloaded; no test harness files were changed.

```text
Group 1
tests/test_plan_local_preaction_convergence_v2.py
tests/test_session_controller_recovery_contract.py
tests/test_generate_corrective_repair_phase1.py
Result: 46 passed, 31 subtests passed

Group 2
tests/test_managed_artifact_contract_v2.py
tests/test_generate_script_artifact_repair_convergence.py
tests/test_managed_action_env_v2.py
tests/test_recovery_policy_ownership.py
Result: 46 passed, 20 subtests passed

Combined result: 92 passed, 51 subtests passed
```

Production syntax validation passed for all five modified production modules. A Git-index-only archive was then tested independently and reproduced the same 92 passed tests and 51 passed subtests, proving that the isolated commit snapshot does not depend on unrelated unstaged hunks.

## 11. Incident Replay Result

The minimized graph replay named `session-1787622048-prepare-snpeff-config` retained canonical managed environment use, literal fallback text, and the incident heredoc sentence. Deterministic materialization passed; `pending_action` was persisted; Execute was visited; the task completed; `action_format_repair_count` remained 0; `generate_attempts_by_step` remained empty; and a `MANAGED_LITERAL_IN_FREE_SOURCE` diagnostic remained visible in the event evidence.

Separate materializer regressions cover both incident candidates, including the second candidate's `test -s` and `ln -sfn` operations. Both have no hard artifact violations and are Execute-eligible.

## 12. Remaining Risks

- Raw lexical diagnostics can contain false positives by design. They are observability signals, not shell-semantic facts.
- Downstream reporting code must preserve `authoritative=false`; future consumers must not reconstruct a hard gate from diagnostic severity alone.
- Corrective Generate remains model-dependent but is bounded by the existing candidate-repair limit and an identical-candidate convergence guard.
- The repository contains extensive pre-existing tracked and untracked work. Only task hunks may be included in the implementation commit.

## 13. Explicit Non-Goals Preserved

This implementation did not change:

- `execution/shell_safety.py` or `execution/confirmation.py`;
- the snpEff skill, Java runtime, scientific inputs, TaskSpec, Planner contract, or Executor `set -e` behavior;
- action confirmation policy or shell safety policy;
- repair limits by increasing `max_format_repairs`;
- raw source through a new parser or by rewriting/removing user-authored script bytes.

## 14. Git Delta

Baseline HEAD: `483b68b4cf362c8d321106c8382da79f7096a894` on `codex/save-current-work`.

The worktree was already substantially dirty before this task, including pre-existing hunks in `agent_core/managed_artifacts.py`, `agent_core/plan_tracker.py`, `bio_agents.py`, `tests/test_managed_artifact_contract_v2.py`, and `tests/test_plan_local_preaction_convergence_v2.py`. Interactive hunk isolation produced a task-only staged delta of 12 files, 904 insertions, and 397 deletions; `git diff --cached --check` passed. The unrelated tracked and untracked working-tree content remains unstaged. Neither `git add .` nor `git add -A` was used.

Commit disposition: `TASK_ONLY_COMMIT_CREATED`.

Commit target:

```text
fix(controller): separate artifact diagnostics from hard validation
```

## Final Required Answers

Q1. Can arbitrary basename text in free Bash source still terminal-block Execute?

**NO**

Q2. Can typed structured artifact identity violations still block?

**YES**

Q3. Does a managed semantic violation consume `ACTION_FORMAT_REPAIR`?

**NO**

Q4. Does a true malformed action representation consume `ACTION_FORMAT_REPAIR`?

**YES**

Q5. Does a raw source diagnostic consume any terminal repair budget?

**NO**

Q6. Was a Shell AST/parser introduced?

**NO**

Q7. Does the regression for `session_1787622048` allow `prepare_snpeff_config` to reach Execute eligibility?

**YES**
