"use strict";
const { spawn } = require("node:child_process");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { createHash } = require("node:crypto");

function validateProfile(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("INVALID_CONNECTION");
  const alias = value.alias;
  if (typeof alias !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(alias)) throw new Error("INVALID_SSH_ALIAS");
  const jump = value.jump || "";
  if (typeof jump !== "string" || (jump && !jump.split(",").every(item => /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(item))) || jump.split(",").length > 4) throw new Error("INVALID_PROXY_JUMP");
  const localPort = Number(value.localPort), remotePort = Number(value.remotePort);
  if (!Number.isInteger(localPort) || localPort < 1024 || localPort > 65535 || !Number.isInteger(remotePort) || remotePort < 1 || remotePort > 65535) throw new Error("INVALID_PORT");
  const label = typeof value.label === "string" && value.label.trim() ? value.label.trim() : alias;
  if (label.length > 128 || /[\x00-\x1f]/.test(label)) throw new Error("INVALID_CONNECTION_LABEL");
  const serverId = value.serverId;
  if (serverId !== undefined && (typeof serverId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(serverId))) throw new Error("INVALID_SERVER_ID");
  // Return an allowlist: no password/key material or arbitrary ssh flags persist.
  const id = createHash("sha256").update(JSON.stringify({ alias, jump, localPort, remotePort })).digest("hex").slice(0, 24);
  return { id, label, alias, jump, localPort, remotePort, ...(serverId ? { serverId } : {}) };
}

function sshArguments(input, configPath) {
  const profile = validateProfile(input);
  return [...(configPath ? ["-F", configPath] : []), "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no", "-o", "ControlMaster=no", "-o", "ControlPath=none",
    "-o", "RequestTTY=no", "-o", "GatewayPorts=no", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=15", "-L", `127.0.0.1:${profile.localPort}:127.0.0.1:${profile.remotePort}`,
    ...(profile.jump ? ["-J", profile.jump] : []), profile.alias];
}

function portAvailable(port) {
  return new Promise(resolve => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen({ host: "127.0.0.1", port, exclusive: true }, () => server.close(() => resolve(true)));
  });
}

function probeProfile(origin, password) {
  return new Promise((resolve, reject) => {
    const request = http.get(origin + "/api/agent/profile", { timeout: 1500,
      headers: { Authorization: "Basic " + Buffer.from("pi:" + password).toString("base64") } }, response => {
      let length = 0; const chunks = [];
      response.on("data", chunk => { length += chunk.length; if (length > 65536) response.destroy(new Error("INVALID_SERVER_PROFILE")); else chunks.push(chunk); });
      response.on("error", () => reject(new Error("SERVER_UNAVAILABLE")));
      response.on("end", () => {
        if (response.statusCode === 401) return reject(new Error("SERVER_AUTH_FAILED"));
        if (response.statusCode !== 200) return reject(new Error("SERVER_STARTING"));
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (value.profile !== "biomind" || typeof value.serverId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.serverId)) throw new Error();
          resolve(value);
        } catch { reject(new Error("INVALID_SERVER_PROFILE")); }
      });
    });
    request.on("timeout", () => request.destroy());
    request.on("error", () => reject(new Error("SERVER_UNAVAILABLE")));
  });
}

class RemoteConnectionController {
  #password = "";
  constructor(options = {}) {
    this.spawn = options.spawn || spawn;
    this.probe = options.probe || probeProfile;
    this.portAvailable = options.portAvailable || portAvailable;
    this.delay = options.delay || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    this.onState = options.onState || (() => {});
    this.ssh = options.ssh || (process.platform === "win32" ? path.join(process.env.SystemRoot || "C:/Windows", "System32/OpenSSH/ssh.exe") : "/usr/bin/ssh");
    this.sshConfig = options.sshConfig;
    this.sshCwd = options.sshCwd;
    this.child = null; this.generation = 0; this.state = { status: "disconnected" };
  }
  publish(status, extra = {}) { this.state = { status, ...extra }; this.onState({ ...this.state }); }
  authorization(origin) { return this.state.status === "ready" && origin === this.state.origin ? "Basic " + Buffer.from("pi:" + this.#password).toString("base64") : undefined; }
  async connect(input, password) {
    const profile = validateProfile(input);
    if (typeof password !== "string" || !password || Buffer.byteLength(password) > 4096 || /[\r\n\0]/.test(password)) throw new Error("INVALID_SERVER_PASSWORD");
    this.disconnect(); const generation = ++this.generation;
    this.publish("connecting", { connectionId: profile.id });
    const available = await this.portAvailable(profile.localPort);
    if (generation !== this.generation) throw new Error("CONNECTION_CANCELLED");
    if (!available) { this.publish("error", { code: "LOCAL_PORT_IN_USE" }); throw new Error("LOCAL_PORT_IN_USE"); }
    const origin = `http://127.0.0.1:${profile.localPort}`;
    let sshError = "SSH_CONNECTION_FAILED";
    let child;
    try { child = this.spawn(this.ssh, sshArguments(profile, this.sshConfig), { shell: false, windowsHide: true, cwd: this.sshCwd, stdio: ["ignore", "pipe", "pipe"] }); }
    catch { this.publish("error", { code: "SSH_UNAVAILABLE" }); throw new Error("SSH_UNAVAILABLE"); }
    this.child = child;
    child.stdout?.resume();
    child.stderr?.on("data", data => {
      const text = data.toString("utf8").slice(0, 4096);
      if (/host key verification failed|remote host identification has changed/i.test(text)) sshError = "SSH_HOST_KEY_REJECTED";
      else if (/permission denied/i.test(text)) sshError = "SSH_AUTH_FAILED";
      else if (/address already in use|cannot listen to port/i.test(text)) sshError = "LOCAL_PORT_IN_USE";
      // Never retain or return raw SSH diagnostics, usernames, paths or keys.
    });
    const failed = () => {
      if (generation !== this.generation) return;
      this.child = null; this.#password = "";
      this.publish("error", { code: sshError, connectionId: profile.id });
    };
    child.once("error", failed); child.once("exit", failed);
    try {
      for (let attempt = 0; attempt < 60; attempt++) {
        await this.delay(1000);
        if (generation !== this.generation || this.child !== child) throw new Error(this.state.code || "CONNECTION_CANCELLED");
        try {
          const remote = await this.probe(origin, password);
          if (generation !== this.generation || this.child !== child) throw new Error("CONNECTION_CANCELLED");
          if (profile.serverId && profile.serverId !== remote.serverId) throw new Error("SERVER_ID_CHANGED");
          this.#password = password;
          this.publish("ready", { origin, connectionId: profile.id, serverId: remote.serverId });
          return { ...profile, serverId: remote.serverId };
        } catch (error) {
          if (["SERVER_AUTH_FAILED", "INVALID_SERVER_PROFILE", "SERVER_ID_CHANGED", "CONNECTION_CANCELLED"].includes(error.message)) throw error;
          this.publish("server-starting", { connectionId: profile.id });
        }
      }
      throw new Error("SERVER_READY_TIMEOUT");
    } catch (error) {
      if (generation === this.generation) { this.disconnect(); this.publish("error", { code: error.message, connectionId: profile.id }); }
      throw error;
    }
  }
  disconnect() {
    this.generation++; this.#password = "";
    const child = this.child; this.child = null;
    if (child) child.kill(); // Only the owned local ssh process, never a remote command.
    this.publish("disconnected");
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
module.exports = { validateProfile, sshArguments, RemoteConnectionController, isConnectionPageSender, isRemoteNavigationAllowed };
