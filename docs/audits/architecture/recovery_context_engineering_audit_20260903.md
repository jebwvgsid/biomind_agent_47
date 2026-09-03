# BioMind Recovery Context Engineering Baseline Audit

Status: **READ-ONLY ARCHITECTURE AUDIT + OFFLINE INSPECTION ONLY**

- Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`
- Branch at preflight: `refactor/consultant-controller-authority`
- Audited HEAD: `9549734ba72ce783cddf579144a699fb67ec7d5e`
- Audit date: 2026-09-03 (Asia/Shanghai)
- Real provider/API calls: **none**
- Real snpEff or bioinformatics execution: **none**
- Production/test/prompt/Skill/schema/session modifications by this audit: **none**
- Durable outputs: this report, `recovery_context_component_matrix_20260903.tsv`, and `RECOVERY_CONTEXT_CURRENT.md`

## Executive verdict

Current BioMind has **two active provider-facing Recovery entry paths**. Both converge on the same `build_node_context(node_type="recovery")` → `LLMPayload.to_provider_dict()` → `PromptManager.get_recovery_v2_prompt()` assembly and, for the same fixture, produced a byte-identical single `HumanMessage`. They differ after assembly:

1. Execute failure calls `agent_core.middleware._invoke_recovery_v2()` through the registered `error_recovery_middleware`. It supports one bounded `glob`/`read` evidence follow-up and typed Skill continuation handling, but does **not** call `assert_v2_llm_messages_clean()` on the complete initial or follow-up message before `ainvoke_with_snapshot()`.
2. Supervisor/CompletionGate post-review calls `BioAnalystAgent._adjudicate_post_review_recovery()`. It invokes through `_ainvoke_node_llm()`, which does apply `assert_v2_llm_messages_clean()` at the complete-message boundary. It handles typed Skill decisions, but does not implement the prompt-advertised `evidence_request` read/glob follow-up.

Recovery now sees strong task semantics: persisted raw user turns, a canonical TaskSpec projection, accepted Consultant handoff, selected Skill identity/headings, the semantic approved plan, current step/dependencies, accepted upstream outcomes, normalized failed action, bounded streams, artifact paths/facts, and prior accepted Recovery decisions. It does **not** receive raw Generate model text, full assistant/Consultant conversation, full Skill bodies by default, arbitrary internal state, or Controller control digests/revisions/tokens.

The largest correctness gap is **scope isolation**: `semantic_history.accepted_recovery_decisions` is populated from every loaded Recovery attempt without current-step/attempt filtering. A realistic offline fixture proved that an unrelated-step decision reaches the final provider message. Existing session `1788362482` independently shows an upstream cross-step evidence mismatch. The largest context-quality gap is relevance: the whole semantic approved plan and unfiltered Recovery history are injected while the selected Skill's relevant body is absent by default and its typed read capability is not advertised.

## Git and repository preflight

Initial preflight, before this audit created files:

```text
repository: /public/home/liusiqi/03.毕设/Biomind_v1
branch: refactor/consultant-controller-authority
HEAD: 9549734ba72ce783cddf579144a699fb67ec7d5e
tracked modified count: 1
staged count: 0
untracked count: 18
tracked modification: agent_core/plan_tracker.py
```

The worktree changed concurrently during the read-only inspection. Final validation showed 5 tracked modifications, 0 staged paths and 22 untracked paths; the additional tracked paths were `agent_core/completion_gate.py`, `agent_core/state.py`, `bio_agents.py`, and `tests/test_plan_lifecycle.py`, with `agent_core/active_state.py` and additional benchmark/test/audit material untracked. This audit did not reset, stash, clean, overwrite, stage, or modify those files. Because the provider capture used imports from the live working tree, both the audited HEAD and relevant working-tree blob hashes are recorded below. A final targeted diff review confirmed that the concurrent active-Recovery lifecycle edits did not change `_adjudicate_post_review_recovery()`, `_ainvoke_node_llm()`, `agent_core.node_context` or the R1 middleware assembly findings reported here.

## Previous related audits

| File | Date / audited snapshot | Still valid? | Relationship to this baseline / change since then |
|---|---|---|---|
| `runtime_and_recovery_context_contract.md` | 2026-08-17; `34934de0c92d7cae268a101a3dce13606039c21e` plus then-dirty tree | Partly | Still useful for runtime propagation. It does not inventory the current final Recovery message. |
| `recovery_progressive_disclosure_contract.md` | 2026-08-17; `358c31d1b6ef8a159315e7f0ec01aa37345bd196` | Topology/history only | Its one-shot/progressive-disclosure history remains useful, but its conclusions that raw user input/full plan/accepted handoff are absent are stale for the current projection. |
| `recovery_context_disclosure_matrix.tsv` | 2026-08-19-era implementation | Stale for provider visibility | It records full plan and raw original user message as invisible; both can now be provider-visible semantic inputs. Superseded for “what Recovery sees” by the 2026-09-03 component matrix. |
| `node_provider_context_matrix_20260820.tsv` | 2026-08-20-era implementation | Partly | Correct high-level NodeContext convergence, but predates the current conversation/handoff/plan/history details and invocation asymmetries. |
| `recovery_design_vs_effective_implementation_20260902.md`, `recovery_effective_call_chain_20260902.tsv`, `recovery_authority_matrix_20260902.tsv` | 2026-09-02; untracked at audit time | Yes for their incident/authority scope | They answer recovery ownership/effective call-chain questions, not the complete provider payload inventory. This baseline complements rather than replaces their incident findings. |
| `recovery_hypothesis_authority_audit_20260902.md`, `recovery_hypothesis_capability_matrix_20260902.tsv` | 2026-09-02; untracked at audit time | Yes for hypothesis authority | No full component-size or final-message baseline; not superseded outside provider-context questions. |

Future “Recovery 看到了什么” investigations should begin at `RECOVERY_CONTEXT_CURRENT.md` and this report rather than re-running the older matrices.

## Current architecture and exact entry paths

### Active provider-facing paths: 2

| Entry | Source / trigger | State and builders | Safety and provider boundary | Continuations |
|---|---|---|---|---|
| R1 Execute failure | Registered `error_recovery_middleware()` around Execute; non-success/current execution evidence; deterministic trigger/cache may avoid the LLM | `build_recovery_context()` → `build_recovery_semantic_focus()` → `_invoke_recovery_v2()` → `canonical_task_spec_for_state()`, `StateContractStore`, `RecoveryStateStore`, `load_approved_plan_snapshot()`, `project_skill_context()` → `build_node_context(recovery)` | `PromptManager.get_recovery_v2_prompt()` → one `HumanMessage` → `ainvoke_with_snapshot()`. Payload clean check runs in `LLMPayload.__post_init__`, but **no complete-message `assert_v2_llm_messages_clean()`** | One bounded prompt-advertised `read`/`glob` evidence request; typed Skill decisions are parsed and served, although the prompt does not advertise them |
| R2 Post-review | `_adjudicate_post_review_recovery()` after Supervisor `RETRY`, CompletionGate/final-gate failure and related review failures; deterministic/cache paths may avoid the LLM | Same canonical semantic sources and `build_node_context(recovery)`/prompt builder | one `HumanMessage` → `_ainvoke_node_llm()` → `assert_v2_llm_messages_clean()` → configured Recovery LLM | Typed Skill decisions handled in a bounded same-node loop; **prompt-advertised evidence request is not parsed/served here** |

The following are not additional provider-facing Recovery entry paths:

- Generate/pre-action/action-format failures use deterministic RecoveryPolicy and corrective Generate/Planner/Consultant routing; they do not independently assemble a RecoveryJudge message.
- A Generate provider exception does not create a third Recovery LLM call.
- Supervisor deterministic decisions, cached Recovery decisions, and sufficient deterministic diagnoses may finish without a provider call.
- Recovery retry/re-entry reuses R1 or R2 and the same NodeContext/prompt assembly.
- `_legacy_error_recovery_middleware()` is deprecated and not registered. It is not counted as production reachability.
- Recovery-generated action validation is downstream of the decision; it is not a distinct Recovery LLM context path.

## Full dataflow and projection boundaries

```text
StateContract user-turn chain ─────────────┐
Canonical TaskSpec ────────────────────────┤
Accepted Consultant handoff ───────────────┤
Selected Skill refs/headings/results ──────┤
ApprovedPlanSnapshot ──────────────────────┤
ExecutionRecord / RecoveryContext ─────────┤
RecoveryStateStore attempts ───────────────┘
                                           │
                 explicit semantic builders/allowlists
                                           ▼
  conversation + handoff + TaskSpec + plan + accepted outcomes/decisions
  current step + dependency contracts + recovery_focus + evidence + operations
                                           │
                         _without_control_metadata
                                           │
                       LLMPayload clean validation
                                           ▼
          {semantic_history, current_focus}.to_provider_dict()
                                           │
               PromptManager.get_recovery_v2_prompt
                                           ▼
                       one HumanMessage
                        │             │
             R1 Execute │             │ R2 post-review
                        ▼             ▼
             ainvoke_with_snapshot   assert_v2_llm_messages_clean
                        │             │
                        └───────┬─────┘
                                ▼
                         provider boundary

Machine-only Controller binding:
invocation IDs, task/plan semantic hashes and revisions, approval/CAS refs,
failure/evidence identities, signatures, nonces, authorization/runtime envelopes
                        └──────── X ────────> provider payload
```

### Layer-by-layer behavior

| Layer / function | Input → output | Boundary type | Add/remove/truncate/order/failure |
|---|---|---|---|
| `agent_core.diagnostics.build_recovery_context()` | current state + execution evidence → durable `RecoveryContext` | Broad collection | Adds task/plan/runtime/artifact/history/filesystem diagnostics. Per file reads at most 128 KiB, then derives up to the last 100 lines of that read; for files over 128 KiB this is not the true physical file tail. This broad object is not sent wholesale. |
| `agent_core.recovery.build_recovery_semantic_focus()` | durable RecoveryContext → `recovery_focus` | Explicit allowlist | Keeps current step fields, normalized action/script/command/cwd, exit/streams, semantic artifacts, diagnosis and up to three related-attempt summaries. Omits most runtime/control metadata. |
| `_node_context_semantic_inputs()` / middleware source loading | durable stores → semantic source rows | Explicit source selection | Loads persisted user conversation, accepted handoff, execution records, Recovery attempts and approved plan. Execute requires a linked `conversation_evidence.head_ref`; post-review can persist/recover raw submission or legacy user messages. |
| `build_node_context(node_type="recovery")` | canonical sources + runtime/evidence → `NodeContext` | Explicit semantic projections plus control stripping | Builds named semantic sections. Recovery runtime is fail-closed to `runtime.recovery_focus` only. Other internal runtime fields are discarded. |
| `_without_control_metadata()` | semantic trees → cleaned semantic trees | Recursive deny/delete, second defense | Removes known control keys, hash/digest/revision/provenance/timestamp families while preserving deliberate semantic fields such as Skill `content_hash`. It is not the primary source selector. |
| `LLMPayload.__post_init__()` / `assert_llm_payload_clean()` | two semantic maps → validated payload | Fail-closed payload guard | Rejects remaining control metadata/value patterns and enforces the provider payload size (Recovery caller supplies 256,000 chars); it does not silently repair overflow/leaks. Exact raw user-turn content is treated as authoritative semantic text. |
| `LLMPayload.to_provider_dict()` | payload → dict | Final provider projection | Emits exactly `semantic_history` then `current_focus`; ControllerBinding and compatibility aliases are not serialized. |
| `PromptManager.get_recovery_v2_prompt()` | provider dict → string | Ordered serializer | Adds stable Recovery policy/schema, pretty JSON sections, then the textual evidence protocol. It does not add the shared typed Skill-decision protocol. |
| message boundary | prompt → provider messages | Path-dependent | Both send one HumanMessage. R2 applies `assert_v2_llm_messages_clean()` before dispatch; R1 currently does not. |

### Projection questions

**Q1 — projection style:** mixed, led by explicit allowlists. Canonical semantic builders select fields first; `_without_control_metadata()` recursively deletes prohibited control residue; payload/message guards fail closed.

**Q2 — `_without_control_metadata()`:** second-line defense, not the principal boundary. The main boundary is semantic construction plus Recovery runtime replacement. The function cannot make a copied arbitrary internal object semantically appropriate.

**Q3 — bypasses:** there is no active path that serializes the broad `RecoveryContext` or ControllerBinding wholesale. Verbatim user turns and bounded evidence strings are intentionally direct semantic values and can contain arbitrary text. R2's complete-message guard checks the assembled result; R1 lacks that final guard. Prompt static text and follow-up evidence are also outside the earlier structured-value selector, which makes R1's missing final guard material.

### Exact visible-field source traces

| Provider-visible field | Canonical source → intermediate state → projection → final location |
|---|---|
| user wording | StateContract conversation object chain referenced by `plan_state.conversation_evidence.head_ref` → `read_conversation_evidence()` rows → `_semantic_conversation_rows()` role filter → `semantic_history.conversation[].content` → `<semantic_history>` in the HumanMessage |
| canonical requirement | `canonical_task_spec_for_state(plan_state).requirements[n]` → TaskSpec semantic input → `_task_spec_semantic_view()` → `semantic_history.task_spec.highlighted_constraints[n]` → `<semantic_history>` |
| accepted Consultant selected Skill | `task_state.accepted_consultant_handoff.selected_skills` and durable Skill access refs → `_accepted_consultant_handoff_view()` / `project_skill_context()` / `_selected_skill_ref_view()` → handoff + selected refs/capabilities → `<semantic_history>` |
| current step objective | `ApprovedPlanSnapshot.steps[n]` selected by current step handle → `_current_step_view()` → `current_focus.current_step` → `<current_focus>` |
| full plan/future step | approved plan snapshot loaded by `load_approved_plan_snapshot()` → `_approved_plan_semantic_view()` → `semantic_history.approved_plan.steps[]` → `<semantic_history>` |
| accepted upstream output | completed `ExecutionRecord`/plan reconciliation → `_accepted_execution_outcomes()` → `semantic_history.accepted_execution_outcomes[]` → `<semantic_history>` |
| failed normalized action | current `ExecutionRecord`/failure evidence → `build_recovery_context().previous_action` → `build_recovery_semantic_focus().previous_action` → `current_focus.runtime.recovery_focus.previous_action` → `<current_focus>` |
| stderr excerpt | `ExecutionRecord.stderr_path`/full log → bounded read in `build_recovery_context()` → `RecoveryContext.stderr` → `build_recovery_semantic_focus().bounded_stderr` → `current_focus.runtime.recovery_focus.bounded_stderr` → `<current_focus>` |
| concrete artifact path | plan-local binding / TaskSpec / execution reconciliation → `RecoveryContext.artifact_facts` and approved-plan materialization → semantic artifact allowlist / approved-plan view → `current_focus.runtime.recovery_focus.artifact_facts` and/or `semantic_history` → final HumanMessage |
| previous Recovery decision | `recovery/recovery_attempts.jsonl` through `RecoveryStateStore.load_attempts()` → `_accepted_recovery_decisions()` → `semantic_history.accepted_recovery_decisions[]` → `<semantic_history>`; currently no current-step filter |
| allowed operation | Controller path constant (`RecoveryAction` values in R2; fixed list in R1) → `build_node_context(allowed_operations=...)` → `current_focus.allowed_operations` → `<current_focus>` |

## What does Recovery LLM actually see?

### User / task

| # | Answer | Exact evidence |
|---|---|---|
| 1. Current raw task input | **CONDITIONAL** | All persisted `role=user` rows from the linked StateContract conversation chain become `semantic_history.conversation[].content`. Execute has no raw-message fallback when `head_ref` is missing; post-review can recover/persist a raw submission or legacy user messages. |
| 2. Complete chat history | **NO** | User turns can be complete, but assistant/Consultant messages and reasoning are filtered out by the semantic conversation projection. |
| 3. Canonical TaskSpec | **YES** | `canonical_task_spec_for_state()` → task semantic view containing goal, ordered requirements/highlighted constraints, inputs, outputs, confirmed decisions and unresolved questions. It is not a byte-for-byte dump of every schema/control field. |
| 4. Explicit user constraints | **YES** | The full ordered TaskSpec `requirements` list is emitted as authoritative `highlighted_constraints`; constraints may also repeat in raw user turns and handoff summaries. |
| 5. Expected outputs and paths | **YES** | TaskSpec inputs/outputs, handoff fields, approved plan materializations and current artifact facts expose semantic names and concrete/expected paths when known. |

Therefore Recovery can directly see original user wording **only when it was durably linked/recovered into conversation evidence**; otherwise it sees canonical/accepted projections.

### Consultant / selected Skill

| Information | Status | Provider-visible form |
|---|---|---|
| Consultant final conclusion/handoff | **YES** | Accepted allowlist: summary, requirements summary, goal interpretation, input files, required outputs, selected skills, capability decisions, runtime requirements, resolved ambiguities, unresolved questions, database strategy. |
| Consultant reasoning/history/messages | **NO** | Raw reasoning, prior assistant turns and retry chatter are not projected. |
| selected skill ID (`genomics/snpeff-annotation`) | **YES** | Can appear in accepted handoff, `selected_skill_refs[].skill_id`, `selected_capabilities`, and step Skill fields. The fixture confirmed the ID in the final message. |
| Skill title/description | **CONDITIONAL** | Compact headings/catalog metadata only when present in the projected Skill access state. |
| Skill contract/relevant instructions | **CONDITIONAL** | Not present by default beyond compact headings/refs. Served excerpts/results can enter after a typed context request. |
| full `SKILL.md`, examples, document body/resources | **NO by default / CONDITIONAL on demand** | Default capture contained no full body. The controller supports typed operations `headings`, `section`, `search`, `range`, `resource`, `full`, but `get_recovery_v2_prompt()` does not include `_skill_decision_protocol()`, so this capability is not disclosed to the Recovery model. Both active paths contain handlers if the model nevertheless emits a valid typed decision. |

### Planner / plan

| Information | Status | Provider-visible form |
|---|---|---|
| Full approved plan | **YES, semantic form** | `_approved_plan_semantic_view()` emits all approved steps, including previous/current/future steps, dependencies, success criteria, plan-local artifacts and task output materialization paths. It strips approval/control identity. |
| Raw approved Plan JSON / plan revision | **NO** | No complete internal object and no `plan_revision` in provider payload. |
| Current step/objective/inputs/outputs/completion criteria | **YES** | `current_focus.current_step` is a dedicated semantic projection; the same step also occurs within the approved plan. |
| Direct dependencies | **YES** | `current_focus.dependency_contracts`; dependency handles also appear in the plan step. |
| Previous completed step outcomes | **YES** | `accepted_execution_outcomes` contains completed step handle/status/action summary/produced handles/reconciliation; not raw logs. |
| Future downstream steps | **YES** | Present because the full semantic approved-plan step list is included. |
| Plan repair history / Plan Review result | **NO by default / INDIRECT if normalized elsewhere** | No raw review or repair history section. A resulting approved snapshot or typed diagnostic evidence may reflect the outcome. |

### Generate and Execute

| Information | Status | Exact route / boundary |
|---|---|---|
| Generate raw model response | **NO** | Never serialized as raw text. |
| Normalized `ExecutableAction` | **YES** | `RecoveryContext.previous_action` → `build_recovery_semantic_focus().previous_action`; allowlisted action/type/language/argv/code/command/cwd/purpose/command_role. |
| script/code/argv/cwd/actual command | **YES when recorded** | Normalized action plus `previous_script` and `failed_command`; the fixture included and recovered them. |
| explicit env / actual runtime environment | **CONDITIONAL / PARTIAL** | Only if encoded in allowlisted action or diagnostic evidence. Broad runtime manifests are not copied into `recovery_focus`; arbitrary `env` is not an allowlisted action field. |
| action identity | **INDIRECT** | Semantic action summary is visible; machine action/binding IDs are removed. |
| NORMAL/RECOVERY generation mode | **NO** | Control/runtime mode is not a semantic field. |
| prior Generate attempts/errors | **NO raw history / CONDITIONAL semantic failure** | Current normalized failure and typed diagnostic facts can be visible; raw prior responses are not. |
| exit code / exception | **YES / CONDITIONAL** | `exit_code` is explicit. Exception text appears only when captured into diagnosis/evidence. |
| stdout/stderr | **YES, bounded** | RecoveryContext reads at most 128 KiB per source file, then takes up to the last 100 lines of the bytes it read. For a <=128 KiB file, the full string may pass through; for a larger file the excerpt is the tail of the prefix, not the true file tail. |
| dedicated stdout/stderr excerpt | **YES** | `recovery_focus.bounded_stdout` and `.bounded_stderr`; evidence rows can repeat the same content. `bounded_log_excerpt` is normally capped near 6,000 characters. |
| progressive disclosure | **LIMITED** | R1 permits one bounded read/glob follow-up relative to the current task evidence root. R2 advertises the same protocol but does not serve it. There is no general semantic level expansion. |
| attempt ID/timestamps/timeout | **NO machine ID/timestamps; timeout CONDITIONAL** | ControllerBinding keeps attempt identity machine-only. Timing/control values are absent unless included as explicit semantic evidence. |

### Artifacts

Provider-visible semantic facts include handle/identity, role, direction, required, format/kind, concrete or expected path, existence, readability, parent existence/readability/writability, plan materialization path and accepted produced handles. Writability is normally a parent-path fact rather than a universal artifact-file field.

Not provider-visible by default: artifact size, producer attempt identity, raw manifest identity, control binding, capability/contract/state/runtime snapshot/value/manifest/registry digests, CAS/signature/nonce/token, and arbitrary runtime manifest metadata. Producer step can be inferred from accepted outcome/plan association, but is not consistently attached to each artifact fact.

### Recovery history and Supervisor

| Information | Status | Exact behavior |
|---|---|---|
| prior Recovery attempts | **YES** | Two projections exist: `recovery_focus.previous_recovery` takes the last three related summaries selected by step/fingerprint/action-family score, while `semantic_history.accepted_recovery_decisions` projects **all** loaded accepted attempts. |
| raw previous Recovery LLM response / raw PATCH_SCRIPT | **NO** | Only accepted semantic decision fields and bounded summaries are projected. |
| retry count / budget / execution envelope | **NO** | Control-plane counters, IDs and envelope metadata stay machine-only. |
| current-step/attempt filtering | **NO overall** | The diagnostic “related” list is scored but can include same-family cross-step items; accepted decisions are not filtered at all. The fixture included an unrelated-step decision in the final prompt. |
| Supervisor previous decision/validation/evidence | **CONDITIONAL / INDIRECT** | Post-review diagnostics and bounded evidence can reflect validation/completion failure. There is no always-present raw Supervisor decision/history section. |
| completion audit result/escalation history | **CONDITIONAL** | Visible only if normalized into the Recovery diagnosis/evidence supplied on that path. |

### Controller machine metadata

Current fixture verification found no provider-visible `capability_contract_hash`, `contract_hash`, `runtime_snapshot_digest`, `value_digest`, manifest/registry/state digests, state/CAS revisions, signatures, nonces, authorization tokens, approval refs, invocation IDs or recovery execution envelope. `ControllerBinding` retains these machine-side.

One deliberate distinction: selected Skill refs may expose `content_hash`/`resource_content_hash` as semantic content provenance. These are not Controller control digests and were visible in the fixture. Thus “any hash” is **YES for Skill content provenance**, while the requested machine control digest/hash families are **NO in the current fixture**.

## Exact provider message assembly

There is no Recovery SystemMessage, developer message, or provider-native tool schema. The initial provider request is exactly one `HumanMessage` with this order:

1. RecoveryJudge role, constraints and classification rules from `RECOVERY_JUDGE_PROMPT`.
2. Allowed failure-class, failure-owner and action values.
3. Required `RecoveryDecision` JSON schema.
4. `<diagnostic_context>` wrapper.
5. `<semantic_history>` pretty JSON:
   - conversation
   - accepted Consultant handoff
   - canonical TaskSpec
   - selected Skill refs/headings/capabilities and any already-served excerpts
   - semantic approved plan
   - accepted execution outcomes
   - accepted Recovery decisions
6. `<current_focus>` pretty JSON:
   - current step
   - direct dependency contracts
   - `runtime.recovery_focus`
   - bounded evidence
   - allowed operations
7. Textual evidence protocol with read/glob JSON examples.

Selected Skill, user requirements and full plan are in `semantic_history`; failed action and bounded stdout/stderr are mainly in `current_focus.runtime.recovery_focus`; stderr can also appear under `current_focus.evidence`. The evidence request is an application-level JSON protocol, not a provider tool definition. The shared `_skill_decision_protocol()` is absent from the Recovery prompt.

## Offline provider-boundary verification

The audit constructed a realistic fixture in a temporary directory outside the repository. It contained two persisted user turns; accepted Consultant handoff; selected `genomics/snpeff-annotation`; a three-step approved plan; current step 2 and direct dependency; prior success; failed normalized action with rc=2, stdout, stderr, script/argv/cwd and concrete paths; a same-step prior Recovery attempt; an unrelated-step Recovery attempt; and internal digests/tokens.

No real provider was configured or contacted. A mock provider captured R1 through production `_invoke_recovery_v2()`. A fake dispatch captured R2 through production `_ainvoke_node_llm()`, thereby exercising that path's real complete-message guard.

| Check | Result |
|---|---|
| R1 provider calls/messages | 1 call; 1 HumanMessage |
| R2 guarded dispatch/messages | 1 dispatch; 1 HumanMessage |
| Initial message identical across paths | **YES, byte-identical** |
| Prompt size | 13,692 characters; ~3,423 tokens using chars/4 |
| Embedded provider payload JSON | 6,859 characters |
| Payload top-level keys | `semantic_history`, `current_focus` only |
| Machine-control needles | none found |
| `assert_v2_llm_messages_clean()` result on captured prompt | passed manually for R1 fixture; genuinely invoked by R2 |
| Selected Skill ID visible | yes |
| Full Skill body visible | no |
| Both original user turns visible | yes |
| Consultant question/assistant history visible | no |
| Future plan step visible | yes |
| Unrelated-step Recovery decision visible | **yes** |
| Skill content hashes visible | yes, deliberate semantic provenance |
| Prompt SHA-256 (audit fixture only) | `9c5ba583a26405af39f45ac7117281c76b565b87eda1242f93ec8c06ce29b66e` |

Important limitation: R1's production caller did not run the complete-message guard; the audit applied it after capture only to test the fixture. Passing this fixture does not close the missing production boundary.

## Historical session evidence (read only)

### `session_1788356337`

Selected because it contains the requested historical Recovery failure and old digest-leak evidence. `run_metrics.json`, `plan_state.json`, `recovery/diagnostic_context.json`, `recovery/recovery_context.json`, `recovery/llm_results.json`, `recovery/recovery_decision.json` and execution evidence show one RecoveryJudge attempt while step 1 was verifying inputs. The decision failed internally with `LLM_CONTROL_METADATA_LEAK` because the then-built `recovery_focus` contained `runtime_snapshot_digest`/`value_digest`. No Recovery provider request directory was persisted. This is **historical pre-provider failure**, not proof that the current projection leaks those fields.

### `session_1788362482`

Selected because it records a real state/step cross-line. The current step was `check-contig-compatibility`, while deterministic Supervisor evidence stated that an `ExecutionRecord` belonged to `validate-inputs`. Metrics show zero RecoveryJudge provider calls; therefore this session is evidence of upstream scope risk, not an observed provider payload.

Neither session persists a trustworthy source commit identity that can be equated with the audited HEAD. Both directories remained unchanged.

## Context size and token engineering

The following is the final captured 13,692-character message. Token counts are deliberately lightweight estimates at four characters/token; no dependency was installed.

| Component | Chars | Est. tokens | % total | Current limit/behavior |
|---|---:|---:|---:|---|
| Recovery policy + decision schema | 3,186 | 796 | 23.3% | Static template |
| Semantic user conversation | 309 | 77 | 2.3% | Persisted user turns; payload hard cap only |
| Consultant handoff | 469 | 117 | 3.4% | Field allowlist |
| TaskSpec semantic view | 1,696 | 424 | 12.4% | Field allowlist; all ordered requirements |
| Selected Skill refs | 348 | 87 | 2.5% | Compact refs |
| Skill headings/capabilities | 504 | 126 | 3.7% | Compact headings; no full body by default |
| Approved plan | 1,222 | 306 | 8.9% | All semantic steps; no per-section token cap |
| Accepted execution outcomes | 152 | 38 | 1.1% | Semantic completed outcomes |
| Accepted Recovery decisions | 529 | 132 | 3.9% | All loaded accepted decisions; unbounded count |
| Current step + dependencies | 586 | 146 | 4.3% | Dedicated duplicate of plan neighborhood |
| Recovery focus (action/logs/artifacts/history) | 2,033 | 508 | 14.8% | RecoveryContext read caps; payload hard cap |
| Bounded evidence | 286 | 72 | 2.1% | Caller-specific excerpts; commonly ~6,000-char log excerpt cap |
| Allowed operations | 77 | 19 | 0.6% | Fixed list |
| Evidence protocol | 1,010 | 252 | 7.4% | Static text |
| Structural tags/JSON overhead | 1,285 | 321 | 9.4% | Pretty JSON/wrappers |
| **Total** | **13,692** | **3,423** | **100%** | Recovery payload caller cap 256,000 chars; overflow fails rather than semantically shrinking |

The sample is not a production maximum. History and plan have no independent bounded-count/token budgets; aggregate capacity is a hard failure boundary. Log collection is bounded, but the same stream can be serialized in `recovery_focus`, evidence rows and post-review diagnostic excerpts.

## Duplication and contradiction audit

| Finding | Rating | Exact source/projection path |
|---|---|---|
| Same user requirement repeated | **MEDIUM** | StateContract user turn → conversation; TaskSpec requirements → highlighted constraints; accepted handoff → requirements summary. |
| Same input/output path repeated | **MEDIUM** | TaskSpec inputs/outputs + handoff input/output lists + approved plan materialization + current action/artifact facts. |
| Full plan plus current step/dependencies | **LOW–MEDIUM** | Approved-plan semantic view contains current/dependency steps; current_focus repeats current step and direct dependency contracts. Usually consistent because both derive from one approved snapshot. |
| Selected Skill identity repeated | **MEDIUM** | handoff selected skills + selected_skill_refs + selected_capabilities + step Skill list. |
| Selected Skill versus body/context inconsistency | **MEDIUM** | Identity/headings are visible, body absent by default; typed retrieval handlers exist but current Recovery prompt does not advertise the protocol. |
| Old-step execution evidence mixed into current step | **HIGH risk, conditional occurrence** | Upstream record selection can mismatch; session `1788362482` observed the mismatch. Accepted execution outcomes are semantic, but current evidence correctness depends on caller scope. |
| Old Recovery decision mixed into current step | **HIGH, reproduced** | `RecoveryStateStore.load_attempts()` → `_accepted_recovery_decisions()` without current-step/attempt filter. Offline final message contained the unrelated decision. |
| Same stderr repeated | **MEDIUM–HIGH** | `recovery_focus.bounded_stderr` + `current_focus.evidence`; post-review may add `bounded_log_excerpt`/diagnosis evidence. |
| Current step conflicts with approved plan | **LOW in builder** | Both normally derive from the same approved snapshot and step handle; stale/current binding checks guard result use. Upstream evidence can still be cross-scoped. |
| Full Skill text repeated | **NONE** | Full Skill body was not in the default message. |
| Historical context unbounded growth | **MEDIUM** | Accepted Recovery decisions include all loaded attempts; whole payload eventually fails at 256,000 chars instead of truncating by relevance/count. |

## Context authority matrix

| Information | Authoritative source | Recovery-visible form | Machine-only form | Scope | Lifecycle |
|---|---|---|---|---|---|
| User requirements | StateContract conversation + canonical TaskSpec | verbatim user turns when linked; ordered highlighted constraints | conversation object refs/revisions | task | stable/revised by accepted user input |
| Task intent/outputs | canonical TaskSpec | goal, requirements, inputs, outputs, decisions/questions | TaskSpec revision/hash | task | stable/revised |
| Consultant conclusion | accepted Consultant handoff in task state | accepted field allowlist | raw reasoning, invocation/retry metadata | task | accepted stable snapshot |
| Selected Skill | Skill access state / selected refs | skill ID, compact ref, headings/capabilities, served excerpts | continuation/controller envelope and binding revisions | task/step | selected + on-demand |
| Approved plan | approved PlanSnapshot | semantic all-step plan and materializations | approval ref, plan hash/revision/CAS | task/plan | approved snapshot |
| Current step | approved plan + current step handle | current-step fields and direct dependency contracts | current state generation/signature | step | active |
| Execution evidence | ExecutionRecord and attempt files | normalized action, exit, bounded streams, command/script/cwd | attempt ID, timestamps/manifests/digests | attempt | active/history |
| Artifact evidence | plan bindings + execution reconciliation | semantic handle/role/path/existence/readability/materialization | manifest/control binding/digests | step/attempt | active/history |
| Recovery diagnosis | durable RecoveryContext | allowlisted recovery_focus and evidence | context ID/fingerprint/control provenance | recovery/attempt | active |
| Recovery decision | RecoveryStateStore attempts | accepted semantic summaries/fields | raw LLM response, decision IDs, budgets/envelopes | recovery/history | active/history |
| Supervisor/completion result | deterministic review/completion state | conditional typed diagnosis/evidence | full control decision/audit refs | step/recovery | event/history |
| Capability/state hashes | ControllerBinding/internal state | **NOT VISIBLE** | hashes, digests, revisions, CAS/signatures/tokens | internal | invocation/state lifecycle |
| Skill content hash | selected Skill ref | content provenance hash **VISIBLE** | Controller binding around it remains hidden | task/step | selected snapshot |

## Desired three-layer model comparison

| Desired layer/item | Current status | Assessment |
|---|---|---|
| Layer 1: canonical user intent/constraints/TaskSpec/outputs | **FULLY PRESENT** | Strong semantic view; raw user turns are conditional on durable linkage and create some duplication. |
| Layer 1: selected Skill identity | **FULLY PRESENT** | ID/refs/headings/capabilities are visible. |
| Layer 1: accepted handoff/relevant Skill contract | **PARTIALLY PRESENT** | Handoff is present; relevant Skill body is absent by default and retrieval is not advertised. |
| Layer 2: current step/objective/I/O/criteria/dependencies | **FULLY PRESENT** | Dedicated current step and direct dependency contracts. |
| Layer 2: relevant completed upstream outcomes | **FULLY PRESENT** | Semantic outcomes, not raw logs. |
| Layer 2: only relevant plan neighborhood | **OVER-INJECTED** | Entire semantic approved plan, including future steps, is sent. |
| Layer 3: failed action/exit/streams/artifacts | **FULLY PRESENT with bounds** | Explicit env/runtime and exceptions remain conditional. |
| Layer 3: previous Recovery attempts | **OVER-INJECTED / incorrectly scoped** | A related last-three summary coexists with an unfiltered all-attempt accepted-decision list. |
| Machine-only isolation | **PARTIALLY PRESENT across paths** | Structured payload isolation passed; R1 lacks the final complete-message guard applied in R2. |

The current problem is **not** “technical evidence is sufficient but user task semantics are weak.” User semantics are strong and sometimes duplicated. The important weaknesses are relevance/scoping, selected Skill body availability, and path-consistent safety/capability handling.

## Key questions — concise answers

1. **Recovery 是否看到用户当前任务原始输入？ — CONDITIONAL.** Persisted/recovered user turns are verbatim; Execute has no fallback when the conversation head ref is absent.
2. **是否看到完整聊天历史？ — NO.** User turns only, not full assistant/Consultant history.
3. **是否看到 canonical TaskSpec？ — YES.** Canonical semantic projection, not raw control schema.
4. **是否看到明确用户 constraints？ — YES.** Ordered `highlighted_constraints` is authoritative.
5. **是否看到 selected_skills？ — YES.** Handoff + selected refs/capabilities/step Skill fields.
6. **是否看到 Consultant handoff？ — YES.** Accepted allowlisted fields.
7. **是否看到完整 Skill 文档？ — NO by default.**
8. **是否只看到 selected Skill ID / relevant rules？ — ID/headings YES; relevant body only if already served.**
9. **是否可以按需读取 Skill？ — CONDITIONAL.** Production handlers support typed reads, but Recovery prompt does not advertise them.
10. **是否看到完整 approved plan？ — YES in semantic all-step form; NO for raw internal JSON/revision.**
11. **是否看到 current step？ — YES.**
12. **是否看到前置 step 完成结果？ — YES, accepted semantic outcomes.**
13. **是否看到后续 step？ — YES, through the all-step approved plan.**
14. **是否看到 Generate script？ — YES if normalized/persisted; raw Generate response NO.**
15. **是否看到真实 Execute command？ — YES when failure evidence records it.**
16. **是否看到 stdout/stderr？ — YES, bounded.**
17. **截取多少？ — At most 128 KiB read per log followed by last 100 lines of that read; additional diagnostic excerpts are commonly ~6,000 chars.**
18. **是否看到真实 input/output paths？ — YES when concrete bindings/materializations exist.**
19. **是否看到 previous Recovery attempts？ — YES.**
20. **是否按 current step scope 过滤？ — NO overall.**
21. **是否看到任何 digest/hash/revision/token？ — Machine control values NO in the fixture; Skill content hashes YES by design.**

## Recommendations (not implemented)

### P0 — correctness / isolation

1. Scope `accepted_recovery_decisions` to the current step/attempt/fingerprint or an explicit relevance contract; fail closed when current evidence's owning step does not match the current step.
2. Apply `assert_v2_llm_messages_clean()` to every complete R1 initial and follow-up message immediately before `ainvoke_with_snapshot()`, using one shared provider dispatch boundary.
3. Make the evidence protocol truthful and path-consistent: either serve the advertised bounded read/glob request in R2 or omit it from that path's prompt.

### P1 — context quality

1. Advertise the typed Skill-context protocol to Recovery and supply only the smallest relevant selected-Skill excerpt, or provide a small default relevant contract. Do not inject full Skill documents unconditionally.
2. Define a bounded approved-plan neighborhood: current step, direct dependencies, relevant completed upstream outcomes, and only downstream steps required for impact analysis.
3. Establish one explicit relevance rule for execution and Recovery history and expose provenance that the model can use without leaking Controller identities.

### P2 — token / maintainability

1. Deduplicate raw/canonical requirements, paths, selected Skill identity and log excerpts while retaining an authoritative representation.
2. Centralize Recovery message assembly and mandatory complete-message validation for both entry paths.
3. Add per-component character/count telemetry and bounded history policies before the 256,000-character hard-failure boundary; use an existing tokenizer only if already available.
4. Re-audit based on relevant source blob changes rather than unrelated repository HEAD changes.

## Source-of-truth and staleness record

Blob hashes below describe the live files used by the audit. For dirty files both the capture blob and HEAD blob are shown.

| Path | Relevant functions / role | Live capture blob | HEAD blob / last relevant commit |
|---|---|---|---|
| `agent_core/recovery.py` | semantic focus, decision/evidence parsing | `72d43f54c534f5beee363ab236011d1d69780205` | same; last `d27238d...` |
| `agent_core/diagnostics.py` | broad RecoveryContext, log/artifact/history collection | `1e45c8296b4225af55e14de8110fe51bc8962ac1` | same; last `0b0b0ef...` |
| `agent_core/node_context.py` | Recovery semantic projections, stripping, payload guard/binding | `9e7ddbf8423cb380e9ebff3e1438cb5bf6b05423` | same; last `d27238d...` |
| `agent_core/middleware.py` | R1 entry, evidence/Skill continuations, provider invoke | `0e21dadc3ff9c9a81a2b822ebd9003681c50d67f` | same; last `d27238d...` |
| `agent_core/plan_tracker.py` | canonical/approved plan state and step tracking | `863ea28290c0172dc66900f4078a68689ff2cc57` (capture); `bf317b50bda8e9236408823f023792be00154085` (final validation) | `cbcf17974636340971b097af5a6046602b969025` |
| `agent_core/action_runtime.py` | normalized action/runtime evidence | `a65d6a13382dc8f58d495f17acd249f23a81d910` | same |
| `agent_core/controller_audit.py` | complete-message/payload safety validation | `f8036da1c9701498131cc6e5d94e5ad323b7601f` | same |
| `agent_core/task_spec.py` | canonical TaskSpec projection/revision | `a6d9f84d7dc6beef7d9dec66bbb194679e635607` | same; last `5cfcff...` |
| `agent_core/skill_access.py` | selected Skill refs/projection/continuations | `52f3ebf14bdfa441577384305aaf6fecb3af705c` | same; last `e13a4cf...` |
| `agent_core/context_lineage.py` | conversation evidence lineage | `a767f633a959b3a6fc78b9526a381e4e4421dc94` | same; last `d70ec0f...` |
| `prompts/manager.py` | `get_recovery_v2_prompt()` final assembly | `7ff26534c53244bad6d5062975ac8ba08835698c` | same |
| `prompts/templates.py` | Recovery role/schema template | `56d956897d983bffa61521d314bb1b748ac165c4` | same |
| `bio_agents.py` | R2 post-review entry and guarded provider dispatch | `4cd369453104befbe3e532c4900614b2fab01f65` (capture); `09b42a71c4a9846051c4ef221dd016875233df64` (final validation) | `f1915f769d5b0b301d628bbddbea230707940458` |

The baseline is stale when relevant behavior or blobs change in Recovery collection/projection, NodeContext, prompt/message assembly, complete-message safety, selected Skill projection, canonical TaskSpec/approved Plan projection, execution evidence schema, or recovery/state lifecycle. A repository HEAD change alone is insufficient if these boundaries are unchanged.

## Limitations

- The capture is a realistic synthetic fixture, not a real biological execution, and it exercised only the initial provider message. It intentionally avoided real API and snpEff calls.
- Token counts use the requested low-cost chars/4 estimate; they are not model-tokenizer exact.
- R1 complete-message validation was tested after capture because production does not call it. R2 tested its actual production guard.
- The live worktree changed concurrently. This report anchors the audited repository HEAD and relevant file blobs; `bio_agents.py` and `plan_tracker.py` capture blobs differ from HEAD.
- Historical sessions do not persist a source commit that can be confidently mapped to current code, and neither contains a current-code Recovery provider capture.
- This audit records architecture and recommendations only; it does not claim any P0/P1/P2 item was repaired.
