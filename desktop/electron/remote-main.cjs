"use strict";
// BioMind desktop: flowflic renderer + existing remote BioMind owner.
// The upstream local Pi bootstrap and Pi updater are deliberately not imported.
const { app, BrowserWindow, dialog, ipcMain, Menu, protocol, session, safeStorage } = require("electron/main");
const fs = require("node:fs"), path = require("node:path"), { pathToFileURL } = require("node:url");
const { RemoteConnectionController, validateProfile, isConnectionPageSender } = require("./remote-controller.cjs");
const { ConnectionStore } = require("./connection-store.cjs");
const { BioMindBridge, fileUrl } = require("./biomind-bridge.cjs");
const { createDiagnostics } = require("./diagnostics.cjs");
const { setTimeout, clearTimeout } = require("node:timers");
const PAGE = "biomind-connection://app/index.html";
protocol.registerSchemesAsPrivileged([{ scheme: "biomind-connection", privileges: { standard: true, secure: true } }]);
app.setName("BioMind Studio");
app.setPath("userData", path.join(app.getPath("appData"), "BioMind Studio"));
app.enableSandbox();
app.commandLine.removeSwitch("remote-debugging-port"); app.commandLine.removeSwitch("remote-debugging-pipe");
let connectionWindow, workbenchWindow, controller, store, bridge, activeProfile, connecting = false, quitting = false;
let quitFlushed = false, quitStarted = false, workbenchNeedsReload = false;
const diagnostic = createDiagnostics(app.getPath("userData"), app.getVersion());
const ROOT = path.resolve(__dirname, "..");
const WORKBENCH = pathToFileURL(path.join(ROOT, "out/renderer/index.html")).href;
const defaults = { piCliPath: "", pinnedProjects: [], pinnedThreads: [], archivedProjects: [], archivedThreads: [],
  theme: "system", language: "zh", threadPermissions: {}, remoteSignalingUrl: "", remoteSignalingEnabled: false, remoteStunUrls: [] };
const unsupported = () => { throw new Error("服务器未提供此能力；未修改任何 Windows Pi 配置。此功能需要单独迁移验收。"); };
function send(channel, payload) { try { if (workbenchWindow && !workbenchWindow.isDestroyed()) workbenchWindow.webContents.send(channel, payload); } catch {} }
function localUIState() {
  const state = activeProfile ? store.uiSaveStatus(activeProfile.id) : {};
  return state.errorCode ? { status: "unsaved", code: state.errorCode } : { status: state.pending ? "pending" : "saved" };
}
function recoverWorkbench(code, reload = true) {
  if (quitting) return;
  if (reload) workbenchNeedsReload = true;
  diagnostic({ source: "main", code });
  workbenchWindow?.hide(); connectionWindow?.show(); connectionWindow?.focus();
  connectionWindow?.webContents.send("biomind-connection-state", { status: "error", code });
}
function config() {
  const ui = activeProfile ? store.ui(activeProfile.id) : {};
  return { ...defaults, ...ui.config, lastThreadId: ui.lastThreadId };
}
function saveConfig(patch) {
  if (!activeProfile) throw new Error("SERVER_NOT_CONNECTED");
  const allowed = new Set(["theme", "language", "pinnedProjects", "pinnedThreads", "archivedProjects", "archivedThreads", "windowBounds"]);
  if (!patch || typeof patch !== "object" || Object.keys(patch).some(k => !allowed.has(k))) throw new Error("SERVER_SETTINGS_REQUIRE_SERVER_CONTROL");
  if (patch.theme && !["system", "light", "dark"].includes(patch.theme) || patch.language && !["zh", "en"].includes(patch.language)) throw new Error("INVALID_WINDOW_SETTINGS");
  const ui = store.ui(activeProfile.id); ui.config = { ...config(), ...patch }; store.saveUI(activeProfile.id, ui); return config();
}
function guard(event, window, url) {
  if (!window || window.isDestroyed() || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url.split("#")[0] !== url) throw new Error("UNTRUSTED_IPC_SENDER");
}
function local(name, callback) {
  ipcMain.handle(name, async (event, ...args) => {
    guard(event, connectionWindow, PAGE);
    try { return { ok: true, value: await callback(...args) }; }
    catch (e) { return { ok: false, code: /^[A-Z_0-9]+$/.test(e.message) ? e.message : "CONNECTION_FAILED" }; }
  });
}
function workspace(name, callback) {
  ipcMain.handle(name, async (event, ...args) => { guard(event, workbenchWindow, WORKBENCH); return callback(...args); });
}
function lockdown(window, allowed) {
  const contents = window.webContents;
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  for (const event of ["will-navigate", "will-redirect"]) contents.on(event, (e, url) => { if (!allowed(url)) e.preventDefault(); });
  contents.on("will-attach-webview", e => e.preventDefault());
  contents.session.setPermissionRequestHandler((_w, _p, done) => done(false));
  contents.session.setPermissionCheckHandler(() => false);
}
async function showWorkbench(profile) {
  if (workbenchWindow && !workbenchWindow.isDestroyed()) {
    if (workbenchNeedsReload) { await workbenchWindow.loadURL(WORKBENCH); workbenchNeedsReload = false; }
    workbenchWindow.show(); workbenchWindow.focus(); connectionWindow?.hide(); return;
  }
  const cfg = config(), bounds = cfg.windowBounds;
  const window = new BrowserWindow({ width: bounds?.width || 1440, height: bounds?.height || 920, minWidth: 1000, minHeight: 650,
    title: `BioMind Studio — ${profile.label}`, frame: false, show: false,
    webPreferences: { partition: `persist:biomind-workbench-${profile.id}`, sandbox: true, contextIsolation: true,
      nodeIntegration: false, webSecurity: true, webviewTag: false, devTools: false, preload: path.join(ROOT, "out/preload/index.js") } });
  workbenchWindow = window;
  workbenchNeedsReload = false;
  lockdown(window, url => url.split("#")[0] === WORKBENCH);
  // Renderer has no direct network authority or server credentials. All requests
  // go through the checked IPC bridge, including attachment byte uploads.
  window.webContents.session.webRequest.onBeforeRequest((details, done) => {
    let allowed = false;
    try { const u = new URL(details.url); allowed = ["file:", "data:", "blob:"].includes(u.protocol); } catch {}
    done({ cancel: !allowed });
  });
  window.on("maximize", () => send("window:maximized-changed", true));
  window.on("unmaximize", () => send("window:maximized-changed", false));
  window.webContents.on("render-process-gone", () => recoverWorkbench("WORKBENCH_RENDERER_STOPPED"));
  window.webContents.on("did-fail-load", (_event, code, _description, _url, isMainFrame) => {
    if (isMainFrame && code !== -3) recoverWorkbench("WORKBENCH_LOAD_FAILED");
  });
  window.on("unresponsive", () => recoverWorkbench("WORKBENCH_UNRESPONSIVE"));
  window.on("close", () => {
    try { if (!quitting && !window.isDestroyed()) saveConfig({ windowBounds: { ...window.getNormalBounds(), maximized: window.isMaximized() } }); }
    catch { diagnostic({ source: "storage", code: "UI_STATE_SAVE_FAILED" }); }
  });
  window.on("closed", () => { workbenchWindow = null; if (!quitting) app.quit(); });
  await window.loadURL(WORKBENCH);
  if (bounds?.maximized) window.maximize();
  window.show(); connectionWindow?.hide();
}
async function connect(profile, password, remember = true) {
  if (connecting) throw new Error("CONNECTION_IN_PROGRESS");
  const value = validateProfile(profile);
  if (workbenchWindow && activeProfile?.id !== value.id) throw new Error("CLOSE_WORKBENCH_TO_SWITCH_SERVER");
  password = password || store.password(value.id);
  if (!password) throw new Error("INVALID_SERVER_PASSWORD");
  // Fail before network activity if durable credential storage was requested but unavailable.
  if (remember && !store.encryptionAvailable()) throw new Error("SECURE_STORAGE_UNAVAILABLE");
  connecting = true;
  try {
    const connected = await controller.connect(value, password);
    activeProfile = store.save(connected, password, remember);
    if (!bridge) bridge = new BioMindBridge({ controller, send, store, input: require("../out/main/biomind-client.js"), receiptFile: path.join(app.getPath("userData"), `input-receipts-${activeProfile.id}.json`) });
    await bridge.identity();
    await showWorkbench(activeProfile); return activeProfile;
  } finally { password = ""; connecting = false; }
}
async function clearConnection(id, remove) {
  if (!/^[a-f0-9]{24}$/.test(id)) throw new Error("INVALID_CONNECTION_ID");
  store.clear(id, remove);
  if (activeProfile?.id === id) { controller.disconnect(); bridge?.detach(); send("biomind:status", { status: "disconnected" }); }
}
function registerWorkspace() {
  const w = workspace;
  w("app:getVersion", () => app.getVersion());
  w("app:getConfig", config); w("app:setConfig", saveConfig);
  w("app:resolveRuntime", async () => { const p = await bridge.identity(); return { ok: true, node: "server", cli: `BioMind Main · ${p.serverId}` }; });
  w("app:getProjects", () => bridge.projects());
  w("app:openProject", cwd => bridge.openProject(cwd));
  w("app:prewarm", () => ({ remote: true, prewarm: false }));
  w("app:getFileTree", (cwd, rel) => bridge.tree(cwd, rel));
  w("app:readPreview", abs => bridge.preview(abs));
  w("app:fileExists", async abs => { try { await bridge.json(fileUrl(abs, "meta")); return true; } catch { return false; } });
  w("app:stageClipboardFile", args => bridge.upload(args));
  w("app:searchThreads", async query => {
    if (typeof query !== "string" || query.length > 256) throw new Error("INVALID_QUERY");
    const projects = await bridge.projects();
    return projects.flatMap(p => p.threads.filter(t => (t.title + t.preview).toLowerCase().includes(query.toLowerCase())).map(t => ({ ...t, cwd: p.cwd, projectName: p.name, snippet: t.preview, matchCount: 1 })));
  });
  for (const [name, field, key] of [["app:setProjectPinned", "pinnedProjects", "cwd"], ["app:setThreadPinned", "pinnedThreads", "file"]]) {
    w(name, args => { const items = config()[field].filter(x => x !== args[key]); if (args.pinned) items.push(args[key]); return saveConfig({ [field]: items }); });
  }
  w("app:unpinProject", cwd => saveConfig({ pinnedProjects: config().pinnedProjects.filter(x => x !== cwd) }));
  w("app:editAction", action => { if (!["copy", "cut", "paste", "delete", "selectAll"].includes(action)) throw new Error("INVALID_EDIT_ACTION"); workbenchWindow.webContents[action](); });
  w("app:showOpenDialog", unsupported);
  for (const name of ["app:openFolderInExplorer", "app:savePreviewHtml", "app:showFileContextMenu", "app:updatePi", "app:checkAppUpdate", "app:downloadAppUpdate", "app:installAppUpdate", "app:checkCoreUpdate", "app:getTotalUsage"]) w(name, unsupported);
  w("app:relaunch", () => { app.relaunch(); app.quit(); });
  w("thread:open", args => bridge.open(args)); w("thread:loadHistory", args => bridge.loadHistory(args));
  w("thread:close", () => true); // A tab closure is not permission to stop its server owner.
  w("thread:prompt", args => bridge.prompt(args)); w("thread:steer", args => bridge.prompt(args, "steer")); w("thread:followUp", args => bridge.prompt(args, "follow_up"));
  w("thread:abort", id => bridge.control(id, { type: "abort" }));
  w("thread:getModels", id => bridge.control(id, { type: "get_models" }));
  w("thread:refreshModels", id => bridge.control(id, { type: "get_models" }));
  w("thread:setModel", async ({ threadId, provider, modelId }) => {
    await bridge.control(threadId, { type: "set_model", provider, modelId });
    const h = bridge.handles.get(threadId), s = await bridge.state(threadId, h.cwd);
    return { model: s.model, thinkingLevel: s.thinkingLevel };
  });
  w("thread:setThinking", async ({ threadId, level }) => { await bridge.control(threadId, { type: "set_thinking_level", level }); const h = bridge.handles.get(threadId); return { thinkingLevel: (await bridge.state(threadId, h.cwd)).thinkingLevel }; });
  w("thread:getThinkingLevels", async id => ({ levels: (await bridge.control(id, { type: "get_models" })).availableThinkingLevels || ["off"] }));
  w("thread:getCommands", id => bridge.control(id, { type: "get_commands" }));
  w("thread:getStats", async id => { const h = bridge.handles.get(id); return h ? (await bridge.state(id, h.cwd))?.stats || null : null; });
  w("thread:extuiResponse", args => bridge.extui(args));
  for (const name of ["thread:setPermission", "thread:delete", "thread:newSession", "thread:getBranchMessages", "thread:fork", "thread:clone", "thread:setName"]) w(name, unsupported);
  const skillOwner = args => {
    if (!args || typeof args.threadId !== "string" || !args.threadId || typeof args.cwd !== "string") throw new Error("SKILLS_REQUIRE_ACTIVE_NATIVE_SESSION");
    const h = bridge.handles.get(args.threadId);
    if (!h || !h.state) throw new Error("SKILLS_OWNER_UNAVAILABLE_OR_READ_ONLY");
    if (h.id !== args.threadId || h.cwd !== args.cwd || h.state.sessionId !== h.id || (h.state.project ?? h.state.cwd) !== h.cwd) throw new Error("REMOTE_SCOPE_MISMATCH");
    return h;
  };
  const displayedSkills = (h, resources) => (resources.skills || []).map(s => ({ ...s, root: s.source,
    enabled: s.configuredEnabled === true, threadId: h.id, cwd: h.cwd }));
  w("plugins:getSkills", async args => {
    const h = skillOwner(args);
    const r = await bridge.control(h.id, { type: "get_resources" });
    return displayedSkills(h, r);
  });
  w("plugins:setSkillEnabled", async args => {
    const h = skillOwner(args);
    if (typeof args.enabled !== "boolean" || typeof args.resourceId !== "string" || typeof args.path !== "string") throw new Error("INVALID_RESOURCE_SELECTION");
    const r = await bridge.control(h.id, { type: "get_resources" });
    const skill = r.skills?.find(s => s.resourceId === args.resourceId && s.path === args.path);
    if (!skill) throw new Error("RESOURCE_NOT_FOUND");
    if (!Array.isArray(r.selectionScopes) || !r.selectionScopes.includes("project")) throw new Error("PROJECT_SKILL_SELECTION_UNAVAILABLE");
    const saved = await bridge.control(h.id, { type: "set_skill", resourceId: skill.resourceId, scope: "project", enabled: args.enabled });
    const after = await bridge.control(h.id, { type: "get_resources" });
    return { skills: displayedSkills(h, after), effect: saved.effect, applied: saved.applied === true };
  });
  for (const name of ["plugins:getPackages", "plugins:setPackageEnabled", "plugins:installPackage", "plugins:removePackage", "plugins:updatePackages", "skillsHub:leaderboard", "skillsHub:search", "skillsHub:detail", "skillsHub:install", "automation:getTasks", "automation:saveTask", "automation:deleteTask", "automation:runNow", "remote:getStatus", "remote:createPairing", "remote:getTransportConfig", "remote:enableSignaling", "remote:disableSignaling", "remote:approvePairing", "remote:rejectPairing", "remote:revokeDevice", "remote:setConfig", "remote:transportOpen", "remote:transportClose", "remote:transportStatus", "remote:transportFrame", "remote:sendSignal"]) w(name, unsupported);
  for (const action of ["getUsagePricing", "saveUsagePricing", "getPlanQuotas", "getUsage", "getMcp", "saveMcp", "getCodemode", "saveCodemode", "mcpAction", "cancelMcpAction", "getModels", "discoverModels", "testModel", "saveModels", "getThinking", "saveThinking", "getDiagnostics", "getPaths", "openPath", "showItem", "openAgentDir"]) w(`settings:${action}`, unsupported);
  w("window:minimize", () => workbenchWindow.minimize()); w("window:maximize", () => workbenchWindow.isMaximized() ? workbenchWindow.unmaximize() : workbenchWindow.maximize());
  w("window:close", () => workbenchWindow.close()); w("window:isMaximized", () => workbenchWindow.isMaximized());
  w("biomind:getStatus", () => ({ ...controller.state, version: app.getVersion(), upstreamCommit: "0fbc8178b80af55bf043465ef36d5bfe8373463c" }));
  w("biomind:getLocalUIState", localUIState);
  w("biomind:reportRendererIssue", issue => {
    if (!issue || !["render", "window_error", "unhandledrejection"].includes(issue.source)) throw new Error("INVALID_RENDERER_DIAGNOSTIC");
    diagnostic(issue); return true;
  });
  w("biomind:showConnection", () => { connectionWindow.show(); connectionWindow.focus(); });
  w("biomind:browseProjects", cwd => bridge.browse(cwd));
  w("biomind:restoreThreads", () => bridge.restore()); w("biomind:activateThread", id => bridge.activate(id));
  w("biomind:focusThread", id => {
    if (!activeProfile) throw new Error("SERVER_NOT_CONNECTED");
    if (id !== null && !bridge.handles.has(id)) throw new Error("THREAD_NOT_OPEN");
    const ui = store.ui(activeProfile.id); ui.lastThreadId = id; store.saveUI(activeProfile.id, ui); return true;
  });
  w("biomind:finishInput", receipt => bridge.finishInput(receipt));
  w("biomind:control", ({ threadId, ...command }) => bridge.control(threadId, command));
  w("biomind:getControls", async id => {
    const h = bridge.handles.get(id); if (!h) throw new Error("请先选择一个真实 BioMind 会话。");
    const state = await bridge.state(id, h.cwd); if (!state) throw new Error("OWNER_UNAVAILABLE");
    const out = { state };
    for (const [key, type, cap] of [["models", "get_models", "getModels"], ["resources", "get_resources", "resources"]]) {
      const c = state.capabilities?.[cap]; if (c === true || c?.available) out[key] = await bridge.control(id, { type });
    }
    return out;
  });
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { const w = workbenchNeedsReload ? connectionWindow : workbenchWindow || connectionWindow; w?.show(); w?.focus(); });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", event => {
    if (quitFlushed) return;
    event.preventDefault();
    if (quitStarted) return;
    quitStarted = true; quitting = true;
    try { if (workbenchWindow && !workbenchWindow.isDestroyed()) saveConfig({ windowBounds: { ...workbenchWindow.getNormalBounds(), maximized: workbenchWindow.isMaximized() } }); }
    catch { diagnostic({ source: "storage", code: "UI_STATE_SAVE_FAILED" }); }
    bridge?.close(); controller?.disconnect();
    // A preference lock must not trap application exit. Critical admission
    // receipts remain synchronous and are never relaxed by this UI-only flush.
    let timer;
    const timeout = new Promise(resolve => { timer = setTimeout(() => { diagnostic({ source: "storage", code: "UI_FLUSH_TIMEOUT" }); resolve(false); }, 2000); });
    void Promise.race([store ? store.flushUI() : Promise.resolve(true), timeout]).catch(() => false).finally(() => {
      clearTimeout(timer); quitFlushed = true; app.quit();
    });
  });
  app.whenReady().then(async () => {
    const data = app.getPath("userData"); fs.mkdirSync(data, { recursive: true, mode: 0o700 });
    store = new ConnectionStore(data, safeStorage, validateProfile);
    store.onUIWriteError = ({ id, code }) => { diagnostic({ source: "storage", code }); if (id === activeProfile?.id) send("biomind:localUIState", localUIState()); };
    store.onUIWriteRecovered = ({ id }) => { diagnostic({ source: "storage", code: "UI_STATE_RECOVERED" }); if (id === activeProfile?.id) send("biomind:localUIState", localUIState()); };
    controller = new RemoteConnectionController({ onState: state => {
      connectionWindow?.webContents.send("biomind-connection-state", state);
      if (state.status !== "ready") bridge?.detach();
      send("biomind:status", state);
      if (workbenchWindow) workbenchWindow.setTitle(`BioMind Studio — ${state.status === "ready" ? "已连接" : "连接恢复中；发送暂停"}`);
    } });
    const localSession = session.fromPartition("biomind-local-connections", { cache: false });
    localSession.protocol.handle("biomind-connection", request => {
      const url = new URL(request.url), files = { "/index.html": ["remote-connection.html", "text/html"], "/connection.js": ["remote-connection.js", "text/javascript"], "/connection.css": ["remote-connection.css", "text/css"] };
      const file = url.hostname === "app" && files[url.pathname]; if (!file) return new Response("Not found", { status: 404 });
      return new Response(fs.readFileSync(path.join(__dirname, file[0])), { headers: { "Content-Type": file[1], "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'" } });
    });
    Menu.setApplicationMenu(null);
    connectionWindow = new BrowserWindow({ width: 900, height: 860, minWidth: 680, minHeight: 600, title: "BioMind Studio — 连接服务器",
      webPreferences: { partition: "biomind-local-connections", preload: path.join(__dirname, "remote-preload.cjs"), sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true, webviewTag: false, devTools: false } });
    lockdown(connectionWindow, url => url === PAGE);
    connectionWindow.on("close", event => { if (workbenchWindow && !quitting) { event.preventDefault(); connectionWindow.hide(); } });
    local("biomind-connection-list", () => ({ profiles: store.list().map(p => ({ ...p, remembered: store.hasPassword(p.id) })), state: controller.state, lastId: store.last()?.id, secureStorage: store.encryptionAvailable() }));
    local("biomind-connection-save", profile => store.save(profile));
    local("biomind-connection-delete", id => clearConnection(id, true));
    local("biomind-connection-clear-auth", id => clearConnection(id, false));
    local("biomind-connection-connect", connect);
    local("biomind-connection-preset", () => JSON.parse(fs.readFileSync(path.join(ROOT, "server/setup-preset.json"), "utf8")));
    local("biomind-connection-install", async (profile, setup, password) => {
      const value = validateProfile(profile);
      if (connecting || workbenchWindow) throw new Error("INSTALL_REQUIRES_DISCONNECTED");
      const result = await controller.install(value, { ...setup, schema: "BioMindStudioConfigureV1", workbenchPassword: password }, path.join(ROOT, "server"));
      const saved = store.save({ ...value, serverId: result.serverId });
      return { ...result, profile: saved };
    });
    local("biomind-connection-disconnect", () => { bridge?.detach(); controller.disconnect(); });
    local("biomind-connection-select-key", async () => { const r = await dialog.showOpenDialog(connectionWindow, { title: "选择已有 SSH 私钥文件", properties: ["openFile"] }); return r.canceled ? null : r.filePaths[0]; });
    registerWorkspace();
    await connectionWindow.loadURL(PAGE);
    const last = store.last();
    if (last && store.hasPassword(last.id)) {
      try { await connect(last, "", true); }
      catch (e) { connectionWindow.webContents.send("biomind-connection-state", { status: "error", code: e.message }); }
    }
  }).catch(error => {
    try { fs.writeFileSync(path.join(app.getPath("userData"), "startup-error.log"), String(error.stack || error), { mode: 0o600 }); } catch {}
    dialog.showErrorBox("BioMind Studio", "启动失败。请检查用户数据目录中的 startup-error.log。"); app.quit();
  });
}
