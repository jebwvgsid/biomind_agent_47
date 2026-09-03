# Recovery Context Engineering P0 Delta

Status: **IMPLEMENTATION + TARGETED OFFLINE VERIFICATION**

- Repository: `/public/home/liusiqi/03.毕设/Biomind_v1`
- Branch: `refactor/consultant-controller-authority`
- Previous full baseline: `recovery_context_engineering_audit_20260903.md`
- Previous audited HEAD: `9549734ba72ce783cddf579144a699fb67ec7d5e`
- Pre-patch HEAD read from the live checkout: `295819aae195c05d95698fb3676c06f0ec055216`
- Current implementation baseline HEAD: `8ae7db95ff28b4f85f8f19d2c6c251046d062251`
- Real provider/API calls: **none**
- Real snpEff execution: **none**
- Existing benchmark-session modifications: **none**

This is an incremental record. It does not repeat or replace the full provider
context inventory in the previous baseline.

## Relevant source changes

| Path | Previous baseline/pre-patch blob | Current blob | Delta |
|---|---|---|---|
| `agent_core/node_context.py` | `9e7ddbf8423cb380e9ebff3e1438cb5bf6b05423` | `8cfcca6dec5a0cbb001019d3c7ce52aa9ca90677` | active-failure Recovery history scope and single authoritative projection |
| `agent_core/middleware.py` | `0e21dadc3ff9c9a81a2b822ebd9003681c50d67f` | `f4256c3a3820eaf248c8542457cb82f2483d86df` | R1 guarded assembly, shared evidence continuation, post-guard call accounting, durable scope fields |
| `agent_core/recovery_evidence.py` | `8b10b0dd7b13c8f5359917e6f2c5f2fb61e46905` | `753b8b700e46dd1ab8db0ee4e3c8d4014fcdfdf7` | shared root/executor and machine-only state exclusion |
| `agent_core/provider_message_safety.py` | absent | `836a79ef88085aece3390b6af29f211dc303998f` | extracted complete-message typed safety guard |
| `agent_core/recovery_provider.py` | absent | `6d05d0ab7578f0e929889636f8ca6a069834ea70` | shared guarded Recovery prompt/message and evidence-result assembly |
| `bio_agents.py` | `16afe34a96b6ac8b2bf947597d663c4ebfa88eab` | `5990276b4a200e7f5251edba844f470078f90259` | R2 shared assembly and one bounded read/glob follow-up |
| `prompts/manager.py` | `7ff26534c53244bad6d5062975ac8ba08835698c` | unchanged | advertised read/glob protocol now matches both paths |
| `tests/test_recovery_context_p0.py` | absent | `c345eeb419fbdb4e437caed460dc86fdb0651e27` | P0 negative, parity, metric, and delta-capture fixtures |

## P0 fixes

### Active-failure Recovery history

`semantic_history.accepted_recovery_decisions` is now the only
provider-facing Recovery history. `recovery_focus.previous_recovery` and
`previous_recovery_summary` remain available to machine-side construction but
are removed before provider projection.

An accepted decision must prove current task/session, plan ID/version/revision,
and current step. It must also match an available source attempt, source action,
failure fingerprint, or normalized action family. Legacy entries without the
required scope are excluded. The projection keeps only the most recent three
matching decisions. The underlying `RecoveryStateStore` rows are not deleted or
shortened; existing checkpoint-upsert behavior is unchanged.

### Complete-message safety and call accounting

R1 and R2 now use `guarded_recovery_provider_messages()` for the final
Recovery prompt plus HumanMessage construction. Initial messages and bounded
evidence-result continuations run the extracted
`assert_v2_llm_messages_clean()` guard and fail with
`ControlMetadataLeakError`; there is no silent redaction. R2 also retains the
generic `_ainvoke_node_llm()` guard as defense in depth.

R1 records `recovery_judge_count`, `recovery_judge_calls`, and
`judge_invoked` only through a dispatch callback reached after the complete
message guard passes. R2 sets its invoked flag only after guarded assembly and
charges the durable judge counter after an accepted, fresh response. A rejected
message therefore produces zero provider calls and no call indicator.

### Symmetric read/glob continuation

Both R1 and R2 use the same parser, task-root resolver, reader, size/line/file
limits, and follow-up message builder. One invocation can produce at most one
evidence follow-up; a repeated request is rejected before a second evidence
read or a third provider call. Absolute paths, parent traversal, unavailable
task roots, symlink escape, machine-only directories/files, JSONL ledgers, and
manifest paths are unavailable. The follow-up complete message is guarded
before dispatch.

## Delta provider capture

`RecoveryDeltaProviderCaptureTests` builds a non-empty offline fixture with two
persisted user turns, canonical TaskSpec, accepted Consultant handoff, selected
Skill ID plus compact headings, a two-step approved plan and dependency,
normalized failed action, exit code, bounded stdout/stderr, concrete input and
output paths, same-step and unrelated-step history, and machine hashes/digests.
It executes production R1 and R2 assembly with mock providers.

| Assertion | R1 | R2 |
|---|---:|---:|
| Initial mock provider calls | 1 | 1 |
| Shared complete-message guard calls | 1 | 1 |
| Same-step relevant decision visible | yes | yes |
| Unrelated-step decision visible | no | no |
| Machine control metadata visible | no | no |
| Selected Skill ID/headings visible | yes | yes |
| Full Skill body visible by default | no | no |
| Prompt advertises implemented read/glob | yes | yes |

Separate integration fixtures execute one real shared bounded read follow-up on
each path (two mock provider calls per path), verify identical read semantics,
and verify that repeated requests stop at two calls.

## Skill protocol inventory

Recovery continues to expose selected Skill identity and compact headings by
default and does not inject a full Skill body. Typed Skill handlers for
`headings`, `section`, `search`, `range`, `resource`, and explicit `full`
requests still exist in both paths, but the Recovery prompt does not advertise
that protocol. They are marked compatibility-only in the shared provider
module. Formal publication is deferred to P1 because it requires one fully
specified, shared continuation contract; it is not required for the P0
read/glob and safety correction.

## Validation

| Check | Result |
|---|---|
| P0 history/safety/evidence/metrics/delta capture plus NodeContext and Recovery Skill regressions | PASS — 50 tests |
| Cross-step active-state, current nonterminal ESCALATE, Recovery policy ownership, and session Recovery contract | PASS — 50 tests + 19 subtests |
| Relevant `py_compile` | PASS |
| `git diff --check` before production commit | PASS |

One broader pre-action/real-graph run was inspected but is not an acceptance
failure for this delta: 60 tests and 21 subtests passed; six tests in
`test_plan_local_preaction_convergence_v2.py` failed. One still expects the old
terminal `finish` route explicitly superseded by `295819a`; five stop at an
unrelated Planner capability-scope gate before Execute. No production change
was made to satisfy those stale/out-of-scope expectations.

## Remaining P1/P2 findings

- P1: formally publish or remove the compatibility-only typed Skill protocol;
  if published, both paths must share the same bounded handler and `full` must
  remain explicit-only.
- P1: consider bounding approved-plan projection to the current dependency and
  impact neighborhood rather than the whole semantic approved plan.
- P2: reduce remaining TaskSpec/plan/path/log duplication and add per-component
  provider-context telemetry before the aggregate hard cap.
- P2: improve the repository pytest bootstrap so its LangChain stubs do not
  shadow an installed `langchain_core` package during collection.

No Controller artifact environment injection, GenerationMode behavior,
active-Recovery lifecycle, or nonterminal ESCALATE semantics was changed.
