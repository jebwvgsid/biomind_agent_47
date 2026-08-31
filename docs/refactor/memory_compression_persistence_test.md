# Memory Compression Persistence Test

Date: 2026-08-02

## Purpose

Verify by execution, not variable names, that `MemoryManager.maybe_compress()` replaces LangGraph state/checkpoint messages, archives old messages, and exposes the expected retrieval limitations.

No API, benchmark data, or bioinformatics command was used. Temporary files were placed under `/tmp`.

## Test 1: Compression and archive

The probe instantiated `MemoryManager` with a deliberately low token threshold and a fake summarizer, then supplied synthetic messages containing the seven-turn facts. It checked:

1. `maybe_compress()` reports compression;
2. old messages are written to JSONL;
3. a `[Memory Compression #N]` SystemMessage is inserted;
4. recent original messages remain;
5. the summary is indexed;
6. representative searches return or fail predictably.

Observed result:

```text
compressed: true
archived messages: 5
compacted messages: 3
compression summary present: true
raw archive records: 5
```

The fake exact summary retained species, VCF/FASTA/GTF paths, `gtf22`, the prohibition, thread count, and latest output path. This isolates persistence/retrieval behavior from summarizer quality.

## Test 2: LangGraph reducer/checkpoint semantics

A minimal `StateGraph` used a `messages: Annotated[list, add_messages]` field and a `MemorySaver`. Its node returned the same update as `_profile_node()`:

```python
{
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *compacted_messages,
    ]
}
```

After graph execution, the stored checkpoint was read using the same thread ID.

Observed result:

```text
checkpoint message count == compacted message count: true
old first payload still present: false
compression summary present: true
```

Conclusion: old messages are genuinely removed from the active checkpoint on the successful path. The compact list is not merely used for one LLM request.

## Test 3: Seven-turn TaskSpec and prompt visibility

The conversation was simulated by calling `create_task_spec()` for turn 1 and `apply_user_feedback()` for turns 2-7.

Observed TaskSpec state:

```text
inputs: []
requirements: database_source=custom_genome only
expected_outputs: []
user_directives: all seven facts retained
confirmed_decisions: turns 2-7 retained
```

`render_task_spec(max_chars=3500)` contained every tested fact. This demonstrates that later answers are retained as directives but are not re-extracted into `InputBinding`, `RequirementBinding`, or `OutputBinding` by `apply_user_feedback()`.

## Test 4: Final prompt capture

The current `BioAnalystAgent` was instantiated with temporary paths and a fake LLM. Compiled Consultant, Planner, and Generate node callables were invoked, and a RecoveryJudge prompt was built from the same state. No external LLM was called.

Observed prompt sizes and visibility are recorded in `node_prompt_input_matrix.md`. The important outcomes were:

- Consultant lost the early species correction but recovered paths through formal input discovery.
- Initial Planner and Generate saw all seven facts.
- Planner format repair saw none of the seven facts.
- RecoveryJudge saw paths/output only because the test step carried them; it lost species, `gtf22`, and the prohibition.

The generated capture was stored at `/tmp/biomind_prompt_capture.json` during the audit. It is diagnostic output, not a repository artifact.

## Compression behavior under failure/replay

Normal successful sequence:

```text
state.messages
→ Consultant/Generate result
→ add_messages in `_profile_node`
→ maybe_compress
→ raw JSONL + memory index
→ RemoveMessage(REMOVE_ALL_MESSAGES) + compact messages
→ LangGraph reducer
→ MemorySaver checkpoint
```

The same batch is not re-compressed after a successful checkpoint because it no longer exists in state. There is still a replay window: archive and index are filesystem side effects completed before checkpoint commit. A crash in that window may create a duplicate archive/index chunk on retry. The implementation has no compression event ID or transaction connecting those stores.

## Reproducibility notes

The temporary probes used only:

- current `MemoryManager`;
- current `TaskSpec` functions;
- LangGraph `add_messages`, `RemoveMessage`, and `MemorySaver`;
- a fake async LLM returning deterministic text;
- temporary directories.

No production source file was modified by the probes. Importing the application may append ordinary ignored log files, but it did not change task data or environment configuration.

## Verdict

| Question | Result |
| --- | --- |
| Does compression trigger at the configured threshold? | YES |
| Is the compact list written to checkpoint? | YES |
| Are archived messages removed from active state? | YES |
| Are raw archives durable files? | YES |
| Is raw archive automatically loaded later? | NO |
| Do managed nodes consistently consume compression summaries? | NO |
| Can crash/replay duplicate archive side effects? | YES, possible |

