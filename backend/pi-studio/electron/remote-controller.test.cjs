"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { validateProfile, sshArguments, RemoteConnectionController, isConnectionPageSender, isRemoteNavigationAllowed } = require("./remote-controller.cjs");
const profile = { label: "Synthetic server", alias: "fixture-host", jump: "fixture-jump", localPort: 30142, remotePort: 30141 };
function harness(overrides = {}) {
  const children = [], calls = [], states = [];
  const controller = new RemoteConnectionController({ sshConfig: "/synthetic/strict.conf", portAvailable: async () => true,
    delay: async () => {}, probe: async () => ({ profile: "biomind", serverId: "fixture-server" }), onState: state => states.push(state),
    spawn: (command, args, options) => {
      const child = new EventEmitter(); child.stdout = { resume() {} }; child.stderr = new EventEmitter(); child.kills = 0;
      child.kill = () => { child.kills++; child.emit("exit", 0); return true; };
      children.push(child); calls.push({ command, args, options }); return child;
    }, ...overrides });
  return { controller, children, calls, states };
}
test("remote SSH argv is shell-free, strict and only forwards loopback with aliases", () => {
  const args = sshArguments(profile, "/synthetic/strict.conf");
  assert.deepEqual(args.slice(0, 2), ["-F", "/synthetic/strict.conf"]);
  for (const value of ["-N", "-T", "BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no", "PermitLocalCommand=no", "ExitOnForwardFailure=yes", "UpdateHostKeys=no"]) assert(args.includes(value));
  assert.equal(args[args.indexOf("-L") + 1], "127.0.0.1:30142:127.0.0.1:30141");
  assert.equal(args[args.indexOf("-J") + 1], "fixture-jump"); assert.equal(args.at(-1), "fixture-host");
  for (const alias of ["-oProxyCommand=evil", "a;evil", "$(evil)", "a b", "a\nb", "user@host", "a/host"]) assert.throws(() => validateProfile({ ...profile, alias }));
  assert.throws(() => validateProfile({ ...profile, jump: "good,-oevil" }));
  for (const localPort of [0, 22, 65536, 1.5, NaN]) assert.throws(() => validateProfile({ ...profile, localPort }));
  const clean = validateProfile({ ...profile, password: "never-persist", privateKey: "never-persist", args: ["bad"] });
  assert(!JSON.stringify(clean).includes("never-persist")); assert.equal("args" in clean, false);
});
test("remote connect checks identity and disconnect kills only its local transport without remote commands", async () => {
  const h = harness(); const connected = await h.controller.connect(profile, "synthetic-password");
  assert.equal(connected.serverId, "fixture-server"); assert.equal(h.controller.state.status, "ready");
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].options.shell, false);
  assert(h.controller.authorization("http://127.0.0.1:30142").startsWith("Basic "));
  assert.equal(h.controller.authorization("https://evil.invalid"), undefined);
  assert(!JSON.stringify(h.states).includes("synthetic-password"));
  h.controller.disconnect(); assert.equal(h.children[0].kills, 1); assert.equal(h.calls.length, 1);
  assert.equal(h.controller.authorization("http://127.0.0.1:30142"), undefined);
});
test("changed server identity, failed auth and occupied local port fail closed", async () => {
  const mismatch = harness();
  await assert.rejects(mismatch.controller.connect({ ...profile, serverId: "previous-server" }, "synthetic"), /SERVER_ID_CHANGED/);
  assert.equal(mismatch.children[0].kills, 1); assert.equal(mismatch.controller.state.status, "error");
  const auth = harness({ probe: async () => { throw new Error("SERVER_AUTH_FAILED"); } });
  await assert.rejects(auth.controller.connect(profile, "synthetic"), /SERVER_AUTH_FAILED/);
  assert.equal(auth.children[0].kills, 1);
  const occupied = harness({ portAvailable: async () => false });
  await assert.rejects(occupied.controller.connect(profile, "synthetic"), /LOCAL_PORT_IN_USE/); assert.equal(occupied.calls.length, 0);
});
test("disconnect during readiness cannot revive a cancelled connection", async () => {
  let release, entered;
  const seen = new Promise(resolve => { entered = resolve; });
  const h = harness({ probe: async () => { entered(); return new Promise(resolve => { release = resolve; }); } });
  const pending = h.controller.connect(profile, "synthetic"); await seen;
  h.controller.disconnect(); release({ profile: "biomind", serverId: "fixture-server" });
  await assert.rejects(pending, /CONNECTION_CANCELLED/); assert.equal(h.controller.state.status, "disconnected");
  assert.equal(h.children[0].kills, 1);
});
test("connection IPC requires the exact trusted main frame and navigation has one origin", () => {
  const url = "biomind-connection://app/index.html";
  const frame = { url }, contents = { mainFrame: frame }, window = { isDestroyed: () => false, webContents: contents };
  assert.equal(isConnectionPageSender({ sender: contents, senderFrame: frame }, window, url), true);
  assert.equal(isConnectionPageSender({ sender: contents, senderFrame: { url } }, window, url), false);
  assert.equal(isConnectionPageSender({ sender: {}, senderFrame: frame }, window, url), false);
  frame.url = "http://127.0.0.1:30142/";
  assert.equal(isConnectionPageSender({ sender: contents, senderFrame: frame }, window, url), false);
  const origin = "http://127.0.0.1:30142";
  assert.equal(isRemoteNavigationAllowed(origin + "/", origin), true);
  assert.equal(isRemoteNavigationAllowed(origin + "/api/files/tmp/result?type=download", origin), true);
  for (const value of ["https://evil.invalid", "file:///etc/passwd", "javascript:alert(1)", origin + "/api/terminal", "http://x@127.0.0.1:30142/"]) assert.equal(isRemoteNavigationAllowed(value, origin), false);
});
test("remote entry precedes legacy initialization and remote content receives no preload or node bridge", () => {
  const entry = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  assert(entry.indexOf('require("./remote-main.cjs")') < entry.indexOf('require("electron")'));
  const source = fs.readFileSync(path.join(__dirname, "remote-main.cjs"), "utf8");
  assert(!/startServer\(|startBridge\(|spawnSync\(/.test(source));
  const remote = source.slice(source.indexOf("remoteWindow = new BrowserWindow"), source.indexOf("const window = remoteWindow", source.indexOf("remoteWindow = new BrowserWindow")));
  assert(!remote.includes("preload:"));
  for (const flag of ["partition", "sandbox: true", "contextIsolation: true", "nodeIntegration: false", "webviewTag: false", "webSecurity: true"]) assert(remote.includes(flag));
  assert(source.includes('removeSwitch("remote-debugging-port")'));
});
