# BioMind Managed-Artifact Pre-Execution Validator / Repair Routing Boundary Audit

Status: **READ-ONLY ARCHITECTURE AUDIT**

Date: 2026-08-25

Incident: `session_1787622048`

Audit baseline: `483b68b4cf362c8d321106c8382da79f7096a894`

## Audit baseline and worktree boundary

The checkout inspected for this report was:

```text
HEAD:   483b68b4cf362c8d321106c8382da79f7096a894
Branch: codex/save-current-work
Tracked dirty files: 54
Untracked paths: 66
Unstaged diff stat: 54 files changed, 8696 insertions(+), 2832 deletions(-)
Staged diff: empty
```

The repository was already heavily dirty. This audit treats HEAD plus the current working-tree changes as the live checkout and does not attribute unrelated changes to this task. The whole-source basename branch at `agent_core/managed_artifacts.py:1319-1370` is present in the current checkout and is unchanged in substance from the stated HEAD baseline. No production source, prompt, skill, runtime, input, session artifact, or test was modified. The only file added by this task is this report.

Evidence inspected:

- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/plan.md`
- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/plan_state.json`
- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/run_metrics.json`
- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/context_trace/events.jsonl`
- `context_trace/generate-0012-81709eaeb9ed/{request,response,manifest}.json`
- `context_trace/generate-0013-654dabb75915/{request,response,manifest}.json`
- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/recovery/recovery_context.json`
- `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787622048/recovery/llm_results.json`
- Current validator, routing, fingerprint, persistence, execution-safety, and regression-test code cited below.

No workflow, snpEff command, Java command, generated candidate, or test suite was re-executed. This is an evidence and code-path audit only.

## 1. Executive Conclusion

The first real fault in `session_1787622048` is a Controller false positive, not a snpEff, Java, input-data, contig-compatibility, runtime-contract, or Executor failure.

Both Generate candidates correctly referenced all three current-step managed bindings:

```text
input.annotation_gtf
  -> BIOMIND_INPUT_ANNOTATION_GTF

input.reference_fasta
  -> BIOMIND_INPUT_REFERENCE_FASTA

output.plan-artifact-08155249a1edac79ca526d62
  -> BIOMIND_OUTPUT_PLAN_ARTIFACT_08155249A1EDAC79CA526D62
```

The rejected text was the data written into the snpEff configuration heredoc:

```text
Custom genome from provided genome.fasta and genome.gtf
```

`materialize_managed_action()` scans the complete model-authored `code` string. For every `MANAGED_EXACT` binding it searches either the canonical absolute path as an arbitrary substring or the required basename with a boundary regex (`agent_core/managed_artifacts.py:1271-1370`). That scan does not distinguish a command argument from a comment, log string, variable description, quoted value, or heredoc body. It therefore converted two ordinary descriptive names into two `MANAGED_ARTIFACT_HANDLE_REQUIRED` violations.

This violates the correct Controller boundary:

> Controller may hard-block facts proven by typed action fields, approved state, exact binding identity, materialization, and dependency availability. It must not turn an ambiguous source-text occurrence into terminal contract evidence.

Recommendation: **Option B**.

- Keep basename/path detection only as non-authoritative telemetry for free-form script source.
- Hard-block only structured, deterministic violations.
- Do not consume `ACTION_FORMAT_REPAIR` for managed-artifact policy/semantic rejection as a category.
- Reserve `ACTION_FORMAT_REPAIR` for representation/protocol failures such as malformed script-block framing, invalid `argv`/`env` shape, or an unparseable action envelope.
- Route structurally proven Generate-owned semantic mismatches to a bounded `CORRECTIVE_GENERATE` path.
- Route dependency facts to recovery and Controller projection/materialization invariants to `internal_protocol` without spending model repair budget.
- Do not add a Shell AST/parser for this fix.

This retains the deterministic managed-artifact contract while removing the mechanism that caused the false terminal failure.

## 2. Incident Reconstruction

### 2.1 What completed before the fault

`plan.md:13-65` marks the first four steps complete:

1. `validate_inputs`
2. `inspect_vcf`
3. `inspect_reference`
4. `check_contig_compatibility`

`run_metrics.json.summary` records four commands, all four successful, zero failed commands, and 2.456 seconds of command execution. `events.jsonl` shows Generate → Execute → Supervisor transitions for those steps. The next step, `prepare_snpeff_config`, remained current (`plan.md:66-78`). No event routed that step to Execute.

Consequently, the incident cannot be evidence of a snpEff database-build or annotation failure: neither downstream step started.

### 2.2 Exact timeline

Times below use Asia/Shanghai; the trace stores the same events in UTC.

| Time | Event | Evidence/result |
|---|---|---|
| 09:42:59 | Planner completes | `plan.md` generated; trace routes Planner to plan review. |
| 09:43:50 | Plan review completes | Trace routes approved plan to Generate. |
| 09:44:46–10:06:57 | First four steps run | Four successful command records; current step advances to `prepare_snpeff_config`. |
| 10:07:01 | Candidate 1 request starts | Call `generate-0012-81709eaeb9ed`. |
| 10:07:46 | Candidate 1 response completes | Correct BIOMIND input/output variables; descriptive heredoc contains `genome.fasta` and `genome.gtf`. |
| 10:07:47 | Candidate 1 pre-action rejection | Candidate fingerprint `85cb2ff4700a709680bec1a63e7510824b1008fedf4440141cb10d4c087e7efd`; error fingerprint `2cc7982d28dfd5f54a0fc0f5c983d758b6b9c82cf7d2aaa439128e60eb1bb836`; ordinal 1. |
| 10:07:48 | Repair committed | `ACTION_FORMAT_REPAIR` increments the per-step format count from 0 to 1; trace routes Generate back to Generate. |
| 10:07:51 | Candidate 2 request starts | Call `generate-0013-654dabb75915`. |
| 10:08:43 | Candidate 2 response completes | Inputs are now referenced directly, but the same legitimate description remains. |
| 10:08:44 | Candidate 2 pre-action rejection | Candidate fingerprint changes to `6fdcf81b4b92916778da2a51be1b230a79fef03e43685aa64e309eb51d290269`; error fingerprint remains `2cc7982d...`; ordinal 2. |
| 10:08:44 | Repair policy escalates | Per-step `max_format_repairs=1` is exhausted; termination reason becomes `action_format_repair_budget_exhausted`. |
| 10:08:45–10:08:47 | Terminal routing | `events.jsonl` records `terminal_failed`, Generate → finish, then Supervisor → finish. |

The run terminated itself through durable Controller state. It was not made terminal by the later user interruption.

### 2.3 Candidate 1 actual semantics

Candidate 1:

- Reads the managed output through `$BIOMIND_OUTPUT_PLAN_ARTIFACT_08155249A1EDAC79CA526D62`.
- Reads the managed FASTA through `$BIOMIND_INPUT_REFERENCE_FASTA`, assigns it to `fasta`, verifies it with `test -s`, and derives `fasta_abs` from that value.
- Reads the managed GTF through `$BIOMIND_INPUT_ANNOTATION_GTF`, assigns it to `gtf`, verifies it with `test -s`, and derives `gtf_abs` from that value.
- Creates snpEff staging symlinks from those derived managed input paths.
- Writes and verifies the managed configuration output.

`adapt_script_block_to_action()` represents this script as `bash -lc <body>` and derives the controller artifact environment from actual BIOMIND variable references (`agent_core/controller_audit.py:587-639`). The resulting structured candidate records all three `referenced_artifact_handles` and maps their environment variables to the three typed placeholders.

The literal strings `genome.fasta` and `genome.gtf` occur only in the human-readable `.genome` description inside the configuration heredoc. They are not the source operands of `test`, `ln`, or any other file-reading command.

### 2.4 Candidate 2 actual semantics

Candidate 2 makes the binding usage even more direct:

```bash
test -s "$BIOMIND_INPUT_REFERENCE_FASTA"
test -s "$BIOMIND_INPUT_ANNOTATION_GTF"
ln -sfn "$BIOMIND_INPUT_REFERENCE_FASTA" "$genome_dir/sequences.fa"
ln -sfn "$BIOMIND_INPUT_ANNOTATION_GTF" "$genome_dir/genes.gtf"
```

It retains the same valid managed output reference and the same descriptive heredoc line. The adapter again records all three expected handles. The second response therefore changed executable source and its candidate fingerprint, but did not and should not have removed truthful descriptive text.

### 2.5 Scope caveat

The scripts also create a task-local `data/custom_genome` layout and two symlinks derived from the output directory. This audit proves that the managed input and declared configuration-output identities are correctly bound and that the reported basename violation is false. It does not claim that the present managed-artifact validator structurally proves every incidental filesystem side effect of arbitrary Bash. That is a separate execution-sandbox/output-reconciliation concern and was not the trigger in this incident.

## 3. Current Validator Call Graph

The live Generate → validation → Execute path is:

```text
Generate LLM response
  -> parse_executable_script_block()
  -> adapt_script_block_to_action()
       -> script_references_env()
       -> argv = ["bash", "-lc", raw_body]
       -> env = {BIOMIND_*: typed placeholder}
       -> referenced_artifact_handles
  -> audit_generated_script()
       -> source-text runtime/artifact checks
  -> managed_action_fingerprint()
  -> load_approved_plan_snapshot()
  -> build_approved_visible_artifact_set()
  -> build_managed_artifact_bindings()
  -> materialize_managed_action()
       -> typed argv/env validation and materialization
       -> whole-code placeholder/path/basename scans       <-- first fault
  -> validate_materialized_artifacts()
  -> validate_generate_output_coverage()
  -> violation ownership branch
       INTERNAL_PROTOCOL -> persist + terminal
       DEPENDENCY        -> Supervisor recovery
       otherwise         -> _managed_preaction_recovery()
                             -> RecoveryPolicy(max_format_repairs=1)
                             -> FORMAT_REPAIR / ESCALATE
  -> if valid: persist pending_action
  -> Execute
       -> _inject_managed_action_environment()
       -> other boundary/safety/confirmation checks
       -> process start
```

Primary code anchors:

- Script parsing/adaptation: `bio_agents.py:25834-25835`; `agent_core/controller_audit.py:587-639`.
- Controller script audit: `bio_agents.py:25852-25950`; `agent_core/controller_audit.py:754-853`.
- Candidate identity and managed binding construction: `bio_agents.py:25959-26000`.
- Materialization and secondary validators: `bio_agents.py:25994-26033`.
- Ownership/routing branch: `bio_agents.py:26034-26309`.
- Pre-action recovery construction: `bio_agents.py:8111-8208`.
- Generic pre-action recovery policy: `agent_core/recovery.py:2502-2546`.
- Budget persistence: `agent_core/plan_tracker.py:2525-2563`.
- Durable failure persistence: `agent_core/plan_tracker.py:2755-2847`.
- Execute-time environment injection: `bio_agents.py:27430-27456`.

The decisive architecture fact is that the current script adapter intentionally preserves the raw provider body as executable `code`, while the managed-artifact layer later treats that entire body as if every basename occurrence were a path operand.

## 4. Managed-Artifact Validation Inventory

| Check | Current evidence | Current action | Classification | Recommended authority |
|---|---|---|---|---|
| Duplicate/unknown/wrong-direction `outputs` handle | Typed list entry and approved binding map (`managed_artifacts.py:992-1033`) | Violation | Deterministic | Hard-block; Generate-owned semantic correction. |
| Runtime placeholder in structured scalar | Exact contract token in `argv/env` (`1035-1063`) | Violation | Deterministic protocol token | Hard-block; representation/corrective route according to field ownership. |
| Unknown operation handle | Full-value typed placeholder plus pinned operation map (`1065-1080`) | Violation | Deterministic | Hard-block; corrective Generate. |
| Managed placeholder resolution/direction/runtime availability/path | Full-value placeholder plus binding/ledger facts (`1081-1133`) | Violation or exact materialization | Deterministic | Keep hard-block; dependency and internal cases keep their distinct owners. |
| Embedded placeholder in structured `argv/env` value | Exact placeholder token inside one typed scalar (`1134-1188`) | Violation | Deterministic protocol rule | Keep hard-block. |
| Literal path/basename as an entire structured scalar | Equality to canonical path or `Path(path).name` (`786-789`, `1190-1212`) | Violation | Deterministic identity comparison | Keep hard-block for typed semantic actions. |
| `argv` list and `env` object/key/value shapes | Python types and key regex (`1215-1269`) | Violation | Deterministic representation | Keep; route malformed shape to `ACTION_FORMAT_REPAIR`. |
| `{operation:*}` anywhere in raw `code` | Arbitrary substring (`1271-1283`) | Violation | Textual heuristic in free source | Do not use as terminal evidence; diagnostic or protocol-specific advisory. |
| Managed placeholder anywhere in raw `code` | Arbitrary substring (`1284-1318`) | Violation | Textual heuristic in free source | Downgrade unless the protocol supplies a structured token field. |
| Canonical absolute path anywhere in raw `code` | Arbitrary substring (`1319-1327`) | Violation | Textual heuristic | Remove as hard-block. |
| Required basename anywhere in raw `code` | Boundary regex over complete source (`1319-1370`) | Violation | Textual heuristic | Remove as hard-block; optional diagnostic only. |
| Current-step output coverage | Approved step outputs minus `referenced_output_handles` (`1408-1457`) | Violation | Deterministic for structured actions; heuristic-derived for script adapter | Hard-block when coverage comes from explicit typed declarations; advisory/corrective when inferred solely from source scanning. |
| Controller materialized handle/path equality | Controller-produced map against binding map (`1460-1498`) | Internal violation | Deterministic Controller invariant | Keep hard-block; `internal_protocol`, budget-neutral. |
| Runtime env key conflict | Set intersection with Controller runtime keys (`bio_agents.py:26001-26023`) | Violation | Deterministic | Keep; classify by the layer that authored the conflicting map. |
| Execute-time env injection validity | Typed map/key/value/conflict checks (`bio_agents.py:27430-27456`) | Pre-start rejection | Deterministic | Keep; a post-materialization inconsistency is normally Controller/internal, not another format repair. |

`validate_plan_managed_artifacts()` at `agent_core/managed_artifacts.py:826-904` is a separate plan-time check. It should not be conflated with the immediate Generate pre-action rejection at `materialize_managed_action()`.

## 5. Deterministic vs Heuristic Checks

### 5.1 Deterministic evidence

At this boundary, evidence is deterministic when the Controller can establish the proposition without interpreting program meaning. Examples are:

- The approved current-step artifact set contains or does not contain an identity.
- A typed placeholder is a complete `argv` or `env` value.
- A handle resolves to a binding with a known direction and canonical path.
- An input dependency is marked runtime-available or unavailable in approved state.
- A Controller materialization result equals or differs from the canonical binding.
- An `argv` field is not a non-empty list, an `env` field is not an object, or an env key is invalid.
- A typed structured scalar equals an authoritative canonical path/basename instead of using its handle.

These facts are appropriate hard-block inputs because their truth does not depend on Bash execution semantics.

### 5.2 Heuristic evidence

Evidence is heuristic when a substring or regex over free program source is used to infer what the program will do. Examples are:

- A basename occurs somewhere in the Bash body.
- A canonical path occurs inside a comment, string, heredoc, example, or data literal.
- An environment variable name occurs somewhere and is therefore assumed to be a meaningful artifact read.
- An executable name or `/dev/null` text occurs and is therefore assumed to be an invocation/redirection.

`agent_core/controller_audit.py` already documents the limitation of its quote masker: it is a “bounded lexical filter, not a shell parser” (`517-523`). `managed_artifacts.py` does not even apply that bounded filtering before its whole-code path/basename scan.

The governing rule should be:

> **No structural proof, no terminal pre-execution block.**

Heuristics remain useful for telemetry, warnings, repair suggestions, and test observability. They are not sufficient by themselves to mark a task failed.

## 6. Basename Detection Root Cause

### 6.1 How the basename is obtained

Bindings derive `required_basename` from explicit metadata or `Path(canonical_path).name` (`agent_core/managed_artifacts.py:236-266`). Plan-local outputs similarly derive it from their canonical path (`:678-739`). For this incident, the approved FASTA and GTF paths therefore yield:

```text
genome.fasta
genome.gtf
```

The derivation itself is valid semantic metadata. The fault is using that metadata as a raw-source hard-block pattern.

### 6.2 How the false match occurs

For every exact managed binding, lines `1319-1325` evaluate:

```python
(literal and literal in code) or re.search(<basename boundary regex>, code)
```

`code` is the whole original Bash body, also carried as `argv[2]` by the script adapter. There is:

- no language branch;
- no shell tokenization;
- no comment removal;
- no quote removal;
- no heredoc recognition;
- no distinction between a command operand and bytes being written to an output file.

The boundary regex contains explicit lookbehinds for Bash parameter-expansion operators (`:-`, `:=`, `:+`, `:?`), but that only widens the textual match. It does not prove that a match is an active fallback expression.

The loop then emits one violation for FASTA and one for GTF, both with `field_path="code"`, and labels each occurrence a “literal/default fallback” (`1335-1369`). That diagnosis is false for the incident heredoc.

### 6.3 Internal contradiction in the current path

Before the basename scan, `script_references_env()` finds the BIOMIND references and `adapt_script_block_to_action()` creates correct typed env mappings. In the rejected candidate saved in `recovery_context.json`, the structured action contains:

```text
env:
  BIOMIND_INPUT_ANNOTATION_GTF: {input:annotation_gtf}
  BIOMIND_INPUT_REFERENCE_FASTA: {input:reference_fasta}
  BIOMIND_OUTPUT_PLAN_ARTIFACT_...: {output:plan-artifact-...}

referenced_artifact_handles:
  input.annotation_gtf
  input.reference_fasta
  output.plan-artifact-08155249a1edac79ca526d62
```

The same Controller path therefore first recognizes the approved bindings and then rejects the candidate by reinterpreting unrelated prose. The rejection is not additional identity evidence; it is a conflicting heuristic layered after successful binding projection.

### 6.4 Required outcomes for the requested examples

Assuming the script also uses the required BIOMIND variables for actual file operations:

| Source form | Pre-execution result | Reason |
|---|---|---|
| `# source was genome.fasta` | PASS; optional diagnostic at most | Comment text is not a path operand. |
| `echo "Using provided genome.fasta"` | PASS | Logging text is not artifact identity. |
| `printf 'Reference genome.fasta validated\n'` | PASS | Output string is not artifact identity. |
| Heredoc containing `genome.fasta` / `genome.gtf` | PASS | Data being written is not proof of an unmanaged input read. This is the incident case. |
| `DESCRIPTION="genome.fasta and genome.gtf"` | PASS | Description assignment is ambiguous source text, not terminal evidence. |
| `tool genome.fasta` in a raw script | PASS with high-signal diagnostic/advisory corrective feedback | It is suspicious, but without a structured action field or shell parser the Controller still lacks the proof required for a terminal contract verdict. If the same value is a complete typed `argv` scalar, it should BLOCK deterministically. |
| `REFERENCE="${BIOMIND_INPUT_REFERENCE_FASTA:-genome.fasta}"` | PASS with diagnostic; no format-budget charge | The style is undesirable, but Controller materialization supplies a non-empty canonical BIOMIND value before Execute, so the fallback is dead under the managed runtime. An empty/missing injected value is a Controller/dependency invariant failure and must be caught structurally. |

The last two cases expose the trade-off honestly: removing a source heuristic can increase the chance that an ill-authored free script ignores the intended input. The correct answer is not to promote ambiguous regex matches to terminal facts. The robust long-term answer is a more structured action ABI or execution/output provenance, not an ever-growing shell regex validator.

## 7. Other Text-Semantics Overreach Findings

The basename branch is the incident trigger, but it is not the only source-text inference on the Generate → Execute path.

### 7.1 `controller_audit.audit_generated_script()`

`agent_core/controller_audit.py:754-853` scans source for:

- bare or absolute runtime executable names;
- `/dev/null` suppression patterns;
- required artifact env references.

Bash comments and quoted text are masked for executable/suppression checks (`:763-767`), but heredoc bodies are not parsed. Artifact usage is separately inferred by `script_references_env()` (`:587-605`); a mere `echo "$BIOMIND_INPUT_X"` can count as usage, while indirect environment access can be missed. Every resulting finding is `HIGH`, and `bio_agents.py:25852-25950` routes high findings through a bounded corrective path. Repeated findings can become terminal protocol failures.

Recommendation: keep these as machine diagnostics unless backed by structured command identity. The function already prefers structured identity for non-script actions (`controller_audit.py:770-817`); that precedence should become the general authority rule.

### 7.2 Raw placeholder scans in `materialize_managed_action()`

The raw `"{operation:" in code` and `binding.placeholder in code` checks (`managed_artifacts.py:1275-1318`) have the same comment/string/heredoc ambiguity. Typed placeholder rules in `argv/env` should remain hard. Whole-source token occurrences should not independently terminate.

### 7.3 Forbidden recovery-action matching

Execute normalizes the entire source and uses equality, substring, and token regexes to decide whether a candidate repeats a forbidden recovery action (`bio_agents.py:27479-27504`; normalization at `agent_core/diagnostics.py:1680-1684`). This routes back to Generate rather than immediately terminating, but descriptive text can still look like a forbidden command. It should remain bounded, non-terminal evidence unless a structured action signature matches.

### 7.4 Completion boundary checks

`CompletionGate.validate_code_boundary()` checks wrong benchmark roots and broad `find` commands using raw-source regexes (`agent_core/completion_gate.py:1893-1957`, `2155-2170`). These checks have some filesystem/context evidence, but the code occurrence is still source-text evidence. They should not be generalized into a precedent for managed-artifact identity decisions.

### 7.5 Install/runtime-skill inference

`_redirect_install_command()` and `_infer_runtime_skills_from_code()` scan source with regexes (`bio_agents.py:10450-10553`). The former can reroute execution; the latter changes skill inference. Comments/heredocs can therefore influence control behavior. These are secondary to this incident and should be audited separately rather than expanded during the minimal managed-artifact fix.

### 7.6 Executor shell safety and confirmation

`execution/shell_safety.py:105-181` scans the whole source for shell-startup edits, interactive commands, terminal modes, and selected unsafe mutations; `execution/executor.py:714-777` can reject with return code 126. `execution/confirmation.py:55-276` similarly uses raw regexes for risk classification and manual confirmation. Safety enforcement has a different risk tolerance from artifact identity, but comments/strings can still cause false positives. These checks are not the cause of this incident and must not be rewritten as part of the minimal repair.

## 8. `ACTION_FORMAT_REPAIR` Routing Audit

### 8.1 Why this incident consumed the format budget

The two basename matches are emitted as `owner="GENERATED_ACTION"`, `route="generate"` (`managed_artifacts.py:1337-1340`). `bio_agents.py:26034-26182` special-cases only internal-protocol and dependency ownership. Every other managed-artifact violation enters `_managed_preaction_recovery()`.

That helper then unconditionally constructs:

```text
valid = false
repairable = true
failure_owner = first violation owner
```

for all such violations (`bio_agents.py:8182-8199`). `RecoveryPolicy` does not inspect violation class. If the per-step format count is below one, it returns `FORMAT_REPAIR` and consumes `action_format_repair_count`; otherwise it escalates (`agent_core/recovery.py:2502-2546`). `FORMAT_REPAIR` maps to Generate / `action_format_repair` / `ACTION_FORMAT_REPAIR` (`recovery.py:2145-2148`). `apply_recovery_decision()` then increments both the global and per-step format counters and records the invalid candidate hash (`plan_tracker.py:2529-2563`).

Thus “managed-artifact policy rejection” and “malformed action representation” currently share one budget even though they are different failure domains.

### 8.2 Required route taxonomy

| Failure class | Examples | Owner | Required route | Budget behavior |
|---|---|---|---|---|
| Representation/protocol malformed | Invalid script-block framing; invalid action envelope; non-list/empty `argv`; non-object `env`; invalid env key/value type | Generate response | `ACTION_FORMAT_REPAIR` | Consume format budget. |
| Deterministic Generate semantic mismatch | Unknown handle, wrong direction, complete structured literal instead of handle, embedded typed placeholder, deterministic output-coverage gap | `GENERATED_ACTION` | `CORRECTIVE_GENERATE` | Use a bounded candidate/semantic repair policy, not format budget. Do not increase the existing budget merely to hide the distinction. |
| Ambiguous source-text policy signal | Basename/path/comment/string/heredoc regex hit; source-inferred env “usage” | None proven | `diagnostic_only` | Consume no terminal repair budget; action may proceed if all deterministic checks pass. |
| Dependency unavailable | Approved input binding exists but is not runtime-valid | `DEPENDENCY` | `recovery` / Supervisor | Dependency/recovery accounting, not format. |
| Controller projection/materialization invariant | Approved visible handle absent from bindings; materialized path differs from canonical; post-materialization env injection is inconsistent | `INTERNAL_PROTOCOL` / controller projection | `internal_protocol` | Budget-neutral fail closed, or one deterministic reproject only when independent state can change. Never ask the model to repair Controller state. |
| Runtime/execution/output evidence failure | Process nonzero, expected output absent/invalid, observed path mismatch | Runtime/Generate according to first divergent layer | `recovery` | Execution/recovery budget. |
| Proven non-repairable input/constraint failure | Required input missing with machine evidence; explicit user constraint forbids the only operation | Input/user constraint | `terminal` / blocked | No format repair. |

The current message at `bio_agents.py:26264-26276` also tells Generate to remove “literal/default fallbacks” for every script violation. For this incident that instruction was factually wrong. Repair projection must carry the evidence class; diagnostic source hits must not be presented as proven executable behavior.

## 9. Retry / Fingerprint Behavior

### 9.1 Candidate fingerprint

`managed_action_fingerprint()` hashes normalized `type`, `argv`, `env`, `cwd`, and `outputs` (`agent_core/managed_artifacts.py:496-516`). A script adapter places the complete body in `argv[2]`, so a changed body changes the candidate fingerprint.

That is why the incident has two different candidate fingerprints:

```text
candidate 1: 85cb2ff4700a709680bec1a63e7510824b1008fedf4440141cb10d4c087e7efd
candidate 2: 6fdcf81b4b92916778da2a51be1b230a79fef03e43685aa64e309eb51d290269
```

### 9.2 Error fingerprint

`build_preaction_recovery_context()` hashes only:

```text
failure_stage
step_id
normalized violations
```

The candidate is deliberately stored separately and excluded from the error hash (`agent_core/diagnostics.py:414-468`). Because both candidates generated the same two false violations, both share:

```text
2cc7982d28dfd5f54a0fc0f5c983d758b6b9c82cf7d2aaa439128e60eb1bb836
```

This is correct as an incident-class identity. It is not proof that the second candidate made no progress.

### 9.3 Why termination was budget exhaustion, not no progress

The Generate branch only sets `repair_no_progress` after repair is no longer allowed and the previous candidate fingerprint equals the current candidate fingerprint (`bio_agents.py:26186-26214`). Here the fingerprints differ, so termination becomes `action_format_repair_budget_exhausted`.

`plan_state.json` confirms:

```text
action_format_repair_count: 1
action_format_repair_by_step.prepare_snpeff_config: 1
invalid_action_hashes_by_step: candidate 1 only
```

The durable `repair_attempt_count: 2` is the rejection ordinal, not evidence that two repair-budget units were consumed. There was one initial rejection, one actual format repair, and one post-repair rejection.

Recommendation: preserve both fingerprints, but make routing and budget selection depend on an explicit evidence/repair domain. Same error fingerprint means “same validation finding set,” not “same executable candidate” and not “terminal by itself.”

## 10. Architecture Boundary Recommendation

### 10.1 Controller authority

Controller should own and hard-enforce:

- approved snapshot and current-step artifact visibility;
- semantic handle existence, direction, and requiredness;
- dependency runtime availability;
- typed placeholder atomicity in structured fields;
- canonical materialization into Controller-owned environment values;
- action envelope and field shape;
- exact output coverage when declared through typed identities;
- Controller projection/materialization consistency;
- post-execution output identity and validation evidence.

### 10.2 Generate authority

Generate owns:

- the executable algorithm and shell logic;
- meaningful use of the supplied managed inputs;
- choice of task-local staging names and explanatory content;
- correction of structurally proven handle/direction/output mistakes.

Controller may give Generate source-level warnings, but raw script meaning is not Controller-owned deterministic state.

### 10.3 Execute/Recovery authority

Execute and Recovery own evidence unavailable before process start:

- the command actually run under injected non-empty environment bindings;
- return code, stdout/stderr, and safety/confirmation outcomes;
- actual output existence, format, identity, and provenance;
- runtime failures and bounded repair decisions based on those facts.

### 10.4 Minimal governing invariant

For `script_block_v1`, the minimal deterministic invariant is:

```text
approved handle
  -> Controller-created env mapping
  -> non-empty canonical materialized value
  -> conflict-free Execute injection
```

The current code already has all four components. Whether arbitrary Bash uses that value in every semantically intended place cannot be proven by basename regex. Source-text analysis may advise; it must not override the structural invariant with terminal evidence.

## 11. Option A/B/C Comparison

| Criterion | Option A: exclude only heredoc/comments, keep basename hard-block | Option B: detect basename, raw occurrence warning only, structured proof blocks | Option C: remove basename-based blocking entirely |
|---|---|---|---|
| Correctness | Improves this exact case only if heredoc parsing is correct; still confuses strings/data with paths. | Best balance: structural violations remain exact; ambiguous text cannot terminate. | Cleanest identity model, but loses useful source diagnostics. |
| False-positive risk | Medium/high. Shell quoting, command substitution, nested heredocs, Python/R strings, generated files, and escaped content remain difficult. | Low for hard blocks; warnings may be noisy without harming task completion. | Lowest. |
| False-negative risk | Lower on obvious bare literals, but apparent precision is fragile. | Medium for free scripts; low for typed actions. Mitigated by non-empty Controller injection, diagnostics, and runtime/output evidence. | Highest for free scripts that ignore bindings. |
| Implementation complexity | Medium/high and open-ended; every new shell construct expands the lexical mini-parser. | Low. Partition existing evidence by authority and downgrade the raw scan. | Lowest in `managed_artifacts.py`, but broader observability changes may be needed. |
| Maintenance burden | High. Regex/parser exceptions grow with shell syntax. | Low/moderate. One clear authority rule plus a small diagnostic surface. | Low. |
| Architecture consistency | Weak. Controller still claims to understand arbitrary script semantics. | Strong. Matches structured identity, ownership, and first-divergent-layer principles. | Strong for identity, weaker operational guidance to Generate. |
| Regression risk | High: incomplete exclusions can both miss real commands and keep false blocks. | Lowest overall; deterministic tests stay, heuristic expectations intentionally change. | Medium: existing telemetry and fallback tests require larger expectation changes. |

**Recommendation: Option B.** It preserves observability that Option C discards, while making Option B's warning non-authoritative. Option A is not recommended because it creates a growing partial Shell parser, the exact direction prohibited by the task.

## 12. Recommended Minimal Change Surface

This section defines a future implementation boundary; no change is implemented in this audit.

### KEEP

- `materialize_value()` full-value handle/operation resolution, direction checks, dependency availability, canonical-path availability, typed placeholder atomicity, and structured exact-literal rejection (`managed_artifacts.py:1035-1212`).
- `argv/env` type and key/value checks (`:1215-1269`).
- approved visible-set and binding construction (`bio_agents.py:25976-25993`).
- `validate_materialized_artifacts()` internal invariants (`managed_artifacts.py:1460-1498`).
- deterministic current-step output coverage when based on explicit typed references (`:1408-1457`).
- Execute-time non-empty, conflict-free Controller environment injection.
- separate candidate and error fingerprints.

### CHANGE

- Partition pre-action findings into `representation`, `generated_semantic`, `dependency`, `internal_protocol`, and `source_diagnostic` before invoking recovery policy.
- Make `RecoveryPolicy` select `FORMAT_REPAIR` only when the caller supplies a representation/protocol repair domain; stop treating every `pre.valid=false, repairable=true` as a format error (`recovery.py:2502-2546`).
- Route deterministic Generate-owned artifact mistakes to `CORRECTIVE_GENERATE`, with bounded candidate repair accounting distinct from format accounting.
- Treat script-derived output/reference coverage as lower-confidence than explicit structured coverage.
- If diagnostic metadata is retained, label it `authoritative=false`, include the matched location/context, and never put it in the terminal violation list.

### DOWNGRADE

- Canonical path or basename occurrences anywhere in raw script `code`.
- Raw managed/operation placeholder occurrences in comments, strings, examples, or heredocs.
- Source-inferred required-artifact usage when no structured identity supports it.
- Ambiguous runtime executable and suppression matches not backed by structured command identity.

### REMOVE

- The whole-code hard-block branch at `managed_artifacts.py:1319-1370` as a source of `ArtifactViolation`.
- The claim that any matched basename is necessarily a “literal/default fallback.”
- Any terminal decision whose only evidence is a raw-script basename/path substring.

The detection itself may remain in a diagnostic helper; what must be removed is its authority to populate the hard violation list.

### ROUTING

- `ACTION_FORMAT_REPAIR`: malformed representation only.
- `CORRECTIVE_GENERATE`: structurally proven Generate-owned semantic mismatch.
- `recovery`: dependency, execution, runtime, or observed output evidence.
- `internal_protocol`: Controller projection/materialization/injection invariant.
- `diagnostic_only`: raw source textual heuristic.
- `terminal`: proven non-repairable input/user constraint or durable internal invariant after correct persistence; never a bare basename match.

## 13. Regression Test Matrix

No tests were executed during this read-only audit. The following matrix is the required acceptance surface for the future fix.

| ID | Candidate/evidence | Expected validator outcome | Expected route/budget |
|---|---|---|---|
| P01 | Incident candidate 1 verbatim | PASS deterministic artifact validation; two optional diagnostics at most | Execute; format count unchanged. |
| P02 | Incident candidate 2 verbatim | PASS deterministic artifact validation; two optional diagnostics at most | Execute; format count unchanged. |
| P03 | `# source was genome.fasta` plus real BIOMIND read | PASS | No repair budget. |
| P04 | `echo "Using provided genome.fasta"` plus real BIOMIND read | PASS | No repair budget. |
| P05 | `printf 'Reference genome.fasta validated\n'` plus real BIOMIND read | PASS | No repair budget. |
| P06 | Heredoc contains both managed input basenames | PASS | No repair budget. |
| P07 | `DESCRIPTION="genome.fasta and genome.gtf"` plus real BIOMIND reads | PASS | No repair budget. |
| D01 | Raw `tool genome.fasta` | Non-authoritative high-signal diagnostic | No terminal/format charge; future policy may request advisory corrective Generate. |
| D02 | Raw `${BIOMIND_INPUT_REFERENCE_FASTA:-genome.fasta}` | Non-authoritative diagnostic; canonical env still materializes | No terminal/format charge. |
| D03 | Raw canonical absolute path in log/heredoc | Diagnostic only | No terminal/format charge. |
| B01 | Structured `argv` scalar equals managed canonical path | BLOCK | `CORRECTIVE_GENERATE`, not format. |
| B02 | Structured `argv` scalar equals managed basename | BLOCK | `CORRECTIVE_GENERATE`, not format. |
| B03 | Structured `env` scalar equals managed canonical path/basename | BLOCK | `CORRECTIVE_GENERATE`, not format. |
| B04 | Managed placeholder embedded inside structured scalar | BLOCK | Corrective/protocol route; bounded, no execution. |
| B05 | Unknown handle or wrong direction | BLOCK | `CORRECTIVE_GENERATE`. |
| B06 | Approved visible handle missing from Controller projection | BLOCK | `internal_protocol`; zero model repair budget. |
| B07 | Required dependency marked runtime-unavailable | BLOCK current execution | `recovery`; zero format budget. |
| B08 | Controller materializes a path different from canonical binding | BLOCK | `internal_protocol`; zero model repair budget. |
| B09 | `argv` not a non-empty list or `env` not an object | BLOCK | `ACTION_FORMAT_REPAIR`; consume format budget. |
| B10 | Explicit typed current-step output is omitted | BLOCK | `CORRECTIVE_GENERATE`; no format budget. |
| F01 | Candidate A and changed candidate B produce same diagnostic-only basename hits | Both eligible to execute if deterministic checks pass | Same diagnostic fingerprint must not cause terminal state. |
| F02 | Two byte-identical malformed representations | Second rejection recognized as no representation progress | Bounded format terminal; candidate/error fingerprints persisted separately. |
| I01 | End-to-end graph replay of `prepare_snpeff_config` | One Generate candidate reaches Execute; config output reconciles | No `ACTION_FORMAT_REPAIR`; no `terminal_failed`. |

Existing tests that encode the current overreach must change:

- `tests/test_generate_script_artifact_repair_convergence.py:90-199` currently expects `${BIOMIND_INPUT_VARIANT_VCF:-all.filtered.snp.vcf}` to emit `MANAGED_ARTIFACT_HANDLE_REQUIRED` and use `action_format_repair`. It should instead verify successful canonical env materialization plus a non-authoritative diagnostic, if diagnostics are retained.
- `tests/test_plan_local_preaction_convergence_v2.py:914-974` currently expects a source literal fallback to require one repair before execution. It should expect no format repair when the injected BIOMIND output value is canonical and non-empty.
- `tests/test_managed_artifact_contract_v2.py:54-171` should continue to cover typed placeholder materialization, unknown/direction/duplicate checks, embedded structured placeholders, and exact structured literals.
- `tests/test_plan_local_preaction_convergence_v2.py:223-241` and `1007-1021` should continue to prove Controller projection failures are internal and budget-neutral.
- `tests/test_managed_action_env_v2.py:52-113` should continue to prove canonical values with spaces reach the subprocess and Controller runtime keys cannot be overridden.
- `tests/test_recovery_policy_ownership.py:29-59` should be narrowed so only true representation/protocol failures consume format budget; add a managed semantic-policy case that does not.
- `tests/test_session_controller_recovery_contract.py:402-580` should add heredoc cases and distinguish structured command evidence from ambiguous source matches.

## 14. Files / Functions Likely To Change

Primary minimal implementation surface:

1. `agent_core/managed_artifacts.py`
   - `materialize_managed_action()` raw-code branches at `1271-1370`.
   - Preserve `materialize_value()`, `validate_generate_output_coverage()`, and `validate_materialized_artifacts()` deterministic behavior.
2. `bio_agents.py`
   - Generate violation partition/routing at `26034-26309`.
   - `_managed_preaction_recovery()` at `8111-8208` so it no longer asserts every violation is format-repairable.
   - Execute-time env error ownership at `27430-27456` if a post-materialization Controller inconsistency is still labelled Generate-owned.
3. `agent_core/recovery.py`
   - `RecoveryPolicy.decide()` pre-action branch at `2502-2546`; require an explicit repair domain instead of inferring “format” from generic invalidity.
4. `agent_core/controller_audit.py`
   - Source diagnostic authority at `754-853`; keep structured identity authoritative and prevent ambiguous heredoc/source matches from becoming terminal machine findings.
5. `agent_core/diagnostics.py`
   - Preserve fingerprint construction; optionally add evidence/repair-domain metadata to the recovery context. Do not fold candidate identity into the error fingerprint.
6. `agent_core/plan_tracker.py`
   - Preserve durable primary/latest/terminal failure records; ensure only actual format repairs increment `action_format_repair_*`.
7. Tests listed in section 13, plus an incident-specific graph replay fixture.

`execution/shell_safety.py`, `execution/confirmation.py`, prompts, snpEff skill files, Java/runtime configuration, inputs, and repair-budget limits are not part of the minimal incident fix.

## 15. Explicit Non-Goals

The recommended future repair must not:

- modify the snpEff skill, Java/runtime, VCF, FASTA, GTF, or task outputs;
- raise `ACTION_FORMAT_REPAIR` or generic retry budgets;
- disable typed managed-artifact validation;
- permit unknown handles, wrong directions, missing dependencies, or Controller path mismatches;
- introduce a Semantic Reviewer, an LLM review stage, or an LLM decision about machine contracts;
- implement or vendor a Shell AST/parser;
- solve arbitrary Bash data-flow or incidental side-effect proof in the Controller;
- fold the separate shell-safety/confirmation audit into this change;
- reinterpret this report as authorization to modify production code.

## 16. Confidence / Remaining Unknowns

### Confidence

- **High**: first real fault, both candidate bodies, BIOMIND references, validator match, violation payload, fingerprints, budget accounting, and terminal route. These are directly present in session artifacts and current code.
- **High**: the basename detector is a whole-source regex/substr scan with no language/context discrimination.
- **High**: all ordinary managed-artifact violations reaching `_managed_preaction_recovery()` are currently asserted `repairable=True` and sent through a one-unit format-repair policy.
- **High**: no failing-step Executor process started; snpEff build and annotation were not reached.
- **Medium/high**: Option B is the smallest architecture-consistent fix because it changes authority/routing without adding a parser or weakening structured checks.

### Remaining unknowns

- This audit did not execute the scripts, so it does not certify the biological correctness of the eventual custom snpEff database or annotation output.
- The current free-script protocol cannot prove complete semantic data flow. A candidate could mention a BIOMIND variable without meaningfully using it. Existing regexes do not solve that problem; they only create an illusion of proof.
- A future structured script/action ABI could reduce that ambiguity, but it is not required for this incident fix and would be a separate design task.
- The checkout contains extensive unrelated dirty changes. A future implementation must isolate its patch by hunk and validate against this exact worktree before any commit decision.

## Required final answers

### Q1: Should whole-shell basename hard-block be retained?

**NO.**

Code evidence: `agent_core/managed_artifacts.py:1319-1370` applies canonical substring/basename regex matching to the complete raw `code`, without comment, quote, assignment, or heredoc semantics. Session evidence proves that this converted configuration prose into two terminal violations even though the structured candidate contained the correct three BIOMIND bindings. Retain detection only as non-authoritative telemetry; retain structured exact-value blocking.

### Q2: May an ordinary prose/comment/echo/printf/heredoc basename occurrence become terminal pre-execution evidence?

**NO.**

Code evidence: the matcher sees only a string and `field_path="code"`; it has no evidence that the occurrence is a file operand. Even the separate Controller audit calls its own quote masking a bounded lexical filter rather than a shell parser (`agent_core/controller_audit.py:517-523`). The incident heredoc is a concrete false-positive counterexample.

### Q3: Should managed-artifact policy rejection continue consuming `ACTION_FORMAT_REPAIR` budget?

**NO.**

Code evidence: `_managed_preaction_recovery()` marks every non-special managed-artifact violation `repairable=True` (`bio_agents.py:8182-8199`), and `RecoveryPolicy` maps any such invalid pre-action evidence to `FORMAT_REPAIR` until the one-unit budget is exhausted (`agent_core/recovery.py:2502-2546`). That conflates semantic policy with representation format. Only actual action-envelope/script-block/`argv`/`env` representation failures should consume this budget; deterministic managed semantic mismatches should use `CORRECTIVE_GENERATE`, heuristics should be diagnostic-only, dependencies should use recovery, and Controller invariants should be internal/budget-neutral.

### Q4: Does the recommended fix require a new Shell AST/parser?

**NO.**

Code evidence: the deterministic contract is already available in approved bindings, typed `argv/env`, canonical materialization, output coverage, and Execute env injection (`managed_artifacts.py:1035-1269`, `1408-1498`; `bio_agents.py:25976-26033`, `27430-27456`). The minimal repair removes authority from ambiguous raw-source matches. Adding a parser would enlarge the validator and recreate Option A's maintenance problem instead of fixing the authority boundary.
