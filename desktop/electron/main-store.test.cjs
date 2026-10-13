"use strict";
// Main-process tests with Electron/safeStorage API mocks; not a Windows run.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), vm = require("node:vm");
const { EventEmitter } = require("node:events");
const controllerModule = require("./remote-controller.cjs");
const { ConnectionStore, ReceiptStorage } = require("./connection-store.cjs");
const profile = { host: "fixture-login.invalid", user: "fixture", servicePath: "/fixture/server-service.py", serviceConfig: "/fixture/service.json", localPort: 30142 };
const serverId = "fixture-server";
const cleanupStores = new Map();
function directory(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-main-test-")), stores = new Set(); cleanupStores.set(dir, stores);
  t.after(async () => { await Promise.all([...stores].map(store => store.flushUI())); cleanupStores.delete(dir); fs.rmSync(dir, { recursive: true, force: true }); });
  return dir;
}
function safeStorage(available = true, backend = "mock_os_crypt") {
  return { isEncryptionAvailable: () => available, getSelectedStorageBackend: () => backend,
    encryptString: value => Buffer.from([...Buffer.from(value)].map(byte => byte ^ 171)),
    decryptString: value => Buffer.from([...value].map(byte => byte ^ 171)).toString("utf8") };
}
async function boot(t, { appData = directory(t), crypt = safeStorage(), existing, lock = true, storageOptions = {} } = {}) {
  const userData = path.join(appData, "BioMind Studio");
  if (existing) {
    const seed = new ConnectionStore(userData, crypt, controllerModule.validateProfile), saved = seed.save(existing.profile, existing.password, true);
    if (existing.ui) await seed.saveUI(saved.id, existing.ui);
  }
  const windows = [], instances = [], bridges = [], stores = [], handlers = new Map(), errors = [], partitions = new Map(), loaded = [];
  class HarnessStore extends ConnectionStore {
    constructor(directory, crypt, validateProfile) {
      super(directory, crypt, validateProfile, storageOptions); stores.push(this); cleanupStores.get(appData)?.add(this);
    }
  }
  const app = new EventEmitter(); let readyTask;
  app.commandLine = { removed: [], removeSwitch: name => app.commandLine.removed.push(name) };
  app.paths = { appData, userData }; app.setName = value => { app.name = value; };
  app.setPath = (key, value) => { app.paths[key] = value; }; app.getPath = key => app.paths[key];
  app.enableSandbox = () => { app.sandbox = true; }; app.requestSingleInstanceLock = () => lock;
  app.quitCount = 0; app.quitAttempts = 0;
  app.quit = () => {
    app.quitAttempts++; let prevented = false;
    app.emit("before-quit", { preventDefault() { prevented = true; } });
    if (!prevented) { app.quitCount++; app.emit("quit"); }
  };
  app.waitForQuit = async () => {
    if (app.quitCount) return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("MOCK_APP_QUIT_TIMEOUT")), 3000);
      app.once("quit", () => { clearTimeout(timer); resolve(); });
    });
  };
  app.getVersion = () => "1.2.0-rc.fixture"; app.relaunch = () => {};
  app.whenReady = () => ({ then(callback) { readyTask = Promise.resolve().then(callback); return readyTask; } });
  function sessionFor(name) {
    if (!partitions.has(name)) partitions.set(name, { protocol: { handle() {} }, webRequest: { onBeforeRequest(fn) { this.filter = fn; } },
      setPermissionRequestHandler(fn) { this.requestPermission = fn; }, setPermissionCheckHandler(fn) { this.checkPermission = fn; } });
    return partitions.get(name);
  }
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.destroyed = false; this.visible = true; this.messages = []; this.loads = [];
      this.webContents = new EventEmitter(); this.webContents.mainFrame = { url: "" }; this.webContents.session = sessionFor(options.webPreferences.partition);
      this.webContents.isDestroyed = () => this.destroyed;
      this.webContents.reload = () => { this.loads.push(this.url); };
      this.webContents.setWindowOpenHandler = fn => { this.openHandler = fn; };
      this.webContents.send = (channel, payload) => this.messages.push({ channel, payload }); windows.push(this);
    }
    async loadURL(url) { this.url = url; this.webContents.mainFrame.url = url; this.loads.push(url); }
    isDestroyed() { return this.destroyed; } show() { this.visible = true; } hide() { this.visible = false; } focus() {}
    setTitle(title) { this.title = title; } maximize() { this.maximized = true; } unmaximize() { this.maximized = false; }
    isMaximized() { return !!this.maximized; } getNormalBounds() { return { x: 1, y: 2, width: 1200, height: 800 }; }
    close() { let prevented = false; this.emit("close", { preventDefault() { prevented = true; } }); if (!prevented) { this.destroyed = true; this.emit("closed"); } }
  }
  class Controller {
    constructor(options) { this.options = options; this.generation = 0; this.state = { status: "disconnected" }; this.calls = []; this.installCalls = []; instances.push(this); }
    publish(state) { this.state = state; this.options.onState(state); }
    async connect(p, password) {
      this.calls.push({ profile: p, password }); this.generation++;
      this.publish({ status: "connecting", connectionId: p.id });
      this.publish({ status: "ready", connectionId: p.id, serverId, origin: "http://127.0.0.1:30142" });
      return { ...p, serverId };
    }
    async install(p, setup, bundledServerDir) {
      this.installCalls.push({ profile: p, setup, bundledServerDir });
      this.publish({ status: "configured", connectionId: p.id, serverId });
      return { status: "configured", configPath: p.serviceConfig, serverId, serviceId: "fixture-service" };
    }
    disconnect() { this.generation++; this.publish({ status: "disconnected" }); }
  }
  class Bridge {
    constructor(options) { Object.assign(this, options); this.detachCount = 0; this.closeCount = 0; this.taskCalls = []; this.handles = new Map(); bridges.push(this); }
    async identity() { return { profile: "biomind", serverId }; }
    detach() { this.detachCount++; } close() { this.closeCount++; }
    prompt(args) { this.taskCalls.push(args); } control() { throw new Error("UNEXPECTED_REMOTE_CONTROL"); }
  }
  const electron = { app, BrowserWindow, safeStorage: crypt,
    dialog: { showErrorBox: (...args) => errors.push(args), showOpenDialog: async () => ({ canceled: true, filePaths: [] }) },
    ipcMain: { handle(name, fn) { assert(!handlers.has(name), "duplicate IPC " + name); handlers.set(name, fn); } },
    Menu: { setApplicationMenu() {} }, protocol: { registerSchemesAsPrivileged() {} }, session: { fromPartition: sessionFor } };
  const customRequire = name => {
    loaded.push(name);
    if (name === "electron/main") return electron;
    if (name === "./remote-controller.cjs") return { ...controllerModule, RemoteConnectionController: Controller };
    if (name === "./connection-store.cjs") return { ...require(name), ConnectionStore: HarnessStore };
    if (name === "./diagnostics.cjs") return require(name);
    if (name === "./biomind-bridge.cjs") return { BioMindBridge: Bridge, fileUrl: value => value };
    if (name === "../out/main/biomind-client.js") return {};
    if (name.startsWith("node:")) return require(name);
    throw new Error("UNEXPECTED_BOOTSTRAP_IMPORT_" + name);
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "remote-main.cjs"), "utf8"), { require: customRequire, __dirname, Response, URL, console, setTimeout, clearTimeout }, { filename: "remote-main.cjs" });
  if (readyTask) await readyTask;
  assert.deepEqual(errors, []);
  const invoke = (window, name, ...args) => handlers.get(name)({ sender: window.webContents, senderFrame: window.webContents.mainFrame }, ...args);
  return { app, windows, instances, bridges, stores, handlers, invoke, loaded, userData, crypt, partitions };
}
test("connection store encrypts via safeStorage, preserves identity on rename, restores UI refs separately", async t => {
  const dir = directory(t), crypt = safeStorage(), store = new ConnectionStore(dir, crypt, controllerModule.validateProfile);
  const saved = store.save(profile, "SECRET_PASSWORD_DO_NOT_PERSIST", true);
  const data = fs.readFileSync(store.file, "utf8"); assert(!data.includes("SECRET_PASSWORD_DO_NOT_PERSIST"));
  assert.equal(new ConnectionStore(dir, crypt, controllerModule.validateProfile).password(saved.id), "SECRET_PASSWORD_DO_NOT_PERSIST");
  await store.saveUI(saved.id, { openThreads: [{ cwd: "/A", sessionFile: "native-A" }, { cwd: "/B", sessionFile: "native-B" }], lastThreadId: "native-B" });
  const renamed = store.save({ ...saved, label: "Renamed" }); assert.equal(renamed.id, saved.id); assert.equal(store.list().length, 1);
  const reopened = new ConnectionStore(dir, crypt, controllerModule.validateProfile);
  assert.deepEqual(reopened.ui(saved.id).openThreads, [{ cwd: "/A", sessionFile: "native-A" }, { cwd: "/B", sessionFile: "native-B" }]);
  assert.equal(reopened.ui(saved.id).lastThreadId, "native-B");
  assert(!data.includes("messages")); assert(!data.includes("memory"));
  store.clear(saved.id); assert.equal(store.password(saved.id), ""); assert.equal(store.list().length, 1);
});
test("unavailable safeStorage or basic_text cannot persist a password", t => {
  for (const crypt of [safeStorage(false), safeStorage(true, "basic_text")]) {
    const store = new ConnectionStore(directory(t), crypt, controllerModule.validateProfile);
    assert.throws(() => store.save(profile, "SECRET", true), /SECURE_STORAGE_UNAVAILABLE/);
    assert.equal(fs.existsSync(store.file), false);
  }
});
test("durable receipts roundtrip independently of native session history", t => {
  const file = path.join(directory(t), "input-receipts.json"), first = new ReceiptStorage(file);
  first.setItem("correlation", JSON.stringify({ sessionId: "native-A", digest: "hash", clientMessageId: "request-id" }));
  const reopened = new ReceiptStorage(file); assert.equal(reopened.length, 1); assert.equal(reopened.key(0), "correlation");
  assert.equal(JSON.parse(reopened.getItem("correlation")).sessionId, "native-A"); reopened.removeItem("correlation"); assert.equal(first.length, 0);
});
test("first-run connection creates the packaged flowflic renderer window with sandboxed preload", async t => {
  const f = await boot(t); assert.equal(f.windows.length, 1);
  const connection = f.windows[0]; assert.equal(connection.url, "biomind-connection://app/index.html");
  const result = await f.invoke(connection, "biomind-connection-connect", profile, "synthetic-password", true);
  assert.equal(result.ok, true); assert.equal(f.windows.length, 2); const workbench = f.windows[1];
  assert(workbench.url.endsWith("/out/renderer/index.html")); assert.equal(connection.visible, false);
  const web = workbench.options.webPreferences;
  assert.equal(web.nodeIntegration, false); assert.equal(web.contextIsolation, true); assert.equal(web.sandbox, true);
  assert(web.preload.endsWith("out/preload/index.js")); assert.equal(f.app.sandbox, true);
  assert.equal(f.instances[0].calls.length, 1); assert.equal(f.bridges[0].taskCalls.length, 0);
  assert(!f.loaded.some(name => /pi-bridge|pi-coding-agent|core-updater|session-store/.test(name)));
  assert.deepEqual(f.app.commandLine.removed, ["remote-debugging-port", "remote-debugging-pipe"]);
});
test("Skills IPC selects native B with busy A open, uses project selection scope and reads back server state", async t => {
  const f = await boot(t);
  await f.invoke(f.windows[0], "biomind-connection-connect", profile, "synthetic-password", true);
  const bridge = f.bridges[0], workbench = f.windows[1], calls = [];
  for (const id of ["native-A", "native-B"]) bridge.handles.set(id, { id, cwd: "/project", state: { sessionId: id, project: "/project", active: id === "native-A" } });
  const row = { resourceId: "B-resource", name: "skill", path: "/shared/skill/SKILL.md", source: "native", scope: "user", configuredEnabled: false };
  bridge.control = async (id, command) => {
    calls.push({ id, ...command });
    assert.equal(id, "native-B", "must never pick the first same-project owner A");
    if (command.type === "set_skill") return { saved: true, applied: false, effect: "reload_required" };
    return { skills: [row], selectionScopes: ["user", "project"] };
  };
  const args = { threadId: "native-B", cwd: "/project" };
  const skills = await f.invoke(workbench, "plugins:getSkills", args);
  assert.equal(skills[0].threadId, "native-B");
  const result = await f.invoke(workbench, "plugins:setSkillEnabled", { ...args, resourceId: row.resourceId, path: row.path, enabled: true });
  assert.deepEqual(calls.map(call => call.type), ["get_resources", "get_resources", "set_skill", "get_resources"]);
  assert.equal(calls[2].scope, "project", "discovery source scope is not the selection scope");
  assert.equal(calls[2].resourceId, "B-resource");
  assert.equal(result.skills[0].enabled, false, "UI value must be server read-back, not requested value");
  assert.equal(result.effect, "reload_required");
  assert.equal(result.applied, false);
});
test("Skills IPC refuses cross-project, read-only, missing session, stale resource and unavailable project scope", async t => {
  const f = await boot(t);
  await f.invoke(f.windows[0], "biomind-connection-connect", profile, "synthetic-password", true);
  const bridge = f.bridges[0], workbench = f.windows[1], calls = [];
  bridge.handles.set("A", { id: "A", cwd: "/A", state: { sessionId: "A", project: "/A" } });
  bridge.handles.set("history", { id: "history", cwd: "/A", state: null });
  bridge.control = async (id, command) => { calls.push({ id, ...command }); return { skills: [{ resourceId: "current", path: "/A/SKILL.md" }], selectionScopes: ["user"] }; };
  await assert.rejects(f.invoke(workbench, "plugins:getSkills", { cwd: "/A" }), /SKILLS_REQUIRE_ACTIVE_NATIVE_SESSION/);
  await assert.rejects(f.invoke(workbench, "plugins:getSkills", { threadId: "history", cwd: "/A" }), /READ_ONLY/);
  await assert.rejects(f.invoke(workbench, "plugins:getSkills", { threadId: "A", cwd: "/B" }), /REMOTE_SCOPE_MISMATCH/);
  assert.equal(calls.length, 0);
  const args = { threadId: "A", cwd: "/A", path: "/A/SKILL.md", enabled: false };
  await assert.rejects(f.invoke(workbench, "plugins:setSkillEnabled", { ...args, resourceId: "stale" }), /RESOURCE_NOT_FOUND/);
  await assert.rejects(f.invoke(workbench, "plugins:setSkillEnabled", { ...args, resourceId: "current" }), /PROJECT_SKILL_SELECTION_UNAVAILABLE/);
  assert.equal(calls.some(call => call.type === "set_skill"), false);
});
test("second launch restores remembered credentials automatically without user resubmission", async t => {
  const dir = directory(t), crypt = safeStorage();
  const f = await boot(t, { appData: dir, crypt, existing: { profile, password: "fixture-remembered" } });
  assert.equal(f.windows.length, 2); assert.equal(f.instances[0].calls.length, 1);
  assert.equal(f.instances[0].calls[0].password, "fixture-remembered");
  assert.equal(f.bridges[0].taskCalls.length, 0);
  const saved = new ConnectionStore(f.userData, crypt, controllerModule.validateProfile).last();
  assert.equal(saved.serverId, serverId);
});
test("remember request fails before network if secure credential storage is unavailable", async t => {
  const f = await boot(t, { crypt: safeStorage(false) });
  const result = await f.invoke(f.windows[0], "biomind-connection-connect", profile, "synthetic", true);
  assert.equal(result.ok, false); assert.equal(result.code, "SECURE_STORAGE_UNAVAILABLE");
  assert.equal(f.instances[0].calls.length, 0); assert.equal(f.windows.length, 1);
});
test("explicit installation IPC fixes schema and bundle source, keeping server setup and password out of Windows settings", async t => {
  const f = await boot(t), setup = { schema: "untrusted-schema", backendRoot: "/server/backend", runtimeConfig: "/server/runtime.json", workbenchPassword: "ignored-setup-password" };
  const result = await f.invoke(f.windows[0], "biomind-connection-install", profile, setup, "SYNTHETIC_INSTALL_PASSWORD");
  assert.equal(result.ok, true); assert.equal(result.value.status, "configured");
  const install = f.instances[0].installCalls[0]; assert(install);
  assert.equal(install.setup.schema, "BioMindStudioConfigureV1"); assert.equal(install.setup.workbenchPassword, "SYNTHETIC_INSTALL_PASSWORD");
  assert.equal(install.bundledServerDir, path.resolve(__dirname, "../server"));
  assert.equal(install.profile.servicePath, profile.servicePath);
  assert.equal(f.instances[0].calls.length, 0); assert.equal(f.windows.length, 1); assert.equal(f.bridges.length, 0);
  const persisted = new ConnectionStore(f.userData, f.crypt, controllerModule.validateProfile);
  const raw = fs.readFileSync(persisted.file, "utf8");
  for (const sensitive of ["SYNTHETIC_INSTALL_PASSWORD", "ignored-setup-password", "/server/backend", "/server/runtime.json"]) assert(!raw.includes(sensitive));
  assert.equal(persisted.hasPassword(persisted.last().id), false);
});
test("installation IPC cannot replace service files while its workbench is open", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } });
  const result = await f.invoke(f.windows[0], "biomind-connection-install", profile, {}, "new-password");
  assert.equal(result.ok, false); assert.equal(result.code, "INSTALL_REQUIRES_DISCONNECTED");
  assert.equal(f.instances[0].installCalls.length, 0);
});
test("IPC rejects child frames and server settings cannot silently become Windows Pi settings", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } }), workbench = f.windows[1];
  await assert.rejects(f.handlers.get("app:getConfig")({ sender: workbench.webContents, senderFrame: { url: workbench.url } }), /UNTRUSTED_IPC_SENDER/);
  await assert.rejects(f.invoke(workbench, "app:setConfig", { piCliPath: "C:\\LocalPi.exe" }), /SERVER_SETTINGS_REQUIRE_SERVER_CONTROL/);
  await assert.rejects(f.invoke(workbench, "settings:saveModels", { models: ["local"] }), /单独迁移验收/);
  const saved = await f.invoke(workbench, "app:setConfig", { theme: "dark" }); assert.equal(saved.theme, "dark");
  assert.equal(await f.invoke(workbench, "thread:close", "native-A"), true);
  assert.equal(f.bridges[0].taskCalls.length, 0);
});
test("renderer config exposes the saved active native thread without duplicating its history", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic",
    ui: { lastThreadId: "native-B", openThreads: [{ cwd: "/A", sessionFile: "native-A" }, { cwd: "/B", sessionFile: "native-B" }] } } });
  const config = await f.invoke(f.windows[1], "app:getConfig");
  assert.equal(config.lastThreadId, "native-B"); assert.equal("messages" in config, false);
});
test("focusing A/B persists only a known thread reference and does not issue backend control", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } }), workbench = f.windows[1];
  f.bridges[0].handles.set("native-A", { id: "native-A", cwd: "/A" });
  f.bridges[0].handles.set("native-B", { id: "native-B", cwd: "/B" });
  for (const id of ["native-A", "native-B", "native-A"]) {
    assert.equal(await f.invoke(workbench, "biomind:focusThread", id), true);
    assert.equal((await f.invoke(workbench, "app:getConfig")).lastThreadId, id);
  }
  await assert.rejects(f.invoke(workbench, "biomind:focusThread", "not-an-owner"), /THREAD_NOT_OPEN/);
  assert.equal(f.bridges[0].taskCalls.length, 0); assert.equal(f.instances[0].calls.length, 1);
});
test("window close detaches local bridge and saves bounds; no remote abort/control is called", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } });
  const connection = f.windows[0], workbench = f.windows[1];
  connection.close(); assert.equal(connection.isDestroyed(), false); assert.equal(connection.visible, false);
  workbench.close(); await f.app.waitForQuit(); assert.equal(f.app.quitCount, 1); assert.equal(f.bridges[0].closeCount, 1);
  assert.equal(f.instances[0].state.status, "disconnected"); assert.equal(f.bridges[0].taskCalls.length, 0);
  const stored = new ConnectionStore(f.userData, f.crypt, controllerModule.validateProfile), p = stored.last();
  assert.equal(stored.ui(p.id).config.windowBounds.width, 1200);
});
test("persistent Windows UI rename denial does not reject native focus or window close, and exposes unsaved state", async t => {
  const originalUI = { lastThreadId: "native-A", openThreads: [{ cwd: "/A", sessionFile: "native-A" }, { cwd: "/B", sessionFile: "native-B" }] };
  const storageOptions = { fs: { ...fs, promises: { ...fs.promises, rename: async () => { throw Object.assign(new Error("EPERM"), { code: "EPERM" }); } } }, retryDelays: [] };
  const f = await boot(t, { existing: { profile, password: "synthetic", ui: originalUI }, storageOptions });
  const workbench = f.windows[1], bridge = f.bridges[0], store = f.stores[0], saved = store.last();
  const file = path.join(f.userData, `ui-${saved.id}.json`), before = fs.readFileSync(file, "utf8");
  for (const id of ["native-A", "native-B"]) bridge.handles.set(id, { id, cwd: "/" + id, state: { active: true } });
  assert.equal(await f.invoke(workbench, "biomind:focusThread", "native-B"), true);
  assert.equal((await f.invoke(workbench, "app:getConfig")).lastThreadId, "native-B");
  assert.equal(await store.flushUI(), false);
  assert.deepEqual(JSON.parse(JSON.stringify(await f.invoke(workbench, "biomind:getLocalUIState"))), { status: "unsaved", code: "EPERM" });
  assert(workbench.messages.some(row => row.channel === "biomind:localUIState" && row.payload.status === "unsaved"));
  assert.equal(bridge.taskCalls.length, 0); assert.equal(f.instances[0].calls.length, 1);
  workbench.close(); await f.app.waitForQuit();
  assert.equal(f.app.quitCount, 1); assert.equal(bridge.closeCount, 1); assert.equal(bridge.taskCalls.length, 0);
  assert.equal(fs.readFileSync(file, "utf8"), before);
});
test("local UI state reports pending, unsaved and saved recovery without replacing the active native handle", async t => {
  let locked = true;
  const storageOptions = { fs: { ...fs, promises: { ...fs.promises, rename: async (from, to) => {
    if (locked) throw Object.assign(new Error("EBUSY"), { code: "EBUSY" });
    return fs.promises.rename(from, to);
  } } }, retryDelays: [] };
  const f = await boot(t, { existing: { profile, password: "synthetic" }, storageOptions }), window = f.windows[1], bridge = f.bridges[0], store = f.stores[0];
  const owner = { id: "native-A", cwd: "/A", state: { active: true } }; bridge.handles.set(owner.id, owner);
  assert.equal((await f.invoke(window, "biomind:getLocalUIState")).status, "saved");
  await f.invoke(window, "biomind:focusThread", owner.id);
  assert.equal((await f.invoke(window, "biomind:getLocalUIState")).status, "pending");
  assert.equal(await store.flushUI(), false); assert.equal((await f.invoke(window, "biomind:getLocalUIState")).code, "EBUSY");
  locked = false; await f.invoke(window, "biomind:focusThread", owner.id); assert.equal(await store.flushUI(), true);
  assert.equal((await f.invoke(window, "biomind:getLocalUIState")).status, "saved");
  assert(window.messages.some(row => row.channel === "biomind:localUIState" && row.payload.status === "saved"));
  assert.equal(bridge.handles.get(owner.id), owner); assert.equal(bridge.taskCalls.length, 0);
});
test("OS quit waits for the latest UI write and saves window bounds before shutting local owners down exactly once", async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const storageOptions = { fs: { ...fs, promises: { ...fs.promises, rename: async (from, to) => { await pending; return fs.promises.rename(from, to); } } } };
  const f = await boot(t, { existing: { profile, password: "synthetic" }, storageOptions });
  f.app.quit(); f.app.quit();
  assert.equal(f.app.quitCount, 0); assert.equal(f.bridges[0].closeCount, 1);
  release(); await f.app.waitForQuit(); assert.equal(f.app.quitCount, 1); assert.equal(f.bridges[0].closeCount, 1);
  assert.equal(f.bridges[0].taskCalls.length, 0);
  const disk = new ConnectionStore(f.userData, f.crypt, controllerModule.validateProfile), saved = disk.last();
  assert.equal(disk.ui(saved.id).config.windowBounds.width, 1200);
});
test("an indefinitely pending UI flush cannot trap application exit or cause duplicate shutdown", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } }), store = f.stores[0];
  const flush = store.flushUI.bind(store); store.flushUI = () => new Promise(() => {});
  try {
    f.app.quit(); assert.equal(f.app.quitCount, 0); await f.app.waitForQuit();
    assert.equal(f.app.quitCount, 1); assert.equal(f.bridges[0].closeCount, 1); assert.equal(f.bridges[0].taskCalls.length, 0);
    assert(fs.readFileSync(path.join(f.userData, "diagnostics.jsonl"), "utf8").includes("UI_FLUSH_TIMEOUT"));
  } finally { store.flushUI = flush; await flush(); }
});
test("renderer diagnostics retain fixed codes and bundle locations while excluding raw messages and private paths", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } }), workbench = f.windows[1];
  const issue = { source: "render", code: "RENDER_FAILED", name: "TypeError", message: "PRIVATE_PROMPT Authorization Bearer SECRET_TOKEN",
    stack: "TypeError: SYNTHETIC_PRIVATE_LABEL.js:7:8 PRIVATE_PROMPT SECRET_TOKEN\n    at App (file:///C:/Users/PRIVATE_USER/project/assets/index-PublicHash.js:42:7)\n    at send (https://secret-host.invalid/error.js:1:2?token=SECRET_TOKEN)",
    password: "WORKBENCH_PASSWORD", sourceCode: "PRIVATE_CODE" };
  assert.equal(await f.invoke(workbench, "biomind:reportRendererIssue", issue), true);
  const file = path.join(f.userData, "diagnostics.jsonl"), raw = fs.readFileSync(file, "utf8"), row = JSON.parse(raw.trim());
  for (const sensitive of ["SYNTHETIC_PRIVATE_LABEL", "PRIVATE_PROMPT", "SECRET_TOKEN", "PRIVATE_USER", "secret-host", "WORKBENCH_PASSWORD", "PRIVATE_CODE", "C:/Users", "file://", "https://"]) assert(!raw.includes(sensitive));
  assert.equal(row.code, "RENDER_FAILED"); assert.equal(row.name, "TypeError"); assert.deepEqual(row.frames, ["index-PublicHash.js:42:7"]);
  await assert.rejects(f.invoke(workbench, "biomind:reportRendererIssue", { source: "untrusted", message: "PRIVATE" }), /INVALID_RENDERER_DIAGNOSTIC/);
  await assert.rejects(f.handlers.get("biomind:reportRendererIssue")({ sender: workbench.webContents, senderFrame: { url: workbench.url } }, issue), /UNTRUSTED_IPC_SENDER/);
  assert.equal(fs.readFileSync(file, "utf8"), raw);
});
test("native renderer crash exposes recovery and manual reconnect reloads the same bridge, window and native handles", async t => {
  const f = await boot(t, { existing: { profile, password: "synthetic" } }), connection = f.windows[0], workbench = f.windows[1], bridge = f.bridges[0];
  const ownerA = { id: "native-A", cwd: "/A", state: { active: true } }, ownerB = { id: "native-B", cwd: "/B", state: { active: false } };
  bridge.handles.set(ownerA.id, ownerA); bridge.handles.set(ownerB.id, ownerB);
  workbench.webContents.emit("render-process-gone", {}, { reason: "crashed", exitCode: 19 });
  assert.equal(connection.visible, true); assert.equal(workbench.visible, false); assert.equal(workbench.loads.length, 1);
  assert(connection.messages.some(row => row.channel === "biomind-connection-state" && row.payload.code === "WORKBENCH_RENDERER_STOPPED"));
  assert.equal(bridge.closeCount, 0); assert.equal(f.instances[0].calls.length, 1); assert.equal(bridge.taskCalls.length, 0);
  f.app.emit("second-instance"); assert.equal(connection.visible, true); assert.equal(workbench.visible, false);
  const result = await f.invoke(connection, "biomind-connection-connect", profile, "", true);
  assert.equal(result.ok, true); assert.equal(f.windows.length, 2); assert.equal(f.bridges.length, 1); assert.equal(f.bridges[0], bridge);
  assert.equal(workbench.loads.length, 2); assert.equal(workbench.visible, true); assert.equal(connection.visible, false);
  assert.equal(bridge.handles.get(ownerA.id), ownerA); assert.equal(bridge.handles.get(ownerB.id), ownerB); assert.equal(bridge.taskCalls.length, 0);
});
test("second process lock failure never initializes a second controller or service", async t => {
  const f = await boot(t, { lock: false }); assert.equal(f.app.quitCount, 1); assert.equal(f.instances.length, 0); assert.equal(f.windows.length, 0);
});
