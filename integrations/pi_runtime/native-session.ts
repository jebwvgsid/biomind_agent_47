/** Session selection only. Pi owns JSONL, indexes, branches, queueing and saves. */
import fs from 'node:fs';
import path from 'node:path';
import { SessionManager } from '@earendil-works/pi-coding-agent';

export interface NativeSessionSelection {
  sessionMode?: 'persistent' | 'temporary';
  session?: string;
  continueSession?: boolean;
}
export class ProductSessionError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'ProductSessionError'; }
}
export function validateSessionSelection(selection: NativeSessionSelection): void {
  if (selection.sessionMode !== undefined && !['persistent', 'temporary'].includes(selection.sessionMode)) throw new ProductSessionError('SESSION_MODE_INVALID');
  if (selection.session !== undefined && (typeof selection.session !== 'string' || !selection.session.trim())) throw new ProductSessionError('SESSION_REFERENCE_INVALID');
  if (selection.continueSession !== undefined && typeof selection.continueSession !== 'boolean') throw new ProductSessionError('SESSION_CONTINUE_INVALID');
  if ((selection.session && selection.continueSession) || (selection.sessionMode === 'temporary' && (selection.session || selection.continueSession))) throw new ProductSessionError('SESSION_SELECTION_CONFLICT');
}
export function openProductSession(cwd: string, selection: NativeSessionSelection = {}): SessionManager {
  validateSessionSelection(selection);
  if (selection.sessionMode === 'temporary') return SessionManager.inMemory(cwd);
  try {
    let manager: SessionManager;
    if (selection.session) {
      const reference = selection.session;
      const filename = reference.includes('/') || reference.includes('\\') || reference.endsWith('.jsonl')
        ? path.resolve(cwd, reference) : SessionManager.findById(cwd, reference);
      // Pi.open permits creating a file at a missing path. A BioMind resume
      // request must instead report an error, never manufacture a new session.
      if (!filename || !fs.statSync(filename, { throwIfNoEntry: false })?.isFile()) throw new ProductSessionError('SESSION_NOT_FOUND');
      if (fs.statSync(filename).size === 0) throw new ProductSessionError('SESSION_INVALID_FILE');
      manager = SessionManager.open(filename);
      if (fs.realpathSync(manager.getCwd()) !== fs.realpathSync(cwd)) throw new ProductSessionError('SESSION_PROJECT_MISMATCH');
    } else manager = selection.continueSession ? SessionManager.continueRecent(cwd) : SessionManager.create(cwd);
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
