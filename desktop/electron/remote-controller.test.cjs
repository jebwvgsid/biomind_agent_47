"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { validateProfile, sshArguments, serviceCommand, createSSHConfig, validateService, RemoteConnectionController, isConnectionPageSender, isRemoteNavigationAllowed } = require("./remote-controller.cjs");
const manual = { label: "Synthetic server", alias: "fixture-host", jump: "fixture-jump", localPort: 30142, remotePort: 30141 };
const managed = { label: "Synthetic cluster", host: "login.fixture.invalid", user: "fixture", sshPort: 2222, localPort: 30142, servicePath: "/synthetic/server-service.py", serviceConfig: "/synthetic/service.json" };
const ready = { status: "ready", transport: "jump", host: "dynamic-node-17", user: "fixture", port: 42931, serverId: "fixture-server", jobId: "7123" };
function harness(t, overrides = {}) {
  const children = [], calls = [], states = [], timers = new Map(); let timerId = 0;
  const controller = new RemoteConnectionController({ portAvailable: async () => true, delay: async () => {}, pollAttempts: 4, readyAttempts: 3,
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    probe: async () => ({ profile: "biomind", serverId: "fixture-server" }), onState: state => states.push(state),
    spawn: (command, args, options) => {
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stdout.resume = () => {}; child.stderr = new EventEmitter(); child.kills = 0;
      child.kill = () => { child.kills++; child.emit("exit", 0); child.emit("close", 0); return true; };
      children.push(child); calls.push({ command, args, options }); return child;
    }, ...overrides });
  t.after(() => controller.disconnect());
  return { controller, children, calls, states, timers };
}
test("SSH profile is allowlisted, explicit host/user works, shell/option injection is rejected", () => {
  const clean = validateProfile({ ...managed, identityFile: "C:\\Keys\\Fixture Key", password: "SECRET", privateKey: "SECRET", args: ["bad"] });
  assert.equal(clean.host, managed.host); assert.equal(clean.user, "fixture"); assert.equal(clean.sshPort, 2222);
  assert.equal(validateProfile(managed).id, validateProfile({ ...managed, label: "Renamed connection" }).id);
  assert(!JSON.stringify(clean).includes("SECRET")); assert.equal("args" in clean, false);
  for (const host of ["-oProxyCommand=evil", "a;evil", "$(evil)", "a b", "a\nb", "user@host", 123]) assert.throws(() => validateProfile({ ...managed, host }));
  assert.throws(() => validateProfile({ ...managed, user: "" }));
  assert.throws(() => validateProfile({ ...manual, jump: "good,-oevil" }));
  assert.throws(() => validateProfile({ ...managed, servicePath: "relative/script.py" }));
  assert.throws(() => validateProfile({ ...managed, identityFile: "key\nProxyCommand bad" }));
  for (const localPort of [0, 22, 65536, 1.5, NaN]) assert.throws(() => validateProfile({ ...manual, localPort }));
});
test("legacy tunnel retains strict flags and loopback forwarding", () => {
  const args = sshArguments(manual, "/synthetic/strict.conf");
  assert.deepEqual(args.slice(0, 2), ["-F", "/synthetic/strict.conf"]);
  for (const value of ["-N", "-T", "BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no", "PermitLocalCommand=no", "ExitOnForwardFailure=yes", "UpdateHostKeys=no"]) assert(args.includes(value));
  assert.equal(args[args.indexOf("-L") + 1], "127.0.0.1:30142:127.0.0.1:30141");
  assert.equal(args[args.indexOf("-J") + 1], "fixture-jump"); assert.equal(args.at(-1), "fixture-host");
});
test("service paths survive real POSIX shell parsing with spaces, quotes and substitution characters", t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-quote-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const servicePath = path.join(directory, "中文 ' $(echo altered).py");
  fs.writeFileSync(servicePath, "import sys, json\nprint(json.dumps(sys.argv[1:]))\n");
  const profile = validateProfile({ ...managed, servicePath, serviceConfig: "/literal ' $(echo altered)/配置.json" });
  const actual = JSON.parse(execFileSync("/bin/sh", ["-c", serviceCommand(profile, "ensure")], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
  assert.deepEqual(actual, ["ensure", "--config", profile.serviceConfig]);
  assert.throws(() => serviceCommand(profile, "stop"));
});
test("OpenSSH parses generated two-hop config, including separate host, user, port and key", t => {
  const profile = validateProfile({ ...managed, identityFile: "C:\\Keys\\Fixture Key" });
  const config = createSSHConfig(profile, ready, "/nonexistent/fixture.conf");
  t.after(config.dispose);
  const effective = target => execFileSync("/usr/bin/ssh", ["-G", "-F", config.path, target], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  const control = effective(config.control), tunnel = effective(config.target);
  assert.match(control, /hostname login\.fixture\.invalid/); assert.match(control, /port 2222/);
  assert.match(tunnel, /hostname dynamic-node-17/); assert.match(tunnel, /user fixture/);
  assert.match(tunnel, new RegExp("proxyjump " + config.control));
  for (const output of [control, tunnel]) { assert.match(output, /stricthostkeychecking true/); assert.match(output, /batchmode yes/); assert.match(output, /identityfile C:\/Keys\/Fixture Key/); }
});
test("direct descriptor does not infer a jump from an alias/hostname mismatch", t => {
  const config = createSSHConfig(validateProfile(manual), { status: "ready", transport: "direct", host: "actual-login-fqdn", port: 12345 }, "/nonexistent/fixture.conf");
  t.after(config.dispose);
  assert.equal(config.target, manual.alias);
  assert(!fs.readFileSync(config.path, "utf8").includes("actual-login-fqdn"));
});
test("scheduler discover -> ensure once -> pending -> ready uses the discovered endpoint", async t => {
  const actions = [], responses = [{ status: "stopped" }, { status: "pending", jobId: "7123" }, { status: "pending", jobId: "7123" }, ready];
  const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return responses.shift(); } });
  const connected = await h.controller.connect(managed, "synthetic-password");
  assert.deepEqual(actions, ["discover", "ensure", "discover", "discover"]);
  assert.equal(connected.serverId, "fixture-server"); assert.equal(h.controller.state.status, "ready");
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].options.shell, false);
  assert.equal(h.calls[0].args[h.calls[0].args.indexOf("-L") + 1], "127.0.0.1:30142:127.0.0.1:42931");
  assert(h.states.some(s => s.status === "service-pending"));
  assert(!JSON.stringify(h.states).includes("synthetic-password"));
  assert(h.controller.authorization("http://127.0.0.1:30142").startsWith("Basic "));
  assert.equal(h.controller.authorization("https://evil.invalid"), undefined);
  h.controller.disconnect(); assert.equal(h.children[0].kills, 1); assert.equal(h.calls.length, 1);
});
test("already-running service and repeated connect do not submit, tunnel twice or change identity", async t => {
  const actions = [];
  const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return ready; } });
  const first = h.controller.connect(managed, "synthetic");
  assert.equal(h.controller.connect(managed, "synthetic"), first);
  const saved = await first;
  await h.controller.connect(saved, "synthetic");
  assert.deepEqual(actions, ["discover"]); assert.equal(h.calls.length, 1);
});
test("slow or unknown service never reports ready and never submits another job", async t => {
  for (const status of ["pending", "slow-start", "unknown", "unhealthy", "stopping"]) {
    const actions = []; const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return { status }; } });
    await assert.rejects(h.controller.connect(managed, "synthetic"), /SERVICE_READY_TIMEOUT/);
    assert(actions.every(action => action === "discover"));
    assert(!h.states.some(s => s.status === "ready")); assert.equal(h.calls.length, 0);
  }
});
test("a newly started service that exits fails immediately without a second submission", async t => {
  const actions = [], descriptors = [{ status: "stopped" }, { status: "starting" }, { status: "stopped" }];
  const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return descriptors.shift(); } });
  await assert.rejects(h.controller.connect(managed, "synthetic"), /SERVICE_STOPPED_DURING_START/);
  assert.deepEqual(actions, ["discover", "ensure", "discover"]);
  assert.equal(h.controller.state.status, "error"); assert.equal(h.controller.state.code, "SERVICE_STOPPED_DURING_START");
  assert(!h.states.some(s => s.status === "ready" || s.serviceStatus === "stopped"));
  assert.equal(h.calls.length, 0);
});
test("failed service and changed config fail truthfully without automatic replacement", async t => {
  for (const [status, code] of [["failed", "SERVICE_FAILED"], ["configuration-changed", "SERVICE_CONFIGURATION_CHANGED"]]) {
    const actions = []; const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return { status }; } });
    await assert.rejects(h.controller.connect(managed, "synthetic"), new RegExp(code));
    assert.deepEqual(actions, ["discover"]); assert.equal(h.controller.state.status, "error");
  }
});
test("identity mismatch, failed auth and occupied port fail closed", async t => {
  const mismatch = harness(t);
  await assert.rejects(mismatch.controller.connect({ ...manual, serverId: "previous-server" }, "synthetic"), /SERVER_ID_CHANGED/);
  assert.equal(mismatch.children[0].kills, 1);
  const auth = harness(t, { probe: async () => { throw new Error("SERVER_AUTH_FAILED"); } });
  await assert.rejects(auth.controller.connect(manual, "synthetic"), /SERVER_AUTH_FAILED/);
  assert.equal(auth.children[0].kills, 1);
  const occupied = harness(t, { portAvailable: async () => false });
  await assert.rejects(occupied.controller.connect(manual, "synthetic"), /LOCAL_PORT_IN_USE/); assert.equal(occupied.calls.length, 0);
});
test("disconnect during slow probe cannot revive the cancelled connection", async t => {
  let release, entered;
  const seen = new Promise(resolve => { entered = resolve; });
  const h = harness(t, { probe: async () => { entered(); return new Promise(resolve => { release = resolve; }); } });
  const pending = h.controller.connect(manual, "synthetic"); await seen;
  h.controller.disconnect(); release({ profile: "biomind", serverId: "fixture-server" });
  await assert.rejects(pending, /CONNECTION_CANCELLED/); assert.equal(h.controller.state.status, "disconnected");
  assert.equal(h.children[0].kills, 1);
});
test("health failure rediscovers original service and reconnects without ensure or task replay", async t => {
  const actions = [], probes = []; let fail = false;
  const h = harness(t, { serviceRequest: async (_, action) => { actions.push(action); return ready; },
    probe: async (origin, password) => { probes.push({ origin, password }); if (fail) { fail = false; throw new Error("SERVER_UNAVAILABLE"); } return { profile: "biomind", serverId: "fixture-server" }; } });
  await h.controller.connect(managed, "synthetic"); fail = true;
  await h.controller.checkHealth(h.controller.generation); await h.controller.inflight;
  assert.deepEqual(actions, ["discover", "discover"]); assert.equal(h.calls.length, 2);
  assert.equal(h.children[0].kills, 1); assert.equal(h.controller.state.serverId, "fixture-server");
  assert(h.states.some(s => s.status === "reconnecting")); assert.equal(h.controller.state.status, "ready");
  assert.equal(probes.length, 3); assert.equal(h.calls.some(call => call.args.some(arg => /prompt|abort|scancel/.test(arg))), false);
});
test("server identity change during health check requires action instead of trusting replacement", async t => {
  let changed = false;
  const h = harness(t, { probe: async () => ({ profile: "biomind", serverId: changed ? "other-server" : "fixture-server" }) });
  await h.controller.connect(manual, "synthetic"); changed = true;
  await h.controller.checkHealth(h.controller.generation);
  assert.equal(h.controller.state.status, "error"); assert.equal(h.controller.state.code, "SERVER_ID_CHANGED");
  assert.equal(h.timers.size, 0); assert.equal(h.children[0].kills, 1);
});
test("SSH service runner tolerates banner, bounds data, and does not expose stderr diagnostics", async t => {
  const h = harness(t); const profile = validateProfile(managed);
  const operation = h.controller.runService(profile, "discover", h.controller.generation);
  const child = h.children[0];
  child.stdout.emit("data", Buffer.from("Synthetic login banner\n" + JSON.stringify(ready) + "\n")); child.emit("close", 0);
  assert.deepEqual(await operation, ready);
  const rejected = h.controller.runService(profile, "ensure", h.controller.generation);
  h.children[1].stderr.emit("data", Buffer.from("Permission denied for SECRET user and private key path")); h.children[1].emit("close", 255);
  await assert.rejects(rejected, /SSH_AUTH_FAILED/); assert(!JSON.stringify(h.states).includes("SECRET"));
  const oversized = h.controller.runService(profile, "discover", h.controller.generation);
  h.children[2].stdout.emit("data", Buffer.alloc(65537, 120));
  await assert.rejects(oversized, /INVALID_SERVICE_RESPONSE/); assert.equal(h.children[2].kills, 1);
});
test("managed connect publishes sanitized DNS, timeout and refusal codes", async t => {
  for (const [diagnostic, code] of [
    ["ssh: Could not resolve hostname fixture-host: No such host is known.\r\n", "SSH_HOST_NOT_FOUND"],
    ["ssh: connect to host login.fixture.invalid port 2222: Connection timed out\r\n", "SSH_CONNECTION_TIMEOUT"],
    ["ssh: connect to host login.fixture.invalid port 2222: Connection refused\n", "SSH_CONNECTION_REFUSED"],
  ]) {
    const h = harness(t), pending = h.controller.connect(managed, "SYNTHETIC_SECRET");
    const bytes = Buffer.from("private diagnostic SYNTHETIC_SECRET\n" + diagnostic);
    for (let i = 0; i < bytes.length; i += 7) h.children[0].stderr.emit("data", bytes.subarray(i, i + 7));
    h.children[0].emit("close", 255);
    await assert.rejects(pending, error => error.message === code);
    assert.equal(h.controller.state.status, "error"); assert.equal(h.controller.state.code, code);
    assert.equal(h.controller.state.retrying, false); assert.equal(h.timers.size, 0);
    assert(!JSON.stringify(h.states).includes("SYNTHETIC_SECRET"));
    assert(!JSON.stringify(h.states).includes("private diagnostic"));
    if (code === "SSH_HOST_NOT_FOUND") {
      h.controller.beginReconnect(code);
      assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
      assert.equal(h.controller.state.code, code);
    }
  }
});
test("only the exact missing Python service manager is fatal not-installed, including fragmented UTF-8", async t => {
  const profile = { ...managed, servicePath: "/synthetic/研究 项目/server-service.py" };
  const h = harness(t), pending = h.controller.connect(profile, "SYNTHETIC_SECRET");
  const bytes = Buffer.from(`python3: can't open file '${profile.servicePath}': [Errno 2] No such file or directory\n`);
  for (const byte of bytes) h.children[0].stderr.emit("data", Buffer.from([byte]));
  h.children[0].emit("close", 2);
  await assert.rejects(pending, error => error.message === "SERVICE_NOT_INSTALLED");
  assert.equal(h.controller.state.code, "SERVICE_NOT_INSTALLED");
  assert.equal(h.controller.state.retrying, false);
  h.controller.beginReconnect("SERVICE_NOT_INSTALLED");
  assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
  assert(!JSON.stringify(h.states).includes(profile.servicePath));
  assert(!JSON.stringify(h.states).includes("SYNTHETIC_SECRET"));
});
test("missing config, other Python files and unrelated error text are not reported as uninstalled", async t => {
  for (const diagnostic of [
    `python3: can't open file '${managed.serviceConfig}': [Errno 2] No such file or directory\n`,
    `python3: can't open file '${managed.servicePath}.other': [Errno 2] No such file or directory\n`,
    `FileNotFoundError: [Errno 2] No such file or directory: '${managed.servicePath}'\n`,
    `Task output: python3: can't open file '${managed.servicePath}': [Errno 2] No such file or directory\n`,
    "Task output: ssh: Could not resolve hostname an-api-host: not found\n",
  ]) {
    const h = harness(t), profile = validateProfile(managed);
    const pending = h.controller.runService(profile, "discover", h.controller.generation);
    h.children[0].stderr.emit("data", Buffer.from(diagnostic)); h.children[0].emit("close", 2);
    await assert.rejects(pending, error => error.message === "SERVICE_COMMAND_FAILED");
  }
});
test("tunnel SSH diagnostics use the same fixed codes without changing reconnect or SSH options", async t => {
  const h = harness(t);
  await h.controller.connect(manual, "synthetic");
  const child = h.children[0];
  child.stderr.emit("data", Buffer.from("ssh: Could not resolve host"));
  child.stderr.emit("data", Buffer.from("name fixture-host: Name or service not known\n"));
  child.emit("exit", 255);
  assert.equal(h.controller.state.status, "error"); assert.equal(h.controller.state.code, "SSH_HOST_NOT_FOUND");
  assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
  assert(h.calls[0].args.includes("StrictHostKeyChecking=yes"));
  assert(h.calls[0].args.includes("BatchMode=yes"));
});
test("service configuration refusal preserves safe error code even with exit status 2", async t => {
  const h = harness(t); const profile = validateProfile(managed);
  const operation = h.controller.runService(profile, "discover", h.controller.generation);
  const descriptor = { status: "error", code: "SERVICE_CONFIGURATION_OR_STATE_INVALID", automaticReplay: false };
  h.children[0].stdout.emit("data", Buffer.from(JSON.stringify(descriptor))); h.children[0].emit("close", 2);
  assert.deepEqual(await operation, descriptor);
  const refusal = harness(t, { serviceRequest: async () => descriptor });
  await assert.rejects(refusal.controller.connect(managed, "synthetic"), /SERVICE_CONFIGURATION_ERROR/);
  assert.equal(refusal.controller.state.serviceCode, descriptor.code);
  assert.equal(refusal.controller.state.retrying, false);
});
test("descriptor requires explicit transport and rejects malformed dynamic host", () => {
  assert.throws(() => validateService({ ...ready, transport: undefined }, managed));
  assert.throws(() => validateService({ ...ready, host: "-oProxyCommand=evil" }, managed));
  assert.throws(() => validateService({ ...ready, port: 0 }, managed));
});
test("connection IPC validates main frame and navigation stays on the authenticated origin", () => {
  const url = "biomind-connection://app/index.html";
  const frame = { url }, contents = { mainFrame: frame }, window = { isDestroyed: () => false, webContents: contents };
  assert.equal(isConnectionPageSender({ sender: contents, senderFrame: frame }, window, url), true);
  assert.equal(isConnectionPageSender({ sender: contents, senderFrame: { url } }, window, url), false);
  const origin = "http://127.0.0.1:30142";
  assert.equal(isRemoteNavigationAllowed(origin + "/", origin), true);
  assert.equal(isRemoteNavigationAllowed(origin + "/api/files/tmp/result?type=download", origin), true);
  for (const value of ["https://evil.invalid", "file:///etc/passwd", "javascript:alert(1)", origin + "/api/terminal", "http://x@127.0.0.1:30142/"]) assert.equal(isRemoteNavigationAllowed(value, origin), false);
});
