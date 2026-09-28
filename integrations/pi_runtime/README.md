# BioMind Pi runtime

This private package uses `@earendil-works/pi-coding-agent`, `pi-agent-core` and
`pi-ai` **0.87.1**, with the documented reversible dependency observer patch
**0.87.1+biomind-observer.2** (see `compat/README.md`). Native interactive mode launches the upstream fullscreen CLI;
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

After authorization to install dependencies, run the following **in a separate
checkout's** `integrations/pi_runtime`, using Node >=22.19.0 (the observed
toolchain is Linux x86_64, Node 22.22.0, npm 10.9.4). `npm ci` replaces that
directory's node_modules; do not use the production directory for restore tests.
The existing postinstall applies the pinned observer patch, without upgrading Pi:

```bash
export PATH="$PWD/.runtime/node/bin:$PATH"
npm ci --no-audit --no-fund
npm run check:compat
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
`registryPath`, `preferencesPath`, `memoryRoot`, `wikiRoot`, `archivePath`,
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

Shared Skills have one source tree at the installation's `skills/`. Pi's native
discovery, `/skill:name` expansion and `read` load their SKILL.md and relative
references/scripts. `read` uses line-based `offset`/`limit`; the old
`list_skills`/`read_skill` tools are not exposed. User Skills remain under Pi's
native discovery policy. Registered environment metadata stays in the Python
domain; discovering a Skill does not install scientific dependencies.
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
not certified. JSON and native modes use Pi's persistent SessionManager by
default. Ordinary startup creates a new session; `--session` selects an exact ID
or file, and `--continue` explicitly selects the latest session in that project.
`--no-session` is temporary. Session restore does not take over an existing job.

## Product identity and development baseline

`integrations/pi_runtime/package.json` → `version` is the single version authority
for BioMind's jointly delivered source, host, domain service and shared resources.
The lockfile root mirrors it. The package remains private; a version declaration
is not a release or an installation-integrity certificate.

```text
biomind --version
BioMind 1.0.0-dev.1
Source: <full commit SHA or unknown>; worktree: not checked
Pi declared: 0.87.1
Pi patch expected: 0.87.1+biomind-observer.2; verification: not checked
```

The query returns before dotenv projection, model selection, Node checks, domain
initialization or state creation. It reads the installation manifest regardless
of the caller's project directory. It still works without Node/node_modules or
Python domain dependencies. A missing/invalid product manifest exits nonzero.
Git metadata is optional: only an installation with its own `.git` marker gets
one `rev-parse` call, limited to one second and checked against its own root.
An export inside another repository cannot borrow that parent's SHA. Git failure
means `unknown`; worktree/patch verification always remains `not checked` here.
No status/describe-dirty/full-tree hashing runs in this query or ordinary startup.
To verify installed patch bytes explicitly, use the existing `check:compat`.
The JSON ready frame's existing `version` remains an engine/observer label.

The candidate generator defaults to the same manifest version:
`python -B scripts/generate_pi_package_candidates.py --output /new/candidate-dir`.
Its existing `--version` is an explicit version for those candidate archives; it
does not change BioMind's product manifest. It follows the extension's JS imports
and excludes dist miscellany such as `host-diagnostic.mjs`; build in the separate
checkout first. Skills/extension archives are not a self-contained product, do
not include the Python service, and do not lock a downstream npm consumer's tree.

### 1.0.0-dev.1 — 2026-09-28

Save the current Pi migration and related legacy retirement as a local source
baseline; add the offline version query, align candidate defaults/lock root,
and declare the Python domain's direct dependencies. No Pi upgrade, tag or
publication is implied. The independent workbench, Skill script versions,
domain protocol and model configuration schemas keep their separate identities.

The current development scope is Linux server startup, Consult/Generate,
native Skills/read, approved file/command operations, explicit observation and
cancellation, and new-project Pi sessions. The selected offline tests use a
scripted provider. Real-model/TUI acceptance, scientific correctness, all-provider
accounting, old-session migration, desktop/Orca and broad workflow performance
are not certified by this development baseline.

## Source restore and dependency restore are separate

Use a distinct checkout/export of the recorded full commit; preserve the running
directory and user links. Python 3.12.12 was observed. `requirements-cli.txt`
pins the current direct domain/launch dependencies (langchain-core, packaging,
python-dotenv), not their complete transitive resolution. Test tools such as
pytest and optional scientific environments are separate. After installation
authorization, use a fresh environment without system-site-packages, install
only those dependencies and the required test tools, and record the resolved
transitive versions. Do not freeze or reinstall the production Conda environment.

For isolated validation, invoke the new checkout's `bin/biomind` or `bio_agents.py`
directly, not an old user command. Set `BIOMIND_PI_PYTHON` for the shell launcher
and explicitly select the domain Python via the existing `python` config field
or that same environment variable; `--pi-node` selects the Node entry. Confirm
the resolved host, extension, domain service and Skills all belong to the new
checkout. Isolate HOME, agentDir, project, configuration and test output. Existing
dependencies may be reused for an offline source check, but that is not a clean
dependency restore. No dependency installation is performed by `--version`.

The observed user `~/.local/bin/biomind` links to this checkout's launcher, and
`~/.pi/agent` links into this runtime's `.biomind/pi-agent`. Do not change these
links during a restore test. Credentials, user sessions, configuration, private
memory, registry state, project `.biomind/pi-domain`, inputs and results are not
source-code artifacts. Tags would not restore or downgrade them. Do not point
older code at newer user state without checking format compatibility.

Future release preparation explicitly reviews selected source/resource paths,
remaining dirty, dependency/patch recovery and tests on the exact commit. Only
after separate release authorization should an immutable annotated tag be made;
never retag a development/rc manifest as final without updating and validating
its metadata. Real user acceptance and any push/Release remain separate steps.

## Verification limits

Run `npm run check`; the migration delivery contains the exact commands and
separate SDK, Python domain, STDIO, native PTY and serializer evidence. Synthetic
provider success is not remote-provider acceptance. Fetch transport substitution
is not loopback HTTP/SSE acceptance. Native PTY injection is not an IME test.
The supplied lock and compat patch define the dependency restore steps. Historical
migration evidence is not this development commit's clean-install validation.
Retain each actual install and regression result separately; source migration,
a build, or an older passing run is not a new full acceptance.
The current delivery records the user-run dynamic checks that remain pending.
DeepSeek online compatibility and real-model behavior need their own evidence.
