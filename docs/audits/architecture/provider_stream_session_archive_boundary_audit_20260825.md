# BioMind Provider Stream Reliability + SessionArchive Blocking

## Phase 3 Pre-Implementation Read-Only Boundary Audit

**Audit date:** 2026-08-25  
**Incident:** `session_1787671230`  
**Commit basis:** `68725d6ceb5cd1082e70610070252abd3ffea690`  
**Audit basis:** the commit above **plus the live dirty working tree**, because the running behavior is defined by both.  
**Status:** `READ_ONLY_AUDIT_COMPLETE`  
**Real runtime replay:** `NOT_RUN`  
**Observability verdict:** `OBSERVABILITY_GAP_CONFIRMED`  
**Launcher boundary:** `OPAQUE_RUNTIME_ENTRYPOINT`; it was not read, modified, or invoked.  

Evidence labels used below:

- **CONFIRMED** — directly established by current code, persisted incident artifacts, or read-only inspection of the deployed database/filesystem.
- **INFERRED** — the strongest conclusion supported by several confirmed facts, but the missing runtime telemetry prevents direct proof.
- **UNKNOWN** — the incident did not persist enough evidence to decide retrospectively.

The audit began with the expected safety boundary: HEAD was `68725d6`, there were 54 tracked dirty files and 67 untracked paths, the tracked diff was `54 files changed, 8695 insertions(+), 2831 deletions(-)`, and the index was clean. Those pre-existing changes were not cleaned, staged, or modified.

## 1. Executive Conclusion

### Direct provider answers

| ID | Direct answer |
| --- | --- |
| P1 | **CONFIRMED:** the exact 300-second error is owned by BioMind's outer wall-clock watchdog around the complete `invocation_llm.ainvoke(messages)` future. It is not a first-token or idle-stream timer. The same numeric value is also passed to the provider client, but the exact incident message comes from BioMind. |
| P2 | **No, not persistently.** LangChain can deliver chunks to the callback, but BioMind records no per-attempt first/last chunk time or chunk counts. |
| P3 | **No.** Long continuous reasoning, no first chunk, mid-stream stall, content arriving without final completion, and callback/SDK/network stall all collapse into the same total deadline. |
| P4 | **No.** `cancel()` is requested, but the task is not awaited or drained and no cancellation/close result is recorded. |
| P5 | **Possible, not proven.** The code does not establish that the SDK stream and connection completed cleanup before control moves to blocked handling or a future retry. |
| P6 | **Possible, incident presence unknown.** Reasoning may have been printed and LangChain may have transiently aggregated content, but the failure snapshot replaces all of that with only the exception. |
| P7 | Raw snapshot: `TimeoutError`; human summary: `LLM request_failed during generate`; plan/result: generic `TASK_BLOCKED` with blank owner and stage. There is no terminal provider-specific error code. |
| P8 | **Conditionally safe.** This `build_snpeff_db` Generate attempt had produced no action and never entered Executor, so a single retry has business-level idempotency. It is not safe to dispatch until old-stream cleanup is awaited/bounded. |
| P9 | Partial primitives exist: provider SDK `max_retries`, Consultant's one transient retry, Planner's retryable pause, and Consultant FINALIZE's typed transport-attempt machinery. Generate has no controller-owned provider retry or model failover. |
| P10 | Add a per-call/per-attempt stream observer at the LangChain chunk boundary inside a Generate stream adapter owned by `_ainvoke_llm`; the robust design is explicit chunk consumption, with SDK connect timeout below it and controller first-token/idle/total watchdogs above it. |

### Direct SessionArchive answers

| ID | Direct answer |
| --- | --- |
| S1 | `_observe_session_memory()` writes redacted summaries, inferred task semantics, recent-message excerpts, metadata, paths, tags, and source references for historical recall. |
| S2 | For current task correctness it is `OPTIONAL_ENRICHMENT`, not a canonical TaskSpec, approval, binding, pending answer, plan, or execution-state write. |
| S3 | `ARCHIVE_BUSY_TIMEOUT_SECONDS = 15.0` is used both as `sqlite3.connect(timeout=15)` and `PRAGMA busy_timeout=15000`. These configure the SQLite busy handler; there is no application retry loop. |
| S4 | **Yes.** An `async def` directly calls synchronous SQLite. In the current `asyncio.run(...)` + LangGraph `app.astream(...)` path, that blocks the process's event-loop thread and remains on workflow latency's critical path. |
| S5 | Three production write methods exist in `SessionArchive`: initialization/schema writes, event append/FTS writes, and wiki-ingest status writes. The number of live writers equals the number of agent/process instances sharing the DB and was not persisted. |
| S6 | **Yes, architecturally and in this deployment layout.** Each agent resolves a process-level memory directory and opens `session_archive.sqlite3`; the benchmark runner creates an agent per process, and only one archive DB was found under the project. |
| S7 | A bounded per-process writer queue is appropriate for optional observations and removes them from the workflow critical path. It does not by itself serialize writers across processes. |
| S8 | Observation append, optional archive search/context, and report/cache refresh may fail open with explicit telemetry. |
| S9 | Canonical task/approval/plan/execution persistence must never fail open. Archive initialization must not claim availability after failure, and wiki-ingest acknowledgement must not claim `processed` if its status write is uncertain. |
| S10 | `NOT_PROVEN`. The file is on a proprietary `CloudStor`/`storage` mount, not a proven same-host local filesystem; cross-node access semantics are unknown. Do not enable WAL. |

The two incident domains are independent:

```text
Generate provider lifecycle failure
!= snpEff / VCF / GTF / Java failure
!= generated-action schema or semantic failure
!= SessionArchive lock root cause
```

The provider incident is a missing-completion event under an undifferentiated total watchdog. The SessionArchive incident is optional persistence synchronously paying the full SQLite busy timeout on the async workflow path. Both require implementation work, but they require different owners, states, and tests.

## 2. Incident Timeline

All persisted trace timestamps below are UTC; local runtime timestamps are Asia/Shanghai (UTC+8).

| Local time | Evidence | Conclusion |
| --- | --- | --- |
| 23:20:44 | `run_metrics.json.started_at` | Task began. |
| 23:23:57 | successful Generate `generate-0006`; 59.134 s, finish reason `stop`, 6,858 prompt / 7,238 completion tokens | Thinking-high Generate can complete in this run. |
| 23:26:54 | successful Generate `generate-0008`; 45.836 s, finish reason `stop`, 8,350 prompt / 4,856 completion tokens | A second thinking-high Generate also completed. |
| 23:28:13 | last recorded Executor action for step `prepare_snpeff_config` completed successfully | No `build_snpeff_db` command had begun. |
| 23:28:52–55 | Generate node begins; `generate-0010` manifest timestamp `15:28:55.122589Z` | Provider request for `build_snpeff_db` was dispatched. |
| approximately 23:28:55 | supplied runtime log reports HTTP 200 headers | Headers prove request acceptance only, not a chunk or completed stream. This field is not present in the persisted call manifest. |
| 23:33:55.178 | failed call completed exactly about 300.056 s after manifest start | `TimeoutError: generate LLM request timed out after 300s`. |
| 23:34:22 | Generate node returns terminal blocked; `blocked_status.json` written | Raw provider failure has been collapsed to task-level blocking. |

The failed request had 31,446 input characters, an estimated 7,862 tokens, a 200,000-token context limit, 192,138 tokens of reported headroom, 3.931% usage, no duplicate characters, and `warning=false`. The two preceding Generate calls were similarly configured and completed. There is no evidence for context overflow or prompt length as the cause.

The user-supplied runtime log reports eleven `SessionArchive database remained locked for 15s` messages, or about 165 seconds of potential cumulative waits. That console log was not found in the incident artifact tree, so exact per-occurrence timestamps and the historical lock holder are unavailable. The error text and 15-second duration do exactly match the current SQLite wrapper.

`run_metrics.json` reports 818.769 s total wall time, 689.952 s active agent time, 469.891 s in Generate nodes, 150.710 s in nine completed/profiled LLM calls, two commands, and two successful commands. There are ten persisted call directories but only nine profiler LLM events; the failed timeout call is the missing profiler event. This is consistent with the outer task cancellation not being awaited through LangChain callback termination, although it does not prove which callback event was lost.

## 3. First Real Failure

The first real failure for the current step is:

```text
Generate(build_snpeff_db)
  -> provider-backed LangChain ainvoke did not complete
  -> BioMind total wall-clock deadline expired at 300 seconds
  -> TimeoutError
  -> generic Generate exception handler
  -> current step blocked; downstream steps skipped
  -> terminal TASK_BLOCKED
```

Direct artifact evidence:

- `context_trace/generate-0010-a3992ca98911/response.json` contains only `error_type=TimeoutError` and `message=generate LLM request timed out after 300s`.
- Its manifest is `status=failed`, `node_name=generate`, `step_id=build_snpeff_db`, and records thinking enabled/high.
- `executions/execution_records.jsonl` contains actions only for `input_inventory` and `prepare_snpeff_config`.
- `plan_state.json` records `build_snpeff_db=blocked`; all later steps are `skipped_upstream_blocked`.
- `blocked_status.json` records `error_code=TASK_BLOCKED`, with empty `error_owner` and `failure_stage`.

Therefore the failure occurred before generated-action parsing, before an action was committed for this step, and before Executor. No biology tool had an opportunity to fail for `build_snpeff_db`.

## 4. Provider Call Stack

The live working-tree call stack is:

| Layer | File and current line | Caller -> callee | Ownership |
| --- | --- | --- | --- |
| Task runner | `bio_agents.py:32046-32061` | `BioAnalystAgent.go()` -> `self.app.astream(...)` | Streams **LangGraph events**, not provider chunks. |
| Graph registration | `bio_agents.py:31561-31580` | registered profiled `generate` handler | Graph/node scheduling owner. |
| Generate implementation | `bio_agents.py:26280` and `26842-26871` | `_generate_impl()` -> `_ainvoke_node_llm(...)` | Builds immutable `NodeContext`; selects Generate-bound model. |
| Invocation protocol | `bio_agents.py:9243-9299` | `_ainvoke_node_llm()` -> `_ainvoke_llm()` | Registers one controller binding; on any `BaseException`, terminates that invocation as failed. |
| Model policy | `bio_agents.py:803-870`, `1593-1626` | shared client -> `bind_generate_llm()` | Shared DeepSeek client has thinking disabled; Generate gets a node-scoped thinking binding. |
| LangChain model | `bio_agents.py:1596-1621` | `init_chat_model(streaming=True, timeout=..., max_retries=...)` | LangChain/provider SDK owns HTTP request and internal response streaming/callback dispatch. Dependency snapshots record LangChain 1.2.7, langchain-openai 1.1.7, OpenAI 2.15.0; these files are deployment snapshots, not proof of the active interpreter. |
| Application dispatch | `bio_agents.py:9418-9425` | `asyncio.create_task(invocation_llm.ainvoke(messages))` -> `asyncio.wait(...)` | BioMind waits for one final `BaseMessage`; this is not an explicit provider `astream()` loop. |
| Callback | `bio_agents.py:1496-1515` | LangChain token callback -> `VibeThinkingHandler.on_llm_new_token()` | LangChain invokes it; handler prints reasoning, suppresses ordinary-content display, and stores no timing/count state. |
| Profiler callback | `execution/profiling.py:1766-1813` | LangChain start/end/error -> profiler | Records completed/error call summaries; has no `on_llm_new_token`. |
| Total watchdog | `bio_agents.py:9423-9430` | `asyncio.wait(timeout=timeout)` -> `cancel()` + `TimeoutError` | BioMind owns the exact 300-second timeout and its text. |
| Failure snapshot | `bio_agents.py:9452-9457`; `agent_core/context_lineage.py:5878-5883` | exception -> `fail_call()` | Saves only typed exception, not stream lifecycle or partial chunks. |
| Generate failure | `bio_agents.py:26901-26912` | exception -> `_llm_failure_response()` -> `_mark_plan_current_blocked()` | Generate converts every exception into stop/block. |
| Terminal projection | `bio_agents.py:8120-8121`, `13161-13211` | `_finalize_blocked_state()` -> plan/downstream block and `blocked_status.json` | Generic task-block owner; provider identity is lost. |

Owner summary:

- **HTTP/SDK stream owner:** LangChain/provider SDK.
- **callback dispatch owner:** LangChain; callback implementation is BioMind.
- **total timeout owner:** BioMind `_ainvoke_llm()`.
- **cancellation request owner:** BioMind `_ainvoke_llm()`.
- **cancellation completion/connection-close owner:** not explicitly owned in current BioMind code.
- **exception translation owner:** `_ainvoke_llm()` for timeout/cancel translation, then Generate for generic failure-to-block translation.

## 5. Streaming Lifecycle

### Q1 — explicit stream or callback + `ainvoke`?

**CONFIRMED: callback + `ainvoke`.** The application creates a task for `invocation_llm.ainvoke(messages)` (`bio_agents.py:9423`) and waits for a final result. `streaming=True` on the model client (`bio_agents.py:1600`) enables provider-side streaming and token callbacks internally, but BioMind does not iterate provider chunks. The `self.app.astream(...)` in `go()` is the separate LangGraph event stream.

The effective lifecycle is:

```text
provider HTTP response stream
  -> provider SDK / LangChain chunk decoding
  -> LangChain callback dispatch
  -> LangChain internal aggregation
  -> final BaseMessage
  -> ainvoke task completes
  -> BioMind receives response
```

A callback receiving one or many chunks **does not imply** the outer `ainvoke` future will complete. It can still wait for the next network read, finish frame, SDK termination, callback completion, or aggregation/finalization. The incident cannot distinguish where that later wait occurred.

### Q2 — reasoning vs ordinary content

`VibeThinkingHandler.on_llm_new_token(token, chunk=...)` inspects `chunk.additional_kwargs["reasoning_content"]`. If reasoning exists it prints it. Otherwise, when `token` is non-empty, the handler recognizes ordinary token arrival, ends the thinking UI state, and deliberately does not print the token because `OutputRouter` handles final output.

Thus:

- reasoning chunks are **delivered and displayed**, but not structurally saved, counted, or timestamped;
- ordinary content tokens are **delivered to this callback boundary and not displayed**, but also not saved, counted, or timestamped;
- ordinary content is theoretically observable at this boundary, yet **not observed by current durable telemetry**;
- the shared handler has only one `is_thinking` boolean and no call/attempt identity, so it is also unsuitable as-is for concurrent stream attribution.

### Q3 — safe metadata-only instrumentation

Yes. Without logging model text, a per-attempt observer can record:

```text
call_id / attempt_id / provider_request_id when available
request_started_at / headers_at
first_reasoning_chunk_at / first_content_chunk_at / first_any_chunk_at
last_reasoning_chunk_at / last_content_chunk_at / last_any_chunk_at
reasoning_chunk_count / content_chunk_count / total_chunk_count
finish_reason / stream_closed_at
cancel_requested_at / cancel_completed_at / close_result
```

The instrumentation must be created per `_ainvoke_llm` dispatch and attached at the LangChain chunk boundary. Counting in the current process-global `VibeThinkingHandler` would mix concurrent calls. For reliable first-token and idle enforcement, a Generate-specific adapter should explicitly consume `astream()` chunks (or equivalently own an observable chunk iterator), aggregate the final message, and expose lifecycle events to the controller watchdog. Only timing/count/metadata should go to routine logs; text belongs, if retained at all, in a separately classified bounded diagnostic artifact.

## 6. Timeout Ownership

### Configuration chain

The live working tree defines:

```text
BIOMIND_LLM_TIMEOUT_SECONDS
  -> config.config.LLM_TIMEOUT_SECONDS (live default 600.0)
  -> BioAnalystAgent.llm_timeout_seconds
  -> provider client timeout=<same value>
  -> _ainvoke_llm total timeout=<same value for managed tasks>
```

Relevant code: `config/config.py:154-163`, `bio_agents.py:1577-1582`, `1596-1604`, and `9322-9327`.

The committed HEAD default is 90 seconds, while a pre-existing dirty change makes the live working-tree default 600 seconds. The incident actually used 300 seconds. A production-code search found no node assignment or per-call override beyond initialization. Within the audited current checkout, the only supported source for an effective 300 is an environment-layer value for `BIOMIND_LLM_TIMEOUT_SECONDS`; because the launcher is intentionally opaque, the exact injection point was not inspected. There is no Generate-specific timeout override. The provider client receives the same global value.

### What is bounded?

The outer timer wraps the entire `ainvoke` task from task creation to final `BaseMessage`. It includes, as part of one undifferentiated wall-clock interval:

- client setup/connect and request send;
- waiting for response headers;
- waiting for the first chunk;
- all subsequent chunk reads and callback work;
- LangChain aggregation and final completion.

The scalar `timeout` is also passed into the provider client, but current BioMind code does not persist the SDK's effective connect/read/write/pool breakdown or which internal timeout fired. The exact incident text is created only at `bio_agents.py:9428-9430`, proving the outer total watchdog fired.

### Q4 — can the 300 seconds distinguish timeout phases?

**No.** There is no separate clock or state for connect, first token, inter-chunk idle, or total duration. The current persisted state is only `started` followed by either a completed whole response or a failed exception. Consequently A–E in the audit request remain indistinguishable after the fact.

## 7. Cancellation / Cleanup

The timeout path is:

```text
asyncio.wait(..., timeout=300) returns no completed task
  -> invoke_task.cancel()
  -> raise application TimeoutError
  -> ContextLineageStore.fail_call(error only)
  -> finally: cancel again only if task is still not done
  -> Generate catches Exception
  -> finalize memory attempt
  -> mark current plan step blocked
  -> finish
```

### Q5 — task lifecycle after timeout

| Operation | Current result | Evidence |
| --- | --- | --- |
| cancellation requested | **YES** | `invoke_task.cancel()` at `bio_agents.py:9427`; possible second request at `9460-9461`. |
| cancellation awaited | **NO** | no `await invoke_task` after either cancellation. |
| task result/exception drained | **NO** | no post-cancel `gather(..., return_exceptions=True)` or result retrieval. |
| stream explicitly closed | **NO** | no `aclose()`/response close call in BioMind. |
| cleanup outcome persisted | **NO** | failed manifest has no cancel/close fields. |

Calling `cancel()` schedules `CancelledError` for delivery at the task's next cancellation point; it does not prove that the coroutine has stopped by the time `cancel()` returns. The `finally` block repeats cancellation but still does not wait.

### Q6 — HTTP/stream connection closure

There is no explicit BioMind close contract. Cleanup is delegated implicitly to LangChain, the provider SDK, the HTTP client's cancellation behavior, connection-pool behavior, or eventual object cleanup. Current artifacts contain neither a provider request ID nor stream-close result, so the incident's actual connection state is **UNKNOWN**.

### Q7 — residual work risk

The following are possible, not established incident facts:

- provider computation can continue server-side after the client disconnects;
- the local SDK coroutine can remain alive briefly or ignore/delay cancellation while closing;
- a callback can arrive late if cancellation is not honored promptly;
- a connection can remain checked out until SDK cleanup;
- a future retry can overlap an incompletely cleaned old attempt.

This is a P0 boundary because retry safety depends on cleanup completion. The next implementation must request cancellation, explicitly await/drain it under a small bounded cleanup deadline, close an explicitly owned stream where supported, record the result, and prevent late chunks from an old attempt from mutating the active attempt.

## 8. Partial Response Behavior

### Current storage

`ContextLineageStore.start_call()` writes request and manifest (`agent_core/context_lineage.py:5780-5853`). `finish_call()` writes one whole response and marks completion (`5855-5876`). `fail_call()` calls `finish_call()` with only `{role:error, error_type, message}` (`5878-5883`). There is no generic partial-response accumulator or artifact for Generate.

At runtime, possible partial data has these lifetimes:

- reasoning text: delivered to and printed by `VibeThinkingHandler`, then not retained by that handler;
- ordinary content: delivered as the callback `token` and transiently aggregated inside LangChain's still-running `ainvoke` coroutine, but not exposed to BioMind until final completion;
- profiler: no token callback, so it has no chunk data;
- context trace: only whole final response or exception.

### Q8 — where would partial content be?

Only in ephemeral SDK/LangChain aggregation state, callback arguments, and possibly terminal stdout for reasoning. It is not present in the structured call snapshot.

### Q9 — is it lost on timeout?

For the generic Generate path, **yes from durable diagnostic state**. The failure snapshot contains only the exception. Whether this incident had any partial chunks is **UNKNOWN**; absence from the snapshot is an observability absence, not evidence that no chunks arrived.

### Q10 — safe future preservation

A separate immutable artifact such as `partial_response_diagnostic.json` is appropriate if it has all of these properties:

- `classification: DIAGNOSTIC_ONLY` and `executable: false`;
- bound to `call_id`, `attempt_id`, request hash, and NodeContext semantic identity;
- never used to populate `pending_action`, never sent to action parsing, and never replayed as a completed response;
- bounded size, secret-redacted, with truncation/hash metadata;
- explicit completeness fields: chunk counts, last chunk time, `finish_reason=null`, cancel/close outcome;
- stored separately from the immutable completed `response.json` contract.

If retaining raw partial content is not required, counts/timestamps plus a content hash and bounded redacted tail are safer defaults. Partial generated shell/code must remain non-executable even if syntactically complete.

## 9. Provider Error Taxonomy

### Current real taxonomy

```text
provider/transport exception
  ├─ text matches content-filter markers -> LLM input_rejected
  └─ everything else                    -> LLM request_failed
       └─ Generate catches Exception
            -> deterministic failure message
            -> block current step and downstream
            -> TASK_BLOCKED

completed model response
  -> generated response parsing / script protocol / schema / semantic audit
  -> separate post-provider controller/recovery paths
```

`agent_core/llm_safety.py:18-31,48-60` has a text-marker transient classifier, but `llm_failure_summary()` exposes only `input_rejected` versus `request_failed`. Generate's catch at `bio_agents.py:26901-26912` does not consult the transient classifier at all.

### Q11 — distinctions currently retained

| Failure | Current Generate distinction |
| --- | --- |
| provider connect timeout | No; generic exception/request_failed |
| provider first-token timeout | No timer or type |
| provider idle-stream timeout | No timer or type |
| provider total timeout | Outer `TimeoutError` text exists, but no durable provider error code |
| HTTP error | Exception text/type only; generic request_failed unless input filter text matches |
| SDK exception | Exception text/type only; generic request_failed |
| malformed completed response | Yes, downstream of a completed provider response |
| generated-action schema failure | Yes, downstream controller validation |
| generated-action semantic failure | Yes, downstream controller/recovery validation |

The last three generated-response categories are not evidence about this incident because no completed response reached parsing.

### Q12 — incident's final type

The chain is exactly:

```text
context trace response: TimeoutError
stage_status/report:    LLM request_failed during generate
plan step:              blocked
downstream steps:       skipped_upstream_blocked
blocked_status/result:  TASK_BLOCKED
error_owner:            empty
failure_stage:          empty
```

It is not classified as `PROVIDER_FAILURE`, `GENERATE_TIMEOUT`, or another typed terminal condition. The generic terminal projection obscures the first real fault.

## 10. Retry Safety

### Q13 — side effects before this timeout

For the failing `build_snpeff_db` Generate dispatch:

- no generated action was recorded for that step;
- there is no Executor route or execution record for that step;
- no shell command, declared output write, or external business/API mutation was initiated by it.

Earlier steps in the same task did run two successful commands and created plan/config artifacts. A retry of **this Generate dispatch** must continue from those committed results and must not repeat earlier steps. The provider request itself may still incur provider billing/logging, and an old request may remain server-side; that is why cleanup/attempt fencing is a prerequisite.

Conclusion: one bounded Generate retry is business-idempotent after a transport-class failure, provided no completed response/action has been committed and cancellation cleanup is bounded and observed.

### Q14 — reusable mechanisms

| Existing mechanism | Reuse assessment |
| --- | --- |
| client `max_retries=LLM_MAX_RETRIES` (default 1) | SDK-level and opaque to controller attempt state; exact behavior for an already-streaming request is not recorded. Do not count it as the controller's one safe Generate retry without instrumentation. |
| `is_transient_llm_failure()` | Reusable concept, but current text matching is too coarse for phase-specific stream failures. |
| Consultant `llm_retry_counts` one retry (`bio_agents.py:22185-22209`) | Reusable bounded-budget/state pattern. |
| Planner `_pause_retryable_llm_failure()` (`bio_agents.py:6669-6717`, `25020-25021`) | Reusable state-preservation and resume pattern; it is a pause, not automatic Generate retry. |
| Consultant FINALIZE transport-attempt/retry authorization | Reusable typed attempt, idempotency, and unknown-outcome concepts; it is scoped to FINALIZE and should not be copied wholesale. |
| model failover/fallback | No Generate model-failover mechanism was found in the audited production path. |

The next spec should compose these existing ideas into one provider-attempt contract, not add an unrelated ad hoc loop inside `_generate_impl`.

### Q15 — context identity for retry

Reuse the **same immutable semantic NodeContext snapshot**, approved plan snapshot, current step, request payload/hash, resolved bindings, and authorization. Do not rebuild from mutable live state unless staleness is revalidated.

Do **not** reuse the exact single-use controller invocation ID: `_ainvoke_node_llm()` registers it and terminates it as failed. Create a fresh dispatch/attempt envelope with:

- a new `call_id`, `attempt_id`, and attempt ordinal;
- `parent_call_id`/previous provider failure reference;
- the same semantic request/NodeContext hash;
- explicit previous attempt cancellation and close outcome;
- retry budget consumption;
- attempt-scoped provider policy if authorized.

Previous failure evidence should normally remain controller metadata rather than silently changing the semantic model prompt. If attempt 2 changes reasoning effort or model route, that policy delta must be explicit and hashed as attempt metadata.

## 11. Thinking Policy Boundary

The shared DeepSeek client is bound with thinking disabled (`bio_agents.py:809-816`). `bind_generate_llm()` creates a Generate-only binding with `thinking={type:enabled}` and configured reasoning effort (`824-870`). The live defaults are `BIOMIND_GENERATE_THINKING_ENABLED=true` and `BIOMIND_GENERATE_REASONING_EFFORT=high`; accepted effort values are only `high` and `max` (`config/config.py:85-90`).

### Q16 — scope

- All calls through the Generate-bound DeepSeek model use the Generate node policy by default.
- This is not a system-wide policy; Planner/Consultant/Supervisor do not inherit the shared-client thinking setting.
- Non-DeepSeek targets or an explicit Generate disable setting do not get the enabled binding.
- The incident manifest directly confirms `scope=generate_only`, `thinking_enabled=true`, and `reasoning_effort=high`.

The two earlier successful Generate calls in the same incident had the same recorded policy. That rejects “thinking high always fails” and provides no proof that the failed model reasoned continuously for 300 seconds.

### Q17 — attempt-scoped reduction

Node-scoped binding exists, so the architectural seam is available. Attempt-scoped retry policy does not exist, and current Generate config does not accept a lower effort value. Lowering attempt 2 safely therefore requires an explicit spec and code change that records the attempt policy; it cannot be achieved through the current retry path. Disabling thinking globally or changing the shared client is not justified by this incident.

## 12. SessionArchive Call Stack

The common observation path is:

```text
async LangGraph node
  -> await _observe_session_memory(...)
  -> synchronous SessionArchive.append_event(...)
  -> connection 1: dedup SELECT by content_hash
  -> optional large-value externalization
  -> connection 2: INSERT session_events
       -> optional INSERT session_events_fts
       -> commit
  -> on sqlite OperationalError containing "locked"
       -> translated 15-second SessionArchive error
  -> _observe_session_memory catches Exception and logs warning
  -> node continues only after the synchronous wait has ended
```

Evidence:

- `_observe_session_memory()` is async but directly calls `self.session_archive.append_event()` at `bio_agents.py:1875-1922`.
- It builds content from the last four messages, inferred/merged semantics, bounded payload/summary, paths, tags, return code, and task source references.
- `SessionArchive.append_event()` uses a dedup read connection and a separate insert/FTS/commit connection at `memory/session_archive.py:156-288`.
- `_connect()` applies the timeout and translates lock failures at `memory/session_archive.py:886-906`.
- `_observe_session_memory()` catches all exceptions and does not alter node routing.

Observation calls exist after Consultant, Planner, plan review, Generate action creation, Execute result persistence, Supervisor decisions, installation decisions, wait-for-user state, and finalization (`bio_agents.py:1954`, `14007`, `22243`, `25797`, `26120`, `26245`, `27567`, `29445`, `29933`, `30227`, `30345`, `30496`, `30537`, `31021`, `31238`, `31295`, `31370`, `31397`, `31539`). The canonical action/plan/execution decision is generally established before the observation call.

The `SessionArchive` production write methods are:

| Method | Trigger/process | Transaction/write | Frequency |
| --- | --- | --- | --- |
| `_init_db()` (`session_archive.py:530-602`) | Every `SessionArchive` construction, hence every agent instance | DDL/schema/index/FTS checks and schema-version upsert, then commit | Agent startup/initialization |
| `append_event()` (`156-288`) | `_observe_session_memory()` from many nodes | dedup SELECT; event INSERT; optional FTS INSERT; commit | approximately once per observed node/transition/finalization |
| `mark_wiki_ingested()` (`438-470`) | `memory/wiki_memory.py:671` | one or more wiki status upserts, then commit | wiki ingest batches |

No other direct production connection to this exact database path was found. Other `append_event` symbols in the repository belong to different stores/profilers.

## 13. Archive Data Criticality Classification

### Q18 — what is written?

Classification: **`OPTIONAL_ENRICHMENT` for current workflow correctness.**

The observation contains historical memory, observational trace, inferred semantics for future recall, recent-message excerpts, event metadata, and source/path tags. It does not own:

- canonical TaskSpec or accepted user facts;
- a pending user answer/interrupt;
- approved plan or plan step authority;
- artifact bindings;
- pending executable action;
- execution result correctness state.

The class is a source of truth for its own cross-session event archive, but that does not make each observation a prerequisite for the current task's controller correctness.

### Q19 — can the task remain correct if append fails?

**Yes.** The exception is caught and the node continues after the wait. In this incident, the generated task summary explicitly says it is derived from SessionArchive and is a report/cache, reports `Status: not_started` and `Event count: 0`, while canonical plan/execution artifacts show the task planned, executed two actions, and then blocked at Generate. This is direct evidence that archive loss degraded recall/reporting but did not own workflow state.

The architectural defect is therefore not fail-open behavior itself. It is that fail-open happens only **after** paying up to the full synchronous busy timeout on the main path, potentially eleven times.

## 14. SQLite Lock / Writer Analysis

### Exact 15-second source and Q20

`ARCHIVE_BUSY_TIMEOUT_SECONDS = 15.0` (`memory/session_archive.py:17`) becomes both:

```python
sqlite3.connect(..., timeout=15.0)
PRAGMA busy_timeout = 15000
```

These set/replace SQLite's connection busy handler; they are not two additive application waits. There is no application retry loop in `_connect()` or `append_event()`. When one database operation cannot acquire its lock within the busy window, `_connect()` translates the `OperationalError` to the fixed message “remained locked for 15s” and closes the connection.

One logged warning therefore corresponds to one failed archive call/connection operation waiting through one SQLite busy-handler window, not an accumulation of BioMind retries. `append_event()` has two connections, so either the dedup read or write/commit phase can encounter a lock; the first translated failure aborts that call. Eleven warning occurrences represent eleven separate observation attempts and up to approximately 165 seconds of cumulative critical-path delay.

### Actual database state

Read-only inspection on 2026-08-25 found:

```text
path: /public/home/liusiqi/03.毕设/memorydata/memory/session_archive.sqlite3
size: 315,699,200 bytes
journal_mode: delete
locking_mode: normal
synchronous: 2 (FULL)
session_events: 3,941
events for session_1787671230: 0
```

No current holder appeared in `lsof` or `lslocks`. This is a post-incident observation only: the historical PID/process/transaction that held the lock is **UNKNOWN**. The user-supplied console warnings were not persisted into the task output tree, so they cannot identify the holder retrospectively.

### Q26 — same-process concurrency

The normal benchmark graph route is sequential and each observation is awaited; no internal fan-out of archive writes was found in that path. However, there is no archive mutex or single-writer owner, so concurrent `go()` calls, multiple agent instances in one process, a wiki write, or future graph concurrency can contend. Same-process concurrent writing is architecturally possible even though it is not proven for this incident.

### Q27 — multiple processes and one global DB

Each `BioAnalystAgent` resolves the configured process memory root and constructs the same filename (`bio_agents.py:1539-1545`, `1736-1738`). The benchmark runner instantiates one agent per running process (`benchmarks/.../run_biomind_prompt.py:122-139`). Only one `session_archive.sqlite3` was found in the project tree, and the incident's prompt/summary artifacts are under that same `memorydata/memory` root.

Therefore multiple benchmark or agent processes can and, when run concurrently with the same configuration, do share one archive DB. The exact number alive during this incident is not recorded. A per-process queue will not turn this into a globally single-writer deployment.

## 15. Async Critical-Path Analysis

### Q21 — sync SQLite inside async node

**CONFIRMED.** `_observe_session_memory()` is `async def`, but contains no offload around `SessionArchive.append_event()`. The benchmark runs `await agent.go()` under an asyncio runner, `go()` consumes `self.app.astream(...)`, and the registered Generate/other handlers are async. No worker executor boundary surrounds this archive write.

Consequences in the current path:

- the node coroutine cannot progress for the SQLite wait;
- the process's event-loop thread cannot schedule other coroutines during the synchronous call;
- other OS threads and other processes are not frozen;
- regardless of whether the SQLite extension releases the Python GIL internally, the event loop itself remains occupied on its thread.

### Why `asyncio.to_thread()` is incomplete

Wrapping the write in `await asyncio.to_thread(...)` would free the event-loop thread to schedule other work, but the awaiting node would still not route forward until the write or 15-second lock timeout completes. It solves **event-loop responsiveness**, not **workflow critical-path latency**. It also does not serialize multiple threads/processes or make an in-flight SQLite operation cancellable.

### Q22 — existing lifecycle primitives

No process-level SessionArchive writer, background archive worker, queue, drain operation, or archive shutdown hook exists. The only `atexit` hook restores terminal state. `agent_core/consultant_continuation.py:336-395` has an optional-enrichment daemon-thread helper with a bounded join; its classification and telemetry ideas are reusable, but its timed-out thread keeps running and it has no queue, serialization, drain, or shutdown contract. It is not a writer lifecycle.

### Q23 — bounded queue obligations

A future queue must specify:

- **full:** non-blocking enqueue; drop/coalesce optional observations with a counter, never wait 15 seconds in the node;
- **shutdown:** stop accepting, bounded drain, then record remaining/dropped count; an explicit agent/runner close is primary, `atexit` only best effort;
- **failure:** bounded lock retries with jitter in the worker, circuit/backoff telemetry, and no exception routed into canonical task state;
- **duplicate:** retain content identity and use atomic insert-ignore/upsert semantics because the current SELECT-then-INSERT race remains across processes;
- **ordering:** assign monotonic per-process/task sequence at enqueue; document that global cross-process ordering is not guaranteed;
- **attempt ownership:** include task/stage/event ID so late writes cannot be attributed to a newer task attempt;
- **capacity/health:** queue depth, oldest age, enqueue/drop/write/retry/failure counters, and last-success time.

One process-level worker serializes only that process. Cross-process contention still requires either a dedicated database-local writer service, per-process/per-job archive partitioning with later merge, or a client/server store.

### Q24 — simplest safe optional policy

For `_observe_session_memory()`:

1. `put_nowait` into a bounded process-level queue;
2. continue the workflow immediately;
3. on full, drop or coalesce the optional event and increment explicit telemetry—do not block;
4. let the worker perform a very small bounded retry/backoff policy;
5. bounded drain at orderly shutdown; after the bound, report/drop remaining optional events.

This is simpler and safer than spawning one untracked thread per observation or awaiting `to_thread`. A short bounded wait is justified only in the background worker, not in the node. Final/terminal observations may receive higher queue priority, but they still must not become canonical workflow state.

## 16. Filesystem / WAL Assessment

Read-only deployment inspection found:

```text
mount target: /public/home/liusiqi/03.毕设
mount source: CloudStor_20N_SG_FS01[/home/liusiqi/03.毕设]
reported fstype: storage
statfs type: UNKNOWN (0x797083)
options: rw,nosuid,nodev,noatime
```

This is not reported as ext4, XFS, or another proven local filesystem. It is also not labeled NFS or Lustre, but the CloudStor source and proprietary `storage` type indicate a shared-storage boundary whose mmap/shared-lock/cross-node guarantees were not established. The audit found no authoritative proof that all processes accessing the DB are on one host.

SQLite's official [WAL documentation](https://www.sqlite.org/wal.html) states that the WAL-index uses shared memory and all readers must be on the same machine; WAL does not work over a network filesystem. SQLite's [network filesystem guidance](https://www.sqlite.org/useovernet.html) also warns that sync and locking reliability vary across implementations and recommends a client/server database when multiple systems require concurrent access.

### Q25 decision

```text
WAL = NOT_PROVEN
```

It is neither approved nor justified for the current deployment. The 315 MB file size alone says nothing about WAL safety. Required proof before reconsideration would include the storage vendor's SQLite/WAL support statement, same-host access enforcement, shared-memory/mmap and lock semantics, checkpoint/backup ownership, and a multi-process durability test on the exact mount. Until then, retain the current journal mode and address optional critical-path writes independently.

## 17. Recommended Implementation Boundary

This section defines the next-spec boundary only; nothing below was implemented.

| Priority | Requirement | Classification | Boundary |
| --- | --- | --- | --- |
| P0 | Per-attempt stream lifecycle instrumentation | **REQUIRED** | Generate stream adapter records headers/first/last chunks, counts, idle, finish reason, provider request ID, cancel/close result without routine text logging. |
| P0 | Cancellation ownership and cleanup | **REQUIRED** | Own stream; cancel; explicitly close where available; await/drain under bounded cleanup deadline; fence late callbacks by attempt ID. |
| P1 | Typed provider timeout/error taxonomy | **REQUIRED** | Separate connect, first-token, stream-idle, total, HTTP, SDK, cancellation-cleanup, and unknown-outcome states from completed-response parse/schema/semantic failures. Preserve first fault in terminal output. |
| P1 | One bounded Generate provider retry | **REQUIRED** after P0 | Retry only a typed transient, pre-action failure; same semantic NodeContext, new attempt identity; never retry an unknown completed/action-committed outcome. |
| P1 | Partial response diagnostic | **RECOMMENDED** | Separate, bounded, redacted, immutable, `DIAGNOSTIC_ONLY`, `executable=false`; never feed action parser. |
| P1 | Optional SessionArchive observation off critical path | **REQUIRED** | Non-blocking bounded enqueue and explicit fail-open telemetry. Do not merely increase timeout or await `to_thread`. |
| P2 | Process-level archive writer queue | **RECOMMENDED** | One lifecycle-owned worker/connection per process, bounded retry/drain/capacity. Document that it does not solve cross-process serialization. |
| P2 | SQLite transaction/query/index tuning | **RECOMMENDED** after measurement | Measure lock phase/duration; reduce transaction scope/FTS amplification; make dedup atomic; review startup DDL and index/query plans. |
| P2 | Cross-process storage design | **RECOMMENDED** if concurrent processes are intended | Dedicated writer service, per-job DB partitioning/merge, or client/server database. |
| P2 | Attempt-2 reasoning/model policy | **OPTIONAL** | Only after stream telemetry shows benefit; make attempt-scoped and recorded. No global thinking change. |
| P3 | Switch DELETE journal to WAL now | **NOT_JUSTIFIED** | Filesystem/same-host support is not proven. |
| P3 | Raise total timeout as the fix | **NOT_JUSTIFIED** | It preserves the observability and cancellation defects and increases worst-case latency. |

Recommended provider state progression:

```text
ATTEMPT_STARTED
  -> HEADERS_RECEIVED
  -> FIRST_CHUNK_RECEIVED
  -> STREAM_PROGRESS
  -> COMPLETED

or typed failure:
  PROVIDER_CONNECT_TIMEOUT
  PROVIDER_FIRST_TOKEN_TIMEOUT
  PROVIDER_STREAM_IDLE_TIMEOUT
  PROVIDER_TOTAL_TIMEOUT
  PROVIDER_HTTP_ERROR
  PROVIDER_SDK_ERROR
  PROVIDER_CANCEL_CLEANUP_FAILED / PROVIDER_OUTCOME_UNKNOWN

completed response only:
  RESPONSE_MALFORMED
  GENERATED_ACTION_SCHEMA_INVALID
  GENERATED_ACTION_SEMANTIC_INVALID
```

SDK connect/read controls, stream-adapter first-token/idle controls, controller total deadline, and cancellation cleanup must each have one named owner. A terminal result should retain the typed first provider fault instead of projecting only `TASK_BLOCKED`.

## 18. Required Regression Tests

Future implementation tests, using a deterministic fake stream/provider and no real credentials, must include at least:

1. **First attempt total/stream timeout -> retry succeeds -> task continues.** Verify same semantic NodeContext hash, new attempt/call ID, one consumed retry budget, and no repeated earlier Executor action.
2. **Two consecutive provider timeouts -> terminal typed provider failure.** Verify no third dispatch and no generic first-fault loss into bare `TASK_BLOCKED`.
3. **Headers but no first chunk -> first-token timeout.** Verify it is distinct from connect and total timeout.
4. **Reasoning chunks continuously arrive -> idle watchdog does not fire.** Verify counts/timestamps without logging reasoning text.
5. **Content chunks continuously arrive -> idle watchdog does not fire.** Verify content can be observed without UI/body logging.
6. **Chunks arrive and then stop -> idle timeout fires.** Verify first/last timestamps and correct typed phase.
7. **Long active stream crosses total deadline -> total deadline policy is explicit.** Verify this is distinguishable from idle and records recent progress.
8. **Timeout -> old stream cancelled, closed, awaited/drained.** Assert no pending task, no late callback mutation, and connection/adapter cleanup completes or becomes typed unknown outcome.
9. **Delayed old-attempt chunk after retry begins.** Verify attempt fencing discards it from the active attempt.
10. **Partial content before failure.** Persist a bounded `DIAGNOSTIC_ONLY`, `executable=false` artifact; prove it never enters `pending_action`, parser, approval, or Executor.
11. **Provider/SDK internal retry visibility.** Ensure SDK attempts do not silently exceed the controller budget and provider request IDs map to controller attempts.
12. **Attempt-2 policy delta.** If reasoning/model policy is supported, verify it is attempt-scoped, hashed/recorded, and does not mutate shared defaults.
13. **SessionArchive locked.** Optional observation enqueue returns within a small local bound and the main workflow does not stall for 15 seconds.
14. **Queue full.** Workflow continues; drop/coalesce counters and task/stage identity are accurate.
15. **Worker lock retries exhausted.** Optional failure is reported in telemetry and does not alter canonical task state.
16. **Orderly and abrupt shutdown.** Bounded drain works; residual optional events are explicitly counted, never silently claimed durable.
17. **Duplicate observation and cross-process race.** At most one logical event survives; no uncaught uniqueness error or orphaned large-output reference.
18. **Canonical required state write failure.** It must fail closed/typed and must never use the optional archive fail-open path.
19. **Wiki acknowledgement failure.** Never claim an event `processed` when the status transaction is failed or outcome-unknown.
20. **WAL deployment gate.** Any future WAL change must fail configuration validation unless exact filesystem and same-host requirements are proven.

## 19. Questions P1–P10 / S1–S10

### Provider

**P1. 300 秒 timeout 真正包围哪一层？**  
整个 `invocation_llm.ainvoke(messages)` final-result future 的应用层总墙钟；SDK 也收到同一 scalar timeout，但精确事故消息由外层 `asyncio.wait` 产生。

**P2. 当前能否观察 first/last chunk？**  
当前回调边界理论上能看到 chunk 到达，但没有 per-call 计数/时间持久化，因此事故后不能观察。

**P3. stream stall 与 long reasoning 当前能否区分？**  
不能；也不能区分 headers 后无首 chunk、mid-stream stall、content 仍到达但 finalization 卡住、callback/SDK/network stall。

**P4. timeout 后旧 asyncio task 是否被正确 await/cleanup？**  
否。只请求 `cancel()`，未 await、drain 或记录 cleanup 完成。

**P5. 底层 provider stream 是否可能残留？**  
可能；代码未建立关闭完成契约。事故是否实际残留为 UNKNOWN。

**P6. 部分 response 是否存在但被丢弃？**  
可能。当前故障快照必然丢弃结构化 partial；事故是否收到过任何 chunk 为 UNKNOWN。

**P7. 当前 timeout 被归为什么错误类型？**  
原始 `TimeoutError` -> `LLM request_failed during generate` -> step blocked -> terminal `TASK_BLOCKED`；owner/stage 为空。

**P8. Generate provider timeout 是否安全可重试？**  
本次在业务副作用层面可安全重试一次，因为该 step 尚无 action/Executor；前提是先完成或有界确认旧 attempt 的取消/关闭，并保持已有前置步骤结果。

**P9. 现有系统有没有可复用 retry/model-routing 机制？**  
有 SDK max retry、Consultant 单次 transient retry、Planner paused-retryable、Consultant FINALIZE typed attempt 概念；Generate 无 controller retry，未发现 Generate model failover。

**P10. 应该在哪一层增加 stream instrumentation？**  
`_ainvoke_llm` 所拥有的、per-attempt 的 LangChain chunk/stream adapter；不要放在共享 UI handler，也不要等 `ainvoke` 返回后才记录。

### SessionArchive

**S1. `_observe_session_memory` 写的到底是什么？**  
跨会话历史观察：消息片段、summary、inferred semantics、metadata、paths/tags/source refs，用于 future recall/report/cache。

**S2. 它是否 correctness-critical？**  
对当前任务控制正确性不是；分类为 `OPTIONAL_ENRICHMENT`。

**S3. 为什么一次锁等待正好是 15 秒？**  
硬编码 `ARCHIVE_BUSY_TIMEOUT_SECONDS=15.0` 同时配置 connect timeout 和 `PRAGMA busy_timeout=15000`，即同一 SQLite busy handler 窗口。

**S4. 同步 SQLite 是否位于 async workflow critical path？**  
是。async node 直接同步调用；当前 runner/event-loop 路径下既阻塞该 node，也占住该进程 event-loop thread。

**S5. 当前有几个可能 writer？**  
代码上三个写方法；运行时 writer 实例数等于共享该路径的 agent/process 数，事故时数量未知。当前事后检查无持锁进程。

**S6. 是否多个进程共用一个 archive DB？**  
架构允许且当前部署路径支持这一结论：每进程 agent 指向同一配置 memory root/filename，项目内只找到一个 DB。事故时实际并发数未持久化。

**S7. 单 writer queue 是否适合现有架构？**  
适合把 optional observation 移出主路径并串行化进程内写入；单个 per-process queue 不能解决跨进程竞争。

**S8. 哪些 archive 操作可以 fail-open？**  
observation append、optional recall/search/context、由 archive 派生的 summary/cache 刷新；必须显式记录丢弃/失败。

**S9. 哪些绝对不能 fail-open？**  
TaskSpec、用户答案、approval、plan、binding、pending action、execution/result 等 canonical persistence；archive 初始化不可虚报成功，wiki `processed` acknowledgement 不可在写入失败/未知时静默成功。

**S10. 当前 filesystem 是否适合 WAL？**  
`NOT_PROVEN`。CloudStor proprietary/shared mount 与跨节点边界未满足 SQLite 官方要求的可证明条件，本轮禁止切换。

## 20. Final Go / No-Go Recommendation

### Decision

```text
PHASE_3_BOUNDARY_AUDIT = GO / COMPLETE
IMPLEMENTATION_SPEC_AUTHORING = GO
IMMEDIATE_PRODUCTION_RETRY_CHANGE = NO-GO until P0 instrumentation and cleanup contract exist
IMMEDIATE_SESSIONARCHIVE_OFF-PATH SPEC = GO
IMMEDIATE_WAL CHANGE = NO-GO (NOT_PROVEN)
REAL_RUNTIME_REPLAY = NOT_RUN
```

There is enough evidence to write a narrow Implementation Spec. There is not enough evidence to claim that DeepSeek reasoned for 300 seconds, that no chunk arrived, or that the SDK stream closed. Recovery must first become observable and cancellation-owned. A Generate retry may then be added at the controller/provider-attempt boundary, not as a blind exception loop.

SessionArchive observations are safe to move off the workflow correctness path. A process-level bounded writer is a valid first step, but the spec must acknowledge the shared global DB and define a cross-process direction. WAL is explicitly out of scope until deployment support is proven.

### Runtime and secret safety

- The opaque launcher was not read, changed, or invoked.
- No environment dump, process environment, API credential, or denied key file was read or printed.
- A real rerun was intentionally omitted: current code cannot answer chunk timing questions, and a run would create workflow/database state without improving the decisive evidence.

### Git safety result

This audit added only this unstaged, uncommitted Markdown report. All pre-existing dirty files were preserved.

```text
PRODUCTION_CODE_CHANGED = NO
TEST_CODE_CHANGED = NO
PROMPT_CHANGED = NO
SKILL_CHANGED = NO
RUNTIME_CONFIG_CHANGED = NO
SQLITE_JOURNAL_MODE_CHANGED = NO
TIMEOUT_VALUE_CHANGED = NO
MODEL_ROUTING_POLICY_CHANGED = NO
GIT_INDEX_CHANGED = NO
GIT_COMMIT_CREATED = NO
AUDIT_DOCUMENT_ADDED = YES (unstaged, uncommitted)
```

Final principle:

```text
HTTP 200 != completed streaming response
Provider timeout != snpEff failure
Provider timeout != generated-action semantic failure
SessionArchive lock != provider timeout root cause
Optional memory persistence must not freeze the workflow correctness path
Streaming reliability must be observable before it is recoverable
```
