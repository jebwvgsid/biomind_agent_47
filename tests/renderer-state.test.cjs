"use strict";
const { test, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const root = path.resolve(__dirname, "..");
const legacy = path.resolve(root, "../BioMind_Studio_S2_S3_Implementation_20261008/candidate/pi-studio/node_modules");
const ts = require(path.join(legacy, "typescript"));
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  try { return originalResolve.call(this, request, parent, ...rest); }
  catch (error) {
    if (!request.startsWith(".") && !request.startsWith("/") && !request.startsWith("node:")) return require.resolve(request, { paths: [legacy] });
    throw error;
  }
};
for (const extension of [".ts", ".tsx"]) {
  require.extensions[extension] = (module, file) => {
    const compiled = ts.transpileModule(fs.readFileSync(file, "utf8"), { fileName: file, reportDiagnostics: true,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } });
    const errors = (compiled.diagnostics || []).filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
    assert.deepEqual(errors.map((error) => ts.flattenDiagnosticMessageText(error.messageText, "\n")), [], file);
    module._compile(compiled.outputText, file);
  };
}
global.requestAnimationFrame = (callback) => setImmediate(callback);
const calls = { opened: [], closed: [], prompts: [], activated: [], uploads: [] };
const snapshot = (id, cwd = "/remote/Project", extra = {}) => ({ threadId: id, sessionFile: id, cwd, sessionName: id, connected: true, messages: [], model: { provider: "server", id: "model" }, ...extra });
const restoration = (snapshots = [], failures = []) => ({ schema: "biomind-restore-v1", snapshots, failures });
global.window = { pi: {
  app: { getProjects: async () => [], stageClipboardFile: async (value) => { calls.uploads.push(value); return { abs: "/remote/Project/uploads/input.txt", name: value.name }; } },
  biomind: { restoreThreads: async () => restoration(), activateThread: async (id) => { assert.ok(useStore.getState().threads[id], "snapshot must exist before event activation"); calls.activated.push(id); }, finishInput: async () => {} },
  thread: {
    open: async (args) => { calls.opened.push(args); await new Promise(setImmediate); return snapshot(args.sessionFile || `session-${calls.opened.length}`, args.cwd); },
    close: async (id) => calls.closed.push(id),
    getThinkingLevels: async () => ({ levels: ["off"] }),
    getBranchMessages: async () => ({ messages: [] }),
    loadHistory: async (args) => snapshot(args.sessionFile, args.cwd),
    prompt: async (args) => { calls.prompts.push(args); return { ok: true, receipt: { clientMessageId: "accepted" } }; },
  },
} };
const { useStore, normalizeThreadFile } = require(path.join(root, "desktop/src/renderer/src/store.ts"));
const { fileToAttachment } = require(path.join(root, "desktop/src/renderer/src/components/Composer.tsx"));
beforeEach(() => {
  for (const list of Object.values(calls)) list.length = 0;
  useStore.setState({ config: { language: "zh" }, threads: {}, projects: [], openThreadIds: [], activeThreadId: null, activeProjectCwd: "/remote/Project", connectionStatus: "ready", toasts: [] });
});

test("Linux paths remain case-sensitive and preserve literal backslashes", () => {
  assert.notEqual(normalizeThreadFile("/remote/Project/A"), normalizeThreadFile("/remote/project/a"));
  assert.equal(normalizeThreadFile("/remote/A\\B"), "/remote/A\\B");
  assert.equal(normalizeThreadFile("/"), "/");
});

test("new A and B keep server identities; closing and reopening a view does not terminate or replace A", async () => {
  const a = await useStore.getState().openThread("/remote/Project");
  const realA = await useStore.getState().ensureConnected(a);
  assert.equal(calls.opened[0].clientOpenId, a);
  useStore.setState((state) => ({ threads: { ...state.threads, [realA]: { ...state.threads[realA], isStreaming: true } } }));
  const b = await useStore.getState().openThread("/remote/project");
  const realB = await useStore.getState().ensureConnected(b);
  assert.notEqual(realA, realB);
  useStore.getState().setActiveThread(realA);
  useStore.getState().setActiveThread(realB);
  await useStore.getState().closeThread(realA);
  assert.equal(useStore.getState().threads[realA].isStreaming, true);
  assert.deepEqual(calls.closed, []);
  assert.equal(await useStore.getState().openThread("/remote/Project", realA), realA);
  assert.equal(calls.opened.length, 2);
  assert.ok(useStore.getState().openThreadIds.includes(realA));
});

test("late history response cannot steal focus from a newer session selection", async () => {
  let release;
  window.pi.thread.loadHistory = ({ sessionFile, cwd }) => sessionFile === "slow" ? new Promise((resolve) => { release = () => resolve(snapshot("slow", cwd)); }) : Promise.resolve(snapshot(sessionFile, cwd));
  const slow = useStore.getState().openThread("/remote/Project", "slow");
  await useStore.getState().openThread("/remote/project", "fast");
  release(); await slow;
  assert.equal(useStore.getState().activeThreadId, "fast");
  assert.equal(useStore.getState().activeProjectCwd, "/remote/project");
  assert.ok(useStore.getState().threads.slow);
});

test("uncertain prompt leaves execution status unknown; snapshot recovery does not replay it", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  window.pi.thread.prompt = async (args) => { calls.prompts.push(args); throw new Error("INPUT_OUTCOME_UNKNOWN"); };
  const sent = await useStore.getState().sendPrompt("A", "write actual result");
  assert.equal(sent.accepted, false);
  assert.equal(useStore.getState().threads.A.connected, false);
  assert.equal(useStore.getState().threads.A.isStreaming, true, "transport error must not claim task settled");
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true, messages: [{ role: "user", content: "write actual result" }] }));
  assert.equal(calls.prompts.length, 1);
  assert.equal(useStore.getState().threads.A.connected, true);
  assert.equal(useStore.getState().threads.A.messages.length, 1);
});

test("disconnected window refuses prompt without a backend POST", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  useStore.setState({ connectionStatus: "reconnecting" });
  const result = await useStore.getState().sendPrompt("A", "do not send yet");
  assert.equal(result.accepted, false);
  assert.equal(calls.prompts.length, 0);
});

test("Windows attachment path is ignored; bytes upload to selected Linux project", async () => {
  window.pi.app.getPathForFile = () => { throw new Error("must not pass Windows paths to Linux"); };
  const input = new File([Buffer.from("actual attachment bytes")], "input.txt", { type: "text/plain" });
  Object.defineProperty(input, "path", { value: "C:\\Users\\test\\input.txt" });
  const result = await fileToAttachment(input);
  assert.equal(result.file.abs, "/remote/Project/uploads/input.txt");
  assert.equal(calls.uploads[0].cwd, "/remote/Project");
  assert.equal(Buffer.from(calls.uploads[0].data, "base64").toString(), "actual attachment bytes");
  assert.equal(JSON.stringify(calls.uploads).includes("C:"), false);
});


test("snapshot projects full live messages and tool progress once before newer SSE activates", async () => {
  const user = { role: "user", timestamp: 100, content: "run" };
  const finished = { role: "assistant", timestamp: 101, content: [{ type: "text", text: "starting" }] };
  const partial = { role: "assistant", timestamp: 102, content: [{ type: "text", text: "working" }, { type: "toolCall", id: "tool-1", name: "bash", arguments: { command: "ls" } }] };
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", {
    isStreaming: true, messages: [user, finished], liveEvents: [
      { type: "message_start", message: user },
      { type: "message_start", message: { ...finished, content: [] } },
      { type: "message_end", message: finished },
      { type: "message_start", message: { ...partial, content: [] } },
      { type: "message_update", message: partial },
      { type: "tool_execution_start", toolCallId: "tool-1", toolName: "bash", args: { command: "ls" } },
    ],
  }));
  const thread = useStore.getState().threads.A;
  assert.equal(thread.messages.filter((message) => message.role === "user").length, 1);
  assert.equal(thread.messages.filter((message) => message.role === "assistant").length, 1);
  assert.equal(thread.streaming.blocks.find((block) => block.type === "text").text, "working");
  assert.equal(thread.toolRuns["tool-1"].running, true);
  assert.deepEqual(calls.activated, ["A"]);
});

test("resolved extension question closes only its matching thread dialog", async () => {
  useStore.setState({ extuiQueue: [{ threadId: "A", request: { id: "approve", method: "confirm" } }, { threadId: "B", request: { id: "approve", method: "confirm" } }] });
  useStore.getState().handleEvent("A", { type: "extension_ui_resolved", id: "approve" });
  assert.deepEqual(useStore.getState().extuiQueue.map((item) => item.threadId), ["B"]);
});

test("queued input with unknown outcome survives recovery but never auto-retries", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  useStore.getState().setPendingFollowUp("A", { text: "next task", images: [], files: [] });
  window.pi.thread.prompt = async (args) => { calls.prompts.push(args); throw new Error("INPUT_OUTCOME_UNKNOWN"); };
  await useStore.getState().sendPendingSteering("A", "followUp");
  assert.ok(useStore.getState().threads.A.pendingFollowUp);
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  assert.ok(useStore.getState().threads.A.pendingFollowUp);
  await useStore.getState().sendPendingSteering("A", "followUp");
  assert.equal(calls.prompts.length, 1);
});

test("pending questions deduplicate per session and do not steal active thread", async () => {
  useStore.setState({ activeThreadId: "B", extuiQueue: [] });
  const request = { id: "question", method: "confirm", title: "Allow?" };
  useStore.getState().handleExtUi("A", request);
  useStore.getState().handleExtUi("A", { ...request, title: "Updated" });
  useStore.getState().handleExtUi("B", request);
  assert.equal(useStore.getState().extuiQueue.length, 2);
  assert.equal(useStore.getState().extuiQueue[0].request.title, "Updated");
  assert.equal(useStore.getState().activeThreadId, "B");
});

test("authorization reply waits for receipt, retains failure, and removes only matching session", async () => {
  useStore.setState({ extuiQueue: ["A", "B"].map((threadId) => ({ threadId, request: { id: "question", method: "confirm" } })) });
  window.pi.thread.extuiResponse = async () => { throw new Error("RESPONSE_UNKNOWN"); };
  await useStore.getState().respondExtUi("A", "question", { confirmed: true });
  assert.equal(useStore.getState().extuiQueue.length, 2);
  let release;
  window.pi.thread.extuiResponse = () => new Promise((resolve) => { release = resolve; });
  const reply = useStore.getState().respondExtUi("A", "question", { confirmed: true });
  assert.equal(useStore.getState().extuiQueue.length, 2);
  release(true); await reply;
  assert.deepEqual(useStore.getState().extuiQueue.map((item) => item.threadId), ["B"]);
});

test("snapshot clears stale authorization before replaying authoritative pending questions", async () => {
  useStore.setState({ extuiQueue: ["A", "B"].map((threadId) => ({ threadId, request: { id: "stale", method: "confirm" } })) });
  const prior = window.pi.biomind.activateThread;
  window.pi.biomind.activateThread = async (id) => {
    assert.equal(useStore.getState().extuiQueue.some((item) => item.threadId === id), false);
    useStore.getState().handleExtUi(id, { id: "current", method: "input" });
  };
  try { await useStore.getState().applyRemoteSnapshot(snapshot("A")); } finally { window.pi.biomind.activateThread = prior; }
  assert.deepEqual(useStore.getState().extuiQueue.map((item) => `${item.threadId}:${item.request.id}`), ["B:stale", "A:current"]);
});

test("restored window activates only saved existing session and restores original project", async () => {
  const prior = window.pi.biomind.restoreThreads;
  useStore.setState({ config: { language: "zh", lastThreadId: "B" }, activeThreadId: null });
  window.pi.biomind.restoreThreads = async () => restoration([snapshot("A", "/remote/Project"), snapshot("B", "/remote/project")]);
  try { await useStore.getState().restoreRemoteThreads(); } finally { window.pi.biomind.restoreThreads = prior; }
  assert.deepEqual(useStore.getState().openThreadIds, ["A", "B"]);
  assert.equal(useStore.getState().activeThreadId, "B");
  assert.equal(useStore.getState().activeProjectCwd, "/remote/project");
  assert.equal(calls.opened.length, 0);
});

test("connection error retains last execution state and permissions remain server managed", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true }));
  const permission = useStore.getState().threads.A.permission;
  useStore.getState().handleError("A", "SSE_EOF");
  assert.equal(useStore.getState().threads.A.connected, false);
  assert.equal(useStore.getState().threads.A.isStreaming, true);
  await useStore.getState().setPermission("A", "full");
  assert.equal(useStore.getState().threads.A.permission, permission);
});

test("server skills load even when extension package management is unsupported", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  useStore.setState({ activeThreadId: "A" });
  window.pi.plugins = { getPackages: async () => { throw new Error("unsupported"); }, getSkills: async ({ threadId, cwd }) => {
    assert.equal(threadId, "A");
    return [{ name: "server-skill", path: `${cwd}/SKILL.md`, enabled: true, threadId, cwd, resourceId: "A-skill" }];
  } };
  await useStore.getState().loadPlugins();
  assert.equal(useStore.getState().skills[0].path, "/remote/Project/SKILL.md");
  assert.equal(useStore.getState().pluginsLoading, false);
});

test("Skills load and toggle stay bound to active native B while busy A exists", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true }));
  await useStore.getState().applyRemoteSnapshot(snapshot("B"));
  useStore.setState({ activeThreadId: "B" });
  const called = [], row = { resourceId: "B-skill", name: "skill", path: "/shared/SKILL.md", root: "native", enabled: true, threadId: "B", cwd: "/remote/Project" };
  window.pi.plugins = {
    getSkills: async (args) => { called.push({ type: "get", ...args }); return [row]; },
    setSkillEnabled: async (args) => { called.push({ type: "set", ...args }); return { skills: [{ ...row, enabled: false }], effect: "reload_required", applied: false }; },
  };
  await useStore.getState().loadPlugins();
  await useStore.getState().toggleSkill(row.path, false);
  assert.deepEqual(called.map(call => call.threadId), ["B", "B"]);
  assert.equal(called[1].resourceId, "B-skill");
  assert.equal(useStore.getState().skills[0].enabled, false);
  assert.equal(useStore.getState().threads.A.isStreaming, true);
});

test("late A Skills cannot replace B; stale A toggle never targets B", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  await useStore.getState().applyRemoteSnapshot(snapshot("B"));
  useStore.setState({ activeThreadId: "A" });
  let release, sets = 0;
  const row = id => ({ resourceId: `${id}-skill`, name: id, path: "/shared/SKILL.md", root: "native", enabled: true, threadId: id, cwd: "/remote/Project" });
  window.pi.plugins = {
    getSkills: async ({ threadId }) => threadId === "A" ? new Promise(resolve => { release = () => resolve([row("A")]); }) : [row("B")],
    setSkillEnabled: async () => { sets++; throw new Error("unexpected"); },
  };
  const pending = useStore.getState().loadPlugins();
  useStore.setState({ activeThreadId: "B" });
  await useStore.getState().loadPlugins();
  release(); await pending;
  assert.equal(useStore.getState().skills[0].threadId, "B");
  useStore.setState({ skills: [row("A")] });
  await useStore.getState().toggleSkill(row("A").path, false);
  assert.equal(sets, 0);
  assert.equal(useStore.getState().skills[0].threadId, "B");
});

test("no session and read-only history cannot load or mutate another owner's Skills", async () => {
  let calls = 0;
  window.pi.plugins = { getSkills: async () => { calls++; return []; }, setSkillEnabled: async () => { calls++; } };
  await useStore.getState().loadPlugins();
  await useStore.getState().applyRemoteSnapshot(snapshot("history", "/remote/Project", { connected: false }));
  useStore.setState({ activeThreadId: "history" });
  await useStore.getState().loadPlugins();
  await useStore.getState().toggleSkill("/shared/SKILL.md", false);
  assert.equal(calls, 0);
  assert.deepEqual(useStore.getState().skills, []);
  assert.equal(useStore.getState().pluginsLoading, false);
});

test("Linux artifact identity preserves case, backslashes, and one-letter root directories", () => {
  const { resolveArtifactPath, collectFileArtifacts } = require(path.join(root, "desktop/src/renderer/src/lib/artifacts.ts"));
  assert.equal(resolveArtifactPath("/p/Result.csv", "/remote/Project"), "/p/Result.csv");
  assert.equal(resolveArtifactPath("A\\B.csv", "/remote/Project"), "/remote/Project/A\\B.csv");
  const messages = [{ blocks: ["A", "a"].map((name) => ({ type: "toolCall", id: name, name: "write", arguments: { path: `${name}.csv` } })) }];
  const tools = Object.fromEntries(["A", "a"].map((name) => [name, { name: "write", completed: true, running: false, args: { path: `${name}.csv` } }]));
  assert.deepEqual(collectFileArtifacts(messages, tools, "/remote/Project").map((file) => file.path), ["/remote/Project/A.csv", "/remote/Project/a.csv"]);
});

test("failed project refresh rejects restoration instead of reporting ready", async () => {
  const previous = window.pi.app.getProjects;
  window.pi.app.getProjects = async () => { throw new Error("PROJECTS_OFFLINE"); };
  try { await assert.rejects(useStore.getState().restoreRemoteThreads(), /PROJECTS_OFFLINE/); }
  finally { window.pi.app.getProjects = previous; }
});

test("bootstrap finishing after a disconnect cannot overwrite it with ready", async () => {
  const previous = window.pi.biomind.restoreThreads;
  window.pi.app.resolveRuntime = async () => ({ ok: true });
  window.pi.app.getConfig = async () => ({ language: "zh" });
  window.pi.biomind.getStatus = async () => ({ status: "ready" });
  window.pi.biomind.restoreThreads = async () => {
    useStore.setState((state) => ({ connectionStatus: "disconnected", connectionRevision: state.connectionRevision + 1 }));
    return restoration();
  };
  try { await useStore.getState().bootstrap(); } finally { window.pi.biomind.restoreThreads = previous; }
  assert.equal(useStore.getState().connectionStatus, "disconnected");
});

test("partial restore preserves failed history and allows only a verified new session to send manually", async () => {
  const previous = { restore: window.pi.biomind.restoreThreads, config: window.pi.app.getConfig, runtime: window.pi.app.resolveRuntime,
    status: window.pi.biomind.getStatus, prompt: window.pi.thread.prompt };
  await useStore.getState().applyRemoteSnapshot(snapshot("old-A", "/remote/Project", { messages: [{ role: "user", content: "existing history" }], isStreaming: true }));
  const oldMessages = useStore.getState().threads["old-A"].messages;
  useStore.setState({ connectionStatus: "restoring" });
  window.pi.app.resolveRuntime = async () => ({ ok: true });
  window.pi.app.getConfig = async () => ({ language: "zh", lastThreadId: "old-A" });
  window.pi.biomind.getStatus = async () => ({ status: "ready" });
  window.pi.biomind.restoreThreads = async () => ({ schema: "biomind-restore-v1", snapshots: [snapshot("B")], failures: [{ threadId: "old-A", cwd: "/remote/Project", code: "SERVER_HTTP_404" }] });
  window.pi.thread.prompt = async args => { calls.prompts.push(args); return { ok: true, receipt: { clientMessageId: "accepted-manual" } }; };
  try {
    await useStore.getState().bootstrap();
    assert.equal(useStore.getState().connectionStatus, "ready");
    assert.equal(useStore.getState().threads["old-A"].connected, false);
    assert.equal(useStore.getState().threads["old-A"].messages, oldMessages);
    assert.equal(useStore.getState().threads["old-A"].isStreaming, true, "failed restoration does not claim a running task ended");
    assert.match(useStore.getState().threads["old-A"].error, /SERVER_HTTP_404/);
    assert.equal(useStore.getState().threads.B.connected, true);
    assert.ok(useStore.getState().openThreadIds.includes("old-A"));
    assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.opened, []);
    assert.equal((await useStore.getState().sendPrompt("old-A", "must not send")).accepted, false);
    const temp = await useStore.getState().openThread("/remote/New");
    const newId = await useStore.getState().ensureConnected(temp);
    assert.equal(useStore.getState().threads[newId].connected, true);
    assert.equal((await useStore.getState().sendPrompt(newId, "one explicit request")).accepted, true);
    assert.equal(calls.prompts.length, 1); assert.equal(calls.prompts[0].threadId, newId);
    assert.equal(calls.opened.length, 1); assert.equal(calls.closed.length, 0);
  } finally {
    window.pi.biomind.restoreThreads = previous.restore; window.pi.app.getConfig = previous.config;
    window.pi.app.resolveRuntime = previous.runtime; window.pi.biomind.getStatus = previous.status; window.pi.thread.prompt = previous.prompt;
  }
});

test("malformed partial restore or changed scope commits no snapshot and cannot report ready", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  const previousThreads = useStore.getState().threads;
  const previous = window.pi.biomind.restoreThreads;
  const partial = failures => ({ schema: "biomind-restore-v1", snapshots: [snapshot("B")], failures });
  const malformed = [
    { schema: "other", snapshots: [], failures: [] },
    partial({ threadId: "A" }),
    partial([{ threadId: "A", cwd: "/remote/Project", code: {} }]),
    partial([{ threadId: "B", cwd: "/remote/Project", code: "SERVER_HTTP_404" }]),
    partial([{ threadId: "A", cwd: "/different", code: "SERVER_HTTP_404" }]),
    { schema: "biomind-restore-v1", snapshots: [snapshot("B", "/remote/Project", { connected: "true" })], failures: [] },
    { schema: "biomind-restore-v1", snapshots: [snapshot("B", "/remote/Project", { sessionFile: "different" })], failures: [] },
  ];
  try {
    for (const value of malformed) {
      window.pi.biomind.restoreThreads = async () => value;
      await assert.rejects(useStore.getState().restoreRemoteThreads(), /SESSION_RESTORE_FAILED/);
      assert.equal(useStore.getState().threads, previousThreads);
    }
    window.pi.app.resolveRuntime = async () => ({ ok: true }); window.pi.app.getConfig = async () => ({ language: "zh" });
    window.pi.biomind.getStatus = async () => ({ status: "ready" });
    await useStore.getState().bootstrap();
    assert.equal(useStore.getState().connectionStatus, "error");
    assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.opened, []);
  } finally { window.pi.biomind.restoreThreads = previous; }
});

test("obsolete array restoration is rejected without any store commit, activation, or prompt", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A"));
  const previousState = useStore.getState();
  const previousRestore = window.pi.biomind.restoreThreads;
  const previousActivated = [...calls.activated];
  try {
    for (const obsolete of [[], [snapshot("B")], [snapshot("../invalid", "/remote/Project", { connected: "true" })]]) {
      window.pi.biomind.restoreThreads = async () => obsolete;
      await assert.rejects(useStore.getState().restoreRemoteThreads(), /SESSION_RESTORE_FAILED/);
      assert.equal(useStore.getState(), previousState);
      assert.deepEqual(calls.activated, previousActivated);
      assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.opened, []); assert.deepEqual(calls.closed, []);
    }
  } finally { window.pi.biomind.restoreThreads = previousRestore; }
});

test("partial restore does not turn an actual disconnected transport into ready", async () => {
  const previous = window.pi.biomind.restoreThreads;
  window.pi.app.resolveRuntime = async () => ({ ok: true }); window.pi.app.getConfig = async () => ({ language: "zh" });
  window.pi.biomind.getStatus = async () => ({ status: "disconnected" });
  window.pi.biomind.restoreThreads = async () => ({ schema: "biomind-restore-v1", snapshots: [snapshot("B")], failures: [{ threadId: "A", cwd: "/remote/Project", code: "SERVER_HTTP_404" }] });
  try {
    await useStore.getState().bootstrap();
    assert.equal(useStore.getState().connectionStatus, "disconnected");
    assert.equal((await useStore.getState().sendPrompt("B", "must not send")).accepted, false);
    assert.deepEqual(calls.prompts, []);
  } finally { window.pi.biomind.restoreThreads = previous; }
});

test("server execution failure remains visible after settlement and null scientific success is not claimed", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("result-A", "/remote/Project", { isStreaming: true }));
  useStore.getState().handleEvent("result-A", { type: "biomind_result", runtime_status: "error", scientific_success: null });
  useStore.getState().handleEvent("result-A", { type: "agent_settled" });
  await new Promise(setImmediate);
  assert.equal(useStore.getState().threads["result-A"].isStreaming, false);
  assert.match(useStore.getState().threads["result-A"].error, /服务器报告本次执行失败/);
  await useStore.getState().applyRemoteSnapshot(snapshot("result-B", "/remote/Project", { isStreaming: true }));
  useStore.getState().handleEvent("result-B", { type: "biomind_result", runtime_status: "completed", scientific_success: null });
  useStore.getState().handleEvent("result-B", { type: "agent_settled" });
  await new Promise(setImmediate);
  assert.equal(useStore.getState().threads["result-B"].error, undefined);
  useStore.getState().handleEvent("result-B", { type: "biomind_result", runtime_status: "completed", scientific_success: false });
  await new Promise(setImmediate);
  assert.match(useStore.getState().threads["result-B"].error, /科学目标未达成/);
  assert.equal(calls.prompts.length, 0);
});

test("malformed project refresh leaves native A/B and the existing project list intact", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true }));
  await useStore.getState().applyRemoteSnapshot(snapshot("B"));
  useStore.setState({ activeThreadId: "A", projects: [{ cwd: "/remote/Project", name: "Project", threads: [] }] });
  const previousProjects = useStore.getState().projects;
  const previousThreads = useStore.getState().threads;
  const previousGet = window.pi.app.getProjects;
  window.pi.app.getProjects = async () => [{ cwd: "/remote/Project", name: { bad: true }, threads: [] }];
  try { await useStore.getState().refreshProjects(); } finally { window.pi.app.getProjects = previousGet; }
  assert.equal(useStore.getState().projects, previousProjects);
  assert.equal(useStore.getState().threads, previousThreads);
  assert.equal(useStore.getState().activeThreadId, "A");
  assert.equal(useStore.getState().threads.A.isStreaming, true);
  assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.closed, []); assert.deepEqual(calls.opened, []);
});

test("invalid snapshot cannot poison an existing native session or activate another owner", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true }));
  const previous = useStore.getState().threads.A;
  await assert.rejects(useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { messages: {} })), /SESSION_RESTORE_FAILED/);
  assert.equal(useStore.getState().threads.A, previous);
  assert.deepEqual(calls.activated, ["A"]);
  assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.opened, []);
});

test("bootstrap malformed metadata ends in a visible error and never creates or replays a task", async () => {
  const previous = { getProjects: window.pi.app.getProjects, getConfig: window.pi.app.getConfig, restoreThreads: window.pi.biomind.restoreThreads };
  window.pi.app.resolveRuntime = async () => ({ ok: true });
  window.pi.app.getConfig = async () => ({ pinnedProjects: { bad: true } });
  window.pi.app.getProjects = async () => [{ cwd: "/remote/Project", name: "Project", threads: null }];
  window.pi.biomind.restoreThreads = async () => restoration();
  window.pi.biomind.getStatus = async () => ({ status: "ready" });
  try { await assert.doesNotReject(useStore.getState().bootstrap()); }
  finally { window.pi.app.getProjects = previous.getProjects; window.pi.app.getConfig = previous.getConfig; window.pi.biomind.restoreThreads = previous.restoreThreads; }
  assert.equal(useStore.getState().connectionStatus, "error");
  assert.deepEqual(useStore.getState().projects, []);
  assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.closed, []); assert.deepEqual(calls.opened, []);
});

test("focus EPERM retains A/B native identity, active project, history, and running state", async () => {
  await useStore.getState().applyRemoteSnapshot(snapshot("A", "/remote/Project", { isStreaming: true, messages: [{ role: "user", content: "original accepted task" }] }));
  await useStore.getState().applyRemoteSnapshot(snapshot("B", "/remote/Other"));
  useStore.setState({ activeThreadId: "A", activeProjectCwd: "/remote/Project", openThreadIds: ["A", "B"] });
  const threads = useStore.getState().threads;
  const previous = window.pi.biomind.focusThread;
  window.pi.biomind.focusThread = async () => { throw Object.assign(new Error("EPERM synthetic file rename"), { code: "EPERM" }); };
  const { persistWindowThread } = require(path.join(root, "desktop/src/renderer/src/lib/rendererSafety.ts"));
  try { await persistWindowThread("A"); } finally { window.pi.biomind.focusThread = previous; }
  assert.equal(useStore.getState().threads, threads);
  assert.equal(useStore.getState().activeThreadId, "A");
  assert.equal(useStore.getState().activeProjectCwd, "/remote/Project");
  assert.equal(useStore.getState().threads.A.isStreaming, true);
  assert.deepEqual(useStore.getState().openThreadIds, ["A", "B"]);
  assert.deepEqual(calls.prompts, []); assert.deepEqual(calls.closed, []); assert.deepEqual(calls.opened, []);
});
