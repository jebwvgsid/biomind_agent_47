# BioMind Pi runtime

This private package uses `@earendil-works/pi-coding-agent`, `pi-agent-core` and
`pi-ai` **0.87.1**, with the documented reversible dependency observer patch
**0.87.1+biomind-observer.5** (see `compat/README.md`). Native interactive mode launches the upstream fullscreen CLI;
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

The build also emits `dist/biomind-extension.ts` for the native CLI. This bundles
BioMind's local modules with esbuild; Pi and other package dependencies stay
external. The `.ts` entry lets Pi's extension loader reuse its embedded public
SDK instead of importing a second SDK tree through an ESM `.js` entry. Keep the
bundle directly under `dist`, since runtime file paths resolve from there.
Use the complete `npm run build` after source changes; running `tsc` alone does
not refresh the native bundle. JSON mode keeps the ordinary compiled extension.

After selecting a conversation, interactive startup displays an initialization
message while Pi loads and binds the execution profile. Python's legacy
LangChain message classes load only when their legacy guidance or Wiki LLM
operations actually need them. Profile binding still completes before tools
become available.

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
failed startup, use `/reload` explicitly to retry initialization before submitting
new input. Requests are not
replayed automatically, and services that completed their handshake are retained
when a request is rejected, so existing jobs keep their original owner.

### Project selection and startup failures

`biomind` keeps a legal launch directory as its project. From HOME, interactive
startup first offers the existing project/open/create selector. Cancellation,
`--help`, and `--version` do not start the scientific backend. Explicit
`--project-dir` values use the same directory validation as the backend; HOME is
still prohibited as an analysis root. Noninteractive invalid roots return an
explanatory error instead of opening a menu. Exact `--session` metadata selects
the owning project, and an explicit conflicting project is rejected. A trusted
project's existing custom session directory remains a locator fallback.

The welcome view advertises analysis only after the backend handshake and
execution binding succeed. A lost connection immediately revokes admission;
structured initialization errors take precedence over EOF, while bounded,
redacted stderr and exit details are collected until process close. JSON output
keeps the existing frame protocol and never contains welcome text.

The pinned Pi patch handles errors on the actual terminal input stream using
its existing stop/dispose lifecycle. BioMind suppresses upstream update commands
only in its own child process, retains Pi's configuration and authentication
paths, and generates recovery instructions from an existing persisted session
and its project. Restoring conversation records does not automatically resume
scientific jobs. Input-EIO injection verifies cleanup, not the original incident's
unknown trigger. See `../../docs/fixes/startup_boundary_20261008.md` for evidence.

History reads start disabled unless explicitly enabled. An existing preference
file still refreshes at request boundaries. Existing memory stores are opened
only through explicitly selected roots; old sessions are not imported into Pi.
An explicit `archivePath` enables the original public-observation archive writer.
History controls future retrieval and optional maintenance, not replay of
already accepted conversation content or these audit writes. The extension
projects public text and tool results from Pi's current request; it does not
export private reasoning or maintain a second mutable conversation tree.
`archive_receipt` acknowledges enqueueing (`durable:false`), not a committed
database write. Python drains the original writer on a normal disconnect after
its independent jobs finish; an abrupt crash can lose queued observations.

## Interaction and transport

The terminal shows `本轮运行 01:23` above the editor while Pi is active, then
keeps `本轮用时 01:23` after settlement. The clock updates once per second and
includes tools, retries, compaction and queued continuations in that activity.
The next activity resets it; switching sessions or reloading clears it. It is
elapsed time in this process, not the duration of independent background jobs.
`/status` also shows this clock.

The footer displays Pi's estimated current context as `上下文 38.1k/312k`
(used/capacity, `k = 1000` tokens), instead of a percentage/capacity pair.
After compaction, unavailable usage stays `?/312k` until Pi can estimate it
again. Cumulative input/output, cache counters, `CH` (latest response cache-hit
percentage), cost, model and extension status remain visible as space permits.
History and context calculations are cached between session/leaf/model changes,
so clock ticks do not repeatedly scan the conversation. The custom footer uses
Pi's public `setFooter` and context APIs; it omits the native `(auto)` display
marker because the current compaction setting has no public live getter.
Pi still owns automatic compaction and its settings.

Disabling memory retrieval or forgetting a Wiki source does not rewrite earlier
tool arguments/results, assistant responses, plans, or summaries already in the
selected Pi conversation. BioMind no longer propagates source tags to redact
that history or reconstruct messages that Pi has compacted away. Pi's compactor
keeps its previous summary and file references, using its existing templates for
both manual and automatic compaction. The preparation hook decodes obsolete
Consult/Generate handoff instructions as historical facts and adds the editable
BioMind focus described below; it does not query memory policy. Same-request branch
summaries also remain available when retrieval is off. Explicit new-task
boundaries, branch selection, current project instructions, execution permissions
and checks on future memory reads/writes retain their existing behavior.

`prompts/compaction-bioinformatics-v1.txt` is the editable UTF-8 “BioMind 生信任务
压缩增补 v1” resource. `compaction-addendum.ts` resolves it relative to the installed
runtime and reuses `readEditableFile`, reading only this one file for each summary
operation. Edits apply to the next operation without rebuilding; an empty file
disables the default focus while preserving the caller's custom instructions.
Missing/unreadable files are reported through Pi's existing extension-error path.
This file is neither a main-agent system prompt nor a project/Skill instruction.

The production `session_before_compact` hook returns the default focus followed
by the current `/compact` instructions. Pi's default history, previous-summary
update and split-turn prefix prompts receive this through `Additional focus`.
Manual, automatic threshold and overflow recovery all use the same native
summarizer. The `session_before_tree` hook uses Pi's already-supported result to
append the same focus only when the user requests a branch summary. An explicit
native `replaceInstructions:true` remains a user choice, never the default here.
No composed instructions are saved back to the event or accumulated across
operations; Pi retries the already-composed request unchanged.

Pi 0.87.1 lacks a compaction-hook instruction return field and split-prefix
forwarding. Observer compatibility revision `.4` adds only that parameter bridge
in the SDK and the actual CLI bundle, plus its declaration. It does not replace
Pi templates or change selection, token budgets, models, retries, cancellation
or session persistence. See `compat/README.md` for the reproducible patch and
`../../docs/changes/pi_compaction_addendum_v1_20261004/README.md` for acceptance
evidence. Offline synthetic-provider wiring tests and real-model quality tests
are reported separately.

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
ordinary interactive `biomind` offers a project menu from any launch directory.
The launch directory remains the default for noninteractive use. Ordinary file tools access
this project and its descendants, with private/protected paths excluded and
separate host-issued read-only sources such as Skills. `/permissions` explains
the current project boundary and how to change it; it does not grant access.
There is no unified per-input read-only/per-output writable directory setting.
Consult and Generate both expose Pi's `ls`, `find`, and `grep`; `rg` is an alias
of `grep` with identical parameters. `ls` lists directory entries, `find` matches
filename/path globs, and `grep` searches text before `read` loads full context.
The adapter reuses Pi schemas, renderers, listing/glob result formatting and the
native ripgrep implementation. Directory metadata and each searched file use
BioMind's existing read authorization. Native grep receives an authorized open
file descriptor, so its subprocess cannot traverse an unchecked directory.
Search respects `.gitignore`/`.ignore` within the search root (and `.rgignore`
for grep), skips protected paths, links, `.git` and `node_modules`, and reports
incomplete scans. It does not import global or ancestor ignore files outside
the search root. Listing is bounded to 4096 inspected entries/256 KiB per
directory; recursive search to 10000 entries. Grep scans at most 1000 files,
32 MiB per file and 128 MiB total, with a 30-second tool deadline. Large skipped
files require bounded inspection through `run_command`. Readable directory
grants are reused; discovering files does not grant access or enable shell tools.

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
default. Ordinary interactive `biomind` and `biomind project` first show a project
menu regardless of the launch directory. Choose a known project, open another
existing directory, create a project, or browse all saved conversations directly.
The history entry starts in Pi's **All** scope; selecting a conversation restores
its own project before initialization. All means the configured Pi history store,
not a search of arbitrary files on disk.

After choosing a project, the actions offer **新建对话 / 继续最近对话 / 选择历史对话**
when history exists, or **新建对话** for an empty project. Both offer **返回项目选择**.
Esc from history returns to the actions; Esc from the actions returns to projects;
Esc at the project menu exits. Opening a directory does not create AGENTS.md;
creating a project asks before creating it and missing instructions.

An explicit `--project-dir` or `project open` skips project selection, retaining
the existing session menu for projects with history and direct startup for empty
projects. Explicit `--session`, `--continue`, `--no-session`, or `--prompt` skips
automatic menus. JSON and non-TTY startup also bypass automatic menus.

Use `biomind --resume` (or `biomind -r`) to open Pi's searchable history selector
directly. It initially lists the current project; without an explicit project,
Tab can switch to all projects and a selected session restores its own project
before runtime initialization. An explicitly selected project limits the list
to that project. `biomind project open /absolute/project` also offers the startup
menu when that project has saved history.

```bash
biomind --resume
biomind --project-dir /absolute/project --resume
biomind --project-dir /absolute/project --continue
biomind --session /absolute/path/to/session.jsonl
```

`--session` selects an exact ID or file, `--continue` selects the latest session
in the project, and `--no-session` is temporary. These options and `--resume` are
mutually exclusive. `--resume` requires an interactive terminal and cannot be
combined with `--pi-mode json` or `project list/create`; `project` (the menu) and
`project open` support it. The existing in-chat `/resume` command and its project,
execution-profile, and active-job checks remain unchanged. Session restore does
not take over an existing job. Pi conversation history browsing is independent
of the domain memory `historyEnabled` setting described above; legacy BioMind
conversations are not imported automatically.

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
