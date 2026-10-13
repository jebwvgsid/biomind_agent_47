import { packageManagementActive } from "./biomind-package-gate";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";

// This adapter owns transport only. BioMind owns model execution, UI authority,
// admissions, Pi queues, persistence and jobs. No SDK session is constructed here.
export type BioMindFrame = { type: string; [key: string]: unknown };
type Listener = (event: BioMindFrame) => void;
type Pending = {
  resolve: (value: BioMindFrame) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  command: string;
  clientMessageId?: string;
};
export class BioMindTransportError extends Error {
  constructor(readonly code: string, readonly rejected = false) { super(code); }
}
export function bioMindErrorBody(error: unknown) {
  const known = error instanceof BioMindTransportError;
  return { error: known ? error.code : "BIOMIND_REQUEST_FAILED", code: known ? error.code : "BIOMIND_REQUEST_FAILED",
    rejected: known && error.rejected, ...(!(known && error.rejected) ? { admission: "unknown" } : {}) };
}

export interface BioMindLaunch {
  python: string;
  entrypoint: string;
  config: string;
  pipeLauncher?: string;
  cwd: string;
  serverId: string;
  /** Server-resolved, authorized native session only; never sourced directly from an HTTP path. */
  sessionFile?: string;
  env?: NodeJS.ProcessEnv;
  spawnChild?: typeof spawn;
  timeoutMs?: number;
  changed?: () => void;
}

const MAX_FRAME_BYTES = 12 * 1024 * 1024;
const MAX_INPUT_BYTES = 10 * 1024 * 1024;
const MAX_EDITOR_PREFILL_BYTES = 64 * 1024;
const MAX_REQUESTS = 32;
const MAX_LISTENERS = 32;
const MESSAGE_COMMANDS = new Set(["prompt", "steer", "follow_up"]);
const CONTROL_COMMANDS = new Set(["get_state", "abort", "reload", "compact", "settings", "job", "permissions", "get_models", "set_model", "set_thinking_level", "get_tools", "set_tools", "get_commands", "get_instructions", "save_instructions", "get_prompt_snapshot", "get_resources", "set_skill", "set_auto_compaction"]);
const RESPONSE_TYPES = new Set(["packages_shutdown_ready", "packages_shutdown_cancelled", "accepted", "message_status", "state", "error", "ui_response_result", "abort_result", "reload_result", "compact_result", "settings_result", "job_result", "permissions_result", "models_result", "model_result", "thinking_result", "tools_result", "commands_result", "instructions_result", "prompt_snapshot_result", "resources_result", "auto_compaction_result"]);
const SAFE_ID = /^[A-Za-z0-9._:-]{1,128}$/;

export class BioMindChildSession {
  readonly profile = "biomind";
  readonly cwd: string;
  private child: ChildProcessWithoutNullStreams;
  private alive = true;
  private active = false;
  private readyFrame?: BioMindFrame;
  private listeners = new Set<Listener>();
  private pending = new Map<string, Pending>();
  private questions = new Map<string, BioMindFrame>();
  private output = Buffer.alloc(0);
  private destroyCallback?: () => void;
  private readyPromise: Promise<void>;
  private readyResolve!: () => void;
  private readyReject!: (error: Error) => void;
  private startupTimer: ReturnType<typeof setTimeout>;
  private firstMessage = "";
  private created = new Date().toISOString();
  private closed = false;
  private packagePrepared = false;
  private packageShutdownRequested = false;
  private processExited = false;
  private processExitPromise: Promise<void>;
  private processExitResolve!: () => void;
  // Ephemeral transport recovery only. Native BioMind remains the sole session
  // store; completed requests are removed once their native result arrives.
  private transportSequence = 0;
  private liveMessageIndex = 0;
  private liveRows = new Map<string, { sequence: number; serialized: string; bytes: number }>();
  private liveBytes = 0;
  private liveTruncated = false;

  private clearLiveSnapshot(): void {
    this.liveRows.clear(); this.liveBytes = 0; this.liveTruncated = false; this.liveMessageIndex = 0;
  }
  private removeLiveRow(key: string): void {
    const previous = this.liveRows.get(key);
    if (previous) { this.liveBytes -= previous.bytes; this.liveRows.delete(key); }
  }
  private rememberLiveEvent(event: BioMindFrame): void {
    let key: string | undefined;
    if (event.type === "message_start") key = `message:${++this.liveMessageIndex}:start`;
    else if (event.type === "message_update") key = `message:${this.liveMessageIndex}:update`;
    else if (event.type === "message_end") {
      this.removeLiveRow(`message:${this.liveMessageIndex}:update`);
      key = `message:${this.liveMessageIndex}:end`;
    } else if (["tool_execution_start", "tool_execution_update", "tool_execution_end"].includes(event.type)) {
      const tool = String(event.toolCallId ?? event.id ?? "unknown").slice(0, 256);
      if (event.type === "tool_execution_end") this.removeLiveRow(`tool:${tool}:tool_execution_update`);
      key = `tool:${tool}:${event.type}`;
    } else if (event.type === "agent_start" || event.type === "agent_end") key = event.type;
    if (!key) return;
    this.removeLiveRow(key);
    // message_update.message is Pi's whole current message. Omit the delta:
    // replaying accumulated text plus its last delta would duplicate text.
    const recoveryEvent = { ...event };
    delete recoveryEvent.assistantMessageEvent;
    const serialized = JSON.stringify(recoveryEvent), bytes = Buffer.byteLength(serialized);
    if (bytes > 512 * 1024) { this.liveTruncated = true; return; }
    this.liveRows.set(key, { sequence: Number(event.transportSequence), serialized, bytes }); this.liveBytes += bytes;
    while (this.liveRows.size > 512 || this.liveBytes > 2 * 1024 * 1024) {
      const oldest = [...this.liveRows].reduce((a, b) => a[1].sequence < b[1].sequence ? a : b);
      this.removeLiveRow(oldest[0]); this.liveTruncated = true;
    }
  }
  liveSnapshot() {
    return { sessionId: this.sessionId, ownerGeneration: this.ownerGeneration,
      sequence: this.transportSequence, active: this.active, truncated: this.liveTruncated,
      events: [...this.liveRows.values()].sort((a, b) => a.sequence - b.sequence).map(row => JSON.parse(row.serialized) as BioMindFrame) };
  }

  constructor(private launch: BioMindLaunch) {
    this.cwd = launch.cwd;
    this.processExitPromise = new Promise(resolve => { this.processExitResolve = resolve; });
    this.readyPromise = new Promise((resolve, reject) => { this.readyResolve = resolve; this.readyReject = reject; });
    // Always use argv, never a shell, and never accept executable/config from HTTP.
    const entryArgs = ["-B", launch.entrypoint, "--pi-mode", "json", "--project-dir", this.cwd, "--pi-config", launch.config,
      ...(launch.sessionFile ? ["--session", launch.sessionFile] : [])];
    const args = launch.pipeLauncher ? ["-B", launch.pipeLauncher, launch.python, ...entryArgs] : entryArgs;
    this.child = (launch.spawnChild ?? spawn)(launch.python, args,
      { cwd: this.cwd, env: launch.env ?? process.env, stdio: ["pipe", "pipe", "pipe"], shell: false }) as ChildProcessWithoutNullStreams;
    this.startupTimer = setTimeout(() => this.fail("BIOMIND_START_TIMEOUT"), launch.timeoutMs ?? 90_000);
    this.child.stdout.on("data", (data: Buffer) => this.consume(data));
    // Drain continuously even without a UI. Do not expose raw diagnostics/config.
    this.child.stderr.on("data", () => {});
    this.child.stdin.on("error", () => this.fail("BIOMIND_INPUT_CLOSED"));
    this.child.stdout.on("error", () => this.fail("BIOMIND_OUTPUT_CLOSED"));
    this.child.on("error", () => this.fail("BIOMIND_START_FAILED"));
    this.child.on("close", () => { this.processExited = true; this.processExitResolve(); this.finish("BIOMIND_OWNER_EXITED"); });
  }
  get sessionId(): string { return String(this.readyFrame?.sessionId ?? ""); }
  get sessionFile(): string { return typeof this.readyFrame?.sessionFile === "string" ? this.readyFrame.sessionFile : ""; }
  get ownerGeneration(): string { return String(this.readyFrame?.ownerGeneration ?? ""); }
  isAlive(): boolean { return this.alive; }
  isRunning(): boolean { return this.alive && this.active; }
  waitUntilReady(): Promise<void> { return this.readyPromise; }
  onDestroy(callback: () => void): void { this.destroyCallback = callback; }
  pendingInfo() {
    return { path: this.sessionFile, id: this.sessionId, cwd: this.cwd, projectRoot: this.cwd,
      created: this.created, modified: this.created, messageCount: this.firstMessage ? 1 : 0,
      firstMessage: this.firstMessage || "(BioMind session)", profile: "biomind" as const, ownerGeneration: this.ownerGeneration };
  }
  metadata() {
    return { profile: this.profile, serverId: this.launch.serverId, ownerGeneration: this.ownerGeneration, project: this.cwd,
      protocolVersion: this.readyFrame?.protocolVersion, capabilities: this.readyFrame?.capabilities,
      inputLimits: this.readyFrame?.inputLimits, model: this.readyFrame?.model };
  }
  private consume(data: Buffer): void {
    if (!this.alive) return;
    // One chunk can contain many frames; bound each pending frame, not the chunk.
    let offset = 0;
    while (offset < data.length) {
      const newline = data.indexOf(10, offset);
      const end = newline < 0 ? data.length : newline;
      if (this.output.length + end - offset > MAX_FRAME_BYTES) { this.fail("BIOMIND_OUTPUT_FRAME_LIMIT"); return; }
      this.output = Buffer.concat([this.output, data.subarray(offset, end)]);
      if (newline < 0) return;
      const line = this.output.toString("utf8").trim();
      this.output = Buffer.alloc(0); offset = newline + 1;
      if (!line) continue;
      let frame: BioMindFrame;
      try { frame = JSON.parse(line); }
      catch { this.fail("BIOMIND_INVALID_OUTPUT"); return; }
      if (!frame || typeof frame !== "object" || typeof frame.type !== "string") { this.fail("BIOMIND_INVALID_OUTPUT"); return; }
      this.receive(frame);
    }
  }
  private receive(frame: BioMindFrame): void {
    if (frame.type === "ready") {
      if (this.readyFrame || frame.protocolVersion !== 1 || typeof frame.sessionId !== "string" || !frame.sessionId ||
          typeof frame.ownerGeneration !== "string" || !frame.ownerGeneration || frame.cwd !== this.cwd) { this.fail("BIOMIND_READY_IDENTITY_MISMATCH"); return; }
      this.readyFrame = frame; clearTimeout(this.startupTimer); this.readyResolve(); return;
    }
    const acknowledgedCommand = typeof frame.id === "string" ? this.pending.get(frame.id)?.command : undefined;
    if (RESPONSE_TYPES.has(frame.type)) {
      let key = typeof frame.id === "string" ? frame.id : undefined;
      if ((!key || !this.pending.has(key)) && typeof frame.clientMessageId === "string") {
        key = [...this.pending].find(([, p]) => p.clientMessageId === frame.clientMessageId)?.[0];
      }
      const pending = key ? this.pending.get(key) : undefined;
      if (pending && key) {
        clearTimeout(pending.timer); this.pending.delete(key);
        if (frame.type === "error") pending.reject(new BioMindTransportError(String(frame.code ?? "BIOMIND_REJECTED"), frame.code !== "PI_CONTROL_FAILED"));
        else if (frame.type === "message_status" && frame.status !== "accepted" && frame.status !== "consumed") {
          pending.reject(new BioMindTransportError(String(frame.code ?? "ADMISSION_PENDING"), frame.status === "failed"));
        } else if (frame.status === "error") {
          const code = String(frame.code ?? "BIOMIND_CONTROL_ERROR");
          pending.reject(new BioMindTransportError(code, code.startsWith("INVALID_")));
        } else pending.resolve(frame);
      }
    }
    if (frame.type === "state") {
      this.active = frame.active === true;
      if (!this.active) this.clearLiveSnapshot();
      const previousQuestions = new Set(this.questions.keys());
      this.questions.clear();
      if (Array.isArray(frame.pendingUI)) for (const request of frame.pendingUI) {
        if (request && typeof request === "object") this.rememberQuestion(request as BioMindFrame);
      }
      for (const id of previousQuestions) if (!this.questions.has(id)) this.emit({ type: "extension_ui_resolved", id, reason: "owner_snapshot", sessionId: this.sessionId, ownerGeneration: this.ownerGeneration });
      this.launch.changed?.();
    } else if (frame.type === "event") {
      const event = frame.event as BioMindFrame | undefined;
      if (event?.type) {
        if (event.type === "agent_start") { if (!this.active) this.clearLiveSnapshot(); this.active = true; this.launch.changed?.(); }
        // agent_end only ends a Pi turn; host result defines request completion.
        this.emit({ ...event, sessionId: this.sessionId, ownerGeneration: this.ownerGeneration });
      }
      return;
    } else if (frame.type === "question" || frame.type === "approval") {
      this.rememberQuestion(frame); return;
    } else if (frame.type === "ui_response_result" && frame.status === "accepted" && acknowledgedCommand === "ui_response") {
      this.questions.delete(String(frame.id));
      this.emit({ ...frame, type: "extension_ui_resolved" });
    } else if (frame.type === "abort_result" && frame.status === "requested" && acknowledgedCommand === "abort") {
      this.clearQuestions("abort_requested");
    } else if (frame.type === "result") {
      this.active = false; this.clearLiveSnapshot(); this.clearQuestions("request_result"); this.launch.changed?.();
      this.emit({ ...frame, type: "biomind_result" });
      this.emit({ type: "prompt_done", sessionId: this.sessionId, ownerGeneration: this.ownerGeneration,
        runtime_status: frame.runtime_status, scientific_success: frame.scientific_success, requestId: frame.requestId });
      return;
    } else if (frame.type === "accepted" && frame.command === "prompt") {
      if (!this.active) this.clearLiveSnapshot();
      this.active = true; this.launch.changed?.();
    }
    this.emit({ ...frame, sessionId: this.sessionId, ownerGeneration: this.ownerGeneration });
  }
  private rememberQuestion(frame: BioMindFrame): void {
    if (typeof frame.id !== "string" || this.questions.size >= 32 && !this.questions.has(frame.id)) { this.fail("BIOMIND_UI_REQUEST_LIMIT"); return; }
    const approval = frame.type === "approval" || frame.kind === "approval";
    const choices = Array.isArray(frame.choices) ? frame.choices.filter((v): v is string => typeof v === "string") : [];
    const editor = !approval && frame.method === "editor";
    if (frame.method === "editor" && approval || editor && frame.choices !== undefined ||
        frame.prefill !== undefined && (!editor || typeof frame.prefill !== "string" || Buffer.byteLength(frame.prefill) > MAX_EDITOR_PREFILL_BYTES)) {
      this.fail("BIOMIND_INVALID_UI_REQUEST"); return;
    }
    const event: BioMindFrame = { ...frame, type: "extension_ui_request", biomindKind: approval ? "approval" : "question",
      ownerGeneration: this.ownerGeneration, method: approval ? "confirm" : editor ? "editor" : choices.length ? "select" : "input",
      title: String(frame.question ?? (approval ? "BioMind approval" : "BioMind question")),
      ...(approval ? { message: String(frame.question ?? "Confirm this BioMind operation"), proposal: frame.proposal ?? frame.payload } : {}),
      ...(choices.length ? { options: choices } : {}) };
    this.questions.set(frame.id, event); this.emit(event);
  }
  private clearQuestions(reason: string): void {
    for (const id of this.questions.keys()) this.emit({ type: "extension_ui_resolved", id, reason, sessionId: this.sessionId, ownerGeneration: this.ownerGeneration });
    this.questions.clear();
  }
  onEvent(listener: Listener): () => void {
    if (this.listeners.size >= MAX_LISTENERS) throw new BioMindTransportError("SUBSCRIBER_LIMIT", true);
    this.listeners.add(listener);
    try { for (const question of this.questions.values()) listener(question); }
    catch { this.listeners.delete(listener); }
    return () => { this.listeners.delete(listener); };
  }
  private emit(event: BioMindFrame): void {
    event = { ...event, transportSequence: ++this.transportSequence };
    this.rememberLiveEvent(event);
    if (event.type === "state") {
      event.liveSnapshot = this.liveSnapshot();
    }
    for (const listener of this.listeners) {
      try { listener(event); } catch { this.listeners.delete(listener); }
    }
  }
  private request(frame: BioMindFrame, timeoutMs = 30_000): Promise<BioMindFrame> {
    if (!this.alive || !this.readyFrame) return Promise.reject(new BioMindTransportError("BIOMIND_OWNER_UNAVAILABLE"));
    if (this.pending.size >= MAX_REQUESTS) return Promise.reject(new BioMindTransportError("TRANSPORT_BUSY", true));
    const id = String(frame.id ?? randomUUID());
    if (this.pending.has(id) || typeof frame.clientMessageId === "string" && [...this.pending.values()].some(p => p.clientMessageId === frame.clientMessageId)) {
      return Promise.reject(new BioMindTransportError("ADMISSION_IN_FLIGHT"));
    }
    const encoded = JSON.stringify({ ...frame, id }) + "\n";
    if (Buffer.byteLength(encoded) > MAX_INPUT_BYTES) return Promise.reject(new BioMindTransportError("INPUT_FRAME_TOO_LARGE", true));
    // No unbounded stdin queue and no hidden retry. If not written it is rejected.
    if (this.child.stdin.writableLength + Buffer.byteLength(encoded) > MAX_INPUT_BYTES) return Promise.reject(new BioMindTransportError("TRANSPORT_BACKPRESSURE", true));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new BioMindTransportError("BIOMIND_RESPONSE_TIMEOUT")); }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, command: frame.type, clientMessageId: typeof frame.clientMessageId === "string" ? frame.clientMessageId : undefined });
      this.child.stdin.write(encoded, (error) => { if (error) this.fail("BIOMIND_INPUT_CLOSED"); });
    });
  }
  async send(command: Record<string, unknown>): Promise<unknown> {
    await this.waitUntilReady();
    const type = command.type;
    if (typeof type !== "string") throw new BioMindTransportError("INVALID_COMMAND", true);
    if ((packageManagementActive() || this.packagePrepared) && !["get_state", "get_resources", "get_tools"].includes(type)) throw new BioMindTransportError("PACKAGE_OPERATION_IN_PROGRESS", true);
    if ((command.serverId !== undefined && command.serverId !== this.launch.serverId)
        || (command.sessionId !== undefined && command.sessionId !== this.sessionId)
        || (command.ownerGeneration !== undefined && command.ownerGeneration !== this.ownerGeneration)
        || (command.project !== undefined && command.project !== this.cwd)) throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
    if (MESSAGE_COMMANDS.has(type)) {
      if (typeof command.clientMessageId !== "string" || !SAFE_ID.test(command.clientMessageId) ||
          command.serverId !== this.launch.serverId || command.sessionId !== this.sessionId || command.ownerGeneration !== this.ownerGeneration || command.project !== this.cwd) {
        throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
      }
      if (typeof command.message !== "string") throw new BioMindTransportError("TEXT_REQUIRED", true);
      const result = await this.request({ type, text: command.message, ...(command.images !== undefined ? { images: command.images } : {}),
        clientMessageId: command.clientMessageId, sessionId: this.sessionId, ownerGeneration: this.ownerGeneration, project: this.cwd });
      if (!this.firstMessage) this.firstMessage = command.message.slice(0, 160) || "[image]";
      return result;
    }
    if (type === "extension_ui_response") {
      const question = typeof command.id === "string" ? this.questions.get(command.id) : undefined;
      if (!question || ["sessionId", "requestId", "businessRequestId", "ownerGeneration"].some(key => command[key] !== question[key])) {
        throw new BioMindTransportError("STALE_UI_RESPONSE", true);
      }
      let value: unknown;
      if (command.cancelled === true) value = { cancelled: true };
      else if (question.biomindKind === "approval") {
        if (typeof command.confirmed !== "boolean") throw new BioMindTransportError("EXPLICIT_CONFIRMATION_REQUIRED", true);
        value = { choice: command.confirmed ? "approve" : "deny" };
      } else {
        if (typeof command.value !== "string") throw new BioMindTransportError("INVALID_UI_RESPONSE", true);
        value = command.value;
      }
      // Authority is reconstructed from the server-owned original request.
      return this.request({ type: "ui_response", id: question.id, value, sessionId: question.sessionId,
        ownerGeneration: this.ownerGeneration, requestId: question.requestId, businessRequestId: question.businessRequestId });
    }
    if (!CONTROL_COMMANDS.has(type)) throw new BioMindTransportError("UNSUPPORTED_BIOMIND_COMMAND", true);
    if (type !== "get_state" && (command.serverId !== this.launch.serverId || command.sessionId !== this.sessionId
        || command.ownerGeneration !== this.ownerGeneration || command.project !== this.cwd)) throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
    const allowed: Record<string, string[]> = { settings: ["history_enabled", "memory_mode"], job: ["action", "run_ref", "after_cursor"],
      permissions: ["action", "grant_id", "request_scope", "policy_revision"], compact: ["customInstructions"],
      set_model: ["provider", "modelId"], set_thinking_level: ["level"], set_tools: ["tools"],
      get_resources: ["resourceId"], get_instructions: ["scope"], save_instructions: ["scope", "expectedVersion", "text", "create"],
      set_skill: ["resourceId", "scope", "enabled"], set_auto_compaction: ["enabled", "scope"] };
    const payload: BioMindFrame = { type, ownerGeneration: this.ownerGeneration, sessionId: this.sessionId, project: this.cwd };
    for (const key of allowed[type] ?? []) if (command[key] !== undefined) payload[key] = command[key];
    const result = await this.request(payload, type === "compact" || type === "reload" ? 120_000 : 30_000);
    if (type !== "get_state") return result;
    const queue = result.queue as { pendingAssociationCount?: number } | undefined;
    const actualModel = result.model as { input?: string[]; blockImages?: boolean } | undefined;
    return { ...result, ...this.metadata(), model: result.model, liveSnapshot: this.liveSnapshot(),
      capabilities: { ...(this.readyFrame?.capabilities as Record<string, unknown> ?? {}), ...(result.capabilities as Record<string, unknown> ?? {}), imageInputEnabled: actualModel?.input?.includes("image") === true && actualModel.blockImages !== true },
      isStreaming: result.active === true, isPromptRunning: result.active === true,
      isBashRunning: false, isCompacting: result.controlBusy === "compact", pendingMessageCount: queue?.pendingAssociationCount ?? 0,
      displayPendingUI: [...this.questions.values()],
      // No fabricated queue contents/system prompt/tool list; native state stays available.
      extensionStatuses: [], extensionWidgets: [], safeToRelease: false };
  }
  async preparePackageShutdown(): Promise<void> {
    if (!this.alive || this.active || this.pending.size || this.questions.size || !this.sessionFile) throw new BioMindTransportError("PACKAGES_BUSY", true);
    this.packagePrepared = true;
    const result = await this.request({ type: "prepare_packages_shutdown", sessionId: this.sessionId, ownerGeneration: this.ownerGeneration, project: this.cwd }, 30_000);
    if (result.type !== "packages_shutdown_ready" || result.safeToRelease !== true || result.packagesIdle !== true
      || result.sessionId !== this.sessionId || result.ownerGeneration !== this.ownerGeneration || result.cwd !== this.cwd) throw new BioMindTransportError("PACKAGES_STATE_UNAVAILABLE", true);
    this.packagePrepared = true;
  }
  async cancelPackageShutdown(): Promise<void> {
    if (!this.alive || !this.packagePrepared || this.packageShutdownRequested) return;
    const result = await this.request({ type: "resume_packages_operations", sessionId: this.sessionId, ownerGeneration: this.ownerGeneration, project: this.cwd }, 30_000);
    if (result.type !== "packages_shutdown_cancelled" || result.safeToRelease !== false || result.packagesIdle !== false
      || result.sessionId !== this.sessionId || result.ownerGeneration !== this.ownerGeneration) throw new BioMindTransportError("PACKAGES_STATE_UNAVAILABLE", true);
    this.packagePrepared = false;
  }
  packageExitConfirmed(): boolean { return this.processExited; }
  async shutdownForPackages(): Promise<void> {
    if (!this.packagePrepared || !this.alive || this.active || this.pending.size || this.questions.size) throw new BioMindTransportError("PACKAGES_BUSY", true);
    // Only the core's explicit quiescing receipt authorizes this orderly stop.
    // Registry ownership is retained until actual child close; no signal/kill.
    this.packageShutdownRequested = true;
    this.child.stdin.end(JSON.stringify({ type: "shutdown" }) + "\n");
    let timer: ReturnType<typeof setTimeout> | undefined;
    try { await Promise.race([this.processExitPromise, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new BioMindTransportError("PACKAGES_OWNER_EXIT_UNCONFIRMED", true)), 30_000);
    })]); } finally { if (timer) clearTimeout(timer); }
    if (!this.processExited) throw new BioMindTransportError("PACKAGES_OWNER_EXIT_UNCONFIRMED", true);
  }
  async shutdown(): Promise<void> {
    // No idle timer calls this. Explicit service shutdown does not cancel domain jobs.
    this.destroy();
  }
  destroy(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.alive) this.child.stdin.end(JSON.stringify({ type: "shutdown" }) + "\n");
    this.finish("BIOMIND_OWNER_CLOSED");
  }
  private fail(code: string): void {
    this.finish(code);
    this.child.stdin.end();
  }
  private finish(code: string): void {
    if (!this.alive) return;
    this.alive = false; this.active = false; clearTimeout(this.startupTimer);
    const error = new BioMindTransportError(code);
    this.readyReject(error);
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.pending.clear(); this.questions.clear(); this.output = Buffer.alloc(0); this.clearLiveSnapshot();
    this.emit({ type: "owner_unavailable", code, sessionId: this.sessionId, ownerGeneration: this.ownerGeneration,
      admission: "unknown", jobs: { status: "unknown" }, automaticReplay: false });
    this.listeners.clear(); this.destroyCallback?.(); this.launch.changed?.();
  }
}
