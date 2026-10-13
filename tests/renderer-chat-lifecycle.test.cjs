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
const compiled = require(path.join(desktop, "node_modules/esbuild")).buildSync({
  stdin: { contents: 'export { Chat } from "./src/renderer/src/components/Chat"; export { useStore } from "./src/renderer/src/store";', resolveDir: desktop, loader: "ts" },
  bundle: true, write: false, format: "cjs", platform: "node", target: "node22", jsx: "automatic",
  external: ["react", "react-dom", "react-dom/*"], loader: { ".png": "dataurl" },
}).outputFiles[0].text;
const fixtureModule = new Module(path.join(desktop, "chat-lifecycle-fixture.cjs"), module);
fixtureModule.filename = fixtureModule.id;
fixtureModule.paths = Module._nodeModulePaths(desktop);
fixtureModule._compile(compiled, fixtureModule.filename);
const { Chat, useStore } = fixtureModule.exports;
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
    activeProjectCwd: "/remote/A", extuiQueue: [], connectionStatus: "ready", toasts: [] });
});

async function renderChat(run) {
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
    await act(async () => reactRoot.render(React.createElement(Capture, null, React.createElement(Chat))));
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
