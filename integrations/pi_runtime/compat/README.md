# Pi 0.87.1 + BioMind observer compatibility patch

This runtime is **0.87.1+biomind-observer.2**, not unmodified upstream.
The original negative P15.d artifacts remain historical evidence.

`pi-0.87.1-observer.patch.json` contains exact, unique replacement anchors and
SHA-256 checksums for both original and patched files. It changes the
ModelRuntime provider dispatch hook, its ModelRegistry registration method,
and records the exact fetch/dispatcher pair created by Pi's existing HTTP setup.
When canary environment flags are present, dispatch fails before provider entry
unless that hook is installed. The host verifies all patched hashes before
creating a session, because Pi may otherwise report an extension-load error and
continue running without that extension.
Provider composition, authentication, headers, model catalog, retries, loops,
compaction and stream event delivery remain owned by Pi.

Both execution layouts are patched: the SDK's `dist/core/model-runtime.js` and
`dist/core/model-registry.js`, `dist/core/http-dispatcher.js`, and the CLI's actual bundled
`dist/bundle/chunks/chunk-OJP47DM6.js`. Patching the unbundled CLI entry alone
would miss the published `bin.pi` path.

After the locked install (`npm ci`), the package `postinstall` runs:

```
node compat/apply-pi-observer.mjs
node compat/apply-pi-observer.mjs --check
```

For an isolated dependency root containing the same lock and package:

```
node compat/apply-pi-observer.mjs --runtime /absolute/isolated/runtime
```

To restore the exact upstream bytes:

```
node compat/apply-pi-observer.mjs --revert
```

The helper first verifies every file and refuses unknown bytes/version. Apply
and revert are idempotent. Its JSON output includes the patch hash and the
resulting per-file hashes. No installation or network request is performed by
the helper. Restoring upstream also requires disabling this BioMind observer
adapter; its session hook otherwise reports the missing compatibility patch.

`usage-observer.ts` records only identifiers, operation type, terminal usage,
and HTTP origin/status. URL paths and queries are excluded because a configured
gateway may embed a secret there. It does not retain headers, credentials, prompts,
or response text. It observes the provider's independent `result()` promise,
without consuming, copying or replaying events. The authoritative ledger is
persisted as custom Pi session entries across the entire session tree;
`/biomind-usage`, the `biomind:usage` event, and SDK host `usage` all use it.
Native `/stats` remains Pi's entry-based view, returned separately by the SDK
as `pi_entry_usage`; these two views must never be added.

Audited HTTP attempt accounting wraps the original, identity-checked Pi dispatcher
with a synchronous reservation immediately before each dispatch. It preserves
Pi's fetch and EnvHttpProxyAgent, including its existing proxy configuration.
Canary per-call/custom dispatchers and replaced fetch implementations are refused.
This lower boundary also covers Undici's internal HTTP 421 recovery: manual
redirect mode alone does not prevent that resend. Each dispatch gets a separate
ID and terminal result. Successful SSE is parsed only by Pi.
Ordinary sessions also observe this trusted dispatcher with no canary quota or
payload cap. SDK host initialization now calls the same Pi HTTP setup as the
native CLI. Other/custom fetch paths remain explicitly partial: their outer
fetch invocations are separate from `physicalSends`, and `transport_coverage`
does not assert complete wire accounting. Legacy .1 session entries have that
same conservative partial classification on reload.
For failed OpenAI-compatible JSON responses, fallback fetch observation uses a
bounded 64 KiB clone to extract reported usage that Pi's retry helper otherwise
discards. Trusted dispatch observation captures at most 64 KiB of failed JSON response bytes (including
bounded gzip/deflate/br decoding); incomplete/unsupported bodies stay unknown.
Terminal provider
usage belongs to its final physical attempt and replaces, rather than adds to,
the matching HTTP report. Earlier failed responses retain their own usage.
Absent/all-zero default usage is conservative `unknown`, and missing cost is
unknown rather than a fabricated zero bill. Nested tool usage is not re-added.

The migration canary permits only the audited `openai-completions` HTTP path
and rejects arbitrary custom stream implementations, native provider overrides,
other APIs or non-SSE transports before dispatch. This is a declared narrow
coverage boundary, not an all-provider transport guarantee. Product usage
observation remains enabled on other Pi provider paths, with absent physical
observations explicitly counted. Canary fetches use manual redirect handling so
an automatic redirect cannot produce an unreserved extra request. Canary cache warming is stopped through Pi's
existing hook; normal product retry/loop policy is unchanged. Persistent canary
limits and cancellation/unknown reservation semantics live in `canary-budget.ts`
and `canary_ledger.py`.

For this migration canary only, the final serialized JSON body is limited to
10,000 UTF-8 bytes (a deliberately conservative input capacity bound, not an
assertion that a model tokenizer measured exactly 10,000 tokens), and the output
limit is at most 2,048 tokens, also respecting the model's smaller capacity.
The body check runs after provider/extension payload transforms and before quota
reservation on every send, including hidden retries and summaries. It rejects
oversized input, missing/excessive output caps, and multiple completions; no
facts are truncated. Rejections are recorded separately and do not count as
physical sends. Normal product sessions do not receive these limits.

Canary tool exposure excludes installation, memory tools and optional plan
updates; optional memory maintenance and cache warming are disabled. R2 exposes
only ask_user, handoff_generate, read_file, file_stat and write_file, supporting
its approved clarification, one handoff and tiny explanation-file scenario.
R3 retains the execution, observation, cancellation, environment, Skills and
read-file interfaces; its tiny scientific output is generated by run_command,
so write_file is excluded. These tools retain their original implementations.
This smaller catalog leaves capacity for the minimal synthetic canary dialogue;
longer histories still fail closed at the same body limit instead of silently
losing context. Normal product tool exposure is unchanged.

Logical-operation labels follow public Pi session events. An aborted/failed
branch navigation has no public terminal tree event in 0.87.1; the next normal
session prompt clears its stage through `input`. A raw SDK `completeSimple`
invocation made directly after that cancellation, without a session input
boundary, may retain the prior branch-summary label. This does not remove or
refund its physical send reservation or usage, but is not complete standalone
SDK operation-label attribution. No replacement lifecycle loop is introduced.

The offline fixtures exercise actual SDK and bundled provider serialization,
response parsing and hidden retries. The 421 fixture uses the locked Undici fetch
implementation and a synthetic lower dispatcher, proving reservation at each
resend without sockets. It does not prove remote receipt or billing. The separate
real-loopback 421 test retains a FAIL when socket permissions prevent it from
running; it never falls back to a mock.

Upgrading from observer .1 requires its original helper/manifest to revert the
three .1 target files first, then applying/checking this four-file .2 manifest.
The .2 helper intentionally refuses mixed .1/.2 bytes. Reverting .2 restores all
four upstream files; restore a matching source tree/package manifest and rebuild
before enabling its entrypoint. Restoring source alone does not undo dependencies.
