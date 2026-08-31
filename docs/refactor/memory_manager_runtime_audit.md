# MemoryManager Runtime Audit

Date: 2026-08-02

## Two different memory systems

The current code has two separate mechanisms:

1. `MemoryManager` (`memory_manager.py:109`) compacts the main LangGraph message list, writes raw JSONL archives, and stores searchable summary chunks in `memory_index.json`.
2. `SessionArchive` (`memory/session_archive.py:135`) records structured task events in SQLite/FTS and supplies the Consultant/Generate archive retrieval shown in prompts.

They are not connected. Active managed nodes retrieve from `SessionArchive`; no production caller of `MemoryManager.search_archives()` or `load_raw_archive()` was found.

## Threshold and counted data

| Setting | Current value | Evidence |
| --- | ---: | --- |
| `max_tokens` | 200,000 | `memory_manager.py:125-140` |
| `compress_threshold` | 0.85 | `memory_manager.py:130` |
| Actual trigger | 170,000 estimated message tokens | `int(max_tokens * threshold)`, `159-163` |
| `keep_recent_tokens` | 40,000 | `memory_manager.py:131` |

The threshold counts only the supplied `state.messages` using `_msg_tokens()`. Dynamic Consultant/Planner/Generate system prompts, skill documents, plan state, RAG, runtime audit, RecoveryContext, and validation evidence are not counted unless a representation of them has already been appended to main messages. Prompt size can therefore exceed the apparent memory meter without triggering compression.

## Active invocation

`BioAnalystAgent._profile_node()` invokes `maybe_compress()` only after the Consultant and Generate nodes (`bio_agents.py:8659-8687`). Planner, RecoveryJudge, Supervisor, Installer, and final audit do not call it directly.

When compression occurs, the node update contains:

```python
[
    RemoveMessage(id=REMOVE_ALL_MESSAGES),
    *compacted_messages,
]
```

This is important: compression is not merely a local prompt variable. LangGraph's `add_messages` reducer removes the old list and checkpoints the compact list.

## Split algorithm

`_split_messages()` is at `memory_manager.py:377-414`.

It always retains:

- the first stable SystemMessage;
- every previous `[Memory Compression ...]` SystemMessage;
- every protected plan SystemMessage;
- newest non-System messages whose whole-message token counts fit under 40,000.

Other old SystemMessages are archived. Therefore the system does **not** retain every SystemMessage forever. However, all prior compression summaries and protected plan messages are retained indefinitely and can accumulate.

Recent history is selected by message token count, not conversation turn. It does not split a message, but it can retain one side of a logical action/observation pair if the boundary falls between them.

## Summary construction and loss risk

The summary prompt (`memory_manager.py:434-485`) explicitly asks to preserve goals, paths, dataset/sample IDs, commands, parameters, environments, outputs, errors, status, and next action. Before the LLM receives an archived message, `_message_summary_excerpt()` clips it (`360-375`):

- observations/execute: 2,500 characters;
- HumanMessage: 1,500;
- AIMessage: 900;
- other messages: 1,200.

Clipping keeps the beginning and end, but long path lists, scripts, and middle log sections can be lost before summarization. The fallback after summarization failure is only the first 200 characters of the last five archived messages (`423-426`).

## Persistence and restart behavior

| Store | Durable across process restart | Isolation |
| --- | --- | --- |
| Main graph `MemorySaver` | NO | thread ID while process lives |
| `memory_index.json` | YES | shared file; chunks carry task ID |
| `archive_<task>_<timestamp>.jsonl` | YES | filename/task metadata |
| `compression_audit.jsonl` | YES | records task ID |
| PlanTracker files | YES | task-scoped |
| SessionArchive SQLite | YES | task/project fields and filters |

`MemoryManager.search_archives(query, top_k=3)` has no task or project argument (`memory_manager.py:223-255`). It scores all loaded chunks, so cross-task summary retrieval is possible if this function is activated later. Raw archive filenames use second-resolution timestamps (`537-541`); two compressions for the same task in the same second can target the same filename.

There is no transaction spanning raw archive write, memory index write, and LangGraph checkpoint update. On the normal successful path old messages are removed and are not compressed again. A crash/replay after archive side effects but before checkpoint persistence can archive the same logical batch again.

## Search behavior

`MemoryManager.search_archives()` tokenizes with `query.lower().split()`, compares those terms with ASCII-only keywords, and checks literal substring hits. `_extract_keywords()` only captures ASCII words (`memory_manager.py:517-535`). It has no task filter, time filter, stemming, Chinese segmentation, or path-specific normalization.

The runtime probe with an exact synthetic summary produced:

| Query | Hit |
| --- | --- |
| `Arachis hypogaea` | YES |
| `花生` | YES by literal substring |
| `genome.gtf` | YES |
| `--gtf22` | NO when summary only contained `gtf22` |
| `/data/project/sample.vcf` | NO when exact path differed |
| `线程数 32` | YES by literal terms |
| `不允许猜物种数据库` | YES by exact literal text |

This is opportunistic, not reliable semantic retrieval. `SessionArchive` is better for Unicode/path terms because it uses FTS and `query_terms()` (`memory/utils.py:82-90`), but active Consultant and Generate archive calls intentionally use project-level search with `task_id=None`, so relevant events from another task in the same project can be returned.

## Actual consumers

| Module | `maybe_compress` | `MemoryManager.search_archives` | `load_raw_archive` | Uses compression summary | Uses recent raw messages |
| --- | --- | --- | --- | --- | --- |
| Consultant | Via profile wrapper | NO | NO | NO in normal local selector | PARTIAL |
| Planner | NO | NO | NO | NO | NO raw stream |
| Generate | Via profile wrapper | NO | NO | NO in normal local selector | PARTIAL |
| RecoveryJudge | NO | NO | NO | NO | NO |
| Supervisor | NO | NO | NO | NO | Legacy observation fallback only |
| Installer | NO | NO | NO | NO | Own bounded messages |
| Direct chat | NO | NO | NO | YES because full main messages are passed | YES |
| Final response/audit | NO | NO | NO | NO direct prompt consumer | NO |

Existence of example calls at `memory_manager.py:116-119` and test calls under `tests/` does not constitute an active production consumer.

## Verdict

Message compaction is real and its result is written back to the live checkpoint. The long-term archive retrieval half of `MemoryManager` is not integrated into the managed-task prompt chain. Managed nodes instead depend on TaskSpec, PlanTracker, bounded recent messages, and the separate SessionArchive. Consequently, a Memory Compression summary can exist in state yet be invisible to Consultant, Planner, Generate, RecoveryJudge, and Supervisor.

