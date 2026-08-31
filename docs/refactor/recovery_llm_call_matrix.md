# Recovery LLM Call Matrix

Audit date: 2026-07-31

This matrix distinguishes active correction calls from ancillary and dead
implementations. Line numbers refer to the audited working tree.

## Active Calls

| ID | File/function | Trigger | Role | Prompt builder | Input and bounds | Output / route effect |
|---|---|---|---|---|---|---|
| G1 | `bio_agents.py:10782-11121`, call through `_ainvoke_llm()` at `bio_agents.py:5357` | Normal next action or committed same-step repair | Generate action / repair generator | stable system prompt + `PromptManager.build_ephemeral_context_result()` + `_generate_visible_messages()` | Stable system; ephemeral max 26k chars; visible messages max 6 / 12k chars | Parsed action; Controller alone accepts and routes it |
| G2 | Same call as G1 | First `unrecognized_action` or illegal action | Action format repair | `_action_format_repair_feedback()` plus dynamic current action contract | Same Generate projection, with raw-invalid-response record/hash and changed repair feedback | One repaired action attempt; no separate format-repair model |
| R1 | `agent_core/middleware.py:1718` | `RecoveryTrigger.judge_required` for ambiguous/conflicting evidence | RecoveryJudge | `PromptManager.get_recovery_judge_prompt()` | One HumanMessage; structured diagnostic projection clipped to 26k | Structured `RecoveryDecision`; does not emit code |
| R2 | `agent_core/middleware.py:1730` | R1 output fails schema parsing | RecoveryJudge schema repair | R1 prompt + parse error | One HumanMessage; does not include the complete raw invalid answer | One repair only; failure becomes deterministic escalation |
| S1 | `supervisor.py:578` | Scientific/artifact validation level, non-probe action, LLM review enabled | Scientific advisory | `_scientific_advisory()` local prompt | Code 1.5k, projected stdout 3k, stderr 1.2k, task context 1.5k | Advisory fields merged under deterministic constraints; cannot route |
| S2 | `supervisor.py:586` | S1 output is not valid JSON | Advisory schema repair | Original prompt + schema error | Two HumanMessages | One repair only; deterministic validation remains authoritative |
| P1 | `bio_agents.py:10255` through `_ainvoke_llm()` | New plan generation | Planner | Planner prompt assembly | TaskSpec, selected skills/runtime audit, formal inputs and bounded context | Creates plan; not itself an execution-error diagnosis |
| P2 | `bio_agents.py:10316` through `_ainvoke_llm()` | Explicit PLAN-owned patch or invalid plan format | Planner local/schema repair | Existing plan context + structured patch/validation feedback | At most two Planner attempts in the local repair loop | Changes only the requested plan scope |
| I1 | `agent_core/installer.py:507` | Installer work order, failed install observation or registration failure | Installer action/recovery | Installer system prompt + work order + local RAG + step-local messages | Max 6 messages / 12k local history; stdout/stderr observation clipped to 500 chars each | Generates Markdown command or registration tag inside independent Installer graph |

## Ancillary LLM Calls

| File/function | Purpose | Why it is not a recovery decision |
|---|---|---|
| `memory_manager.py:420` | Compress graph memory at >=85% usage | Summarizes messages; does not choose recovery |
| `memory/wiki_memory.py:753,806` | Analyze and patch wiki memory | Knowledge maintenance, not current-step correction |
| `agent_core/router.py:143,178` | Triage/retrieval routing | Runs before task execution and does not diagnose an attempt |

## Dead Or Inactive Correction Calls

| File/function | Status | Risk if reactivated |
|---|---|---|
| `agent_core/middleware.py:884,926` | Inside `_legacy_error_recovery_middleware`; not wrapped into the current graph | Duplicates diagnosis, relies on observation text, applies repairs directly and injects large unstructured messages |
| `bio_agents.py:2426` `_adjudicate_post_review_recovery()` | Current callers are inside legacy Supervisor/final-audit implementations | Would duplicate active policy/judge work after review |
| `execution/monitoring.py:134` `ErrorRecoverySystem._llm_analyze_error()` | No current constructor/caller found | Creates another independent root-cause model |

## Maximum Calls Per Failure Episode

The exact maximum depends on the route:

| Failure route | Maximum correction-related calls |
|---|---:|
| Deterministic runtime/input/OOM/permission diagnosis + valid Generate repair | 1 Generate |
| Same route + one action-format repair | 2 Generate |
| Ambiguous execution evidence | 2 RecoveryJudge + up to 2 Generate = 4 |
| Confirmed plan structural defect | 2 RecoveryJudge + up to 2 Planner repair + up to 2 Generate = 6 |
| rc=0 artifact/final-gate issue with scientific advisory | up to 2 Supervisor advisory + up to 2 Generate = 4 |
| Installer delegated repair | Up to 10 Installer generation turns, in a separate graph |

The main recovery chain does not intentionally run all categories for every
failure. A deterministic failure should skip RecoveryJudge. Planner must be
entered only for a PLAN-owned structural defect.

The table excludes one optional memory-compaction call at high token usage and
the original action call that caused the failure. Including the original
Generate action adds one to an episode count.

## Current Call-Site Concern

The active middleware invokes RecoveryJudge directly with
`llm.ainvoke()` (`agent_core/middleware.py:1718,1730`) rather than the central
`_ainvoke_llm()` wrapper. It therefore does not automatically inherit the
central timeout, request-character logging and task-stage instrumentation used
by Generate/Planner. Installer has the same independence.

