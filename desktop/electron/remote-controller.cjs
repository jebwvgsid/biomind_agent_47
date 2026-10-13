"use strict";
// Derived from the verified AACS111 candidate transport. This module never
// creates, stops, or replays agent tasks; it only owns local SSH processes.
const { spawn } = require("node:child_process");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const { createHash } = require("node:crypto");
const { StringDecoder } = require("node:string_decoder");
const { installService } = require("./service-installer.cjs");
const HOST = /^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/;
const USER = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/;
const ID = /^[A-Za-z0-9._:-]{1,128}$/;
const FATAL = new Set(["SERVER_AUTH_FAILED", "INVALID_SERVER_PROFILE", "SERVER_ID_CHANGED", "SSH_HOST_KEY_REJECTED", "SSH_AUTH_FAILED", "SSH_UNAVAILABLE", "SSH_HOST_NOT_FOUND", "SERVICE_NOT_INSTALLED", "INVALID_SERVICE_RESPONSE", "SERVICE_CONFIGURATION_CHANGED", "SERVICE_CONFIGURATION_ERROR", "SERVICE_STOPPED_DURING_START", "SERVICE_FAILED"]);
function port(value, fallback) {
  const result = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isInteger(result) || result < 1 || result > 65535) throw new Error("INVALID_PORT");
  return result;
}
function validateProfile(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("INVALID_CONNECTION");
  const alias = value.alias || "", host = value.host || "", user = value.user || "";
  if (typeof alias !== "string" || typeof host !== "string" || Boolean(alias) === Boolean(host) || alias && !HOST.test(alias) || host && !HOST.test(host)) throw new Error("INVALID_SSH_HOST");
  if (typeof user !== "string" || user && !USER.test(user) || host && !user) throw new Error("INVALID_SSH_USER");
  const jump = value.jump || "";
  if (typeof jump !== "string" || jump && (!jump.split(",").every(item => HOST.test(item)) || jump.split(",").length > 4)) throw new Error("INVALID_PROXY_JUMP");
  const localPort = port(value.localPort, 30142), sshPort = port(value.sshPort, 22);
  if (localPort < 1024) throw new Error("INVALID_PORT");
  const identityFile = value.identityFile || "";
  if (typeof identityFile !== "string" || identityFile.length > 4096 || /[\x00-\x1f\"]/.test(identityFile)) throw new Error("INVALID_IDENTITY_PATH");
  const servicePath = value.servicePath || "", serviceConfig = value.serviceConfig || "";
  const managed = Boolean(servicePath || serviceConfig);
  if (managed && [servicePath, serviceConfig].some(v => typeof v !== "string" || !v.startsWith("/") || v.length > 4096 || /[\x00-\x1f]/.test(v))) throw new Error("INVALID_SERVICE_PATH");
  const remotePort = managed ? undefined : port(value.remotePort);
  const label = typeof value.label === "string" && value.label.trim() ? value.label.trim() : alias || host;
  if (label.length > 128 || /[\x00-\x1f]/.test(label)) throw new Error("INVALID_CONNECTION_LABEL");
  if (value.serverId !== undefined && (typeof value.serverId !== "string" || !ID.test(value.serverId))) throw new Error("INVALID_SERVER_ID");
  // Allowlist: local key paths are settings; private key bytes are never stored.
  const result = { label, alias, host, user, sshPort, identityFile, jump, localPort,
    ...(managed ? { servicePath, serviceConfig } : { remotePort }) };
  const { label: _displayLabel, ...identity } = result;
  const id = createHash("sha256").update(JSON.stringify(identity)).digest("hex").slice(0, 24);
  return { id, ...result, ...(value.serverId ? { serverId: value.serverId } : {}) };
}
const STRICT = ["BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no", "ForwardAgent=no", "ForwardX11=no", "PermitLocalCommand=no", "ControlMaster=no", "ControlPath=none", "RequestTTY=no", "ServerAliveInterval=15", "ServerAliveCountMax=3", "ConnectTimeout=15"];
const shellQuote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
function serviceCommand(profile, action) {
  if (!["ensure", "discover"].includes(action)) throw new Error("INVALID_SERVICE_ACTION");
  return `python3 ${shellQuote(profile.servicePath)} ${action} --config ${shellQuote(profile.serviceConfig)}`;
}
function sshArguments(input, configPath, endpoint, command) {
  const profile = validateProfile(input);
  const args = [...(configPath ? ["-F", configPath] : []), "-T", ...STRICT.flatMap(option => ["-o", option])];
  if (!endpoint?.configTarget) {
    if (profile.user) args.push("-l", profile.user);
    if (profile.host) args.push("-p", String(profile.sshPort));
    if (profile.identityFile) args.push("-i", profile.identityFile);
    if (profile.jump) args.push("-J", profile.jump);
  }
  if (!command) args.push("-N", "-o", "ExitOnForwardFailure=yes", "-o", "GatewayPorts=no", "-L", `127.0.0.1:${profile.localPort}:127.0.0.1:${endpoint?.port || profile.remotePort}`);
  args.push(endpoint?.configTarget || profile.alias || profile.host);
  if (command) args.push(command);
  return args;
}
function configQuote(value) { return '"' + value.replace(/\\/g, "/") + '"'; }
function createSSHConfig(profile, endpoint, includePath) {
  const include = includePath || path.join(os.homedir(), ".ssh", "config");
  if (/[\r\n\0\"]/.test(include)) throw new Error("INVALID_SSH_CONFIG_PATH");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-ssh-"));
  const configPath = path.join(directory, "config");
  const control = profile.alias || "biomind-control-" + profile.id;
  const target = endpoint?.transport === "jump" ? "biomind-service-" + profile.id : control;
  // ProxyJump subprocesses read these same strict options and key paths.
  const lines = ["Host *", ...STRICT.map(option => "  " + option.replace("=", " ")), "  ExitOnForwardFailure yes"];
  lines.push(`Host ${control}`);
  if (profile.host) lines.push(`  HostName ${profile.host}`, `  Port ${profile.sshPort}`);
  if (profile.user) lines.push(`  User ${profile.user}`);
  if (profile.identityFile) lines.push(`  IdentityFile ${configQuote(profile.identityFile)}`);
  if (profile.jump) lines.push(`  ProxyJump ${profile.jump}`);
  if (target !== control) {
    lines.push(`Host ${target}`, `  HostName ${endpoint.host}`, `  User ${endpoint.user || profile.user}`, `  ProxyJump ${control}`);
    if (profile.identityFile) lines.push(`  IdentityFile ${configQuote(profile.identityFile)}`);
  }
  lines.push("Host *", `  Include ${configQuote(include)}`, "");
  fs.writeFileSync(configPath, lines.join("\n"), { mode: 0o600 });
  return { path: configPath, control, target, dispose: () => fs.rmSync(directory, { recursive: true, force: true }) };
}
function validateService(value, profile) {
  if (!value || typeof value !== "object" || !["ready", "pending", "starting", "slow-start", "unhealthy", "stopping", "stopped", "failed", "unknown", "configuration-changed", "error"].includes(value.status)) throw new Error("INVALID_SERVICE_RESPONSE");
  if (value.serverId && (typeof value.serverId !== "string" || !ID.test(value.serverId))) throw new Error("INVALID_SERVICE_RESPONSE");
  if (profile.serverId && value.serverId && profile.serverId !== value.serverId) throw new Error("SERVER_ID_CHANGED");
  if (value.status === "ready") {
    if (!["direct", "jump"].includes(value.transport)) throw new Error("INVALID_SERVICE_RESPONSE");
    port(value.port);
    if (value.transport === "jump" && (typeof value.host !== "string" || !HOST.test(value.host) || !USER.test(value.user || profile.user))) throw new Error("INVALID_SERVICE_RESPONSE");
  }
  return value;
}
function portAvailable(portNumber) {
  return new Promise(resolve => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen({ host: "127.0.0.1", port: portNumber, exclusive: true }, () => server.close(() => resolve(true)));
  });
}
function probeProfile(origin, password) {
  return new Promise((resolve, reject) => {
    const request = http.get(origin + "/api/agent/profile", { timeout: 5000,
      headers: { Authorization: "Basic " + Buffer.from("pi:" + password).toString("base64") } }, response => {
      let length = 0; const chunks = [];
      response.on("data", chunk => { length += chunk.length; if (length > 65536) response.destroy(new Error("INVALID_SERVER_PROFILE")); else chunks.push(chunk); });
      response.on("error", () => reject(new Error("SERVER_UNAVAILABLE")));
      response.on("end", () => {
        if (response.statusCode === 401) return reject(new Error("SERVER_AUTH_FAILED"));
        if (response.statusCode !== 200) return reject(new Error("SERVER_STARTING"));
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (value.profile !== "biomind" || typeof value.serverId !== "string" || !ID.test(value.serverId)) throw new Error();
          resolve(value);
        } catch { reject(new Error("INVALID_SERVER_PROFILE")); }
      });
    });
    request.on("timeout", () => request.destroy());
    request.on("error", () => reject(new Error("SERVER_UNAVAILABLE")));
  });
}
function sshError(text, fallback = "SSH_CONNECTION_FAILED", servicePath) {
  if (/host key verification failed|remote host identification has changed/i.test(text)) return "SSH_HOST_KEY_REJECTED";
  if (/permission denied/i.test(text)) return "SSH_AUTH_FAILED";
  if (/address already in use|cannot listen to port/i.test(text)) return "LOCAL_PORT_IN_USE";
  if (/^ssh: Could not resolve hostname [^\r\n]+:/im.test(text)) return "SSH_HOST_NOT_FOUND";
  if (/^ssh: connect to host [^\r\n]+ port \d+: (?:Connection|Operation) timed out\s*$/im.test(text)) return "SSH_CONNECTION_TIMEOUT";
  if (/^ssh: connect to host [^\r\n]+ port \d+: Connection refused\s*$/im.test(text)) return "SSH_CONNECTION_REFUSED";
  // Only the selected manager script being absent means installation is needed.
  // A missing config, dependency or unrelated file must retain its own error.
  if (servicePath && text.split(/\r?\n/).some(line => {
    const match = /^(?:[^:\r\n]*[\\/])?python(?:\d+(?:\.\d+)*)?(?:\.exe)?: can't open file (.*)$/i.exec(line);
    return match && ["'", '"'].some(quote => match[1] === `${quote}${servicePath}${quote}: [Errno 2] No such file or directory`);
  })) return "SERVICE_NOT_INSTALLED";
  return fallback;
}
function sshDiagnosticReader(initialCode, servicePath) {
  const decoder = new StringDecoder("utf8");
  let text = "", code = initialCode;
  return data => {
    // Bounded, ephemeral diagnostics are classified locally; callers receive
    // only fixed codes. UTF-8 and message fragments can span stderr chunks.
    text = (text + decoder.write(data)).slice(-16384);
    code = sshError(text, code, servicePath);
    return code;
  };
}

class RemoteConnectionController {
  #password = "";
  constructor(options = {}) {
    this.spawn = options.spawn || spawn;
    this.probe = options.probe || probeProfile;
    this.portAvailable = options.portAvailable || portAvailable;
    this.delay = options.delay || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    this.setTimer = options.setTimeout || setTimeout; this.clearTimer = options.clearTimeout || clearTimeout;
    this.onState = options.onState || (() => {}); this.makeSSHConfig = options.createSSHConfig || createSSHConfig;
    this.ssh = options.ssh || (process.platform === "win32" ? path.join(process.env.SystemRoot || "C:/Windows", "System32/OpenSSH/ssh.exe") : "/usr/bin/ssh");
    this.sshConfig = options.sshConfig; this.sshCwd = options.sshCwd;
    this.pollMs = options.pollMs ?? 3000; this.pollAttempts = options.pollAttempts ?? 600;
    this.healthMs = options.healthMs ?? 5000; this.readyAttempts = options.readyAttempts ?? 60;
    this.commandTimeoutMs = options.commandTimeoutMs ?? 45000;
    this.serviceRequest = options.serviceRequest || ((profile, action, generation) => this.runService(profile, action, generation));
    this.child = null; this.commandChildren = new Set(); this.configs = new Set(); this.generation = 0;
    this.state = { status: "disconnected" }; this.timer = null; this.inflight = null; this.profile = null; this.retry = 0;
  }
  publish(status, extra = {}) { this.state = { status, ...extra }; this.onState({ ...this.state }); }
  authorization(origin) { return this.state.status === "ready" && origin === this.state.origin ? "Basic " + Buffer.from("pi:" + this.#password).toString("base64") : undefined; }
  assertCurrent(generation) { if (generation !== this.generation) throw new Error("CONNECTION_CANCELLED"); }
  schedule(callback, ms) { this.clearScheduled(); this.timer = this.setTimer(() => { this.timer = null; callback(); }, ms); this.timer?.unref?.(); }
  clearScheduled() { if (this.timer !== null) this.clearTimer(this.timer); this.timer = null; }
  ownConfig(profile, endpoint) {
    const include = this.sshConfig && (path.isAbsolute(this.sshConfig) ? this.sshConfig : path.resolve(this.sshCwd || process.cwd(), this.sshConfig));
    const config = this.makeSSHConfig(profile, endpoint, include); this.configs.add(config); return config;
  }
  releaseConfig(config) { if (this.configs.delete(config)) config.dispose(); }
  async runService(profile, action, generation) {
    this.assertCurrent(generation);
    const config = this.ownConfig(profile);
    try {
      return await new Promise((resolve, reject) => {
        let child, output = "", errorCode = "SERVICE_COMMAND_FAILED", settled = false;
        const diagnostic = sshDiagnosticReader(errorCode, profile.servicePath);
        const finish = (error, result) => {
          if (settled) return; settled = true; this.clearTimer(timer); this.commandChildren.delete(child);
          if (error) reject(error); else resolve(result);
        };
        const timer = this.setTimer(() => { finish(new Error("SERVICE_COMMAND_TIMEOUT")); child?.kill(); }, this.commandTimeoutMs);
        timer?.unref?.();
        try { child = this.spawn(this.ssh, sshArguments(profile, config.path, { configTarget: config.control }, serviceCommand(profile, action)), { shell: false, windowsHide: true, cwd: this.sshCwd, stdio: ["ignore", "pipe", "pipe"] }); }
        catch { return finish(new Error("SSH_UNAVAILABLE")); }
        this.commandChildren.add(child);
        child.stdout?.on("data", data => { output += data.toString("utf8"); if (Buffer.byteLength(output) > 65536) { finish(new Error("INVALID_SERVICE_RESPONSE")); child.kill(); } });
        child.stderr?.on("data", data => { errorCode = diagnostic(data); });
        child.once("error", () => finish(new Error("SSH_UNAVAILABLE")));
        child.once("close", code => {
          if (generation !== this.generation) return finish(new Error("CONNECTION_CANCELLED"));
          try {
            // A login banner may precede the service's one-line JSON result.
            const line = output.trim().split(/\r?\n/).filter(text => text.trim().startsWith("{")).at(-1);
            const descriptor = validateService(JSON.parse(line), profile);
            if (code !== 0 && !["error", "failed"].includes(descriptor.status)) return finish(new Error(errorCode));
            finish(null, descriptor);
          } catch (error) { finish(new Error(error.message === "SERVER_ID_CHANGED" ? error.message : code !== 0 ? errorCode : "INVALID_SERVICE_RESPONSE")); }
        });
      });
    } finally { this.releaseConfig(config); }
  }
  connect(input, password) {
    if (this.installing) return Promise.reject(new Error("INSTALLATION_IN_PROGRESS"));
    const profile = validateProfile(input);
    if (typeof password !== "string" || !password || Buffer.byteLength(password) > 4096 || /[\r\n\0]/.test(password)) return Promise.reject(new Error("INVALID_SERVER_PASSWORD"));
    if (this.profile?.id === profile.id && this.#password === password) {
      if (this.inflight) return this.inflight;
      if (this.state.status === "ready") return Promise.resolve({ ...this.profile });
    }
    this.disconnect(); const generation = ++this.generation;
    this.profile = profile; this.#password = password; this.retry = 0;
    return this.start(generation, false);
  }
  async install(input, setup, bundledServerDir) {
    const profile = validateProfile(input);
    if (this.installing) throw new Error("INSTALLATION_IN_PROGRESS");
    if (this.inflight || this.state.status === "ready") throw new Error("INSTALL_REQUIRES_DISCONNECTED");
    this.clearScheduled();
    const generation = ++this.generation;
    this.installing = true;
    this.publish("installing", { connectionId: profile.id });
    try {
      const result = await installService(this, profile, setup, bundledServerDir, sshArguments, sshDiagnosticReader);
      this.assertCurrent(generation);
      this.publish("configured", { connectionId: profile.id, serverId: result.serverId });
      return result;
    } catch (error) {
      if (generation === this.generation) this.publish("error", { code: error.message, connectionId: profile.id });
      throw error;
    } finally { this.installing = false; }
  }
  start(generation, reconnecting) {
    const operation = this.establish(generation, reconnecting).catch(error => {
      if (generation === this.generation) {
        this.stopTransport();
        this.publish("error", { code: error.message, ...(error.serviceCode ? { serviceCode: error.serviceCode } : {}), connectionId: this.profile?.id, retrying: reconnecting && !FATAL.has(error.message) });
        if (reconnecting && !FATAL.has(error.message)) this.schedule(() => this.beginReconnect(error.message), Math.min(30000, 1000 * 2 ** Math.min(++this.retry, 5)));
      }
      throw error;
    }).finally(() => { if (this.inflight === operation) this.inflight = null; });
    this.inflight = operation; return operation;
  }
  async establish(generation, reconnecting) {
    const profile = this.profile;
    this.publish(reconnecting ? "reconnecting" : "connecting", { connectionId: profile.id });
    let endpoint = { status: "ready", transport: "direct", port: profile.remotePort }, ensured = false;
    if (profile.servicePath) {
      for (let attempt = 0; attempt < this.pollAttempts; attempt++) {
        this.assertCurrent(generation);
        endpoint = validateService(await this.serviceRequest(profile, "discover", generation), profile);
        this.assertCurrent(generation);
        if (endpoint.status === "stopped" && !ensured) {
          ensured = true;
          endpoint = validateService(await this.serviceRequest(profile, "ensure", generation), profile);
          this.assertCurrent(generation);
        }
        if (endpoint.status === "ready") break;
        if (endpoint.status === "stopped" && ensured) throw new Error("SERVICE_STOPPED_DURING_START");
        if (endpoint.status === "error") {
          const error = new Error("SERVICE_CONFIGURATION_ERROR");
          if (typeof endpoint.code === "string" && /^[A-Z][A-Z0-9_]{1,80}$/.test(endpoint.code)) error.serviceCode = endpoint.code;
          throw error;
        }
        if (endpoint.status === "failed" || endpoint.status === "configuration-changed") throw new Error(endpoint.status === "failed" ? "SERVICE_FAILED" : "SERVICE_CONFIGURATION_CHANGED");
        this.publish(endpoint.status === "pending" ? "service-pending" : "service-starting", {
          connectionId: profile.id, serviceStatus: endpoint.status,
          ...(typeof endpoint.jobId === "string" && /^\d+$/.test(endpoint.jobId) ? { jobId: endpoint.jobId } : {}),
          elapsedSeconds: Math.round(attempt * this.pollMs / 1000)
        });
        await this.delay(this.pollMs);
      }
      if (endpoint.status !== "ready") throw new Error("SERVICE_READY_TIMEOUT");
    }
    this.assertCurrent(generation);
    if (!await this.portAvailable(profile.localPort)) throw new Error("LOCAL_PORT_IN_USE");
    this.assertCurrent(generation);
    const config = this.ownConfig(profile, endpoint);
    let child;
    try { child = this.spawn(this.ssh, sshArguments(profile, config.path, { ...endpoint, configTarget: config.target }), { shell: false, windowsHide: true, cwd: this.sshCwd, stdio: ["ignore", "pipe", "pipe"] }); }
    catch { this.releaseConfig(config); throw new Error("SSH_UNAVAILABLE"); }
    this.child = child; this.transportError = null;
    let code = "SSH_CONNECTION_FAILED";
    const diagnostic = sshDiagnosticReader(code);
    child.stdout?.resume(); child.stderr?.on("data", data => { code = diagnostic(data); });
    const failed = () => {
      this.releaseConfig(config);
      if (generation !== this.generation || this.child !== child) return;
      this.child = null; this.transportError = code;
      if (this.state.status === "ready") this.beginReconnect(code);
    };
    child.once("error", () => { code = "SSH_UNAVAILABLE"; failed(); }); child.once("exit", failed);
    const origin = `http://127.0.0.1:${profile.localPort}`;
    for (let attempt = 0; attempt < this.readyAttempts; attempt++) {
      this.assertCurrent(generation);
      if (this.child !== child) throw new Error(this.transportError || "SSH_CONNECTION_FAILED");
      try {
        const remote = await this.probe(origin, this.#password);
        this.assertCurrent(generation);
        if (this.child !== child) throw new Error(this.transportError || "SSH_CONNECTION_FAILED");
        if ((profile.serverId && profile.serverId !== remote.serverId) || (endpoint.serverId && endpoint.serverId !== remote.serverId)) throw new Error("SERVER_ID_CHANGED");
        this.profile = { ...profile, serverId: remote.serverId }; this.retry = 0;
        this.publish("ready", { origin, connectionId: profile.id, serverId: remote.serverId, ...(endpoint.jobId ? { jobId: String(endpoint.jobId) } : {}) });
        this.schedule(() => this.checkHealth(generation), this.healthMs);
        return { ...this.profile };
      } catch (error) {
        this.assertCurrent(generation);
        if (FATAL.has(error.message)) throw error;
        this.publish("server-starting", { connectionId: profile.id, code: error.message });
        await this.delay(1000);
      }
    }
    throw new Error("SERVER_READY_TIMEOUT");
  }
  async checkHealth(generation) {
    if (generation !== this.generation || this.state.status !== "ready") return;
    try {
      const remote = await this.probe(this.state.origin, this.#password);
      this.assertCurrent(generation);
      if (remote.serverId !== this.profile.serverId) throw new Error("SERVER_ID_CHANGED");
      this.schedule(() => this.checkHealth(generation), this.healthMs);
    } catch (error) {
      if (generation !== this.generation) return;
      this.beginReconnect(error.message);
    }
  }
  beginReconnect(code) {
    this.clearScheduled();
    if (!this.profile || !this.#password) return;
    this.stopTransport();
    if (FATAL.has(code)) { this.publish("error", { code, connectionId: this.profile.id }); return; }
    const generation = ++this.generation;
    this.start(generation, true).catch(() => {});
  }
  stopTransport() {
    const child = this.child; this.child = null;
    if (child) child.kill(); // No remote termination, scancel, or task abort.
    for (const config of [...this.configs]) this.releaseConfig(config);
  }
  disconnect() {
    this.generation++; this.clearScheduled(); this.#password = ""; this.profile = null; this.inflight = null;
    this.stopTransport();
    for (const child of this.commandChildren) child.kill();
    this.commandChildren.clear(); this.publish("disconnected");
  }
}
function isConnectionPageSender(event, window, pageUrl) {
  return Boolean(window && !window.isDestroyed() && event.sender === window.webContents
    && event.senderFrame === window.webContents.mainFrame && event.senderFrame?.url === pageUrl);
}
function isRemoteNavigationAllowed(raw, origin) {
  try { const url = new URL(raw); return url.origin === origin && !url.username && !url.password
    && (!url.pathname.startsWith("/api/") || url.pathname.startsWith("/api/files/") && url.searchParams.get("type") === "download"); }
  catch { return false; }
}
module.exports = { validateProfile, sshArguments, serviceCommand, createSSHConfig, validateService, probeProfile, RemoteConnectionController, isConnectionPageSender, isRemoteNavigationAllowed };
