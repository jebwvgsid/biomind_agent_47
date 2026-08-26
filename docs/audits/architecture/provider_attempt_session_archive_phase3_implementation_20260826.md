# BioMind Provider Attempt / SessionArchive Phase 3 Implementation

Date: 2026-08-26
Status: IMPLEMENTED; deterministic and focused regressions pass; real-model success path remains unverified because the opaque launcher/provider path did not return model chunks.
Audit basis: `docs/audits/architecture/provider_stream_session_archive_boundary_audit_20260825.md`
Incident: `session_1787671230`
Implementation base: `68725d6ceb5cd1082e70610070252abd3ffea690`

## 1. Result and root-cause preservation

The implementation preserves the two independent failure domains identified by the
read-only audit:

- The incident's Generate request failed before Executor. It was a provider/stream
  lifecycle failure, not a snpEff, Java, VCF, GTF, shell, or generated-action
  semantic failure.
- SessionArchive lock waits were a separate latency amplifier. Optional historical
  observation is not canonical task state and must not freeze the workflow.

After this change:

```text
Generate semantic invocation
  -> frozen request/context identity
  -> Provider Attempt #1
  -> per-attempt stream observer and phased watchdogs
  -> cancel + bounded await/drain on timeout
  -> retry once only when cleanup and canonical-state gates pass
  -> completed response OR typed provider terminal failure
```

```text
_observe_session_memory()
  -> bounded payload construction
  -> enqueue_nowait()
  -> workflow continues

background process-local writer
  -> short SQLite busy timeout
  -> bounded retry/backoff
  -> written / coalesced / failed telemetry
```

## 2. Files changed

- `agent_core/provider_attempt.py`
  - Adds ProviderAttempt envelope, record, stream observer, timeout/error taxonomy,
    phased watchdogs, cancellation/drain, attempt fencing, metadata-only telemetry,
    and non-executable partial diagnostics.
- `config/reliability.py`
  - Adds environment-overridable provider phase, cleanup, retry, queue, writer, and
    shutdown bounds without modifying the legacy config source.
- `memory/session_archive_writer.py`
  - Adds a bounded per-process optional-observation queue and background SQLite
    writer with short bounded retries and explicit shutdown accounting.
- `bio_agents.py`
  - Integrates the unified provider boundary, Generate-only controller retry,
    canonical retry guard, typed provider terminal projection, non-blocking archive
    enqueue, explicit `aclose()`, and best-effort atexit cleanup.
- `tests/test_provider_attempt_reliability.py`
  - Adds A1-A15 and a non-Generate compatibility assertion.
- `tests/test_session_archive_observation_writer.py`
  - Adds B1-B9.

## 3. Provider Attempt state model

### Identity

One semantic invocation owns immutable:

- `semantic_invocation_id`
- `node_context_hash`
- `request_hash`
- approved step/binding context and message payload

Each physical dispatch separately owns:

- `attempt_id`, `attempt_ordinal`, and `parent_attempt_id`
- `call_id`
- provider/model and thinking policy
- timeout policy and retry budget
- SDK retry configuration (the SDK-internal count remains unobservable)
- stream timing/count/hash state and cleanup result

Attempt #2 receives the same message objects, request hash, semantic invocation ID,
node-context hash, model, and thinking policy. It receives a new attempt ID and call
ID.

### States

Observed attempt states are:

- `ATTEMPT_STARTED`
- `STREAM_ACTIVE`
- `TIMED_OUT`
- `FAILED_TRANSIENT`
- `FAILED_TERMINAL`
- `OUTCOME_UNKNOWN`
- `COMPLETED`

Cleanup results are:

- `CLEANUP_NOT_REQUIRED`
- `CLEANUP_CONFIRMED`
- `CLEANUP_FAILED`
- `CLEANUP_TIMEOUT`
- `CLEANUP_NOT_OBSERVABLE`

If bounded cleanup is not confirmed, the outward failure becomes
`PROVIDER_OUTCOME_UNKNOWN` and no retry is dispatched.

## 4. Stream implementation choice

**KEPT AINVOKE + CALLBACK**

The installed LangChain callback boundary delivers per-generation token/chunk events
to a callback passed in `RunnableConfig`. A fresh callback is created for every
attempt. Both reasoning chunks and ordinary content chunks refresh
`last_any_chunk_at`; ordinary content remains hidden from the UI.

This is the narrowest compatible implementation because completed responses retain
the existing `ainvoke()` BaseMessage contract. Planner, parser, controller, Recovery,
and Executor do not know whether the provider is streaming.

Observable:

- first/last reasoning, content, and any-chunk timestamps
- reasoning/content/total counts and character counts
- content/reasoning hashes, never bodies in routine telemetry
- finish reason and provider request ID when present in callback/response metadata
- cancellation and attempt completion times

Not observable with this SDK boundary in the real probe:

- exact HTTP-header arrival time
- an explicitly owned stream handle
- provider request ID when DeepSeek/LangChain does not expose it

Those fields remain empty rather than being fabricated.

Relevant LangChain contracts:

- <https://reference.langchain.com/python/langchain-core/callbacks/base/AsyncCallbackHandler/on_llm_new_token>
- <https://reference.langchain.com/python/langchain-core/runnables/config/RunnableConfig>

## 5. Timeout and error taxonomy

Generate uses four distinct concepts:

- SDK connect/header timeout, classified as `PROVIDER_CONNECT_TIMEOUT` when the SDK
  raises typed connect evidence
- no reasoning/content activity before the first-chunk bound:
  `PROVIDER_FIRST_CHUNK_TIMEOUT`
- silence after any reasoning/content activity:
  `PROVIDER_STREAM_IDLE_TIMEOUT`
- active or inactive request exceeds the total attempt deadline:
  `PROVIDER_TOTAL_TIMEOUT`

Other typed failures are:

- `PROVIDER_HTTP_ERROR`
- `PROVIDER_SDK_ERROR`
- `PROVIDER_CANCEL_CLEANUP_FAILED`
- `PROVIDER_OUTCOME_UNKNOWN`
- `PROVIDER_CANONICAL_CONTEXT_CHANGED`

A completed malformed or semantically invalid response is returned to the existing
Generate/parser/controller path. It is not a transport retry.

## 6. Cancellation proof and ownership boundary

On a watchdog timeout the wrapper:

1. fences the per-attempt observer;
2. records `cancel_requested_at`;
3. calls `cancel()` on the adapter-owned `ainvoke` task;
4. waits for that task within the cleanup deadline;
5. consumes its terminal result/exception;
6. records `cancel_completed_at` and `cleanup_result`;
7. permits retry only after `CLEANUP_CONFIRMED`.

Late callbacks on the fenced observer increment only that old observer's
`late_chunk_count`; they cannot modify the new attempt.

Because the selected implementation is `ainvoke + callback`, BioMind does not own an
explicit stream object. Real telemetry therefore truthfully records
`stream_close_supported=false`. The adapter does not infer ownership of unrelated
asyncio tasks and does not cancel graph/runtime tasks it did not create.

## 7. Retry contract

Controller-owned Generate provider attempts are capped at two. The SDK's own
`max_retries` remains separate and is reported separately.

A second dispatch requires all of the following:

- typed retryable provider/transport failure;
- no completed response;
- no parsed or committed generated action;
- no Executor side effect;
- old adapter-owned attempt cleanup confirmed;
- controller retry budget remains;
- state generation and approved current step still match the frozen NodeContext.

A canonical-state change fails closed with
`PROVIDER_CANONICAL_CONTEXT_CHANGED`; it does not dispatch a stale request.

No controller retry was added to Planner, Consultant, Supervisor, or Recovery.
Non-Generate typed SDK/HTTP exceptions retain their former outward exception contract.

## 8. Partial response boundary

If a failed attempt observed chunks,
`partial_response_diagnostic.json` contains metadata only:

- semantic/attempt/request identities
- counts and first/last timestamps
- failure and cleanup state
- bounded hashes and truncation metadata
- `classification=DIAGNOSTIC_ONLY`
- `complete=false`
- `executable=false`

It contains no `pending_action` and is never sent to the action parser, approval, or
Executor.

## 9. Generate terminal projection

Exhausted or non-retryable provider attempts may still end in top-level
`TASK_BLOCKED`, but plan/task evidence retains:

- `error_owner=PROVIDER`
- `failure_stage=GENERATE_PROVIDER`
- typed `error_code`
- semantic invocation and final attempt identity
- up to two attempt summaries, chunk counts, cleanup state, and completion time

This path stops at the provider boundary and does not ask RecoveryJudge to diagnose a
nonexistent completed generated action.

## 10. SessionArchive optional writer

### Queue and enqueue

- Scope: **PER_PROCESS**
- Default capacity: 256
- Node operation: `enqueue_nowait()`
- Full queue: optional event is dropped, counter increments, workflow continues
- Same pending logical identity: event is coalesced
- Routine telemetry contains identities, counts, queue depth, and latency only

Observation identity binds task/session, node/stage, event type, logical event ID,
enqueue sequence, creation time, and content hash, so delayed writes cannot attach to
a later task.

### Background write

Defaults:

- SQLite busy timeout: 0.25 s
- maximum write attempts: 2
- backoff: 0.05 s
- explicit shutdown drain: 2 s

Retries happen only in the background writer. Exhaustion increments
`write_failure_count`; the workflow remains unchanged. Duplicate
`sqlite3.IntegrityError` is treated as coalesced optional observation, not an
uncaught workflow error.

### Lifecycle

The process registry returns one writer per canonical DB path within one process.
`BioAnalystAgent.aclose()` releases it with a bounded drain; `atexit` is best
effort only. Remaining optional events after the deadline are counted as
not-durable/dropped.

This does **not** solve cross-process SQLite contention. Future choices such as
per-job DBs, a dedicated writer service, or a client/server database remain out of
scope.

## 11. Canonical / optional boundary

Only `_observe_session_memory()` optional enrichment uses this queue.

The following remain outside the fail-open queue:

- canonical TaskSpec
- accepted user answers
- approval state
- plan state
- artifact bindings
- pending executable action
- execution records
- completion and terminal state
- wiki durable-processing acknowledgement

B5 injects failure into canonical plan persistence and confirms the exception still
propagates.

## 12. WAL

`SessionArchive` journal mode remains DELETE.

**WAL_CHANGED = NO**

No production or test path in this change enables WAL.

## 13. Configuration

| Setting | Clean implementation base | Effective dirty runtime used by real probe |
|---|---:|---:|
| legacy `LLM_TIMEOUT_SECONDS` / total default | 90 s | 600 s (pre-existing worktree change) |
| first chunk | min(75, total) | 75 s |
| inter-chunk idle | min(60, total) | 60 s |
| total attempt | legacy total | 600 s |
| cancellation cleanup | 5 s | 5 s |
| Generate controller max attempts | 2 | 2 |
| SDK `max_retries` | unchanged | 1 |

Environment overrides:

- `BIOMIND_LLM_FIRST_CHUNK_TIMEOUT_SECONDS`
- `BIOMIND_LLM_STREAM_IDLE_TIMEOUT_SECONDS`
- `BIOMIND_LLM_TOTAL_TIMEOUT_SECONDS`
- `BIOMIND_LLM_CANCEL_CLEANUP_TIMEOUT_SECONDS`
- `BIOMIND_GENERATE_PROVIDER_MAX_ATTEMPTS`
- `BIOMIND_SESSION_ARCHIVE_OBSERVATION_QUEUE_SIZE`
- `BIOMIND_SESSION_ARCHIVE_WRITER_BUSY_TIMEOUT_SECONDS`
- `BIOMIND_SESSION_ARCHIVE_WRITER_MAX_ATTEMPTS`
- `BIOMIND_SESSION_ARCHIVE_WRITER_BACKOFF_SECONDS`
- `BIOMIND_SESSION_ARCHIVE_SHUTDOWN_DRAIN_SECONDS`

The implementation did not replace the incident's 300-second timeout with a larger
single magic number. The split policy defaults from the existing configured total.

## 14. Deterministic and regression validation

Python used:
`/public/home/liusiqi/anaconda3/envs/biomind/bin/python`

### New A1-A15 and B1-B9

```bash
python -m py_compile   agent_core/provider_attempt.py config/reliability.py   memory/session_archive_writer.py bio_agents.py   tests/test_provider_attempt_reliability.py   tests/test_session_archive_observation_writer.py

python -c 'import langchain_core, langchain_core.messages, langchain_core.callbacks, pytest, sys; sys.exit(pytest.main(["-q", "tests/test_provider_attempt_reliability.py", "tests/test_session_archive_observation_writer.py"]))'
```

Result in both the live dirty worktree and the clean commit-only worktree:

```text
25 passed in 3.72/3.73s
```

This is A1-A15, one non-Generate compatibility assertion, and B1-B9.

### Focused existing coverage

Focused existing tests covering runtime preflight, exact context lineage wrapper,
NodeContext, Generate, corrective Generate, Recovery, runtime reconciliation,
SessionArchive deduplication, Consultant continuation, and benchmark graph routing
were run before final packaging.

Result:

```text
144 passed
13 nested subtests passed
```

### Broad baseline signal

A broader live-worktree batch produced `302 passed, 15 failed`; the clean
`68725d6` worktree produced `232 passed, 19 failed`. The failures were baseline
or unrelated dirty-tree mismatches, including unavailable `networkx`, and were not
reproduced in the focused task coverage. They are recorded here rather than
misrepresented as a fully green repository-wide suite.

## 15. REAL_MODEL_RUNTIME replay

The launcher was used only as the opaque entrypoint:

```bash
bash /public/home/liusiqi/03.毕设/启动.sh
```

The launcher was not read, its environment was not dumped, and no credentials or
model response bodies were recorded.

### Official benchmark replay

Session: `session_1787711898`

The official VCF/FASTA/GTF annotation prompt was submitted. The run stalled upstream
in TRIAGE for more than 600 seconds:

- trace call: `task_router-0001-7887044dfd57`
- manifest status: `started`
- response hash: empty
- plan steps: none
- Generate attempts: 0
- Executor commands: 0
- final route: manually terminated; launcher exit 1

This run did not reach Generate and therefore cannot validate Generate success. It
does prove that the current runtime connectivity/launcher condition is broader than
the original Generate-only incident.

### Narrow real Generate provider probe

Task: `phase3_real_generate_probe_20260826`

The probe used the real launcher-provided DeepSeek client, model
`deepseek-v4-flash`, and a harmless one-word request. Response text was not printed.

Attempt 1:

- attempt ID: `provider-attempt-f82702d3ffd7-1-9fab8021`
- call ID: `generate-0001-40420c1c3e6c`
- started: `2026-08-26T02:53:44.850136Z`
- chunks: reasoning 0, content 0, total 0
- result: `PROVIDER_FIRST_CHUNK_TIMEOUT` after 75 s
- cancel requested: `02:55:00.161560Z`
- cancel completed: `02:55:00.162373Z`
- cleanup: `CLEANUP_CONFIRMED`

Attempt 2:

- attempt ID: `provider-attempt-f82702d3ffd7-2-f58a5e35`
- call ID: `generate-0002-6b08dafe1523`
- parent: attempt 1
- started: `2026-08-26T02:55:00.274407Z`
- chunks: reasoning 0, content 0, total 0
- result: `PROVIDER_FIRST_CHUNK_TIMEOUT` after 75 s
- cancel requested: `02:56:15.526787Z`
- cancel completed: `02:56:15.541285Z`
- cleanup: `CLEANUP_CONFIRMED`
- no third attempt

Both attempts retained:

- semantic invocation:
  `semantic-invocation-063e6a6153d0f82702d3ffd7`
- request hash:
  `49e09e92daa77365f17ecb287664a42c8f12cfcbb53550297e718ad723071cea`
- node-context hash: `real-probe-context`

Evidence:

- `benchmarks/biomind_agent_benchmark/runs/provider_phase3_real_probe/task_outputs/phase3_real_generate_probe_20260826/context_trace/generate-0001-40420c1c3e6c/manifest.json`
- `benchmarks/biomind_agent_benchmark/runs/provider_phase3_real_probe/task_outputs/phase3_real_generate_probe_20260826/context_trace/generate-0002-6b08dafe1523/manifest.json`
- `logs/biomind_20260826_105339.log`

This validates the real typed failure, cleanup, same-semantic retry, and two-attempt
cap. It does not validate a successful provider response.

### Real archive writer probe

A real SQLite enqueue/write/close probe completed with:

```json
{
  "scope": "PER_PROCESS",
  "capacity": 4,
  "enqueued_count": 1,
  "written_count": 1,
  "dropped_count": 0,
  "coalesced_count": 0,
  "retry_count": 0,
  "write_failure_count": 0,
  "last_enqueue_latency_ms": 0.145,
  "last_queue_delay_ms": 0.371,
  "last_write_latency_ms": 79.445,
  "queue_depth": 0,
  "remaining_not_durable_count": 0
}
```

The final short launcher investigation was terminated at the user's request. The user
suspected the launcher; no further provider/network root-cause claim is made here.

## 16. Before / after

### Before

```text
provider stall
  -> one outer wall-clock timeout
  -> cancel requested but not awaited/drained
  -> generic TimeoutError / request_failed
  -> TASK_BLOCKED with provider first fault obscured

optional SessionArchive lock
  -> synchronous append_event
  -> workflow pays the full SQLite busy wait
```

### After

```text
provider stall
  -> first/idle/total typed watchdog
  -> cancel + bounded await/drain + fence
  -> one safe same-semantic retry
  -> completed response or typed provider terminal cause

optional SessionArchive lock
  -> bounded enqueue_nowait
  -> workflow continues
  -> background short retry and explicit telemetry
```

## 17. Acceptance review

Provider gates:

- first/last chunk observable: PASS within callback visibility
- reasoning/content both count as activity: PASS
- timeout phases typed: PASS
- adapter-owned cancellation awaited/drained: PASS
- cleanup recorded: PASS
- old observer fenced: PASS
- one bounded retry: PASS
- unknown outcome does not retry: PASS
- partial response never executable: PASS
- provider failure bypasses generated-action Recovery: PASS
- terminal state retains provider cause: PASS
- real successful model response: NOT VERIFIED due launcher/provider response failure

SessionArchive gates:

- optional write off workflow critical path: PASS
- bounded queue and queue-full fail-open: PASS
- background failures observable: PASS
- shutdown bounded/accounted: PASS in deterministic and real SQLite probes
- canonical state excluded: PASS
- process scope/cross-process limitation documented: PASS
- WAL unchanged: PASS
