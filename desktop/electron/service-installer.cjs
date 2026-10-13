"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const FILES = Object.freeze(["server-service.py", "next-service.mjs", "configure-service.py"]);
// The command is constant; paths, credentials and file bytes travel only on stdin.
// Directory-relative O_NOFOLLOW operations reject symlinks and overwrite races.
const BOOTSTRAP = String.raw`
import base64, hashlib, json, os, stat, subprocess, sys
FILES = {'server-service.py', 'next-service.mjs', 'configure-service.py'}
class Refused(Exception): pass
def require(ok, code):
    if not ok: raise Refused(code)
def directory(value):
    require(isinstance(value, str) and value.startswith('/') and not any(c in value for c in '\x00\r\n'), 'INVALID_SERVICE_PATH')
    parts = value.split('/')[1:]
    require(parts and all(p and p not in ('.', '..') for p in parts), 'INVALID_SERVICE_PATH')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try: nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                require(os.fstat(fd).st_uid == os.getuid(), 'INSTALL_DIRECTORY_NOT_OWNED')
                os.mkdir(part, 0o700, dir_fd=fd)
                nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = nextfd
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'INSTALL_DIRECTORY_NOT_PRIVATE')
        return fd
    except BaseException:
        os.close(fd); raise
def main():
    raw = sys.stdin.buffer.read(3 * 1024 * 1024 + 1)
    require(len(raw) <= 3 * 1024 * 1024, 'INSTALL_PAYLOAD_TOO_LARGE')
    payload = json.loads(raw)
    require(payload.get('schema') == 'BioMindStudioInstallerV1', 'INSTALL_PAYLOAD_INVALID')
    service = payload.get('servicePath')
    require(isinstance(service, str) and os.path.basename(service) == 'server-service.py', 'INVALID_SERVICE_PATH')
    target = os.path.dirname(service)
    config = payload.get('serviceConfig')
    require(isinstance(config, str) and config.startswith('/') and not any(c in config for c in '\x00\r\n'), 'INVALID_SERVICE_CONFIG_PATH')
    setup = payload.get('setup')
    require(isinstance(setup, dict) and len(json.dumps(setup).encode()) <= 65536, 'INSTALL_SETUP_INVALID')
    rows = payload.get('files')
    require(isinstance(rows, list) and len(rows) == len(FILES) and {r.get('name') for r in rows} == FILES, 'INSTALL_FILE_ALLOWLIST_VIOLATION')
    decoded = {}
    for row in rows:
        data = base64.b64decode(row['data'], validate=True)
        require(len(data) <= 1024 * 1024 and hashlib.sha256(data).hexdigest() == row['sha256'], 'INSTALL_FILE_INTEGRITY_FAILED')
        decoded[row['name']] = data
    fd = directory(target)
    try:
        missing = []
        for name, data in decoded.items():
            try: existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError: missing.append(name); continue
            try:
                info = os.fstat(existing)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022, 'INSTALL_FILE_NOT_PRIVATE')
                with os.fdopen(existing, 'rb', closefd=False) as stream: actual = stream.read(1024 * 1024 + 1)
                require(actual == data, 'INSTALLER_FILE_CONFLICT')
            finally: os.close(existing)
        for name in missing:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, 'wb') as stream:
                stream.write(decoded[name]); stream.flush(); os.fsync(stream.fileno())
        os.fsync(fd)
        # Execute the verified uploaded file through the held directory, not a
        # subsequently substituted pathname. No shell or credential argv.
        configure = '/proc/self/fd/' + str(fd) + '/configure-service.py'
        result = subprocess.run([sys.executable, configure, '--config', config], input=json.dumps(setup),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, pass_fds=(fd,))
        require(len(result.stdout.encode()) <= 65536, 'CONFIGURATION_RESPONSE_INVALID')
        output = json.loads(result.stdout.strip().splitlines()[-1])
        require(isinstance(output, dict) and output.get('status') in ('configured', 'error'), 'CONFIGURATION_RESPONSE_INVALID')
        if result.returncode != 0 and output.get('status') != 'error': raise Refused('CONFIGURATION_FAILED')
        print(json.dumps({k: output[k] for k in ('status', 'code', 'configPath', 'serverId', 'serviceId', 'reused', 'checks') if k in output}), flush=True)
        return result.returncode
    finally: os.close(fd)
try:
    sys.exit(main())
except Refused as error:
    print(json.dumps({'status': 'error', 'code': str(error)}), flush=True); sys.exit(2)
except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
    print(json.dumps({'status': 'error', 'code': 'INSTALLATION_FAILED'}), flush=True); sys.exit(2)
`;
const COMMAND = "python3 -c 'import base64; exec(base64.b64decode(\"" + Buffer.from(BOOTSTRAP).toString("base64") + "\"))'";
function payload(profile, setup, bundledServerDir) {
  if (!profile.servicePath || !profile.serviceConfig || path.posix.basename(profile.servicePath) !== "server-service.py") throw new Error("INVALID_SERVICE_PATH");
  if (!setup || typeof setup !== "object" || Array.isArray(setup) || Buffer.byteLength(JSON.stringify(setup)) > 65536) throw new Error("INSTALL_SETUP_INVALID");
  if (typeof setup.workbenchPassword !== "string" || !setup.workbenchPassword || Buffer.byteLength(setup.workbenchPassword) > 4096 || /[\r\n\0]/.test(setup.workbenchPassword)) throw new Error("INVALID_SERVER_PASSWORD");
  const files = FILES.map(name => {
    const source = path.join(bundledServerDir, name), info = fs.lstatSync(source);
    if (!info.isFile() || info.size > 1024 * 1024) throw new Error("INSTALL_SOURCE_INVALID");
    const bytes = fs.readFileSync(source);
    return { name, data: bytes.toString("base64"), sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  return JSON.stringify({ schema: "BioMindStudioInstallerV1", servicePath: profile.servicePath, serviceConfig: profile.serviceConfig, setup, files });
}
async function installService(controller, profile, setup, bundledServerDir, sshArguments, sshDiagnosticReader) {
  const input = payload(profile, setup, bundledServerDir), generation = controller.generation;
  const config = controller.ownConfig(profile);
  try {
    return await new Promise((resolve, reject) => {
      let child, output = "", settled = false, code = "SSH_CONNECTION_FAILED";
      const diagnostic = sshDiagnosticReader(code);
      const finish = (error, result) => {
        if (settled) return; settled = true; controller.clearTimer(timer); controller.commandChildren.delete(child);
        if (error) reject(error); else resolve(result);
      };
      const timer = controller.setTimer(() => { finish(new Error("INSTALLATION_TIMEOUT")); child?.kill(); }, 120000);
      timer?.unref?.();
      try { child = controller.spawn(controller.ssh, sshArguments(profile, config.path, { configTarget: config.control }, COMMAND), {
        shell: false, windowsHide: true, cwd: controller.sshCwd, stdio: ["pipe", "pipe", "pipe"] }); }
      catch { return finish(new Error("SSH_UNAVAILABLE")); }
      controller.commandChildren.add(child);
      child.stdout.on("data", chunk => { output += chunk.toString("utf8"); if (Buffer.byteLength(output) > 65536) { finish(new Error("INSTALL_RESPONSE_INVALID")); child.kill(); } });
      child.stderr.on("data", chunk => {
        code = diagnostic(chunk);
      });
      child.once("error", () => finish(new Error("SSH_UNAVAILABLE")));
      child.once("close", exitCode => {
        if (generation !== controller.generation) return finish(new Error("CONNECTION_CANCELLED"));
        try {
          const value = JSON.parse(output.trim().split(/\r?\n/).filter(line => line.trim().startsWith("{")).at(-1));
          if (value.status === "error") return finish(new Error(typeof value.code === "string" && /^[A-Z][A-Z_0-9]{1,80}$/.test(value.code) ? value.code : "INSTALLATION_FAILED"));
          if (exitCode !== 0) return finish(new Error(code));
          if (value.status !== "configured" || value.configPath !== profile.serviceConfig || typeof value.serverId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.serverId)) throw new Error();
          if (profile.serverId && profile.serverId !== value.serverId) return finish(new Error("SERVER_ID_CHANGED"));
          finish(null, value);
        } catch { finish(new Error(exitCode !== 0 ? code : "INSTALL_RESPONSE_INVALID")); }
      });
      child.stdin.on("error", () => {}); // SSH's close/error supplies the sanitized diagnostic.
      child.stdin.end(input);
    });
  } finally { controller.releaseConfig(config); }
}
module.exports = { FILES, BOOTSTRAP, COMMAND, payload, installService };
