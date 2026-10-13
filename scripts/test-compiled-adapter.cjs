"use strict";
// Executes the production webpack module and the source adapter with a fake
// native child. No network, subprocess, model, credential or session file use.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const Module = require("node:module");
const { EventEmitter } = require("node:events");
const root = path.resolve(__dirname, "..");
const backendArg = process.argv.indexOf("--backend-root");
const backend = backendArg >= 0 ? path.resolve(process.argv[backendArg + 1]) : path.join(root, "backend/pi-studio");
const bridgePath = path.join(root, "desktop/electron/biomind-bridge.cjs");
const sourcePath = path.join(backend, "lib/biomind-session-adapter.ts");
const gatePath = path.join(backend, "lib/biomind-package-gate.ts");
const outputArg = process.argv.indexOf("--output");
const outputPath = outputArg >= 0 ? path.resolve(process.argv[outputArg + 1]) : path.join(root, "evidence/compiled-adapter-compatibility.json");
const sha256 = file => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
const serverId = "compiled-contract-server", project = "/synthetic/project", sessionId = "compiled-session", ownerGeneration = "compiled-owner";
const nativeQuestions = [
  { type: "approval", id: "approval-1", sessionId, requestId: "task-1", businessRequestId: "business-1",
    question: "Write this reviewed output?", proposal: { path: "/synthetic/project/output.txt", effects: ["write"] } },
  { type: "question", id: "editor-1", sessionId, requestId: "task-1", businessRequestId: "business-1",
    question: "Review exact text", method: "editor", prefill: "line one\nline two\n" },
];
function loadCompiled() {
  const chunkDir = path.join(backend, ".next-pkg/server/chunks");
  const routeCode = fs.readFileSync(path.join(backend, ".next-pkg/server/app/api/agent/[id]/route.js"), "utf8");
  const externals = new Map([...routeCode.matchAll(/(?:\{|,)(\d+):([A-Za-z_$][\w$]*)=>\{\2\.exports=require\("(node:child_process|node:crypto)"\)\}/g)]
    .map(match => [Number(match[1]), match[3]]));
  assert.equal(externals.size, 2, "actual route must identify both node externals");
  for (const name of fs.readdirSync(chunkDir).filter(name => /^\d+\.js$/.test(name)).sort()) {
    const chunk = path.join(chunkDir, name), text = fs.readFileSync(chunk, "utf8");
    if (!text.includes("BIOMIND_OWNER_CLOSED") || !text.includes("displayPendingUI")) continue;
    const factories = require(chunk).modules;
    for (const [moduleId, factory] of Object.entries(factories)) {
      const code = factory.toString();
      if (!code.includes("BIOMIND_OWNER_CLOSED") || !code.includes("displayPendingUI")) continue;
      const gates = Object.entries(factories).filter(([, value]) => value.toString().includes("__bioMindPackageMutation"));
      assert.equal(gates.length, 1, "actual adapter chunk must identify its one package gate factory");
      const [gateModuleId, gateFactory] = gates[0];
      let gateExports;
      // Resolve IDs from the actual route/chunk, never guess or execute startup.
      // Unknown externals fail rather than importing arbitrary application modules.
      const requireExternal = id => {
        if (externals.get(id) === "node:child_process") return { spawn() { throw new Error("REAL_PROCESS_FORBIDDEN"); } };
        if (externals.get(id) === "node:crypto") return crypto;
        if (id === Number(gateModuleId)) {
          if (!gateExports) { gateExports = {}; gateFactory({}, gateExports, requireExternal); }
          return gateExports;
        }
        throw new Error("UNRECOGNIZED_COMPILED_EXTERNAL_" + id);
      };
      requireExternal.d = (exports, getters) => {
        for (const [key, get] of Object.entries(getters)) Object.defineProperty(exports, key, { enumerable: true, get });
      };
      const exports = {}; factory({}, exports, requireExternal);
      const Adapter = Object.values(exports).find(value => typeof value === "function" && value.prototype.send && value.prototype.liveSnapshot && value.prototype.onEvent);
      assert(Adapter, "actual compiled adapter export must be found");
      return { Adapter, chunk, moduleId: Number(moduleId), sha256: sha256(chunk), gateModuleId: Number(gateModuleId) };
    }
  }
  throw new Error("COMPILED_ADAPTER_NOT_FOUND");
}
function loadSource() {
  const candidates = [path.join(backend, "node_modules/typescript"), path.join(root, "desktop/node_modules/typescript"),
    path.resolve(root, "../BioMind_Studio_S1_Implementation_20261006/candidate/pi-studio/node_modules/typescript")];
  const ts = require(candidates.find(candidate => fs.existsSync(path.join(candidate, "package.json"))));
  const gate = new Module(gatePath, module);
  gate._compile(ts.transpileModule(fs.readFileSync(gatePath, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, gatePath);
  const source = new Module(sourcePath, module), sourceRequire = source.require.bind(source);
  source.require = id => id === "./biomind-package-gate" ? gate.exports : sourceRequire(id);
  source._compile(ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, sourcePath);
  return source.exports.BioMindChildSession;
}
async function fixture(Adapter) {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter();
  child.stdin.writableLength = 0; child.stdin.end = () => {};
  const writes = [];
  let pendingUI = [], active = true;
  const frame = value => child.stdout.emit("data", Buffer.from(JSON.stringify(value) + "\n"));
  child.stdin.write = (encoded, callback) => {
    const command = JSON.parse(encoded); writes.push(command);
    assert.equal(command.type, "get_state", "contract must never submit a task or answer a question");
    frame({ type: "state", id: command.id, active, pendingUI, sessionId, ownerGeneration, project, cwd: project, model: { provider: "server-owned", id: "synthetic-model", input: ["text"] } });
    callback?.(null); return true;
  };
  const owner = new Adapter({ python: "/synthetic/python", entrypoint: "/synthetic/main.py", config: "/synthetic/runtime.json",
    cwd: project, serverId, env: {}, timeoutMs: 1000, spawnChild: () => child });
  frame({ type: "ready", protocolVersion: 1, sessionId, ownerGeneration, cwd: project, capabilities: {} });
  await owner.waitUntilReady();
  return { owner, writes, frame, setQuestions: rows => { pendingUI = rows; }, state: () => frame({ type: "state", active, pendingUI, sessionId, ownerGeneration, cwd: project }),
    stop: () => owner.destroy() };
}
async function adapterContract(Adapter) {
  const f = await fixture(Adapter), events = [];
  try {
    const off = f.owner.onEvent(event => events.push(event));
    f.setQuestions(nativeQuestions); f.state();
    assert.deepEqual(events.map(event => event.type), ["extension_ui_request", "extension_ui_request", "state"]);
    assert.equal(events[0].method, "confirm"); assert.deepEqual(events[0].proposal, nativeQuestions[0].proposal);
    assert.equal(events[1].method, "editor"); assert.equal(events[1].prefill, nativeQuestions[1].prefill);
    assert.equal(events[0].businessRequestId, "business-1"); assert.equal(events[0].ownerGeneration, ownerGeneration);
    assert(events.every((event, i) => event.transportSequence === i + 1));
    assert.equal(events[2].liveSnapshot.sequence, events[2].transportSequence);
    const sseDisplayPendingUI = Array.isArray(events[2].displayPendingUI);
    const replay = [], writesBeforeReplay = f.writes.length;
    const offReplay = f.owner.onEvent(event => replay.push(event));
    assert.deepEqual(replay.map(event => event.id), ["approval-1", "editor-1"]);
    assert.deepEqual(replay[0].proposal, nativeQuestions[0].proposal); assert.equal(replay[1].prefill, nativeQuestions[1].prefill);
    assert.equal(f.writes.length, writesBeforeReplay); offReplay();
    events.length = 0;
    const state = await f.owner.send({ type: "get_state" });
    assert.deepEqual(events.map(event => event.type), ["extension_ui_request", "extension_ui_request", "state"]);
    assert.equal(state.serverId, serverId); assert.equal(state.project, project); assert.equal(state.sessionId, sessionId); assert.equal(state.ownerGeneration, ownerGeneration);
    assert.equal(state.liveSnapshot.sequence, events.at(-1).transportSequence);
    assert.deepEqual(state.displayPendingUI.map(q => [q.id, q.method, q.requestId, q.businessRequestId, q.proposal, q.prefill]),
      [["approval-1", "confirm", "task-1", "business-1", nativeQuestions[0].proposal, undefined], ["editor-1", "editor", "task-1", "business-1", undefined, nativeQuestions[1].prefill]]);
    events.length = 0; f.setQuestions([nativeQuestions[1]]); f.state();
    assert.deepEqual(events.map(event => [event.type, event.id]), [["extension_ui_request", "editor-1"], ["extension_ui_resolved", "approval-1"], ["state", undefined]]);
    events.length = 0; f.setQuestions([]); f.state();
    assert.deepEqual(events.map(event => [event.type, event.id]), [["extension_ui_resolved", "editor-1"], ["state", undefined]]);
    off();
    f.frame({ type: "event", event: { type: "tool_execution_start", toolCallId: "tool-1", toolName: "synthetic-tool", args: {} } });
    f.frame({ type: "event", event: { type: "tool_execution_update", toolCallId: "tool-1", partialResult: { content: [{ type: "text", text: "live progress" }] } } });
    const live = await f.owner.send({ type: "get_state" });
    assert.deepEqual(live.liveSnapshot.events.map(event => event.type), ["tool_execution_start", "tool_execution_update"]);
    assert.equal(live.liveSnapshot.sessionId, sessionId); assert.equal(live.liveSnapshot.ownerGeneration, ownerGeneration);
    assert(f.writes.every(command => command.type === "get_state"));
    return { canonicalRequestBeforeState: true, canonicalResolvedBeforeState: true, subscribeReplaysCompleteQuestions: true,
      httpCompleteDisplayPendingUI: true, httpSequenceCoversEmittedState: true, liveToolSnapshot: true, noTaskOrAnswerSubmitted: true,
      optionalSseDisplayPendingUI: sseDisplayPendingUI };
  } finally { f.stop(); }
}
async function compiledBridgeContract(Adapter) {
  const { BioMindBridge } = require(bridgePath), f = await fixture(Adapter), events = [];
  const controller = { generation: 1, state: { status: "ready", serverId, connectionId: "compiled-connection", origin: "http://127.0.0.1:30142" }, authorization: () => "Basic synthetic" };
  const bridge = new BioMindBridge({ controller, send: (channel, payload) => events.push({ channel, payload }), store: {}, input: { configureInputStorage() {} },
    fetcher: async (url, options) => {
      assert.equal(url, controller.state.origin + "/api/agent/" + sessionId); assert(!options.method || options.method === "GET");
      return new Response(JSON.stringify({ running: true, state: await f.owner.send({ type: "get_state" }) }), { status: 200, headers: { "content-type": "application/json" } });
    } });
  let dropRequests = false;
  try {
    bridge.profile = { serverId }; const h = bridge.handle(sessionId, project);
    h.state = await f.owner.send({ type: "get_state" }); h.watching = true;
    const off = f.owner.onEvent(event => { if (dropRequests && event.type === "extension_ui_request") return; bridge.event(h, event); });
    const beforeCanonical = f.writes.length;
    f.setQuestions(nativeQuestions); f.state();
    assert.equal(f.writes.length, beforeCanonical, "complete normalized events need no fallback GET");
    assert.deepEqual(h.questions.get("approval-1").proposal, nativeQuestions[0].proposal); assert.equal(h.questions.get("editor-1").prefill, nativeQuestions[1].prefill);
    // Deliberately drop normalized deliveries; retain actual compiled raw state.
    // The HTTP fallback itself produces SSE state, exercising in-flight coalescing.
    dropRequests = true; h.questions.clear(); f.state();
    for (let i = 0; i < 4; i++) { if (h.uiRecovery?.pending) await h.uiRecovery.pending; await Promise.resolve(); }
    assert.equal(f.writes.length, beforeCanonical + 1, "one HTTP state read must repair both missing questions without a self-loop");
    assert.deepEqual(h.questions.get("approval-1").proposal, nativeQuestions[0].proposal); assert.equal(h.questions.get("editor-1").prefill, nativeQuestions[1].prefill);
    assert.equal(h.questions.get("approval-1").businessRequestId, "business-1");
    f.state(); await Promise.resolve(); assert.equal(f.writes.length, beforeCanonical + 1);
    assert(!events.some(event => event.channel === "pi:error"));
    dropRequests = false; f.setQuestions([]); f.state(); assert.equal(h.questions.size, 0);
    assert(f.writes.every(command => command.type === "get_state")); off();
    return { actualCompiledAdapterToBridge: true, canonicalMetadataPreserved: true, rawMismatchRestoresCompleteHttpQuestions: true,
      getInducedStateCoalesced: true, subsequentSameIdsDoNotQuery: true, canonicalResolutionApplied: true, noTaskOrAnswerSubmitted: true };
  } finally { bridge.close(); f.stop(); }
}
(async () => {
  const receipt = { schema: "BioMindCompiledAdapterCompatibilityV1", checkedAt: new Date().toISOString(), result: "FAIL",
    mode: "production-module-with-injected-native-child", modelStarted: false, networkStarted: false, windowsAcceptance: "NOT_RUN" };
  try {
    const compiled = loadCompiled();
    receipt.buildId = fs.readFileSync(path.join(backend, ".next-pkg/BUILD_ID"), "utf8").trim();
    receipt.compiled = { chunk: compiled.chunk, sha256: compiled.sha256, moduleId: compiled.moduleId, gateModuleId: compiled.gateModuleId };
    receipt.bridge = { path: bridgePath, sha256: sha256(bridgePath) };
    receipt.test = { path: __filename, sha256: sha256(__filename) };
    receipt.source = { path: sourcePath, sha256: sha256(sourcePath), packageGate: { path: gatePath, sha256: sha256(gatePath) }, contract: await adapterContract(loadSource()) };
    receipt.compiled.contract = await adapterContract(compiled.Adapter);
    receipt.bridge.contract = await compiledBridgeContract(compiled.Adapter);
    for (const [key, value] of Object.entries(receipt.compiled.contract)) if (key !== "optionalSseDisplayPendingUI") assert.equal(receipt.source.contract[key], value);
    receipt.result = "PASS";
  } catch (error) { receipt.error = error.message; process.exitCode = 1; }
  fs.mkdirSync(path.dirname(outputPath), { recursive: true }); fs.writeFileSync(outputPath, JSON.stringify(receipt, null, 2) + "\n");
  process.stdout.write(JSON.stringify(receipt, null, 2) + "\n");
})();
