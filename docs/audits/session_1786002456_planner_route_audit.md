# session_1786002456 Planner Route Audit

Date: 2026-08-06

Scope: read-only audit of `session_1786002456`. The session was not resumed,
approved, rerun, or modified. No hidden answer or expected-answer file was read.

## Executive conclusion

The observed behavior is a combination of three separate defects and one
correct repair action:

1. The initial Planner response was complete and parseable, but omitted the
   user-explicit required output `ann_gene_summary.tsv`. The deterministic
   coverage check correctly triggered one typed local PlanPatch.
2. The patch was valid, was applied to the full base Plan, and produced plan
   revision `plan_v2`. It did not replace the effective Plan with patch JSON.
3. Plan Review incorrectly chose `pending_review_diff` as the primary display
   whenever a review diff existed. This replaced the full Plan on screen with
   the raw internal diff and made it appear that only `step-4` existed.
4. The effective repaired Plan is still not safe to approve: it omits the
   user-required `stage_status.tsv` from its logical contract and creates
   separate steps for outputs already owned by the Renderer manifest. The
   TaskSpec also contains false required inputs extracted from forbidden/path-like
   prose.

`planner_repair` was therefore not caused by truncation, parser failure, schema
failure, memory contamination, or a hidden fixture. The Plan Review symptom is
a confirmed UI/state-field selection error.

## Git and evidence snapshot

- Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`
- Branch: `codex/save-current-work`
- Start commit: `e7b43f9b7881a9a2176ee63624dc0508ac805141`
- Worktree: dirty before this audit, with pre-existing modified skills/tests and
  untracked `docs/refactor`, skill, test, and preference files. None were
  reverted or included in this audit.
- `git diff --check`: no whitespace error was reported for tracked diffs.
- Session root:
  `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1786002456`

The full user request in `run_metrics.json.metadata.test_prompt` and
`plan_state.json.task_spec.goal` is 1,590 characters including the final newline.
Both have SHA-256:

```text
d496ff241a77b76055b011c18526bf28f72cebd8706b71364bca591e0e73d8a7
```

The input is complete. The shortened terminal excerpt is presentation
truncation, not ingestion truncation. Both Planner trace manifests have an empty
`truncation_manifest`.

## Actual route

| Order | Node/call | Time | State/result | Trigger |
|---:|---|---|---|---|
| 1 | TRIAGE | 15:48:35-15:48:46 | managed, retrieval=kb | multi-step annotation task |
| 2 | CONSULTANT call 1 | 15:48:50-15:48:53 | requests/loads selected skill | canonical skill found |
| 3 | Skill load | 15:48:53 | `genomics/snpeff-annotation` | Consultant action |
| 4 | CONSULTANT call 2 | 15:48:54-15:48:58 | selected skill handed to Planner | skill-only fallback |
| 5 | PLANNER initial | 15:49:03-15:50:13 | complete six-step Markdown Plan | normal Planner call |
| 6 | Parse/normalize/validate | 15:50:13 | renderer steps 2+3 collapsed; output coverage violation | deterministic checks |
| 7 | PLANNER repair | 15:50:13-15:50:25 | typed `replace_steps` patch | missing `ann_gene_summary.tsv` |
| 8 | Patch parse/apply | 15:50:25 | `plan_v1 -> plan_v2`; only step-4 changed | valid base hash/version |
| 9 | PLAN_REVIEW | 15:50:26-15:57:42 | raw diff shown; user quit | `pending_review_diff` present |

There were five LLM calls total and zero command executions. Planner repair is
inside the Planner node's normal validation/repair protocol; it did not pass
through the execution transition guard because no execution transition had yet
occurred.

## Initial Planner output

Trace source:
`context_trace/planner_initial-0004-f3d7e027571e/response.json`

- Raw response length: 9,809 characters.
- Format: complete Markdown with four sections and one machine checklist.
- Completion: clean ending; no unclosed JSON or truncation marker.
- Parsed steps: `step-1` through `step-6`.
- Selected skill: present on steps 2 and 3.
- `ann_gene_summary.tsv`: absent.
- `stage_status.tsv`: absent.
- The response also violated the Renderer planning rule by splitting environment,
  annotation, summary, report, and reproduction work into separate steps even
  though the execution card says the Renderer owns its internal actions and
  final reporting artifacts.

### Initial step index

| Stable ID | Initial title/output | After renderer normalization | Repaired? |
|---|---|---|---|
| step-1 | validate VCF/FASTA/GTF | retained | no |
| step-2 | create output dir/check runtime | anchor of composite Renderer step | no |
| step-3 | build database and annotate; `annotated.vcf`, `snpEff_summary.html` | merged into step-2 and removed | no |
| step-4 | generate `ann_effect_summary.tsv` | retained | yes, gene summary added |
| step-5 | generate `report.md` | retained | no |
| step-6 | generate `reproduce.sh` | retained | no |

`collapse_renderer_composite_steps()` preserves the anchor's stable ID and
removes only the additional Renderer-backed step. Thus the post-normalization
gap at `step-3` is intentional stable-ID preservation. `changed_step_ids` means
patch scope, not current execution position. The current step remained
`step-1`.

## Validator matrix

| Validator | Input | Result | Triggered repair |
|---|---|---|---|
| Plan parser | full Planner Markdown checklist | passed; six steps parsed | no |
| Schema/contract | normalized steps | passed; action and success criteria present | no |
| Step IDs | parsed checklist and normalization | passed; stable IDs retained | no |
| Skill plan validation | normalized Plan + selected skill | passed | no |
| Step-to-skill validation | normalized steps | passed | no |
| Dependency normalization | Renderer collapse | passed | no |
| Legacy output coverage | raw request's `预期产物` section vs rendered Plan | failed only for `ann_gene_summary.tsv` | yes |

The triggering code is `BioAnalystAgent._skill_plan_violations()` in
`bio_agents.py:6786-6845`; lines 6830-6843 regex-extract filenames only from the
raw `预期产物` section. The Planner pipeline invokes it after normalization at
`bio_agents.py:10815-10835`.

This validator did not check the structured TaskSpec output contracts. As a
result, it detected `ann_gene_summary.tsv` but missed `stage_status.tsv`, which
appears in a later generic requirement rather than inside `预期产物`.

## `ann_gene_summary.tsv` lineage

```yaml
requirement:
  value: ann_gene_summary.tsv
  first_introduced_at: original persisted user request, expected-artifacts section
  source_type: user_explicit
  source_ref: run_metrics.json.metadata.test_prompt
  required_or_optional: required
  introduced_by: user request text
  propagated_to:
    - TaskSpec.expected_outputs
    - TaskSpec.output_contracts
    - Planner repair violation
    - repaired step-4 artifacts and success criteria
  validator_interpretation: user-expected output
```

It is genuinely user-required in the actual request, even though the shorter
problem-statement excerpt omitted it. It was not introduced by the skill,
workflow, memory, Planner, repair model, or a hidden fixture. A project search
outside tests/docs/task outputs found no production skill declaration for
`ann_gene_summary.tsv`; the current Renderer produces only the effect summary.

The current Renderer explicitly produces `ann_effect_summary.tsv` and its final
action produces `report.md`, `reproduce.sh`, and `stage_status.tsv` at
`skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py:284-374`
and declares them in the manifest at lines 539-587. It does not produce
`ann_gene_summary.tsv`, so a separate logical action for that requested output is
reasonable. Combining it with another Renderer-owned output in step-4 is not.

## Repair contract and application

```yaml
planner_repair_contract:
  expected_output_type: one typed local PlanPatch JSON object
  operation: replace_steps
  parser: agent_core.plan_tracker.parse_plan_patch
  validator: base version/hash, existing step ID, allowed step scope
  application_function: agent_core.plan_tracker.apply_plan_patch
  persisted_as:
    - plan_state.json revision_history and pending_review_diff
    - plans/active_plan.json
    - plan_patches.jsonl audit record
    - plan.md rendered effective checklist
  plan_version_increment: plan_v1 to plan_v2
  user_visible: should be summary-only, but raw diff currently replaces Plan
```

The raw repair response replaced only existing `step-4`. The patch base version
and semantic hash matched. `apply_plan_patch()` at
`agent_core/plan_tracker.py:2914-3056` preserved status, evidence, and
controller-owned Renderer fields, applied the semantic change, and appended
revision history. Validation was rerun at `bio_agents.py:10938-10948`.

Unmodified steps were retained; the patch did not overwrite `current_plan`.
The effective machine Plan is stored in `plan_state.json` and
`plans/active_plan.json`; the rendered effective checklist is in `plan.md`.
Both machine files contain steps 1, 2, 4, 5, and 6.

On approval, `plan_review()` passes the runtime full Plan text to
`_ensure_plan_tracker()`, but `ensure_plan_state()` at
`agent_core/plan_tracker.py:3642-3656` returns the already persisted machine Plan
whenever it has steps. Therefore Generate would consume the full effective
PlanTracker state, not the patch JSON.

## Why Plan Review showed only step-4

The direct cause is `bio_agents.py:11224-11229`:

```python
review_diff = persisted_plan.get("pending_review_diff") or []
review_content = (
    "## Plan Change Review\n\n" + json.dumps(review_diff, ...)
    if review_diff else plan
)
```

`output.page_text()` displays `review_content` at line 11241. Thus any non-empty
review diff replaces the complete Plan. `pending_review_diff` was last written
from patch audit data at `bio_agents.py:11039-11049`.

This is a confirmed `PLAN_REVIEW_RENDERING_ERROR`, not a patch application or
state corruption error. The correct boundary is:

- primary display: full effective Plan;
- secondary display: concise change summary;
- audit persistence: raw typed patch/diff.

## Prompt budget audit

Actual initial Planner request length: 42,879 characters. Budget:
`LLM_STEP_EPHEMERAL_CHARS=26000`. The request was sent unchanged because
`bio_agents.py:10694-10700` only logs a warning.

| Section in actual request | Characters | Assessment |
|---|---:|---|
| user preferences | 311 | small |
| clipped `User Goal` | 828 | duplicates TaskSpec source text |
| effective user facts + source refs | 18,552 | dominant; intended 6,000 limit is bypassed |
| Consultant handoff | 2,535 | useful but repeats runtime/input facts |
| memory context | 1,043 | current-task packet, partly repetitive |
| runtime audit | 1,912 | useful but duplicated in handoff/execution card |
| KB context | 31 | negligible |
| workflow state | 95 | negligible |
| static Planner protocol | 10,758 | large but deterministic |
| selected-skill contract/execution card | 6,774 | required subset plus repeated runtime facts |

The root of the ineffective TaskSpec budget is
`format_effective_user_facts_context()` at
`agent_core/task_spec.py:1971-1996`: it budgets the fact payload, then appends the
complete `source_refs` outside that budget. The full task appears twice in the
final prompt. Runtime readiness appears in Consultant handoff, the dedicated
runtime audit, and the mandatory execution card.

The repair request was 33,770 characters: 18,552 characters of effective facts
plus 14,667 characters of selected-skill/repair contract. It also had no
truncation.

Budget excess increased latency, tokens, and failure risk, but did not cause the
observed repair. The initial response was complete, parsed successfully, and
failed one deterministic coverage check for a genuinely omitted output.

## Skill and memory visibility

`active_skill_docs=0` is intentional. Planner receives:

- the Consultant-selected shallow skill catalog;
- a reference index;
- mandatory execution cards containing Renderer ID, entrypoint, argument
  schema, runtime, and healthcheck.

This is assembled at `bio_agents.py:10595-10627`. Full SKILL.md loading is
deferred to Generate for the active step. The skill therefore did enter Planner
context through `skill_execution_context`; it was not absent.

The Planner memory section came from this task's Consultant memory packet.
Planner trace `retrieval_refs` is empty, and no old archive chunk or hidden
fixture was injected into the call. No memory chunk introduced
`ann_gene_summary.tsv`. The displayed `compressed 7x / 7 archives` counter does
not establish cross-task retrieval, and this trace provides no evidence of
memory contamination.

## Additional state-integrity findings

### TaskSpec false required inputs (high)

`extract_path_tokens()` at `agent_core/task_spec.py:464-475` treats broad
slash-containing prose as a path. The structured state consequently contains
false candidate/required inputs such as:

- the explicitly forbidden `tests/demo_ref.fa`;
- slash phrases such as `ask_user/blocked_need_user_decision` and
  `benchmark/hidden/fixture`;
- skill IDs and tool prose containing `/`;
- an unresolved shorthand `data/genome.fasta` in addition to the verified
  formal benchmark path.

`extract_request_contracts()` filters by input suffix at lines 644-666, but the
generic effective-fact extraction path still admits the broad tokens. Negative
directives are not applied per artifact, so “禁止使用” can become
`source=user_explicit, required=true`. This did not trigger the observed repair,
but it is a real execution and prompt-bloat risk.

### Renderer normalization metadata gap (medium)

`collapse_renderer_composite_steps()` at
`agent_core/plan_tracker.py:3318-3411` sets `renderer_composite=True` and the
tool name, but does not persist the canonical `renderer_id` on the anchor. Later
logic can often infer it from the selected skill, but the machine contract is
weaker than intended.

### Duplicate Renderer-owned work (high)

The current effective Plan retains separate steps for
`ann_effect_summary.tsv`, `report.md`, and `reproduce.sh`, although the Renderer
manifest owns those outputs and `stage_status.tsv`. This contradicts the Planner
rule at `prompts/manager.py:631-633` and risks duplicate generation, conflicting
artifact producers, or unnecessary Generate/Execute cycles.

### Required output missed by legacy coverage (high)

TaskSpec marks `stage_status.tsv` required, but the effective Plan does not
declare it. The legacy raw-text coverage regex only scans the `预期产物` section,
so it did not request repair. The later Renderer manifest would promise the
file, but the pre-review logical Plan contract is incomplete.

### Metrics artifact pollution (medium)

`run_metrics.json.final_result.output_artifacts` includes fragments and skill
documentation/script paths such as URL tails and compiled Python cache paths.
This is another broad path-extraction symptom. It did not affect Plan Review but
can corrupt reporting and completion evidence.

## Root-cause classification

### F1

```yaml
category: PLANNER_OUTPUT_ERROR
severity: high
status: confirmed
first_bad_transition: complete user contract -> initial Plan omits ann_gene_summary.tsv and splits Renderer-owned work
user_visible_effect: planner_repair call and extra latency
execution_risk: duplicate artifact producers and incomplete logical output contract
responsible_module: Planner prompt compliance and deterministic post-normalization coverage
```

### F2

```yaml
category: PLAN_REVIEW_RENDERING_ERROR
severity: high
status: confirmed
first_bad_transition: full repaired effective Plan + review diff -> review_content becomes raw diff only
user_visible_effect: only step-4 appears
execution_risk: unsafe/incomplete human approval information; execution state itself remains full
responsible_module: bio_agents.py plan_review
```

### F3

```yaml
category: PROMPT_BUDGET_ERROR
severity: medium
status: confirmed
first_bad_transition: bounded projection -> unbounded source_refs appended -> warning-only overbudget send
user_visible_effect: 70-second initial Planner call and 12-second repair call
execution_risk: latency, cost, and elevated transport/model failure risk
responsible_module: task_spec context formatter and Planner prompt assembly
```

### F4

```yaml
category: REQUIREMENT_LINEAGE_ERROR
severity: high
status: confirmed
first_bad_transition: negative/path-like request prose -> active required input facts
user_visible_effect: prompt bloat; latent contradictory input contract
execution_risk: forbidden or invalid path may be treated as executable input
responsible_module: task_spec extraction and directive-aware fact resolution
```

### Explicitly not confirmed

- `PLAN_PARSE_ERROR`: no.
- `PATCH_APPLICATION_ERROR`: no.
- `STATE_OVERWRITE_ERROR` for effective Plan: no.
- `MEMORY_CONTAMINATION`: no evidence.
- `INPUT_TRUNCATION`: no.
- `UNSUPPORTED_REQUIREMENT_INJECTION` for `ann_gene_summary.tsv`: no; it is
  user-explicit.
- Hidden fixture exposure: no evidence, and no hidden answer file was read.

## FIRST_EFFECTIVE_DEVIATION

For the reported Planner/repair route, the first effective deviation is the
initial Planner output: it omitted the explicit required
`ann_gene_summary.tsv` and did not obey the Renderer composite ownership rule.
The deterministic local repair of the missing gene summary was a correct
reaction. The later “only step-4” symptom is a separate Plan Review rendering
error.

The TaskSpec path-classification defect occurs earlier during request ingestion,
but it is an independent state-integrity issue rather than the cause of this
specific repair trigger.

## Minimum repair plan (not implemented)

1. **Plan Review rendering** - modify `plan_review()` in `bio_agents.py` so the
   effective Plan is always primary; render a bounded human-readable diff below
   it. Keep raw patch JSON only in audit files.
2. **One coverage source** - replace `_skill_plan_violations()` raw request regex
   output coverage with TaskSpec `expected_outputs/output_contracts`, including
   source/required level. Validate again after Renderer normalization and patch.
3. **Renderer-aware coverage** - reconcile logical required outputs against the
   Renderer execution card before review. Keep only outputs not owned by the
   Renderer as separate steps. Persist canonical `renderer_id` during collapse.
4. **Directive-aware input extraction** - filter path candidates by semantic
   direction and negative directives; never promote forbidden/demo/path-like
   prose to active required inputs. Keep unresolved mentions as non-active audit
   facts at most.
5. **Real prompt budget** - include source references inside the projection
   budget, deduplicate the repeated goal/runtime facts, and fail/compact
   deterministically before sending an overbudget Planner request.
6. **Metrics output extraction** - derive final artifacts from structured output
   contracts/provenance, not generic slash-token scans over skill text.

No new Plan manager, state model, or review store is needed. Reuse TaskSpec,
PlanState, PlanPatch audit, PlanTracker, Renderer metadata, and existing prompt
projection helpers.

## Required regression tests

1. Plan Review with no patch displays the full effective Plan.
2. Plan Review after a typed patch displays the full repaired Plan plus a
   secondary summary, never raw diff alone.
3. Approval after repair and Generate both consume persisted effective
   PlanTracker state, not patch JSON.
4. Renderer normalization preserves stable IDs and canonical `renderer_id`.
5. TaskSpec-required output outside the `预期产物` section is still covered.
6. Renderer-owned outputs do not create duplicate Plan steps; a user-only output
   remains a separate logical step.
7. A model-created output without user/TaskSpec/skill provenance is not upgraded
   to `user-expected`.
8. Negative directives such as “禁止使用 tests/demo_ref.fa” never create active
   required inputs.
9. Slash-containing control prose is not parsed as a path.
10. Effective-facts context, including source refs, never exceeds its requested
    character budget.
11. Cross-task memory output names cannot mutate current TaskSpec requirements.
12. Original input hash remains traceable through TaskSpec and Planner call.
13. Metrics output artifacts come only from structured contracts/provenance.

## Existing-test observation

Read-only focused run:

```text
python -m unittest tests.test_plan_tracker tests.test_plan_lifecycle tests.test_prompt_manager
Ran 95 tests; 94 passed, 1 failed.
```

The failure was
`test_step_completion_candidate_advances_only_after_evidence_gate`: the gate
picked unrelated existing status/report declarations under `/tmp` and reported
missing `/public/home/sunyehan/finished_samples.txt`, `/tmp/seurat_qc.rds`, and
`/tmp/report.md`. This is not caused by the audit document, but it is a real
test-isolation/output-root selection issue in the current dirty baseline and
should be recorded before implementation.

## Approval recommendation

Do **not** approve the current Plan as-is.

Although approval would execute the full effective Plan rather than the JSON
patch, the user cannot review that Plan in the current UI, `stage_status.tsv` is
missing from the logical Plan contract, several Renderer-owned outputs are
duplicated as independent steps, and TaskSpec contains false required inputs.
Preserve this session as evidence. Apply and test the minimum fixes in a new
change, then reproduce with a new task ID.
