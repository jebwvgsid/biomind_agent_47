# PI-MIG-02 Pi RPC proof-of-concept audit

Date: 2026-09-03

## Status

Implementation and mocked protocol coverage are complete, but PI-MIG-02 is
`NOT_READY` on this host.  The required real-process acceptance cannot run
because no Node executable is installed and the approved attempt to download
Node 22.19.0 from `nodejs.org` was rejected by the platform network policy
(`curl: (7) failed to open socket: Operation not permitted`).  No system or
global Node installation was changed, no alternative Pi version was selected,
and mock results are not treated as real Pi evidence.

## Frozen baseline and Git boundary

- Branch: `refactor/consultant-controller-authority`
- Base HEAD: `67fa03adf00b0ff32da86fa225ab4a8546b80a2a`
- Base subject: `feat(step-worker): add replaceable typed execution boundary`
- Tracked and staged state at entry: clean
- Pre-existing untracked status entries at entry: 30; none are task-owned and
  none were deleted, moved, overwritten, stashed, cleaned, or staged.
- The PI-MIG-01 `StepRequest`, `StepOutcome`, outcome vocabulary, typed router,
  and default `legacy` backend remain unchanged.

## Fixed runtime and installation isolation

- Package: `@earendil-works/pi-coding-agent`
- Version: exactly `0.84.4`
- Required Node engine: `>=22.19.0`
- Local integration: `integrations/pi_step_worker/`
- Intended executable:
  `integrations/pi_step_worker/node_modules/.bin/pi`
- `package.json` uses exact versions; `.npmrc` sets `ignore-scripts=true` and
  exact-save behavior.  No global install or update command is used.
- Host `node --version` and `npm --version`: command not found.  The local
  dependency is therefore intentionally not installed on this host.

The package identity, binary mapping, version, engine requirement, extension
API, custom-provider API, RPC protocol, and CLI flags were checked against the
official `earendil-works/pi` `v0.84.4` tag before implementation.

## Process topology

```text
StepRequest
  -> PiStepWorkerBackend
  -> PiRpcProcess
  -> exact local pi --mode rpc subprocess
  -> explicit TypeScript extension
  -> deterministic streamSimple provider
  -> biomind_finish_step_poc (terminate: true)
  -> tool_execution_end + agent_settled
  -> private PiTerminalOutcomePayload
  -> existing StepOutcome validator
  -> existing LangGraph typed outcome edge
```

`bio_agents.py` only selects the backend and projects route-neutral transport
audit facts.  RPC framing and process logic are isolated in
`agent_core/workflow/pi_rpc.py`.

## StepRequest transfer and authority

The adapter serializes `StepRequest.to_dict()` with sorted keys, compact UTF-8
JSON, disallowed NaN values, and a SHA-256 over those exact bytes.  The prompt
contains schema version, that canonical request object, and the hash.  A
recursive fail-closed check rejects legacy routing keys.  The Pi process never
receives `AgentState`, `PlanState`, a Controller, or a recovery decision.

Pi can return only the five existing outcome types.  The private terminal
envelope requires exactly `protocol`, `schema_version`, and `outcome`.
`worker_session_ref` is forbidden in Pi output and is injected from the actual
`get_state.sessionId` only after the existing `StepOutcome` validation succeeds.
No backend output can select a graph node.

## RPC protocol and terminal semantics

- UTF-8 JSON objects are framed by byte LF only; one optional trailing CR is
  removed.  U+2028 and U+2029 remain JSON string data.
- Each command receives a fresh correlation ID.  Command responses are matched
  by ID and command name; asynchronous events are queued separately.
- A successful `prompt` response is only acknowledgement.
- `agent_end` is recorded but never completes a step.
- A successful normal result requires exactly one matching
  `tool_execution_start`/`tool_execution_end`, structured `result.details`,
  `isError=false`, and a later `agent_settled` event.
- Missing, duplicate, malformed, unexpected-tool, extension-error, early-exit,
  framing, correlation, and timeout conditions fail closed.
- The private command dispatcher enforces only `abort`, `get_messages`,
  `get_state`, `prompt`, and `switch_session`; `bash` is rejected before any
  bytes are written.

## Fake provider and terminating tool

The explicit extension registers provider `biomind-poc-fake`, model
`biomind-poc-model`, and exactly one tool, `biomind_finish_step_poc`.  Its local
`streamSimple` emits a deterministic tool call and performs no fetch, file I/O,
or subprocess operation.  The tool uses a closed TypeBox schema, returns the
strict structured details envelope, and sets `terminate: true`.  The CLI starts
with built-in tools, discovered extensions, skills, prompt templates, and
context files disabled; its tool allowlist contains only this terminating tool.

## Security isolation

Each attempt owns a distinct run root with separate agent, session, and fake
home directories.  `HOME`, `PI_CODING_AGENT_DIR`, and
`PI_CODING_AGENT_SESSION_DIR` point only at those directories.  The child
receives a small environment allowlist plus controlled POC variables; provider
keys, cloud credentials, and the user's normal Pi config are not forwarded.
The task did not inspect or print API key values.

Session IDs must be safe opaque filename components.  Session and agent paths
must resolve beneath the configured POC runtime root, and the session file must
remain beneath its isolated session directory.

## Session ownership and resume

`get_state` supplies the actual Pi `sessionId` and `sessionFile`.  The persisted
sidecar binds them to task ID, plan ID/version, step ID, attempt ordinal,
canonical request hash, and Pi version.  Resume validates all ownership and
storage fields, starts a new Pi process with the prior session file, checks that
the process reports the same session identity, and requires prior message/tool
history.  Unit tests cover cross-process shape and mismatch rejection.

Real session creation and cross-process reopen remain unverified on this host
because the pinned executable cannot be installed.  A dedicated append-only
StepWorker attempt ledger and production crash recovery remain later work.

## Timeouts and error mapping

Startup, response, settlement, and shutdown each have independent bounded POC
timeouts.  Pi transport/protocol failures map to the existing
`StepOutcome(type="harness_error")` with a narrow `PI_RPC_*` code.  There is no
fallback to `legacy`, Generate, Execute, Recovery, or route fields.  Python task
cancellation attempts RPC abort and child cleanup before re-raising;
`GraphBubbleUp` is not converted.

## Test evidence

- New PI-MIG-02 suite: protocol/backend tests pass; the real executable test is
  skipped with the explicit missing-local-package reason.
- The real acceptance test is already wired to spawn the local Pi executable,
  run a compiled LangGraph, corrupt all legacy route fields simultaneously,
  require the Supervisor edge, and reopen the persisted session in a new Pi
  process once the runtime is available.
- PI-MIG-01 focused regression: all current test methods pass.  The supplied N2
  method name no longer exists at this checkout; its current equivalent,
  `test_n2_equivalent_second_rejection_converges_without_increment`, passes.
- Python compilation and Git whitespace checks pass.
- TypeScript source invariants pass.  `tsc --noEmit` cannot run without Node and
  is therefore not reported as passing.

## External-call evidence

- Real Pi process: NO (environment blocker)
- Real Pi RPC: NO (environment blocker)
- Real Pi session persistence: NO (environment blocker)
- Real Pi custom extension/tool: NO (environment blocker)
- DeepSeek/OpenAI/Anthropic/Google/other model API: NO
- snpEff/Java/scientific commands: NO

The only network attempt was the explicit runtime download attempt; it failed
before a socket could be opened.  Tests use the local deterministic extension
or in-memory protocol fakes and make no model/provider requests.

## Known limitations and next slice

The POC remains an explicit opt-in; `legacy` is production default.  It has no
scientific filesystem/shell/Python/R tools, no BioMind skill, no Pi approval,
no binding/artifact mutation, no scientific PASS authority, and no durable
attempt ledger.  Supervisor still contains legacy Controller territory.

PI-MIG-03 readiness is `NOT_READY` until a compatible Node runtime is available,
the exact dependency can be installed locally with lifecycle scripts disabled,
TypeScript compilation passes, and the real Pi process/RPC/tool/session/compiled
LangGraph acceptance test passes without external model or scientific calls.
