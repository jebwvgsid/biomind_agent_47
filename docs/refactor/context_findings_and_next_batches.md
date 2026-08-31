# Context Findings and Next Batches

Date: 2026-08-02

This document proposes follow-up work only. No proposal was implemented during this audit.

## Prioritized findings

### P0: Later user corrections are not normalized into TaskSpec bindings

**Current state:** initial requests use `extract_request_contracts()`, while `apply_user_feedback()` records later answers as directive/decision text.

**Impact:** corrected paths, output directories, parameters, species, and prohibitions can be visible to one node and absent from another; old structured values may coexist with newer text.

**Evidence:** `agent_core/task_spec.py:267-398`, `878-924`; seven-turn runtime TaskSpec had empty inputs and outputs despite explicit later paths.

**Suggested location:** reuse extraction/merge and source-priority functions in `agent_core/task_spec.py`; call from the existing feedback update path in `bio_agents.py:9988-10019`.

**Suggested batch:** Context Batch A.

### P0: Node projections do not share one effective user-facts view

**Current state:** Consultant uses a 2,000-char render, Planner a 3,500-char render, Generate a custom projection, Planner repair a separate prompt, and RecoveryContext another task summary.

**Impact:** species disappeared from Consultant; all facts disappeared from Planner repair; `gtf22` and prohibitions disappeared from RecoveryJudge.

**Evidence:** `bio_agents.py:2174-2198`, `9362-9375`, `10041-10218`, `10365-10403`; `agent_core/diagnostics.py:1059-1238`; prompt-capture test.

**Suggested location:** add one compact projection function to existing TaskSpec module and reuse it from those existing builders. Do not add another context manager.

**Suggested batch:** Context Batch B.

### P1: MemoryManager archive retrieval is disconnected and unscoped

**Current state:** compression/checkpoint replacement works, but managed nodes do not consume its summary/search API. `search_archives()` has no task/project filter, and no active caller reloads raw archive.

**Impact:** durable raw history exists but cannot restore exact facts; activating current search unchanged would introduce cross-task leakage.

**Evidence:** `memory_manager.py:152-273`; only example/tests call search/load. Active retrieval uses `SessionArchive` (`bio_agents.py:1213`, `1912-1964`).

**Suggested location:** either formally limit MemoryManager to compaction/audit, or add task-scoped read APIs and use them only as low-priority historical evidence. Reuse SessionArchive where possible.

**Suggested batch:** Memory Batch C.

### P1: Compression side effects are not replay-idempotent

**Current state:** archive/index writes precede LangGraph checkpoint commit and use second-resolution names.

**Impact:** crash/replay can duplicate or overwrite archive side effects; old compression summaries accumulate forever.

**Evidence:** `memory_manager.py:174-205`, `537-572`.

**Suggested batch:** Memory Batch D.

## Option assessment

| Option | Problem solved | Files/functions to reuse | Migration/checkpoint risk | Tests | Batch |
| --- | --- | --- | --- | --- | --- |
| A. Keep main messages; harden compaction persistence | Replay duplication and summary accumulation | `MemoryManager.maybe_compress`, `_write_archive`, memory audit; `_profile_node` | Low-medium; preserve summary message compatibility | crash/replay idempotency, same-second compression, old checkpoint | D |
| B. Consultant reads task user turns and emits structured handoff | Reliable multi-turn paths/species/parameters | `extract_request_contracts`, TaskSpec merge/source priority, `apply_user_feedback` | Medium; migrate text directives without overwriting explicit values | latest-wins, user-explicit precedence, seven-turn capture | A |
| C. Planner reads TaskSpec + handoff, not positional messages | Stable initial and repair plans | `render_task_spec`, Planner prompt builder/repair validator | Low; no checkpoint shape required | repair prompt preserves facts, compressed-state planner | B |
| D. Generate reads CurrentStepContext + effective constraints | Prevent full-history pollution while retaining current facts | `_generate_task_spec_projection`, current-step context builder | Low | compact/full projection equivalence, conflicting correction | B |
| E. Recovery uses incident context and DiagnosisHandoff | Isolated error diagnosis with exact task constraints | `RecoveryContext`, `build_recovery_context`, structured middleware handoff | Low-medium; add optional fields with defaults | gtf22/prohibition/species visibility, old recovery state | B |
| F. Summary is long-term evidence; TaskSpec owns exact facts | Clear memory authority | Existing TaskSpec, SessionArchive, MemoryManager | Medium policy migration, low schema risk if additive | exact path survives compression, archive never overrides TaskSpec | C |

## Recommended sequence

1. **Batch A:** re-extract and revision-merge later user feedback into the existing TaskSpec. Record migration source; never overwrite a newer `user_explicit` value with inference.
2. **Batch B:** introduce one bounded `effective_user_facts` projection in the TaskSpec module and use it in Consultant, Planner initial/repair, Generate, and RecoveryContext.
3. **Batch C:** define memory authority. Keep exact current facts in TaskSpec; treat SessionArchive/summary as lower-priority retrieval evidence. Do not activate unfiltered `MemoryManager.search_archives()`.
4. **Batch D:** make compression archive writes idempotent and bound retained compression summaries.

## Required regression tests

- Seven-turn conversation before and after compression produces identical effective TaskSpec facts.
- Latest explicit output path supersedes an earlier explicit path in every node projection.
- Planner format repair and RecoveryJudge receive the same immutable constraints as their initial calls.
- Skill defaults and archive summaries cannot override explicit user constraints.
- Generic SystemMessage content is not interpreted as a user directive.
- Task-scoped retrieval cannot return another task unless explicitly project-scoped and labeled.
- Compression replay produces one archive event and one active summary.
- Old checkpoints missing new optional fields load safely.

## Direct answers

1. **Which modules can see past user turns?** Direct chat sees the full main stream; Triage aggregates all HumanMessages; Consultant and Generate see bounded recent messages plus TaskSpec; Planner sees structured projections, not raw turns; Recovery/Supervisor/Gate only see facts projected into structured task/step/evidence state.
2. **How much?** Consultant/Generate local selector: at most six messages and about 12k characters; Consultant TaskSpec: 2,000 chars; Planner TaskSpec: 3,500; direct chat: full messages; Recovery: bounded RecoveryContext up to its prompt budget; Installer: only its own bounded work-order history.
3. **Can Consultant extract paths, species and parameters from past turns?** PARTIAL. Paths can be restored by formal discovery, but the runtime capture lost the earlier species correction.
4. **Does Planner only see the first user message?** NO. Initial Planner uses TaskSpec and Consultant summary, not `messages[0]`; its repair call can nevertheless omit later facts.
5. **Does Generate see full main messages?** NO. It receives current-step/TaskSpec context and a bounded filtered subset, or no raw messages in compact mode.
6. **Does RecoveryJudge have an independent context?** YES as a bounded stateless `RecoveryContext`; NO as an independent thread/checkpointer.
7. **Is Installer an independent subagent?** YES. It has its own graph, state, messages, MemorySaver and derived thread ID.
8. **Does summary MemoryManager really work?** PARTIAL. Compression, archive writing and checkpoint replacement work; managed-node summary retrieval does not.
9. **Actual compression threshold?** About 170,000 estimated tokens (`200,000 * 0.85`).
10. **Are compressed messages written to checkpoint?** YES, via `RemoveMessage(REMOVE_ALL_MESSAGES)` and replacement messages.
11. **Where is the compression summary used?** Direct chat sees it through full messages. Normal Consultant/Planner/Generate/Recovery/Supervisor managed prompts do not deliberately consume it.
12. **Is raw MemoryManager archive reloaded by active modules?** NO active production caller was found.
13. **Can archive retrieval cross tasks?** YES/PARTIAL. MemoryManager search is globally unfiltered; active SessionArchive retrieval is deliberately project-scoped with `task_id=None` in Consultant/Generate memory lookups.
14. **Are all SystemMessages retained forever?** NO. Old ordinary SystemMessages are archived, but compression summaries and protected plan messages are always retained.
15. **Top three fixes?** Structure later user feedback; unify effective-user-fact projection across node prompts; define scoped memory authority and avoid unfiltered archive retrieval.

## Key conclusion table

| Question | Conclusion | Evidence |
| --- | --- | --- |
| Consultant sees complete task conversation | PARTIAL | `bio_agents.py:3839-3933`, `9347-9413`; prompt capture |
| Planner sees later additions | PARTIAL | `bio_agents.py:10041-10313`; repair capture loss |
| Generate sees later additions | PARTIAL | `bio_agents.py:2174-2198`, `11207-11255`; prompt capture |
| RecoveryJudge sees user history | PARTIAL | `agent_core/diagnostics.py:1059-1238`; prompt capture |
| Supervisor sees user history | NO in normal structured path | `supervisor.py:1092-1161`, `1348-1363` |
| Installer has independent context | YES | `agent_core/installer.py:38-48`, `409-740`; `bio_agents.py:14454` |
| Memory compresses near 200k | 170,000 token trigger | `memory_manager.py:129-163` |
| Recent raw text retained | Up to 40,000 tokens, whole messages | `memory_manager.py:131`, `399-410` |
| Compression persisted to checkpoint | YES | `bio_agents.py:8659-8687`; reducer runtime test |
| Old raw messages removed from state | YES on successful checkpoint | reducer runtime test |
| Same batch can be compressed twice | NO normally; possible on crash/replay | side-effect/checkpoint ordering |
| All SystemMessages retained | NO | `memory_manager.py:377-414` |
| Archive isolated by task | PARTIAL | task-tagged files; unfiltered search |
| Chinese/path archive search | PARTIAL | literal runtime queries; ASCII keyword extractor |
| `maybe_compress` callers | Consultant and Generate profile wrappers | `bio_agents.py:8659-8668` |
| Active `MemoryManager.search_archives` callers | None | repository `rg` audit |
| Active raw archive readers | None | repository `rg` audit |

