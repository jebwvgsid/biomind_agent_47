# BioMind Pi runtime

This private package uses `@earendil-works/pi-coding-agent`, `pi-agent-core` and
`pi-ai` **0.87.1**, with the documented reversible dependency observer patch
**0.87.1+biomind-observer.1** (see `compat/README.md`). Native interactive mode launches the upstream fullscreen CLI;
JSON mode calls its public `createAgentSession`. Both load
`extensions/biomind.ts`. Pi owns model calls, tool continuation, retry,
compaction, messages and branches. Python owns authorization, execution
receipts, registered environments, files and memory storage.

The product entry `python -B bio_agents.py` uses **Pi**. `--engine pi` remains
an explicit spelling; the old BioMind agent loop and terminal are retired from
production under the current migration instruction. A startup failure is reported
as an error and never selects another engine. The current migration applies code
and physical retirement separately from the dynamic checks the user will run;
retirement does not certify those checks or online model behavior.

## Start in any project; use Pi's existing account settings

The user command `biomind` launches from the caller's current directory and uses
the existing private Python/Node runtimes; Conda activation is unnecessary.
Install the command once with `python scripts/install_user_launcher.py --apply`
from the repository (using the existing BioMind Python interpreter).

Ordinary native and JSON launches now use Pi's public `getAgentDir()` and native
credential storage. Pi defaults to `~/.pi/agent/auth.json` and already supports
`PI_CODING_AGENT_DIR`; BioMind adds no separate account configuration rule.
Changing project directories does not require entering the key again. The setup
script reuses the previously saved agent directory via a directory link, without
opening, copying or printing credentials or replacing existing Pi settings.
Project execution data stays under the project's `.biomind/pi-domain`, and Pi
continues to partition its native sessions by project. Explicit `agentDir` is
still supported; synthetic fixtures retain isolated, empty credentials.

Normal interactive startup now follows Pi's online startup behavior; BioMind
does not force `--offline` or `PI_OFFLINE=1`. Pi manages its own `fd`/`rg` tools
under the native agent directory's `bin` folder. An explicitly inherited
`PI_OFFLINE=1` is still respected, and synthetic provider fixtures remain offline.

## Build and launch

From `integrations/pi_runtime`, use the private Node installation
(22.22.0 was validated) or Node >=22.19.0:

```bash
export PATH="$PWD/.runtime/node/bin:$PATH"
npm ci --no-audit --no-fund
npm run build
```

From the BioMind repository:

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B bio_agents.py \
  --engine pi --pi-check
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B bio_agents.py \
  --engine pi --project-dir /absolute/scientific/project \
  --pi-config /absolute/nonsecret-runtime.json
```

An explicit nonsecret settings file can select `provider`, `model`,
`thinkingLevel`, `python`, `agentDir`, `dataRoot`, `installRoot`, `handshakeTimeoutMs`,
`registryPath`, `environmentInventory`, `preferencesPath`, `memoryRoot`, `wikiRoot`, `archivePath`,
`historyEnabled`, and `memoryMode` (`ask`, `auto`, `manual`, `disabled`).
`providerFixture` is reserved for declared synthetic tests. Do not put tokens
in this file. The product launcher maps its existing nonsecret model/software
configuration into this host; an explicit runtime config may override that
selection. Pi owns native authentication, and the domain bridge does not receive
provider credentials. JSON mode requires the selected provider/model or a
declared synthetic fixture. The Python bridge
receives a minimal execution environment without provider credentials.

The local Python service has a **60-second** startup handshake deadline to allow
slow dependency loading on shared storage. Set `handshakeTimeoutMs` in the runtime
JSON or `BIOMIND_PI_HANDSHAKE_TIMEOUT_MS` in the launch environment to override it
(integer milliseconds, 1–600000; JSON takes precedence). For example,
`BIOMIND_PI_HANDSHAKE_TIMEOUT_MS=120000 biomind` allows two minutes.
Startup timeouts report `DOMAIN_HANDSHAKE_TIMEOUT` and the actual limit. After a
failed startup, resubmit the input to create a fresh connection. Requests are not
replayed automatically, and services that completed their handshake are retained
when a request is rejected, so existing jobs keep their original owner.

History reads start disabled unless explicitly enabled. An existing preference
file still refreshes at request boundaries. Existing memory stores are opened
only through explicitly selected roots; old sessions are not imported into Pi.
An explicit `archivePath` enables the original public-observation archive writer.
History controls reads and memory use, not these audit writes. The extension
projects public text and tool results from Pi's current request; it does not
export private reasoning or maintain a second mutable conversation tree.
`archive_receipt` acknowledges enqueueing (`durable:false`), not a committed
database write. Python drains the original writer on a normal disconnect after
its independent jobs finish; an abrupt crash can lose queued observations.

## Interaction and transport

Ordinary requests start in Consult. It may answer and finish, ask and wait, or
use `handoff_generate` once. Generate can use optional `update_plan` notes and
domain tools. These notes do not grant execution authority or impose artifacts.
The handoff continues in the same Pi conversation: Generate is instructed to
advance the authorized task with tools, not merely announce a future execution.
Current role instructions are refreshed on each send; memory maintenance uses a
separate restricted instruction through its final receipt acknowledgement.
A settled conversation is not evidence that the requested analysis ran or
succeeded; execution claims must come from actual tool receipts and outputs.
Execution proposals use the existing Python authorization object and CAS.
The first native approval selection is refusal. Editing requires confirmation
of the new proposal. Built-in shell tools and the native `!` shortcut are not
alternate execution routes.

Approval dialogs show the working directory, environment and multiline command
without dumping internal hashes into the main view. `查看校验详情` reveals the
original proposal and its bookkeeping without recording an approval. The
original domain payload and offered decisions remain authoritative.

Choose the project boundary at startup with `biomind --project-dir /absolute/project`;
without this option the startup directory is used. Ordinary file tools access
this project and its descendants, with private/protected paths excluded and
separate host-issued read-only sources such as Skills. `/permissions` explains
the current project boundary and how to change it; it does not grant access.
There is no unified per-input read-only/per-output writable directory setting.
`BIOMIND_COMMAND_READ_ROOTS` belongs to the command service and does not expand
the ordinary `file_stat`/`read` boundary. Changing the project directory does not
approve commands or writes; verify relative input/output paths when restarting.

`/biomind-memory status`, `history on|off`, and `mode ask|auto|manual|disabled`
control the domain policy. `/biomind-job list|status|logs|cancel` observes or
explicitly cancels an existing handle without another model call. For status,
logs or cancel, supply the `run_ref`; pass the returned log cursor unchanged.

Use `--pi-mode json` for newline-delimited host frames:

```json
{"type":"prompt","requestId":"client-request-1","text":"Explain this result"}
{"type":"ui_response","id":"the-emitted-question-or-approval-id","value":"answer"}
{"type":"settings","id":"settings-1","history_enabled":false,"memory_mode":"disabled"}
{"type":"job","id":"job-1","action":"status","run_ref":"the-real-run-reference"}
```

Approvals use an explicit value `approve`, `deny`, or `cancel`. A proposal
offering directory authorization also accepts `scope`; installation proposals
accept `{"choice":"edit","replacement":{"cmd":"revised command"}}`, which
requires a new explicit approval. Only offered decisions are accepted. An ordinary
prompt, a stale ID, or a model argument cannot supply a trusted human decision.
The host emits `ready`, Pi `event` frames, correlated `question`/`approval`,
`settings_result`/`job_result`, and a final `result`. `businessRequestId` is
the extension's request identity; the external `requestId` identifies the
caller's input. Session and extension hashes accompany results.

`result.runtime_status` is the runtime outcome. `model_conclusion`,
`execution_facts`, `scientific_success:null`, and observed attempt usage
are separate fields. Runtime completion never establishes scientific success.
`usage` and `/biomind-usage` include observed failed/retried summary calls across
the session tree. `pi_entry_usage` and native `/session` retain Pi's entry-based
view; do not add these totals. Missing provider usage or cost remains unknown.

Skills have one content authority: the existing Python SkillManager, exposed
through `list_skills` and `read_skill`. The latter supports `relative_path`,
`offset` and `max_chars` for complete paged references/scripts under the Skill
directory, and preserves runtime metadata. Pi native Skill discovery is disabled.
Current valid human authorization references appear as read-only scoped context;
they never expose a permit or authorize a different action.

Tool display caches have explicit per-component byte/line limits. Full logs stay
with Python; model log pages retain the cursor, receipt and complete delta. The
domain transport bounds outstanding/queued requests and reserves cancellation
capacity. Persistent conversation history and full disk logs may still grow.

`testing/run-canaries.py --plan` checks the migration prerequisites without sends.
An explicit `--execute` uses the existing provider configuration only after the
required cases pass. A fixed SQLite ledger reserves each HTTP attempt before
dispatch: six per R1/R2/R3, eighteen total, including retries and summaries, with
no reset/refund across processes. These limits and the conservative request-size
caps apply only to this migration canary, not ordinary scientific work.

`steer` and `follow_up` delegate to Pi's queues; no durable delivery ACK is
promised. `abort` stops the conversation/waiter. `job/cancel` addresses one
actual run. EOF/`shutdown` disconnects and leaves independent Python workers
with their owner until terminal. A live owner restricts session changes;
`reload` keeps its process-local bridge. Process-crash or SSH reattachment is
not certified. JSON sessions currently use Pi's in-memory SessionManager;
native CLI uses its own newly created Pi sessions.

## Verification limits

Run `npm run check`; the migration delivery contains the exact commands and
separate SDK, Python domain, STDIO, native PTY and serializer evidence. Synthetic
provider success is not remote-provider acceptance. Fetch transport substitution
is not loopback HTTP/SSE acceptance. Native PTY injection is not an IME test.
The supplied lock and compat patch have a reproducible clean-install entry in the
migration delivery. Retain each actual install and regression result separately;
source migration, a build, or an older passing run is not a new full acceptance.
The current delivery records the user-run dynamic checks that remain pending.
DeepSeek online compatibility and real-model behavior need their own evidence.
