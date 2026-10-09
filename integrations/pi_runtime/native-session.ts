/** Session selection only. Pi owns JSONL, indexes, branches, queueing and saves. */
import fs from 'node:fs';
import path from 'node:path';
import { SessionManager, CURRENT_SESSION_VERSION, SettingsManager, ProjectTrustStore, hasTrustRequiringProjectResources } from '@earendil-works/pi-coding-agent';

import { ProductSessionError, validateSessionSelection, resolveSessionDir, type NativeSessionSelection } from './native-session-selection.js';
export { ProductSessionError, validateSessionSelection, resolveSessionDir, type NativeSessionSelection } from './native-session-selection.js';
/** Inspect without opening/migrating Pi history. A corrupt explicit file never becomes a new session. */
export function readTargetSessionRoot(filename: string): string {
  try {
    const stat = fs.statSync(filename, { throwIfNoEntry: false });
    if (!stat?.isFile()) throw new ProductSessionError('SESSION_NOT_FOUND');
    if (!stat.size) throw new ProductSessionError('SESSION_INVALID_FILE');
    fs.accessSync(filename, fs.constants.R_OK | fs.constants.W_OK);
    const lines = fs.readFileSync(filename, 'utf8').split('\n').filter(line => line.trim());
    const header = JSON.parse(lines[0]);
    const version = header?.version ?? 1; // Native legacy headers may omit version.
    if (header?.type !== 'session' || typeof header.id !== 'string' || !header.id.trim() ||
        !Number.isInteger(version) || version < 1 || version > CURRENT_SESSION_VERSION ||
        typeof header.cwd !== 'string' || !path.isAbsolute(header.cwd)) throw new Error('Invalid session header');
    // Validate ownership/header only. Pi's native loader handles interrupted
    // tail records and native migration; the host never truncates JSONL.
    for (const line of lines.slice(1)) {
      let entry: any;
      try { entry = JSON.parse(line); } catch { continue; } // Native interrupted-record recovery.
      if (entry?.type === 'message' && (!entry.message || typeof entry.message !== 'object'))
        throw new Error('Invalid complete message record');
    }
    return header.cwd;
  } catch (error) {
    if (error instanceof ProductSessionError) throw error;
    throw new ProductSessionError('SESSION_OPEN_FAILED');
  }
}
export async function resolveSessionTarget(cwd: string, selection: NativeSessionSelection, launchCwd: string, explicitProject: boolean): Promise<string> {
  if (!selection.session) return cwd;
  const reference = selection.session;
  let filename = reference.includes('/') || reference.includes('\\') || reference.endsWith('.jsonl')
    ? path.resolve(launchCwd, reference) : SessionManager.findById(cwd, reference, selection.sessionDir);
  if (!filename) {
    const matches = (await SessionManager.listAll(selection.sessionDir)).filter(row => row.id === reference);
    if (matches.length > 1) throw new ProductSessionError('SESSION_REFERENCE_AMBIGUOUS');
    filename = matches[0]?.path;
  }
  if (!filename) throw new ProductSessionError('SESSION_NOT_FOUND');
  const target = readTargetSessionRoot(filename);
  try {
    if (!fs.statSync(target).isDirectory()) throw new Error();
    if (explicitProject && fs.realpathSync(target) !== fs.realpathSync(cwd)) throw new ProductSessionError('SESSION_PROJECT_MISMATCH',
      `SESSION_PROJECT_MISMATCH: 会话属于 ${target}，与显式项目 ${cwd} 冲突；请使用会话所属项目的 --project-dir，或移除显式项目参数。`);
  } catch (error) {
    if (error instanceof ProductSessionError) throw error;
    throw new ProductSessionError('SESSION_PROJECT_UNAVAILABLE');
  }
  selection.session = filename;
  return explicitProject ? cwd : target;
}
export function openProductSession(cwd: string, selection: NativeSessionSelection = {}): SessionManager {
  validateSessionSelection(selection);
  if (selection.sessionMode === 'temporary') return SessionManager.inMemory(cwd);
  try {
    let manager: SessionManager;
    if (selection.session) {
      const reference = selection.session;
      const filename = reference.includes('/') || reference.includes('\\') || reference.endsWith('.jsonl')
        ? path.resolve(cwd, reference) : SessionManager.findById(cwd, reference, selection.sessionDir);
      // Pi.open permits creating a file at a missing path. A BioMind resume
      // request must instead report an error, never manufacture a new session.
      if (!filename || !fs.statSync(filename, { throwIfNoEntry: false })?.isFile()) throw new ProductSessionError('SESSION_NOT_FOUND');
      if (fs.statSync(filename).size === 0) throw new ProductSessionError('SESSION_INVALID_FILE');
      if (fs.realpathSync(readTargetSessionRoot(filename)) !== fs.realpathSync(cwd)) throw new ProductSessionError('SESSION_PROJECT_MISMATCH');
      manager = SessionManager.open(filename, selection.sessionDir);
    } else manager = selection.continueSession ? SessionManager.continueRecent(cwd, selection.sessionDir) : SessionManager.create(cwd, selection.sessionDir);
    // Pi defers writing a fresh transcript until the first assistant message.
    // Catch directory permission failures early; write failures still propagate
    // from Pi and are never converted into an in-memory fallback.
    fs.accessSync(manager.getSessionDir(), fs.constants.W_OK);
    return manager;
  } catch (error) {
    if (error instanceof ProductSessionError) throw error;
    throw new ProductSessionError(selection.session ? 'SESSION_OPEN_FAILED' : 'SESSION_CREATE_FAILED');
  }
}
export function describeProductSession(manager: SessionManager) {
  return { sessionId: manager.getSessionId(), sessionFile: manager.getSessionFile() ?? null,
    sessionPersistence: manager.isPersisted() ? 'persistent' as const : 'temporary' as const,
    sessionSaved: !!manager.getSessionFile() && fs.existsSync(manager.getSessionFile()!) };
}

/** Pi public trust store + native settings, with no prompt or trust writes in JSON mode. */
export function createProductSettings(cwd: string, agentDir: string): SettingsManager {
  const user = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
  const saved = fs.existsSync(path.join(agentDir, 'trust.json')) ? new ProjectTrustStore(agentDir).get(cwd) : null;
  const trusted = !hasTrustRequiringProjectResources(cwd) || (saved ?? user.getDefaultProjectTrust() === 'always');
  return SettingsManager.create(cwd, agentDir, { projectTrusted: trusted });
}
