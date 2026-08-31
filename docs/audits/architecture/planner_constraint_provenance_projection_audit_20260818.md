# Planner Constraint Provenance and Projection Audit

- Audit target: BioMind session **session_1787061170**
- Audit date: **2026-08-18**
- Repository snapshot: branch **codex/save-current-work**, HEAD **77d71307b3269d2e16f1e556fcca5b00e5f10452**
- Method: read-only source, state-contract, trace, test, and Git-provenance inspection; one pure in-memory TaskSpec replay; no workflow rerun.

## 1. Executive Conclusion

### Direct answers to Q1-Q5

| Question | Answer | Evidence class | Confidence |
| --- | --- | --- | --- |
| **Q1. Did the raw user submission contain the demo/test/hidden/expected-answer guard?** | **Yes.** The authoritative persisted raw text contains both “禁止使用 tests/demo_ref.fa 或 demo.vcf” and “不要使用 demo、toy、tests、fixtures_legacy_toy、expected_answers 或任何隐藏答案文件作为分析依据”. The premise that the raw session text lacked the guard is false. | FACT | HIGH |
| **Q2. If it did not, who first produced it?** | The conditional premise does not apply. At policy-origin level, the guard is **BENCHMARK_INJECTED**: the benchmark case supplies the task-specific prohibition and the prompt generator appends the generic footer. At representation level, the exact combined English constraint sentence first appears in the Consultant response. | FACT plus bounded provenance classification | HIGH |
| **Q3. Why did it enter canonical TaskSpec and become hard?** | Consultant emitted an upsert_constraint operation with invalid semantic key/category **forbidden_inputs.demo_files / forbidden_inputs**, operator **forbid**, and strength **hard**, citing U1. The Consultant V2 mutation route copied it into TaskSpec without running the existing closed-ontology validator. normalize_requirement then silently fell back to a long **required_behavior.*** key, preserved **hard**, and assigned **enforcement_scope=plan**. | FACT, confirmed by pure in-memory replay | HIGH |
| **Q4. Why did Plan Contract demand explicit step coverage?** | constraint_requires_plan_projection returned true because the canonical row was hard, had no clarification flag, and was incorrectly scoped to plan. plan_contract_violations deliberately requires an exact resolved semantic claim for such plan-scoped constraints; checklist prose is not coverage. | FACT | HIGH |
| **Q5. What is the earliest real error?** | **B. Constraint classification**, more precisely the TaskSpec mutation admission/canonicalization boundary. The malformed Consultant key is the precursor; the first accepted architectural fault is that apply_task_spec_mutation_proposal admits it and normalize_requirement silently recategorizes a global negative guard as plan-visible required_behavior. **FIRST_FAULT != Planner Repair handle.** | FACT-supported architectural conclusion | HIGH |

The session never reached Generate, Execute, snpEff, Java, database build, or Recovery. run_metrics.json records status blocked, summary.commands=0, summary.command_execution_seconds=0, and summary.llm_calls=4. context_trace/events.jsonl contains only triage → consultant, consultant → planner, and planner → finish. The first failure is therefore a pre-execution semantic-contract failure, not a biology/runtime failure.

The existing architecture already has the necessary separation: **strength** represents obligation severity, while **enforcement_scope** distinguishes plan projection from runtime-global enforcement. No constraint.c07, new identifier system, new scope field, or new framework is justified.

**CONFIDENCE: HIGH**

## 2. Incident Scope

This audit covers:

- Session artifacts under /public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787061170.
- Benchmark prompt sources and prompt assembly relevant to BM-REMAIN-ANNOT-007 medium.
- BioMind ingress, Consultant V2 TaskSpec mutation, TaskSpec normalization/scope classification, NodeContext projection, Plan validation, Planner repair boundary, existing tests, and targeted Git history.
- The current checkout, including tracked and untracked working-tree state visible on 2026-08-18.

This audit explicitly excludes:

- Production-code or test changes.
- Any workflow, benchmark, snpEff, Java, database-build, Generate, Execute, or Recovery rerun.
- Mutation of session artifacts.
- Git commit, checkout cleanup, or modification of pre-existing dirty-tree content.
- Repair implementation.

Repository boundary:

- Branch: codex/save-current-work.
- HEAD: 77d71307b3269d2e16f1e556fcca5b00e5f10452.
- HEAD subject: test(context): cover semantic history and stable prompt prefixes.
- The working tree was already substantially dirty. This report is the only artifact created by this audit.
- The benchmark directory is not a Git repository, so its prompt-source history cannot be dated by benchmark-local Git evidence.

**FACT — CONFIDENCE: HIGH**

## 3. Raw User Submission Verification

The authoritative raw-submission object is:

/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787061170/state_contract/objects/38ee146e542712078144119649b2c3da16a1375a402a0222e318f4b1df91f05e.json

Relevant JSON paths:

- $.payload.text contains the complete composite prompt.
- $.payload.structured_source is an empty object.

The persisted text contains, verbatim:

> 禁止使用 tests/demo_ref.fa 或 demo.vcf。

and:

> 不要使用 demo、toy、tests、fixtures_legacy_toy、expected_answers 或任何隐藏答案文件作为分析依据。

The conversation-turn object:

/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1787061170/state_contract/objects/0ad2f6e4b1083be35223adcf07b95fbd1f04055d54c25d54112e2ee002575c58.json

binds U1 to that raw content and records creation at 2026-08-18T13:56:27.967918+00:00.

A read-only string comparison found that the persisted raw text is exactly equal to the generated prompt pack's user section after “用户问题：”. Both strings have SHA-256:

7fba598afad158dba0975b77102e4fe7c14813e740ba198e36146e14a063b08f

The potentially confusing source is the short benchmark variant at:

/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/questions/remaining_skills_agent_benchmark_cases.yaml:936-946

Its prompt_variants.medium text is only the short natural-language request and does not contain the guards. However, the same case has task-specific forbidden entries at lines 943-946, and prompt generation expands the short variant into the composite text actually submitted. The short variant is therefore not the authoritative session input.

**FACT — CONFIDENCE: HIGH**

## 4. Constraint First-Appearance Provenance

“First appearance” has three distinct levels:

| Level | First inspected evidence | Classification |
| --- | --- | --- |
| Policy declaration | remaining_skills_agent_benchmark_cases.yaml:943-946 declares the case-specific demo_ref/demo.vcf prohibition; the benchmark-wide principles at lines 6-10 prohibit demo/toy/tests/expected-answer data. | BENCHMARK_INJECTED policy source |
| Concrete submitted Chinese text | generate_biomind_prompt_pack.py::build_medium_prompt adds case forbidden rules, and common_footer adds the generic prohibition; biomind_ready_to_paste_prompts.yaml:6066-6160 contains the resulting composite prompt. | BENCHMARK_INJECTED prompt assembly |
| Exact canonical-candidate English sentence | consultant-0002-83705a546d34/response.json task_spec_operations contains “Do not use tests/demo_ref.fa, demo.vcf, or any demo/toy/test/hidden/expected answer files as analysis input.” | CONSULTANT_DERIVED representation |

The benchmark generator evidence is:

- scripts/generate/generate_biomind_prompt_pack.py::common_footer, lines 59-69, supplies the generic guard.
- scripts/generate/generate_biomind_prompt_pack.py::build_medium_prompt, lines 142-179, supplies case forbidden entries at lines 167-168 and appends the footer at line 179.
- questions/biomind_ready_to_paste_prompts.yaml:6130 contains the task-specific guard.
- questions/biomind_ready_to_paste_prompts.yaml:6159 contains the generic guard.

BioMind later labels the constraint user_explicit because the entire expanded prompt arrived as U1 and $.payload.structured_source was empty. This describes the immediate in-system evidence source, not the policy's upstream authoring origin.

- **FACT:** the policy originated in benchmark prompt material.
- **FACT:** BioMind's persisted source_type is user_explicit.
- **INFERENCE:** this is provenance flattening at the ingress boundary.
- **UNRESOLVED:** which exact launcher path submitted this historical session, because the current typed benchmark runner would populate structured_source but this persisted session did not.
- **CONFIDENCE:** HIGH for the first three statements; MEDIUM for any launcher-history inference.

## 5. End-to-End Provenance Timeline

| Order | Stage and evidence | Constraint representation | Transition result |
| --- | --- | --- | --- |
| 1 | Benchmark case YAML | Separate case-specific forbidden inputs plus benchmark-wide anti-demo policy | Policy exists before BioMind |
| 2 | build_medium_prompt + common_footer | Composite Chinese prompt containing both guard forms | Benchmark policy becomes submitted prompt text |
| 3 | Raw object 38ee… and U1 object 0ad2… | Same composite text; structured_source={} | BioMind can only see one user-message envelope |
| 4 | task_router request/response | Full U1 is visible; router returns route classification only | No semantic constraint is created here |
| 5 | Consultant request | conversation_evidence contains full U1; current TaskSpec is empty | Consultant receives the guard |
| 6 | Consultant response | upsert_constraint with forbidden_inputs.demo_files, category forbidden_inputs, forbid, hard, evidence U1 | Invalid closed-ontology row is proposed |
| 7 | Consultant V2 mutation admission | Nested constraint is copied and source_type=user_explicit is attached | Invalid semantic key is not rejected |
| 8 | TaskSpec normalization | Long required_behavior key, forbid, hard, enforcement_scope=plan | Global negative guard becomes plan-visible |
| 9 | Planner initial request | Accepted handoff contains hard constraint; canonical TaskSpec row exposes enforcement_scope=plan and a truncated handle | Planner is instructed to project it |
| 10 | Planner initial response | No step semantic_claims; checklist includes only prose anti-demo check | Exact claim coverage remains absent |
| 11 | Plan validation | SEMANTIC_CONSTRAINT_UNCOVERED | Initial candidate is rejected |
| 12 | Planner repair | Repair returns a full semantic-key-like value rather than the exact exposed handle | Unknown reference plus original uncovered condition persist |
| 13 | Controller route | planner → finish, task_status=blocked at 2026-08-18T13:57:07.080275+00:00 | No Generate, Execute, or Recovery |

Controller route timestamps:

- triage → consultant: 2026-08-18T13:56:31.365795+00:00.
- consultant → planner: 2026-08-18T13:56:47.044628+00:00.
- planner → finish: 2026-08-18T13:57:07.080275+00:00.

**FACT — CONFIDENCE: HIGH**

## 6. Canonical TaskSpec Formation Path

The actual formation chain is:

1. bio_agents.py::BioAnalystAgent._persist_user_submission writes raw_user_submission text and structured_source and creates U1.
2. prompts/manager.py::PromptManager.get_consultant_v2_prompt instructs the Consultant to emit task_spec_operations. Its example shows failure_policy and hard, but does not enumerate the closed semantic-key prefixes or enforcement_scope.
3. The Consultant emits:

   - operation: upsert_constraint
   - semantic_key: forbidden_inputs.demo_files
   - category: forbidden_inputs
   - operator: forbid
   - strength: hard
   - supporting_evidence: U1

4. bio_agents.py::_consultant_task_spec_mutation_proposal resolves U1, copies the nested constraint, and adds immediate provenance fields. It does not validate the nested semantic key/category/operator/strength.
5. agent_core/consultation_state.py::build_consultant_candidate calls agent_core/task_spec.py::apply_task_spec_mutation_proposal.
6. apply_task_spec_mutation_proposal validates operation shape but its upsert_constraint branch sends the nested row directly to merge_compiled_constraints.
7. merge_compiled_constraints documents its input as already-validated Consultant IR, but this route has not performed that validation.
8. normalize_requirement cannot resolve forbidden_inputs.demo_files through the closed ontology. Its fallback constructs:

   required_behavior.do_not_use_tests_demo_ref_fa_demo_vcf_or_any_demo_toy_test_hidden_expected_answer_files_as_analysis_input

9. normalize_requirement preserves operator=forbid and strength=hard. semantic_constraint_enforcement_scope sees required_behavior rather than forbidden_behavior and assigns enforcement_scope=plan.

The persisted TaskSpec row in plan_state.json confirms every output field: the long required_behavior semantic key, operator forbid, strength hard, enforcement_scope plan, source/source_type user_explicit, and U1 provenance.

The existing validate_compiled_constraints function rejects the incident's original key with “unknown or missing ontology key”. A pure in-memory replay confirmed:

- Incident row → required_behavior.*, hard, plan, constraint_requires_plan_projection=true.
- Changing only semantic_key to valid forbidden_behavior.demo_files → enforcement_scope=runtime_global and constraint_requires_plan_projection=false.

This proves a validator exists and the defect is route-specific admission/canonicalization bypass, not missing architecture.

**FACT — CONFIDENCE: HIGH**

## 7. Hard-Constraint Classification Path

The hard strength was not invented by Plan Contract or Planner Repair:

1. Consultant explicitly emitted strength=hard.
2. _consultant_task_spec_mutation_proposal copied it.
3. merge_compiled_constraints and normalize_requirement retained it.
4. The persisted canonical row remains hard.

The semantic defect is not that a prohibition can never be hard. A global negative guard may correctly be hard while still not requiring a plan-step claim. The defect is the combination created by fallback:

| Dimension | Incident value | Correct architectural meaning |
| --- | --- | --- |
| Strength | hard | Mandatory; plausible and not itself the root defect |
| Operator | forbid | Negative guard; preserved correctly |
| Semantic category | required_behavior | Incorrect fallback from invalid forbidden_inputs |
| Enforcement scope | plan | Incorrect consequence of the fallback category |
| Provenance | user_explicit | Immediate U1 source, but upstream benchmark origin is lost |

Strength and projection scope are already independent fields. “hard” does not semantically imply “must appear in a Plan step”; hard plus enforcement_scope=plan does.

**FACT — CONFIDENCE: HIGH**

## 8. Plan Coverage Validation Path

The actual owner of SEMANTIC_CONSTRAINT_UNCOVERED is agent_core/plan_tracker.py, not agent_core/plan_contract.py.

The validation chain is:

1. normalize_plan_contracts invokes resolve_plan_semantic_claims.
2. resolve_plan_semantic_claims resolves exact exposed constraint identifiers and emits SEMANTIC_CONSTRAINT_REFERENCE_INVALID for unknown references.
3. normalize_plan_contracts invokes plan_contract_violations.
4. plan_contract_violations iterates semantic constraints from TaskSpec.
5. It skips any constraint for which constraint_requires_plan_projection is false.
6. For remaining plan-scoped hard constraints, it requires exact resolved semantic-key/value coverage or typed input/output/skill coverage.
7. If no coverage exists, it emits SEMANTIC_CONSTRAINT_UNCOVERED.

The initial Planner candidate had no semantic_claims on any step. Its validation_checklist contained the prose “Ensure no prohibited demo/toy/test files are used as analysis input”, but checklist prose is intentionally not an exact semantic claim and therefore cannot satisfy typed coverage.

The validator is not indiscriminately enforcing all hard constraints. It is enforcing all hard constraints already classified as plan-scoped. That policy is consistent with the current tests and the Planner contract text. The incident row reached it with the wrong scope.

**FACT — CONFIDENCE: HIGH**

## 9. Global Guard vs Plan-Step Constraint Analysis

The prohibited-demo rule is best modeled as a **global negative guard**, not a plan-step semantic requirement:

| Property | Plan semantic requirement | Global negative guard | Incident rule |
| --- | --- | --- | --- |
| Needs an affirmative step that realizes it | Yes | No | No |
| Must remain mandatory | Usually | Yes | Yes |
| Natural category | required_behavior / required_input / required_output | forbidden_behavior | forbidden_behavior |
| Natural enforcement_scope | plan | runtime_global | runtime_global |
| Exact Plan semantic claim required | Yes | No | No |
| Enforced elsewhere | Plan validation and downstream execution | Runtime/controller policy gates | Should be runtime/controller policy |

Existing task_spec.py behavior supports this distinction:

- semantic_constraint_enforcement_scope maps forbidden_behavior to runtime_global.
- constraint_requires_plan_projection returns false for runtime-global constraints.
- format_planner_task_contract_context separates plan constraints from global_policies and explicitly says global policies have no claim IDs.
- tests already assert that an anonymous hard prohibition can remain runtime-global without producing SEMANTIC_CONSTRAINT_UNCOVERED.

Therefore no new constraint.c07, no new scope field, no new “global guard” ID family, and no parallel contract framework is needed. Existing enforcement_scope is sufficient.

**ARCHITECTURAL CONCLUSION — CONFIDENCE: HIGH**

## 10. Planner Repair Boundary

The nested planner function in bio_agents.py::_build_graph:

- receives the already-formed canonical TaskSpec;
- calls normalize_plan_contracts for the initial candidate;
- receives SEMANTIC_CONSTRAINT_UNCOVERED;
- constructs the repair request and invokes the repair LLM;
- validates the patch and decides rollback/blocking.

The initial uncovered violation exists before repair starts. Planner Repair consumes the violation; it does not create the invalid semantic category or plan scope.

The repair output then introduces a secondary identity mismatch by using a full semantic-key-like string rather than the exact NodeContext handle. That secondary failure worsens convergence, but it cannot explain the original SEMANTIC_CONSTRAINT_UNCOVERED.

**Planner Repair: NO CHANGE for the P0 classification fix.**

**FACT — CONFIDENCE: HIGH**

## 11. Recovery Boundary

events.jsonl proves that the graph did not route through a Recovery node. consultation_state.finalize_protocol is null and the consultation phase is ready_to_plan; the versioned FINALIZE recovery path was not used.

The Planner implementation may use RecoveryStateStore as an internal cache/state utility, but that does not turn this session into a Recovery-route incident. No Recovery decision produced the malformed semantic key, the fallback category, the plan scope, or the first validation failure.

- **Recovery: NO CHANGE.**
- **Recovery prompt/context: NO CHANGE.**
- **Recovery budgets/cache identity: NO CHANGE.**

**FACT — CONFIDENCE: HIGH**

## 12. Secondary Handle Contract Finding

agent_core/node_context.py::_slug truncates semantic handle slugs to 80 characters. _unique_handle and _constraint_rows expose the resulting handle and retain a manifest mapping to the stable constraint identity and full semantic key.

For this row, the exposed handle is:

constraint.required_behavior.do_not_use_tests_demo_ref_fa_demo_vcf_or_any_demo_toy_test_hid

The manifest maps it to:

required_behavior.do_not_use_tests_demo_ref_fa_demo_vcf_or_any_demo_toy_test_hidden_expected_answer_files_as_analysis_input

During repair, the model returned a full semantic-key-like value instead of the exact exposed handle. resolve_plan_proposal_handles resolves exact known handles and leaves unknown strings unchanged, enabling a SEMANTIC_CONSTRAINT_REFERENCE_INVALID condition while the original uncovered condition remains.

This is a real secondary contract/convergence finding. It is not P0 for this incident because correct upstream classification as forbidden_behavior/runtime_global makes the Plan-step claim requirement unreachable for this rule.

**SECONDARY FINDING — CONFIDENCE: HIGH**

**Handle-length or resolver repair: NO CHANGE in the next P0 fix.**

## 13. Earliest Incorrect Transition

### Observed Chain

1. Benchmark case prohibition + common footer.
2. Composite prompt submitted as U1 with structured_source={}.
3. Consultant derives forbidden_inputs.demo_files, forbid, hard.
4. V2 mutation admission accepts an invalid ontology key.
5. normalize_requirement silently falls back to long required_behavior.*.
6. semantic_constraint_enforcement_scope assigns plan.
7. Planner candidate has no exact step semantic claim.
8. SEMANTIC_CONSTRAINT_UNCOVERED.
9. Planner repair uses a non-matching full identifier.
10. Reference-invalid/uncovered repair conflict.
11. planner → finish(blocked).

### Earliest Semantically Incorrect Transition

The malformed Consultant semantic key is the immediate precursor. The earliest accepted architectural fault is:

1. apply_task_spec_mutation_proposal(upsert_constraint).
2. It assumes nested IR is already validated.
3. It calls merge_compiled_constraints.
4. normalize_requirement silently changes a negative guard into required_behavior.
5. enforcement_scope becomes plan.

Selection: **B. constraint classification / TaskSpec mutation admission-canonicalization**.

Why not the alternatives:

- **A. provenance:** provenance flattening is real, but plan projection does not branch on source_type. Changing benchmark-injected to user_explicit does not itself cause coverage.
- **C. Plan coverage policy:** it correctly filters through constraint_requires_plan_projection and is intentionally scope-aware.
- **D. Planner Repair handle:** it occurs only after the first candidate has already failed.
- **E. other:** no executor/runtime/recovery evidence exists.

**FIRST_FAULT != planner repair handle.**

**ARCHITECTURAL CONCLUSION — CONFIDENCE: HIGH**

## 14. Existing Functions and Responsibilities

| Existing symbol | Current responsibility | Incident role |
| --- | --- | --- |
| BioAnalystAgent._persist_user_submission | Persist raw text/structured source and create U1 evidence | Preserved composite prompt; metadata was empty |
| benchmark_source_context | Build typed benchmark_question / benchmark_contract source metadata | Current runner capability; not reflected in this historical session |
| PromptManager.get_consultant_v2_prompt | Define Consultant V2 operation wire contract | Does not expose the closed constraint ontology |
| _consultant_task_spec_mutation_proposal | Translate Consultant operations and bind evidence/provenance | Copies nested constraint without semantic validation |
| build_consultant_candidate | Apply Consultant proposal to candidate canonical state | Calls TaskSpec mutation owner |
| apply_task_spec_mutation_proposal | Validate/apply TaskSpec operations | Validates shape, not nested upsert_constraint ontology |
| validate_compiled_constraints | Strictly validate semantic constraint IR | Would reject forbidden_inputs.demo_files but is bypassed here |
| merge_compiled_constraints | Merge already-validated Consultant IR | Receives unvalidated IR on this route |
| normalize_requirement | Canonicalize requirement rows and apply fallback | Silently produces required_behavior long slug |
| semantic_constraint_enforcement_scope | Derive plan vs runtime-global scope | Correctly follows the category it receives |
| constraint_requires_plan_projection | Gate exact Plan projection | Correctly returns true for hard+plan incident row |
| format_planner_task_contract_context | Split plan constraints from global policies | Existing correct scope-aware presentation |
| agent_core/node_context.py::_constraint_rows | Expose constraint handles and manifest identities | Represents the already-wrong canonical row |
| resolve_plan_proposal_handles | Resolve exposed handles back to stable identities | Leaves unknown repair values unresolved |
| normalize_plan_contracts | Normalize/validate Planner candidate contracts | Invokes claim resolution and violation checks |
| plan_contract_violations | Enforce coverage for plan-scoped constraints | Emits the first uncovered violation |
| planner_candidate_rejection_is_internal_conflict | Classify downstream rejection-code conflicts | Downstream only |

## 15. Modification Boundary Matrix

| File | Existing Symbol | Current Responsibility | Evidence of involvement | Future change needed? | Exact responsibility of possible change | Do NOT change |
| ---- | --------------- | ---------------------- | ----------------------- | --------------------- | --------------------------------------- | ------------- |
| agent_core/task_spec.py | apply_task_spec_mutation_proposal | Validate operation shape and apply TaskSpec mutations | upsert_constraint forwards invalid nested IR to merge | **YES, primary** | Validate/canonicalize the nested constraint with the existing closed ontology before merge; reject invalid IR through the existing Consultant retry boundary | Do not add a new field, ID system, or parallel constraint type |
| prompts/manager.py | PromptManager.get_consultant_v2_prompt | Specify Consultant V2 task_spec_operations | Current examples do not enumerate allowed semantic-key prefixes or scope semantics | **YES, supporting** | State the existing ontology contract and use forbidden_behavior.* for negative guards | Do not make prompt wording the only enforcement |
| bio_agents.py | _consultant_task_spec_mutation_proposal | Resolve evidence handles and translate operations | Copies forbidden_inputs.demo_files unchanged | **CONDITIONAL** | Only add wire/default shaping if required by the strict existing validator; keep semantic interpretation in task_spec.py | Do not infer policy from free prose here |
| agent_core/task_spec.py | validate_compiled_constraints | Enforce closed semantic constraint ontology | Pure replay shows it rejects the incident key | **NO direct change; reuse** | Reuse from the mutation admission path or reuse its checks | Do not fork a second validator |
| agent_core/task_spec.py | merge_compiled_constraints | Merge already-validated Consultant IR | Its precondition was violated by caller | **NO direct change** | Preserve its validated-IR contract | Do not turn merge into a second policy owner unless call-site proof changes |
| agent_core/task_spec.py | normalize_requirement | Normalize canonical rows and legacy fallbacks | Converts invalid typed row to required_behavior.* | **CONDITIONAL, Option B only** | Compatibility-canonicalize typed forbid rows before the generic fallback | Do not change for Option A |
| agent_core/task_spec.py | SEMANTIC_CONSTRAINT_CATEGORY_ALIASES / canonicalize_compiled_constraint | Canonicalize known aliases | Existing aliases demonstrate a compatibility mechanism | **CONDITIONAL, Option B only** | Add forbidden_inputs → forbidden_behavior only if compatibility is deliberately selected | Do not create constraint.c07 |
| agent_core/task_spec.py | semantic_constraint_enforcement_scope | Assign plan/runtime-global scope from canonical semantics | Produces plan only because input category was wrongly required_behavior | **NO** | None | Do not special-case demo filenames |
| agent_core/task_spec.py | constraint_requires_plan_projection | Decide whether exact Plan coverage is required | Correctly evaluates hard+plan as true | **NO** | None | Do not weaken plan-scoped hard coverage |
| agent_core/task_spec.py | format_planner_task_contract_context | Present plan constraints and global policies separately | Already represents runtime-global policy without claim IDs | **NO** | None | Do not add another projection framework |
| bio_agents.py | _accepted_consultant_handoff_projection | Project accepted handoff hard constraints | Scope-less duplicate can obscure the canonical scoped row | **CONDITIONAL, non-P0** | If separately specified, include/dedupe scope for clarity | Do not use this as the incident fix |
| agent_core/node_context.py | _slug / _unique_handle | Generate bounded LLM-visible handles | 80-character truncation contributes only during repair | **NO for P0** | None in classification repair | Do not fix handle length opportunistically |
| agent_core/node_context.py | _constraint_rows / build_node_context | Project canonical constraints and violations | Faithfully projects the already-misclassified row | **NO** | None | Do not move provenance/classification ownership here |
| agent_core/node_context.py | resolve_plan_proposal_handles | Resolve exact semantic handles | Unknown full repair value remains unresolved | **NO for P0** | Separate future convergence audit only | Do not add prefix-stripping heuristics |
| agent_core/plan_tracker.py | resolve_plan_semantic_claims | Resolve exact claims and report invalid references | Detects the secondary handle mismatch | **NO** | None | Do not accept unknown aliases silently |
| agent_core/plan_tracker.py | normalize_plan_contracts | Run contract normalization and validation | Calls the correct validators | **NO** | None | Do not bypass validation |
| agent_core/plan_tracker.py | plan_contract_violations | Require coverage only for plan-scoped constraints | Emits SEMANTIC_CONSTRAINT_UNCOVERED after upstream misclassification | **NO** | None | Do not weaken scope-aware coverage |
| bio_agents.py | _build_graph().planner | Initial Plan validation and repair routing | Consumes the first violation | **NO** | None | Do not patch Planner Repair for this P0 |
| agent_core/plan_contract.py | planner_candidate_rejection_is_internal_conflict | Classify downstream rejection conflicts | Appears after initial rejection | **NO** | None | Do not move uncovered generation here |
| agent_core/consultant_finalize.py | versioned FINALIZE path | Validate/finalize versioned Consultant protocol | consultation_state.finalize_protocol=null; route unused | **NO** | None | Do not alter an unexecuted path |
| benchmark runner | benchmark_source_context / run_prompt | Attach typed benchmark provenance and invoke agent.go | Current source uses structured_source; session persisted {} | **NO on current evidence** | Use the existing typed path for future runs | Do not claim it launched this historical session |
| benchmark generator | common_footer / build_medium_prompt | Assemble benchmark policy into prompt | Origin of the guard | **NO for BioMind P0** | Change only if benchmark policy itself is product-rejected | Do not erase the guard to mask classification |

No new function is justified. Existing validation, canonicalization, source-type, strength, and enforcement_scope mechanisms cover the required responsibilities.

## 16. Explicit No-Change Areas

For the next narrowly scoped P0 repair:

- Planner initial validation: **NO CHANGE**.
- Planner Repair prompt, routing, patch application, and budget: **NO CHANGE**.
- Recovery state machine, prompts, disclosure, cache, budgets, and routing: **NO CHANGE**.
- Generate contracts and prompts: **NO CHANGE**.
- Execute/runtime closure: **NO CHANGE**.
- managed-artifact binding and output identity: **NO CHANGE**.
- NodeContext handle length and handle resolver: **NO CHANGE**.
- plan_contract.py rejection-conflict logic: **NO CHANGE**.
- semantic_constraint_enforcement_scope: **NO CHANGE**.
- constraint_requires_plan_projection: **NO CHANGE**.
- plan_tracker.py coverage policy: **NO CHANGE**.
- benchmark data files and generated run artifacts: **NO CHANGE**.
- constraint.c07 or any new constraint identifier: **DO NOT IMPLEMENT**.

These exclusions are causal, not merely scope preferences: none owns the earliest incorrect transition.

## 17. Existing Regression-Test Inventory

Tests were inspected statically; none was executed or modified.

| Test file and test | Current assertion | Design implication |
| --- | --- | --- |
| tests/test_plan_contract_convergence_v2.py::test_29_unknown_constraint_reference_is_explicit | Unknown constraint references produce an explicit validation error | Exact handle resolution is intentional |
| tests/test_plan_contract_convergence_v2.py::test_30_valid_constraint_reference_resolves_once | A valid handle resolves deterministically once | Stable handle mapping is intentional |
| tests/test_plan_contract_convergence_v2.py::test_30a_anonymous_hard_directive_is_runtime_global_not_plan_binding | A hard anonymous prohibition is runtime-global and does not produce uncovered Plan coverage | Hard is intentionally not equivalent to plan-visible |
| tests/test_plan_contract_convergence_v2.py::test_30b_planner_context_projects_global_policy_without_claim_id | Global policies are shown without a constraint claim ID | Existing projection model already supports this incident's intended semantics |
| tests/test_semantic_constraint_protocol.py::test_invalid_consultant_payload_requires_structured_retry | Invalid old-style compiled constraint payload triggers structured retry | Strict semantic validation exists, but this test does not cover V2 operations |
| tests/test_semantic_constraint_protocol.py::test_consultant_json_payload_preserves_semantic_ir | Old semantic_constraints payload retains typed IR | Does not exercise task_spec_operations.upsert_constraint |
| tests/test_semantic_constraint_protocol.py::test_semantic_aliases_are_canonicalized_with_audit | Known aliases, including forbidden.demo_data, canonicalize to forbidden_behavior.demo_or_toy_input with audit | Compatibility alias machinery already exists |
| tests/test_consultant_first_canonical_state.py::test_08_candidate_finish_applies_skill_and_mutation_before_completion | Consultant V2 mutation is applied before completion | Exercises mutation application but not invalid nested constraint ontology |
| tests/test_consultant_first_graph_integration_v21.py task_spec_operations and snpEff scenarios | Graph applies Consultant-first operations in integration paths | No guard-classification regression matching this incident |

The current behavior that plan-scoped hard constraints require coverage is intentional. The regression gap is upstream:

1. No test submits an invalid nested task_spec_operations.upsert_constraint key and proves it is rejected or intentionally canonicalized before merge.
2. No exact incident replay proves that the benchmark demo/test prohibition becomes forbidden_behavior/runtime_global and does not generate SEMANTIC_CONSTRAINT_UNCOVERED.
3. No test explicitly preserves both provenance layers: benchmark policy origin and immediate U1 evidence.

These are future test requirements only; this audit does not edit tests.

## 18. Git Provenance

| Commit | Date and subject | Relevant design provenance |
| --- | --- | --- |
| cf4cab4011537728e244e3f15f5b4e2c6be9ce26 | 2026-08-08 — fix(constraints): compile user intent into typed semantic contracts | Introduced the typed ontology/fallback family and SEMANTIC_CONSTRAINT_UNCOVERED. The original coverage behavior was broader. |
| 21bfffeed64eef9b3a723736576be512ad9bf8ca | 2026-08-12 — fix(state): enforce consultant-first canonical lifecycle | Added enforcement_scope and constraint_requires_plan_projection, the Consultant V2 mutation translator, and NodeContext slug behavior. Its comments/tests establish that anonymous/global prose must not automatically become Plan bindings. |
| 99f66a9b063e58688aae0bf1805c5669bcd80c92 | 2026-08-12 — fix(agent): unify output contracts and retire renderer execution path | Carries the current plan-contract convergence test-file provenance; targeted blame attributes the scope-aware assertions to the lifecycle work. |

Targeted blame/log evidence:

- normalize_requirement's generic fallback originates in cf4cab4.
- semantic_constraint_enforcement_scope and the plan-projection filter originate in 21bff.
- plan_contract_violations' uncovered emission originates in cf4cab4; 21bff later added the scope filter.
- node_context.py::_slug truncation originates in 21bff.

The historical direction is therefore not “all hard constraints must always be in a step.” The newer lifecycle commit explicitly narrowed that rule using enforcement_scope. This incident bypassed the intended classification boundary and fell back into the plan bucket.

No evidence ties the scope-aware coverage rule specifically to a legacy Hi-C or snpEff exception. No benchmark-local Git history is available, so the authoring date and commit rationale for the benchmark guards remain unresolved.

**FACT for commit ownership — CONFIDENCE: HIGH**

**UNRESOLVED for benchmark authoring history — CONFIDENCE: LOW**

## 19. Minimal Future Fix Options

### Option A

**Strict admission at the existing mutation boundary — recommended.**

| Required question | Answer |
| --- | --- |
| Which existing function changes? | Primarily agent_core/task_spec.py::apply_task_spec_mutation_proposal, specifically its upsert_constraint branch; supportively prompts/manager.py::PromptManager.get_consultant_v2_prompt. Reuse validate_compiled_constraints or its existing closed-ontology checks. |
| New field? | No. Reuse semantic_key, operator, strength, provenance, and enforcement_scope. |
| New function? | No. |
| Scope of change? | Narrow: validate/canonicalize nested Consultant IR before merge; reject invalid typed keys through the existing Consultant retry/error boundary. Update the V2 prompt to enumerate existing valid prefixes and show forbidden_behavior.*. |
| Planner Repair impact? | None. Correct classification prevents this incident's repair path. |
| Recovery impact? | None. |
| Other managed-task impact? | Yes, intentionally: all Consultant V2 upsert_constraint operations receive consistent strict admission. Invalid typed IR may retry/reject instead of being silently downgraded. |

Risk: stricter admission may expose other prompts that currently rely on invalid ad hoc categories. That is observable and preferable to silent semantic recategorization, but must be covered by targeted tests before implementation.

### Option B

**Compatibility canonicalization in the existing normalizer/alias machinery.**

| Required question | Answer |
| --- | --- |
| Which existing function changes? | agent_core/task_spec.py::normalize_requirement and/or the existing SEMANTIC_CONSTRAINT_CATEGORY_ALIASES / canonicalize_compiled_constraint path. |
| New field? | No. |
| New function? | No. |
| Scope of change? | Narrow code surface but broader behavior: map typed forbid rows or forbidden_inputs to forbidden_behavior before the generic required_behavior fallback. |
| Planner Repair impact? | None for this incident. |
| Recovery impact? | None. |
| Other managed-task impact? | Yes: previously invalid categories may be silently accepted and canonicalized across managed tasks. |

Risk: this preserves compatibility but expands silent normalization and may conceal future Consultant schema drift. It must not become a filename-specific demo special case.

## 20. Recommended Next-Step Scope

Prepare a separate implementation specification selecting **Option A** and limiting the future patch to:

1. Strict nested upsert_constraint admission in apply_task_spec_mutation_proposal by reusing existing ontology validation.
2. Consultant V2 prompt alignment with the same existing ontology.
3. Regression tests for invalid V2 nested keys, the exact benchmark guard classification, runtime-global non-projection, and preservation of typed provenance when structured_source is supplied.

The implementation specification should explicitly retain:

- current Plan coverage policy;
- current enforcement_scope semantics;
- current NodeContext identity model;
- current Planner Repair and Recovery behavior;
- current Generate/runtime/artifact contracts.

No implementation should begin from this audit alone.

**RECOMMENDATION — CONFIDENCE: HIGH**

## 21. Open Questions

1. **Exact historical launcher path — UNRESOLVED.** Current scripts/run/run_biomind_prompt.py::benchmark_source_context constructs benchmark_question and benchmark_contract metadata, and run_prompt calls agent.go with structured_source. This session persisted structured_source={}. The exact older/manual/alternate launch path cannot be proven from inspected artifacts.
2. **Benchmark source history — UNRESOLVED.** The benchmark directory has no Git metadata, so the original author/date/rationale of the guard cannot be reconstructed through Git.
3. **Desired compatibility policy — UNRESOLVED product decision.** Should any legacy forbidden_inputs category be accepted as an alias, or should invalid V2 typed IR always retry? Evidence favors strict Option A, but compatibility acceptance is a deliberate product choice.
4. **Broader handle repair — OUTSIDE THIS INCIDENT P0.** The exact-handle/full-key mismatch is real and may affect other plan-scoped long constraints. It requires a separate incident inventory before any resolver change.
5. **Provenance preservation for pasted generated prompts — UNRESOLVED.** If a human pastes a fully generated benchmark prompt rather than using the typed runner, upstream benchmark origin cannot be recovered from U1 alone without new trusted envelope evidence. This does not block the classification fix.

## 22. Confidence / Evidence Classification

| Statement | Classification | Confidence | Basis |
| --- | --- | --- | --- |
| Raw session U1 contains both anti-demo guard forms | FACT | HIGH | State-contract raw object and exact text/hash comparison |
| The short benchmark medium variant alone lacks the guards | FACT | HIGH | Targeted YAML inspection |
| Prompt generation injects case and common-footer guards before BioMind | FACT | HIGH | Generator functions and generated prompt pack |
| Policy origin is benchmark-injected while immediate BioMind source is U1/user_explicit | FACT plus provenance interpretation | HIGH | Source files, raw envelope, persisted TaskSpec |
| Exact historical launcher is known | UNRESOLVED | LOW | Persisted empty metadata conflicts with current typed runner behavior |
| Consultant first creates the exact combined English sentence | FACT within inspected chain | HIGH | Consultant raw response |
| forbidden_inputs.demo_files is outside the closed ontology | FACT | HIGH | task_spec.py ontology and strict-validator replay |
| V2 mutation route bypasses nested semantic validation | FACT | HIGH | Call-chain inspection and pure replay |
| Silent fallback causes required_behavior + plan scope | FACT | HIGH | Persisted TaskSpec and pure replay |
| Plan validator is scope-aware rather than all-hard indiscriminate | FACT | HIGH | plan_tracker.py and regression tests |
| Checklist prose does not satisfy exact semantic claims | FACT | HIGH | Initial Planner response and validator logic |
| Earliest accepted architectural fault is classification/admission | ARCHITECTURAL CONCLUSION | HIGH | Full causal chain and counterfactual replay |
| Provenance flattening caused the uncovered violation | REJECTED INFERENCE | HIGH | Coverage path does not consume source_type |
| Planner Repair handle is the first fault | REJECTED INFERENCE | HIGH | Initial violation predates repair |
| Handle mismatch is a real secondary issue | FACT | HIGH | Manifest map, repair payload, resolver behavior |
| Recovery/Generate/runtime/artifact code contributed | REJECTED INFERENCE | HIGH | Route trace stops at Planner; zero commands |
| Existing fields are sufficient | ARCHITECTURAL CONCLUSION | HIGH | strength + enforcement_scope + global_policies already implemented/tested |
| Option A is lower-risk than silent compatibility normalization | RECOMMENDATION | MEDIUM-HIGH | Narrow owner alignment; compatibility blast radius requires future tests |

Final causal statement:

> A benchmark-origin global negative guard was present in the authoritative raw submission. Consultant represented it with an invalid semantic category; the Consultant V2 TaskSpec mutation boundary accepted that invalid typed IR, and generic normalization silently reclassified it as a plan-scoped required behavior. The scope-aware Plan validator then correctly rejected a plan with no exact claim. Planner Repair's handle mismatch was downstream noise, not the first fault.
