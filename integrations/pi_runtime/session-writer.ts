/** Lifetime ownership only. Pi remains the sole transcript reader/writer.
 * Unknown/pre-lease owners and abandoned claims are never automatically taken over.
 */
import { constants } from 'node:fs';
import { lstat, open, realpath, rename, unlink } from 'node:fs/promises';
import path from 'node:path';
import { hostname } from 'node:os';
import { randomUUID } from 'node:crypto';

export class SessionWriterError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'SessionWriterError'; }
}
interface FileIdentity { dev: string; ino: string; size: string; mtimeNs: string; ctimeNs: string }
export interface SessionWriterIdentity {
  schema: 1; sessionFile: string; sessionId: string; cwd: string;
  hostname: string; pid: number; ownerGeneration: string; nonce: string;
}
export interface SessionWriterLease {
  identity: SessionWriterIdentity;
  inheritedEnvironment: string;
  release: () => Promise<void>;
}
interface ClosedWriter {
  schema: 1; sessionFile: string; sessionId: string; cwd: string;
  closedAt: string; file: FileIdentity;
}
type Options = { sessionFile: string; sessionId: string; cwd: string; ownerGeneration: string;
  mode: 'new' | 'resume'; inherited?: string;
  /** Trusted native parent only: Pi fork created this unique, unpublished file. */
  createdByThisOwner?: boolean };
const liveKey = Symbol.for('biomind.session-writer.leases');
const globals = globalThis as typeof globalThis & { [liveKey]?: Map<string, SessionWriterLease> };
const lives = () => globals[liveKey] ??= new Map<string, SessionWriterLease>();
const fail = (code: string): never => { throw new SessionWriterError(code); };
const lockPath = (filename: string) => filename + '.biomind-writer.lock';
const closedPath = (filename: string) => filename + '.biomind-writer.closed';

async function target(sessionFile: string): Promise<string> {
  if (!path.isAbsolute(sessionFile) || !sessionFile.endsWith('.jsonl') || sessionFile.includes('\0') || sessionFile.length > 4096) fail('SESSION_WRITER_INVALID_TARGET');
  const filename = path.join(await realpath(path.dirname(sessionFile)), path.basename(sessionFile));
  const info = await lstat(filename).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
  if (info && (!info.isFile() || info.isSymbolicLink())) fail('SESSION_WRITER_INVALID_TARGET');
  return filename;
}
async function fileIdentity(filename: string, sessionId: string, cwd: string): Promise<FileIdentity> {
  const handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat({ bigint: true });
    if (!info.isFile()) fail('SESSION_WRITER_INVALID_TARGET');
    const bytes = Buffer.alloc(8192);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    const newline = bytes.subarray(0, bytesRead).indexOf(10);
    if (newline < 0) fail('SESSION_WRITER_INVALID_TARGET');
    let header;
    try { header = JSON.parse(bytes.subarray(0, newline).toString('utf8')); } catch { fail('SESSION_WRITER_INVALID_TARGET'); }
    if (header?.type !== 'session' || header.version !== 3 || header.id !== sessionId || typeof header.cwd !== 'string'
        || await realpath(header.cwd) !== cwd) fail('SESSION_WRITER_INVALID_TARGET');
    return { dev: String(info.dev), ino: String(info.ino), size: String(info.size), mtimeNs: String(info.mtimeNs), ctimeNs: String(info.ctimeNs) };
  } finally { await handle.close(); }
}
async function readRecord<T>(filename: string): Promise<T> {
  const handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > 32768) fail('SESSION_WRITER_INVALID_RECORD');
    return JSON.parse(await handle.readFile('utf8')) as T;
  } finally { await handle.close(); }
}
const equalFile = (left: FileIdentity | undefined, right: FileIdentity) => left
  && ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs'].every(key => left[key as keyof FileIdentity] === right[key as keyof FileIdentity]);

/** Read-only, bounded, no liveness probing, stale claim deletion or session open. */
export async function inspectSessionWriter(sessionFile: string, sessionId: string, project: string): Promise<{
  resumable: boolean; reason: string; sessionFile?: string;
}> {
  try {
    const filename = await target(sessionFile), cwd = await realpath(project);
    const claim = await lstat(lockPath(filename)).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
    if (claim) return { resumable: false, reason: 'SESSION_WRITER_OWNED_OR_UNKNOWN', sessionFile: filename };
    const closed = await readRecord<ClosedWriter>(closedPath(filename));
    if (closed.schema !== 1 || closed.sessionFile !== filename || closed.sessionId !== sessionId || closed.cwd !== cwd
        || typeof closed.closedAt !== 'string' || !equalFile(closed.file, await fileIdentity(filename, sessionId, cwd))) {
      return { resumable: false, reason: 'SESSION_WRITER_CLOSE_IDENTITY_CHANGED', sessionFile: filename };
    }
    return { resumable: true, reason: 'SESSION_WRITER_CLOSED', sessionFile: filename };
  } catch (error) {
    return { resumable: false, reason: error instanceof SessionWriterError ? error.code : 'SESSION_WRITER_HISTORY_UNVERIFIED' };
  }
}

export async function acquireSessionWriter(options: Options): Promise<SessionWriterLease> {
  const filename = await target(options.sessionFile), cwd = await realpath(options.cwd);
  if (!options.sessionId || options.sessionId.length > 256 || !options.ownerGeneration || options.ownerGeneration.length > 256) fail('SESSION_WRITER_INVALID_OWNER');
  const existing = lives().get(filename);
  if (existing) {
    if (existing.identity.sessionId !== options.sessionId || existing.identity.cwd !== cwd) fail('SESSION_WRITER_SCOPE_MISMATCH');
    if (existing.identity.ownerGeneration !== options.ownerGeneration) fail('SESSION_WRITER_OWNED_OR_UNKNOWN');
    // Borrowers inside the same process must not release the host's claim.
    return { ...existing, release: async () => {} };
  }
  if (options.inherited) {
    let inherited: SessionWriterIdentity;
    try { inherited = JSON.parse(options.inherited); } catch { return fail('SESSION_WRITER_INVALID_INHERITANCE'); }
    const held = await readRecord<SessionWriterIdentity>(lockPath(filename));
    if (inherited.schema !== 1 || inherited.sessionFile !== filename || inherited.sessionId !== options.sessionId || inherited.cwd !== cwd
        || inherited.hostname !== hostname() || ![process.pid, process.ppid].includes(inherited.pid)
        || held.nonce !== inherited.nonce || held.pid !== inherited.pid || held.hostname !== inherited.hostname
        || held.sessionFile !== filename || held.sessionId !== options.sessionId || held.cwd !== cwd) fail('SESSION_WRITER_INVALID_INHERITANCE');
    return { identity: held, inheritedEnvironment: options.inherited, release: async () => {} };
  }
  if (options.mode === 'resume') {
    const state = await inspectSessionWriter(filename, options.sessionId, cwd);
    if (!state.resumable) fail(state.reason);
  } else {
    const file = await lstat(filename).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
    if (file && !options.createdByThisOwner) fail('SESSION_WRITER_EXISTING_FILE_REQUIRES_RESUME');
    // No existing transcript with a close receipt can be relabelled as a fork.
    // The caller proves the SDK fork event and its previously held parent file.
    if (options.createdByThisOwner) {
      if (!file) fail('SESSION_WRITER_UNVERIFIED_FORK');
      const closed = await lstat(closedPath(filename)).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
      if (closed) fail('SESSION_WRITER_EXISTING_FILE_REQUIRES_RESUME');
      await fileIdentity(filename, options.sessionId, cwd);
    }
  }
  const identity: SessionWriterIdentity = { schema: 1, sessionFile: filename, sessionId: options.sessionId, cwd,
    hostname: hostname(), pid: process.pid, ownerGeneration: options.ownerGeneration, nonce: randomUUID() };
  let handle;
  try { handle = await open(lockPath(filename), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'EEXIST') fail('SESSION_WRITER_OWNED_OR_UNKNOWN'); throw error; }
  try { await handle.writeFile(JSON.stringify(identity) + '\n'); await handle.sync(); }
  finally { await handle.close(); }
  // Recheck after taking the atomic claim: a different owner may have completed
  // between the read-only inspection and this open(O_EXCL).
  if (options.mode === 'resume') {
    const closed = await readRecord<ClosedWriter>(closedPath(filename));
    if (!equalFile(closed.file, await fileIdentity(filename, options.sessionId, cwd))) {
      await unlink(lockPath(filename)); fail('SESSION_WRITER_CLOSE_IDENTITY_CHANGED');
    }
  }
  let released = false;
  const lease: SessionWriterLease = { identity, inheritedEnvironment: JSON.stringify(identity), release: async () => {
    if (released) return;
    const held = await readRecord<SessionWriterIdentity>(lockPath(filename));
    if (held.nonce !== identity.nonce || held.pid !== process.pid || held.sessionFile !== filename) fail('SESSION_WRITER_CLAIM_CHANGED');
    // A never-saved fresh session has no transcript to resume. No fake header is
    // written just to manufacture a successful lifecycle record.
    const exists = await lstat(filename).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
    if (exists) {
      const closed: ClosedWriter = { schema: 1, sessionFile: filename, sessionId: options.sessionId, cwd,
        closedAt: new Date().toISOString(), file: await fileIdentity(filename, options.sessionId, cwd) };
      const temporary = closedPath(filename) + '.' + identity.nonce + '.tmp';
      const output = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
      try { await output.writeFile(JSON.stringify(closed) + '\n'); await output.sync(); } finally { await output.close(); }
      await rename(temporary, closedPath(filename));
    }
    await unlink(lockPath(filename)); released = true; lives().delete(filename);
  } };
  lives().set(filename, lease);
  return lease;
}
