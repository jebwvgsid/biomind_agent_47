"use strict";
// Real connection HTML/script in an offline DOM. This is not Chromium/Windows acceptance.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const { parseHTML } = require("../../backend/pi-studio/node_modules/linkedom");
const html = fs.readFileSync(path.join(__dirname, "remote-connection.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "remote-connection.js"), "utf8");
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
async function fixture(overrides = {}, noPreload = false) {
  const { document, window: domWindow } = parseHTML(html), $ = id => document.getElementById(id), calls = [];
  let focused = null, stateListener;
  // Linkedom does not implement browser form values/focus/constraint validation.
  // Supply select values and focus; numeric validation remains the actual script's.
  for (const select of document.querySelectorAll("select")) Object.defineProperty(select, "value", {
    get() { return this.querySelector("option[selected]")?.getAttribute("value") ?? this.querySelector("option")?.getAttribute("value") ?? ""; },
    set(value) { for (const option of this.querySelectorAll("option")) { if (option.getAttribute("value") === String(value)) option.setAttribute("selected", ""); else option.removeAttribute("selected"); } },
  });
  for (const element of document.querySelectorAll("[id]")) {
    element.focus = () => { if (!element.disabled) focused = element.id; };
    element.scrollIntoView = () => {};
  }
  const success = value => ({ ok: true, value });
  const defaults = {
    list: async () => success({ profiles: [], lastId: "", state: { status: "disconnected" } }),
    connect: async () => ({ ok: false, code: "SERVICE_NOT_INSTALLED" }),
    install: async () => ({ ok: false, code: "SERVICE_NOT_INSTALLED" }),
    preset: async () => ({ ok: false, code: "INVALID_SETUP_PRESET" }),
    save: async () => success({ id: "saved" }), remove: async () => success(true), clearAuth: async () => success(true),
    selectKey: async () => success(null), disconnect: async () => success(true),
    onState: callback => { stateListener = callback; },
  };
  const api = Object.fromEntries(Object.entries({ ...defaults, ...overrides }).map(([method, fn]) => [method, (...args) => { calls.push({ method, args }); return fn(...args); }]));
  const window = { bioMindConnection: noPreload ? undefined : api, addEventListener: domWindow.addEventListener.bind(domWindow) };
  function Option(label, value) { const option = document.createElement("option"); option.textContent = label; option.setAttribute("value", value); return option; }
  vm.runInNewContext(script, { window, document, Option, console }, { filename: "remote-connection.js" });
  await flush();
  const event = (id, type) => $(id).dispatchEvent(new domWindow.Event(type, { bubbles: true, cancelable: true }));
  const values = data => { for (const [id, value] of Object.entries(data)) $(id).value = value; };
  const connection = () => values({ alias: "yongjiusuo", servicePath: "/owned/service/server-service.py", serviceConfig: "/owned/private/service.json", password: "SYNTHETIC_WORKBENCH_PASSWORD" });
  const setup = () => { connection(); values({ backendRoot: "/backend", mainRoot: "/main", runtimeConfig: "/runtime.json", sessionRoot: "/sessions", pythonPath: "/python", nodePath: "/node", uploadsRoot: "/uploads", projectRoots: "/projects" }); };
  return { $, document, window: domWindow, calls, values, connection, setup, focused: () => focused, emitState: value => stateListener(value),
    click: id => event(id, "click"), submit: () => event("connection-form", "submit"), change: id => event(id, "change"),
    text: () => $("status").textContent };
}
test("connection feedback is sticky and repeated adjacent to both action groups; script owns scoped validation", () => {
  assert.match(html, /<form[^>]*id="connection-form"[^>]*novalidate/);
  assert(html.indexOf('id="status"') < html.indexOf('id="connection-form"'));
  assert.match(html, /id="status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert(html.includes('id="action-status"')); assert(html.includes('id="install-status"'));
  assert.match(fs.readFileSync(path.join(__dirname, "remote-connection.css"), "utf8"), /\.connection-status\s*\{[^}]*position:sticky;[^}]*top:12px/);
  assert(!html.includes("youjiusuo")); assert(html.includes("工作台服务启动方式")); assert(html.includes("不是 SSH 登录密码"));
});
test("missing preload and failed startup settings read are visible, sanitized errors", async () => {
  const missing = await fixture({}, true); assert.match(missing.text(), /CONNECTION_PRELOAD_UNAVAILABLE/);
  const failed = await fixture({ list: async () => { throw new Error("synthetic password must not be displayed"); } });
  assert.match(failed.text(), /SETTINGS_LOAD_FAILED/); assert(!failed.text().includes("synthetic password"));
});
test("connect gives synchronous visible feedback, prevents duplicate actions, and retains password on failure", async () => {
  const request = deferred(), f = await fixture({ connect: () => request.promise }); f.connection();
  f.submit(); assert.match(f.text(), /已收到连接请求/); assert.equal(f.$("connect").disabled, true);
  assert.equal(f.$("action-status").textContent, f.text());
  f.emitState({ status: "error", code: "INVALID_SERVER_PASSWORD" }); assert.equal(f.focused(), null, "native disabled inputs cannot receive focus");
  f.submit(); f.click("install"); assert.equal(f.calls.filter(c => c.method === "connect").length, 1); assert.equal(f.calls.filter(c => c.method === "install").length, 0);
  request.resolve({ ok: false, code: "INVALID_SERVER_PASSWORD" }); await flush();
  assert.match(f.text(), /INVALID_SERVER_PASSWORD/); assert.equal(f.focused(), "password");
  assert.equal(f.$("password").value, "SYNTHETIC_WORKBENCH_PASSWORD"); assert.equal(Boolean(f.$("connect").disabled), false);
  assert(!f.text().includes("SYNTHETIC_WORKBENCH_PASSWORD"));
});
test("empty workbench password is explicitly reported and focused rather than swallowed", async () => {
  const f = await fixture({ connect: async () => ({ ok: false, code: "INVALID_SERVER_PASSWORD" }) }); f.connection(); f.$("password").value = "";
  f.submit(); await flush(); assert.match(f.text(), /请输入服务器工作台口令/); assert.equal(f.focused(), "password");
});
test("installation password helper locates the masked field without generating or displaying a password", async () => {
  const f = await fixture(); f.connection(); const before = f.calls.length;
  f.click("focus-password"); assert.equal(f.focused(), "password"); assert.equal(f.calls.length, before);
  assert.match(f.text(), /首次安装请自行设置/); assert.match(f.text(), /不是 SSH 登录密码/);
  assert.equal(f.$("password").getAttribute("type"), "password"); assert.equal(f.$("password").value, "SYNTHETIC_WORKBENCH_PASSWORD");
  assert(!f.text().includes("SYNTHETIC_WORKBENCH_PASSWORD"));
});
test("successful connection clears only the in-memory password after acceptance", async () => {
  const f = await fixture({ connect: async () => ({ ok: true, value: { id: "connected" } }) }); f.connection(); f.submit(); await flush();
  assert.equal(f.calls.filter(c => c.method === "connect").length, 1); assert.equal(f.$("password").value, "");
});
test("connect ignores unrelated collapsed install and hidden manual-port constraints", async () => {
  const f = await fixture(); f.connection(); f.values({ cpus: "1.5", remotePort: "0", backendRoot: "" });
  f.submit(); await flush(); assert.equal(f.calls.filter(c => c.method === "connect").length, 1); assert.match(f.text(), /SERVICE_NOT_INSTALLED/);
  assert.match(f.text(), /首次安装/);
});
test("invalid active port blocks the operation and identifies the field before any IPC", async () => {
  const f = await fixture(); f.connection(); f.$("localPort").value = "1.5";
  f.submit(); await flush(); assert.equal(f.calls.filter(c => c.method === "connect").length, 0);
  assert.match(f.text(), /FORM_VALIDATION_FAILED/); assert.equal(f.focused(), "localPort"); assert.equal(Boolean(f.$("connect").disabled), false);
});
test("missing required installation field opens collapsed setup and focuses it", async () => {
  const f = await fixture(); f.setup(); f.$("backendRoot").value = "";
  const details = f.$("backendRoot").closest("details"); details.open = false; f.click("install"); await flush();
  assert.equal(details.open, true); assert.equal(f.focused(), "backendRoot"); assert.equal(f.calls.filter(c => c.method === "install").length, 0);
});
test("direct workbench installation is allowed and ignores inactive Slurm CPU settings", async () => {
  const request = deferred(), f = await fixture({ install: () => request.promise }); f.setup(); f.values({ "start-mode": "local", cpus: "0" });
  f.click("install"); assert.match(f.text(), /服务器安装配置/); assert.equal(f.$("install-status").textContent, f.text());
  f.click("install"); f.submit(); assert.equal(f.calls.filter(c => c.method === "install").length, 1); assert.equal(f.calls.filter(c => c.method === "connect").length, 0);
  const sent = f.calls.find(c => c.method === "install"); assert.equal(sent.args[1].mode, "local"); assert.equal(sent.args[1].slurm, undefined);
  request.resolve({ ok: false, code: "INVALID_WORKBENCH_PASSWORD" }); await flush();
  assert.equal(f.focused(), "password"); assert.equal(f.$("password").value, "SYNTHETIC_WORKBENCH_PASSWORD");
});
test("active Slurm CPU validation blocks installation and opens its details", async () => {
  const f = await fixture(); f.setup(); f.values({ "start-mode": "slurm", cpus: "1.5" }); f.change("start-mode");
  f.click("install"); await flush(); assert.equal(f.calls.filter(c => c.method === "install").length, 0); assert.equal(f.focused(), "cpus");
});
test("preset, save and file-picker promise failures are caught without secret diagnostics", async () => {
  for (const [id, method, expected] of [["load-preset", "preset", "PRESET_LOAD_FAILED"], ["save", "save", "SETTINGS_SAVE_FAILED"], ["select-key", "selectKey", "KEY_SELECTION_FAILED"]]) {
    const f = await fixture({ [method]: async () => { throw new Error("SECRET diagnostic text"); } }); f.connection();
    f.click(id); await flush(); assert.match(f.text(), new RegExp(expected)); assert(!f.text().includes("SECRET")); assert.equal(Boolean(f.$("connect").disabled), false);
  }
});
test("safe backend codes remain visible and auth failures focus the retained password", async () => {
  const f = await fixture(); f.connection();
  for (const code of ["SSH_HOST_NOT_FOUND", "SSH_CONNECTION_TIMEOUT", "SSH_CONNECTION_REFUSED", "SERVICE_NOT_INSTALLED", "SERVER_AUTH_FAILED"]) {
    f.emitState({ status: "error", code }); assert(f.text().includes("[" + code + "]"));
  }
  assert.equal(f.focused(), "password"); assert.equal(f.$("password").value, "SYNTHETIC_WORKBENCH_PASSWORD");
});
test("global unhandled rejection has a visible safe fallback", async () => {
  const f = await fixture(), event = new f.window.Event("unhandledrejection", { cancelable: true });
  event.reason = new Error("SECRET arbitrary failure"); f.window.dispatchEvent(event);
  assert.match(f.text(), /CONNECTION_UI_ERROR/); assert(!f.text().includes("SECRET")); assert.equal(event.defaultPrevented, true);
});
