# Node Prompt Input Matrix

Date: 2026-08-02

## Final LLM call inventory

| Caller | Final call | Input source | Main messages | Independent thread |
| --- | --- | --- | --- | --- |
| Task-mode router | `agent_core/router.py:143` | One generated router prompt containing user query | NO direct list | NO |
| Direct chat | `bio_agents.py:8975` via `_ainvoke_llm()` | Dynamic System prompt plus full main messages | YES | NO |
| Consultant | `bio_agents.py:9409` | Dynamic Consultant System prompt plus bounded local messages | PARTIAL | NO |
| Planner | `bio_agents.py:10309` | One assembled HumanMessage | NO raw list | NO |
| Planner format repair | `bio_agents.py:10398` | One repair HumanMessage | NO | NO |
| Generate | `bio_agents.py:11254` | Current-step System prompt plus bounded local messages | PARTIAL | NO |
| RecoveryJudge | `agent_core/middleware.py:1777` | One bounded RecoveryContext prompt | NO | NO |
| Supervisor reviewer | `supervisor.py:578`, repair `586` | Code/evidence/task-context review prompt | NO | NO |
| Installer | `agent_core/installer.py:507` | Installer System prompt, optional RAG, own bounded messages | NO | YES |
| Memory summarizer | `memory_manager.py:420` | Archived-message summary prompt | Archived main subset | NO |
| Wiki analysis/patch | `memory/wiki_memory.py:753`, `806` | Wiki-specific prompts | NO | NO |

`agent_core/middleware.py:895` and `937` are inside `_legacy_error_recovery_middleware`; the active Execute wrapper is the structured middleware used at `bio_agents.py:13049-13054`. `execution/monitoring.py:139` belongs to a deprecated standalone `ErrorRecoverySystem` and is not instantiated by the active graph. These are not counted as active task calls.

## Prompt composition

### Triage

The router receives a single query formed by concatenating all `HumanMessage` content (`bio_agents.py:8848-8873`). It does not receive AI/System messages, TaskSpec, PlanTracker, or compression summaries. Because internal observations also use HumanMessage, the query can include non-user content on resume.

### Consultant

System prompt components (`bio_agents.py:9362-9394`):

- user preferences;
- reduced effective TaskSpec, max 2,000 chars;
- formal input resolution audit;
- broad SessionArchive memory retrieval context;
- candidate workflow/skill catalog;
- runtime evidence;
- RAG/action history;
- response protocol.

Additional messages come from `_step_local_messages()` (`3839-3933`) with a six-message/12k bound. The raw original goal is removed if duplicated by TaskSpec (`9347-9360`). Generic Memory Compression SystemMessages are not selected.

### Planner

Planner does not pass `state.messages`. It assembles one prompt from the TaskSpec goal/rendering, Consultant `requirements_summary`, selected skill catalog and execution cards, runtime audit, workflow state, latest RAG, and compact wiki/archive context (`bio_agents.py:10041-10218`). It no longer relies on `messages[0]` for the user goal.

The separate plan-format repair call is materially narrower (`bio_agents.py:10365-10403`). Runtime capture showed it could omit all seven tested user facts, because the repair prompt centered on the invalid plan and validation errors rather than the effective TaskSpec.

### Generate

Generate receives a stable protocol prompt plus ephemeral current-step context built by the controller (`bio_agents.py:11024-11226`): current Plan step, active skill documents/cards, TaskSpec projection, formal input/renderer information, recent execution/Supervisor/recovery evidence, and optional on-demand SessionArchive context. `_generate_visible_messages()` filters internal Consultant loads and then applies the six-message/12k selector (`3936-3962`). In compact projection mode it receives no raw messages.

Generate does not see full main history and does not call `MemoryManager.search_archives()`. Its archive query is an on-demand `SessionArchive` project search, activated for recovery, missing input, or evidence lookup (`bio_agents.py:1475-1564`, `11036-11049`).

### RecoveryJudge

The active middleware creates `RecoveryContext` with current task/step, ExecutionEvidence, action/runtime/manifest contracts, validations, artifact state, bounded logs, and recent attempts (`agent_core/diagnostics.py:1059-1238`). `prompt_view()` enforces a bounded projection (`131-370`). It is a stateless call, cached by recovery context ID, not a conversational subagent.

The task summary currently includes the original goal and selected legacy fields, but not the complete `user_directives`, requirements, expected outputs, or confirmed decisions. Recovery sees later user facts only if they were copied into the current step, action contract, or evidence.

### Supervisor and CompletionGate

Supervisor prefers `execution_evidence`; its legacy fallback scans messages only for recent observation/code (`supervisor.py:1092-1161`). The optional LLM reviewer receives a compact task/step/action context, not user dialogue (`supervisor.py:1348-1363`). It writes a compact validation SystemMessage (`1465-1475`).

CompletionGate is deterministic. It reads TaskSpec, PlanTracker, artifact contracts/manifests and validation state. It has no LLM prompt and does not inspect full message history. Controller code may append gate feedback and advance/block steps.

## Runtime capture

A fake LLM captured the final prompts without API or bioinformatics execution. The test used the requested seven-turn plant annotation conversation and current node implementations.

| Call | Prompt characters | Species | VCF | FASTA | GTF | `gtf22` | Prohibition | Latest output |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| Consultant | 12,854 | NO | YES | YES | YES | YES | YES | YES |
| Planner initial | 22,555 | YES | YES | YES | YES | YES | YES | YES |
| Planner repair | 1,565 | NO | NO | NO | NO | NO | NO | NO |
| Generate | 37,807 | YES | YES | YES | YES | YES | YES | YES |
| RecoveryJudge | 6,554 | NO | YES | YES | YES | NO | NO | YES |

This capture proves prompt visibility for the constructed state, not universal semantic understanding. The paths in Consultant were restored by the formal input audit despite `TaskSpec.inputs` being empty; the species was not.

## Context duplication and conflict risk

- The same fact can appear in original goal, TaskSpec directives, Consultant summary, Plan step, formal audit, and SessionArchive retrieval.
- Source precedence exists for structured TaskSpec bindings, but natural-language prompt sections are not globally reconciled before LLM calls.
- User feedback and internal observations can both be HumanMessage.
- Memory compression summaries can coexist with recent original messages; managed selectors usually omit the summary, while direct chat sees both.
- Recovery emits one structured handoff plus deterministic state fields; older recovery SystemMessages can remain in main state, but current Generate is instructed by structured recovery and bounded selectors rather than the full backlog.
