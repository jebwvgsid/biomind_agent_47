"use strict";
// Render the shipped Chat, Composer and store together. Only the IPC boundary
// and browser geometry APIs are fixtures; this does not execute a model task.
const { test, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");
const root = path.resolve(__dirname, "..");
const desktop = path.join(root, "desktop");
const { parseHTML } = require(path.join(root, "backend/pi-studio/node_modules/linkedom"));
const { window } = parseHTML("<html><head></head><body></body></html>");
global.window = window;
global.document = window.document;
Object.defineProperty(global, "navigator", { configurable: true, value: { userAgent: "offline-chat-lifecycle" } });
global.IS_REACT_ACT_ENVIRONMENT = true;
global.requestAnimationFrame = (fn) => setImmediate(fn);
global.cancelAnimationFrame = clearImmediate;
global.ResizeObserver = class { observe() {} disconnect() {} };
window.matchMedia = () => ({ matches: false });
const React = require(path.join(desktop, "node_modules/react"));
const { act } = React;
const { createRoot } = require(path.join(desktop, "node_modules/react-dom/client"));
const { Simulate } = require(path.join(desktop, "node_modules/react-dom/test-utils"));
const compiled = require(path.join(desktop, "node_modules/esbuild")).buildSync({
  stdin: { contents: 'export { Chat } from "./src/renderer/src/components/Chat"; export { BioMindConnectionStatus } from "./src/renderer/src/components/BioMindConnectionStatus"; export { useStore } from "./src/renderer/src/store";', resolveDir: desktop, loader: "ts" },
  bundle: true, write: false, format: "cjs", platform: "node", target: "node22", jsx: "automatic",
  external: ["react", "react-dom", "react-dom/*"], loader: { ".png": "dataurl" },
}).outputFiles[0].text;
const fixtureModule = new Module(path.join(desktop, "chat-lifecycle-fixture.cjs"), module);
fixtureModule.filename = fixtureModule.id;
fixtureModule.paths = Module._nodeModulePaths(desktop);
fixtureModule._compile(compiled, fixtureModule.filename);
const { Chat, BioMindConnectionStatus, useStore } = fixtureModule.exports;
const calls = { opened: [], prompts: [], closed: [], aborted: [], activated: [] };
const pending = [];
const snapshot = (id, cwd, extra = {}) => ({ threadId: id, sessionFile: id, cwd, sessionName: id, connected: true,
  messages: [{ role: "user", timestamp: 1, content: `history-${id}` }], model: { provider: "server", id: "model" }, ...extra });
beforeEach(() => {
  for (const value of Object.values(calls)) value.length = 0;
  pending.length = 0;
  window.pi = {
    app: { getProjects: async () => [] },
    biomind: { activateThread: async (id) => calls.activated.push(id) },
    thread: {
      open: (args) => { calls.opened.push(args); return new Promise((resolve, reject) => pending.push({ resolve, reject })); },
      getThinkingLevels: async () => ({ levels: ["off"] }),
      getBranchMessages: async () => ({ messages: [] }),
      prompt: async (args) => calls.prompts.push(args),
      close: async (id) => calls.closed.push(id),
      abort: async (id) => calls.aborted.push(id),
    },
  };
  useStore.setState({ config: { language: "zh" }, projects: [], threads: {}, openThreadIds: [], activeThreadId: null,
    activeProjectCwd: "/remote/A", extuiQueue: [], connectionStatus: "ready", connectionRevision: 0, toasts: [] });
});

async function renderChat(run, withConnectionStatus = false) {
  const container = document.createElement("div"); document.body.appendChild(container);
  const reactRoot = createRoot(container);
  const errors = [];
  class Capture extends React.Component {
    state = { error: null };
    static getDerivedStateFromError(error) { return { error }; }
    componentDidCatch(error) { errors.push(error.message); }
    render() { return this.state.error ? React.createElement("div", { "data-failed": true }, this.state.error.message) : this.props.children; }
  }
  const previousError = console.error;
  console.error = (...args) => { if (!String(args[0]).includes("Warning:") && !String(args[0]).startsWith("The above error")) previousError(...args); };
  try {
    await act(async () => reactRoot.render(React.createElement(Capture, null,
      withConnectionStatus && React.createElement(BioMindConnectionStatus), React.createElement(Chat))));
    await run(container, () => assert.deepEqual(errors, [], "Chat must keep a stable hook order across lifecycle changes"));
  } finally {
    await act(async () => reactRoot.unmount()); container.remove(); console.error = previousError;
  }
}

test("new remote thread: loading to native identity renders the real Chat and Composer", async () => {
  let temp;
  await act(async () => { temp = await useStore.getState().openThread("/remote/A"); });
  await renderChat(async (dom, noErrors) => {
    assert.ok(dom.querySelector(".chat-loading"));
    await act(async () => { pending[0].resolve(snapshot("native-A", "/remote/A")); await useStore.getState().ensureConnected(temp); });
    noErrors();
    assert.equal(useStore.getState().activeThreadId, "native-A");
    assert.match(dom.textContent, /history-native-A/);
    assert.ok(dom.querySelector("textarea"), "actual Composer mounted");
    assert.equal(calls.opened.length, 1);
    assert.deepEqual(calls.prompts, []);
  });
});

test("visible A to loading B and back preserves both projects, transcripts and A running state", async () => {
  await act(async () => {
    await useStore.getState().applyRemoteSnapshot(snapshot("native-A", "/remote/A", { isStreaming: true }));
    useStore.getState().setActiveThread("native-A");
  });
  await renderChat(async (dom, noErrors) => {
    assert.match(dom.textContent, /history-native-A/);
    let temp;
    await act(async () => { temp = await useStore.getState().openThread("/remote/B"); });
    noErrors();
    assert.ok(dom.querySelector(".chat-loading"));
    await act(async () => { pending[0].resolve(snapshot("native-B", "/remote/B")); await useStore.getState().ensureConnected(temp); });
    noErrors();
    for (const [id, cwd] of [["native-A", "/remote/A"], ["native-B", "/remote/B"], ["native-A", "/remote/A"]]) {
      await act(async () => useStore.getState().setActiveThread(id));
      noErrors();
      assert.match(dom.textContent, new RegExp(`history-${id}`));
      assert.equal(useStore.getState().activeProjectCwd, cwd);
    }
    assert.equal(useStore.getState().threads["native-A"].isStreaming, true);
    assert.equal(calls.opened.length, 1, "switching existing A/B must not open replacements");
    assert.deepEqual([calls.prompts, calls.closed, calls.aborted], [[], [], []]);
  });
});

test("failed initialization leaves the chat usable for inspection without resubmitting or replacing a session", async () => {
  let temp;
  await act(async () => { temp = await useStore.getState().openThread("/remote/A"); });
  await renderChat(async (dom, noErrors) => {
    const connected = useStore.getState().ensureConnected(temp);
    await act(async () => { pending[0].reject(new Error("SESSION_START_FAILED")); await connected; });
    noErrors();
    assert.match(dom.textContent, /SESSION_START_FAILED/);
    assert.equal(useStore.getState().activeThreadId, temp);
    assert.equal(useStore.getState().threads[temp].connected, false);
    assert.equal(calls.opened.length, 1);
    assert.deepEqual(calls.prompts, []);
  });
});

test("missing selection and restored snapshot can change in either direction in the same mounted Chat", async () => {
  await renderChat(async (dom, noErrors) => {
    assert.equal(dom.textContent, "");
    await act(async () => {
      await useStore.getState().applyRemoteSnapshot(snapshot("native-A", "/remote/A"));
      useStore.getState().setActiveThread("native-A");
    });
    noErrors();
    assert.match(dom.textContent, /history-native-A/);
    await act(async () => useStore.getState().setActiveProject("/remote/B"));
    noErrors();
    assert.equal(dom.textContent, "");
    await act(async () => useStore.getState().setActiveThread("native-A"));
    noErrors();
    assert.match(dom.textContent, /history-native-A/);
    assert.deepEqual([calls.opened, calls.prompts, calls.closed, calls.aborted], [[], [], [], []]);
  });
});

test("one failed old restore preserves its history while the real Composer can send once in a verified connected session", async () => {
  window.pi.app.resolveRuntime = async () => ({ ok: true });
  window.pi.app.getConfig = async () => ({ language: "zh" });
  window.pi.biomind.getStatus = async () => ({ status: "ready" });
  window.pi.biomind.restoreThreads = async () => ({
    schema: "biomind-restore-v1",
    snapshots: [snapshot("native-B", "/remote/B")],
    failures: [{ threadId: "old-A", cwd: "/remote/A", code: "SERVER_HTTP_404" }],
  });
  await act(async () => {
    await useStore.getState().applyRemoteSnapshot(snapshot("old-A", "/remote/A"));
    await useStore.getState().bootstrap();
    useStore.getState().setActiveThread("native-B");
  });
  assert.equal(useStore.getState().connectionStatus, "ready");
  assert.equal(useStore.getState().threads["old-A"].connected, false);
  assert.deepEqual(calls.prompts, [], "restore never submits or replays a prompt");
  await renderChat(async (dom, noErrors) => {
    const input = dom.querySelector("textarea");
    assert.ok(input);
    await act(async () => Simulate.change(input, { target: { value: "one explicit fixture request" } }));
    const send = dom.querySelector('button[title="发送"]');
    assert.ok(send && !send.disabled, "normal controlled input enables only the verified session");
    await act(async () => { Simulate.click(send); await new Promise(setImmediate); });
    noErrors();
    assert.equal(calls.prompts.length, 1);
    assert.equal(calls.prompts[0].threadId, "native-B");
    assert.equal(calls.prompts[0].text, "one explicit fixture request");
    await act(async () => useStore.getState().setActiveThread("old-A"));
    assert.match(dom.textContent, /history-old-A/, "failed restore retains the known transcript");
    await act(async () => Simulate.change(dom.querySelector("textarea"), { target: { value: "must not submit" } }));
    assert.equal(dom.querySelector('button[title="发送"]').disabled, true);
    await act(async () => Simulate.click(dom.querySelector('button[title="发送"]')));
    assert.equal(calls.prompts.length, 1, "failed owner stays blocked by the original Composer/store guards");
    assert.deepEqual([calls.opened, calls.closed, calls.aborted], [[], [], []]);
  });
});

test("partial restore cannot enable the Composer when the actual transport is disconnected", async () => {
  window.pi.app.resolveRuntime = async () => ({ ok: true });
  window.pi.app.getConfig = async () => ({ language: "zh" });
  window.pi.biomind.getStatus = async () => ({ status: "disconnected" });
  window.pi.biomind.restoreThreads = async () => ({
    schema: "biomind-restore-v1", snapshots: [snapshot("native-B", "/remote/B")],
    failures: [{ threadId: "old-A", cwd: "/remote/A", code: "SERVER_HTTP_404" }],
  });
  await act(async () => {
    await useStore.getState().bootstrap();
    useStore.getState().setActiveThread("native-B");
  });
  assert.equal(useStore.getState().connectionStatus, "disconnected");
  await renderChat(async (dom, noErrors) => {
    await act(async () => Simulate.change(dom.querySelector("textarea"), { target: { value: "must not submit while offline" } }));
    const send = dom.querySelector('button[title="发送"]');
    assert.equal(send.disabled, true);
    await act(async () => Simulate.click(send));
    noErrors();
    assert.deepEqual(calls.prompts, []);
  });
});

test("mounted reconnect status rechecks transport after partial restore before enabling the Composer", async () => {
  let statusListener;
  let statusCalls = 0;
  window.pi.biomind.getStatus = async () => ({ status: ++statusCalls === 1 ? "ready" : "disconnected" });
  window.pi.biomind.onStatus = (listener) => { statusListener = listener; return () => {}; };
  window.pi.biomind.restoreThreads = async () => ({
    schema: "biomind-restore-v1", snapshots: [snapshot("native-B", "/remote/B")],
    failures: [{ threadId: "old-A", cwd: "/remote/A", code: "SERVER_HTTP_404" }],
  });
  await act(async () => {
    await useStore.getState().applyRemoteSnapshot(snapshot("native-B", "/remote/B"));
    useStore.getState().setActiveThread("native-B");
  });
  await renderChat(async (dom, noErrors) => {
    await act(async () => { statusListener({ status: "ready" }); await new Promise(setImmediate); });
    assert.equal(statusCalls, 2, "ready event must be followed by an actual status check after restore");
    assert.equal(useStore.getState().connectionStatus, "disconnected");
    assert.match(dom.querySelector('[role="status"]').textContent, /已断开/);
    await act(async () => Simulate.change(dom.querySelector("textarea"), { target: { value: "still offline" } }));
    assert.equal(dom.querySelector('button[title="发送"]').disabled, true);
    noErrors();
    assert.deepEqual(calls.prompts, []);
  }, true);
});
