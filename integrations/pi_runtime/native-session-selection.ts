import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
/** Pure session selection; Pi owns session discovery, projection and persistence. */
export interface NativeSessionSelection {
  sessionMode?: 'persistent' | 'temporary';
  session?: string;
  continueSession?: boolean;
  sessionDir?: string;
}
export class ProductSessionError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'ProductSessionError'; }
}
export function validateSessionSelection(selection: NativeSessionSelection): void {
  if (selection.sessionMode !== undefined && !['persistent', 'temporary'].includes(selection.sessionMode)) throw new ProductSessionError('SESSION_MODE_INVALID');
  if (selection.session !== undefined && (typeof selection.session !== 'string' || !selection.session.trim())) throw new ProductSessionError('SESSION_REFERENCE_INVALID');
  if (selection.continueSession !== undefined && typeof selection.continueSession !== 'boolean') throw new ProductSessionError('SESSION_CONTINUE_INVALID');
  if (selection.sessionDir !== undefined && (typeof selection.sessionDir !== 'string' || !selection.sessionDir.trim())) throw new ProductSessionError('SESSION_DIRECTORY_INVALID');
  if ((selection.session && selection.continueSession) || (selection.sessionMode === 'temporary' && (selection.session || selection.continueSession))) throw new ProductSessionError('SESSION_SELECTION_CONFLICT');
}

/** Resolve only the directory override; native Pi still owns its default layout. */
export function resolveSessionDir(cwd: string, selection: NativeSessionSelection, settings: { getSessionDir(): string | undefined }): string | undefined {
  const value = selection.sessionDir ?? process.env.PI_CODING_AGENT_SESSION_DIR ?? settings.getSessionDir();
  if (!value) return undefined;
  const expanded = value === '~' ? os.homedir() : value.startsWith('~/') ? path.join(os.homedir(), value.slice(2))
    : value.startsWith('file://') ? fileURLToPath(value) : value;
  return path.resolve(cwd, expanded);
}
