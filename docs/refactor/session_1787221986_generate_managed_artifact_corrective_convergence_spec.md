# BioMind Generate Script / Managed-Artifact Corrective Convergence Specification

## Status

- **Document type:** implementation specification.
- **Implementation status:** implemented and covered by focused/unit plus real-graph regression tests in the associated change.
- **Incident anchor:** `session_1787221986`.
- **Audit snapshot:** branch `codex/save-current-work`, HEAD `516db73aeadd81a2bebe567b80b75b996237c757` (`refactor: stabilize execution recovery contracts`).
- **Working-tree boundary:** the checkout was already dirty when this audit started. A future implementation must preserve all unrelated tracked and untracked changes.
- **Date:** 2026-08-20.

## 1. Executive decision

`session_1787221986` did not fail in Planner, input validation, Java, snpEff, database construction, or command execution. Planner produced an approved plan, the three biological inputs were verified, the resolved Java/snpEff runtime passed health checks, and the process boundary was never crossed.

The failure is a **Generate corrective-context convergence defect** at the boundary between the provider-facing `script_block_v1` protocol and the Controller's internal managed-artifact action protocol:

1. Generate may return only one executable `<shell>`, `<python>`, or `<r>` block.
2. In that block, the provider must consume Controller-supplied `BIOMIND_*` environment variables.
3. Only the Controller adapter may bind those variables to internal managed placeholders such as an input or output handle placeholder.
4. The first generated script added literal-path/basename fallbacks to the `BIOMIND_*` variables and was correctly rejected.
5. The rejection text incorrectly told a script-only provider to use an internal placeholder in `code`.
6. Corrective Generate followed that exact instruction and embedded the placeholder in the script, which is also correctly forbidden.
7. The corrective budget was then exhausted before Execute.

The repair must therefore make the managed-artifact diagnostic and repair projection **protocol-aware**, hide Controller-only placeholder bindings from the script provider, preserve the current validation gates, and expose both the primary and terminal repair failures in durable diagnostics.

This specification explicitly rejects the following shortcuts:

- allowing managed placeholders inside free-shell/script text;
- accepting literal input/output paths or literal basename fallbacks;
- rewriting model-authored scripts inside the Controller;
- increasing retry budgets to mask a contradictory prompt;
- changing Planner V2, TaskSpec, or the approved artifact ledger;
- bypassing the pre-action admission gate.

## 2. Verified incident facts

### 2.1 Planning and runtime were healthy

The incident artifacts under:

```text
benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787221986
```

show all of the following:

- `plan_status=approved` and `approval_status=approved`;
- `task_state.planning_failure={}`;
- exactly one successful Planner request, with no full-replan loop;
- the VCF, FASTA, and GTF existed, were readable, and were non-empty;
- the FASTA/GTF compatibility audit found two shared contigs;
- runtime health checks resolved a usable snpEff JAR and Java 21 runtime;
- `commands=0`, `successful_commands=0`, and `execution_started=false`.

Consequently, the absent annotation output is a downstream consequence of pre-action rejection, not evidence of a failed snpEff command.

### 2.2 First Generate candidate

The first response used constructs equivalent to:

```bash
VCF="${BIOMIND_INPUT_VARIANT_VCF:-<literal-vcf-path>}"
FASTA="${BIOMIND_INPUT_REFERENCE_FASTA:-<literal-fasta-path>}"
GTF="${BIOMIND_INPUT_ANNOTATION_GTF:-<literal-gtf-path>}"
```

`adapt_script_block_to_action()` correctly detected the three environment-variable references and constructed an internal action whose `env` mapped those variables to Controller-owned managed placeholders. This part of the adapter worked as designed.

`materialize_managed_action()` then correctly rejected the literal fallbacks with `MANAGED_ARTIFACT_HANDLE_REQUIRED`. The defect was not rejection itself; it was the provider-facing repair instruction:

```text
The action uses literal '<basename>'; use <managed-placeholder> for managed artifact ...
```

That instruction is valid for a provider authoring structured `argv`/`env` JSON, but invalid for a provider constrained to return only an executable script block.

### 2.3 Corrective request and second candidate

The corrective request contained two incompatible authorities:

- the stable `script_block_v1` prompt said to use the `artifact_environment` `BIOMIND_*` variable and not invent paths;
- the exact violation in `<action_repair>` said to put the internal managed placeholder in `code`.

The second response followed the more local exact violation and changed the fallbacks to forms equivalent to:

```bash
VCF="${BIOMIND_INPUT_VARIANT_VCF:-<managed-input-placeholder>}"
FASTA="${BIOMIND_INPUT_REFERENCE_FASTA:-<managed-input-placeholder>}"
GTF="${BIOMIND_INPUT_ANNOTATION_GTF:-<managed-input-placeholder>}"
```

This was a different candidate, not a repeated identical response. It was rejected by the existing atomicity rules as:

- `MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED`; and
- `MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL`.

Those rejections are correct and must remain.

### 2.4 Terminal observability was incomplete

`record_preaction_contract_failure()` intentionally implements first-write-wins for the primary failure. On later attempts it updates counters and the last candidate fingerprint but not the latest violation payload. `RunProfiler` then prioritizes that durable first failure.

As a result, the terminal report continued to display `MANAGED_ARTIFACT_HANDLE_REQUIRED`, while the actual last candidate had progressed to a different, later violation. Preserving the primary causal failure is correct; omitting the terminal repair failure is not.

## 3. Root-cause classification

### 3.1 Immediate model defect

The initial provider response violated the stable script contract by adding literal path/basename fallbacks. A valid initial script should have consumed the environment variables directly.

### 3.2 Architectural root cause

One generic managed-artifact violation currently serves three different purposes:

- internal validation evidence;
- durable user-facing diagnosis; and
- provider-facing corrective instruction.

Those purposes have different projection rules. In particular, a placeholder is a legal complete scalar in `semantic_action_v2` structured `argv`/`env`, but is Controller-owned and illegal in `script_block_v1` script text. The current violation message does not encode that ABI distinction.

### 3.3 Corrective-convergence defect

The recovery projection copies `recovery_context.validation.violations` and the internal `structured_candidate` into the next Generate request without a script-protocol projection. This leaks internal `env` placeholder bindings into a provider that cannot author them and allows an exact generic message to override the stable script guidance.

### 3.4 Observability defect

The durable state records only an immutable primary failure plus attempt metadata. It lacks a typed latest/terminal failure capsule, so the final projection cannot explain the repair transition.

## 4. Required authority model

The implementation must enforce this one-way boundary:

```text
TaskSpec / approved artifact ledger
        |
        v
Controller internal artifact environment
  BIOMIND_* -> managed placeholder
        |
        | provider sees env names only
        v
Generate script_block_v1
  reads BIOMIND_* directly
        |
        v
adapt_script_block_to_action
  injects referenced placeholders into internal action.env
        |
        v
materialize_managed_action
  resolves complete env scalar placeholders to canonical paths
        |
        v
Execute
```

The provider does not own, serialize, or place managed placeholders. The adapter does not invent a reference that the script did not author. The materializer does not rewrite shell, Python, or R source.

### 4.1 Valid provider forms

For an environment row whose name is `BIOMIND_INPUT_VARIANT_VCF`, valid direct references include:

```bash
VCF="$BIOMIND_INPUT_VARIANT_VCF"
```

```python
import os
vcf = os.environ["BIOMIND_INPUT_VARIANT_VCF"]
```

```r
vcf <- Sys.getenv("BIOMIND_INPUT_VARIANT_VCF")
```

### 4.2 Invalid provider forms

All of the following remain invalid:

- a canonical path or required basename written in script text;
- an environment-variable default whose fallback is a literal path/basename;
- an environment-variable default whose fallback is a managed placeholder;
- a managed placeholder anywhere in free-shell, Python, or R source;
- a guessed path used when the required `BIOMIND_*` variable is absent;
- a Controller-injected env binding for an artifact variable the script never references.

Failing closed when a required environment variable is unavailable is allowed; silently falling back to a path is not.

## 5. Provider-visible context contract

### 5.1 Split internal and provider artifact-environment views

The current `script_artifact_environment()` rows contain both the provider-facing `env_var` and the Controller-only `placeholder`. They are reused for prompt construction and adapter binding. This dual use must be split.

Required internal view:

```json
{
  "semantic_handle": "input.variant_vcf",
  "direction": "input",
  "env_var": "BIOMIND_INPUT_VARIANT_VCF",
  "placeholder": "<controller-owned managed binding>",
  "required": true,
  "format": "vcf",
  "artifact_kind": "file"
}
```

Required provider view:

```json
{
  "semantic_handle": "input.variant_vcf",
  "direction": "input",
  "env_var": "BIOMIND_INPUT_VARIANT_VCF",
  "required": true,
  "format": "vcf",
  "artifact_kind": "file"
}
```

The full internal rows must continue to be passed to `adapt_script_block_to_action()`. Only the placeholder-free projection may enter `NodeContext.runtime.artifact_environment` for `script_block_v1`.

The split must be explicit in names and types. Do not mutate the list after it has been passed to the prompt, and do not rely on callers remembering to `pop("placeholder")` ad hoc.

### 5.2 Project the previous candidate back to its provider ABI

For corrective `script_block_v1`, `previous_candidate` must contain only the provider-authored surface:

```json
{
  "protocol": "script_block_v1",
  "language": "bash",
  "script": "<exact previous script body>"
}
```

It must not contain:

- adapted `argv`;
- internal `env` placeholder values;
- `controller_artifact_environment`;
- runtime snapshot internals;
- materialized paths.

This is a projection, not a rewrite: the exact prior script body and language must be preserved.

### 5.3 Protocol-aware exact violations

For structured `semantic_action_v2`, the current atomic-placeholder guidance remains valid: a managed placeholder may be the complete value of one `argv` or `env` scalar.

For `script_block_v1`, every managed-artifact violation projected to Generate must instead identify:

- the stable violation code;
- the semantic artifact handle;
- the script language;
- the corresponding `BIOMIND_*` env name;
- the provider-language reference form;
- the forbidden form that was observed;
- the narrow preservation rule.

Example provider repair capsule:

```json
{
  "code": "MANAGED_ARTIFACT_HANDLE_REQUIRED",
  "field_path": "script",
  "artifact_handle": "input.variant_vcf",
  "repair_hint": {
    "protocol": "script_block_v1",
    "strategy": "replace_literal_fallback_with_controller_env_reference",
    "env_var": "BIOMIND_INPUT_VARIANT_VCF",
    "provider_reference": "$BIOMIND_INPUT_VARIANT_VCF",
    "rule": "Use the Controller environment variable directly; remove the literal/default fallback and do not place a managed placeholder in the script."
  }
}
```

For Python and R, `provider_reference` must use the language-appropriate forms from section 4.1.

The stable error codes should be retained for backward compatibility. In particular, do not introduce a second validator code merely to change English wording. The mode distinction belongs in the typed `repair_hint` and provider projection.

### 5.4 No Controller-introduced placeholder leakage

When `generate_output_contract=script_block_v1`, Controller-generated prompt surfaces must not introduce a concrete managed placeholder into:

- `runtime.artifact_environment`;
- corrective `previous_candidate`;
- provider-facing `exact_violations` or repair hints;
- the `<managed_artifact_action_repair>` SystemMessage;
- script-protocol examples.

This rule does not authorize altering verbatim user text. It applies to Controller-generated projections only.

## 6. Validator and corrective-projection changes

### 6.1 `agent_core/controller_audit.py`

Add explicit helpers for the two views and for language-specific env references. One acceptable shape is:

```python
script_artifact_environment(...)                 # internal binding rows
project_script_artifact_environment_for_provider(...)
script_env_reference(env_var, language)
project_script_repair_candidate(...)
```

Exact names may vary, but ownership must remain centralized and deterministic. `adapt_script_block_to_action()` must continue to receive internal rows and must continue binding only variables actually referenced in the script.

### 6.2 `agent_core/managed_artifacts.py`

Keep all current admission gates. Make messages and `repair_hint` protocol-aware when the proposal has `script_adapter="script_block_v1"` and a matching Controller artifact-environment row.

At minimum, normalize these code paths:

- `MANAGED_ARTIFACT_HANDLE_REQUIRED` from literal path/basename use in `code`;
- `ACTION_OUTPUT_BINDING_MISMATCH` from a literal managed output in `code`;
- `MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED` when the embedded value belongs to the adapted script source/argv;
- `MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL`.

For script mode, their message and repair hint must direct the provider to the matching environment variable, not to an internal placeholder. For structured action mode, existing placeholder guidance remains unchanged.

If a script-mode violation cannot be mapped to exactly one artifact-environment row, fail closed as an internal Controller projection defect. Do not guess an env name from a basename at repair time.

### 6.3 `bio_agents.py`

At initial Generate prompt construction:

- retain the full internal artifact environment for the post-response adapter;
- pass only the provider view to `build_node_context()`.

In `_action_repair_projection()`:

- detect `script_block_v1` from the adapted candidate/recovery action contract;
- project the previous candidate back to `{protocol, language, script}`;
- project exact violations through the script-aware repair capsule;
- exclude internal env mappings, runtime internals, and canonical paths;
- preserve the existing `repair_instruction`, allowed inputs/outputs, and current-step scope.

At the pre-action rejection return:

- replace the hard-coded sentence `Use the required semantic handle placeholder in structured argv/env` with protocol-aware feedback;
- use the same normalized repair capsule that enters the next Generate request;
- never tell a script-only provider to author structured JSON or an internal placeholder.

No branch routing, retry count, repair ordinal, or recovery enum is changed by this work.

### 6.4 `prompts/manager.py`

For `script_block_v1`, make the stable contract explicit for shell, Python, and R:

- use the listed `BIOMIND_*` variable through the language's environment API;
- do not write a canonical path or basename fallback;
- do not write Controller-owned managed placeholders in the executable block;
- return only the single executable block.

During corrective Generate:

- state that `repair_hint` is the provider-facing authority for the reported violation;
- preserve unrelated valid script content;
- do not show structured-action placeholder examples in the script branch;
- do not append a generic placeholder rule that contradicts the script ABI.

The prompt manager must not reconstruct env names from semantic handles. It consumes the Controller's deterministic provider projection.

## 7. Durable failure and terminal-diagnostic contract

### 7.1 Preserve primary failure compatibility

The existing top-level `plan_state.failure` fields remain the immutable primary causal diagnosis:

```json
{
  "error_code": "MANAGED_ARTIFACT_HANDLE_REQUIRED",
  "message": "<primary message>",
  "fingerprint": "<primary error fingerprint>",
  "candidate_fingerprint": "<first candidate fingerprint>"
}
```

Existing readers that rely on first-write-wins must continue to work.

### 7.2 Add latest and terminal capsules in the same durable owner

Every call to `record_preaction_contract_failure()` must also update a typed `latest_failure` nested in the same `failure` object:

```json
{
  "latest_failure": {
    "error_code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
    "owner": "GENERATED_ACTION",
    "subsystem": "managed_artifacts",
    "stage": "pre_action_contract",
    "message": "<latest message>",
    "fingerprint": "<latest error fingerprint>",
    "candidate_fingerprint": "<latest candidate fingerprint>",
    "field_path": "<latest field path>",
    "artifact_handle": "<latest handle>",
    "recorded_at": "<timestamp>"
  }
}
```

When `termination_reason` is set, copy that latest capsule to `terminal_failure` and record the termination reason and repair ordinal/count. The top-level primary fields are not overwritten.

Do not create a second top-level failure owner or an unbounded history. Primary plus latest/terminal is sufficient for causal explanation and bounded persistence.

### 7.3 Terminal projection

`RunProfiler._durable_terminal_projection()` and `_terminal_diagnostic_text()` must render both layers:

- primary error code/detail/fingerprint;
- terminal repair error code/detail/fingerprint;
- termination reason;
- first and last candidate fingerprints;
- repair attempt count;
- `execution_started=false`.

For compatibility, `terminal_diagnostic.error_code` may remain the primary causal code. Additive typed fields such as `terminal_failure`/`latest_failure` must make the final failed repair visible, and the human-readable output must include it when it differs from the primary failure.

The summarized top-level `run_metrics.failure` projection must not discard the nested terminal capsule. Planner terminal diagnostics, Consultant FINALIZE diagnostics, and repaired failures that later cross the process boundary must retain their current precedence behavior.

For this incident, the final explanation must be equivalent to:

```text
Primary: MANAGED_ARTIFACT_HANDLE_REQUIRED — the initial script used literal fallbacks.
Corrective terminal: MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED — the repair put Controller placeholders into script text.
Termination: action_format_repair_budget_exhausted; execution_started=false.
```

## 8. Recovery behavior that must remain unchanged

- One invalid generated action never reaches Execute.
- The current action-format repair budget and repair ordinal semantics remain unchanged.
- A byte/contract-identical repaired candidate may still terminate as `repair_no_progress`.
- A changed candidate with a new violation may still terminate as `action_format_repair_budget_exhausted` when no budget remains.
- Dependency-owned and Controller-internal failures retain their current routes.
- Recovery does not modify TaskSpec, Plan, approval, selected skill, runtime snapshot, or artifact ledger.
- No model call is added solely to translate a validation error.

The intended convergence comes from supplying one coherent repair contract, not from granting more retries.

## 9. Implementation map

| File | Required change |
| --- | --- |
| `agent_core/controller_audit.py` | Split internal/provider artifact-environment views; centralize language-specific env references and script repair projection. |
| `agent_core/managed_artifacts.py` | Preserve gates and codes; emit script-aware messages/repair hints for literal and placeholder-in-script violations. |
| `bio_agents.py` | Keep internal bindings out of provider context; project previous script and exact violations to the script ABI; remove generic structured-placeholder feedback for script repairs. |
| `prompts/manager.py` | Strengthen stable script rules and consume the normalized script repair capsule without structured-action examples. |
| `agent_core/plan_tracker.py` | Retain immutable primary failure and add bounded latest/terminal failure capsules. |
| `execution/profiling.py` | Preserve and render primary plus terminal repair failures without changing unrelated diagnostic precedence. |
| `tests/test_managed_artifact_contract_v2.py` | Add protocol-aware validator and no-regression cases. |
| `tests/test_session_controller_recovery_contract.py` | Add internal/provider projection and adapter-boundary tests. |
| `tests/test_preaction_failure_terminal_v2.py` | Add primary/latest/terminal persistence and profiler-rendering tests. |
| `tests/test_plan_local_preaction_convergence_v2.py` or a focused new integration test | Replay the two-response incident through Generate/recovery and prove convergence/no execution leakage. |

## 10. Required test matrix

### 10.1 Environment projection

1. Internal rows contain the managed placeholder required by the adapter.
2. Provider rows contain the same semantic handle and `BIOMIND_*` env name but no placeholder or canonical path.
3. Prompt construction uses provider rows.
4. Adapter construction uses internal rows.
5. Optional artifact env entries are injected into internal action `env` only when referenced by the script.

### 10.2 Valid scripts

For shell, Python, and R:

1. direct environment-variable access is detected;
2. the adapter adds the matching internal env placeholder;
3. materialization resolves the complete env scalar;
4. no managed-artifact violation is produced;
5. the script body is byte-for-byte unchanged.

### 10.3 Invalid literal fallbacks

For `${BIOMIND_INPUT_X:-literal}`, plus language-equivalent literal fallbacks:

1. validation rejects the candidate before Execute;
2. the stable code remains `MANAGED_ARTIFACT_HANDLE_REQUIRED` for an input;
3. the script repair hint names the exact env variable and language-specific reference;
4. the hint says to remove the fallback;
5. provider-facing text does not instruct insertion of a managed placeholder.

### 10.4 Invalid placeholders in script text

1. embedded placeholders remain `MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED`;
2. free-shell placeholders remain `MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL` where applicable;
3. script-mode repair says to use the existing env variable directly;
4. structured-action mode retains the complete-scalar `argv`/`env` rule;
5. neither mode weakens atomicity.

### 10.5 Corrective request shape

Given an adapted `script_block_v1` candidate with internal `argv`, `env`, runtime snapshot, and Controller artifact rows:

1. `previous_candidate` contains only protocol, language, and the exact script;
2. exact violations contain the script-aware repair capsule;
3. the Controller introduces no concrete managed placeholder into the script-provider request;
4. the request contains the matching `BIOMIND_*` env name;
5. it contains no instruction to return JSON or structured `argv`/`env`;
6. current step, allowed inputs/outputs, and preservation rules remain present.

### 10.6 Incident convergence replay

Use a small sanitized fixture based on `session_1787221986`:

1. mocked first Generate response uses three literal fallbacks;
2. pre-action validation rejects it and zero commands execute;
3. the next request instructs direct `BIOMIND_*` access and removal of fallbacks;
4. mocked second response uses direct env references only;
5. adapter and materializer accept the corrected action;
6. exactly one approved action reaches the mocked Execute boundary;
7. the Plan, TaskSpec, runtime snapshot, and artifact identities are unchanged.

This is the decisive graph-level regression. Helper-only tests are insufficient because the original defect arose from the composition of prompt, adapter, validator, recovery projection, and terminal persistence.

### 10.7 Failure progression and terminal reporting

Record first `MANAGED_ARTIFACT_HANDLE_REQUIRED`, then `MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED` with termination:

1. top-level primary fields remain first-write-wins;
2. `latest_failure` and `terminal_failure` contain the second code and fingerprints;
3. first and last candidate fingerprints differ;
4. run metrics report `commands=0` and `execution_started=false`;
5. final human-readable output mentions both codes and the termination reason;
6. an identical second candidate still reports `repair_no_progress` correctly.

### 10.8 Unrelated regression protection

Run existing tests for:

- managed-artifact atomicity;
- script adapter environment detection;
- plan-local pre-action convergence;
- durable pre-action terminal projection;
- Planner full-replan convergence;
- Consultant FINALIZE terminal diagnostics;
- runtime output reconciliation.

## 11. Implementation sequence

1. **Projection primitives:** add internal/provider environment views and script-language reference helpers with unit tests.
2. **Validator diagnostics:** add protocol-aware messages/repair hints without changing validation decisions or codes.
3. **Generate context:** remove Controller-only fields from initial and corrective script requests; project the previous script faithfully.
4. **Prompt contract:** strengthen script rules and remove conflicting structured-action feedback.
5. **Durable observability:** add latest/terminal capsules and update profiler projection/rendering.
6. **Graph replay:** add the sanitized incident test and prove zero execution before a valid candidate.
7. **Focused regression suite:** run the files in section 10.8, then the broader test suite if the focused set passes.

Do not combine this work with Planner, TaskSpec, artifact-identity, or runtime-resolution refactors. Narrow commits by contract layer are preferred so each change can be reviewed and reverted independently.

## 12. Acceptance criteria

The implementation is complete only when all of the following are true:

- a valid direct-`BIOMIND_*` script still adapts and materializes successfully;
- a literal/default fallback is still rejected before execution;
- the resulting script-mode repair instruction names the corresponding env variable and never asks the provider to insert an internal placeholder;
- a corrective script using the direct env variable passes without Controller source rewriting;
- structured `semantic_action_v2` placeholder behavior is unchanged;
- the actual corrective provider request contains no Controller-introduced placeholder binding;
- recovery budgets and routing are unchanged;
- terminal state preserves the first causal failure and the last repair failure;
- final output explicitly states that no execution occurred when the repair budget is exhausted pre-action;
- the incident convergence replay crosses Execute exactly once, only after the valid repaired candidate;
- focused and graph-level regression tests pass;
- unrelated dirty-worktree changes remain untouched.

## 13. Non-goals

This change does not:

- modify Planner V2 prompt assembly or full-replan convergence;
- modify canonical TaskSpec semantics or output coverage;
- change approved-plan snapshots or managed-artifact identity rules;
- loosen literal-path, basename, placeholder-atomicity, or output-binding checks;
- redesign Recovery decisions, retry budgets, or Supervisor routing;
- add automatic script rewriting or regex-based repair;
- change Java, snpEff, database, FASTA, GTF, or VCF handling;
- retroactively mutate historical session artifacts;
- perform a live biological annotation run as part of implementation.

## 14. Rollback boundary

If the change causes regressions, rollback may independently remove:

1. provider-safe script environment/repair projection;
2. script prompt wording changes;
3. additive latest/terminal diagnostic fields.

Rollback must not remove or weaken the existing validation gates. Because top-level primary failure fields and stable violation codes remain compatible, the additive diagnostic schema can be reverted without migrating historical `plan_state.json` files.

## 15. Done definition

“The sample task happens to pass” is not sufficient. Done means the provider and Controller have one unambiguous ABI at every stage:

- the script provider sees and authors environment references;
- the adapter alone owns placeholder injection;
- the materializer alone owns canonical path resolution;
- corrective context is projected back into the provider's ABI;
- durable diagnostics explain both the original invalid action and the final repair outcome.

Only after those contracts are covered by helper, projection, persistence, and graph-level tests should the same biological task be rerun as an acceptance check.
