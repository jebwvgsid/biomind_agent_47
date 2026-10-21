"use strict";
// Offline protocol tests. These do not constitute Windows/model acceptance.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");
const ts = require("../../../BioMind_Studio_S1_Implementation_20261006/candidate/pi-studio/node_modules/typescript");
const { BioMindBridge, remotePath, sessionId } = require("./biomind-bridge.cjs");
const { ConnectionStore } = require("./connection-store.cjs");
const sourcePath = path.resolve(__dirname, "../src/main/biomind-client.ts");
const inputModule = new Module(sourcePath, module);
inputModule._compile(ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, sourcePath);
const input = inputModule.exports;
const serverId = "fixture-server", projectA = "/projects/中文 A", projectB = "/projects/B";
const modelA = { provider: "remote-provider", id: "remote-model-a", input: ["text", "image"] };
const modelB = { provider: "remote-provider", id: "remote-model-b", input: ["text"] };
const connectionId = "0123456789abcdef01234567";
function json(value, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }); }
function state(id, cwd) {
  return { serverId, sessionId: id, project: cwd, cwd, ownerGeneration: "owner-" + id, active: false,
    capabilities: { getModels: true, getCommands: true }, model: { ...modelA }, admissions: [],
    inputLimits: { maxCount: 2, maxImageBytes: 1024, maxTotalBytes: 2048, maxFrameBytes: 20000, acceptedMimeTypes: ["image/png"] } };
}
function fixture(t, storeOptions = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-bridge-test-"));
  const stores = [], bridges = [];
  t.after(async () => {
    for (const bridge of bridges) bridge.close();
    await Promise.all(stores.map(store => store.flushUI()));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  const receiptFile = path.join(directory, "receipts.json"), requests = [], events = [], states = new Map(), histories = new Map(), streams = new Map();
  let nextId = 0, nextFailure = null;
  const controller = { generation: 1, state: { status: "ready", serverId, connectionId, origin: "http://127.0.0.1:30142" }, authorization: () => "Basic synthetic" };
  const newStore = () => {
    const value = new ConnectionStore(directory, {}, value => value, storeOptions);
    stores.push(value); return value;
  };
  const store = newStore();
  const put = (id, cwd) => {
    states.set(id, state(id, cwd)); histories.set(id, { sessionId: id, info: { cwd, name: "Session " + id }, context: { messages: [], model: { ...modelA } } }); return id;
  };
  async function fetcher(url, init = {}) {
    const parsed = new URL(url), route = parsed.pathname, method = init.method || "GET";
    assert.equal(parsed.origin, controller.state.origin); assert.equal(init.headers.Authorization, "Basic synthetic");
    assert.equal(init.redirect, "error");
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    requests.push({ route, method, body, query: parsed.searchParams });
    if (route === "/api/agent/profile") return json({ profile: "biomind", serverId, capabilities: { automaticReplay: false }, allowedProjects: [projectA, projectB] });
    if (route === "/api/sessions") return json({ historyAvailable: true, registeredProjects: [projectA, projectB], sessions: [...histories].map(([id, h]) => ({ id, cwd: h.info.cwd, name: h.info.name, messageCount: h.context.messages.length })) });
    if (route === "/api/agent/new") {
      assert.equal(method, "POST"); assert.equal(body.type, "ensure_session"); assert.equal(body.serverId, serverId);
      const id = put(String.fromCharCode(65 + nextId++), body.cwd);
      if (nextFailure === "new") { nextFailure = null; throw new Error("SYNTHETIC_TIMEOUT_AFTER_CREATE"); }
      return json({ success: true, serverId, project: body.cwd, sessionId: id });
    }
    if (route.startsWith("/api/sessions/")) {
      const history = histories.get(route.split("/").at(-1)); return history ? json(history) : json({ error: true }, 404);
    }
    if (route.endsWith("/events")) {
      const id = route.split("/").at(-2);
      const stream = new ReadableStream({ start(c) { streams.set(id, c); init.signal?.addEventListener("abort", () => { try { c.error(new Error("SYNTHETIC_ABORT")); } catch {} }, { once: true }); } });
      return new Response(stream, { headers: { "content-type": "text/event-stream" } });
    }
    if (route.startsWith("/api/agent/")) {
      const id = route.split("/").at(-1), current = states.get(id);
      if (method === "GET") return json(current ? { running: true, state: current } : { running: false });
      if (body.type === "resume_session") {
        const history = histories.get(id); assert(history); states.set(id, state(id, history.info.cwd));
        return json({ success: true, data: { sessionId: id } });
      }
      assert(current); assert.equal(body.serverId, serverId); assert.equal(body.project, current.project);
      assert.equal(body.sessionId, id); assert.equal(body.ownerGeneration, current.ownerGeneration);
      const scope = { serverId, project: current.project, sessionId: id, ownerGeneration: current.ownerGeneration };
      if (["prompt", "steer", "follow_up"].includes(body.type)) {
        assert.equal(typeof body.clientMessageId, "string");
        const receipt = { type: "accepted", status: "accepted", ...scope, clientMessageId: body.clientMessageId };
        current.admissions.push({ clientMessageId: body.clientMessageId, status: "accepted", receipt }); current.active = true;
        histories.get(id).context.messages.push({ role: "user", content: [{ type: "text", text: body.message }] });
        if (nextFailure === "prompt") { nextFailure = null; throw new Error("SYNTHETIC_LOST_RECEIPT"); }
        return json({ success: true, data: receipt });
      }
      if (body.type === "get_models") return json({ success: true, data: { type: "models_result", ...scope, data: { models: [modelA, modelB], availableThinkingLevels: ["off", "low"] } } });
      if (body.type === "get_commands") return json({ success: true, data: { type: "commands_result", ...scope, data: { commands: [{ name: "main", description: "BioMind Main" }] } } });
      if (body.type === "set_model") { current.model = { provider: body.provider, id: body.modelId }; return json({ success: true, data: { type: "model_result", ...scope, data: { model: current.model } } }); }
      throw new Error("UNEXPECTED_COMMAND_" + body.type);
    }
    if (route === "/api/uploads") {
      assert(body instanceof FormData); assert.equal(body.get("cwd"), projectA);
      const file = body.get("files"); assert(file instanceof Blob);
      const bytes = new Uint8Array(await file.arrayBuffer());
      assert.deepEqual([...bytes], [0, 1, 2, 255]);
      return json({ serverId, project: projectA, uploaded: [{ path: projectA + "/uploads/attachment.bin", sourceName: "attachment.bin", size: bytes.length }] });
    }
    if (route === "/api/cwd/validate") return json({ valid: true, cwd: body.cwd });
    if (route === "/api/projects/registered") return json({ success: true });
    throw new Error("UNEXPECTED_ROUTE_" + route);
  }
  const construct = (connectionStore = store) => {
    const bridge = new BioMindBridge({ controller, send: (channel, payload) => events.push({ channel, payload }), store: connectionStore, input, fetcher, receiptFile });
    bridges.push(bridge); return bridge;
  };
  return { bridge: construct(), construct, newStore, directory, controller, put, states, histories, requests, events, streams, store, receiptFile, failNext: kind => { nextFailure = kind; } };
}
function lockedUIStorage(warnings) {
  return { retryDelays: [], onUIWriteError: warning => warnings.push(warning),
    fs: { ...fs, promises: { ...fs.promises, rename: async (source, target) => {
      if (/^ui-[a-f0-9]{24}\.json$/.test(path.basename(target))) throw Object.assign(new Error("synthetic Windows UI file lock"), { code: "EPERM" });
      return fs.promises.rename(source, target);
    } } } };
}
const posts = f => f.requests.filter(r => r.method === "POST");
const inputs = f => posts(f).filter(r => ["prompt", "steer", "follow_up"].includes(r.body?.type));
function uiQuestion(id, overrides = {}) {
  return { type: "extension_ui_request", id, sessionId: "A", ownerGeneration: "owner-A", requestId: "request-" + id,
    businessRequestId: "business-" + id, biomindKind: "approval", method: "confirm", title: "Approve " + id,
    message: "Full server-owned description", proposal: { command: "reviewed server operation", effects: ["write artifact"] }, ...overrides };
}
function liveState(f, questions, sequence) {
  return { ...f.states.get("A"), displayPendingUI: questions,
    liveSnapshot: { sessionId: "A", ownerGeneration: "owner-A", sequence, active: true, events: [] } };
}
async function settleUI(handle) {
  for (let i = 0; i < 4; i++) { if (handle.uiRecovery?.pending) await handle.uiRecovery.pending; await Promise.resolve(); }
}
test("A/B opens use authoritative owner IDs and retain each project/history/running state", async t => {
  const f = fixture(t);
  const a = await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const b = await f.bridge.open({ cwd: projectB, clientOpenId: "opening-b" });
  assert.equal(a.threadId, "A"); assert.equal(b.threadId, "B");
  f.bridge.activate("A"); await f.bridge.prompt({ threadId: "A", text: "A task" });
  f.bridge.activate("B"); await f.bridge.prompt({ threadId: "B", text: "B task" });
  const returnedA = await f.bridge.open({ cwd: projectA, sessionFile: "A" });
  const returnedB = await f.bridge.open({ cwd: projectB, sessionFile: "B" });
  assert.equal(returnedA.cwd, projectA); assert.equal(returnedB.cwd, projectB);
  assert.equal(returnedA.messages[0].content[0].text, "A task"); assert.equal(returnedB.messages[0].content[0].text, "B task");
  assert.equal(returnedA.isStreaming, true); assert.equal(returnedB.isStreaming, true);
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 2);
  assert(!posts(f).some(r => ["abort", "delete", "shutdown"].includes(r.body?.type)));
  assert.deepEqual(inputs(f).map(r => [r.body.sessionId, r.body.project]), [["A", projectA], ["B", projectB]]);
});
test("uncertain new-session timeout is retained and cannot create a replacement on retry", async t => {
  const f = fixture(t); f.failNext("new");
  for (let attempt = 0; attempt < 2; attempt++) await assert.rejects(f.bridge.open({ cwd: projectA, clientOpenId: "opening-uncertain" }), /SYNTHETIC_TIMEOUT_AFTER_CREATE/);
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 1);
  assert.deepEqual((await f.bridge.projects()).flatMap(p => p.threads.map(h => h.id)), ["A"]);
});
test("existing inactive history resumes exactly its ID and never allocates an alternate session", async t => {
  const f = fixture(t); f.put("saved-A", projectA); f.states.delete("saved-A");
  f.histories.get("saved-A").context.messages = [{ role: "user", content: "saved history" }];
  const snapshot = await f.bridge.open({ cwd: projectA, sessionFile: "saved-A" });
  assert.equal(snapshot.threadId, "saved-A"); assert.equal(snapshot.messages[0].content, "saved history");
  assert.equal(posts(f).filter(r => r.body?.type === "resume_session").length, 1);
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 0);
});
test("restart restores both persisted native histories using read/control queries, without prompt or new", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); await f.bridge.open({ cwd: projectB, clientOpenId: "opening-b" });
  assert.equal(await f.store.flushUI(), true);
  f.bridge.close(); const offset = f.requests.length, replacement = f.construct(f.newStore());
  const restored = await replacement.restore();
  assert.equal(restored.schema, "biomind-restore-v1"); assert.deepEqual(restored.failures, []);
  assert.deepEqual(restored.snapshots.map(s => [s.threadId, s.cwd]), [["A", projectA], ["B", projectB]]);
  assert(!f.requests.slice(offset).some(r => r.route === "/api/agent/new" || ["prompt", "steer", "follow_up", "resume_session"].includes(r.body?.type)));
});
test("permanent UI rename EPERM cannot hide a new native ID or repeat its creation on retry", async t => {
  const warnings = [], f = fixture(t, lockedUIStorage(warnings));
  const first = await f.bridge.open({ cwd: projectA, clientOpenId: "opening-ui-locked" });
  assert.equal(first.threadId, "A"); assert.equal(first.sessionFile, "A");
  assert.equal(await f.store.flushUI(), false);
  assert.equal(f.store.uiSaveStatus(connectionId).errorCode, "EPERM");
  const second = await f.bridge.open({ cwd: projectA, clientOpenId: "opening-ui-locked" });
  assert.equal(second.threadId, "A"); assert.equal(second.cwd, projectA);
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 1);
  assert.deepEqual(f.store.ui(connectionId), { openThreads: [{ cwd: projectA, sessionFile: "A" }], lastThreadId: "A" });
  assert.deepEqual(warnings, [{ id: connectionId, code: "EPERM" }]);
  assert.equal(fs.existsSync(path.join(f.directory, `ui-${connectionId}.json`)), false);
  assert.equal(inputs(f).length, 0);
});
test("UI rename EPERM preserves existing history, running owner and focused A/B refs for RAM restoration", async t => {
  const warnings = [], f = fixture(t, lockedUIStorage(warnings));
  const uiFile = path.join(f.directory, `ui-${connectionId}.json`), originalDisk = JSON.stringify({ config: { theme: "dark" } });
  fs.writeFileSync(uiFile, originalDisk);
  f.put("native-A", projectA); f.put("native-B", projectB);
  f.histories.get("native-A").context.messages = [{ role: "user", content: "existing A history" }];
  f.histories.get("native-B").context.messages = [{ role: "user", content: "existing B history" }];
  f.states.get("native-A").active = true;
  const opened = await f.bridge.open({ cwd: projectA, sessionFile: "native-A" });
  const loaded = await f.bridge.loadHistory({ cwd: projectB, sessionFile: "native-B" });
  assert.equal(opened.threadId, "native-A"); assert.equal(opened.messages[0].content, "existing A history");
  assert.equal(opened.isStreaming, true);
  assert.equal(loaded.threadId, "native-B"); assert.equal(loaded.messages[0].content, "existing B history");
  assert.equal(await f.store.flushUI(), false);
  const focused = f.store.ui(connectionId); focused.lastThreadId = "native-A";
  await assert.rejects(f.store.saveUI(connectionId, focused), { code: "EPERM" });
  assert.equal(f.store.ui(connectionId).lastThreadId, "native-A");
  assert.deepEqual(f.store.ui(connectionId).openThreads, [{ cwd: projectA, sessionFile: "native-A" }, { cwd: projectB, sessionFile: "native-B" }]);
  f.bridge.close(); const offset = f.requests.length, replacement = f.construct();
  const restored = await replacement.restore();
  assert.deepEqual(restored.snapshots.map(s => [s.threadId, s.cwd, s.messages[0].content, s.isStreaming]), [
    ["native-A", projectA, "existing A history", true], ["native-B", projectB, "existing B history", false],
  ]);
  assert.equal(f.store.ui(connectionId).lastThreadId, "native-A");
  assert.equal(fs.readFileSync(uiFile, "utf8"), originalDisk, "a locked valid destination must remain untouched");
  assert(warnings.length > 0 && warnings.every(w => w.code === "EPERM"));
  assert(!f.requests.slice(offset).some(r => r.route === "/api/agent/new" || ["prompt", "steer", "follow_up", "resume_session"].includes(r.body?.type)));
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 0);
  assert(!posts(f).some(r => ["abort", "delete", "shutdown"].includes(r.body?.type)));
});
test("a failed saved history is explicit while another session restores without replay or deleting refs", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); await f.bridge.open({ cwd: projectB, clientOpenId: "opening-b" });
  f.states.delete("A"); f.histories.delete("A"); f.bridge.detach();
  const offset = f.requests.length;
  const restored = await f.bridge.restore();
  assert.deepEqual(restored.failures, [{ threadId: "A", cwd: projectA, code: "SERVER_HTTP_404" }]);
  assert.deepEqual(restored.snapshots.map(s => s.threadId), ["B"]);
  assert(f.events.some(event => event.channel === "pi:error" && event.payload.threadId === "A"));
  assert.deepEqual(f.store.ui(connectionId).openThreads.map(ref => ref.sessionFile), ["A", "B"]);
  assert.equal(posts(f).filter(r => r.route === "/api/agent/new").length, 2);
  assert(!f.requests.slice(offset).some(r => r.route === "/api/agent/new" || ["prompt", "steer", "follow_up", "resume_session"].includes(r.body?.type)));
});
test("restore transport, identity, and connection-change failures cannot become partial success", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const originalSnapshot = f.bridge.snapshot.bind(f.bridge);
  f.bridge.snapshot = async () => { throw new TypeError("fetch failed"); };
  await assert.rejects(f.bridge.restore(), /fetch failed/);
  f.bridge.snapshot = async () => { throw new Error("SERVER_HTTP_401"); };
  await assert.rejects(f.bridge.restore(), /SERVER_HTTP_401/);
  f.bridge.snapshot = async () => { f.controller.generation++; throw new Error("SERVER_HTTP_404"); };
  await assert.rejects(f.bridge.restore(), /CONNECTION_CHANGED/);
  f.bridge.snapshot = originalSnapshot;
  f.bridge.identity = async () => { throw new Error("INVALID_SERVER_PROFILE"); };
  await assert.rejects(f.bridge.restore(), /INVALID_SERVER_PROFILE/);
  assert.equal(inputs(f).length, 0);
});
test("malformed saved restore identity fails before opening any saved session", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const ui = f.store.ui(connectionId);
  ui.openThreads.push({ sessionFile: "../unknown", cwd: projectB });
  // Inject a malformed reader result; normal saveUI rejects it even earlier.
  f.store.ui = () => ui;
  const offset = f.requests.length;
  await assert.rejects(f.bridge.restore(), /INVALID_SESSION_ID/);
  assert.deepEqual(f.requests.slice(offset).map(r => r.route), ["/api/agent/profile"]);
  assert.equal(inputs(f).length, 0);
});
test("lost prompt receipt survives restart and retry recovers admission without another POST", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.failNext("prompt");
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "DO_NOT_REPLAY" }), /INPUT_STATUS_UNKNOWN/);
  const disk = fs.readFileSync(f.receiptFile, "utf8"); assert(!disk.includes("DO_NOT_REPLAY"));
  assert.equal(inputs(f).length, 1); f.bridge.close();
  assert.equal(await f.store.flushUI(), true);
  const replacement = f.construct(f.newStore()); await replacement.restore();
  assert.equal(inputs(f).length, 1);
  const result = await replacement.prompt({ threadId: "A", text: "DO_NOT_REPLAY" });
  assert.equal(result.receipt.status, "accepted"); assert.equal(inputs(f).length, 1);
  replacement.finishInput(result.receipt); assert.deepEqual(JSON.parse(fs.readFileSync(f.receiptFile, "utf8")), {});
});
test("owner replacement after uncertain prompt is query-only and cannot replay it", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.failNext("prompt");
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "uncertain" }), /INPUT_STATUS_UNKNOWN/);
  f.states.get("A").ownerGeneration = "replacement-owner";
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "uncertain" }), /OWNER_CHANGED_STATUS_UNKNOWN/);
  assert.equal(inputs(f).length, 1);
});
test("model selection applies to the scoped remote owner and exposes its returned models", async t => {
  const f = fixture(t); const snapshot = await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  assert.deepEqual(snapshot.models.map(m => m.id), [modelA.id, modelB.id]);
  const result = await f.bridge.control("A", { type: "set_model", provider: modelB.provider, modelId: modelB.id });
  assert.deepEqual(result.model, { provider: modelB.provider, id: modelB.id });
  assert.equal(f.states.get("A").model.id, modelB.id);
  assert.equal(posts(f).filter(r => r.body?.type === "set_model").length, 1);
  f.states.get("A").ownerGeneration = "changed-owner";
  await assert.rejects(f.bridge.control("A", { type: "set_model", provider: modelA.provider, modelId: modelA.id }), /OWNER_CHANGED_STATUS_UNKNOWN/);
  assert.equal(posts(f).filter(r => r.body?.type === "set_model").length, 1);
});
test("attachments upload exact bytes and only returned Linux paths may reach the owner", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "read local", attachments: [{ abs: "C:\\Users\\fixture\\attachment.bin" }] }), /ATTACHMENT_REQUIRES_SERVER_UPLOAD/);
  assert.equal(inputs(f).length, 0);
  const file = await f.bridge.upload({ cwd: projectA, name: "attachment.bin", mimeType: "application/octet-stream", data: Buffer.from([0, 1, 2, 255]).toString("base64") });
  assert.equal(file.abs, projectA + "/uploads/attachment.bin");
  await f.bridge.prompt({ threadId: "A", text: "read uploaded", attachments: [file] });
  assert(inputs(f)[0].body.message.includes(file.abs)); assert(!inputs(f)[0].body.message.includes("C:\\"));
  await f.bridge.open({ cwd: projectB, clientOpenId: "opening-b" });
  await assert.rejects(f.bridge.prompt({ threadId: "B", text: "wrong project", attachments: [file] }), /ATTACHMENT_REQUIRES_SERVER_UPLOAD/);
});
test("images carry base64 bytes and respect the real remote model/input limits", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const data = Buffer.from([137, 80, 78, 71]).toString("base64");
  await f.bridge.prompt({ threadId: "A", text: "inspect image", images: [{ base64: data, mimeType: "image/png" }] });
  assert.deepEqual(inputs(f)[0].body.images, [{ type: "image", data, mimeType: "image/png" }]);
  f.states.get("A").model = modelB;
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "unsupported", images: [{ base64: data, mimeType: "image/png" }] }), /IMAGE_MODEL_UNSUPPORTED/);
  assert.equal(inputs(f).length, 1);
});
test("cross-project history/state cannot replace an existing handle", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  await assert.rejects(f.bridge.open({ cwd: projectB, sessionFile: "A" }), /REMOTE_SCOPE_MISMATCH/);
  f.states.get("A").project = projectB;
  await assert.rejects(f.bridge.prompt({ threadId: "A", text: "scope failure" }), /REMOTE_SCOPE_MISMATCH/);
  assert.equal(inputs(f).length, 0);
});
test("Pi agent_end is intermediate; only BioMind prompt_done ends the displayed task", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const handle = f.bridge.handles.get("A"), offset = f.events.length;
  f.bridge.event(handle, { type: "agent_end", sessionId: "A", ownerGeneration: "owner-A" });
  assert.equal(f.events.length, offset);
  f.bridge.event(handle, { type: "tool_execution_start", toolName: "bash", sessionId: "A", ownerGeneration: "owner-A" });
  f.bridge.event(handle, { type: "prompt_done", sessionId: "A", ownerGeneration: "owner-A" });
  assert.deepEqual(f.events.slice(offset).filter(e => e.channel === "pi:event").map(e => e.payload.event.type), ["tool_execution_start", "agent_settled"]);
  const renderer = fs.readFileSync(path.join(__dirname, "../src/renderer/src/store.ts"), "utf8");
  assert.match(renderer, /case "agent_settled":/);
});
test("event scope mismatches are rejected for server, project, session and owner", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const handle = f.bridge.handles.get("A"), offset = f.events.length;
  for (const changed of [{ serverId: "other-server" }, { project: projectB }, { sessionId: "B" }, { ownerGeneration: "other-owner" }]) {
    f.bridge.event(handle, { type: "tool_execution_start", serverId, project: projectA, sessionId: "A", ownerGeneration: "owner-A", ...changed });
  }
  assert.equal(f.events.length, offset);
});
test("SSE without displayPendingUI preserves normalized approval/editor metadata when pending IDs match", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const h = f.bridge.handles.get("A"), approval = uiQuestion("approve"), editor = uiQuestion("edit", { biomindKind: "question", method: "editor", prefill: "server-owned draft" });
  f.bridge.event(h, { ...approval, transportSequence: 11 }); f.bridge.event(h, { ...editor, transportSequence: 12 });
  const offset = f.requests.length;
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 13,
    pendingUI: [{ type: "approval", id: approval.id, question: "raw abbreviated" }, { type: "question", id: editor.id }] });
  await settleUI(h);
  assert.deepEqual(h.questions.get("approve").proposal, approval.proposal);
  assert.equal(h.questions.get("approve").message, approval.message); assert.equal(h.questions.get("edit").prefill, editor.prefill);
  assert.equal(f.requests.length, offset); assert.equal(h.uiRecovery.watermark, 13);
});
test("missing both optional lists never means an empty authorization set", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const h = f.bridge.handles.get("A"); f.bridge.event(h, uiQuestion("keep")); const offset = f.requests.length;
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10 });
  await settleUI(h); assert(h.questions.has("keep")); assert.equal(f.requests.length, offset);
});
test("raw-ID mismatch queries once, coalesces GET-induced SSE state and restores full HTTP-owned question", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const h = f.bridge.handles.get("A"), full = uiQuestion("missing"), original = f.bridge.fetcher; let gets = 0;
  f.bridge.fetcher = async (url, init) => {
    if (new URL(url).pathname !== "/api/agent/A") return original(url, init);
    gets++;
    // Production GET get_state emits another raw SSE state before returning.
    f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 11, pendingUI: [{ id: "missing" }] });
    return json({ running: true, state: liveState(f, [full], 11) });
  };
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "missing" }] });
  await settleUI(h);
  assert.equal(gets, 1); assert.deepEqual(h.questions.get("missing"), full); assert.equal(h.uiRecovery.watermark, 11);
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 11, pendingUI: [{ id: "missing" }] });
  await settleUI(h); assert.equal(gets, 1);
  assert.equal(inputs(f).length, 0); assert(!posts(f).some(r => r.body?.type === "extension_ui_response"));
});
test("HTTP refresh cannot resurrect a question already resolved by a newer event", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const h = f.bridge.handles.get("A"); let release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  f.bridge.fetcher = async () => { entered(); return new Promise(resolve => { release = resolve; }); };
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "old" }] });
  await started;
  f.bridge.event(h, { type: "extension_ui_resolved", id: "old", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 21 });
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 22, pendingUI: [] });
  release(json({ running: true, state: liveState(f, [uiQuestion("old")], 20) })); await settleUI(h);
  assert.equal(h.questions.size, 0); assert.equal(h.uiRecovery.watermark, 22);
});
test("HTTP refresh racing a newer missing question performs one bounded follow-up", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const h = f.bridge.handles.get("A"); let release, entered, gets = 0;
  const started = new Promise(resolve => { entered = resolve; });
  f.bridge.fetcher = async () => {
    gets++;
    if (gets === 1) { entered(); return new Promise(resolve => { release = resolve; }); }
    return json({ running: true, state: liveState(f, [uiQuestion("new")], 31) });
  };
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "old" }] });
  await started;
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 30, pendingUI: [{ id: "new" }] });
  release(json({ running: true, state: liveState(f, [uiQuestion("old")], 20) })); await settleUI(h);
  assert.equal(gets, 2); assert.deepEqual([...h.questions.keys()], ["new"]);
});
test("disconnect and scope replacement discard delayed HTTP authorization snapshots", async t => {
  for (const mutate of [f => f.bridge.detach(), f => { f.bridge.handles.get("A").state.ownerGeneration = "replacement"; },
    f => { f.controller.generation++; }, f => { f.controller.state.status = "disconnected"; },
    f => { f.controller.state.serverId = "other-server"; }, f => { f.controller.state.connectionId = "other-connection"; },
    f => { f.bridge.profile.serverId = "other-profile"; }, f => { f.bridge.handles.get("A").cwd = projectB; }]) {
    const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
    const h = f.bridge.handles.get("A"); let release, entered; const started = new Promise(resolve => { entered = resolve; });
    f.bridge.fetcher = async () => { entered(); return new Promise(resolve => { release = resolve; }); };
    f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "stale" }] });
    await started; mutate(f); release(json({ running: true, state: liveState(f, [uiQuestion("stale")], 11) }));
    await settleUI(h); assert(!h.questions.has("stale"));
  }
});
test("continuously stale HTTP recovery is bounded and reports that authorization state is unresolved", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const h = f.bridge.handles.get("A"); let gets = 0;
  f.bridge.event(h, uiQuestion("known"));
  f.bridge.fetcher = async () => {
    gets++;
    f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 100 + gets, pendingUI: [{ id: "newer" }] });
    return json({ running: true, state: liveState(f, [uiQuestion("old")], gets) });
  };
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "newer" }] });
  await settleUI(h);
  assert.equal(gets, 2); assert.equal(h.uiRecovery.pending, null);
  assert.deepEqual([...h.questions.keys()], ["known"]);
  assert(f.events.some(e => e.channel === "pi:error" && e.payload.message === "PENDING_UI_RESYNC_REQUIRED"));
  assert.equal(inputs(f).length, 0);
});
test("invalid or foreign normalized HTTP rows cannot partially replace the current question set", async t => {
  for (const invalid of [uiQuestion("bad", { ownerGeneration: "other-owner" }), uiQuestion("bad", { sessionId: "B" }),
    uiQuestion("bad", { serverId: "other-server" }), uiQuestion("bad", { project: projectB }),
    uiQuestion("bad", { method: "unknown" }), uiQuestion("good")]) {
    const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
    const h = f.bridge.handles.get("A"); f.bridge.event(h, uiQuestion("known"));
    f.bridge.fetcher = async () => json({ running: true, state: liveState(f, [uiQuestion("good"), invalid], 20) });
    f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "good" }] });
    await settleUI(h); assert.deepEqual([...h.questions.keys()], ["known"]);
    assert(f.events.some(e => e.channel === "pi:error"));
  }
});
test("authorization HTTP watermark does not discard delayed task and tool events", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const h = f.bridge.handles.get("A");
  f.bridge.fetcher = async () => json({ running: true, state: liveState(f, [uiQuestion("approval")], 50) });
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "approval" }] });
  await settleUI(h);
  const offset = f.events.length;
  f.bridge.event(h, { type: "tool_execution_start", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 40, toolCallId: "still-needed-tool" });
  f.bridge.event(h, { type: "prompt_done", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 41 });
  assert.deepEqual(f.events.slice(offset).filter(e => e.channel === "pi:event").map(e => e.payload.event.type), ["tool_execution_start", "agent_settled"]);
});
test("failed or cross-scope HTTP reconciliation preserves known questions and reports failure", async t => {
  for (const reply of [() => { throw new Error("READ_ONLY_NETWORK_FAILURE"); }, f => json({ running: true, state: { ...liveState(f, [], 20), project: projectB } })]) {
    const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
    const h = f.bridge.handles.get("A"); f.bridge.event(h, uiQuestion("known"));
    f.bridge.fetcher = async () => reply(f);
    f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "different" }] });
    await settleUI(h); assert(h.questions.has("known")); assert(f.events.some(e => e.channel === "pi:error"));
  }
});
test("empty authoritative HTTP snapshot resolves stale questions without replying to them", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const h = f.bridge.handles.get("A"); f.bridge.event(h, uiQuestion("expired"));
  f.bridge.fetcher = async () => json({ running: true, state: liveState(f, [], 11) });
  const offset = f.events.length;
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [] });
  await settleUI(h); assert.equal(h.questions.size, 0);
  assert(f.events.slice(offset).some(e => e.payload.event?.type === "extension_ui_resolved" && e.payload.event.id === "expired"));
  assert.equal(inputs(f).length, 0);
});
test("newer complete SSE snapshot outranks delayed HTTP recovery and retains full proposal", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const h = f.bridge.handles.get("A"); let release, entered; const started = new Promise(resolve => { entered = resolve; });
  f.bridge.fetcher = async () => { entered(); return new Promise(resolve => { release = resolve; }); };
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 10, pendingUI: [{ id: "old" }] });
  await started;
  f.bridge.event(h, { type: "state", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 30, displayPendingUI: [uiQuestion("new")] });
  release(json({ running: true, state: liveState(f, [uiQuestion("old")], 20) })); await settleUI(h);
  assert.deepEqual([...h.questions.keys()], ["new"]); assert.equal(h.uiRecovery.watermark, 30);
});
test("reconnect snapshots replay active tools once and drop events at the recovered sequence boundary", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" });
  const owner = f.states.get("A"); owner.active = true;
  owner.liveSnapshot = { sessionId: "A", ownerGeneration: "owner-A", sequence: 20, active: true,
    events: [{ type: "agent_start", transportSequence: 18 }, { type: "tool_execution_start", toolName: "bash", transportSequence: 19 }, { type: "agent_end", transportSequence: 20 }] };
  owner.displayPendingUI = [{ type: "extension_ui_request", id: "active-question", ownerGeneration: "owner-A" }];
  const handle = f.bridge.handles.get("A"); handle.questions.set("stale-question", { id: "stale-question" });
  const recovered = await f.bridge.snapshot(handle);
  assert.equal(recovered.snapshotSequence, 20); assert.equal(recovered.isStreaming, true);
  assert.deepEqual(recovered.liveEvents.map(e => e.type), ["agent_start", "tool_execution_start"]);
  assert.deepEqual([...handle.questions.keys()], ["active-question"]);
  f.bridge.activate("A"); const offset = f.events.length;
  f.bridge.event(handle, { type: "tool_execution_start", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 19 });
  f.bridge.event(handle, { type: "tool_execution_end", sessionId: "A", ownerGeneration: "owner-A", transportSequence: 21 });
  assert.deepEqual(f.events.slice(offset).map(e => e.payload.event.type), ["tool_execution_end"]);
  owner.liveSnapshot.ownerGeneration = "other-owner";
  await assert.rejects(f.bridge.snapshot(handle), /REMOTE_SCOPE_MISMATCH/);
});
test("detached SSE cannot queue a frame whose reader continuation was already scheduled", async t => {
  const f = fixture(t); await f.bridge.open({ cwd: projectA, clientOpenId: "opening-a" }); f.bridge.activate("A");
  const handle = f.bridge.handles.get("A"), offset = f.events.length;
  const event = { type: "tool_execution_start", sessionId: "A", ownerGeneration: "owner-A", toolName: "stale transport" };
  f.streams.get("A").enqueue(new TextEncoder().encode("data: " + JSON.stringify(event) + "\n\n"));
  f.bridge.detach();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  f.bridge.activate("A");
  assert.equal(f.events.length, offset);
  assert.equal(handle.queue.length, 0);
});
test("response arriving after transport generation changes is rejected even for same loopback origin", async t => {
  const f = fixture(t); const original = f.bridge.fetcher;
  f.bridge.fetcher = async (...args) => { const r = await original(...args); f.controller.generation++; return r; };
  await assert.rejects(f.bridge.identity(), /CONNECTION_CHANGED/);
});
test("slow JSON body is also rejected when transport generation changes after headers", async t => {
  const f = fixture(t); const original = f.bridge.fetcher;
  f.bridge.fetcher = async (...args) => {
    const r = await original(...args), originalJSON = r.json.bind(r);
    r.json = async () => { const value = await originalJSON(); f.controller.generation++; return value; };
    return r;
  };
  await assert.rejects(f.bridge.identity(), /CONNECTION_CHANGED/);
});
test("upload response received across reconnect cannot grant attachment authority", async t => {
  const f = fixture(t), original = f.bridge.fetcher;
  f.bridge.fetcher = async (...args) => {
    const r = await original(...args);
    if (new URL(args[0]).pathname === "/api/uploads") {
      const originalJSON = r.json.bind(r);
      r.json = async () => { const value = await originalJSON(); f.controller.generation++; return value; };
    }
    return r;
  };
  await assert.rejects(f.bridge.upload({ cwd: projectA, name: "attachment.bin", data: Buffer.from([0, 1, 2, 255]).toString("base64") }), /CONNECTION_CHANGED/);
  assert.equal(f.bridge.uploads.size, 0);
});
test("binary artifact preview from a stale transport generation is rejected", async t => {
  const f = fixture(t);
  f.bridge.fetcher = async url => {
    const parsed = new URL(url);
    if (parsed.searchParams.get("type") === "meta") return json({ size: 4 });
    assert.equal(parsed.searchParams.get("type"), "download");
    return { ok: true, arrayBuffer: async () => { f.controller.generation++; return new Uint8Array([1, 2, 3, 4]).buffer; } };
  };
  await assert.rejects(f.bridge.preview(projectA + "/artifact.png"), /CONNECTION_CHANGED/);
});
test("opening a project validates and registers its Linux path on the remote server", async t => {
  const f = fixture(t), chosen = await f.bridge.openProject(projectA);
  assert.equal(chosen, projectA);
  assert.deepEqual(posts(f).map(r => [r.route, r.body.cwd]), [["/api/cwd/validate", projectA], ["/api/projects/registered", projectA]]);
  await assert.rejects(f.bridge.openProject("C:\\Users\\fixture\\project"), /REMOTE_LINUX_PATH_REQUIRED/);
  assert.equal(posts(f).length, 2);
});
test("server-only identity types, paths and implementation reject local alternatives", () => {
  for (const id of [123, {}, null]) assert.throws(() => sessionId(id), /INVALID_SESSION_ID/);
  for (const value of ["C:\\project", "relative", "file:///tmp/a"]) assert.throws(() => remotePath(value), /REMOTE_LINUX_PATH_REQUIRED/);
  const source = fs.readFileSync(path.join(__dirname, "biomind-bridge.cjs"), "utf8");
  assert(!/require\([^)]*(?:pi-coding-agent|child_process|pi-bridge|session-store)/.test(source));
});
