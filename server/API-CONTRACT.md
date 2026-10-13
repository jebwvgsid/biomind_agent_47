# Existing BioMind server contract

This adapter uses the existing S2/S3 Next backend. It does not create an SDK Agent, upgrade Pi, translate native history into a second database, or introduce another memory store.

## Authentication and service lifecycle

HTTP requests use `Authorization: Basic base64("pi:" + password)`. Desktop transport keeps this header out of renderer-controlled URLs. The backend listens on compute-node `127.0.0.1` only; Slurm mode requires SSH to the discovered compute host through the configured login host. Forwarding from the login host directly to `compute-host:port` will not reach loopback.

```
python3 /absolute/server-service.py ensure --config /absolute/service-config.json
python3 /absolute/server-service.py discover --config /absolute/service-config.json
```

The first `ensure` persists launch intent under a file lock, submits at most one job or detached local holder, and returns immediately. Repeated `ensure` attaches to an active or uncertain launch. The desktop polls `discover` while pending/starting. It does not retry submission on unknown status. Only the scheduler invokes `run --config ... --instance ...`. Closing Windows or SSH never calls stop/scancel.

Results contain `schema`, `serviceId`, `serverId`, `instanceId`, `serviceMode`, `transport` (`jump` for Slurm; `direct` for local), `user`, `status`, optional `jobId`, `host`, `port`, `code`, and `reused`. `host`/`port` come from the current holder and kernel-selected listener. `ready` means the complete Next handler loaded, its actual authenticated `/api/agent/profile` returned the configured BioMind identity and authorized projects, and the holder's identity-checked health probe is fresh. It does not assert that a BioMind session has initialized. Session send controls remain disabled until that session's real `ready/state` exists.

Status values: `pending`, `starting`, `slow-start`, `ready`, `unhealthy`, `stopping`, `stopped`, `failed`, `unknown`, `configuration-changed`, `error`. Slow startup leaves the same holder alive. Scheduler uncertainty, stale heartbeats and identity mismatch never produce ready. Configuration changes while a recorded instance exists are reported; this manager has no destructive reset command.

The example configuration has placeholders and must be bound to the existing installation before use. The audited compatible complete Main is `BioMind_Studio_S2_S3_Implementation_20261008/candidate/Biomind_v1`, including its existing JSON transport, controls and native history helpers; the root `Biomind_v1` is a different source state and must not silently substitute for this candidate. This is an adapter compatibility requirement, not an engine upgrade. `environmentFile` is a server-owned JSON string map with permissions 0600. It supplies `PI_WEB_PASSWORD` and any already-required deployment environment without copying credentials into desktop source/configuration. Keep the existing Main runtime config, native session root, instructions, skill registry, memory paths and authorization setup. No `PI_OFFLINE` or fixture provider is introduced by this service. This task did not activate a real provider or read protected credential files.

## First-run configuration

The Windows installer uploads only `server-service.py`, `next-service.mjs` and `configure-service.py` to the selected private service directory. It invokes `python3 configure-service.py --config /absolute/private/service.json`, sends bounded setup JSON on stdin, and does not put the workbench password in argv or logs. Setup schema is `BioMindStudioConfigureV1`, with `backendRoot`, `mainRoot`, `runtimeConfig`, `sessionRoot`, `pythonPath`, `nodePath`, `projectRoots`, `dataRoots`, `uploadsRoot`, `mode`, optional `slurm`, optional `modelRecordPath`, and `workbenchPassword`. Optional service/server IDs are stable identities, not reset switches.

The configurator reads only the explicitly selected non-secret runtime/model-record metadata JSON, refuses credential/.env paths including their resolved symlink targets, and does not open referenced auth, API key, session, or memory files. If runtime has `sessionDir`, its existing resolved directory must equal `sessionRoot`. Otherwise runtime must explicitly select an existing `agentDir`, and `sessionRoot` must equal its existing `sessions` directory. In the latter case native Main continues choosing per-project subdirectories: it does not force the global history root into `sessionDir`. `modelRecordPath` is validated against the runtime's provider/model/thinking and passed as `BIOMIND_PI_MODEL_RECORD`, allowing the existing Main to register its custom provider through the original resolver.

The generated private `runtime-environment.json` contains **only** the workbench HTTP password. Configuring does not change API credentials, native sessions, memory or instructions, and does not invoke a model. Identical configuration is reused. A changed config requires a confirmed stopped/failed holder; active/uncertain ownership or a changed identity is refused, with backups retained for allowed updates. Result is `{status:"configured",configPath,serviceId,serverId,reused,apiCredentialsRead:false,modelStarted:false}`; failures have `{status:"error",code}`. Runtime scope checks do not imply valid provider credentials or an accepted Windows build.

`setup-preset.json` is a non-secret convenience preset audited for this workspace, with `{schema:"BioMindStudioSetupPresetV1",profile,setup}`. The password comes from first-run Windows input. See `../deployment/README.md` for native-session and memory provenance; the preset selects the current TAD project's established Linux directory and can be changed in the GUI.

## Projects, sessions and history

| Request | Response / behavior |
| --- | --- |
| `GET /api/agent/profile` | `{profile:"biomind",serverId,protocolVersion:1,allowedProjects,capabilities}` |
| `GET /api/projects/registered` | `{projects:string[]}` containing server-authorized roots |
| `GET /api/cwd/browse?path=...` | `{path,parentPath,directories:[{name,path}]}`; omitted path lists authorized roots |
| `POST /api/cwd/validate` `{cwd}` | Validates a server Linux directory |
| `GET /api/sessions` | `{sessions,runningSessionIds,registeredProjects,historyAvailable,historyError?,historyTruncated?}` |
| `POST /api/agent/new` `{cwd,type:"ensure_session",serverId}` | Creates one real BioMind owner; returns `{success,sessionId,serverId,project,ownerGeneration,capabilities,model,...}` after actual initialization |
| `GET /api/agent/:id` | Loaded: `{running:true,state}`; unloaded: `{running:false,readOnly:true,resumable,resumeReason,automaticReplay:false}` |
| `POST /api/agent/:id` `{type:"resume_session",serverId,project,sessionId}` | Explicitly opens the same authorized native history file. Returns `{success,data:state,resumed}` and verifies unchanged native session ID. Never substitutes a new session. |
| `GET /api/sessions/:id` | Native history projection `{sessionId,filePath,info,leafId,tree,context,...}`; `context.messages` is authoritative history |
| `GET /api/agent/:id/events?v2=1` | SSE JSON events; no owner creation in remote mode; disconnect removes only this listener |

Session rows contain `{id,path,cwd,created,modified,messageCount,firstMessage,name?,resumable?,resumeReason?}`. Group them by server `cwd`/project. Switching selected session is a display/subscription operation, not `abort`, `destroy`, or `ensure_session`. Keep each session's events keyed by native ID; do not send the active tab's project to another owner.

Every loaded state includes `serverId`, `sessionId`, `project`/`cwd`, `ownerGeneration`, `model`, `capabilities`, native execution fields including `active`, `pendingUI`, `displayPendingUI`, `admissions`, and queue information. The adapter also supplies `isStreaming`, `isPromptRunning`, `isCompacting`. Do not infer idle from missing state or a broken SSE connection.

The audited original backend had no in-flight message snapshot. This delivery's independent `backend/pi-studio` source adds `state.liveSnapshot = {sessionId,ownerGeneration,sequence,active,truncated,events}` to both HTTP and SSE state. Every streamed event has monotonic `transportSequence` within its owner generation. Snapshot events are sorted by that sequence, contain only the current request, and retain whole `message_update.message` without its redundant delta plus current tool start/update/end. Install the snapshot and then apply buffered events with sequence greater than its watermark. Merge native history by stable message identity; do not append the same snapshot on every status poll. Subscribe before reading state, as the existing SSE route does. A `result` or idle state clears the ephemeral snapshot; refresh native history then. The buffer is strictly in-memory, bounded to 512 events/2 MiB with a 512 KiB per-event limit. `truncated:true` explicitly signals that some live recovery content was omitted; finalized native history remains authoritative. No second session or memory store is introduced.

For deployment, point `BIOMIND_STUDIO_ROOT` and service `cwd` to this delivery's **new** `backend/pi-studio`, and use its freshly built `.next-pkg`. The old backend build does not include live recovery. `preflight.py --config ...` inspects configured paths and credential-file permissions only. It never opens the credential file, connects to a network, starts a service/model, or labels Windows/model checks as passed.

## Input admission, reconnect and extension UI

Send exactly once to `POST /api/agent/:id`:

```json
{"type":"prompt","message":"task","serverId":"...","project":"/linux/project","sessionId":"native-id","ownerGeneration":"...","clientMessageId":"persisted-uuid"}
```

`steer` and `follow_up` use the same scope and admission contract. Optional `images` are actual `{type:"image",mimeType,data:base64}` bytes. HTTP frame bound is 10 MiB, with additional owner-reported image/model limits. No Windows pathname is an image payload.

Success is `{success:true,data:{type:"accepted",status:"accepted",clientMessageId,ownerGeneration,sessionId,...}}`. Persist `clientMessageId` and content digest before POST. If response or connection is lost, refresh state and look up `admissions` for that ID. Unknown admission or owner generation change stays unknown and query-only; never automatically resend the old task. Existing `lib/biomind-client.ts` implements this contract and should be reused.

SSE streams Pi message/tool events plus BioMind `state`, `extension_ui_request`, `extension_ui_resolved`, `owner_unavailable` and `resync_required`. On reattach subscribe and read back state/history. Pending UI must preserve server-issued IDs and authority. Response command is `type:"extension_ui_response"` plus original `id`, `sessionId`, `requestId`, `businessRequestId`, `ownerGeneration`; authorization uses explicit `confirmed:boolean`, other requests `value:string`, cancellation `cancelled:true`. The server reconstructs and verifies authority from its own original request.

## Owner controls

Controls use `POST /api/agent/:id` with `{type,serverId,project,sessionId,ownerGeneration,...fields}`. Read fresh state before mutation, check owner scope and capability, send once, and read back after uncertain results. Returned frames are under response `data`; capability payloads commonly use frame `data` again.

| Command | Fields | Result frame |
| --- | --- | --- |
| `get_models` | none | `models_result`, `data:{models,current,thinkingLevel,availableThinkingLevels,source}` |
| `set_model` | `provider,modelId` | `model_result`; re-read state to display actual selection |
| `set_thinking_level` | `level` | `thinking_result` |
| `get_tools`, `set_tools` | `tools` for setter | `tools_result` |
| `get_commands` | none | `commands_result` |
| `get_resources` | optional `resourceId` | `resources_result`; existing loaded skills/extensions and project trust |
| `set_skill` | `resourceId,scope,enabled` | `resources_result` |
| `get_instructions` | `scope` | `instructions_result` |
| `save_instructions` | `scope,expectedVersion,text,create` | `instructions_result`; server path/version semantics |
| `get_prompt_snapshot` | none | `prompt_snapshot_result` |
| `set_auto_compaction` | `enabled,scope` | `auto_compaction_result` |
| `settings` | `history_enabled,memory_mode` | `settings_result`; the existing server Main settings |
| `permissions` | `action,grant_id,request_scope,policy_revision` | `permissions_result`; existing BioMind authority |
| `job` | `action,run_ref,after_cursor` | `job_result` from existing BioMind jobs |
| `reload`, `compact`, `abort` | compact may include `customInstructions` | Corresponding result frame |

Unsupported Pi features must be disabled with reason rather than invoking a second local agent. Ordinary `/api/settings`, SDK model catalogs and plugin installers are not allowed in BioMind profile. Skills are server resources; upstream plugin package management is a separate feature and is not claimed as inherited.

## Files and attachments

`GET /api/files/<encoded absolute Linux path segments>?type=list` returns `{entries,path}`. `type=read` returns `{content,language,size,truncated,previewBytes}` for text, but actual bytes and MIME for images/audio/documents. `type=download` returns bytes, `type=meta` metadata and `type=preview` supported document preview. Respect content type. Remote profile refuses unrestricted local allow-root, reveal, file-save and watch endpoints.

`GET /api/file-index?cwd=/linux/project` returns `{files:string[],truncated}`; `q=...` returns `{matches:[{path,isDir}]}`.

Upload actual bytes with multipart `POST /api/uploads`, one `cwd` field and one or more `files`. Maximum complete request: 32 MiB. Response `{uploaded:[{name,sourceName,inputIndex,path,size,kind}],errors,project,serverId}`; HTTP 207 indicates partial success. Keep returned server paths in textual attachment references, and send image bytes through the owner image contract when model image input is supported. Never pass local Windows paths to Linux.
