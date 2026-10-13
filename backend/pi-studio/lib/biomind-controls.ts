import { BioMindInputError, readBioMindState, scopeFromState, type BioMindProfile, type BioMindScope, type BioMindState } from './biomind-client';

export interface BioMindCapability { supported: boolean; available: boolean; reason?: string; scope?: string }
export interface BioMindModels {
  models: Array<{ provider: string; id: string; name: string; input?: string[]; reasoning?: boolean; authenticated?: boolean }>;
  current: { provider: string; id: string } | null;
  thinkingLevel: string; availableThinkingLevels: string[]; source: string;
}
export interface BioMindTools {
  registered: Array<{ name: string; description?: string }>;
  allowed: string[]; selected: string[] | null; effective: string[]; scope: string;
}
export interface BioMindInstructions {
  scope: string; source: string; path: string; text: string; exists: boolean;
  version: string; hash: string; saved?: boolean; loaded?: boolean | null; effect?: string;
}
export interface BioMindResources {
  skills: Array<{ resourceId: string; name: string; description: string; path: string; source: string;
    scope: string; configuredEnabled: boolean; loaded: boolean; disableModelInvocation: boolean }>;
  diagnostics: unknown[]; coreExtensions: unknown[]; projectTrusted: boolean; enableSkillCommands: boolean; selectionScopes: Array<'user' | 'project'>;
}
export interface BioMindCommand { name: string; description: string; source: string; type: string; supported: boolean; reason?: string; menuVisible?: boolean }
export interface BioMindControlFrame<T = Record<string, unknown>> {
  type: string; status?: string; id?: string; data?: T; [key: string]: unknown;
}

/** Capability absence and a temporarily busy owner are different states. */
export function bioMindCapability(state: BioMindState | null | undefined, name: string): BioMindCapability {
  if (!state) return { supported: false, available: false, reason: 'OWNER_NOT_LOADED' };
  const value = state.capabilities?.[name];
  if (value === true) return { supported: true, available: true };
  if (!value || typeof value !== 'object') return { supported: false, available: false, reason: 'UNSUPPORTED_BIOMIND_COMMAND' };
  const cap = value as Partial<BioMindCapability>;
  return { supported: cap.supported === true, available: cap.supported === true && cap.available === true,
    ...(typeof cap.reason === 'string' ? { reason: cap.reason } : {}), ...(typeof cap.scope === 'string' ? { scope: cap.scope } : {}) };
}

export function sameBioMindScope(state: BioMindState, scope: BioMindScope): boolean {
  return state.serverId === scope.serverId && state.sessionId === scope.sessionId && state.ownerGeneration === scope.ownerGeneration
    && (state.project ?? state.cwd) === scope.project;
}

const RESULT_TYPES: Record<string, string> = {
  get_models: 'models_result', set_model: 'model_result', set_thinking_level: 'thinking_result',
  get_tools: 'tools_result', set_tools: 'tools_result', get_commands: 'commands_result',
  get_instructions: 'instructions_result', save_instructions: 'instructions_result',
  get_prompt_snapshot: 'prompt_snapshot_result', get_resources: 'resources_result', set_skill: 'resources_result',
  set_auto_compaction: 'auto_compaction_result', settings: 'settings_result', reload: 'reload_result',
  compact: 'compact_result', abort: 'abort_result',
};

/** Exactly one POST, no retry. A timeout is unknown; callers may only read back. */
export async function sendBioMindControl<T = Record<string, unknown>>(
  scope: BioMindScope, command: Record<string, unknown>, options: { fetcher?: typeof fetch; current?: () => boolean } = {},
): Promise<BioMindControlFrame<T>> {
  const underlyingFetch = options.fetcher ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ['compact', 'reload'].includes(String(command.type)) ? 135_000 : 45_000);
  const fetcher: typeof fetch = (input, init) => underlyingFetch(input, { ...init, signal: controller.signal });
  try {
    const current = () => !options.current || options.current();
    if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED');
    const before = await readBioMindState(scope.sessionId, fetcher);
    if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED');
    if (!sameBioMindScope(before, scope)) throw new BioMindInputError('OWNER_CHANGED_STATUS_UNKNOWN', true);
    let response: Response;
    try {
      response = await fetcher(`/api/agent/${encodeURIComponent(scope.sessionId)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...command, ...scope }),
      });
    } catch { throw new BioMindInputError('CONTROL_STATUS_UNKNOWN', true); }
    const body = await response.json().catch(() => null);
    if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED', true);
    if (!response.ok || body?.error) throw new BioMindInputError(body?.code ?? body?.error ?? 'CONTROL_STATUS_UNKNOWN', body?.rejected !== true);
    if (!body?.success || !body.data || typeof body.data.type !== 'string') throw new BioMindInputError('CONTROL_RECEIPT_UNKNOWN', true);
    const frame = body.data as BioMindControlFrame<T>;
    const expectedType = RESULT_TYPES[String(command.type)];
    if (!expectedType || frame.type !== expectedType || frame.status === 'error') throw new BioMindInputError('CONTROL_RECEIPT_INVALID', true);
    for (const key of ['serverId', 'sessionId', 'ownerGeneration', 'project'] as const) {
      if (frame[key] !== undefined && frame[key] !== scope[key]) throw new BioMindInputError('REMOTE_SCOPE_MISMATCH', true);
    }
    return frame;
  } finally { clearTimeout(timer); }
}

export function controlData<T>(frame: BioMindControlFrame<T>): T {
  if (!frame.data || typeof frame.data !== 'object') throw new BioMindInputError('CONTROL_RECEIPT_INVALID', true);
  return frame.data;
}

export async function ownerScope(profile: BioMindProfile, sessionId: string, project: string): Promise<{ state: BioMindState; scope: BioMindScope }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    const state = await readBioMindState(sessionId, (input, init) => fetch(input, { ...init, signal: controller.signal }));
    return { state, scope: scopeFromState(profile, state, sessionId, project) };
  } finally { clearTimeout(timer); }
}

/** Notify other views in this browser; they re-read the owner rather than trusting UI values. */
export function notifyBioMindControl(scope: BioMindScope): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('biomind-control-changed', { detail: scope }));
}

/** A missing receipt never proves failure or idle; only a fresh state can prove busy. */
export function compactFailureOutcome(error: unknown, observedState?: BioMindState | null) {
  const uncertain = !(error instanceof BioMindInputError) || error.uncertain;
  return { status: uncertain ? 'unknown' as const : 'not_completed' as const,
    busy: observedState ? observedState.controlBusy === 'compact' : uncertain ? null : false };
}
