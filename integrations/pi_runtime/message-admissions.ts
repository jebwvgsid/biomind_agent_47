/** Bounded, process-local receipts. This is not a queue or a durable execution log. */
import { createHash } from 'node:crypto';

export interface Admission {
  clientMessageId: string;
  command: string;
  digest: string;
  status: 'pending' | 'accepted' | 'consumed' | 'failed' | 'unknown';
  receipt?: Record<string, unknown>;
  code?: string;
}
export class AdmissionError extends Error {
  constructor(readonly code: string) { super(code); }
}
/** Correlation fields are bounded metadata, never arbitrary JSON objects. */
export function validateFrameIds(frame: Record<string, any>): void {
  for (const key of ['id','requestId','taskId','clientMessageId','ownerGeneration','sessionId','project','businessRequestId']) {
    const value = frame[key];
    if (value === undefined) continue;
    if (key === 'id' && typeof value === 'number' && Number.isSafeInteger(value)) continue;
    if (typeof value !== 'string' || value.length === 0 || Buffer.byteLength(value) > (key === 'project' ? 4096 : 256)) {
      throw new AdmissionError('INVALID_CORRELATION_ID');
    }
  }
}
export class MessageAdmissions {
  private readonly entries = new Map<string, Admission>();
  constructor(readonly scope: { ownerGeneration: string; sessionId: string; project: string }, readonly capacity = 1024) {}
  begin(frame: Record<string, any>): { entry?: Admission; duplicate: boolean } {
    validateFrameIds(frame);
    const keys = ['clientMessageId', 'ownerGeneration', 'sessionId', 'project'];
    if (!keys.some(key => frame[key] !== undefined)) return { duplicate: false };
    if (!keys.every(key => typeof frame[key] === 'string') || !/^[A-Za-z0-9._:-]{1,128}$/.test(frame.clientMessageId)) {
      throw new AdmissionError('INVALID_MESSAGE_SCOPE');
    }
    if (frame.ownerGeneration !== this.scope.ownerGeneration || frame.sessionId !== this.scope.sessionId || frame.project !== this.scope.project) {
      throw new AdmissionError('MESSAGE_SCOPE_MISMATCH');
    }
    const digest = createHash('sha256').update(JSON.stringify({ type: frame.type, text: frame.text,
      images: frame.images, requestId: frame.requestId, taskId: frame.taskId })).digest('hex');
    const previous = this.entries.get(frame.clientMessageId);
    if (previous) {
      if (previous.digest !== digest) throw new AdmissionError('MESSAGE_ID_CONFLICT');
      return { entry: previous, duplicate: true };
    }
    // Never evict and then assume a forgotten id is new. Refuse new ids instead.
    if (this.entries.size >= this.capacity) throw new AdmissionError('ADMISSION_CAPACITY_REACHED');
    const entry: Admission = { clientMessageId: frame.clientMessageId, command: frame.type, digest, status: 'pending' };
    this.entries.set(entry.clientMessageId, entry);
    return { entry, duplicate: false };
  }
  snapshot(): Omit<Admission, 'digest'>[] {
    return [...this.entries.values()].map(({ digest: _digest, ...entry }) => ({ ...entry }));
  }
}
