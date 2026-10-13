"use strict";
const path = require("node:path").posix;
const { ReceiptStorage } = require("./connection-store.cjs");
const ID = /^[A-Za-z0-9._:-]{1,256}$/;
const results = {
  get_models: "models_result", set_model: "model_result", set_thinking_level: "thinking_result",
  get_commands: "commands_result", get_resources: "resources_result", set_skill: "resources_result",
  get_instructions: "instructions_result", save_instructions: "instructions_result",
  get_prompt_snapshot: "prompt_snapshot_result", get_tools: "tools_result", set_tools: "tools_result",
  settings: "settings_result", permissions: "permissions_result", job: "job_result",
  set_auto_compaction: "auto_compaction_result", abort: "abort_result", compact: "compact_result", reload: "reload_result",
};
function remotePath(value) {
  if (typeof value !== "string" || !value.startsWith("/") || /[\0\r\n\\]/.test(value)) throw new Error("REMOTE_LINUX_PATH_REQUIRED");
  return path.normalize(value);
}
function fileUrl(value, type) { return `/api/files/${remotePath(value).split("/").filter(Boolean).map(encodeURIComponent).join("/")}?type=${type}`; }
function sessionId(value) { if (typeof value !== "string" || !ID.test(value)) throw new Error("INVALID_SESSION_ID"); return value; }
function bodyError(body, fallback) { return typeof body?.code === "string" && /^[A-Z_0-9]+$/.test(body.code) ? body.code : fallback; }

/** Flowflic's renderer protocol mapped onto the existing BioMind HTTP owner.
 * This class never spawns an Agent or reads/writes server session files. */
class BioMindBridge {
  constructor({ controller, send, store, input, fetcher = fetch, receiptFile }) {
    this.controller = controller; this.send = send; this.store = store; this.input = input; this.fetcher = fetcher;
    input.configureInputStorage(new ReceiptStorage(receiptFile));
    this.handles = new Map(); this.opens = new Map(); this.uploads = new Map(); this.profile = null;
    this.closed = false; this.generation = 0;
  }
  connectionStamp() {
    return { generation: this.controller.generation, serverId: this.controller.state.serverId,
      origin: this.controller.state.origin, connectionId: this.controller.state.connectionId };
  }
  assertConnection(stamp) {
    if (this.controller.state.status !== "ready" || this.controller.generation !== stamp.generation ||
      ["serverId", "origin", "connectionId"].some(key => this.controller.state[key] !== stamp[key])) throw new Error("CONNECTION_CHANGED");
  }
  async response(route, options = {}) {
    if (this.controller.state.status !== "ready") throw new Error("SERVER_NOT_CONNECTED");
    if (!route.startsWith("/api/") || route.startsWith("//")) throw new Error("INVALID_SERVER_ROUTE");
    const origin = this.controller.state.origin, auth = this.controller.authorization(origin);
    const stamp = this.connectionStamp();
    if (!auth) throw new Error("SERVER_NOT_CONNECTED");
    const headers = { ...options.headers, Authorization: auth };
    const r = await this.fetcher(origin + route, { ...options, headers, redirect: "error", signal: options.signal || AbortSignal.timeout(180000) });
    this.assertConnection(stamp);
    return r;
  }
  async json(route, data, options = {}) {
    const stamp = this.connectionStamp();
    const r = await this.response(route, { ...options, ...(data === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }) });
    const body = await r.json();
    this.assertConnection(stamp);
    if (!r.ok || body?.error) throw new Error(bodyError(body, `SERVER_HTTP_${r.status}`));
    return body;
  }
  async identity() {
    const p = await this.json("/api/agent/profile");
    if (p.profile !== "biomind" || p.serverId !== this.controller.state.serverId || p.capabilities?.automaticReplay !== false) throw new Error("INVALID_SERVER_PROFILE");
    this.profile = p; return p;
  }
  async packages(request = { action: "list" }) {
    if (!request || typeof request !== "object" || Array.isArray(request)) throw new Error("INVALID_PACKAGE_REQUEST");
    const allowed = new Set(["action", "scope", "cwd", "source", "declaration", "trusted", "expectedRevision"]);
    if (Object.keys(request).some(key => !allowed.has(key))) throw new Error("INVALID_PACKAGE_REQUEST");
    if (!this.profile) await this.identity();
    if (this.profile.capabilities?.packages?.protocol !== "BioMindPluginsV1") throw new Error("PACKAGE_MANAGEMENT_UNAVAILABLE");
    const body = { ...request, ...(request.cwd ? { cwd: remotePath(request.cwd) } : {}) };
    const result = request.action === "list"
      ? await this.json("/api/plugins" + (body.cwd ? "?cwd=" + encodeURIComponent(body.cwd) : ""))
      : await this.json("/api/plugins", body, { signal: AbortSignal.timeout(930000) });
    if (result?.schema !== "BioMindPluginsV1" || result.serverId !== this.profile.serverId ||
      !["ok", "BUSY", "BLOCKED", "repair_required", "error"].includes(result.status) ||
      (result.context && result.context.piVersion !== "0.87.1") ||
      (result.packages !== undefined && (!Array.isArray(result.packages) || result.packages.some(p =>
        !p || !["user", "project"].includes(p.scope) || typeof p.source !== "string" || typeof p.installed !== "boolean" ||
        !p.declaration || (typeof p.declaration !== "string" && p.declaration.source !== p.source))))) throw new Error("PACKAGE_RECEIPT_INVALID");
    // Mutations are never automatically retried, including an unknown HTTP outcome.
    for (const id of result.resumedSessionIds || []) {
      const h = this.handles.get(id);
      if (h) {
        // A resumed owner has a new generation; its old SSE subscription may
        // remain open sending heartbeats after the old child exited.
        clearTimeout(h.retry); h.stream?.abort(); h.stream = null;
        h.queue = []; h.questions.clear(); h.uiRecovery = null; h.snapshotSequence = 0;
        try { this.send("biomind:snapshot", await this.snapshot(h)); }
        catch (error) {
          result.viewRestoreFailures = [...(result.viewRestoreFailures || []), id];
          this.send("pi:error", { threadId: id, message: error.message });
        }
      }
    }
    return result;
  }
  async projects() {
    await this.identity();
    const d = await this.json("/api/sessions");
    if (!Array.isArray(d.sessions)) throw new Error("HISTORY_INVALID_RESPONSE");
    if (d.historyAvailable === false) throw new Error(d.historyError || "HISTORY_UNAVAILABLE");
    const byProject = new Map();
    for (const cwd of [...(d.registeredProjects || []), ...(this.profile.allowedProjects || [])]) {
      const p = remotePath(typeof cwd === "string" ? cwd : cwd.cwd);
      byProject.set(p, { cwd: p, name: path.basename(p), threads: [] });
    }
    for (const row of d.sessions) {
      const cwd = remotePath(row.cwd), id = sessionId(row.id);
      if (!byProject.has(cwd)) byProject.set(cwd, { cwd, name: path.basename(cwd), threads: [] });
      byProject.get(cwd).threads.push({ file: id, id, title: row.name || row.firstMessage?.slice(0, 80) || "新会话",
        preview: row.firstMessage || "", updatedAt: Date.parse(row.modified) || 0, messageCount: row.messageCount || 0 });
    }
    // Owner IDs are authoritative even before their first assistant message is persisted.
    for (const h of this.handles.values()) {
      if (!byProject.has(h.cwd)) byProject.set(h.cwd, { cwd: h.cwd, name: path.basename(h.cwd), threads: [] });
      const p = byProject.get(h.cwd);
      if (!p.threads.some(t => t.id === h.id)) p.threads.unshift({ file: h.id, id: h.id, title: "新会话", preview: "", updatedAt: h.created, messageCount: 0 });
    }
    return [...byProject.values()];
  }
  async browse(cwd) {
    const d = await this.json("/api/cwd/browse" + (cwd ? "?path=" + encodeURIComponent(remotePath(cwd)) : ""));
    return { ...d, parent: d.parentPath, entries: (d.directories || []).map(e => ({ ...e, isDirectory: true })) };
  }
  async openProject(cwd) {
    const v = await this.json("/api/cwd/validate", { cwd: remotePath(cwd) });
    if (v.valid === false) throw new Error("PROJECT_NOT_AUTHORIZED");
    const actual = remotePath(v.cwd || v.path || cwd);
    await this.json("/api/projects/registered", { cwd: actual });
    this.send("pi:projects-changed", { cwd: actual });
    return actual;
  }
  async state(id, cwd) {
    const r = await this.json(`/api/agent/${sessionId(id)}`);
    if (!r.running) return null;
    const s = r.state;
    if (!s || s.serverId !== this.profile?.serverId || s.sessionId !== id || (s.project ?? s.cwd) !== cwd || !s.ownerGeneration) throw new Error("REMOTE_SCOPE_MISMATCH");
    return s;
  }
  scope(h, s = h.state) { return { serverId: s.serverId, project: h.cwd, sessionId: h.id, ownerGeneration: s.ownerGeneration }; }
  async control(id, command) {
    const h = this.handles.get(id); if (!h) throw new Error("THREAD_NOT_OPEN");
    const before = await this.state(id, h.cwd); if (!before) throw new Error("OWNER_UNAVAILABLE");
    if (h.state && before.ownerGeneration !== h.state.ownerGeneration) throw new Error("OWNER_CHANGED_STATUS_UNKNOWN");
    h.state = before;
    const type = command?.type;
    if (!results[type]) throw new Error("UNSUPPORTED_BIOMIND_COMMAND");
    const r = await this.json(`/api/agent/${id}`, { ...command, ...this.scope(h) });
    const frame = r.data;
    if (!r.success || !frame || frame.type !== results[type] || frame.status === "error") throw new Error("CONTROL_RECEIPT_INVALID");
    for (const [k, v] of Object.entries(this.scope(h))) if (frame[k] !== undefined && frame[k] !== v) throw new Error("REMOTE_SCOPE_MISMATCH");
    return frame.data ?? frame;
  }
  handle(id, cwd) {
    let h = this.handles.get(id);
    if (h && h.cwd !== cwd) throw new Error("REMOTE_SCOPE_MISMATCH");
    if (!h) { h = { id, cwd, state: null, queue: [], watching: false, stream: null, created: Date.now(), generation: 0, questions: new Map() }; this.handles.set(id, h); }
    return h;
  }
  async history(id, cwd) {
    const d = await this.json(`/api/sessions/${sessionId(id)}`);
    if (d.sessionId !== id || d.info?.cwd !== cwd || !Array.isArray(d.context?.messages)) throw new Error("HISTORY_SCOPE_MISMATCH");
    return d;
  }
  async snapshot(h, { allowMissingHistory = false } = {}) {
    h.watching = false;
    h.state = await this.state(h.id, h.cwd);
    if (h.state) this.subscribe(h);
    h.questions.clear();
    for (const q of h.state?.displayPendingUI || []) h.questions.set(q.id, q);
    let hist;
    try { hist = await this.history(h.id, h.cwd); }
    catch (e) { if (!allowMissingHistory || !h.state || !/SERVER_HTTP_404/.test(e.message)) throw e; }
    const model = h.state?.model || hist?.context?.model || null;
    let models = [], commands = [], levels = ["off"];
    if (h.state) {
      const available = h.state.capabilities?.getModels;
      if (available === true || available?.available) {
        const m = await this.control(h.id, { type: "get_models" }); models = m.models || []; levels = m.availableThinkingLevels || levels;
      }
      const commandCap = h.state.capabilities?.getCommands;
      if (commandCap === true || commandCap?.available) commands = (await this.control(h.id, { type: "get_commands" })).commands || [];
    }
    const live = h.state?.liveSnapshot;
    if (live && (live.sessionId !== h.id || live.ownerGeneration !== h.state.ownerGeneration)) throw new Error("REMOTE_SCOPE_MISMATCH");
    const liveEvents = live?.active && Array.isArray(live.events) ? live.events.filter(e => e.type !== "agent_end") : [];
    h.snapshotSequence = Number.isSafeInteger(live?.sequence) ? live.sequence : 0;
    // The renderer replays the authoritative compacted live rows once after its
    // history projection. Pending events newer than that boundary follow it.
    h.queue = h.queue.filter(row => !row.payload?.event?.transportSequence || row.payload.event.transportSequence > h.snapshotSequence);
    const snapshot = { threadId: h.id, sessionFile: h.id, cwd: h.cwd, sessionName: hist?.info?.name || null,
      connected: !!h.state, isStreaming: h.state?.active === true || h.state?.isStreaming === true,
      model: model && { ...model, id: model.id || model.modelId }, models, thinkingLevel: h.state?.thinkingLevel || hist?.context?.thinkingLevel || "off",
      levels, commands, messages: hist?.context?.messages || [], branchMessages: [], permission: "sandbox",
      capabilities: h.state?.capabilities || {}, ownerGeneration: h.state?.ownerGeneration,
      liveEvents, snapshotSequence: h.snapshotSequence, liveSnapshotTruncated: live?.truncated === true,
      ...(hist ? {} : { historyPending: true }) };
    h.snapshot = snapshot;
    return snapshot;
  }
  async loadHistory({ cwd, sessionFile }) {
    await this.identity();
    const h = this.handle(sessionId(sessionFile), remotePath(cwd));
    const snapshot = await this.snapshot(h);
    this.remember(h);
    return snapshot;
  }
  async open({ cwd, sessionFile, clientOpenId }) {
    await this.identity(); cwd = remotePath(cwd);
    if (sessionFile) {
      const h = this.handle(sessionId(sessionFile), cwd);
      const s = await this.state(h.id, cwd);
      if (!s) {
        // Re-open the same native writer, never allocate a replacement ID.
        const r = await this.json(`/api/agent/${h.id}`, { type: "resume_session", serverId: this.profile.serverId, project: cwd, sessionId: h.id });
        if (!r.success || r.data?.sessionId !== h.id) throw new Error("HISTORY_RESTORE_IDENTITY_MISMATCH");
      }
      const snapshot = await this.snapshot(h); this.remember(h); return snapshot;
    }
    if (typeof clientOpenId !== "string" || !clientOpenId.startsWith("opening-")) throw new Error("CLIENT_OPEN_ID_REQUIRED");
    if (this.opens.has(clientOpenId)) return this.opens.get(clientOpenId);
    const promise = (async () => {
      const r = await this.json("/api/agent/new", { cwd, type: "ensure_session", serverId: this.profile.serverId });
      if (!r.success || r.project !== cwd || r.serverId !== this.profile.serverId) throw new Error("NEW_SESSION_SCOPE_MISMATCH");
      const h = this.handle(sessionId(r.sessionId), cwd);
      const snapshot = await this.snapshot(h, { allowMissingHistory: true }); this.remember(h);
      this.send("pi:projects-changed", { cwd, sessionFile: h.id });
      return { ...snapshot, isNewSession: true };
    })();
    // Retain rejected opens too: an uncertain timeout must never create another owner automatically.
    this.opens.set(clientOpenId, promise); return promise;
  }
  remember(h) {
    const id = this.controller.state.connectionId, ui = this.store.ui(id);
    const refs = (ui.openThreads || []).filter(x => x.sessionFile !== h.id);
    ui.openThreads = [...refs, { cwd: h.cwd, sessionFile: h.id }].slice(-30);
    ui.lastThreadId = h.id; this.store.saveUI(id, ui);
  }
  async restore() {
    await this.identity();
    const ui = this.store.ui(this.controller.state.connectionId), refs = ui.openThreads || [];
    const out = [], errors = [];
    for (const ref of refs) {
      try { out.push(await this.snapshot(this.handle(sessionId(ref.sessionFile), remotePath(ref.cwd)), { allowMissingHistory: true })); }
      catch (e) { errors.push(ref.sessionFile); this.send("pi:error", { threadId: ref.sessionFile, message: e.message }); }
    }
    if (errors.length) throw new Error("SESSION_RESTORE_INCOMPLETE");
    return out;
  }
  activate(id) {
    const h = this.handles.get(id); if (!h) return false;
    h.watching = true;
    for (const item of h.queue.splice(0)) this.send(item.channel, item.payload);
    for (const request of h.questions.values()) this.send("pi:extui", { threadId: id, request });
    return true;
  }
  emit(h, channel, payload) {
    if (h.watching) this.send(channel, payload);
    else {
      if (h.queue.length >= 2000) { h.stream?.abort(); h.queue = []; this.send("pi:error", { threadId: h.id, message: "EVENT_RESYNC_REQUIRED" }); return; }
      h.queue.push({ channel, payload });
    }
  }
  uiRecovery(h) {
    const owner = h.state?.ownerGeneration;
    let recovery = h.uiRecovery;
    if (!recovery || recovery.owner !== owner || recovery.generation !== this.generation || recovery.transport !== this.controller.generation) {
      recovery = { owner, generation: this.generation, transport: this.controller.generation,
        watermark: 0, questionSequence: 0, unsequencedRevision: 0, target: 0, expected: null, pending: null };
      h.uiRecovery = recovery;
    }
    return recovery;
  }
  questionIds(rows) {
    if (!Array.isArray(rows) || rows.length > 32 || rows.some(q => !q || typeof q.id !== "string" || !ID.test(q.id))) throw new Error("PENDING_UI_SNAPSHOT_INVALID");
    const ids = new Set(rows.map(q => q.id));
    if (ids.size !== rows.length) throw new Error("PENDING_UI_SNAPSHOT_INVALID");
    return ids;
  }
  questionIdsMatch(h, ids) { return ids && ids.size === h.questions.size && [...ids].every(id => h.questions.has(id)); }
  reconcileQuestions(h, rows, recovery, sequence) {
    this.questionIds(rows);
    const next = new Map();
    for (const q of rows) {
      if (q.type !== "extension_ui_request" || !["confirm", "select", "input", "editor"].includes(q.method) ||
        q.ownerGeneration !== recovery.owner || q.sessionId !== undefined && q.sessionId !== h.id ||
        q.serverId !== undefined && q.serverId !== this.profile?.serverId || q.project !== undefined && q.project !== h.cwd) throw new Error("PENDING_UI_SCOPE_MISMATCH");
      next.set(q.id, q);
    }
    const previous = h.questions; h.questions = next;
    const sameQuestion = (left, right) => {
      if (!left) return false;
      const { transportSequence: _leftSequence, ...a } = left;
      const { transportSequence: _rightSequence, ...b } = right;
      return JSON.stringify(a) === JSON.stringify(b);
    };
    for (const [id, q] of next) if (!sameQuestion(previous.get(id), q)) this.emit(h, "pi:extui", { threadId: h.id, request: q });
    for (const id of previous.keys()) if (!next.has(id)) this.emit(h, "pi:event", { threadId: h.id, event: { type: "extension_ui_resolved", id, reason: "owner_snapshot" } });
    h.state.displayPendingUI = rows;
    recovery.watermark = Math.max(recovery.watermark, sequence);
    recovery.expected = new Set(next.keys());
  }
  recoverQuestions(h, recovery, retryBudget = 1) {
    if (recovery.pending) return recovery.pending;
    const stamp = this.connectionStamp(), generation = this.generation, owner = recovery.owner;
    const id = h.id, project = h.cwd, serverId = this.profile?.serverId;
    const unsequencedRevision = recovery.unsequencedRevision;
    const current = () => !this.closed && generation === this.generation && h.uiRecovery === recovery &&
      this.handles.get(id) === h && h.id === id && h.cwd === project && this.profile?.serverId === serverId && h.state?.ownerGeneration === owner &&
      this.controller.state.status === "ready" && stamp.generation === this.controller.generation &&
      stamp.serverId === this.controller.state.serverId && stamp.connectionId === this.controller.state.connectionId && stamp.origin === this.controller.state.origin;
    let again = false;
    // Assign the in-flight promise before issuing GET: get_state itself emits
    // an SSE state. Its sequence is covered by the returned HTTP watermark.
    const operation = Promise.resolve().then(async () => {
      const state = await this.state(id, project);
      if (!current()) return;
      if (!state || state.ownerGeneration !== owner) throw new Error("PENDING_UI_OWNER_CHANGED");
      const live = state.liveSnapshot;
      if (!live || live.sessionId !== h.id || live.ownerGeneration !== owner || !Number.isSafeInteger(live.sequence) || live.sequence < 0 || !Array.isArray(state.displayPendingUI)) throw new Error("PENDING_UI_SNAPSHOT_UNAVAILABLE");
      const stale = live.sequence < Math.max(recovery.target, recovery.questionSequence, recovery.watermark, h.snapshotSequence || 0) ||
        recovery.unsequencedRevision !== unsequencedRevision;
      if (stale) {
        // Newer normalized question/resolution events remain authoritative.
        // Only a still-unreconciled ID set needs one bounded follow-up query.
        again = !this.questionIdsMatch(h, recovery.expected);
        if (again && !retryBudget) throw new Error("PENDING_UI_RESYNC_REQUIRED");
        return;
      }
      this.reconcileQuestions(h, state.displayPendingUI, recovery, live.sequence);
    }).catch(error => {
      if (current()) this.send("pi:error", { threadId: h.id, message: error.message });
    }).finally(() => {
      if (recovery.pending === operation) recovery.pending = null;
      if (again && retryBudget && current()) void this.recoverQuestions(h, recovery, retryBudget - 1);
    });
    recovery.pending = operation;
    return operation;
  }
  event(h, event) {
    if (event.sessionId && event.sessionId !== h.id || event.ownerGeneration && h.state && event.ownerGeneration !== h.state.ownerGeneration
      || event.serverId && event.serverId !== this.profile?.serverId || event.project && event.project !== h.cwd) return;
    if (event.transportSequence && event.transportSequence <= (h.snapshotSequence || 0)) return;
    if (event.type === "extension_ui_request") {
      const recovery = this.uiRecovery(h), sequence = Number.isSafeInteger(event.transportSequence) ? event.transportSequence : 0;
      if (sequence && sequence <= recovery.watermark) return;
      if (sequence) recovery.questionSequence = Math.max(recovery.questionSequence, sequence); else recovery.unsequencedRevision++;
      h.questions.set(event.id, event); this.emit(h, "pi:extui", { threadId: h.id, request: event }); return;
    }
    if (event.type === "extension_ui_resolved") {
      const recovery = this.uiRecovery(h), sequence = Number.isSafeInteger(event.transportSequence) ? event.transportSequence : 0;
      if (sequence && sequence <= recovery.watermark) return;
      if (sequence) recovery.questionSequence = Math.max(recovery.questionSequence, sequence); else recovery.unsequencedRevision++;
      h.questions.delete(event.id); this.emit(h, "pi:event", { threadId: h.id, event }); return;
    }
    if (event.type === "state") {
      h.state = { ...h.state, ...event };
      const recovery = this.uiRecovery(h), sequence = Number.isSafeInteger(event.transportSequence) ? event.transportSequence : 0;
      if (sequence && sequence <= recovery.watermark) return;
      try {
        if (Object.hasOwn(event, "displayPendingUI")) this.reconcileQuestions(h, event.displayPendingUI, recovery, sequence);
        else if (Array.isArray(event.pendingUI)) {
          // The compiled BioMind adapter emits normalized requests/resolutions
          // before this raw state. Never replace their proposal/editor metadata
          // with a client-created approximation of a native question.
          recovery.expected = this.questionIds(event.pendingUI);
          recovery.target = Math.max(recovery.target, sequence);
          if (this.questionIdsMatch(h, recovery.expected)) recovery.watermark = Math.max(recovery.watermark, sequence);
          else void this.recoverQuestions(h, recovery);
        }
        // An omitted field is not an empty authoritative question list.
      } catch (error) { this.send("pi:error", { threadId: h.id, message: error.message }); }
      return;
    }
    if (event.type === "owner_unavailable") {
      this.send("pi:error", { threadId: h.id, message: "OWNER_UNAVAILABLE" });
      h.stream?.abort(); return;
    }
    if (event.type === "resync_required") { h.stream?.abort(); return; }
    // BioMind completion, not an intermediate Pi agent_end, ends the request.
    if (event.type === "agent_end") return;
    if (event.type === "prompt_done") {
      this.emit(h, "pi:event", { threadId: h.id, event: { type: "agent_settled", biomind: event, transportSequence: event.transportSequence } });
      this.send("pi:projects-changed", { cwd: h.cwd, sessionFile: h.id }); return;
    }
    this.emit(h, "pi:event", { threadId: h.id, event });
  }
  subscribe(h) {
    if (h.stream || this.closed) return;
    const abort = new AbortController(); h.stream = abort;
    const generation = this.generation;
    void (async () => {
      try {
        const r = await this.response(`/api/agent/${h.id}/events?v2=1`, { signal: abort.signal });
        if (!r.ok || !r.body) throw new Error("EVENT_STREAM_UNAVAILABLE");
        const decoder = new TextDecoder(), reader = r.body.getReader(); let buffer = "";
        try {
          while (!abort.signal.aborted) {
            const { value, done } = await reader.read();
            if (done || abort.signal.aborted || generation !== this.generation || this.closed || h.stream !== abort) break;
            buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
            if (buffer.length > 12 * 1024 * 1024) throw new Error("EVENT_FRAME_TOO_LARGE");
            let end;
            while ((end = buffer.indexOf("\n\n")) !== -1) {
              const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
              const data = frame.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trimStart()).join("\n");
              if (data) this.event(h, JSON.parse(data));
            }
          }
        } finally { reader.releaseLock(); }
      } catch { /* Recovery below is read-only. */ }
      finally {
        const wasCurrent = h.stream === abort;
        if (wasCurrent) h.stream = null;
        if (wasCurrent && !this.closed && generation === this.generation && this.controller.state.status === "ready") {
          h.watching = false;
          this.send("pi:error", { threadId: h.id, message: "EVENT_STREAM_RECONNECTING" });
          h.retry = setTimeout(async () => {
            if (generation !== this.generation || this.closed || this.controller.state.status !== "ready") return;
            try { this.send("biomind:snapshot", await this.snapshot(h, { allowMissingHistory: true })); }
            catch (e) { this.send("pi:error", { threadId: h.id, message: e.message }); }
          }, 2000); h.retry.unref?.();
        }
      }
    })();
  }
  detach() { this.generation++; for (const h of this.handles.values()) { clearTimeout(h.retry); h.stream?.abort(); h.stream = null; h.watching = false; h.queue = []; h.questions.clear(); } }
  close() { this.closed = true; this.detach(); }
  async prompt({ threadId, text, images, attachments }, type = "prompt") {
    const h = this.handles.get(threadId); if (!h) throw new Error("THREAD_NOT_OPEN");
    h.state = await this.state(h.id, h.cwd); if (!h.state) throw new Error("OWNER_UNAVAILABLE");
    let message = String(text || "");
    const escape = v => String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    for (const a of attachments || []) {
      const uploaded = this.uploads.get(a.abs);
      if (!uploaded || uploaded.cwd !== h.cwd || uploaded.serverId !== this.profile.serverId) throw new Error("ATTACHMENT_REQUIRES_SERVER_UPLOAD");
      message += `\n\n<file name="${escape(uploaded.name)}" path="${escape(a.abs)}" note="uploaded to server" />`;
    }
    const normalized = images?.map(i => ({ type: "image", data: i.data || i.base64, mimeType: i.mimeType }));
    this.input.checkBioMindImages(normalized, h.state);
    const scope = this.scope(h);
    const receipt = await this.input.submitBioMindInput(scope, { type, message, images: normalized }, {
      fetcher: (route, init) => this.response(route, init), maxFrameBytes: h.state.inputLimits?.maxFrameBytes,
      current: () => this.controller.state.serverId === scope.serverId && this.controller.state.status === "ready",
    });
    // A receipt is retained until renderer explicitly acknowledges clearing its draft.
    return { ok: true, receipt };
  }
  finishInput(receipt) { this.input.finishAcceptedBioMindInput(receipt); return true; }
  async extui({ threadId, id, payload }) {
    const h = this.handles.get(threadId); if (!h) throw new Error("THREAD_NOT_OPEN");
    const q = h.questions.get(id); if (!q) throw new Error("STALE_UI_RESPONSE");
    const current = await this.state(h.id, h.cwd);
    if (!current || current.ownerGeneration !== q.ownerGeneration) throw new Error("STALE_UI_RESPONSE");
    const r = await this.json(`/api/agent/${h.id}`, { type: "extension_ui_response", id,
      ...this.scope(h, current), requestId: q.requestId, businessRequestId: q.businessRequestId,
      ...Object.fromEntries(Object.entries(payload || {}).filter(([k]) => ["confirmed", "cancelled", "value"].includes(k))) });
    if (!r.success || r.data?.type !== "ui_response_result" || r.data.status !== "accepted") throw new Error("UI_RESPONSE_STATUS_UNKNOWN");
    h.questions.delete(id); return true;
  }
  async upload({ cwd, name, mimeType, data }) {
    cwd = remotePath(cwd); await this.identity();
    if (typeof data !== "string" || data.length > 40 * 1024 * 1024 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) throw new Error("UPLOAD_INVALID_BYTES");
    const bytes = Buffer.from(data, "base64"); if (bytes.toString("base64") !== data) throw new Error("UPLOAD_INVALID_BYTES");
    const form = new FormData(); form.set("cwd", cwd);
    form.append("files", new Blob([bytes], { type: mimeType || "application/octet-stream" }), path.basename(String(name || "attachment.bin")));
    const stamp = this.connectionStamp();
    const r = await this.response("/api/uploads", { method: "POST", body: form });
    const d = await r.json();
    this.assertConnection(stamp);
    if (!r.ok || d.errors?.length || d.project !== cwd || d.serverId !== this.profile.serverId || d.uploaded?.length !== 1) throw new Error("UPLOAD_FAILED");
    const row = d.uploaded[0], abs = remotePath(row.path);
    this.uploads.set(abs, { cwd, serverId: this.profile.serverId, name: row.sourceName || name });
    return { abs, name: row.sourceName || name, size: row.size };
  }
  async tree(cwd, rel = "") {
    cwd = remotePath(cwd); const abs = path.resolve(cwd, rel);
    if (abs !== cwd && !abs.startsWith(cwd + "/")) throw new Error("FILE_OUTSIDE_PROJECT");
    const d = await this.json(fileUrl(abs, "list"));
    return (d.entries || []).map(e => ({ name: e.name, rel: path.relative(cwd, path.join(abs, e.name)), abs: path.join(abs, e.name), isDir: e.isDir, ext: path.extname(e.name), size: e.size }));
  }
  async preview(abs) {
    abs = remotePath(abs); const name = path.basename(abs), ext = path.extname(abs).toLowerCase();
    const meta = await this.json(fileUrl(abs, "meta")), size = meta.size || 0;
    if (size > 20 * 1024 * 1024) return { name, ext, size, kind: "toobig", message: "文件超过 20 MiB 预览限制" };
    const mime = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml" }[ext];
    if (mime || [".docx", ".xlsx", ".xls"].includes(ext)) {
      const stamp = this.connectionStamp();
      const r = await this.response(fileUrl(abs, "download")); if (!r.ok) throw new Error("FILE_READ_FAILED");
      const bytes = await r.arrayBuffer(); this.assertConnection(stamp);
      if (bytes.byteLength > 20 * 1024 * 1024) throw new Error("PREVIEW_TOO_LARGE");
      return { name, ext, size, kind: mime ? "image" : ext === ".docx" ? "docx" : "xlsx", mime, base64: Buffer.from(bytes).toString("base64") };
    }
    const d = await this.json(fileUrl(abs, "read"));
    return { name, ext, size, kind: [".md", ".markdown"].includes(ext) ? "markdown" : [".html", ".htm"].includes(ext) ? "html" : "text", text: d.content, truncated: d.truncated };
  }
}
module.exports = { BioMindBridge, remotePath, fileUrl, sessionId };
