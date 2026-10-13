"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const desktop = path.join(root, "desktop/node_modules");
const backend = path.join(root, "backend/pi-studio/node_modules");
const ts = require(path.join(backend, "typescript"));
for (const extension of [".ts", ".tsx"]) {
  require.extensions[extension] = (module, file) => {
    module._compile(ts.transpileModule(fs.readFileSync(file, "utf8"), { fileName: file,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText, file);
  };
}
const { parseHTML } = require(path.join(backend, "linkedom"));
const { window } = parseHTML("<html><head></head><body><div id='root'></div></body></html>");
global.window = window;
global.document = window.document;
Object.defineProperty(global, "navigator", { configurable: true, value: { userAgent: "offline-renderer-test" } });
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require(path.join(desktop, "react"));
const { act } = React;
const { createRoot } = require(path.join(desktop, "react-dom/client"));
const { WorkbenchErrorBoundary } = require(path.join(root, "desktop/src/renderer/src/components/WorkbenchErrorBoundary.tsx"));
const { BioMindUIStatus } = require(path.join(root, "desktop/src/renderer/src/components/BioMindUIStatus.tsx"));
const safety = require(path.join(root, "desktop/src/renderer/src/lib/rendererSafety.ts"));
const data = require(path.join(root, "desktop/src/renderer/src/lib/remoteData.ts"));
const reports = [];
const actions = [];
window.pi = { biomind: {
  reportRendererIssue: async (issue) => { reports.push(issue); },
  showConnection: async () => { actions.push("connection"); },
  getLocalUIState: async () => ({ status: "saved" }),
  onLocalUIState: () => () => {},
} };
window.location = { reload: () => actions.push("reload") };

test("actual React child render failure leaves visible recovery and reload never submits/opens/closes a task", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const reactRoot = createRoot(container);
  const expectedError = console.error; console.error = () => {};
  const Boom = () => { throw new TypeError("SYNTHETIC_PRIVATE_PROMPT workbenchPassword=fixture"); };
  try {
    await act(async () => { reactRoot.render(React.createElement(WorkbenchErrorBoundary, null, React.createElement("div", null, "original workbench"))); });
    assert.match(container.textContent, /original workbench/);
    await act(async () => { reactRoot.render(React.createElement(WorkbenchErrorBoundary, null, React.createElement(Boom))); });
    assert.match(container.textContent, /界面暂时无法显示/);
    assert.match(container.textContent, /不会自动重发任务/);
    assert.match(container.textContent, /RENDER_FAILED/);
    assert.equal(container.textContent.includes("SYNTHETIC_PRIVATE"), false);
    const before = actions.length;
    await act(async () => { container.querySelector("button").click(); });
    assert.deepEqual(actions.slice(before), ["reload"]);
    const report = reports.find((issue) => issue.code === "RENDER_FAILED");
    assert.equal(report.name, "TypeError");
    assert.equal(JSON.stringify(report).includes("SYNTHETIC_PRIVATE"), false);
    assert.equal(JSON.stringify(report).includes("workbenchPassword"), false);
    assert.equal(JSON.stringify(report).includes(root), false);
  } finally { await act(async () => reactRoot.unmount()); container.remove(); console.error = expectedError; }
});

test("diagnostics retain only bounded program locations even for hostile thrown values", () => {
  const error = { name: "private-name", message: "private-prompt", stack: "Error: SYNTHETIC_PRIVATE_LABEL.js:7:8 index-SECRET00.js:9:10\n    at x (file:///C:/Users/private/workbench/index-ABC12345.js:12:23)\n    at privateFn (/secret/path/SYNTHETIC_PRIVATE_LABEL.js:88:3)" };
  assert.deepEqual(safety.safeRendererIssue("render", "RENDER_FAILED", error), {
    source: "render", code: "RENDER_FAILED", name: "Error", stack: "at index-ABC12345.js:12:23",
  });
  assert.equal(safety.safeRendererIssue("window_error", "WINDOW_ERROR", "private-rejection").stack, "");
  const hostile = Object.defineProperty({}, "name", { get() { throw new Error("getter"); } });
  assert.doesNotThrow(() => safety.safeRendererIssue("render", "RENDER_FAILED", hostile));
});

test("focus persistence rejection and synchronous failure resolve safely with no task operation", async () => {
  const focused = [];
  window.pi.biomind.focusThread = async (id) => { focused.push(id); throw Object.assign(new Error("EPERM: rename private-user-path"), { code: "EPERM" }); };
  const before = actions.length;
  await assert.doesNotReject(safety.persistWindowThread("native-A"));
  window.pi.biomind.focusThread = () => { throw new TypeError("sync bridge failure"); };
  await assert.doesNotReject(safety.persistWindowThread("native-B"));
  assert.deepEqual(focused, ["native-A"]);
  assert.equal(actions.length, before);
  assert.equal(JSON.stringify(reports).includes("private-user-path"), false);
});

test("local unsaved banner renders alongside child workbench and clears on saved event", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const reactRoot = createRoot(container);
  let receive; let unsubscribed = false; let releaseInitial;
  window.pi.biomind.getLocalUIState = () => new Promise(resolve => { releaseInitial = resolve; });
  window.pi.biomind.onLocalUIState = (callback) => { receive = callback; return () => { unsubscribed = true; }; };
  try {
    await act(async () => reactRoot.render(React.createElement("div", null, React.createElement(BioMindUIStatus), React.createElement("section", null, "A and B workbench"))));
    await act(async () => receive({ status: "unsaved", code: "LOCAL_UI_WRITE_FAILED" }));
    assert.match(container.textContent, /本地窗口状态暂未保存/);
    assert.match(container.textContent, /A and B workbench/);
    await act(async () => releaseInitial({ status: "saved" }));
    assert.match(container.textContent, /本地窗口状态暂未保存/, "late initial getter must not erase a newer failure");
    await act(async () => receive({ status: "saved" }));
    assert.equal(container.textContent.includes("本地窗口状态暂未保存"), false);
    assert.match(container.textContent, /A and B workbench/);
  } finally { await act(async () => reactRoot.unmount()); container.remove(); }
  assert.equal(unsubscribed, true);
});

test("project and directory metadata rejects invalid React children before changing a view", () => {
  const project = { cwd: "/remote/Project", name: "Project", threads: [{ file: "native-A", id: "native-A", title: "A", preview: "", updatedAt: 1, messageCount: 1 }] };
  assert.equal(data.readProjects([project])[0].threads[0].file, "native-A");
  assert.throws(() => data.readProjects([{ ...project, name: { unexpected: true } }]), /PROJECT_DATA_INVALID/);
  assert.throws(() => data.readProjects([{ ...project, threads: null }]), /PROJECT_DATA_INVALID/);
  assert.throws(() => data.readProjects([{ ...project, threads: [{ ...project.threads[0], title: { unexpected: true } }] }]), /PROJECT_DATA_INVALID/);
  assert.throws(() => data.readWindowConfig({ pinnedProjects: {} }), /PROJECT_DATA_INVALID/);
  assert.throws(() => data.readRemoteDirectory({ path: "/", entries: [{ name: { unexpected: true } }] }), /REMOTE_DIRECTORY_INVALID/);
  assert.deepEqual(data.readRemoteDirectory({ path: "/remote", directories: [{ name: "A\\B" }] }).entries[0], { name: "A\\B", path: "/remote/A\\B", isDirectory: true });
});
