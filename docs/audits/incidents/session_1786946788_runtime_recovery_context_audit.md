# session_1786946788 Runtime and Recovery Context Audit

Date: 2026-08-17

Mode: read-only incident audit plus documentation

Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`

Run root: `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1786946788`

## Executive conclusion

Two independent contract breaks occurred.

First, the snpEff skill healthcheck invoked the absolute executable `/public/home/liusiqi/03.毕设/source_tools/temurin-21/bin/java` and passed. The Controller persisted only the home directory and JAR paths. The final Generate provider request did not contain `JAVA_HOME`, the Temurin directory, or the Java 21 executable; it only named `$SNPEFF_JAR` and said to resolve/verify the Java runtime. The raw model response was the first artifact to introduce bare `java`. Post-Generate materialization exported `JAVA_HOME` but did not put `$JAVA_HOME/bin` on `PATH` or replace bare `java`. The nested `bash -lc` therefore resolved `/usr/bin/java` (Java 8), producing class-file version 65-versus-52 failure. This is a multi-component integration mismatch: `GENERATE_NOT_GIVEN_RUNTIME_EXECUTABLE`, `PROMPT_CONTRACT_ONLY_SPECIFIED_JAR`, and `EXECUTE_MATERIALIZER_FAILED_TO_ENFORCE_RUNTIME`. It is not `GENERATE_GIVEN_RUNTIME_BUT_IGNORED_IT`.

Second, the so-called “about 525k characters” was not an API request. The durable pretty JSON is 532,259 Unicode characters and 540,854 UTF-8 bytes. The compact JSON is 516,879 characters and 525,474 bytes; the 525k value is therefore compact UTF-8 bytes, not characters. The exact serializer used by `prompt_view()` produces 519,517 characters/528,112 bytes. No RecoveryJudge provider call occurred: zero characters of this raw context were sent to a Recovery provider. The deepest legal local projection was still 42,898 characters, over the 26,000-character local budget. The persisted error reported only the 18,771-character semantic core and omitted 23,715 characters of minimum evidence plus 412 characters of keys/wrapper/limits. This is `DIAGNOSTIC_ACCOUNTING_INCONSISTENCY`.

The original Java error remains durable, but the secondary context-capacity escalation became the terminal `TASK_FAILED` detail. A later Supervisor provider call correctly recognized the Java mismatch and recorded `RETRY_CURRENT_ACTION`, but `ESCALATE` had already made task status `failed`; the terminal guard routed the later Generate visit directly to `finish`.

## Audit boundary

- Branch: `codex/save-current-work`
- HEAD: `34934de0c92d7cae268a101a3dce13606039c21e`
- HEAD subject: `fix(agent): reconcile managed artifact realizations`
- The worktree was already dirty with modified skills/tests and untracked documentation/code before this audit.
- The runtime/recovery production files cited below had no local diff at audit start.
- No production code, test, run artifact, checkpoint, benchmark output, or snpEff process was changed or rerun.
- Non-mutating inspection was limited to JSON/text measurement, Git inspection, `command -v java`, `readlink`, and `java -version`.

## Part A — Java runtime provenance

### A1. Raw user submission

The authoritative raw object is:

```text
state_contract/objects/38ee146e542712078144119649b2c3da16a1375a402a0222e318f4b1df91f05e.json
```

Its `payload.text` is 1,294 characters/2,192 UTF-8 bytes. It contains the generic failure policy “snpEff/Java/database build 失败时 blocked；不得输出假 ANN”, but no concrete runtime selection.

| User-supplied item | Present? | Evidence |
|---|---|---|
| `JAVA_HOME` | no | No key/value or directory in raw text |
| Java path | no | No `java` executable path in raw text |
| Java version | no | No version number in raw text |
| snpEff runtime requirement | generic only | snpEff/Java/database failure must block; no implementation/runtime identity |

The TaskSpec preserves the annotation goal, inputs, constraints, and blocked/no-fake-output policy. It does not add a Java home, executable, or version. Java runtime identity is therefore a system/Skill-derived fact and must not depend on the original user prompt.

### A2. Skill healthcheck

The skill contract at `skills/genomics/snpeff-annotation/SKILL.md:102-122` declares `bio_gatk`, tools `[snpEff, java]`, and this decisive branch:

```bash
test -x "$SNPEFF_JAVA_HOME/bin/java" || exit 1
"$SNPEFF_JAVA_HOME/bin/java" -jar "$SNPEFF_JAR" -version >/dev/null || exit 1
```

The actual persisted audit in `plan_state.json.task_state.runtime_audit[0]` records:

| Field | Incident value |
|---|---|
| wrapper | `conda run -n bio_gatk bash -c '...'` |
| healthcheck Java executable | `/public/home/liusiqi/03.毕设/source_tools/temurin-21/bin/java` |
| JAR | `/public/home/liusiqi/anaconda3/envs/bio_gatk/share/snpeff-5.3.0a-0/snpEff.jar` |
| resolved environment | `/public/home/liusiqi/anaconda3/envs/bio_gatk` |
| persisted status | `status=ready`, `healthcheck_status=passed` |
| numeric process exit code | `UNKNOWN` (not persisted) |
| incident-reported Java version | `UNKNOWN` (not persisted) |
| verified at | `2026-08-17T14:08:14.632574` |

The healthcheck invokes snpEff's `-version` through the absolute Java executable and persists only three `RESOLVED_*` output rows. It does not persist `java -version`. A current, non-mutating inspection of that same absolute executable reports OpenJDK `21.0.11`, but that current result is not evidence that the incident saved a version string.

Runtime audit construction and resolved-path persistence are at `bio_agents.py:8810-8875`.

### A3. Runtime facts and propagation

| Fact | Incident value | Created by | Stored where | Passed to final Generate? | Passed to Execute? |
|---|---|---|---|---|---|
| `RESOLVED_ENV_PREFIX` | `/public/home/liusiqi/anaconda3/envs/bio_gatk` | healthcheck/audit | `plan_state.task_state.runtime_audit`, pending action | no | yes, export |
| `ENV_PREFIX` | same | `RESOLVED_` aliasing | pending action binding | no | yes, export |
| `RESOLVED_SNPEFF_JAR` | `/public/home/liusiqi/anaconda3/envs/bio_gatk/share/snpeff-5.3.0a-0/snpEff.jar` | healthcheck/audit | runtime audit, pending action | no absolute value; symbol only | yes, export |
| `SNPEFF_JAR` | same | `RESOLVED_` aliasing | pending action binding | name `$SNPEFF_JAR`, not value | yes, export |
| `RESOLVED_JAVA_HOME` | `/public/home/liusiqi/03.毕设/source_tools/temurin-21` | healthcheck/audit | runtime audit, pending action | no | yes, export |
| `JAVA_HOME` | same | `RESOLVED_` aliasing | pending action binding | no | yes, export |
| healthchecked Java executable | `/public/home/liusiqi/03.毕设/source_tools/temurin-21/bin/java` | healthcheck command | only embedded inside healthcheck text; no binding field | no | no executable binding |
| `JAVA_EXECUTABLE` | absent | no producer | nowhere | no | no |
| `PATH` runtime binding | absent | no runtime binding producer | nowhere in RuntimeContract | no | wrapper appends only `agent_shim/bin_shims` |

`_runtime_bindings_from_state()` accepts existing paths from `resolved_runtime_paths` and creates aliases by removing `RESOLVED_` (`bio_agents.py:5393-5418`). It does not derive `JAVA_EXECUTABLE` from `JAVA_HOME`.

The decisive temporal boundary is also important: `_bind_pending_action()` attaches these runtime bindings after Generate has produced an action (`bio_agents.py:4775-4867`). The Generate NodeContext builder projects progress/current-action semantics, not the runtime audit or binding map (`bio_agents.py:24675-24753`). Planner requests did contain runtime audit/home values, but Planner was not the executable-action producer; Consultant and the final Generate request did not receive the concrete Java path.

### A4. Final Generate provider request

Trace:

```text
context_trace/generate-0012-6756fa24988d/request.json
context_trace/generate-0012-6756fa24988d/manifest.json
```

The persisted request envelope is 12,047 characters/bytes. It contains exactly one `human` message with 11,005 characters. There is no system message and no separate developer message.

| Final message section | Characters | char/4 estimate | Source |
|---|---:|---:|---|
| static Generate instructions before `<llm_payload>` | 1,958 | 490 | `PromptManager.get_generate_v2_prompt` |
| payload outer braces/newlines | 3 | 1 | serialized LLMPayload |
| `allowed_operations` span | 111 | 28 | NodeContext |
| `current_step` span | 1,198 | 300 | approved current-step projection |
| `feedback` span | 18 | 5 | empty feedback projection |
| `runtime` span | 213 | 54 | progress/current-action semantic projection |
| `task` span | 4,883 | 1,221 | TaskSpec semantic projection |
| tag/newline/static separators | 85 | 22 | prompt wrapper |
| `capability_instructions` content | 2,536 | 634 | selected snpEff skill capability projection |
| **message total** | **11,005** | **2,752** | final provider message |

The manifest estimates 2,751 input tokens. `run_metrics.json` records 2,885 actual provider prompt tokens, 355 completion tokens, model `deepseek-v4-flash`, and successful completion. The provider name is not persisted and is `UNKNOWN`.

The model saw:

- `$SNPEFF_JAR` by name;
- “Runtime tools are already supplied as Controller-managed environment variables”;
- capability guidance to resolve/verify the pinned snpEff JAR and Java runtime.

It did not see:

- `JAVA_HOME=/.../temurin-21`;
- `RESOLVED_JAVA_HOME`;
- `/public/home/liusiqi/03.毕设/source_tools/temurin-21`;
- `/public/home/liusiqi/03.毕设/source_tools/temurin-21/bin/java`;
- a `JAVA_EXECUTABLE` field;
- a rule requiring `"$JAVA_HOME/bin/java"` instead of `java`.

The prompt's only snpEff-specific executable guidance is “reference `$SNPEFF_JAR`” (`prompts/manager.py:1828-1829`). It defines neither a bare-`java` requirement nor an absolute-executable requirement.

### A5. First bare `java` and responsibility classification

The first bare `java` in the action lineage is in the raw model response:

```text
context_trace/generate-0012-6756fa24988d/response.json:2
```

The raw JSON action contains:

```bash
java -jar "$SNPEFF_JAR" build ...
```

The parser preserves it. The materializer adds managed exports but does not rewrite executable tokens. `approved.sh:39` therefore retains bare `java`; the shell wrapper is not its first source.

Classification:

```text
GENERATE_NOT_GIVEN_RUNTIME_EXECUTABLE
PROMPT_CONTRACT_ONLY_SPECIFIED_JAR
EXECUTE_MATERIALIZER_FAILED_TO_ENFORCE_RUNTIME
```

These jointly form an integration mismatch. `GENERATE_GIVEN_RUNTIME_BUT_IGNORED_IT` is false because the final Generate provider request did not include the concrete home or executable.

### A6. Actual Execute environment

The attempt materializes as:

```text
attempts/attempt_a9b512093d864f3602ad/approved.sh
attempts/attempt_a9b512093d864f3602ad/executed.sh
```

Key boundaries:

- `executed.sh:3` appends `/public/home/liusiqi/03.毕设/agent_shim/bin_shims` to the existing `PATH`; it does not prepend Temurin.
- `executed.sh:6` invokes `approved.sh` using `bash`, without conda activation.
- `approved.sh:13-18` exports environment/JAR/Java-home bindings.
- `approved.sh:22` starts nested `bash -lc`.
- `approved.sh:39` executes bare `java`.

The persisted actual environment in `recovery/diagnostic_context.json.execution.actual_environment` is:

```text
CONDA_DEFAULT_ENV=biomind
CONDA_PREFIX=/public/home/liusiqi/anaconda3/envs/biomind
resolved_executables.java=/usr/bin/java
resolved_executables.bash=/usr/bin/bash
resolved_executables.snpEff=""
```

The exact incident pre-login PATH string was not persisted, so it is `UNKNOWN`. A non-mutating reconstruction using the same current parent PATH, exported `JAVA_HOME`, shim append, and nested `bash -lc` established:

| Boundary | PATH result | `command -v java` | version |
|---|---|---|---|
| before nested `bash -lc` | includes `/usr/bin` before the appended shim; no Temurin `bin` | `/usr/bin/java` | OpenJDK `1.8.0_372` |
| inside nested `bash -lc` | login profiles prepend/duplicate several paths; still no Temurin `bin` | `/usr/bin/java` | OpenJDK `1.8.0_372` |

`readlink -f /usr/bin/java` resolves to `/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.372.b07-2.el9.x86_64/jre/bin/java`.

The login shell demonstrably reconstructs PATH, so it is an uncontrolled transformation boundary. It is not the sole cause here: bare `java` already resolved to `/usr/bin/java` before the nested login shell because exporting `JAVA_HOME` does not affect command lookup and Temurin `bin` was never on PATH.

The primary command exits 1 with:

```text
UnsupportedClassVersionError
jar class file version: 65.0
runtime maximum class file version: 52.0
```

This is Java 21-compiled code executed by Java 8. Partial copied files/config/build log were consequences of the script running before the failing JAR invocation, not the cause and not validated annotation output.

### A7. Missing invariant

BioMind currently has no formal generic invariant that guarantees:

```text
healthchecked executable == executed executable
```

The healthcheck verifies that a capability can run through an absolute path. The binding layer retains directory/JAR environment values. Executable identity does not enter the generated action or pre-execute materialization. PATH is not part of the verified RuntimeContract, and the nested login shell may transform it. The code boundaries are `bio_agents.py:5393-5418` for path-to-binding conversion and `bio_agents.py:480-547` for environment-only materialization.

## Part B — Recovery context and provider boundary

### B1. What “525k” means

The two durable files below are byte-identical:

```text
recovery/diagnostic_context.json
recovery/recovery_context.json
```

| Representation | Characters | UTF-8 bytes | char/4 estimate | Sent to provider? |
|---|---:|---:|---:|---|
| persisted pretty JSON file | 532,259 | 540,854 | 133,065 | no |
| exact `prompt_view()` canonical serialization | 519,517 | 528,112 | 129,880 | no |
| compact JSON serialization | 516,879 | 525,474 | 129,220 | no |

Token columns for unsent local objects are only the requested character heuristic, not provider tokenizer counts. The frequently cited `525,474` is compact UTF-8 bytes, not characters.

#### Top-level canonical-serialization breakdown

Percentages use 519,517 characters. “Required” describes projection treatment: semantic sections are preserved byte-for-byte, while evidence sections are excerptable/compactable. None reached RecoveryJudge in this incident.

| Section | Characters | Share | Source/type | Projection status | Duplicate? | Recovery provider? |
|---|---:|---:|---|---|---|---|
| `artifacts` | 454,463 | 87.478% | bounded filesystem snapshot | evidence/optional content | heavily | no |
| `execution` | 22,721 | 4.373% | enriched executor/script/log/runtime evidence | evidence/compactable | yes | no |
| `runtime` | 11,850 | 2.281% | full runtime audit row | evidence/compactable | yes | no |
| `effective_user_facts` | 10,147 | 1.953% | semantic fact projection | semantic/required | overlaps TaskSpec | no |
| `action_contract` | 4,478 | 0.862% | current pending action | semantic/required | duplicated in execution | no |
| `task_spec_summary` | 4,273 | 0.822% | normalized TaskSpec summary | filtered semantic/required | goal duplicated | no |
| `progress` | 4,232 | 0.815% | Plan/attempt/terminal history summary | evidence/compactable | artifact paths repeat | no |
| `bounded_log_excerpt` | 1,832 | 0.353% | combined stream tails | evidence/compactable | repeats stdout/stderr | no |
| `skill_policy` | 1,676 | 0.323% | clipped selected-skill context | evidence/compactable | overlaps runtime/skill | no |
| `diagnosis` | 971 | 0.187% | deterministic diagnosis | evidence/compactable | overlaps execution | no |
| `log_references` | 764 | 0.147% | log/script/trace paths | evidence/compactable | paths repeat | no |
| `current_step` | 672 | 0.129% | current canonical step | semantic/required | overlaps progress | no |
| `validation` | 295 | 0.057% | validation evidence | evidence/compactable | slight | no |
| `limits` | 180 | 0.035% | local DiagnosticLimits | local metadata | no | no |
| `completion` | 114 | 0.022% | completion state | evidence/compactable | no | no |
| remaining identity/empty values plus top-level JSON structure | 849 | 0.163% | identity/empty values and field-name/separator overhead | mixed | no | no |

The raw user submission is not copied verbatim into RecoveryContext. `task_spec_summary.original_user_request` is a 196-character normalized English goal and exactly duplicates `task_spec_summary.goal`; semantic constraints live separately in `effective_user_facts`. The raw 1,294-character Chinese submission remains in the state contract/run metrics.

`effective_user_facts` also reports `projection_budget=6000`, `projection_chars=9628`, and `projection_truncated=true`; the complete serialized section including its metadata is 10,147 characters. It is nevertheless copied into every semantic candidate without further semantic truncation.

#### Top 20 artifact contributors

The snapshot reached its 64-file limit. Row characters include path/metadata/JSON structure as well as content preview. Preview/tail characters are shown separately.

| Rank | Artifact | Row chars | Preview/tail chars |
|---:|---|---:|---:|
| 1 | `context_trace/consultant-0004.../request.json` | 50,019 | 44,074 |
| 2 | `context_trace/consultant-0008.../request.json` | 48,658 | 42,769 |
| 3 | `context_trace/consultant-0006.../request.json` | 48,352 | 42,489 |
| 4 | `context_trace/consultant-0010.../request.json` | 48,167 | 42,312 |
| 5 | `context_trace/consultant-0002.../request.json` | 43,723 | 38,827 |
| 6 | `context_trace/consultant-0004.../manifest.json` | 33,980 | 30,823 |
| 7 | `context_trace/consultant-0010.../manifest.json` | 33,976 | 30,819 |
| 8 | `context_trace/consultant-0008.../manifest.json` | 33,976 | 30,819 |
| 9 | `context_trace/consultant-0006.../manifest.json` | 33,976 | 30,819 |
| 10 | `context_trace/consultant-0002.../manifest.json` | 23,226 | 21,025 |
| 11 | `plan.md` | 10,804 | 9,841 |
| 12 | `cot_session_1786946788.md` | 10,641 | 9,661 |
| 13 | `consultant-0002.../response.json` | 6,933 | 5,689 |
| 14 | `attempts/.../approved.sh` | 2,995 | 2,552 |
| 15 | `executions/...stdout.full.log` | 2,481 | 1,736 |
| 16 | `attempts/.../stdout.log` | 2,466 | 1,736 |
| 17 | `consultant-0004.../response.json` | 2,383 | 1,749 |
| 18 | `attempts/.../executed.sh` | 2,188 | 1,834 |
| 19 | `attempts/.../command_trace.log` | 1,204 | 769 |
| 20 | `consultant-0006.../response.json` | 913 | 516 |

The five Consultant request rows plus five manifests contribute 398,053 characters: 76.620% of the whole canonical context and 87.588% of `artifacts`. They are the dominant source of volume, not the biological inputs or stderr.

Inventory findings for the requested evidence types:

- Raw user submission: not full; normalized semantic facts only.
- TaskSpec: summary plus effective-user-fact projection; no complete raw TaskSpec object.
- Plan: `plan.md` preview plus current-step/progress fields; no full `active_plan.json` content.
- Approved snapshot: no full snapshot content; only empty/identity fields in this context.
- Full generated script/actual command: action argv, approved script preview, executed wrapper preview, and script context overlap.
- stdout/stderr: small incident logs fit the per-file cap, then also appear as tails/excerpt/artifact previews.
- Artifact/provenance/filesystem evidence: 64 bounded rows and the major volume owner.
- Previous attempts/recovery: both empty (`[]`/`{}`).
- Skill content: clipped `skill_policy` plus repeated runtime registry/audit facts.
- Consultant output/context traces: present and dominant.
- Planner output: `plan.md` preview is present; Planner request/response trace files were not reached before the 64-file cap.
- Runtime audit: present in `runtime`, and partially duplicated under `execution.expected_runtime_contract`.
- Full `plan_state.json`, `active_plan.json`, `run_metrics.json`, and JSONL files: metadata-only due the internal-state policy at `agent_core/diagnostics.py:507-510,863-869`.
- Terminal history: bounded fields under `progress`; not a full terminal transcript.

### B2. Duplicate audit

Confirmed exact string duplicates include:

| Duplicate group | One value length | Copies | Avoidable repeated chars after first |
|---|---:|---:|---:|
| identical healthcheck command | 873 | 7 | 5,238 |
| identical stdout tail | 1,736 | 3 | 3,472 |
| executed script context/preview | 1,834 | 2 | 1,834 |
| output-directory path | 152 | 11 | 1,520 |
| command trace tail | 769 | 2 | 769 |
| runtime notes/detail/reason | 232 | 4 | 696 |
| action shell body in action and execution contract | 578 | 2 | 578 |

Nested duplicates also include action contract under both top-level `action_contract` and `execution.action_contract`, input/output paths across TaskSpec facts/action/materialized artifacts/validation/progress, and stdout across execution tails, bounded excerpt, full-log artifact, and attempt-log artifact.

For near duplicates, a character multiset `SequenceMatcher.quick_ratio` measurement over the last four Consultant request previews is 97.88%-99.71%; the four corresponding manifest previews are 99.74%-99.81%. These rows are separate traces but carry almost the same expanding semantic/history payload.

### B3-B4. What 26,000 limits and why 18,771 still fails

The source is `RecoveryContext.prompt_view()` at `agent_core/diagnostics.py:244-389`; the persisted `limits.max_context_chars` is 26,000. The comparison is:

```python
len(json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)) <= 26000
```

It limits a local rendered RecoveryContext candidate. It is not a semantic-only limit, message-envelope byte limit, provider input token limit, model window, or output reserve.

Exact candidates from the four code-defined compaction levels are:

| text limit | list limit | Candidate chars | Fits 26,000? |
|---:|---:|---:|---|
| 3,000 | 24 | 119,253 | no |
| 1,200 | 12 | 78,981 | no |
| 480 | 6 | 56,284 | no |
| 160 | 3 | 42,898 | no |

The actual deepest-candidate accounting is:

```text
semantic identity + required sections       18,771
minimum compacted evidence values           23,715
JSON keys/separators + limits wrapper           412
system/provider instructions                     0  (not built yet)
provider output schema                           0  (not built yet)
output reserve                                    0  (not part of this gate)
---------------------------------------------------
actual minimum candidate                      42,898
effective local capacity                      26,000
over capacity                                 16,898
```

The 23,715 evidence characters at the deepest level are led by `execution=11,865`, `runtime=5,439`, `progress=2,875`, `artifacts=1,585`, `diagnosis=635`, `log_references=460`, `validation=295`, `skill_policy=251`, `bounded_log_excerpt=186`, and `completion=114`; empty/minimal evidence values account for the remainder.

After all four real candidates fail, lines 359-364 calculate `required_semantic_chars` from identity plus semantic sections only. Lines 365-389 then report 18,771 and 26,000. The omitted evidence and wrapper are still compulsory in every candidate. The diagnostic's two displayed numbers are therefore not the operands that produced the failure: `DIAGNOSTIC_ACCOUNTING_INCONSISTENCY`.

The returned capacity object itself is 638 characters/bytes and was also not sent to a provider.

### B5-B6. Actual provider invocations

Strictly within the Recovery subsystem there were zero provider calls. There is no `context_trace/recovery*` directory, and run metrics contain no `recovery_judge` LLM event. Middleware created the raw context, called `prompt_view()`, found `CONTEXT_CAPACITY_EXCEEDED`, and built a deterministic `ESCALATE` before `_invoke_recovery_v2()` (`agent_core/middleware.py:1602-1654`).

| Conceptual Recovery-related operation | Call ID | Provider/model | Request chars | Actual input tokens | Provider invoked? | Status |
|---|---|---|---:|---:|---|---|
| deterministic root-cause analysis | none | none | 0 | 0 | false | completed locally |
| Recovery planning/judgment | none | none | 0 | 0 | false | capacity gate before invocation |
| Recovery decision | none | none | 0 | 0 | false | deterministic `ESCALATE` |
| script patching | none | none | 0 | 0 | false | not reached |
| Generate retry | none | none | 0 | 0 | false | terminal guard; no second Generate LLM call |

If “Recovery-related” is broadened to include the downstream scientific Supervisor review, there was one provider call after the failed Execute:

| Call | Purpose | Model/provider | Final message chars | Manifest estimate | Actual input tokens | Status |
|---|---|---|---:|---:|---:|---|
| `supervisor_reviewer-0013-6bcf399dc77f` | advisory review of failed execution | `deepseek-v4-flash` / provider `UNKNOWN` | 14,346 | 3,586 | 3,964 | completed |

The Supervisor request envelope is 15,384 characters/15,444 bytes and contains one Human message, no system/developer messages. Its final message inventory is:

| Message section | Characters | char/4 estimate | Source | Necessary for this review? |
|---|---:|---:|---|---|
| static prefix | 316 | 79 | Supervisor prompt | yes, role |
| payload outer braces/newlines | 3 | 1 | wrapper | structural |
| `allowed_operations` | 57 | 15 | controller | yes |
| `current_step` | 1,425 | 357 | Plan projection | yes, partially overlapping |
| `evidence` | 2,427 | 607 | failed execution/log evidence | yes, partially duplicative |
| `runtime` | 2,302 | 576 | action/runtime/execution status | yes |
| `task` | 4,453 | 1,114 | TaskSpec projection | partly; full projection larger than minimal Java facts |
| tag/newline/static separators | 29 | 8 | wrapper | structural |
| static suffix/output contract | 3,334 | 834 | Supervisor instructions/schema | yes for protocol |
| **total** | **14,346** | **3,587** | final Human message | sent |

This Supervisor message contains the failing command/script, error excerpts, current step, task constraints, `JAVA_HOME`, and bare `java`. It does not contain the absolute `.../temurin-21/bin/java`. Its response correctly identifies that setting `JAVA_HOME` did not control the executable found on PATH.

Nested, non-additive content inventory: `evidence` contains a 2,099-character stdout excerpt and a 29-character stderr excerpt; `runtime` contains a 2,006-character executed action, return code 1, and a 102-character scientific-review context. There is no separate full-Plan field, filesystem snapshot, artifact ledger, previous-attempt history, or previous-Recovery field. The current step is the only Plan projection. The output schema/contract is in the 3,334-character static suffix. Because these nested strings are already counted inside `evidence` and `runtime`, they must not be added again to the 14,346-character total.

The initial Generate call `generate-0012-6756fa24988d` preceded execution and is not a Recovery call. There was no post-failure Generate provider retry.

Answer to “were the 525k sent?”: **NO** for RecoveryJudge. Exactly zero characters of the raw RecoveryContext were submitted to a Recovery provider. The later 14,346-character Supervisor message was independently built from overlapping state/evidence; it is not a transmitted slice of `diagnostic_context.json`, so no honest “portion copied” count can be derived.

### B7. stdout/stderr bounds

`_read_text_evidence()` reads at most `max_text_bytes_per_file` (131,072 bytes in this context). `enrich_execution_evidence()` stores the full body only if the file fits, and derives the last `log_tail_lines` (100) from the bytes read (`agent_core/diagnostics.py:519-529,623-632`). `bounded_log_excerpt` then clips combined stderr/stdout tails to 6,000 characters (`agent_core/diagnostics.py:1325-1330`).

The artifact scanner separately applies maximum files=64, scan depth=4, total read bytes=1 MiB, per-file bytes=131,072, error-line selection, and last 100 lines for logs (`agent_core/diagnostics.py:826-890`). The incident logs were small enough that their complete content effectively appeared in raw evidence and then repeated in tails/artifact previews. Log ingestion is bounded, not unbounded; raw persistence and LLM projection remain distinct.

### B8. State/Plan duplication and minimum Java facts

The RecoveryContext does not simultaneously embed full TaskSpec, full Plan, full approved snapshot, and full PlanState. It embeds a TaskSpec summary plus effective facts, current step, action contract, progress, and `plan.md` artifact preview. Named internal state files are metadata-only. Historical attempts and previous Recovery are empty. Nevertheless, semantic facts/path/action content are duplicated across these partial projections and filesystem previews.

An evidence-based minimum fact inventory sufficient to classify this Java class-version failure is:

1. action/step/attempt identity and exit code 1;
2. failing command uses bare `java -jar "$SNPEFF_JAR"`;
3. persisted error says JAR class version 65 and runtime maximum 52;
4. expected runtime home and healthchecked absolute executable;
5. actual executable `/usr/bin/java` and Java `1.8.0_372`;
6. the failed script line;
7. retry constraints: same action/step and no fabricated ANN output.

Serialized as a compact audit JSON with exact incident values, this set is 665 characters/673 UTF-8 bytes, approximately 167 tokens by char/4. This is a measurement of necessary facts, not a proposed prompt or code change.

## Part C — Failure masking

### Durable sequence

```text
14:47:01 primary Execute fails: Java class version 65 vs 52
  -> deterministic diagnosis: GENERATED_ACTION / PATCH_SCRIPT / evidence sufficient
14:47:06 local prompt projection fails: 42,898 > 26,000
  -> deterministic ESCALATE / INTERNAL_PROTOCOL / context_capacity_exceeded
  -> apply_recovery_decision sets durable task status failed
  -> execute route is guarded terminal_failed -> finish/supervisor
14:47:06-14:47:14 Supervisor provider review diagnoses Java mismatch
  -> advisory RETRY_CURRENT_ACTION recorded
  -> task status is already failed
14:47:14 supervisor and generate routes are terminal_failed -> finish
```

The original Java failure is preserved in attempt stdout/stderr/build log, execution records, root-cause analysis, `plan_state.json` evidence, and both Recovery context files. The raw context's `diagnosis` had `failure_owner=GENERATED_ACTION`, `next_action=PATCH_SCRIPT`, and `evidence_sufficient=true`. Middleware nevertheless checks the capacity object before that diagnosis (`agent_core/middleware.py:1629-1661`).

`apply_recovery_decision()` maps `ESCALATE` to durable `TASK_STATUS_FAILED` (`agent_core/plan_tracker.py:2087-2090`). The transition guard then routes durable failed/cancelled tasks to `finish` (`bio_agents.py:11923-11930`). `context_trace/events.jsonl:14-16` records three `terminal_failed` routes.

The final user/metrics projection is not the Java error. `run_metrics.json.final_result` contains:

```text
TASK_FAILED | The complete recovery semantic closure exceeds the configured context capacity...
```

Thus a secondary diagnostic/projection failure has higher terminal presentation and routing priority than the primary execution failure. This is a confirmed failure-priority inversion. The later `RETRY_CURRENT_ACTION` cannot recover because it is recorded after durable terminal failure and the transition guard suppresses another Generate/Execute cycle.

## Required final answers

1. **Did the raw user submission contain Java path/version?** No. It contained only a generic snpEff/Java failure policy.
2. **Did final Generate see `JAVA_HOME`?** No.
3. **Did final Generate see the absolute Java 21 executable?** No.
4. **Why did the structured action use bare `java`?** The prompt named only `$SNPEFF_JAR`, projected no executable identity, and the raw model response introduced bare `java`; the parser/materializer preserved it.
5. **Where was healthchecked executable identity lost?** At runtime-audit-to-binding projection: only `RESOLVED_JAVA_HOME`/`JAVA_HOME` survived, not `JAVA_EXECUTABLE`; Generate ran before those bindings were attached, and Execute only exported values.
6. **Why did Execute resolve `/usr/bin/java`?** `JAVA_HOME` does not change PATH, Temurin `bin` was never placed on PATH, the wrapper only appended shim paths, and `bash -lc` still found `/usr/bin` first.
7. **What formed the 525k?** Canonical raw context was 519,517 chars, 87.478% of it `artifacts`; ten near-duplicate Consultant request/manifest rows alone were 398,053 chars. Execution/runtime/TaskSpec/action/progress/log/skill sections formed most of the remainder.
8. **How much was actually sent to API?** Zero characters of the raw RecoveryContext to RecoveryJudge. A separate downstream Supervisor message sent 14,346 characters; it was not the raw context.
9. **How many Recovery-related provider calls?** Strict Recovery subsystem: zero. Broadly including downstream Supervisor review: one.
10. **Which failed before provider invocation?** Recovery planning/judgment (`recovery_judge_v2`) was stopped by `prompt_view()` capacity gate before invocation.
11. **Why capacity exceeded when 18,771 < 26,000?** 18,771 omits 23,715 minimum evidence chars and 412 structural chars; the actual minimum candidate was 42,898.
12. **What is the real formula?** `serialized complete semantic core + deepest compacted values for every evidence key + JSON/wrapper/limits overhead = 18,771 + 23,715 + 412 = 42,898`, compared with 26,000.
13. **Was the Java failure covered by the secondary Recovery failure?** It remained durable, but yes: the capacity failure replaced it in terminal routing and final user/metrics detail.
14. **What is reusable next time?** The architecture baseline now records runtime discovery/binding/Generate/materialization/PATH/shell boundaries; the missing healthcheck-to-execute invariant; Recovery raw-evidence sources/log bounds/projection levels/local budget/provider boundary; incomplete capacity accounting; and failure-precedence behavior.

## Principal evidence index

Run artifacts:

- `state_contract/objects/38ee146e542712078144119649b2c3da16a1375a402a0222e318f4b1df91f05e.json`
- `plan_state.json`
- `run_metrics.json`
- `context_trace/generate-0012-6756fa24988d/{request,response,manifest}.json`
- `attempts/attempt_a9b512093d864f3602ad/{approved.sh,executed.sh,stdout.log,stderr.log,command_trace.log}`
- `recovery/{diagnostic_context.json,recovery_context.json,recovery_decision.json}`
- `context_trace/supervisor_reviewer-0013-6bcf399dc77f/{request,response,manifest}.json`
- `context_trace/events.jsonl`

Production code:

- `skills/genomics/snpeff-annotation/SKILL.md:102-122`
- `bio_agents.py:480-547,4775-4867,5393-5418,8810-8875,11923-11930,24675-24753`
- `prompts/manager.py:1807-1858`
- `agent_core/diagnostics.py:244-389,507-529,610-715,826-890,1300-1340`
- `agent_core/middleware.py:83-128,1602-1661`
- `agent_core/plan_tracker.py:2087-2090`
- `agent_core/recovery.py:2123-2124`
