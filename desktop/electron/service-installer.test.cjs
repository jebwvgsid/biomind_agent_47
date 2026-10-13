"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { execFileSync } = require("node:child_process"), { EventEmitter } = require("node:events");
const { BOOTSTRAP, FILES, payload } = require("./service-installer.cjs");
const { RemoteConnectionController, validateProfile } = require("./remote-controller.cjs");
const setup = { schema: "BioMindStudioConfigureV1", workbenchPassword: "synthetic-password-not-in-argv" };
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-install-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const source = path.join(dir, "source"); fs.mkdirSync(source);
  for (const name of FILES) fs.writeFileSync(path.join(source, name), name === "configure-service.py" ? `import sys,json\ndata=json.load(sys.stdin)\nprint(json.dumps({'status':'configured','configPath':sys.argv[2],'serverId':'fixture-server','serviceId':'fixture-service','reused':False,'checks':{'stdinReceived':data.get('workbenchPassword')=='${setup.workbenchPassword}'}}))\n` : "# fixture bytes\n");
  const profile = validateProfile({ host: "fixture-login.invalid", user: "fixture", servicePath: path.join(dir, "installed", "server-service.py"), serviceConfig: path.join(dir, "private", "service.json") });
  const run = value => {
    const input = path.join(dir, "payload.json"); fs.writeFileSync(input, typeof value === "string" ? value : JSON.stringify(value), { mode: 0o600 });
    const fd = fs.openSync(input, "r"); let output, status = 0;
    try { output = execFileSync("python3", ["-c", BOOTSTRAP], { encoding: "utf8", stdio: [fd, "pipe", "pipe"] }); }
    catch (error) { if (typeof error.status !== "number") throw error; status = error.status; output = error.stdout; }
    finally { fs.closeSync(fd); }
    return { status, value: JSON.parse(output.trim()) };
  };
  return { dir, source, profile, run, request: () => JSON.parse(payload(profile, setup, source)) };
}
test("bootstrap installs only allowlisted exact bytes and configures by stdin without exposing password", t => {
  const f = fixture(t), first = f.run(f.request());
  assert.equal(first.status, 0); assert.equal(first.value.status, "configured"); assert.equal(first.value.checks.stdinReceived, true);
  assert(!JSON.stringify(first.value).includes(setup.workbenchPassword));
  const target = path.dirname(f.profile.servicePath);
  assert.deepEqual(fs.readdirSync(target).sort(), [...FILES].sort());
  assert.equal(fs.statSync(target).mode & 0o777, 0o700);
  for (const name of FILES) { assert.deepEqual(fs.readFileSync(path.join(target, name)), fs.readFileSync(path.join(f.source, name))); assert.equal(fs.statSync(path.join(target, name)).mode & 0o777, 0o600); }
  assert.equal(f.run(f.request()).status, 0);
});
test("installation paths with Chinese, spaces and shell metacharacters stay literal", t => {
  const f = fixture(t), p = { ...f.profile, servicePath: path.join(f.dir, "安装 ' $(echo never-run)", "server-service.py") };
  const r = f.run(payload(p, setup, f.source)); assert.equal(r.status, 0); assert(fs.existsSync(p.servicePath));
});
test("existing different scripts are not overwritten and no other files are partially added", t => {
  const f = fixture(t), target = path.dirname(f.profile.servicePath); fs.mkdirSync(target, { mode: 0o700 });
  fs.writeFileSync(f.profile.servicePath, "existing unrecognized implementation");
  const r = f.run(f.request()); assert.equal(r.value.code, "INSTALLER_FILE_CONFLICT");
  assert.equal(fs.readFileSync(f.profile.servicePath, "utf8"), "existing unrecognized implementation");
  assert.deepEqual(fs.readdirSync(target), ["server-service.py"]);
});
test("symlink directories and script files cannot redirect installation writes", t => {
  const f = fixture(t), other = path.join(f.dir, "other"); fs.mkdirSync(other);
  fs.symlinkSync(other, path.dirname(f.profile.servicePath));
  assert.equal(f.run(f.request()).status, 2); assert.deepEqual(fs.readdirSync(other), []);
  fs.unlinkSync(path.dirname(f.profile.servicePath)); fs.mkdirSync(path.dirname(f.profile.servicePath));
  const victim = path.join(other, "victim"); fs.writeFileSync(victim, "KEEP"); fs.symlinkSync(victim, f.profile.servicePath);
  assert.equal(f.run(f.request()).status, 2); assert.equal(fs.readFileSync(victim, "utf8"), "KEEP");
});
test("group writable target, extra files and corrupted bytes fail before writing scripts", t => {
  const f = fixture(t), target = path.dirname(f.profile.servicePath); fs.mkdirSync(target); fs.chmodSync(target, 0o770);
  assert.equal(f.run(f.request()).value.code, "INSTALL_DIRECTORY_NOT_PRIVATE"); fs.chmodSync(target, 0o700);
  const extra = f.request(); extra.files.push({ name: "other.py", data: "", sha256: "" });
  assert.equal(f.run(extra).value.code, "INSTALL_FILE_ALLOWLIST_VIOLATION");
  const corrupt = f.request(); corrupt.files[0].sha256 = "0".repeat(64);
  assert.equal(f.run(corrupt).value.code, "INSTALL_FILE_INTEGRITY_FAILED"); assert.deepEqual(fs.readdirSync(target), []);
});
function harness(t, f, respond) {
  const calls = [], states = [], children = [];
  const controller = new RemoteConnectionController({ onState: s => states.push(s), spawn(command, args, options) {
    const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter(); child.kills = 0;
    child.stdin.end = input => { calls.push({ command, args, options, input }); if (respond) queueMicrotask(() => respond(child, JSON.parse(input))); };
    child.kill = () => { child.kills++; child.emit("close", 0); }; children.push(child); return child;
  } });
  t.after(() => controller.disconnect()); return { controller, calls, states, children };
}
test("controller installation sends credentials only through stdin and never reports service ready", async t => {
  const f = fixture(t), h = harness(t, f, (child, request) => {
    child.stdout.emit("data", Buffer.from(JSON.stringify({ status: "configured", configPath: request.serviceConfig, serverId: "fixture-server", serviceId: "fixture-service" }))); child.emit("close", 0);
  });
  const result = await h.controller.install(f.profile, setup, f.source);
  assert.equal(result.status, "configured"); assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.shell, false); assert(!h.calls[0].args.join(" ").includes(setup.workbenchPassword));
  assert(h.calls[0].input.includes(setup.workbenchPassword)); assert(!JSON.stringify(h.states).includes(setup.workbenchPassword));
  assert.equal(h.controller.state.status, "configured"); assert(!h.states.some(s => s.status === "ready"));
});
test("disconnect cancels only owned installer transport; concurrent connect does not race setup", async t => {
  const f = fixture(t), h = harness(t, f);
  const operation = h.controller.install(f.profile, setup, f.source);
  await assert.rejects(h.controller.connect(f.profile, "password"), /INSTALLATION_IN_PROGRESS/);
  h.controller.disconnect(); await assert.rejects(operation, /CONNECTION_CANCELLED/);
  assert.equal(h.controller.state.status, "disconnected"); assert.equal(h.children[0].kills, 1);
});
test("installer returns only sanitized configuration failure and preserves pinned server identity", async t => {
  const f = fixture(t), failure = harness(t, f, child => {
    child.stderr.emit("data", Buffer.from("raw secret diagnostic " + setup.workbenchPassword));
    child.stdout.emit("data", Buffer.from(JSON.stringify({ status: "error", code: "SERVICE_ACTIVE_CONFIGURATION_LOCKED" }))); child.emit("close", 2);
  });
  await assert.rejects(failure.controller.install(f.profile, setup, f.source), /SERVICE_ACTIVE_CONFIGURATION_LOCKED/);
  assert(!JSON.stringify(failure.states).includes(setup.workbenchPassword));
  const changed = harness(t, f, (child, request) => {
    child.stdout.emit("data", Buffer.from(JSON.stringify({ status: "configured", configPath: request.serviceConfig, serverId: "another-server" }))); child.emit("close", 0);
  });
  await assert.rejects(changed.controller.install({ ...f.profile, serverId: "fixture-server" }, setup, f.source), /SERVER_ID_CHANGED/);
});
test("installer uses the same sanitized SSH diagnostics before any remote configuration receipt", async t => {
  for (const [line, code] of [
    ["ssh: Could not resolve hostname fixture-login.invalid: Name or service not known\n", "SSH_HOST_NOT_FOUND"],
    ["ssh: connect to host fixture-login.invalid port 22: Connection timed out\n", "SSH_CONNECTION_TIMEOUT"],
    ["ssh: connect to host fixture-login.invalid port 22: Connection refused\n", "SSH_CONNECTION_REFUSED"],
  ]) {
    const f = fixture(t), h = harness(t, f, child => {
      child.stderr.emit("data", Buffer.from("private diagnostic " + setup.workbenchPassword + "\n"));
      child.stderr.emit("data", Buffer.from(line.slice(0, 23)));
      child.stderr.emit("data", Buffer.from(line.slice(23))); child.emit("close", 255);
    });
    await assert.rejects(h.controller.install(f.profile, setup, f.source), error => error.message === code);
    assert.equal(h.controller.state.status, "error"); assert.equal(h.controller.state.code, code);
    assert(!JSON.stringify(h.states).includes(setup.workbenchPassword));
    assert(!JSON.stringify(h.states).includes("private diagnostic"));
    assert.equal(h.calls.length, 1);
  }
});
