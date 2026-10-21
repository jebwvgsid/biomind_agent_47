/** Fixed-schema startup records only. Never pass errors, settings or user input. */
import { randomUUID } from 'node:crypto';

type Stage = 'node_start' | 'main' | 'compat' | 'build' | 'lease' | 'resources' | 'model'
  | 'fixture' | 'session' | 'extensions' | 'domain' | 'memory' | 'ready' | 'failure' | 'exit';
type Event = 'begin' | 'end' | 'emit' | 'error' | 'exit';
type Identity = { build?: string; sessionId?: string; ownerGeneration?: string; code?: 'PI_INITIALIZATION_FAILED'; exitCode?: number };
type State = { origin: bigint; attemptId: string; count: number; bytes: number; dropped: number };
const root = globalThis as typeof globalThis & { __bioMindStartupV1?: State };
const enabled = process.argv.includes('json');
const validId = /^[A-Za-z0-9._:-]{1,128}$/;
const state = enabled ? root.__bioMindStartupV1 ??= {
  origin: process.hrtime.bigint(),
  attemptId: validId.test(process.env.BIOMIND_STARTUP_ATTEMPT_ID ?? '') ? process.env.BIOMIND_STARTUP_ATTEMPT_ID! : randomUUID(),
  count: 0, bytes: 0, dropped: 0,
} : undefined;

export const startup = {
  mark(stage: Stage, event: Event, identity: Identity = {}): void {
    if (!state) return;
    const row: Record<string, unknown> = { v: 1, attemptId: state.attemptId, role: 'worker', pid: process.pid, ppid: process.ppid,
      elapsedMs: Number(process.hrtime.bigint() - state.origin) / 1e6, stage, event };
    for (const key of ['build', 'sessionId', 'ownerGeneration'] as const) {
      if (identity[key] && validId.test(identity[key]!)) row[key] = identity[key];
    }
    if (identity.code === 'PI_INITIALIZATION_FAILED') row.code = identity.code;
    if (Number.isInteger(identity.exitCode)) row.exitCode = identity.exitCode;
    const line = '[biomind-startup-v1] ' + JSON.stringify(row) + '\n';
    const bytes = Buffer.byteLength(line);
    if (bytes > 1024 || state.count >= 64 || state.bytes + bytes > 32768) { state.dropped++; return; }
    state.count++; state.bytes += bytes;
    process.stderr.write(line);
  },
};
