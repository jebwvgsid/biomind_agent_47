"use strict";
// Deliberately separate from the local Studio boot: no Next, CDP or browser bridge.
const { app, BrowserWindow, dialog, ipcMain, Menu, protocol, session } = require("electron/main");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { RemoteConnectionController, validateProfile, isConnectionPageSender, isRemoteNavigationAllowed } = require("./remote-controller.cjs");
const PAGE = "biomind-connection://app/index.html";
protocol.registerSchemesAsPrivileged([{ scheme: "biomind-connection", privileges: { standard: true, secure: true, supportFetchAPI: false } }]);
app.setName("BioMind Remote");
app.setPath("userData", path.join(app.getPath("appData"), "BioMind Remote"));
app.enableSandbox();
// Ignore inherited remote-debugging flags for this restricted profile.
app.commandLine.removeSwitch("remote-debugging-port");
app.commandLine.removeSwitch("remote-debugging-pipe");
let connectionWindow, remoteWindow, controller;
let recordsPath, sshConfigPath;
const securedSessions = new WeakSet();
function saved() {
  try {
    const value = JSON.parse(fs.readFileSync(recordsPath, "utf8"));
    return Array.isArray(value) ? value.slice(0, 20).map(validateProfile) : [];
  } catch { return []; }
}
function save(value) {
  const profile = validateProfile(value), records = saved().filter(item => item.id !== profile.id);
  records.unshift(profile);
  fs.writeFileSync(recordsPath + ".tmp", JSON.stringify(records.slice(0, 20), null, 2), { mode: 0o600 });
  fs.renameSync(recordsPath + ".tmp", recordsPath);
  return profile;
}
async function clearConnection(id, remove) {
  if (typeof id !== "string" || !/^[a-f0-9]{24}$/.test(id)) throw new Error("INVALID_CONNECTION_ID");
  if (controller.state.connectionId === id) {
    controller.disconnect();
    const window = remoteWindow; remoteWindow = undefined;
    if (window && !window.isDestroyed()) window.destroy();
  }
  const isolated = session.fromPartition(`persist:biomind-${id}`, { cache: false });
  await isolated.clearAuthCache();
  await isolated.clearCache();
  // Deliberately preserve localStorage/IndexedDB drafts. This is not revocation.
  if (remove) {
    fs.writeFileSync(recordsPath + ".tmp", JSON.stringify(saved().filter(item => item.id !== id), null, 2), { mode: 0o600 });
    fs.renameSync(recordsPath + ".tmp", recordsPath);
  }
}
function assertLocal(event) {
  if (!isConnectionPageSender(event, connectionWindow, PAGE)) throw new Error("UNTRUSTED_IPC_SENDER");
}
function register(name, callback) {
  ipcMain.handle(name, async (event, ...args) => {
    assertLocal(event);
    try { return { ok: true, value: await callback(...args) }; }
    catch (error) { return { ok: false, code: /^[A-Z_]+$/.test(error.message) ? error.message : "CONNECTION_FAILED" }; }
  });
}
function denyPermissions(ses) {
  if (securedSessions.has(ses)) return;
  securedSessions.add(ses);
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.on("will-download", (_event, item) => {
    item.setSaveDialogOptions({ title: "保存服务器文件 / Save server file" });
  });
}
function lockdown(contents, permitted) {
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.on("will-navigate", (event, url) => { if (!permitted(url)) event.preventDefault(); });
  contents.on("will-redirect", (event, url) => { if (!permitted(url)) event.preventDefault(); });
  contents.on("will-attach-webview", event => event.preventDefault());
}
async function openRemote(profile, generation) {
  if (controller.generation !== generation || controller.state.status !== "ready" || controller.state.connectionId !== profile.id) throw new Error("CONNECTION_CANCELLED");
  const previous = remoteWindow; remoteWindow = undefined;
  if (previous && !previous.isDestroyed()) previous.destroy();
  const origin = controller.state.origin;
  // The partition is selected before window creation/navigation; never defaultSession.
  const partition = `persist:biomind-${profile.id}`;
  const isolated = session.fromPartition(partition, { cache: false });
  denyPermissions(isolated);
  isolated.webRequest.onBeforeRequest((details, done) => {
    try {
      const url = new URL(details.url);
      const media = ["image", "media", "font"].includes(details.resourceType) && ["data:", "blob:"].includes(url.protocol);
      done({ cancel: !media && (controller.generation !== generation || controller.state.status !== "ready" || controller.state.connectionId !== profile.id || url.origin !== origin) });
    } catch { done({ cancel: true }); }
  });
  isolated.webRequest.onBeforeSendHeaders((details, done) => {
    const headers = { ...details.requestHeaders };
    for (const key of Object.keys(headers)) if (key.toLowerCase() === "authorization") delete headers[key];
    let auth; try { if (controller.generation === generation) auth = controller.authorization(new URL(details.url).origin); } catch { /* invalid URL */ }
    if (auth) headers.Authorization = auth;
    done({ requestHeaders: headers });
  });
  remoteWindow = new BrowserWindow({ width: 1440, height: 900, minWidth: 900, minHeight: 600,
    title: `BioMind — ${profile.label}`, autoHideMenuBar: true,
    webPreferences: { partition, sandbox: true, contextIsolation: true, nodeIntegration: false,
      nodeIntegrationInSubFrames: false, nodeIntegrationInWorker: false, webSecurity: true,
      allowRunningInsecureContent: false, webviewTag: false, devTools: false } });
  const window = remoteWindow;
  lockdown(window.webContents, url => isRemoteNavigationAllowed(url, origin));
  window.on("closed", () => {
    if (remoteWindow !== window) return;
    remoteWindow = undefined;
    controller.disconnect(); void isolated.clearAuthCache();
    if (connectionWindow && !connectionWindow.isDestroyed()) connectionWindow.show();
  });
  await window.loadURL(origin + "/");
  if (controller.generation !== generation || controller.state.status !== "ready") {
    if (remoteWindow === window) remoteWindow = undefined;
    if (!window.isDestroyed()) window.destroy();
    throw new Error("CONNECTION_CANCELLED");
  }
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { connectionWindow?.show(); connectionWindow?.focus(); });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => controller?.disconnect());
  app.whenReady().then(async () => {
    const userData = app.getPath("userData"); fs.mkdirSync(userData, { recursive: true });
    recordsPath = path.join(userData, "connections.json");
    sshConfigPath = path.join(userData, "strict-ssh.conf");
    const quote = value => '"' + value.replace(/\\/g, "/").replace(/"/g, '\\"') + '"';
    const systemConfig = process.platform === "win32" ? path.join(process.env.ProgramData || "C:/ProgramData", "ssh/ssh_config") : "/etc/ssh/ssh_config";
    // -F is also used by OpenSSH's ProxyJump subprocess: strict policy precedes
    // the operator's existing aliases/keys/ProxyJump settings on every hop.
    fs.writeFileSync(sshConfigPath, "Host *\n  StrictHostKeyChecking yes\n  UpdateHostKeys no\n  CheckHostIP no\n  VerifyHostKeyDNS no\n  ForwardAgent no\n  ForwardX11 no\n  PermitLocalCommand no\n  BatchMode yes\n  ControlMaster no\n  ControlPath none\n  ControlPersist no\n  ForkAfterAuthentication no\n  AddKeysToAgent no\nInclude "
      + quote(path.join(os.homedir(), ".ssh/config")) + "\nInclude " + quote(systemConfig) + "\n", { mode: 0o600 });
    // OpenSSH interpolates -F into its ProxyJump command. A fixed relative
    // filename avoids shell interpretation of spaces in Windows userData paths.
    controller = new RemoteConnectionController({ sshConfig: "strict-ssh.conf", sshCwd: userData, onState: state => {
      if (connectionWindow && !connectionWindow.isDestroyed()) connectionWindow.webContents.send("biomind-connection-state", state);
      if (state.status === "error" && remoteWindow && !remoteWindow.isDestroyed()) remoteWindow.setTitle("BioMind — 连接中断；远端状态待核对 / Disconnected");
    } });
    const connectionPartition = "biomind-local-connections";
    const connectionSession = session.fromPartition(connectionPartition, { cache: false });
    connectionSession.protocol.handle("biomind-connection", request => {
      const url = new URL(request.url);
      const files = { "/index.html": ["remote-connection.html", "text/html"], "/connection.js": ["remote-connection.js", "text/javascript"], "/connection.css": ["remote-connection.css", "text/css"] };
      const file = url.hostname === "app" && files[url.pathname];
      if (!file) return new Response("Not found", { status: 404 });
      return new Response(fs.readFileSync(path.join(__dirname, file[0])), { headers: { "Content-Type": file[1],
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" } });
    });
    Menu.setApplicationMenu(null);
    denyPermissions(connectionSession);
    connectionWindow = new BrowserWindow({ width: 860, height: 760, minWidth: 640, minHeight: 600,
      title: "BioMind — 服务器连接", autoHideMenuBar: true,
      webPreferences: { partition: connectionPartition, preload: path.join(__dirname, "remote-preload.cjs"),
        sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true, webviewTag: false, devTools: false } });
    lockdown(connectionWindow.webContents, url => url === PAGE);
    register("biomind-connection-list", () => ({ profiles: saved(), state: controller.state }));
    register("biomind-connection-save", profile => save(profile));
    register("biomind-connection-delete", id => clearConnection(id, true));
    register("biomind-connection-clear-auth", id => clearConnection(id, false));
    register("biomind-connection-connect", async (profile, password) => {
      const pending = controller.connect(profile, password), generation = controller.generation;
      const connected = await pending;
      if (controller.generation !== generation) throw new Error("CONNECTION_CANCELLED");
      save(connected); await openRemote(connected, generation); return connected;
    });
    register("biomind-connection-disconnect", async () => {
      const id = controller.state.connectionId;
      if (id) await clearConnection(id, false);
      else {
        controller.disconnect();
        const window = remoteWindow; remoteWindow = undefined;
        if (window && !window.isDestroyed()) window.destroy();
      }
    });
    await connectionWindow.loadURL(PAGE);
  }).catch(error => {
    const errorPath = path.join(app.getPath("userData"), "startup-error.log");
    try { fs.writeFileSync(errorPath, String(error?.stack || error), { mode: 0o600 }); } catch { /* logging unavailable */ }
    dialog.showErrorBox("BioMind Studio startup failed", "Connection window could not initialize. Diagnostic log: " + errorPath);
    app.quit();
  });
}
