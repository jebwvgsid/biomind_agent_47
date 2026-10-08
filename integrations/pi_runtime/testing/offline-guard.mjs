/** Preload only for isolated product tests. Never use for normal provider runs. */
import fs from 'node:fs';
import promises from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Socket } from 'node:net';
import { registerHooks, syncBuiltinESMExports } from 'node:module';

const root = process.env.BIOMIND_PI_TEST_ROOT;
const trace = process.env.BIOMIND_PI_GUARD_TRACE;
if (!root || !path.isAbsolute(root) || !trace) throw new Error('Explicit isolated test root and guard trace required');
const append = fs.appendFileSync.bind(fs);
const realpath = fs.realpathSync.bind(fs);
const record = event => append(trace, JSON.stringify({ pid: process.pid, ...event }) + '\n');
// Performance regression guard for real native startup tests. The launcher
// legitimately uses the SDK; only the bundled CLI must reuse its embedded copy.
if (process.env.BIOMIND_PI_EXPECT_EMBEDDED_SDK === '1' &&
    process.argv[1]?.endsWith('/@earendil-works/pi-coding-agent/dist/bundle/cli.js')) {
  registerHooks({ load(url, context, nextLoad) {
    if (url.startsWith('file:') && fileURLToPath(url).endsWith('/@earendil-works/pi-coding-agent/dist/index.js')) {
      record({ kind: 'duplicate_sdk_import', allowed: false });
      throw new Error('NATIVE_STARTUP_DUPLICATE_SDK');
    }
    return nextLoad(url, context);
  } });
  record({ kind: 'native_embedded_sdk_guard', allowed: true });
}
// These installed SDK modules define the API-key management endpoints. Their
// basenames are source files, not user credential stores. Keep the exception to
// exact source paths; a similarly named file or a symlink elsewhere is denied.
const sdkKeySourceFiles = new Set([
  '../node_modules/openai/resources/admin/organization/projects/api-keys.mjs',
  '../node_modules/@anthropic-ai/sdk/resources/beta/organization/api-keys.mjs',
  '../node_modules/@earendil-works/pi-coding-agent/node_modules/openai/resources/admin/organization/projects/api-keys.mjs',
  '../node_modules/@earendil-works/pi-coding-agent/node_modules/@anthropic-ai/sdk/resources/beta/organization/api-keys.mjs',
].map(relative => fileURLToPath(new URL(relative, import.meta.url))));
function guardRead(filename) {
  if (typeof filename === 'number') return;
  const name = filename instanceof URL ? fileURLToPath(filename) : String(filename);
  let absolute = path.resolve(name);
  try { absolute = realpath(absolute); } catch { /* Missing files have no contents to expose. */ }
  const base = path.basename(absolute);
  const environmentFile = base.startsWith('.env');
  const credentialFile = /^(?:auth\.json|credentials?(?:\..*)?|api[_-]?keys?(?:\..*)?|\.netrc|\.npmrc)$/i.test(base);
  if (process.env.BIOMIND_PI_TRACE_SYNTHETIC_READS === '1' && (absolute === root || absolute.startsWith(root + path.sep)))
    record({ kind: 'synthetic_file_access', path: absolute });
  if (!environmentFile && !credentialFile) return;
  if (sdkKeySourceFiles.has(absolute)) {
    record({ kind: 'installed_sdk_source_read', allowed: true, path: absolute });
    return;
  }
  const synthetic = absolute === root || absolute.startsWith(root + path.sep);
  const allowed = !environmentFile && synthetic;
  record({ kind: 'sensitive_file_attempt', allowed, synthetic, path: absolute });
  if (!allowed) throw new Error('OFFLINE_TEST_CREDENTIAL_READ_FORBIDDEN');
}
for (const name of ['readFileSync', 'readFile', 'createReadStream', 'openSync', 'open']) {
  const original = fs[name].bind(fs);
  fs[name] = function (filename, ...args) { guardRead(filename); return original(filename, ...args); };
}
for (const name of ['readFile', 'open']) {
  const original = promises[name].bind(promises);
  promises[name] = async function (filename, ...args) { guardRead(filename); return original(filename, ...args); };
}
globalThis.fetch = async () => { record({ kind: 'network_attempt', transport: 'fetch', allowed: false }); throw new Error('OFFLINE_TEST_NETWORK_FORBIDDEN'); };
Socket.prototype.connect = function () { record({ kind: 'network_attempt', transport: 'socket', allowed: false }); throw new Error('OFFLINE_TEST_NETWORK_FORBIDDEN'); };
syncBuiltinESMExports();
record({ kind: 'guard_ready', allowed: true, root });
