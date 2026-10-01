/** Host-only startup selection. This file never reads project settings or chat. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

export type ExecutionProfile = 'native' | 'protected';
export const DEFAULT_EXECUTION_PROFILE: ExecutionProfile = 'protected';
export function executionProfile(value: unknown): ExecutionProfile {
  if (value !== 'native' && value !== 'protected') throw new Error('EXECUTION_PROFILE_INVALID');
  return value;
}
export function sessionProfile(dataRoot: string, sessionId: string): ExecutionProfile {
  const task = 'pi-' + createHash('sha256').update(sessionId).digest('hex').slice(0, 24);
  const filename = path.join(dataRoot, 'task_outputs', task, 'plan_state.json');
  if (!fs.existsSync(filename)) return 'protected'; // Legacy session, no implicit downgrade.
  const binding = JSON.parse(fs.readFileSync(filename, 'utf8')).task_state?.pi_execution_profile;
  if (!binding) return 'protected';
  if (binding.schema !== 1 || binding.session_id !== sessionId) throw new Error('EXECUTION_PROFILE_METADATA_UNSUPPORTED');
  return executionProfile(binding.profile);
}
export function chooseExecutionProfile(options: { cli?: unknown; configured?: unknown; environment?: unknown;
  forced?: unknown; previous?: ExecutionProfile; migrate?: boolean }) {
  const requested = options.cli ?? options.configured ?? options.environment;
  const selected = options.forced ?? requested ?? options.previous ?? DEFAULT_EXECUTION_PROFILE;
  const profile = executionProfile(selected);
  if (options.forced !== undefined && requested !== undefined && executionProfile(requested) !== profile)
    throw new Error('EXECUTION_PROFILE_FORCED_POLICY_CONFLICT');
  if (options.previous && options.previous !== profile && !options.migrate)
    throw new Error(`EXECUTION_PROFILE_RESTART_REQUIRED: session=${options.previous}; use an explicit new-process migration`);
  if (options.migrate && options.previous === undefined) throw new Error('EXECUTION_PROFILE_MIGRATION_REQUIRES_SESSION');
  return Object.freeze({ profile, source: options.forced !== undefined ? 'deployment' : options.cli !== undefined ? 'cli'
    : options.configured !== undefined ? 'runtime-config' : options.environment !== undefined ? 'user-environment'
    : options.previous ? 'session-binding' : 'product-default' });
}
