/** Synchronous durable reservation immediately before each canary HTTP send.
 * Pi alone owns retries and compaction. This module cannot reset or refund quota.
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export interface CanaryOptions {
  ledgerPath: string; scenario: 'R1' | 'R2' | 'R3'; python?: string; taskId?: string;
}
export interface CanaryHooks {
  beforeSend(info: any): void;
  onSendResult(info: any): void;
  onProviderResult(info: any): void;
  summary(): any;
}
const TASK = 'BioMind-Pi-20260926';
const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const canonicalCanaryLedger = path.join(runtime, '.runtime', 'live-canary-20260926.sqlite');

export function createCanaryBudgetHooks(options: CanaryOptions): CanaryHooks {
  const python = options.python ?? process.env.BIOMIND_PI_PYTHON;
  if (!python || !path.isAbsolute(python)) throw new Error('CANARY_REQUIRES_ABSOLUTE_EXISTING_PYTHON');
  if (!path.isAbsolute(options.ledgerPath)) throw new Error('CANARY_REQUIRES_ABSOLUTE_LEDGER');
  if (!['R1', 'R2', 'R3'].includes(options.scenario)) throw new Error('CANARY_UNKNOWN_SCENARIO');
  const taskId = options.taskId ?? TASK;
  function call(action: string, info?: any): any {
    // Explicit projection: never serialize AbortSignal, headers, auth or body.
    const clean = info === undefined ? undefined : Object.fromEntries([
      'attemptId', 'providerAttemptId', 'sessionId', 'requestId', 'operation', 'provider', 'model', 'api',
      'transport', 'destination', 'outcome', 'status', 'usage', 'stopReason', 'physicalAttemptIds',
    ].filter(key => info[key] !== undefined).map(key => [key, info[key]]));
    let output: string;
    // Ordinary private file descriptors work where Node's socket-backed
    // stdio pipes are unavailable. No sockets or credential payloads involved.
    const exchange = fs.mkdtempSync(path.join(tmpdir(), 'biomind-canary-ipc-'));
    fs.chmodSync(exchange, 0o700);
    const fds: number[] = [];
    try {
      const input = JSON.stringify({ action, taskId, scenario: options.scenario, info: clean });
      if (Buffer.byteLength(input) > 65536) throw new Error('CANARY_LEDGER_OVERSIZED_REQUEST');
      fs.writeFileSync(path.join(exchange, 'in'), input, { mode: 0o600 });
      fds.push(fs.openSync(path.join(exchange, 'in'), 'r'),
        fs.openSync(path.join(exchange, 'out'), 'wx', 0o600), fs.openSync(path.join(exchange, 'err'), 'wx', 0o600));
      const result = spawnSync(python!, ['-B', path.join(runtime, 'canary_ledger.py'), options.ledgerPath], {
        env: { PATH: path.dirname(python!) + ':/usr/bin:/bin', LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8' },
        stdio: fds as [number, number, number], timeout: 15000,
      });
      if (fs.statSync(path.join(exchange, 'out')).size > 1024 * 1024) throw new Error('CANARY_LEDGER_OVERSIZED_RESULT');
      output = fs.readFileSync(path.join(exchange, 'out'), 'utf8');
      if (result.error || result.signal || result.status !== 0) {
        throw Object.assign(new Error('CANARY_LEDGER_UNAVAILABLE'), { stdout: output });
      }
    } catch (error: any) {
      let code = 'CANARY_LEDGER_UNAVAILABLE';
      try { code = JSON.parse(String(error.stdout)).error ?? code; } catch { /* no raw child errors */ }
      throw new Error(code);
    } finally {
      for (const fd of fds) fs.closeSync(fd);
      fs.rmSync(exchange, { recursive: true, force: true });
    }
    const result = JSON.parse(output);
    if (result.ok !== true) throw new Error(result.error ?? 'CANARY_LEDGER_FAILED');
    return result.summary;
  }
  call('init');
  return { beforeSend: info => { call('reserve', info); },
    onSendResult: info => { call('send_result', info); },
    onProviderResult: info => { call('provider_result', info); }, summary: () => call('summary') };
}

export function canaryBudgetFromEnv(): CanaryHooks | undefined {
  const ledgerPath = process.env.BIOMIND_PI_CANARY_LEDGER;
  const scenario = process.env.BIOMIND_PI_CANARY_SCENARIO;
  if (!ledgerPath && !scenario) return undefined;
  if (!ledgerPath || !scenario) throw new Error('INCOMPLETE_CANARY_CONFIGURATION');
  // The real migration task has one fixed ledger across process/session/scenario
  // restarts; output fixture directories can never implicitly reset its budget.
  if (path.resolve(ledgerPath) !== canonicalCanaryLedger) throw new Error('CANARY_NONCANONICAL_LEDGER');
  return createCanaryBudgetHooks({ ledgerPath: canonicalCanaryLedger,
    scenario: scenario as CanaryOptions['scenario'] });
}
