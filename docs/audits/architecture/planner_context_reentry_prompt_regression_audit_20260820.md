# BioMind Current HEAD Planner / Global Context Regression Audit

- Status: **READ-ONLY ARCHITECTURE AUDIT COMPLETE**
- Date: 2026-08-20
- Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`
- Primary incident: `session_1787190323`
- Comparison incident: `session_1787136833`

This audit changed no production code, tests, runtime state, or session artifacts; ran no workflow, snpEff, Java, database build, Generate action, or shell action from either session; and created no Git commit. The only repository writes are this requested report and its two requested TSV inventories.

## 1. Executive verdict

The regression is not one isolated “Planner prompt typo,” and it is not missing user context caused by compression. Current HEAD does serialize the complete raw user turn, accepted Consultant handoff, canonical TaskSpec semantic projection, selected capability, and runtime audit into Planner. The decisive failure chain is:

1. The active Planner prompt embeds a complete, validator-compatible semantic `PLAN_CANDIDATE`. DeepSeek returned that object **exactly**, including `Create the intermediate summary`, in calls 0003 and 0006.
2. The example candidate did not produce any required TaskSpec output, so deterministic validation produced five `REQUIRED_OUTPUT_UNCOVERED` and five `TASK_OUTPUT_MATERIALIZATION_MISSING` issues.
3. Planner Repair 0004 saw the candidate and all ten issues, but global missing-producer issues had no `affected_step_ids`; `build_plan_repair_request()` therefore authorized `allowed_step_ids=[]`.
4. Repair correctly proposed replacing step 1 and materializing all five outputs, but `apply_plan_patch()` rejected it with `Plan patch modifies out-of-scope step: step-1`. This is an **UNREPAIRABLE_REPAIR_CONTRACT** for a global violation requiring a new/replaced producer.
5. The rejection was durably stored in `task_state.planner_candidate_rejection`, while the next Planner initial builder read only `task_state.planner_feedback`. Neither the rejection nor `state.validation_issues` entered calls 0005 or 0006.
6. Calls 0003, 0005, and 0006 therefore had byte-identical provider messages and identical request hashes. Call 0005 stochastically produced a task-specific plan; call 0006 stochastically copied the example again.
7. Call 0005 also redeclared all five TaskSpec outputs as plan-local artifacts. The pre-allocation validator allowed the shadowing. The output validator accidentally counted each bare `local_ref` as the same-named TaskSpec `output_id`; after allocation rewrote it to `plan-artifact-*`, Plan Review correctly found every required output uncovered.
8. The 0004 raw repair response had been cached before parse, scope authorization, apply, or revalidation. On the repeated example candidate it was replayed without another provider call, failed the same scope check, and triggered `PLANNER_VALIDATION_NO_PROGRESS`.
9. The durable Planner diagnosis was later collapsed to generic `TASK_BLOCKED` / “A node reported a terminal blocked recovery state,” obscuring the primary and secondary causes.

The earlier “normal plan” in `session_1787136833` is not evidence that this architecture was correct. Triage and Consultant requests were byte-identical across the two sessions, but temperature-0.7 Consultant responses differed. The older response built a narrower TaskSpec that omitted `stage_status` as an output contract, and its Planner happened to use `output.*` for TaskSpec outputs and only one genuine plan-local database directory. That candidate passed and was approved; the run later failed during execution.

### Direct answers

| Question | Current-HEAD answer |
|---|---|
| Is the configured Planner contract in the API request? | **YES.** Unique V2 strings and the generated candidate object occur in each `planner_initial-*/request.json`. |
| Is it a real `SystemMessage`? | **NO.** Planner initial, structure repair, and local repair are sent as one `HumanMessage`. `PromptManager` returns text; the caller chooses the role. |
| Why did the model copy the example? | It was shown a complete, valid semantic candidate, not a neutral schema. Responses 0003/0006 are string-exact and JSON-object-exact copies. The same prompt also produced a task-specific 0005 response, so stochastic sampling participates, but temperature is not the contract root cause. |
| Did raw user / TaskSpec / Consult information reach Planner? | **YES.** They occupy 9,769 characters / 3,602 attributed offline CL100K tokens in every initial request. |
| Did compression remove the failure? | **NO.** Truncation/redaction manifests are empty and the decision is `no_compression`. The loss occurs before serialization: the caller does not feed durable rejection/validation fields into `current_focus`. |
| Is `semantic_history + current_focus` implemented? | **PARTIALLY_IMPLEMENTED / NOT_WIRED for Planner failure continuity.** The shared builder works; the Planner re-entry caller omits the pending failure state. |
| Why are 0003/0005/0006 request hashes equal? | `ContextLineageStore.start_call()` hashes the redacted `messages` object only. Those messages are byte-identical; changed `state_generation`, invocation ID, candidate scope, and hidden plan hash live in metadata, not provider content. |

## 2. Git and working-tree boundary

```text
current HEAD: 516db73aeadd81a2bebe567b80b75b996237c757
branch: codex/save-current-work
HEAD subject: refactor: stabilize execution recovery contracts
working tree: dirty before this audit; production files decisive to this audit had no local diff
```

Latest relevant commits, newest first:

```text
516db73 refactor: stabilize execution recovery contracts
8ce98f1 fix: make planner repair contract deterministic and convergent
0a82a03 fix: make generate repair preserve precise protocol errors
4d0dd6d refactor(plan): retire heuristic skill planning contracts
9ae9c7b docs(audit): record planner contract and snapshot findings
2075849 fix(planner): gate approved snapshot loading on approval state
151a6e7 refactor(constraints): migrate generic semantics to open-text requirements
77d7130 test(context): cover semantic history and stable prompt prefixes
9d4255f fix(recovery): remove legacy context gate from managed recovery
60c3158 refactor(context): add semantic continuity to NodeContext V2
69e1bf4 docs(audit): document global prompt and cache lifecycle
02d8232 docs(audit): document recovery progressive disclosure contract
358c31d docs(audit): record runtime and recovery context contract
34934de fix(agent): reconcile managed artifact realizations
174fece fix(recovery): preserve preaction root cause and skip no-execution review
6cd6eb9 refactor(plan): confine success-criteria artifact inference to legacy plans
53920e3 fix(plan): require explicit snpeff intermediate artifact dataflow
7314418 fix(generate): transport managed action env and repair placeholder misuse
b2ba4e4 fix(agent): close plan-local binding and pre-action convergence
99f66a9 fix(agent): unify output contracts and retire renderer execution path
```

Pre-existing tracked modifications are concentrated in skill documents and tests; many docs, scripts, skills, and tests are also untracked. They are user-owned and were preserved. After this audit, the three requested audit files are additional untracked files. The factual code boundary is current HEAD production code plus the observed dirty tree; `git diff` showed no local production change in `bio_agents.py`, `agent_core/{node_context,plan_contract,plan_tracker,recovery,context_lineage}.py`, `prompts/{manager,templates}.py`, or `supervisor.py`.

## 3. Historical audit baselines and drift

| Historical artifact | Audit snapshot / last tracked revision | Current status | Changed production surface | Current behavior difference |
|---|---|---|---|---|
| `global_prompt_context_lifecycle.md` | Audit snapshot `02d8232f...`; amended in `151a6e7...` | **PARTIALLY_STALE** | `60c3158` changed `agent_core/node_context.py`, `bio_agents.py`, and `prompts/manager.py`; later 151a/8ce/516 changed the same lifecycle | Its durable-vs-visible and provider-cache conclusions remain useful. Its statements “Planner/Generate raw user = NONE” are superseded: current semantic nodes receive full user turns through `semantic_history.conversation`. |
| `generic_semantic_constraint_retirement_highlight_migration_audit_20260818.md` | Embedded snapshot `77d71307...`; currently untracked | **PARTIALLY_STALE** | 151a retired generic claim coverage; 4d0/8ce/516 changed plan/recovery contracts | The open-text Highlighted Constraint design and shared `semantic_history/current_focus` intent remain current. This incident shows the builder exists but Planner rejection continuity is not wired at the caller. |
| `runtime_and_recovery_context_contract.md` | Embedded snapshot `34934de0...`; last tracked in `02d8232...` | **PARTIALLY_STALE** | 516 added/changed controller audit, diagnostics, middleware, recovery, recovery evidence, NodeContext, prompts, and `bio_agents.py` | Runtime provenance distinctions remain valid, but current Recovery disclosure and execution-recovery contracts are broader/newer. Current Planner still receives a heavily duplicated runtime audit. |
| `planner_constraint_provenance_projection_audit_20260818.md` | Embedded snapshot `77d71307...`; currently untracked | **SUPERSEDED** for current contract; historical incident remains valid | 151a removed the generic semantic-claim architecture; 8ce changed Planner repair | Its old incident root cause remains evidence, but its semantic-constraint/claim/handle repair boundary is not the current Planner contract being exercised here. |
| `node_prompt_payload_matrix.tsv` | Tracked in `69e1bf4...`, based on older sessions | **SUPERSEDED** for current node visibility | 60c semantic continuity; later open-text and recovery changes | Historical sizes remain historical evidence. Planner/Generate/Recovery raw-user columns are no longer current. |
| `semantic_history_authority_matrix.tsv` | Last tracked in `151a6e7...` | **SUPERSEDED** for Planner/Generate/Recovery visibility | 60c added user conversation, accepted handoff/outcomes/decisions to semantic nodes; later recovery changes | Rows still saying raw user is provider-invisible conflict with current source and the 2026-08-20 trace. |
| `planner_pending_snapshot_reentry_fix_report_20260819.md` | Implementation `2075849d...`; report tracked in `9ae9c7b...` | **STILL_CURRENT** within its narrow boundary | Later 8ce/516 touched Planner/recovery, but the approved-status gate remains | Plan Review can now return to Planner without treating a pending snapshot as approved. The present regression is immediately downstream: that re-entry omits pending failure context. |

The exact change that made raw user text visible is `60c3158 refactor(context): add semantic continuity to NodeContext V2`. It introduced `_semantic_conversation_rows()`, added `conversation_turns`, accepted handoff, execution records, and recovery decisions to `build_node_context()`, defined semantic nodes as Planner/Planner Repair/Generate/Recovery, and added `BioAnalystAgent._node_context_semantic_inputs()`. Current `session_1787190323` proves the implementation is active: `semantic_history.conversation[0].content` contains the complete 1,294-character user request.

## 4. Planner prompt sources and production reachability

| Source | Classification | Production evidence |
|---|---|---|
| `PromptManager.get_planner_v2_prompt()` | **ACTIVE_PROVIDER_PATH** | Called at `bio_agents.py:23547` and for structure repair at `24014`; observed unique text in 0003/0005/0006 requests. |
| `PromptManager.get_plan_repair_v2_prompt()` | **ACTIVE_REPAIR_PATH** | Called at `bio_agents.py:24357`; observed in 0004. |
| `planner_candidate_wire_example()` | **ACTIVE_SCHEMA_HELPER** | Defines the complete semantic example at `agent_core/plan_contract.py:220-265`. |
| `validated_planner_candidate_wire_example()` | **ACTIVE_SCHEMA_HELPER** | Validates the example against the same reader/validator and is called by the active prompt builder. |
| `build_node_context()` | **ACTIVE_PROVIDER_CONTEXT_PATH** | Builds Planner, repair, Generate, and Recovery semantic payloads; ordinary Consultant/Supervisor use compatibility payloads. |
| `candidate_artifact_ref_keys()` / `validate_planner_candidate_deterministically()` | **ACTIVE_DETERMINISTIC_ADMISSION** | Parses authoritative handles and candidate-local declarations before allocation. |
| `prompts/templates.py::PLANNER_PROMPT` and `get_planner_prompt()` | **DEAD / UNUSED in current production** | No Python call site beyond the wrapper definition. |
| `get_detailed_planner_prompt()` | **TEST_ONLY / LEGACY** | Referenced by tests, not `bio_agents.py` production dispatch. |
| `get_plan_repair_prompt()` | **TEST_ONLY / LEGACY** | Referenced by tests, not the current repair dispatch. |

Active initial call graph:

```text
PromptManager.get_planner_v2_prompt
  -> BioAnalystAgent._build_graph.<locals>.planner
  -> [HumanMessage(content=detailed_plan_prompt)]
  -> _ainvoke_node_llm
  -> _ainvoke_llm
  -> ContextLineageStore.start_call(messages, metadata)
  -> invocation_llm.ainvoke(messages)
  -> context_trace/planner_initial-*/request.json
```

Active local-repair call graph:

```text
build_plan_repair_request
  -> build_node_context(node_type="planner_repair")
  -> PromptManager.get_plan_repair_v2_prompt
  -> [HumanMessage(content=repair_prompt)]
  -> _ainvoke_node_llm / _ainvoke_llm / ainvoke
  -> context_trace/planner_repair-*/request.json
```

### Was the configured Planner prompt sent?

Yes. The static V2 contract occupies zero-based half-open span **`[0, 4479)`** in every current initial request (one-based characters 1-4479). The first dynamic opening tag starts at offset 4479. Repair's static prefix is `[0, 1749)`.

| Unique active string | Defined in code | Provider request | Role | Zero-based span in each initial request |
|---|---|---|---|---|
| `You are the BioMind Planner.` | `prompts/manager.py:1649` | YES | human | `[0, 28)` |
| `semantic_history ... current_focus ... non-authoritative` | `prompts/manager.py:1651-1652` | YES | human | begins at 30 |
| `TASKSPEC_RECONCILIATION_REQUIRED` | `prompts/manager.py:1655-1665` | YES | human | `[444, 476)` |
| `Create the intermediate summary` | generated from `plan_contract.py:244` | YES | human | `[1538, 1569)` |
| `Required managed outputs must be referenced by handle` | `prompts/manager.py:1679` | YES | human | begins at 2721 |

Therefore:

- If a prior edit changed `get_planner_v2_prompt()`, it changed the active provider text, even though the role remained `human`.
- If it changed `PLANNER_PROMPT`, `get_planner_prompt()`, `get_detailed_planner_prompt()`, or `get_plan_repair_prompt()`, it changed a dead/legacy/test-only path and did not affect these calls.
- A function name or prose label “system prompt” does not create a system role. `bio_agents.py:23709-23711` and `24411-24413` explicitly construct `HumanMessage`. Ordinary Consultant V2, by contrast, constructs a `SystemMessage` at `21140-21143`.

## 5. Planner examples and exact response equality

| Example / phrase | Source | Function | Skill-derived? | Runtime injected? | Provider visible? |
|---|---|---|---|---|---|
| `Create the intermediate summary` | `agent_core/plan_contract.py:244` | `planner_candidate_wire_example()` | NO | NO | YES |
| `PLAN_CANDIDATE` | `agent_core/plan_contract.py:228` | `planner_candidate_wire_example()` | NO | NO | YES |
| `TASKSPEC_RECONCILIATION_REQUIRED` shape | `prompts/manager.py:1655-1665` | `get_planner_v2_prompt()` | NO | NO | YES |
| Full proposed plan-local artifact | `agent_core/plan_contract.py:229-240` | `planner_candidate_wire_example()` | NO | NO | YES |
| `task_output_materializations` row | `prompts/manager.py:1680-1685` | `get_planner_v2_prompt()` | NO | NO | YES |
| `database_dir` plan-local phrase | `prompts/manager.py:1689-1699` | `get_planner_v2_prompt()` | NO | NO | YES |
| snpEff execution guidance | selected skill capability projection | skill manager / capability context | YES | dynamically selected | YES, separate from the example |

`Create the intermediate summary` does **not** come from `skills/genomics/snpeff-annotation/SKILL.md`. It is source-code data in the active wire example.

The generated example is 1,193 characters / 297 standalone offline CL100K tokens. All schema/examples together are 1,613 characters / 394 attributed tokens in the full request. The example is validator-compatible by construction: `validated_planner_candidate_wire_example()` parses it, runs deterministic reference/flow validation, and prepares allocation intent before returning it.

Response equality:

| Response | Text equality to generated pretty JSON | Field similarity | JSON semantic equality | Exact object equality | Canonical object SHA-256 |
|---|---:|---:|---:|---:|---|
| 0003 | YES | YES | YES | **YES** | `12687bc61279a949ee4e2944cc3039f45e15dac8553d841d1be7a5322c24a4f5` |
| 0005 | NO | partial schema only | NO | NO | `de045d40e8cf432c399e07bbcc4a512811a9c83a6d15a58e729f7b6f7a3d6bba` |
| 0006 | YES | YES | YES | **YES** | `12687bc61279a949ee4e2944cc3039f45e15dac8553d841d1be7a5322c24a4f5` |

This is stronger than keyword similarity: 0003 and 0006 are exact copies of the source-generated object. The prompt also says not to copy illustrative values, but that negative instruction competes with a complete executable demonstration.

## 6. Current-HEAD node/provider context lifecycle

The complete field matrix is in `node_provider_context_matrix_20260820.tsv`. Key corrections to the historical matrix are:

- Triage: full raw user in one `HumanMessage`; no NodeContext.
- Consultant V2 in this session: one `SystemMessage`; full user evidence, candidate capability catalog, current compiled TaskSpec, consultation state, and controller feedback. It does not use the semantic-history layout.
- Planner initial: one `HumanMessage`; full user conversation, accepted handoff, TaskSpec semantic view, selected capability, and full runtime input audit. An Approved Plan is included only when approval is actually `approved`.
- Planner repair: one `HumanMessage`; stable semantic history plus the previous candidate, current violations, and allowed operation/scope.
- Plan Review: **NO PROVIDER PROMPT**. It deterministically revalidates and then interacts with the human if valid.
- Generate: one `HumanMessage`; stable semantic history, Approved Plan semantic view, current step/dependencies, resolved runtime/action/artifact context, and bounded repair evidence.
- Execute/Controller: **NO PROVIDER PROMPT**.
- Supervisor: deterministic validation first; optional advisory uses one `HumanMessage` and compatibility NodeContext, not shared semantic history.
- Recovery: one `HumanMessage`; shared semantic history plus current step/dependencies, structured failure/runtime context, current violations/evidence, and accepted recovery summaries.
- Final audit: no independent LLM node; optional scientific/artifact advisory is the Supervisor path.

The key distinction is: `StateContractStore`, PlanState, trace, and recovery stores may durably hold a fact, but a model sees it only if the node's builder serializes it. `planner_candidate_rejection` is the decisive counterexample in this incident.

## 7. Provider request component accounting

Exact non-overlapping rows for all six real requests are in `planner_request_component_inventory_20260820.tsv`.

Method:

- Character spans partition the exact persisted request string with no overlap.
- Provider token totals come from `run_metrics.json` and are not estimated.
- Offline component tokens use `tiktoken` `cl100k_base`; the complete request is encoded once and each token is attributed exactly once to the component owning its first source byte. This preserves additive totals across BPE boundaries and is not a claim about DeepSeek's tokenizer.
- `trace_chars_div4_estimate` records the current trace estimate for comparison only.
- Duplicate columns are a conservative lower bound: each repeated JSON string scalar of at least eight characters is counted in full after its first occurrence. They do not claim to find paraphrase or substring duplication.

### Real call totals

| Call | Role | Characters | Provider prompt tokens | Offline CL100K | Trace chars/4 estimate |
|---|---|---:|---:|---:|---:|
| task_router-0001 | human | 2,989 | 933 | 1,029 | 747 |
| consultant-0002 | system | 36,463 | 9,310 | 8,975 | 9,115 |
| planner_initial-0003 | human | 30,558 | 9,724 | 9,565 | 7,639 |
| planner_repair-0004 | human | 22,547 | 7,123 | 7,114 | 5,636 |
| planner_initial-0005 | human | 30,558 | 9,724 | 9,565 | 7,639 |
| planner_initial-0006 | human | 30,558 | 9,724 | 9,565 | 7,639 |

Run total: **46,538 prompt tokens, 4,955 completion tokens, 51,493 total tokens**, all reported by the provider; six LLM calls and zero commands.

### Initial Planner decomposition

| Component | Characters | Character % | Offline tokens | Offline % |
|---|---:|---:|---:|---:|
| static protocol excluding examples | 2,905 | 9.51% | 604 | 6.31% |
| schema/examples | 1,613 | 5.28% | 394 | 4.12% |
| raw user evidence | 1,404 | 4.59% | 723 | 7.56% |
| canonical TaskSpec | 4,989 | 16.33% | 1,746 | 18.25% |
| accepted Consultant handoff | 3,376 | 11.05% | 1,133 | 11.85% |
| selected capability | 3,137 | 10.27% | 687 | 7.18% |
| runtime | 12,551 | **41.07%** | 4,005 | **41.87%** |
| envelope/preferences/allowed operations | 583 | 1.91% | 273 | 2.85% |

Definitions matter when comparing “examples” with “task facts”:

- Full semantic candidate example alone: **1,193 chars / 297 standalone offline tokens**.
- All schema/examples in the full request: **1,613 / 394 attributed tokens**.
- Raw user + canonical TaskSpec + accepted handoff: **9,769 / 3,602 attributed tokens**.
- Those task facts plus selected capability: **12,906 / 4,289 attributed tokens**.

The current task facts were therefore not absent or squeezed out. The exact-copy risk comes from example form and authority/identity ambiguity, not from a lack of task text.

The conservative duplicate lower bound for one initial Planner request is **7,604 chars / 2,855 offline tokens**. Runtime alone contributes **5,753 / 2,044** of that lower bound. Current telemetry reports `duplicate_chars=0`, so its duplicate accounting is not trustworthy.

### Compression and telemetry verdict

For each initial call, the manifest records:

```text
decision = no_compression
optional_compression_applied = false
truncation_manifest = []
redaction_manifest = []
context_limit_tokens = 200000
```

No prompt compression or capacity projection removed the violations. `build_node_context()` also explicitly refuses to shorten required semantic lists to fit `max_payload_chars`.

There are three observability defects:

1. Planner accounting serializes `llm_payload.to_dict()` including compatibility aliases, while the prompt builder extracts only provider sections. It reports `llm_payload=40,938` for a complete 30,558-character prompt and then clamps static protocol to zero, despite labeling the map non-overlapping.
2. The trace's 7,639-token estimate undercounts the provider's 9,724 by 2,085 (21.44%). `chars/4` is not adequate as the sole telemetry.
3. `runtime_audit_ref` points to a nonexistent `.../session_1787190323/runtime_audit.json`; runtime evidence is actually embedded in state/request. Provider cache-token details are not persisted, so provider prefix-cache hits remain `NOT_OBSERVABLE`.

## 8. Planner authority hierarchy

Current prompt language contains a real ambiguity:

- `get_planner_v2_prompt()` calls the entire JSON under `semantic_history` and `current_focus` a **non-authoritative semantic view**.
- The same prompt calls every `task_spec.highlighted_constraints` item **authoritative**.
- It says to use artifact handles exactly as shown, but does not define a complete precedence order among raw conversation, accepted handoff, TaskSpec, capability output IDs, runtime facts, examples, and optional Planner feedback.

Potential fact sources visible to initial Planner are:

| Source | Current presentation | Explicit conflict precedence? |
|---|---|---|
| Raw conversation | full user turn in `semantic_history.conversation` | No global order; “plan only from its facts” |
| Canonical TaskSpec | semantic projection in `semantic_history.task_spec`; highlighted constraints locally called authoritative | Partial: constraints are authoritative; the whole TaskSpec is not explicitly declared the single task-semantic authority |
| Accepted Consultant handoff | duplicated summary/goal/inputs/outputs/skills | No explicit precedence against TaskSpec |
| Capability context | selected capability output IDs and defaults | No explicit precedence against TaskSpec output identities |
| Runtime | large machine-generated input audit | No explicit statement that runtime evidence overrides stale handoff/runtime prose |
| Examples | complete semantic candidate plus inline examples | Told to adapt and not copy, but not typed as schema-only/non-factual placeholders |
| Planner feedback | optional `current_focus.feedback.planner_feedback` | No general order; absent in this incident |

The architecture implied by existing state ownership would be clearer as:

```text
Canonical TaskSpec = current task-semantic authority
Approved Plan      = approved plan authority for downstream nodes
Raw user/handoff   = evidence and provenance, used to detect reconciliation needs
Runtime/execution  = current machine evidence
current_focus      = current repair/review objective
```

Current HEAD does not state this complete order in one enforceable prompt contract. This audit records the gap and does not change it.

## 9. TaskSpec output projection

### Canonical outputs in session_1787190323

| output_id | model handle | role | format | required | canonical path_policy | provider placeholder | object_kind | materialization policy |
|---|---|---|---|---:|---|---|---|---|
| `output_dir` | `output.output_dir` | output_dir | directory | true | `managed_exact` | `{output:output_dir}` | directory | exact Controller-managed directory; no Planner filename row |
| `annotated_vcf` | `output.annotated_vcf` | annotated_vcf | file | true | `planner_materialized` | `{output:annotated_vcf}` | file | Planner must supply one relative path under `output_dir` |
| `ann_summary` | `output.ann_summary` | ann_summary | file | true | `planner_materialized` | `{output:ann_summary}` | file | Planner must supply one relative path under `output_dir` |
| `report` | `output.report` | report | file | true | `planner_materialized` | `{output:report}` | file | Planner must supply one relative path under `output_dir` |
| `reproduce_script` | `output.reproduce_script` | reproduce_script | file | true | `planner_materialized` | `{output:reproduce_script}` | file | Planner must supply one relative path under `output_dir` |
| `stage_status` | `output.stage_status` | stage_status | file | true | `planner_materialized` | `{output:stage_status}` | file | Planner must supply one relative path under `output_dir` |

The placeholder is generated by NodeContext; it is not stored in the canonical TaskSpec. Canonical file rows also contain `root_ref=output_dir`, but NodeContext does not project it.

### Field survival by representation

| Representation | output_id | handle | role | format | required | path_policy | placeholder | object_kind/root_ref |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Canonical TaskSpec `output_contracts` | YES | YES | YES | YES | YES | YES | NO | YES |
| NodeContext / Planner `semantic_history.task_spec.outputs` | **NO** | YES | YES | YES | YES | **NO** | YES | **NO** |
| accepted `consultant_handoff.required_outputs` | NO | NO | YES | YES | YES | YES | NO | NO |
| selected capability output rows | YES for four skill outputs | NO | implicit/bare ID | YES | NO | NO | NO | NO; supplies default filename |

`agent_core/node_context.py::_semantic_artifact_rows()` explicitly projects only handle, role, format, required, placeholder, optional description, and optional basename for outputs. It omits `output_id`, `artifact_kind`, `path_policy`, `root_ref`, and the materialization rule.

Thus the prompt tells Planner “when `path_policy` is `planner_materialized` ...” while the canonical TaskSpec projection does not contain `path_policy`. This session works around the loss only because accepted Consultant handoff duplicates `path_policy`. That duplicate is weaker and lacks stable output identity. This is **CONTEXT_PROJECTION**, not compression.

The selected capability context further exposes only four outputs (`annotated_vcf`, `ann_summary`, `report`, `reproduce_script`); it omits `output_dir` and `stage_status`. Capability output IDs are descriptors, not TaskSpec authority, but the prompt does not make that namespace distinction explicit.

## 10. Artifact identity namespaces and shadowing

| Object type | Model-facing identity | Controller canonical identity | Storage identity / path | Current ambiguity |
|---|---|---|---|---|
| TaskSpec input | `input.variant_vcf` | `artifact-bb6b32...` | verified/declared absolute VCF path | Low; prefix separates it from plan-local refs, but bare roles are still available elsewhere |
| TaskSpec output | `output.annotated_vcf` | `output_id/artifact_id = annotated_vcf` | final path selected by Task output materialization | Same semantic object appears with prefixed handle and bare controller ID |
| Plan-local artifact | candidate `local_ref=annotated_vcf`; later model references bare local ref | candidate-local `(scope, annotated_vcf)`; after allocation `plan-artifact-b80cff...` | `task_outputs/session.../plan_artifacts/plan-artifact-b80cff...` | Directly shadows TaskSpec output role/output_id |
| Materialized Task output | `output_ref=output.annotated_vcf`, `path=annotated.vcf` | `output_id=annotated_vcf` | `<canonical output_dir>/annotated.vcf` | Correct identity, but coexists with shadow plan-local artifact |
| Capability output | bare `output_id=annotated_vcf`, default `annotated.vcf` | capability descriptor, not TaskSpec identity | no storage authority by itself | Looks identical to the TaskSpec controller ID and plan-local ref |

Current admission does not require `proposed_artifacts.local_ref` or `semantic_view.role` to be disjoint from:

- TaskSpec input roles or handles;
- TaskSpec output roles, output IDs, or handles;
- capability output IDs.

`candidate_artifact_ref_keys()` first registers authoritative **handles** such as `output.annotated_vcf`, then adds candidate-local **bare refs**. Because `annotated_vcf != output.annotated_vcf`, no dictionary collision occurs. It never checks the local ref or semantic role against the authoritative binding's stable identity/role.

Why 0005 passed before allocation and failed after allocation:

1. 0005 declared plan-local refs `annotated_vcf`, `ann_summary`, `report`, `reproduce_script`, and `stage_status` and used those bare strings in step outputs.
2. Typed artifact-flow admission correctly considered them candidate-local, but did not reject namespace shadowing.
3. `validate_output_contracts()` independently builds `artifact_bindings` keyed by canonical bare output IDs. Before allocation, each bare plan-local output string happens to be in that map, so it is counted as the required Task output producer.
4. Atomic allocation rewrites each local ref to `plan-artifact-*` and stores it under the task-local `plan_artifacts` root.
5. Plan Review reloads and re-normalizes the persisted plan. `plan-artifact-*` is no longer a TaskSpec output ID, so all five canonical outputs have no producer and produce `REQUIRED_OUTPUT_UNCOVERED`.

The earliest deterministic-validation defect is the missing pre-allocation namespace/shadow collision rule. Plan Review is correct to reject the post-allocation plan.

## 11. Planner failure/re-entry state machine

### Real call ledger

`response object hash` below is SHA-256 of canonical sorted JSON. `binding plan hash` is hidden Controller metadata and is not part of the request hash.

| Call | Type | request hash | prompt/content SHA | state_generation | binding plan hash | response object / candidate hash | violations visible | previous candidate visible | repair rejection visible | current_focus |
|---|---|---|---|---:|---|---|---|---|---|---|
| 0003 | initial | `d3c7833a...` | `0caa32a2...` | 6 | `sha256:9a14fad0...` | exact example `12687bc6...`; candidate semantic `acda22bf...` | NO | NO | NO | runtime + allowed operations |
| 0004 | local repair | `d206af1f...` | request content `bff0bc2f...` | 0 (in-memory candidate binding) | `sha256:acda22bf...` | PlanPatch delta; later rejected | **YES: 10** | **YES** | not yet; rejection occurs after response | candidate_plan + violations + allowed operations; scope `[]` |
| 0005 | initial | `d3c7833a...` | `0caa32a2...` | 11 | `sha256:9a14fad0...` | task-specific `de045d40...`; post-allocation plan `sha256:0b50c90d...` | NO | NO | **NO** | runtime + allowed operations |
| 0006 | initial | `d3c7833a...` | `0caa32a2...` | 17 | `sha256:0b50c90d...` | exact example `12687bc6...`; candidate semantic `acda22bf...` | NO | NO | **NO** | runtime + allowed operations |

Initial request files 0003, 0005, and 0006 are byte-for-byte equal, not merely hash-equal. Hidden binding metadata changes, but the provider cannot see it.

### Actual route and state transitions

```text
Triage -> Consultant -> Planner 0003
0003 exact example -> deterministic output/materialization violations
-> Planner Repair 0004 with candidate + ten violations + allowed handles []
-> patch response proposes step-1 + five materializations
-> apply_plan_patch rejects step-1 as out of scope
-> planner_candidate_rejection persisted
-> route Planner initial 0005 without rejection/violations
-> 0005 plan-local-shadows five Task outputs
-> pre-allocation validation appears valid
-> allocation rewrites local refs to plan-artifact-*
-> pending snapshot -> Plan Review
-> Plan Review finds five REQUIRED_OUTPUT_UNCOVERED
-> route Planner initial 0006 without pending candidate/violations/rejection
-> 0006 exact example
-> cached 0004 repair replayed and rejected again
-> repeated rejection -> PLANNER_VALIDATION_NO_PROGRESS
-> terminal projection -> generic TASK_BLOCKED
```

The narrow `2075849` pending-snapshot fix is operating as designed: 0006 does not load the pending snapshot as an Approved Plan. But the caller then builds an initial context with neither the pending candidate nor the Plan Review issues, so the downstream lifecycle is incomplete.

### Core questions A-E

**A. Why did `REQUIRED_OUTPUT_UNCOVERED` enter 0004 but not 0005/0006?**

The same Planner invocation passes its freshly computed `violations` directly into `build_node_context(node_type="planner_repair")`, which serializes them under `current_focus.violations`. On a new initial invocation, `bio_agents.py:23476-23520` reads only `task_state.planner_feedback` and passes only that optional object as feedback. It does not read `state.validation_issues` or `task_state.planner_candidate_rejection`.

**B. Why was the out-of-scope rejection not in the next request?**

The exception creates `PLAN_PATCH_REJECTED` in the in-memory candidate and later persists a durable `planner_candidate_rejection` with terminal diagnostic. The subsequent initial-context builder has no projection from that durable field to `feedback`, `violations`, or `evidence`.

**C. Why is `planner_feedback = null`?**

The Consultant response explicitly returned `planner_feedback: null`. The Controller reserves this field for typed Plan Review/user planning instructions and only keeps feedback with status `pending`. Machine validation and repair rejection use different fields. No code bridges those fields into Planner feedback.

**D. Should `semantic_history + current_focus` have solved this?**

At the shared builder level: **IMPLEMENTED**. `build_node_context()` supports stable history and optional runtime, feedback, violations, evidence, and allowed operations. For Planner failure/re-entry continuity: **PARTIALLY_IMPLEMENTED / NOT_WIRED**, behaviorally a **REGRESSION against the design intent** that current repair focus follows stable history. The builder cannot serialize data the caller never supplies.

**E. Is there a failed-Planner-to-initial route that drops pending failure state?**

**YES, two observed forms:**

1. Local repair rejected -> Planner returns `next_step="planner"` -> next initial context omits durable rejection.
2. Plan Review invalid -> returns `validation_issues` and `next_step="planner"` -> next initial context ignores `state.validation_issues` and pending candidate because it loads only an approved snapshot.

## 12. Repair-scope contract

`build_plan_repair_request()` authorizes existing steps only when a violation supplies `affected_step_ids` or a `steps[N]` field path. This is appropriate for a local violation:

```text
localized issue -> allowed existing step IDs -> PATCH_EXISTING_STEPS
```

For a global missing-producer violation, `validate_output_contracts()` correctly returns no owners and therefore `affected_step_ids=[]`. The repair-scope builder intentionally refuses to widen an unlocated issue to all steps. That safety rule is sound in isolation, but the only active PlanPatch operation is `replace_steps`, and `apply_plan_patch()` rejects any replacement not in the empty set.

```text
global REQUIRED_OUTPUT_UNCOVERED
  -> allowed_step_ids=[]
  -> model must create/replace a producer to fix coverage
  -> every step replacement forbidden
  -> UNREPAIRABLE_REPAIR_CONTRACT
```

A materialization-only patch may use `replace_steps=[]`, but it cannot repair missing logical producers. Current code has no general `FULL_PLAN_REPLACEMENT` PlanPatch operation. The separate `replace_plan_document` path is used only for malformed structured Planner output and asks for a corrected complete candidate; it is not selected for global contract violations.

## 13. Repair cache lifecycle

Current ordering is:

```text
LLM response
  -> save_llm_result(raw_response, parsed_result={})
  -> parse_plan_patch
  -> resolve handles
  -> stamp control binding
  -> staleness/scope authorization
  -> apply_plan_patch
  -> deterministic validation
```

The write at `bio_agents.py:24430-24435` occurs before parsing at 24436 and before authorization/apply at 24445-24463. Planner calls `load_llm_result()` without `cache_identity` or invalid candidate fingerprints.

| Cache state | Persisted distinctly? | Current implementation |
|---|---:|---|
| RAW | **YES** | `raw_response` saved immediately |
| PARSED | NO | transient; `parsed_result` remains `{}` |
| AUTHORIZED | NO | transient scope/staleness checks |
| APPLIED | NO | transient candidate mutation |
| REVALIDATED | NO | transient normalization/validation |
| ACCEPTED | NO | no accepted cache status |
| REJECTED | NO in cache entry | rejection exists elsewhere, but cache remains replayable RAW |

The observed `recovery/llm_results.json` contains one entry with `recovery_stage=planner_patch:task`, the full 0004 raw response, `parsed_result={}`, `cache_identity={}`, and empty `candidate_fingerprint`. On the repeated example candidate, this rejected response was replayed; no second `planner_repair` provider trace exists.

This is a **CACHE / CONVERGENCE** defect, not evidence of a provider cache hit.

## 14. Retry and convergence semantics

| Identity | What it currently controls | What it does not control |
|---|---|---|
| `request_hash` | Immutable provider request trace; hashes redacted messages | Does not gate repeated Planner initial calls |
| `prompt_sha256` | Prompt-budget telemetry and byte-comparison observability | Does not gate retries or candidate repair |
| candidate/plan semantic hash | plan identity, snapshot/allocation/staleness, rejection diagnostics | Does not force failure feedback into a new initial request |
| violation fingerprints | validation convergence fingerprint and repeated rejection detection | Cannot help if violations are omitted from the provider request |
| `state_generation` | ControllerBinding freshness/CAS/staleness | Deliberately excluded from provider semantic content; changes can coexist with identical requests |
| PlanningEpoch call count | hard ceiling (default four calls per epoch) | Does not compare repeated request hash with unresolved rejection |

`_validation_convergence_fingerprint()` includes TaskSpec identity, task revision, Planner-feedback revision, issue fingerprints, and an approved/current-plan hash only under its snapshot condition. It does not make provider-visible context continuous.

Current Planner temperature is **0.7**, inherited from global model initialization because Planner passes no override. Calls 0003/0005/0006 demonstrate intentional stochastic resampling of the same full initial request: exact example, task-specific candidate, exact example. Call 0004 is the one scoped repair attempt. These are different semantics and should not be conflated.

The same known-bad provider request remains callable because the route and PlanningEpoch budget permit another initial call, while no guard combines:

```text
same request/prompt identity
+ unresolved durable candidate rejection
+ unchanged TaskSpec revision
+ unchanged failure/violation identity
```

Lowering temperature may reduce variance but cannot repair missing failure transport, namespace validation, repair authorization, or rejected-cache replay.

## 15. Runtime context audit

The current initial Planner runtime value occupies 12,551 exact request characters and 4,005 attributed offline CL100K tokens: **41.07% of characters and 41.87% of offline tokens**.

The runtime object contains 108 scalar values, including 104 strings but only 41 unique string values. Important duplication facts:

- the exact 873-character snpEff healthcheck command is serialized **six times**; seven field/key occurrences contain the word `healthcheck`;
- `RESOLVED_SNPEFF_JAR` occurs **13 times** in serialized runtime text;
- the identical 232-character resolved env/JAR/Java triplet is serialized four times;
- one 1,150-character historical `last_success_command` contains old task/action IDs and invokes `render_snpeff_annotation_scripts.py --help`, despite current capability guidance saying not to use the legacy Renderer;
- the conservative exact-scalar duplicate lower bound is 5,753 characters / 2,044 offline tokens.

| Class | Current examples | Planner relevance |
|---|---|---|
| `PLANNER_REQUIRED_RUNTIME_FACT` | selected skill, declared environment/runner, snpEff + Java availability, healthcheck status, resolved env/JAR/Java paths, runtime readiness | Required, but one canonical resolved copy is enough |
| `AUDIT_ONLY_RUNTIME_EVIDENCE` | registry timestamps, install history/policy, full package-manager environment, unrelated skill healthchecks, catalog metadata | Useful for audit/recovery; not needed to choose plan semantics |
| `LEGACY_NOISE` | previous session/action/step IDs and historical renderer help command | Not relevant to current managed-generation plan |
| `DUPLICATE` | repeated healthcheck, resolved triplet, package/capability/skill strings | Same fact repeatedly serialized |

This audit classifies only; it does not remove runtime evidence.

## 16. Two-session comparison

| Dimension | session_1787136833 | session_1787190323 | Finding |
|---|---|---|---|
| Triage request | 2,989 chars, hash-equivalent bytes, 933 tokens | same | Identical input to router |
| Consultant request | 36,463 chars, exact same bytes/hash, 9,310 tokens | same | Model saw identical request |
| Consultant response | 3,730 chars; object hash `65ce7756...` | 5,725 chars; `b0b37b61...` | Stochastic response divergence |
| Consultant `planner_feedback` | null | null | Neither run supplied Plan feedback |
| Canonical TaskSpec hash | `sha256:c8b15b57...` | `sha256:f583dbf9...` | Different semantic contracts from identical request |
| Required output contracts | output_dir + annotated_vcf + ann_summary + report + reproduce_script | same + **stage_status** | Old response mentioned stage status in prose/constraint but omitted its output contract |
| Planner request | 27,304 chars, 8,511 provider tokens | 30,558 chars, 9,724 tokens | Static 4,479-char prefix and selected capability are identical; newer handoff/TaskSpec is richer; runtime values also differ |
| Planner response | 5 task-specific steps | 0003 exact example; 0005 task-specific; 0006 exact example | Same current initial request itself is stochastic |
| `proposed_artifacts` | only `snpeff_db_dir` | 0005 database dir plus all five Task outputs as plan-local | Old candidate avoided shadowing by chance/model behavior |
| Task output refs | `output.annotated_vcf`, etc. | bare plan-local refs in 0005 | Old candidate aligned with canonical namespace |
| Validation / approval | valid and approved | post-allocation invalid; pending review; blocked | Old plan passed deterministic validation |
| Run result | later execution failure | blocked before execution; zero commands | “Plan normal” did not mean whole run success |

The older run was a lucky candidate relative to the current validator and a narrower randomly generated TaskSpec. It did not exercise or prove the missing collision gate, failure re-entry contract, global repair fallback, or cache-state distinction.

## 17. Root causes by architecture category

| Category | Finding | Severity / causal role |
|---|---|---|
| `PROMPT_CONTRACT` | Complete validator-compatible semantic candidate is embedded; no explicit rule says TaskSpec outputs may not be redeclared as plan-local | P1 contributor; directly explains exact copying and identity ambiguity |
| `CONTEXT_PROJECTION` | TaskSpec output `path_policy`, output ID, object kind, and root/materialization fields are omitted; durable Planner failures are not passed into current focus | **P0 for re-entry**, P1 for output projection |
| `STATE_REENTRY` | Repair rejection and Plan Review issues route to Planner initial without pending failure/candidate state | **P0 primary convergence break** |
| `DETERMINISTIC_VALIDATION` | Plan-local refs/roles may shadow TaskSpec identities; pre-allocation output validation counts bare-name coincidence | **P0 primary admission defect** |
| `CACHE / CONVERGENCE` | RAW repair cached before parse/authorization/revalidation and replayed after rejection; identical known-bad initial requests remain callable | **P0 secondary amplifier** |
| `OBSERVABILITY` | component map is arithmetically invalid; token estimate undercounts; runtime ref is missing; terminal Planner error collapses to generic TASK_BLOCKED | P1/P2 diagnosis and telemetry defects |

No evidence supports classifying this as a snpEff, Java, GTF, FASTA, VCF, database-build, or execution failure. `run_metrics.json` records zero commands, and the route never reached Generate.

## 18. Repair priorities for a later implementation spec

No repair is implemented in this audit. The likely change surfaces below are deliberately listed for the next spec.

### P0 — restore deterministic progress and identity safety

1. Persist unresolved Planner failure state in `current_focus` until its violation is resolved or the TaskSpec semantic revision changes. Project `planner_candidate_rejection`, Plan Review `validation_issues`, candidate identity, and the exact repair outcome through the Planner caller rather than overloading user `planner_feedback`.
   - Likely surfaces: `bio_agents.py` Planner state assembly at 23476-23520, rejection persistence at 24699-24777, Plan Review return at 25183-25215; `agent_core/node_context.py:1493-1811`.
2. Forbid a new initial provider call when the provider-visible request is identical and an unchanged unresolved rejection is known, unless an explicit stochastic-resample policy exists and carries that failure into the request.
   - Likely surfaces: PlanningEpoch/retry logic in `bio_agents.py`; ContextLineage identities remain observability, not policy ownership.
3. Route global unlocated violations that require a producer to an explicit full-plan replacement transaction. Do not use `allowed_step_ids=[]` with a step-replacement-only schema.
   - Likely surfaces: `agent_core/plan_tracker.py::build_plan_repair_request`, PlanPatch operation/schema, `apply_plan_patch`; `prompts/manager.py::get_plan_repair_v2_prompt`; Planner repair dispatch.
4. Reject plan-local declarations before allocation when `local_ref` or semantic role shadows any TaskSpec input/output stable identity, role, output ID, or handle. Make output coverage consume typed artifact identity, not bare string coincidence.
   - Likely surfaces: `agent_core/plan_contract.py::candidate_artifact_ref_keys`, deterministic candidate validation; `agent_core/plan_tracker.py::validate_output_contracts`.
5. Cache Planner repair only after parse, scope authorization, apply, and revalidation, with explicit ACCEPTED/REJECTED status and candidate/violation/TaskSpec identity. A rejected response must not be replayable as a candidate.
   - Likely surfaces: `bio_agents.py:24369-24563`; `agent_core/recovery.py::save_llm_result/load_llm_result` or a Planner-specific cache contract.

### P1 — make the model contract unambiguous

1. Project canonical output fields required by the prompt: `output_id`, `artifact_kind`, `path_policy`, `root_ref`, and materialization policy/ownership. Remove reliance on Consultant handoff duplicates.
   - Likely surface: `agent_core/node_context.py::_semantic_artifact_rows` and projection tests.
2. Establish one model-facing namespace for Task outputs, plan-local artifacts, and capability output descriptors; clearly type capability IDs as capability descriptors, not task authority.
3. Replace the full semantic `PLAN_CANDIDATE` demonstration with schema/placeholders or a deliberately non-executable shape. Preserve validator/schema synchronization without providing a complete copy target.
   - Likely surfaces: `agent_core/plan_contract.py::planner_candidate_wire_example`; `prompts/manager.py::get_planner_v2_prompt`.
4. State a complete authority hierarchy: canonical TaskSpec for task semantics, Approved Plan for downstream plan authority, user/handoff as evidence, runtime/execution as machine evidence, current focus as the current repair objective.
5. Separate stable Planner policy from dynamic invocation data and evaluate a real `SystemMessage` + `HumanMessage` split. This is role/cache architecture, not a claim that role alone fixes copying.
6. Preserve original Planner terminal error code/message and secondary cache/scope cause in blocked status and run metrics.

### P2 — efficiency and telemetry

1. Give Planner one resolved runtime fact set; keep registry/install/history/legacy command detail in audit/Recovery storage.
2. Lower Planner temperature to a value appropriate for typed planning after deterministic contracts are repaired. Do not treat this as P0.
3. Replace invalid component accounting with exact provider-section spans and retain provider token detail where available. Record the tokenizer used for offline estimates.
4. Persist a valid runtime-audit reference or stop emitting the nonexistent path.

## 19. Acceptance evidence expected from the later repair

The later implementation spec should require focused automated proofs for at least:

- a Planner rejection and Plan Review violation survive repeated Planner re-entry in `current_focus`;
- unchanged TaskSpec + unchanged rejection cannot produce an unannotated byte-identical initial request;
- global `REQUIRED_OUTPUT_UNCOVERED` selects a repair operation that can create/replace a producer;
- local violations still authorize only their exact existing steps;
- plan-local refs/semantic roles cannot collide with TaskSpec inputs/outputs or capability output descriptors;
- pre- and post-allocation required-output ownership agree;
- rejected/unauthorized/unparseable repair responses never replay as accepted cache candidates;
- TaskSpec output projection contains the fields the prompt tests;
- example-copy replay cannot pass merely because the example is validator-compatible;
- terminal metrics preserve `PLANNER_VALIDATION_NO_PROGRESS` and the `PLAN_PATCH_REJECTED` secondary cause;
- exact component character sums equal the request and provider tokens remain separately recorded.

## 20. Final audit boundary

This report stops at diagnosis and prioritized repair direction. It intentionally performs no code repair, no test mutation, no provider/workflow rerun, no snpEff execution, and no commit. A formal corrective-repair spec should be written only after review of these findings.
