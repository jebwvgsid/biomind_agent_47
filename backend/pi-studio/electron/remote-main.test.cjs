"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const subject = require("./remote-controller.cjs");
const tick = () => new Promise(resolve => setImmediate(resolve));
async function harness({ failLoad = false } = {}) {
  const windows = [], handlers = new Map(), sessions = new Map(), files = new Map(), flags = [];
  const controllers = [];
  const app = new EventEmitter();
  Object.assign(app, { setName() {}, setPath() {}, getPath: () => "/synthetic/app data", enableSandbox: () => flags.push("sandbox"),
    commandLine: { removeSwitch: flag => flags.push(flag) }, requestSingleInstanceLock: () => true,
    whenReady: () => Promise.resolve(), quit: () => flags.push("quit") });
  class FakeWindow extends EventEmitter {
    constructor(options) { super(); this.options = options; this.destroyed = false; this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: "" }; this.webContents.send = () => {};
      this.webContents.setWindowOpenHandler = handler => { this.openHandler = handler; }; windows.push(this); }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    show() { if (this.destroyed) throw new Error("Object has been destroyed"); } focus() {} setTitle() {}
    loadURL(url) { this.url = url; this.webContents.mainFrame.url = url;
      if (url.startsWith("biomind-connection:") && (failLoad || !sessions.get(this.options.webPreferences.partition)?.protocols.has("biomind-connection"))) return Promise.reject(new Error("ERR_FAILED"));
      return url.startsWith("http:") ? new Promise(resolve => { this.finishLoad = resolve; }) : Promise.resolve(); }
  }
  class FakeController {
    constructor(options) { this.options = options; this.generation = 0; this.state = { status: "disconnected" }; controllers.push(this); }
    async connect(value) { const profile = subject.validateProfile(value); this.generation++;
      this.state = { status: "ready", origin: `http://127.0.0.1:${profile.localPort}`, connectionId: profile.id, serverId: "fixture" };
      return { ...profile, serverId: "fixture" }; }
    disconnect() { this.generation++; this.state = { status: "disconnected" }; }
    authorization(origin) { return this.state.status === "ready" && origin === this.state.origin ? "Basic synthetic" : undefined; }
  }
  const electron = { app, BrowserWindow: FakeWindow, ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    Menu: { setApplicationMenu() {} }, dialog: { showErrorBox: () => flags.push("startup-error-dialog") }, protocol: { registerSchemesAsPrivileged() {}, handle() { throw new Error("Default session protocol is not available in an isolated window"); } },
    session: { fromPartition(partition) { if (!sessions.has(partition)) { const item = new EventEmitter();
      item.setPermissionRequestHandler = handler => { item.permission = handler; }; item.setPermissionCheckHandler = () => {};
      item.protocols = new Map(); item.protocol = { handle: (scheme, handler) => item.protocols.set(scheme, handler) };
      item.authClears = 0; item.cacheClears = 0; item.drafts = "retained";
      item.clearAuthCache = async () => { item.authClears++; }; item.clearCache = async () => { item.cacheClears++; }; item.webRequest = { onBeforeRequest: handler => { item.before = handler; }, onBeforeSendHeaders: handler => { item.headers = handler; } };
      sessions.set(partition, item); } return sessions.get(partition); } } };
  const fakeFs = { mkdirSync() {}, writeFileSync: (name, text) => files.set(name, text),
    readFileSync: name => { if (!files.has(name)) throw new Error("ENOENT"); return files.get(name); },
    renameSync: (from, to) => { files.set(to, files.get(from)); files.delete(from); } };
  const code = fs.readFileSync(path.join(__dirname, "remote-main.cjs"), "utf8");
  vm.runInNewContext(code, { require: name => {
    if (name === "electron/main") return electron;
    if (name === "node:fs") return fakeFs;
    if (name === "node:path") return path;
    if (name === "node:os") return { homedir: () => "/synthetic/home" };
    if (name === "./remote-controller.cjs") return { ...subject, RemoteConnectionController: FakeController };
    throw new Error("Unexpected dependency " + name);
  }, __dirname, process: { platform: "win32", env: {} }, URL, Response, console });
  await tick();
  const local = windows[0], event = { sender: local.webContents, senderFrame: local.webContents.mainFrame };
  return { windows, handlers, sessions, files, flags, controller: controllers[0], event, invoke: (name, ...args) => handlers.get("biomind-connection-" + name)(event, ...args) };
}
const profile = { alias: "fixture-host", label: "fixture", jump: "", localPort: 30142, remotePort: 30141 };
test("actual remote main isolates local IPC, selects partition before navigation and survives replacing its old window", async () => {
  const h = await harness();
  assert(h.flags.includes("sandbox")); assert(h.flags.includes("remote-debugging-port"));
  assert.equal(h.controller.options.sshConfig, "strict-ssh.conf"); assert.equal(h.controller.options.sshCwd, "/synthetic/app data");
  const pending = h.invoke("connect", profile, "never-persist"); await tick();
  const remote = h.windows[1], isolated = h.sessions.get(remote.options.webPreferences.partition);
  assert(remote.options.webPreferences.partition.startsWith("persist:biomind-")); assert.equal(remote.options.webPreferences.preload, undefined);
  await assert.rejects(h.handlers.get("biomind-connection-list")({ sender: remote.webContents, senderFrame: remote.webContents.mainFrame }), /UNTRUSTED_IPC_SENDER/);
  let result; isolated.before({ url: "https://evil.invalid", resourceType: "xhr" }, value => { result = value; }); assert.equal(result.cancel, true);
  isolated.headers({ url: "https://evil.invalid", requestHeaders: { Authorization: "secret" } }, value => { result = value; }); assert.equal(result.requestHeaders.Authorization, undefined);
  remote.finishLoad(); assert.equal((await pending).ok, true);
  assert(![...h.files.values()].join("\n").includes("never-persist"));
  const replacement = h.invoke("connect", profile, "never-persist"); await tick();
  assert.equal(remote.destroyed, true); assert.equal(h.controller.state.status, "ready", "Old window cleanup must not disconnect the new generation");
  assert.equal(isolated.listenerCount("will-download"), 1);
  h.windows[2].finishLoad(); assert.equal((await replacement).ok, true);
});
test("actual remote main blocks late page load and credential reuse after disconnect", async () => {
  const h = await harness(); const pending = h.invoke("connect", profile, "synthetic"); await tick();
  const remote = h.windows[1], isolated = h.sessions.get(remote.options.webPreferences.partition);
  await h.invoke("disconnect");
  let result; isolated.before({ url: remote.url, resourceType: "xhr" }, value => { result = value; }); assert.equal(result.cancel, true);
  isolated.headers({ url: remote.url, requestHeaders: { authorization: "old" } }, value => { result = value; });
  assert.equal(Object.keys(result.requestHeaders).length, 0);
  remote.finishLoad(); const response = await pending;
  assert.equal(response.ok, false); assert.equal(response.code, "CONNECTION_CANCELLED"); assert.equal(remote.destroyed, true);
  assert.equal(h.controller.state.status, "disconnected");
});

test("actual remote main deletes only shortcut and network credentials while retaining draft storage", async () => {
  const h = await harness(); const pending = h.invoke("connect", profile, "synthetic"); await tick();
  const remote = h.windows[1], isolated = h.sessions.get(remote.options.webPreferences.partition);
  remote.finishLoad(); const connected = await pending; const id = connected.value.id;
  assert.equal((await h.invoke("clear-auth", id)).ok, true);
  assert.equal(h.controller.state.status, "disconnected"); assert.equal(remote.destroyed, true);
  assert.equal(isolated.authClears, 1); assert.equal(isolated.cacheClears, 1); assert.equal(isolated.drafts, "retained");
  assert.equal((await h.invoke("list")).value.profiles.length, 1);
  assert.equal((await h.invoke("delete", id)).ok, true);
  assert.equal((await h.invoke("list")).value.profiles.length, 0); assert.equal(isolated.drafts, "retained");
  assert.equal((await h.invoke("delete", "../unsafe")).ok, false);
});

test("connection protocol is registered on the connection window session", async () => {
  const h = await harness();
  const local = h.windows[0];
  assert(h.sessions.get(local.options.webPreferences.partition).protocols.has("biomind-connection"));
  assert.equal(local.url, "biomind-connection://app/index.html");
  assert(!h.flags.includes("quit"));
});
test("connection startup failure shows a diagnostic instead of silently exiting", async () => {
  const h = await harness({ failLoad: true });
  assert(h.flags.includes("startup-error-dialog"));
  assert(h.flags.includes("quit"));
  assert.match(h.files.get("/synthetic/app data/startup-error.log"), /ERR_FAILED/);
});

test("remote close after the connection window was destroyed exits without reopening it", async () => {
  const h = await harness();
  const pending = h.invoke("connect", profile, "never-persist");
  await tick();
  const remote = h.windows[1];
  remote.finishLoad();
  await pending;
  h.windows[0].destroy();
  assert.doesNotThrow(() => remote.destroy());
  assert.equal(h.controller.state.status, "disconnected");
});
