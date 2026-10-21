import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { EventEmitter } from "node:events";
import { fileURLToPath } from "node:url";
import { PassThrough } from "node:stream";
import test from "node:test";

// Only a compiler is reused. No BioMind SDK dependency is loaded into Studio.
const require = createRequire(import.meta.url);
const ts = require(process.env.BIOMIND_TEST_TYPESCRIPT || "typescript");
function load(file, mocks = {}) {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } });
  const loadedModule = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loadedModule, loadedModule.exports);
  return loadedModule.exports;
}
const { BioMindChildSession, BioMindTransportError, bioMindErrorBody } = load("./biomind-session-adapter.ts", { "./biomind-package-gate": load("./biomind-package-gate.ts") });
const { boundedEventStream } = load("./bounded-event-stream.ts");
const scope = { serverId: "fixture-server", project: "/fixture/project", sessionId: "native-id", ownerGeneration: "generation-one" };
function fixture(options = {}) {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const writes = [];
  let data = "";
  child.stdin.on("data", chunk => {
    data += chunk;
    for (;;) { const i = data.indexOf("\n"); if (i < 0) break; writes.push(JSON.parse(data.slice(0, i))); data = data.slice(i + 1); }
  });
  let args;
  const session = new BioMindChildSession({ python: "/fixture/python", entrypoint: "/fixture/BioMind entry.py", config: "/fixture/runtime.json", cwd: scope.project, serverId: scope.serverId, pipeLauncher: options.pipeLauncher,
    sessionFile: options.sessionFile, spawnChild: (...values) => { args = values; return child; }, timeoutMs: 1000 });
  const frame = value => child.stdout.write(JSON.stringify(value) + "\n");
  if (!options.deferReady) frame({ type: "ready", protocolVersion: 1, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project, sessionFile: "/fixture/session.jsonl",
    model: { provider: "fixture", id: "fixture-vision", input: ["text", "image"] }, inputLimits: { maxImages: 4 }, capabilities: { getState: true, automaticReplay: false }, ...options.readyPatch });
  return { child, session, writes, frame, args };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test("startup lock errors survive child exit and retain only bounded public diagnostics", async () => {
  for (const details of [{ stage: "resource_admission", waitMs: 5000 }, { stage: "resource_lock", waitMs: 0 }]) {
    const f = fixture({ deferReady: true });
    const events = [];
    f.session.onEvent(event => events.push(event));
    const pending = f.session.waitUntilReady();
    f.frame({ type: "error", code: "BUSY", ...details, message: "/private/config", stack: "secret-stack", credential: "private-token" });
    f.child.emit("close", 2);
    await assert.rejects(pending, error => {
      assert.deepEqual(bioMindErrorBody(error), { error: "BUSY", code: "BUSY", rejected: true, ...details });
      return true;
    });
    assert.equal(f.session.isAlive(), false);
    assert.equal(f.writes.length, 0);
    assert.equal(events.length, 1);
    assert.equal(events[0].code, "BUSY");
    assert.equal(events[0].automaticReplay, false);
    assert.doesNotMatch(JSON.stringify(events), /private|secret-stack/);
  }
});

test("startup errors redact unknown codes and malformed lock diagnostics", async () => {
  const cases = [
    [{ code: "PI_INITIALIZATION_FAILED", message: "private-token" }, "PI_INITIALIZATION_FAILED"],
    [{ code: "SECRET_TOKEN", stage: "resource_admission", waitMs: 1 }, "BIOMIND_START_FAILED"],
    [{ code: "BUSY", stage: "/private/path", waitMs: 5000 }, "BUSY"],
    ...[-1, 5001, 0.5, "5000", null].map(waitMs => [{ code: "BUSY", stage: "resource_admission", waitMs }, "BUSY"]),
  ];
  for (const [frame, code] of cases) {
    const f = fixture({ deferReady: true });
    const pending = f.session.waitUntilReady();
    f.frame({ type: "error", ...frame });
    await assert.rejects(pending, error => {
      assert.deepEqual(bioMindErrorBody(error), { error: code, code, rejected: true });
      return true;
    });
    f.child.emit("close", 2);
    assert.equal(f.writes.length, 0);
  }
});

test("startup errors reject a waiting send without forwarding or replaying its prompt", async () => {
  const f = fixture({ deferReady: true });
  const pending = f.session.send({ type: "prompt", message: "must not run", ...scope, clientMessageId: "start-pending" });
  f.frame({ type: "error", code: "BUSY", stage: "resource_admission", waitMs: 5000 });
  await assert.rejects(pending, error => error.code === "BUSY" && error.rejected);
  f.child.emit("close", 2);
  assert.equal(f.writes.length, 0);
});

test("a terminal startup error ignores later ready and event frames in the same chunk", async () => {
  const f = fixture({ deferReady: true });
  const events = [];
  f.session.onEvent(event => events.push(event));
  const pending = f.session.send({ type: "prompt", message: "must stay unsent", ...scope, clientMessageId: "terminal-start" });
  f.child.stdout.write([
    { type: "error", code: "BUSY", stage: "resource_admission", waitMs: 5000 },
    { type: "ready", protocolVersion: 1, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project },
    { type: "event", event: { type: "agent_start" } },
  ].map(frame => JSON.stringify(frame) + "\n").join(""));
  f.child.emit("close", 2);
  await assert.rejects(pending, error => error.code === "BUSY" && error.rejected);
  assert.equal(f.session.isAlive(), false);
  assert.equal(f.session.isRunning(), false);
  assert.equal(f.session.sessionId, "");
  assert.equal(f.session.ownerGeneration, "");
  assert.equal(f.session.liveSnapshot().active, false);
  assert.equal(events.length, 1);
  assert.equal(events[0].code, "BUSY");
  assert.equal(f.writes.length, 0);
});

test("formal Python argv and no fallback; acceptance requires actual host receipt", async () => {
  const f = fixture();
  assert.deepEqual(f.args.slice(0, 2), ["/fixture/python", ["-B", "/fixture/BioMind entry.py", "--pi-mode", "json", "--project-dir", scope.project, "--pi-config", "/fixture/runtime.json"]]);
  assert.equal(f.args[2].shell, false);
  let settled = false;
  const result = f.session.send({ type: "prompt", message: "hello", ...scope, clientMessageId: "m1" }).then(value => { settled = true; return value; });
  await tick(); assert.equal(settled, false); assert.equal(f.writes.length, 1);
  const sent = f.writes[0]; assert.equal(sent.text, "hello"); assert.equal(sent.serverId, undefined);
  f.frame({ type: "accepted", status: "accepted", command: "prompt", id: sent.id, clientMessageId: "m1", sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration });
  assert.equal((await result).status, "accepted"); assert.equal(f.session.isRunning(), true);
  f.frame({ type: "result", runtime_status: "completed", scientific_success: null });
  assert.equal(f.session.isRunning(), false); f.session.destroy();
});

test("cross-server/project scope and unsupported SDK commands reject before write", async () => {
  const f = fixture();
  for (const patch of [{ project: "/wrong" }, { serverId: "other-server" }, { ownerGeneration: "old" }, { clientMessageId: {} }]) {
    await assert.rejects(f.session.send({ type: "prompt", message: "x", ...scope, clientMessageId: "m1", ...patch }), e => e.rejected === true);
  }
  for (const type of ["clear_queue", "inject_context", "bash", "set_session_name", "fork"]) {
    await assert.rejects(f.session.send({ type }), e => e.code === "UNSUPPORTED_BIOMIND_COMMAND" && e.rejected);
  }
  assert.equal(f.writes.length, 0); f.session.destroy();
});

test("host validation rejection is distinct from owner loss after writing", async () => {
  const f = fixture();
  const rejected = f.session.send({ type: "prompt", message: "", images: [], ...scope, clientMessageId: "m1" });
  await tick(); f.frame({ type: "error", id: f.writes[0].id, code: "EMPTY_INPUT" });
  await assert.rejects(rejected, e => bioMindErrorBody(e).rejected === true);
  const uncertain = f.session.send({ type: "prompt", message: "hello", ...scope, clientMessageId: "m2" });
  await tick(); f.child.emit("close", 1);
  await assert.rejects(uncertain, e => bioMindErrorBody(e).rejected === false && bioMindErrorBody(e).admission === "unknown");
  assert.equal(f.writes.length, 2);
});

test("duplicate receipt original id resolves by stable client id without replaying automatically", async () => {
  const f = fixture();
  const result = f.session.send({ type: "steer", message: "next", ...scope, clientMessageId: "same-id" });
  await tick(); f.frame({ type: "accepted", status: "accepted", id: "old-id", clientMessageId: "same-id", duplicate: true });
  assert.equal((await result).duplicate, true); assert.equal(f.writes.length, 1); f.session.destroy();
});

test("original UI identities survive reconnect and require explicit confirmation plus host ACK", async () => {
  const f = fixture();
  const request = { type: "approval", id: "ui-original", sessionId: scope.sessionId, requestId: "input-request", businessRequestId: "domain-request", question: "Install fixture?", proposal: { action_hash: "fixture-authority" } };
  f.frame(request);
  const received = []; const off = f.session.onEvent(event => received.push(event));
  assert.equal(received[0].id, "ui-original"); assert.equal(received[0].businessRequestId, "domain-request");
  off(); assert.equal(f.child.stdin.writableEnded, false);
  await assert.rejects(f.session.send({ ...received[0], type: "extension_ui_response", confirmed: undefined }), e => e.code === "EXPLICIT_CONFIRMATION_REQUIRED");
  await assert.rejects(f.session.send({ ...received[0], type: "extension_ui_response", confirmed: true, businessRequestId: "other" }), e => e.code === "STALE_UI_RESPONSE");
  const pending = f.session.send({ ...received[0], type: "extension_ui_response", confirmed: false });
  await tick(); assert.deepEqual(f.writes[0].value, { choice: "deny" }); assert.equal(f.writes[0].action_hash, undefined);
  const replay = []; const off2 = f.session.onEvent(event => replay.push(event)); assert.equal(replay.length, 1); off2();
  f.frame({ type: "ui_response_result", id: "ui-original", status: "accepted" }); await pending;
  const after = []; f.session.onEvent(event => after.push(event)); assert.equal(after.length, 0); f.session.destroy();
});

test("state keeps admissions, native queues and unknown jobs; no listener can break others", async () => {
  const f = fixture(); let received = 0;
  f.session.onEvent(() => { throw new Error("bad-page"); }); f.session.onEvent(() => received++);
  const pending = f.session.send({ type: "get_state" }); await tick();
  f.frame({ type: "state", id: f.writes[0].id, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project,
    active: false, queue: { pendingAssociationCount: 2, actualHasQueuedMessages: true }, admissions: [{ clientMessageId: "x", status: "unknown" }], jobs: { status: "unknown" }, safeToRelease: false });
  const state = await pending; assert.equal(state.admissions[0].status, "unknown"); assert.equal(state.pendingMessageCount, 2);
  assert.equal(state.jobs.status, "unknown"); assert.equal(state.safeToRelease, false); assert.ok(received > 0); assert.equal(f.session.isAlive(), true); f.session.destroy();
});

test("coalesced and fragmented UTF-8 output remains exact, continuously drained detached", async () => {
  const f = fixture(); const seen = []; const off = f.session.onEvent(event => seen.push(event));
  const bytes = Buffer.from(JSON.stringify({ type: "event", event: { type: "message_update", content: "中文\u2028x" } }) + "\r\n" + JSON.stringify({ type: "event", event: { type: "message_end" } }) + "\n");
  for (let i = 0; i < bytes.length; i++) f.child.stdout.write(bytes.subarray(i, i + 1));
  assert.equal(seen[0].content, "中文\u2028x"); assert.equal(seen[1].type, "message_end");
  off(); f.frame({ type: "event", event: { type: "agent_start" } });
  assert.equal(f.session.isRunning(), true); assert.equal(f.child.stdin.writableEnded, false); f.session.destroy();
});

test("slow SSE subscriber is detached with resync and never shuts down owner", async () => {
  let emit; let detached = 0;
  const abort = new AbortController();
  const stream = boundedEventStream({ signal: abort.signal, initial: { type: "connected" }, maxBytes: 100, maxEvents: 2,
    subscribe: callback => { emit = callback; return () => detached++; } });
  for (let i = 0; i < 100; i++) emit({ type: "message_update", text: "x".repeat(90) });
  const reader = stream.getReader(); let output = "";
  for (;;) { const next = await reader.read(); if (next.done) break; output += new TextDecoder().decode(next.value); }
  assert.match(output, /resync_required/); assert.equal(detached, 1);
});

test("actual existing-session POST never resumes unknown history and waits on host receipt", async () => {
  const f = fixture(); let existing; let starts = 0; let scans = 0;
  const route = load("../app/api/agent/[id]/route.ts", {
    "next/server": { NextResponse: { json: Response.json } },
    "@/lib/session-reader": { resolveSessionPath: async () => { scans++; return "/history.jsonl"; } },
    "@/lib/rpc-manager": { getRpcSession: () => existing, startRpcSession: async () => { starts++; } },
    "@/lib/biomind-profile": { isBioMindRemoteProfile: () => true, readBioMindRequest: req => req.json() },
    "@/lib/biomind-session-adapter": { BioMindTransportError, bioMindErrorBody },
    "@/lib/remote-security": { isBioMindProjectAllowed: () => true },
    "@/lib/biomind-resume": { inspectBioMindResume: async () => { scans++; throw new Error("Ordinary prompt must not inspect or resume history"); } },
  });
  const params = { params: Promise.resolve({ id: scope.sessionId }) };
  const request = () => new Request("http://localhost/api/agent/native-id", { method: "POST", body: JSON.stringify({ type: "prompt", message: "hello", ...scope, clientMessageId: "route-message" }) });
  const missing = await route.POST(request(), params);
  assert.equal(missing.status, 409); assert.equal(starts, 0); assert.equal(scans, 0);
  assert.equal((await missing.json()).rejected, true);
  existing = f.session;
  let done = false; const posting = route.POST(request(), params).then(response => { done = true; return response; });
  await tick(); assert.equal(done, false);
  f.frame({ type: "accepted", status: "accepted", id: f.writes[0].id, clientMessageId: "route-message" });
  const response = await posting; const body = await response.json();
  assert.equal(body.success, true); assert.equal(body.data.status, "accepted"); f.session.destroy();
});

test("mutating control requires current full server/project/session/owner scope", async () => {
  const f = fixture();
  for (const command of [{ type: "abort" }, { type: "job", action: "cancel", run_ref: "fixture", ...scope, ownerGeneration: "old" }]) {
    await assert.rejects(f.session.send(command), e => e.rejected === true);
  }
  assert.equal(f.writes.length, 0);
  const stopped = f.session.send({ type: "abort", ...scope }); await tick();
  f.frame({ type: "abort_result", id: f.writes[0].id, status: "requested" });
  assert.equal((await stopped).status, "requested"); f.session.destroy();
});

test("configured shipping pipe launcher preserves formal entry argv without shell", async () => {
  const f = fixture({ pipeLauncher: "/fixture/POSIX launcher.py" });
  assert.deepEqual(f.args.slice(0, 2), ["/fixture/python", ["-B", "/fixture/POSIX launcher.py", "/fixture/python", "-B", "/fixture/BioMind entry.py", "--pi-mode", "json", "--project-dir", scope.project, "--pi-config", "/fixture/runtime.json"]]);
  assert.equal(f.args[2].shell, false); await f.session.waitUntilReady(); f.session.destroy();
});

test("HTTP JSON bound applies before parse and invalid shapes are explicit rejections", async () => {
  const profile = load("./biomind-profile.ts", {
    "./biomind-session-adapter": { BioMindTransportError },
    "./remote-security": { getBioMindProjectRoots: () => new Set([scope.project]), isBioMindRemoteProfile: () => true },
  });
  for (const body of ["[1]", "null", "{", " ".repeat(10 * 1024 * 1024 + 1)]) {
    await assert.rejects(profile.readBioMindRequest(new Request("http://localhost/api/agent/new", { method: "POST", body })), error => error.rejected === true);
  }
  assert.deepEqual(await profile.readBioMindRequest(new Request("http://localhost/api/agent/new", { method: "POST", body: '{"type":"get_state"}' })), { type: "get_state" });
});

test("ready rejects incompatible protocol and incomplete or mismatched owner identity", async () => {
  for (const readyPatch of [{ protocolVersion: 2 }, { ownerGeneration: "" }, { sessionId: "" }, { cwd: "/other" }]) {
    const f = fixture({ readyPatch });
    await assert.rejects(f.session.waitUntilReady(), error => error.code === "BIOMIND_READY_IDENTITY_MISMATCH");
    assert.equal(f.session.isAlive(), false);
  }
});

test("compact transparently forwards custom instructions with original scoped control", async () => {
  const f = fixture();
  const result = f.session.send({ type: "compact", ...scope, customInstructions: "保留结果来源与未决审批", arbitrary: "excluded" });
  await tick(); assert.equal(f.writes[0].customInstructions, "保留结果来源与未决审批");
  assert.equal(f.writes[0].arbitrary, undefined);
  f.frame({ type: "compact_result", id: f.writes[0].id, status: "completed" });
  await result; f.session.destroy();
});

test("failed or unrelated UI/abort ACK preserves pending; explicit cancellation waits for correlated ACK", async () => {
  const f = fixture();
  f.frame({ type: "question", id: "pending-ui", sessionId: scope.sessionId, requestId: "input", businessRequestId: "business", question: "Choose", choices: ["one", "two"] });
  const questions = () => { const seen = []; const off = f.session.onEvent(event => seen.push(event)); off(); return seen; };
  const question = questions()[0];
  f.frame({ type: "ui_response_result", id: "pending-ui", status: "accepted" });
  assert.equal(questions().length, 1);
  const rejected = f.session.send({ ...question, type: "extension_ui_response", value: "one" });
  await tick(); f.frame({ type: "ui_response_result", id: "pending-ui", status: "error", code: "STALE_UI_RESPONSE" });
  await assert.rejects(rejected); assert.equal(questions().length, 1);
  const abort = f.session.send({ type: "abort", ...scope });
  await tick(); f.frame({ type: "abort_result", id: f.writes.at(-1).id, status: "error", code: "CONTROL_BUSY" });
  await assert.rejects(abort); assert.equal(questions().length, 1);
  const cancelled = f.session.send({ ...question, type: "extension_ui_response", cancelled: true });
  await tick(); assert.deepEqual(f.writes.at(-1).value, { cancelled: true }); assert.equal(questions().length, 1);
  f.frame({ type: "ui_response_result", id: "pending-ui", status: "accepted" });
  await cancelled; assert.equal(questions().length, 0); f.session.destroy();
});

test("actual SSE route subscribes before state and reconnects to same owner without prompt replay", async () => {
  const f = fixture(); let starts = 0;
  const route = load("../app/api/agent/[id]/events/route.ts", {
    "@/lib/session-reader": { resolveSessionPath: () => { throw new Error("no history reopen"); } },
    "@/lib/rpc-manager": { getRpcSession: () => f.session, startRpcSession: () => { starts++; } },
    "@/lib/biomind-profile": { isBioMindRemoteProfile: () => true },
    "@/lib/remote-security": { isBioMindProjectAllowed: () => true },
    "@/lib/bounded-event-stream": { boundedEventStream },
  });
  for (let connection = 0; connection < 2; connection++) {
    const abort = new AbortController();
    const response = await route.GET(new Request("http://localhost/api/agent/native-id/events", { signal: abort.signal }), { params: Promise.resolve({ id: scope.sessionId }) });
    const reader = response.body.getReader();
    await tick(); assert.equal(f.writes.at(-1).type, "get_state");
    const request = { type: "question", id: `during-snapshot-${connection}`, sessionId: scope.sessionId, requestId: "input", businessRequestId: "business", question: "Race" };
    f.frame(request);
    f.frame({ type: "state", id: f.writes.at(-1).id, active: true, pendingUI: [request], sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project });
    let output = "";
    while (!output.includes('"type":"state"')) output += new TextDecoder().decode((await reader.read()).value);
    assert.match(output, /extension_ui_request/); assert.match(output, /automaticReplay/);
    abort.abort(); await reader.cancel(); assert.equal(f.session.isAlive(), true);
  }
  assert.equal(starts, 0); assert.deepEqual(f.writes.map(frame => frame.type), ["get_state", "get_state"]); f.session.destroy();
});


test("S3A controls retain exact scope, whitelist payloads and wait for native result", async () => {
  const f = fixture();
  for (const [type, resultType, payload] of [
    ["set_model", "model_result", { provider: "synthetic", modelId: "second" }],
    ["set_thinking_level", "thinking_result", { level: "off" }],
    ["set_tools", "tools_result", { tools: [] }],
    ["save_instructions", "instructions_result", { scope: "global", expectedVersion: "old-hash", text: "", create: false }],
    ["set_skill", "resources_result", { resourceId: "synthetic-resource", scope: "project", enabled: false }],
    ["set_auto_compaction", "auto_compaction_result", { enabled: false, scope: "session" }],
  ]) {
    const before = f.writes.length;
    await assert.rejects(f.session.send({ type, ...payload }), error => error.code === "MESSAGE_SCOPE_MISMATCH");
    assert.equal(f.writes.length, before);
    const pending = f.session.send({ type, ...scope, ...payload, arbitraryPath: "/outside", persist: true });
    await tick(); const sent = f.writes.at(-1);
    assert.equal(sent.arbitraryPath, undefined); assert.equal(sent.persist, undefined);
    assert.equal(sent.ownerGeneration, scope.ownerGeneration);
    for (const [key, value] of Object.entries(payload)) assert.deepEqual(sent[key], value);
    f.frame({ type: resultType, id: sent.id, data: { applied: true } });
    assert.deepEqual((await pending).data, { applied: true });
  }
  f.session.destroy();
});

test("live capabilities override ready snapshot while unknown fields survive", async () => {
  const f = fixture({ readyPatch: { capabilities: { setModel: false, reload: true } } });
  const pending = f.session.send({ type: "get_state" }); await tick();
  f.frame({ type: "state", id: f.writes.at(-1).id, capabilities: { setModel: { supported: true, available: false, reason: "REQUEST_ACTIVE" } }, model: { provider: "synthetic", id: "nonreasoning" }, thinkingLevel: "off" });
  const result = await pending;
  assert.deepEqual(result.capabilities.setModel, { supported: true, available: false, reason: "REQUEST_ACTIVE" });
  assert.equal(result.capabilities.reload, true); assert.equal(result.thinkingLevel, "off");
  f.session.destroy();
});


test("server-only restored session path becomes formal --session argv without shell", async () => {
  const f = fixture({ sessionFile: "/fixture/project/session one.jsonl" });
  assert.deepEqual(f.args[1].slice(-2), ["--session", "/fixture/project/session one.jsonl"]);
  assert.equal(f.args[2].shell, false);
  await f.session.waitUntilReady(); f.session.destroy();
});


test("explicit native editor retains multiline prefill and all identities through state replay and accepted answer", async () => {
  const f = fixture();
  const original = { type: "question", method: "editor", prefill: "预填第一行\nsecond line", id: "editor-original", sessionId: scope.sessionId,
    ownerGeneration: scope.ownerGeneration, requestId: "input-editor", businessRequestId: "editor-tool-call", question: "Edit the synthetic answer" };
  f.frame({ type: "state", pendingUI: [original], active: true });
  const replay = []; const off = f.session.onEvent(event => replay.push(event)); off();
  const question = replay[0]; assert.equal(question.type, "extension_ui_request"); assert.equal(question.method, "editor");
  for (const key of ["prefill", "id", "sessionId", "ownerGeneration", "requestId", "businessRequestId"]) assert.equal(question[key], original[key]);
  await assert.rejects(f.session.send({ ...question, type: "extension_ui_response", value: "wrong", businessRequestId: "stale-call" }), error => error.code === "STALE_UI_RESPONSE");
  assert.equal(f.writes.length, 0);
  const answer = "  用户修改第一行\nsecond edited line\n";
  const pending = f.session.send({ ...question, type: "extension_ui_response", value: answer }); await tick();
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].type, "ui_response"); assert.equal(f.writes[0].value, answer);
  for (const key of ["id", "sessionId", "ownerGeneration", "requestId", "businessRequestId"]) assert.equal(f.writes[0][key], original[key]);
  const beforeAck=[]; const beforeOff=f.session.onEvent(event=>beforeAck.push(event));beforeOff();assert.equal(beforeAck.length,1);
  f.frame({ type: "ui_response_result", id: original.id, status: "accepted", ...scope, requestId: original.requestId, businessRequestId: original.businessRequestId }); await pending;
  const afterAck=[]; const afterOff=f.session.onEvent(event=>afterAck.push(event));afterOff();assert.equal(afterAck.length,0);f.session.destroy();
});

test("editor cancel retains original pending identity until true host ACK and never becomes approval", async () => {
  const f=fixture();f.frame({type:"question",method:"editor",prefill:"保留的草稿",id:"editor-cancel",sessionId:scope.sessionId,requestId:"input",businessRequestId:"business",question:"Edit"});
  const events=[];const off=f.session.onEvent(event=>events.push(event));off();const question=events[0];
  const pending=f.session.send({...question,type:"extension_ui_response",cancelled:true});await tick();
  assert.deepEqual(f.writes[0].value,{cancelled:true});assert.equal(f.writes[0].businessRequestId,"business");
  const still=[];const off2=f.session.onEvent(event=>still.push(event));off2();assert.equal(still.length,1);
  f.frame({type:"ui_response_result",id:question.id,status:"accepted"});await pending;f.session.destroy();
});

test("old question select/input and approval confirm stay compatible; malformed editor fails closed", async () => {
  for(const [request,method] of [[{type:"question"},"input"],[{type:"question",choices:["A","B"]},"select"],[{type:"approval"},"confirm"]]){
    const f=fixture();f.frame({...request,id:"legacy-ui",sessionId:scope.sessionId,requestId:"input",businessRequestId:"business",question:"Old request"});
    const seen=[];const off=f.session.onEvent(event=>seen.push(event));off();assert.equal(seen[0].method,method);f.session.destroy();
  }
  for(const malformed of [{prefill:{text:"invalid"}},{choices:["cannot mix editor with select"]},{choices:[]},{prefill:"中".repeat(22000)},{method:undefined,prefill:"requires editor"},{type:"approval"}]){
    const f=fixture();const seen=[];f.session.onEvent(event=>seen.push(event));f.frame({type:"question",method:"editor",id:"bad-editor",...malformed});
    assert.equal(f.session.isAlive(),false);assert.equal(seen.at(-1).code,"BIOMIND_INVALID_UI_REQUEST");assert.equal(f.writes.length,0);
  }
});

test("disconnected subscribers recover current whole message and latest tool state without a second store", async () => {
  const f=fixture();const received=[];const off=f.session.onEvent(event=>received.push(event));
  f.frame({type:"event",event:{type:"agent_start"}});off();
  const message={role:"assistant",timestamp:123,content:[{type:"text",text:"partial"}]};
  f.frame({type:"event",event:{type:"message_start",message:{...message,content:[]}}});
  f.frame({type:"event",event:{type:"message_update",message,assistantMessageEvent:{type:"text_delta",delta:"partial"}}});
  f.frame({type:"event",event:{type:"message_update",message:{...message,content:[{type:"text",text:"partial complete"}]},assistantMessageEvent:{type:"text_delta",delta:" complete"}}});
  f.frame({type:"event",event:{type:"tool_execution_start",toolCallId:"tool-1",toolName:"bash",args:{command:"pwd"}}});
  f.frame({type:"event",event:{type:"tool_execution_update",toolCallId:"tool-1",partialResult:{content:[{type:"text",text:"working"}]}}});
  const snapshot=f.session.liveSnapshot();
  assert.equal(snapshot.sessionId,scope.sessionId);assert.equal(snapshot.ownerGeneration,scope.ownerGeneration);
  assert.equal(snapshot.active,true);assert.equal(snapshot.truncated,false);
  const updates=snapshot.events.filter(event=>event.type==="message_update");
  assert.equal(updates.length,1);assert.equal(updates[0].message.content[0].text,"partial complete");
  assert.equal(updates[0].assistantMessageEvent,undefined); // whole message, no replayed delta
  assert.equal(snapshot.events.find(event=>event.type==="tool_execution_update").partialResult.content[0].text,"working");
  assert.equal(f.child.stdin.writableEnded,false);assert.equal(f.writes.length,0);
  updates[0].message.content[0].text="client mutation";
  assert.equal(f.session.liveSnapshot().events.find(event=>event.type==="message_update").message.content[0].text,"partial complete");
  f.session.destroy();
});

test("live recovery is per owner; canonical UI events precede SSE state and HTTP keeps original authority", async () => {
  const a=fixture(),b=fixture({readyPatch:{sessionId:"native-b",ownerGeneration:"generation-b"}});
  a.frame({type:"event",event:{type:"agent_start"}});
  a.frame({type:"event",event:{type:"message_start",message:{role:"assistant",timestamp:44,content:[{type:"text",text:"A-only"}]}}});
  assert.deepEqual(b.session.liveSnapshot().events,[]);
  const streamed=[];const off=a.session.onEvent(event=>streamed.push(event));
  const pending=a.session.send({type:"get_state"});await tick();
  a.frame({type:"state",id:a.writes.at(-1).id,active:true,pendingUI:[{type:"approval",id:"original-approval",sessionId:scope.sessionId,requestId:"input",businessRequestId:"business",question:"Allow?"}],queue:{}});
  const state=await pending;
  assert.equal(state.liveSnapshot.events.some(event=>event.message?.content?.[0]?.text==="A-only"),true);
  const event=streamed.find(row=>row.type==="state");
  assert.equal(event.liveSnapshot.sessionId,scope.sessionId);
  assert.equal(event.liveSnapshot.sequence,event.transportSequence);
  assert.ok(event.liveSnapshot.events.every(row=>row.transportSequence<=event.liveSnapshot.sequence));
  assert.deepEqual(streamed.map(row=>row.type),["extension_ui_request","state"]);
  assert.equal(streamed[0].id,"original-approval");
  assert.equal(streamed[0].method,"confirm");
  assert.equal(streamed[0].businessRequestId,"business");
  assert.ok(streamed[0].transportSequence<event.transportSequence);
  assert.equal(state.displayPendingUI[0].id,"original-approval");
  assert.equal(state.displayPendingUI[0].method,"confirm");
  assert.equal(state.displayPendingUI[0].businessRequestId,"business");
  streamed.length=0;
  const refreshed=a.session.send({type:"get_state"});await tick();
  a.frame({type:"state",id:a.writes.at(-1).id,active:true,pendingUI:[],queue:{}});
  assert.deepEqual((await refreshed).displayPendingUI,[]);
  assert.deepEqual(streamed.map(row=>row.type),["extension_ui_resolved","state"]);
  assert.equal(streamed[0].id,"original-approval");
  assert.ok(streamed[0].transportSequence<streamed[1].transportSequence);
  off();a.session.destroy();b.session.destroy();
});

test("completed request clears ephemeral transcript so history is not replayed twice", async () => {
  const f=fixture();f.frame({type:"event",event:{type:"agent_start"}});
  f.frame({type:"event",event:{type:"message_start",message:{role:"assistant",timestamp:9,content:[]}}});
  f.frame({type:"event",event:{type:"message_update",message:{role:"assistant",timestamp:9,content:[{type:"text",text:"done"}]}}});
  f.frame({type:"event",event:{type:"message_end",message:{role:"assistant",timestamp:9,content:[{type:"text",text:"done"}]}}});
  assert.equal(f.session.liveSnapshot().events.some(row=>row.type==="message_update"),false);
  f.frame({type:"result",runtime_status:"completed"});
  assert.deepEqual(f.session.liveSnapshot().events,[]);assert.equal(f.session.liveSnapshot().active,false);
  f.frame({type:"event",event:{type:"agent_start"}});
  assert.equal(f.session.liveSnapshot().events.some(row=>row.message?.timestamp===9),false);
  f.session.destroy();
});

test("live transcript is bounded and advertises omitted oversized content", async () => {
  const f=fixture();f.frame({type:"event",event:{type:"agent_start"}});
  f.frame({type:"event",event:{type:"message_start",message:{role:"assistant",content:[{type:"text",text:"x".repeat(600*1024)}]}}});
  assert.equal(f.session.liveSnapshot().truncated,true);
  for(let i=0;i<530;i++)f.frame({type:"event",event:{type:"tool_execution_start",toolCallId:`t-${i}`,toolName:"bash",args:{command:"pwd"}}});
  const snapshot=f.session.liveSnapshot();assert.ok(snapshot.events.length<=512);assert.equal(snapshot.truncated,true);
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot))<2.1*1024*1024);
  f.session.destroy();
});


test("package prepare freezes input; cancel releases same native owner without sending prompt", async () => {
  const f = fixture(); const prepared = f.session.preparePackageShutdown(); await tick();
  const sent = f.writes.at(-1); assert.equal(sent.type, "prepare_packages_shutdown");
  f.frame({ type: "packages_shutdown_ready", id: sent.id, safeToRelease: true, packagesIdle: true, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project });
  await prepared;
  await assert.rejects(f.session.send({ type: "prompt", message: "must not send", ...scope, clientMessageId: "m1" }), /PACKAGE_OPERATION_IN_PROGRESS/);
  const cancelled = f.session.cancelPackageShutdown(); await tick();
  f.frame({ type: "packages_shutdown_cancelled", id: f.writes.at(-1).id, safeToRelease: false, packagesIdle: false, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration });
  await cancelled; assert.equal(f.session.isAlive(), true);
  assert.deepEqual(f.writes.map(row => row.type), ["prepare_packages_shutdown", "resume_packages_operations"]); f.session.destroy();
});
test("package shutdown resolves only on real child close and retains native ownership until then", async () => {
  const f = fixture(); const prepared = f.session.preparePackageShutdown(); await tick();
  f.frame({ type: "packages_shutdown_ready", id: f.writes.at(-1).id, safeToRelease: true, packagesIdle: true, sessionId: scope.sessionId, ownerGeneration: scope.ownerGeneration, cwd: scope.project }); await prepared;
  let finished = false; const stopped = f.session.shutdownForPackages().then(() => { finished = true; });
  await tick(); assert.equal(finished, false); assert.equal(f.session.isAlive(), true); assert.equal(f.session.packageExitConfirmed(), false);
  assert.equal(f.writes.at(-1).type, "shutdown"); f.child.emit("close", 0); await stopped;
  assert.equal(f.session.packageExitConfirmed(), true); assert.equal(f.session.sessionId, scope.sessionId);
});
