# Conversation Context Visibility Audit

Date: 2026-08-02

Scope: current dirty worktree. This is an audit only; no production behavior was changed.

## Executive conclusion

BioMind has one shared LangGraph `AgentState.messages` stream for the main graph, but most managed-task nodes do **not** send that full stream to their LLM. They build bounded, node-specific projections. This reduces prompt growth, but it also means that storing a fact in `messages` does not guarantee that Consultant, Planner, Generate, Supervisor, or RecoveryJudge can see it.

The main graph is created in `bio_agents.py:8843` and compiled with one in-memory `MemorySaver` at `bio_agents.py:14851`. Its nodes are registered at `bio_agents.py:14664-14674`. Main-task thread IDs use the task ID (`bio_agents.py:14966`, `15031`). The Installer is the only actual subgraph with its own `InstallerState`, `MemorySaver`, messages, and derived thread ID (`agent_core/installer.py:38-48`, `409-740`; `bio_agents.py:14454`).

## Current graph and deterministic modules

| Component | Kind | Context ownership | LLM |
| --- | --- | --- | --- |
| Triage | Main graph node | Main state | Router LLM |
| Direct chat | Main graph node | Main state | Main LLM |
| Consultant | Main graph node | Main state plus dynamic retrieval | Main LLM |
| Ask user | Main graph HITL node | Main state | No |
| Planner | Main graph node | Main state, but prompt is assembled from structured fields | Main LLM |
| Plan review | Main graph HITL node | Main state | No |
| Generate | Main graph node | Main state plus current-step projection | Main LLM |
| Execute | Main graph deterministic node | `pending_action`, plan and evidence | No |
| Supervisor | Main graph adapter plus `SupervisorNode` | Structured execution evidence | Optional reviewer LLM |
| Supervisor final audit | Main graph node | Plan/artifact/gate state | Optional reviewer LLM |
| CompletionGate | Deterministic service called by controller | Plan, artifacts, TaskSpec | No |
| RecoveryJudge | Active Execute middleware helper | Bounded `RecoveryContext` | Conditional LLM |
| Renderer/Manifest runner | Deterministic controller service | Manifest and plan state | No |
| Installer | Independent LangGraph subgraph | Independent `InstallerState.messages` | Installer LLM |
| Controller/routers | Python control logic in `bio_agents.py` | Main state and PlanTracker | No |

## Visibility matrix

Legend: YES = directly available; PARTIAL = bounded subset; INDIRECT = only through another structured/summary field; NO = not supplied in the normal active path.

| Module | Original request | Later user turns | Recent messages | Consultant result | MemoryManager summary | Raw MemoryManager archive | Plan | Current step |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Triage | YES | YES, all `HumanMessage` content | N/A | NO | NO | NO | NO | NO |
| Direct chat | YES | YES, full main messages | YES | INDIRECT | YES | NO | NO | NO |
| Consultant | YES via TaskSpec/system prompt | PARTIAL, latest local turn plus TaskSpec directives | PARTIAL, at most 6/12k | N/A | NO in normal selector | NO | NO | NO |
| Planner initial | YES via TaskSpec | PARTIAL/INDIRECT via TaskSpec and summary | NO raw stream | YES | NO | NO | Existing workflow state only | NO |
| Planner format repair | PARTIAL | NO in runtime reproduction | NO | PARTIAL, failed plan/validation only | NO | NO | Candidate plan | NO |
| Generate | YES via TaskSpec projection | PARTIAL via TaskSpec and local selector | PARTIAL, at most 6/12k unless compact mode | INDIRECT | NO in normal selector | NO | YES | YES |
| Execute | NO semantic prompt | NO | Legacy fallback only | NO | NO | NO | YES | YES |
| RecoveryJudge | PARTIAL via task summary | PARTIAL only if projected into TaskSpec/step | NO raw messages | NO direct summary | NO | NO | Current step and attempts | YES |
| Supervisor | NO in structured path | NO | Legacy observation fallback only | NO | NO | NO | PARTIAL contract | YES |
| CompletionGate | INDIRECT via TaskSpec/contracts | INDIRECT via structured state | NO | NO | NO | NO | YES | YES |
| Installer | PARTIAL work order only | NO | Own recent messages | Skill/runtime handover only | NO | NO | NO | Installation work item |

Evidence: Triage aggregates all human content at `bio_agents.py:8848-8873`; direct chat passes `[SystemMessage] + messages` at `8919-8977`; Consultant builds bounded local messages through `_step_local_messages()` at `3839-3933` and calls the LLM at `9409`; Planner sends one assembled `HumanMessage` at `10309`; Generate calls with a current-step System prompt plus bounded local messages at `11207-11255`; RecoveryContext is built at `agent_core/diagnostics.py:1059`; Supervisor prefers structured evidence in `supervisor.py:1092-1161`, with the call path starting at `1229`.

## Consultant details

Consultant reads the original goal from TaskSpec, up to six later directives in its consultation context, a reduced 2,000-character rendered TaskSpec, a formal input audit, candidate skills/workflows/runtime evidence, and broad project-level SessionArchive retrieval (`bio_agents.py:9003-9409`). Its raw-message selector includes the first human message, latest `<human_answer>`, latest observation, latest control/recovery feedback, and latest action, but then removes a raw human message equal to the original goal (`bio_agents.py:9347-9360`).

This is enough to recover many paths through the formal resolver, but it is not equivalent to complete multi-turn visibility. In the seven-turn runtime capture, Consultant saw all three paths, `gtf22`, the prohibition, and latest output directory, but did not see `Arachis hypogaea`; the reduced TaskSpec had dropped that earlier directive.

Consultant writes a natural-language `requirements_summary`, selected skill fields, runtime audit, memory packet/wiki context, and optionally workflow state (`bio_agents.py:9924-9944`). User answers are stored as `<human_answer>` and passed to `apply_user_feedback()` (`bio_agents.py:9988-10019`). The answer text is appended to TaskSpec directives/decisions, but is not re-run through the initial structured path extractor.

Consultant may run again after a user answer, plan rejection, or resume. Existing TaskSpec fields are retained, but the node rebuilds its prompt and summary. Its internal actions are bounded to actions after the latest user message, so the prompt does not grow without limit; internal messages still remain in main state until compression.

## Message type pollution

Main `messages` contains more than conversation turns:

| Type | Real user content | Internal uses |
| --- | --- | --- |
| `HumanMessage` | Initial request, user answers, plan feedback | Execute observations, RAG result wrappers, runtime/contract failures |
| `AIMessage` | Direct response/final solution | Consultant actions, plan notices, installer observations |
| `SystemMessage` | None | Search results, control events, Supervisor feedback, recovery handoff, final audit, memory summaries |
| `RemoveMessage` | No | Reducer command used to replace the compressed state |
| `ToolMessage` | Not found in the active BioMind graph path | None observed |

Consultant does not blindly treat every SystemMessage as a user request: `_step_local_messages()` selects specific tagged control/evidence content. Generate similarly filters Consultant load internals. Nevertheless, `HumanMessage` is semantically overloaded for both user speech and observations, so message class alone cannot establish authorship.

## Independent contexts

RecoveryJudge is context-isolated but not a LangGraph subagent: it receives one stateless, bounded `RecoveryContext.prompt_view()` and has no independent checkpointer/thread. Its structured result is appended as a compact `<structured_recovery>` SystemMessage by the active middleware (`agent_core/middleware.py:1616-1942`).

Installer is a true subgraph. The main graph passes an installation work order and selected skill/runtime contract. Installer then uses its own messages and a thread ID of `<task>_installer_<tool>` (`bio_agents.py:14454`). Only its final result/evidence returns to the main state. It has no `MemoryManager` 200k compaction; its own local selector bounds recent installer evidence (`agent_core/installer.py:118-183`). Its `MemorySaver` is process memory, so restart persistence is not durable by itself.

