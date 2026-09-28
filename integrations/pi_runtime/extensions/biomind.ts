/** BioMind's sole Pi extension. Native CLI, SDK and tests load this module. */
import { nativeRead } from '../native-read.js';
import { nativeMutation } from '../native-mutation.js';
import { filterNativeContextFiles, currentNativeProjectContext } from '../native-context.js';
import path from 'node:path';
import { realpath } from 'node:fs/promises';
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, getAgentDir } from '@earendil-works/pi-coding-agent';
import { randomUUID } from 'node:crypto';
import { configuredProvider, validateModelSelection } from '../model-config.js';
import { Type, type TSchema } from 'typebox';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import type { ExtensionAPI, ExtensionContext, SessionEntry, ToolDefinition } from '@earendil-works/pi-coding-agent';
import { buildSessionProjection } from '@earendil-works/pi-coding-agent';
import { createDomainClient, DomainHandshakeTimeoutError, type DomainOptions } from '../domain-client.js';
import { canaryTools, installUsageObservation, isCanaryMode } from '../usage-observer.js';
import { toolRenderers, receiptDetails, projectToolReceipt, displayPreview } from '../tool-display.js';
import { installBioMindProductUI } from '../product-ui.js';
import { formatApprovalProposal, selectApprovalProposal } from '../approval-display.js';

export interface DomainConnection {
  readonly startupFailed?: boolean;
  call(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  hostCall(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  close(options: { disconnectOnly: true }): void | Promise<void>;
}
export interface BioMindExtensionOptions {
  domainFactory?: (options: DomainOptions) => DomainConnection;
}
export interface BioMindUIRequest {
  id: string;
  requestId: string;
  sessionId: string;
  kind: 'question' | 'approval';
  question: string;
  toolCallId: string;
  choices?: string[];
  proposal?: Record<string, unknown>;
}
export interface BioMindUIResponse {
  id: string;
  requestId: string;
  sessionId: string;
  answer?: string;
  choice?: 'y' | 'n' | 'scope' | 'edit' | 'cancel';
  replacement?: Record<string, unknown>;
  cancelled?: boolean;
}
export interface RequestState {
  requestId: string;
  role: 'Consult' | 'Generate';
  status: 'active' | 'settled' | 'aborted' | 'error';
  handoff?: { toolCallId: string; goal: string; manifest?: string };
  handoffConsumed: boolean;
  plan?: Array<{ step: string; status: 'pending' | 'in_progress' | 'completed' }>;
  planSources?: string[];
  maintenance?: { consumed: boolean; consent?: boolean; outboxId?: string; prompt?: string; sources?: unknown; applied?: boolean };
}
interface Snapshot {
  preferences?: unknown;
  authorization_refs?: unknown[];
  history_enabled: boolean;
  history_sources?: string[];
  revoked_sources?: string[];
  revision?: string;
  diagnostics?: unknown;
  memory_mode?: string;
  maintenance?: { enabled?: boolean };
  archive_enabled?: boolean;
}
interface Provenance { entryId: string; sources: string[] }
const REQUEST = 'biomind.request';
const PROVENANCE = 'biomind.provenance';
const COMPACTION = 'biomind.compaction_sources';
const EMPTY = Type.Object({}, { additionalProperties: false });
const OBJECT = <T extends Record<string, TSchema>>(properties: T) => Type.Object(properties, { additionalProperties: false });
const nonempty = () => Type.String({ minLength: 1 });
const optionalString = () => Type.Optional(Type.String());
const asObject = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : [];
const unique = (values: string[]) => [...new Set(values)];
const customEntries = (branch: readonly SessionEntry[], name: string) => branch.filter(
  (entry): entry is Extract<SessionEntry, { type: 'custom' }> => entry.type === 'custom' && entry.customType === name);
function sourceIds(value: unknown): string[] {
  const object = asObject(value);
  return unique([...strings(object.source_ids), ...strings(object.sources), ...strings(object.history_sources), ...strings(asObject(object.provenance).sources)]);
}
export function requestFromBranch(branch: readonly SessionEntry[]): RequestState | undefined {
  return customEntries(branch, REQUEST).at(-1)?.data as RequestState | undefined;
}
function provenanceFromBranch(branch: readonly SessionEntry[]): Map<string, string[]> {
  return new Map(customEntries(branch, PROVENANCE)
    .map(entry => { const item = entry.data as Provenance; return [item.entryId, item.sources]; }));
}
function fingerprint(message: unknown): string { return JSON.stringify(message); }
function textOf(message: unknown): string {
  const content = asObject(message).content;
  return typeof content === 'string' ? content : Array.isArray(content) ? content.filter(item => item.type === 'text').map(item => item.text).join('\n') : '';
}
/** Source-aware projection only. Pi remains owner of all persisted messages. */
export function projectHistory(messages: AgentMessage[], branch: readonly SessionEntry[], snapshot: Snapshot, restoreCompacted = true): AgentMessage[] {
  const provenance = provenanceFromBranch(branch);
  const invalid = (sources: string[]) => sources.length > 0 && (!snapshot.history_enabled || sources.some(source => (snapshot.revoked_sources ?? []).includes(source)
    || source === 'unknown-history' && (snapshot.revoked_sources?.length ?? 0) > 0));
  // Preserve parent links while omitting summary contributions. Filtering the
  // raw entry array would leave children pointing at a missing compaction node.
  const ordinaryProjection = buildSessionProjection(branch.map(entry => entry.type === 'compaction'
    ? { type: 'custom' as const, id: entry.id, parentId: entry.parentId, timestamp: entry.timestamp, customType: 'biomind.omitted_compaction' }
    : entry));
  const byMessage = new Map([...ordinaryProjection.entries, ...buildSessionProjection([...branch]).entries].flatMap(entry => entry.messages.map(message =>
    [fingerprint(message), provenance.get(entry.sourceEntry.id) ?? sourceIds(asObject(message).details)] as const)));
  const latestCompaction = branch.filter(entry => entry.type === 'compaction').at(-1);
  const compactSources = customEntries(branch, COMPACTION)
    .map(entry => asObject(entry.data)).findLast(entry => entry.entryId === latestCompaction?.id)?.sources;
  // Unknown/mixed summaries cannot establish their provenance. Rebuild the active
  // branch's ordinary messages if history is off or a recorded source is withdrawn.
  const invalidateSummary = Boolean(latestCompaction) && (((!snapshot.history_enabled || (snapshot.revoked_sources?.length ?? 0) > 0) && !compactSources) || invalid(strings(compactSources)));
  const input: AgentMessage[] = invalidateSummary && restoreCompacted
    ? ordinaryProjection.messages.filter(message => message.role !== 'system')
    : messages;
  return input.map(message => {
    if (message.role === 'user' || message.role === 'system') return message;
    if (message.role === 'compactionSummary' && invalidateSummary
      || message.role === 'branchSummary' && (!snapshot.history_enabled || (snapshot.revoked_sources?.length ?? 0) > 0)) {
      return { ...message, summary: '[历史摘要已按当前来源策略撤回]' } as AgentMessage;
    }
    const sources = byMessage.get(fingerprint(message)) ?? sourceIds(asObject(message).details);
    if (!invalid(sources)) return message;
    const cloned = structuredClone(message) as any;
    if (cloned.role === 'assistant') {
      // Keep only the protocol identities. Historical tool arguments can contain
      // copied memory too, so their body is withdrawn together with derived text.
      cloned.content = cloned.content.filter((part: any) => part.type === 'toolCall')
        .map((part: any) => ({ ...part, arguments: {} }));
      if (!cloned.content.length) cloned.content = [{ type: 'text', text: '[已按当前历史读取政策撤回来源派生内容]' }];
    } else if (cloned.role === 'toolResult') {
      const facts = asObject(asObject(cloned.details).execution_facts);
      cloned.content = [{ type: 'text', text: JSON.stringify({ history_content: 'withdrawn', ...facts }) }];
      cloned.details = { execution_facts: facts, history_content: 'withdrawn' };
    } else if (cloned.role === 'custom') cloned.content = '[已撤回历史来源内容]';
    return cloned;
  });
}

const READ_TOOLS = ['ask_user', 'handoff_generate', 'select_environment', 'inspect_run', 'wait_run', 'read_run_log', 'cancel_run', 'file_stat', 'read', 'memory_search', 'memory_read'];
const GENERATE_TOOLS = [...READ_TOOLS.filter(name => name !== 'handoff_generate'), 'update_plan', 'run_command', 'install_tool', 'write', 'edit', 'memory_write'];
const CONSULT_RULES = `当前阶段：Consult。普通咨询可直接答复并结束本次请求；需要执行时调用 handoff_generate 一次，由同一会话继续进入 Generate。`;
const GENERATE_RULES = `当前阶段：Generate。交接已经完成，你现在负责执行原用户任务。依据当前请求的 goal、manifest、原始输入及工具回执，使用可用工具继续推进已获授权的工作。
不能以计划摘要、已交接说明或将来会执行的承诺代替实际推进；没有另一个后台代理会替你自动执行。交接中的陈述仍需按证据核实。
按真实状态汇报：已完成、确有阻塞、需用户选择或用户停止。已有证据足以说明完成或阻塞时可以直接说明；缺少关键选择用 ask_user 等待回答。用户明确只要计划、要求等待或停止时遵从其要求。`;
const MAINTENANCE_RULES = `当前阶段：记忆维护。仅处理本次已完成对话的记忆维护；不继续或启动分析任务。仅在尚未提交时使用 maintenance_submit 提交有当前证据支持的 patch 或空 operations；提交后核对实际回执并结束。`;
const HANDOFF_INSTRUCTION = '仅当 request_id 与当前有效请求一致且当前阶段为 Generate 时：Consult 已完成交接，现在由你继续执行原用户任务。遵守用户最新决定与授权范围，使用工具推进 goal；不能只回复已交接或将执行的计划摘要。';
const DOMAIN_RULES = `你是 BioMind 生信助手。缺少关键选择时用 ask_user 等待真实回答。仅推进当前有效请求；历史交接不自动重跑，用户最新的修改、等待或停止要求优先。
根据实际工具回执判断科学进展。update_plan 只是可选笔记；不要求先计划，不用 Step/Gate/Commit 或必须产物判定。
已注册环境按显式注册名直接使用；只有实际依赖错误才诊断。安装需实际授权，偏好不等于授权。长作业观察现有 run_ref，不重复启动，不用模型轮询充当界面心跳。
多样本传 TXT/TSV/CSV 清单路径，执行时读取。保留明确工作目录与合法 argv/shell 调用。退出码、文件存在或 agent_settled 不代表科学结论。
Skills 由 Pi 原生目录发现，用 read 按实际 Skill 路径读取正文及相对其 baseDir 的 references/scripts；/skill:name 是用户显式调用。read 的 offset/limit 按行计数。脚本资源路径相对 Skill，输入输出和工作目录仍属于分析项目。
输出只引用真实文件路径；纯咨询无需文件。禁止假造产物或把服务器路径当成客户端已下载文件。`;

export function createBioMindExtension(options: BioMindExtensionOptions = {}) {
  // Only transport references survive resource reload. Pi keeps all request,
  // role, plan and transcript state on its current session branch.
  const registryKey = Symbol.for('biomind.pi.domain-connections.v1');
  const registryHost = globalThis as typeof globalThis & { [registryKey]?: Map<string, DomainConnection> };
  const connections = options.domainFactory ? new Map<string, DomainConnection>()
    : (registryHost[registryKey] ??= new Map<string, DomainConnection>());
  return function biomind(pi: ExtensionAPI): void {
    // Native CLI registers the same selection inside its own process. JSON
    // mode registers directly on its ModelRuntime and does not set this field.
    const selectionText = process.env.BIOMIND_PI_MODEL_SELECTION;
    if (selectionText) {
      const selection = validateModelSelection(JSON.parse(selectionText));
      const provider = configuredProvider(selection);
      if (provider) pi.registerProvider(selection.provider, provider);
    }
    const setActiveTools = (names: readonly string[]) => pi.setActiveTools(canaryTools(names));
    const clients = connections;
    const pendingUI = new Map<string, { request: BioMindUIRequest; resolve: (response: BioMindUIResponse) => void; reject: (error: Error) => void }>();
    const inputQueue: Array<{ text: string; kind: 'steer' | 'followUp' }> = [];
    // Versions of bytes actually read/written in this request. Approval tokens
    // stay in the domain authority; this map carries no approval capability.
    const readVersions = new Map<string, string>();
    let initialUserPending = false;
    let preparedInputRequestId: string | undefined;
    let activeContext: ExtensionContext | undefined;
    let snapshot: Snapshot = { history_enabled: false };
    let exposedSources: string[] = [];
    let compactionSources: string[] = [];
    let lastOutcome: 'completed' | 'aborted' | 'error' = 'completed';
    // Delivery bookkeeping only; messages and stable entry identities stay in Pi.
    // The original archive writer owns deduplication and actual persistence.
    const archiveSubmitted = new Set<string>();
    const state = (ctx: ExtensionContext) => requestFromBranch(ctx.sessionManager.getBranch());
    installBioMindProductUI(pi, state);
    installUsageObservation(pi, state);
    const sessionId = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId();
    const publish = (ctx: ExtensionContext, value: RequestState) => {
      pi.appendEntry(REQUEST, value);
      pi.events.emit('biomind:state', { ...value, sessionId: sessionId(ctx) });
      ctx.ui.setStatus('biomind', `${value.role} · ${value.status}`);
    };
    const client = (ctx: ExtensionContext) => {
      const id = sessionId(ctx);
      let value = clients.get(id);
      if (!value) {
        value = (options.domainFactory ?? createDomainClient)({ cwd: ctx.cwd, sessionId: id,
          python: process.env.BIOMIND_PI_PYTHON, dataRoot: process.env.BIOMIND_PI_DATA_ROOT,
          installRoot: process.env.BIOMIND_PI_INSTALL_ROOT,
          registryPath: process.env.BIOMIND_PI_REGISTRY_PATH, skillsRoot: process.env.BIOMIND_PI_SKILLS_ROOT,
          preferencesPath: process.env.BIOMIND_PI_PREFERENCES_PATH, memoryRoot: process.env.BIOMIND_PI_MEMORY_ROOT,
          wikiRoot: process.env.BIOMIND_PI_WIKI_ROOT, historyEnabled: process.env.BIOMIND_PI_HISTORY_ENABLED === '1' });
        clients.set(id, value);
      }
      return value;
    };
    const begin = async (ctx: ExtensionContext) => {
      const value: RequestState = { requestId: randomUUID(), role: 'Consult', status: 'active', handoffConsumed: false };
      const ownerSession = sessionId(ctx);
      let connection: DomainConnection | undefined;
      try {
        connection = client(ctx);
        await connection.hostCall('request.begin', { request_id: value.requestId, role: 'consult' });
      }
      catch (error) {
        if (connection?.startupFailed && clients.get(ownerSession) === connection) {
          clients.delete(ownerSession);
          // Release only the failed startup transport. The next user input
          // creates a fresh service; never replay the request automatically.
          try { await connection.close({ disconnectOnly: true }); }
          catch { pi.events.emit('biomind:diagnostic', { code: 'DOMAIN_STARTUP_DISCONNECT_FAILED' }); }
        }
        // An unaccepted identity must never replace the branch's accepted owner.
        // In particular, a live job can reject a new request and still need cancellation.
        lastOutcome = 'error'; setActiveTools([]); throw error;
      }
      publish(ctx, value); setActiveTools(READ_TOOLS); exposedSources = []; archiveSubmitted.clear(); readVersions.clear(); lastOutcome = 'completed';
      return value;
    };
    const current = (ctx: ExtensionContext) => {
      const value = state(ctx);
      if (!value || value.status !== 'active') throw new Error('No active BioMind request');
      return value;
    };
    const archiveTurn = async (ctx: ExtensionContext) => {
      if (!snapshot.archive_enabled) return;
      const value = state(ctx); if (!value) return;
      const branch = ctx.sessionManager.getBranch();
      const start = branch.findIndex(entry => entry.type === 'custom' && entry.customType === REQUEST && asObject(entry.data).requestId === value.requestId);
      if (start < 0) return;
      for (const entry of branch.slice(start + 1)) {
        if (entry.type !== 'message' || !['user', 'assistant', 'toolResult'].includes(entry.message.role)) continue;
        const eventId = `pi:${sessionId(ctx)}:${entry.id}`;
        if (archiveSubmitted.has(eventId)) continue;
        const message = entry.message;
        const publicText = textOf(message);
        // Thinking blocks, provider credentials and full transport messages are
        // never archival content. Tool receipts keep their public text and IDs.
        const content = publicText.slice(0, 64000);
        try {
          const receipt = asObject(await client(ctx).hostCall('archive.append', {
            event_id: eventId, event_type: message.role === 'user' ? 'user_message'
              : message.role === 'assistant' ? 'assistant_message' : 'tool_result',
            stage: value.role.toLowerCase(), content,
            source_refs: [eventId], metadata: { pi_entry_id: entry.id, pi_parent_id: entry.parentId,
              pi_request_id: value.requestId, projection_truncated: publicText.length > content.length,
              ...(message.role === 'toolResult' ? { tool_name: message.toolName, tool_call_id: message.toolCallId, is_error: message.isError } : {}) },
          }, { requestId: value.requestId }));
          if (['enqueued', 'coalesced'].includes(String(receipt.status))) archiveSubmitted.add(eventId);
          pi.events.emit('biomind:archive', { eventId, ...receipt });
        } catch {
          pi.events.emit('biomind:diagnostic', { code: 'ARCHIVE_ENQUEUE_FAILED' });
        }
      }
    };
    const applySettings = async (ctx: ExtensionContext, patch: Record<string, unknown>) => {
      for (const key of Object.keys(patch)) if (!['history_enabled', 'memory_mode'].includes(key)) throw new Error('Unsupported memory setting');
      if ('history_enabled' in patch && typeof patch.history_enabled !== 'boolean') throw new Error('history_enabled must be boolean');
      if ('memory_mode' in patch && !['ask', 'auto', 'manual', 'disabled'].includes(String(patch.memory_mode))) throw new Error('Unsupported memory mode');
      const result = asObject(Object.keys(patch).length ? await client(ctx).hostCall('settings.update', patch) : await client(ctx).call('context.snapshot', {}));
      if (typeof result.history_enabled !== 'boolean') throw new Error('Invalid settings receipt');
      snapshot = result as Snapshot;
      return { history_enabled: result.history_enabled, memory_mode: result.memory_mode, revision: result.revision };
    };
    pi.registerCommand('biomind-memory', {
      description: '记忆设置：status | history on/off | mode ask/auto/manual/disabled',
      async handler(args, ctx) {
        const parts = args.trim().split(/\s+/); let patch: Record<string, unknown> = {};
        try {
          if (!args.trim() || parts[0] === 'status' && parts.length === 1) patch = {};
          else if (parts[0] === 'history' && parts.length === 2 && ['on', 'off'].includes(parts[1])) patch = { history_enabled: parts[1] === 'on' };
          else if (parts[0] === 'mode' && parts.length === 2) patch = { memory_mode: parts[1] };
          else throw new Error('用法：/biomind-memory status | history on/off | mode ask/auto/manual/disabled');
          const result = await applySettings(ctx, patch); ctx.ui.notify(`历史读取：${result.history_enabled ? '开' : '关'}；记忆模式：${result.memory_mode ?? '未配置'}`, 'info');
          pi.events.emit('biomind:settings_result', { status: 'ok', sessionId: sessionId(ctx), ...result });
        } catch (error) { ctx.ui.notify(String(error), 'error'); pi.events.emit('biomind:settings_result', { status: 'error', code: 'SETTINGS_UPDATE_FAILED', message: String(error) }); }
      },
    });
    pi.events.on('biomind:settings_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const patch = Object.fromEntries(Object.entries(request).filter(([key]) => key !== 'id'));
        const result = await applySettings(activeContext, patch);
        pi.events.emit('biomind:settings_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), ...result });
      } catch (error) { pi.events.emit('biomind:settings_result', { id: request.id, status: 'error', code: 'SETTINGS_UPDATE_FAILED', message: String(error) }); }
    });
    const observeJob = async (ctx: ExtensionContext, input: Record<string, unknown>) => {
      const action = String(input.action ?? '');
      if (!['list', 'status', 'logs', 'cancel'].includes(action)) throw new Error('Unknown job action');
      const requestId = state(ctx)?.requestId;
      if (action === 'cancel' && !requestId) throw new Error('Job cancellation requires a BioMind request on the current branch');
      if (action === 'list') return client(ctx).call('session.lifecycle', { action: 'inspect' }, { requestId });
      if (typeof input.run_ref !== 'string' || !input.run_ref.trim()) throw new Error('run_ref is required');
      if (input.after_cursor !== undefined && typeof input.after_cursor !== 'string') throw new Error('after_cursor must be an opaque string');
      const method = action === 'status' ? 'exec.status' : action === 'logs' ? 'exec.read_log' : 'exec.cancel';
      return client(ctx).call(method, { run_ref: input.run_ref, ...(action === 'logs' && input.after_cursor ? { after_cursor: input.after_cursor } : {}),
        ...(action === 'cancel' ? { reason: typeof input.reason === 'string' ? input.reason : 'Explicit user job cancellation' } : {}) }, { requestId });
    };
    pi.registerCommand('biomind-job', {
      description: '不调用模型直接观察作业：list | status <run_ref> | logs <run_ref> [cursor] | cancel <run_ref>',
      async handler(args, ctx) {
        const parts = args.trim().split(/\s+/);
        try {
          if (parts.length > (parts[0] === 'logs' ? 3 : parts[0] === 'list' ? 1 : 2)) throw new Error('Unexpected job command arguments');
          const result = await observeJob(ctx, { action: parts[0], run_ref: parts[1], after_cursor: parts[2] });
          ctx.ui.notify(displayPreview(result), 'info'); pi.events.emit('biomind:job_result', { status: 'ok', sessionId: sessionId(ctx), result });
        } catch (error) { ctx.ui.notify(String(error), 'error'); pi.events.emit('biomind:job_result', { status: 'error', code: 'JOB_REQUEST_FAILED', message: String(error) }); }
      },
    });
    pi.events.on('biomind:job_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const result = await observeJob(activeContext, request);
        pi.events.emit('biomind:job_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), result });
      } catch (error) { pi.events.emit('biomind:job_result', { id: request.id, status: 'error', code: 'JOB_REQUEST_FAILED', message: String(error) }); }
    });
    const reply = (value: unknown, extra: Record<string, unknown> = {}) => {
      const projected = projectToolReceipt(value);
      const object = asObject(projected);
      const execution_facts = Object.fromEntries(['run_ref', 'status', 'exit_code', 'log_path', 'log_cursor', 'next_cursor', 'request_scope', 'call_id',
        'returncode', 'terminal', 'log_refs', 'receipt_ref', 'cancel_requested', 'stop_requested', 'task_cancelled', 'approval', 'approval_id']
        .filter(key => object[key] !== undefined).map(key => [key, object[key]]));
      return { content: [{ type: 'text' as const, text: JSON.stringify(projected) }], details: { ...receiptDetails(object), execution_facts, ...extra } };
    };
    const rejectPending = (reason: string, targetSession?: string) => {
      for (const [id, waiter] of pendingUI) if (!targetSession || waiter.request.sessionId === targetSession) {
        pendingUI.delete(id); waiter.reject(new Error(reason));
      }
    };
    pi.events.on('biomind:ui_response', (raw: unknown) => {
      const response = asObject(raw) as BioMindUIResponse;
      const waiter = pendingUI.get(response.id);
      if (!waiter || response.requestId !== waiter.request.requestId || response.sessionId !== waiter.request.sessionId) {
        pi.events.emit('biomind:diagnostic', { code: 'STALE_OR_UNKNOWN_UI_RESPONSE', id: response.id }); return;
      }
      pendingUI.delete(response.id); waiter.resolve(response);
    });
    const approvalOptions = (proposal: Record<string, unknown>) => {
      if (!Array.isArray(proposal.allowed_decisions)) throw new Error('Authorization did not specify allowed decisions');
      const offered = new Set(proposal.allowed_decisions.filter((value): value is string => typeof value === 'string'));
      // The current permission service explicitly supports installation editing
      // before checking its stored y/n list; other proposal kinds cannot edit.
      if (asObject(proposal.payload).kind === 'installation') offered.add('edit');
      const options = [
        { choice: 'n', label: '拒绝' }, { choice: 'y', label: '明确同意执行此提案' },
        { choice: 'scope', label: '明确授权提案中列出的目录范围内文件工具写入' },
        { choice: 'edit', label: '编辑后重新确认' }, { choice: 'cancel', label: '取消' },
      ].filter(option => option.choice === 'cancel' || offered.has(option.choice));
      return options[0]?.choice === 'n' ? options : [options.find(option => option.choice === 'cancel')!, ...options.filter(option => option.choice !== 'cancel')];
    };
    async function ask(ctx: ExtensionContext, kind: 'question' | 'approval', toolCallId: string, question: string,
      signal?: AbortSignal, proposal?: Record<string, unknown>, choices?: string[]): Promise<BioMindUIResponse> {
      const approval = kind === 'approval' ? approvalOptions(proposal ?? {}) : [];
      if (kind === 'approval') choices = approval.map(option => option.label);
      const request: BioMindUIRequest = { id: randomUUID(), requestId: current(ctx).requestId, sessionId: sessionId(ctx), kind, question, toolCallId, proposal, choices };
      signal?.throwIfAborted();
      if (ctx.hasUI) {
        if (kind === 'question') {
          const answer = choices?.length ? await ctx.ui.select(question, choices, { signal }) : await ctx.ui.input(question, undefined, { signal });
          return { ...request, answer, cancelled: answer === undefined };
        }
        // A blank Enter chooses the first (deny) item, never approval.
        const answer = await selectApprovalProposal(ctx.ui, proposal ?? {}, choices!, signal);
        const choice = (approval.find(option => option.label === answer)?.choice ?? 'cancel') as NonNullable<BioMindUIResponse['choice']>;
        let replacement: Record<string, unknown> | undefined;
        if (choice === 'edit') {
          const edited = await ctx.ui.input('输入修改后的命令；随后需要重新确认', undefined, { signal });
          if (!edited?.trim()) return { ...request, choice: 'cancel', cancelled: true };
          replacement = { cmd: edited };
        }
        return { ...request, choice, replacement, cancelled: choice === 'cancel' };
      }
      return new Promise((resolve, reject) => {
        const abort = () => { pendingUI.delete(request.id); reject(new Error('User interaction aborted; no answer or approval was recorded')); };
        pendingUI.set(request.id, { request,
          resolve: response => { signal?.removeEventListener('abort', abort); resolve(response); },
          reject: error => { signal?.removeEventListener('abort', abort); reject(error); } });
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) { abort(); return; }
        pi.events.emit('biomind:ui_request', request);
        // No timers or model calls are used while waiting for the matching host response.
      });
    }
    const invoke = async (ctx: ExtensionContext, method: string, params: Record<string, unknown>, signal?: AbortSignal) => {
      const request = current(ctx); signal?.throwIfAborted();
      const value = await client(ctx).call(method, params, { signal, requestId: request.requestId });
      signal?.throwIfAborted();
      if (state(ctx)?.requestId !== request.requestId) throw new Error('Late result belongs to an inactive request');
      return value;
    };
    async function withApproval(ctx: ExtensionContext, method: string, params: Record<string, unknown>, toolCallId: string, signal?: AbortSignal) {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const checkOwner = () => {
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_MUTATION_OWNER');
      };
      let value = await invoke(ctx, method, params, signal);
      checkOwner();
      while (asObject(value).status === 'waiting_approval') {
        checkOwner();
        const proposal = asObject(asObject(value).approval);
        if (!proposal.request_id || !proposal.action_hash || !proposal.request_scope) throw new Error('Incomplete authorization object');
        const response = await ask(ctx, 'approval', toolCallId, formatApprovalProposal(proposal), signal, proposal);
        checkOwner();
        const choice = response.cancelled ? 'cancel' : response.choice;
        if (!choice || !approvalOptions(proposal).some(option => option.choice === choice)) throw new Error('Approval requires an explicit offered decision');
        value = await client(ctx).hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
          request_scope: proposal.request_scope, choice, ...(response.replacement ? { replacement: response.replacement } : {}) },
          { signal, requestId: ownerRequest });
        checkOwner();
        if (choice === 'n' || choice === 'cancel') return value;
        if (choice === 'edit') {
          if (asObject(value).status !== 'waiting_approval') return value;
          // Keep the original call identity. Python executes the newly approved
          // object's payload; replaying model/edited arguments must not replace it.
          continue;
        }
        value = await invoke(ctx, method === 'install.propose' ? 'install.execute' : method, params, signal);
        checkOwner();
      }
      return value;
    }
    function register(name: string, description: string, parameters: TSchema,
      execute: (id: string, args: Record<string, any>, signal: AbortSignal | undefined, ctx: ExtensionContext) => Promise<unknown>) {
      pi.registerTool({ name, label: name, description, parameters, executionMode: 'sequential',
        ...toolRenderers(name),
        async execute(id, args, signal, _update, ctx) { return reply(await execute(id, asObject(args), signal, ctx)); },
      } as ToolDefinition);
    }
    // Reuse Pi's schema, renderer, paging, image handling and errors. Only file
    // access is adapted to BioMind's existing domain boundary.
    pi.registerTool({ ...createReadToolDefinition(process.cwd()), executionMode: 'sequential',
      async execute(id, args, signal, update, ctx) {
        const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
        const checkOwner = () => {
          signal?.throwIfAborted();
          if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
        };
        const skillDirs = pi.getCommands().filter(command => command.source === 'skill')
          .map(command => path.dirname(command.sourceInfo.path));
        const result = await nativeRead(ctx.cwd, async absolutePath => {
          checkOwner();
          const trustedSkillDirs: string[] = [];
          let target = absolutePath;
          for (const directory of skillDirs) {
            const canonical = await realpath(directory).catch(() => undefined);
            if (!canonical) continue;
            trustedSkillDirs.push(canonical);
            if (absolutePath.startsWith(directory + path.sep)) {
              const resolved = await realpath(absolutePath);
              if (!resolved.startsWith(canonical + path.sep)) throw new Error('SKILL_RESOURCE_OUTSIDE_ROOT');
              target = resolved;
            }
          }
          const receipt = asObject(await client(ctx).hostCall('files.read_authorize',
            { path: target, trusted_skill_dirs: trustedSkillDirs }, { signal, requestId: ownerRequest }));
          checkOwner();
          return { ...receipt, requested_path: absolutePath };
        }).execute(id, args, signal, update, ctx);
        checkOwner();
        if (result.details?.sha256 && result.details?.write_path) {
          readVersions.set(`${ownerSession}:${ownerRequest}:${result.details.write_path}`, result.details.sha256);
        }
        return result;
      },
    });
    for (const kind of ['write', 'edit'] as const) {
      const definition = kind === 'write' ? createWriteToolDefinition(process.cwd()) : createEditToolDefinition(process.cwd());
      pi.registerTool({ ...definition, executionMode: 'sequential',
        async execute(id, args, signal, update, ctx) {
          const requestId = current(ctx).requestId;
          const ownerSession = sessionId(ctx);
          const checkOwner = () => {
            signal?.throwIfAborted();
            if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== requestId) throw new Error('STALE_MUTATION_OWNER');
          };
          const key = (absolutePath: string) => `${ownerSession}:${requestId}:${absolutePath}`;
          const hostMutation = async (method: string, input: Record<string, unknown>) => {
            checkOwner();
            const result = await client(ctx).hostCall(method, input, { signal, requestId });
            checkOwner(); return asObject(result);
          };
          return nativeMutation(kind, ctx.cwd, {
            replay: input => hostMutation('files.mutation_replay', input),
            snapshot: async absolutePath => await hostMutation('files.mutation_snapshot', { path: absolutePath }) as any,
            commit: async input => { checkOwner(); return asObject(await withApproval(ctx, 'files.write', input, id, signal)); },
            readVersion: absolutePath => { checkOwner(); return readVersions.get(key(absolutePath)); },
            rememberVersion: (absolutePath, hash) => { checkOwner(); readVersions.set(key(absolutePath), hash); },
          }).execute(id, args, signal, update, ctx);
        },
      } as ToolDefinition);
    }
    register('ask_user', '询问关键选择并等待真实用户回答；不猜测答案。', OBJECT({ question: nonempty(), choices: Type.Optional(Type.Array(nonempty())) }),
      async (id, args, signal, ctx) => {
        const response = await ask(ctx, 'question', id, args.question, signal, undefined, args.choices);
        if (response.cancelled || response.answer === undefined) throw new Error('User cancelled the question; no answer available');
        return { question_id: response.id, request_id: response.requestId, answer: response.answer };
      });
    register('handoff_generate', '仅在需要实际执行时明确交接当前请求给 Generate；同一请求只能交接一次。', OBJECT({ goal: nonempty(), manifest: optionalString() }),
      async (id, args, signal, ctx) => {
        signal?.throwIfAborted(); const value = current(ctx);
        if (value.role !== 'Consult' || value.handoffConsumed || value.handoff) throw new Error('This request already has an execution handoff');
        publish(ctx, { ...value, handoff: { toolCallId: id, goal: args.goal, manifest: args.manifest } });
        return { accepted: true, request_id: value.requestId, transition: 'after_tool_results' };
      });
    register('update_plan', '可选的当前分支工作笔记，不是执行许可或必须产物合同。', OBJECT({ steps: Type.Array(OBJECT({ step: nonempty(), status: Type.Union([Type.Literal('pending'), Type.Literal('in_progress'), Type.Literal('completed')]) })) }),
      async (_id, args, _signal, ctx) => { const value = current(ctx); publish(ctx, { ...value, plan: args.steps, planSources: exposedSources }); return { plan: args.steps, required_for_execution: false }; });
    const mapping: Array<[string, string, string, TSchema]> = [
      ['select_environment', 'environments.select', '只读选择已注册运行环境，不重复全量体检。', OBJECT({ requirements: Type.Optional(OBJECT({ env: optionalString(), packages: Type.Optional(Type.Array(Type.String())), tools: Type.Optional(Type.Array(Type.String())), capabilities: Type.Optional(Type.Array(Type.String())) })), skill_id: optionalString(), env_name: optionalString() })],
      ['inspect_run', 'exec.status', '查询已有 run_ref 的真实状态；失败作业不等于查询失败。', OBJECT({ run_ref: nonempty() })],
      ['wait_run', 'exec.wait', '等待已有作业的有限状态变化；界面心跳不需要模型重复调用。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString(), max_wait_ms: Type.Optional(Type.Integer({ minimum: 0, maximum: 30000 })) })],
      ['read_run_log', 'exec.read_log', '分页读取真实日志；游标是服务返回的原始不透明字符串，完整日志留在原文件。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString() })],
      ['cancel_run', 'exec.cancel', '明确取消指定科学作业；与中断当前对话不同。', OBJECT({ run_ref: nonempty(), reason: nonempty() })],
      ['file_stat', 'files.stat', '核对真实文件路径与基本事实。', OBJECT({ path: nonempty() })],
      ['memory_search', 'memory.search', '按当前历史读取开关和项目 scope 查询长期记忆。', OBJECT({ query: nonempty(), source: Type.Optional(Type.Union([Type.Literal('wiki'), Type.Literal('projects'), Type.Literal('archive'), Type.Literal('current')])), scope: optionalString() })],
      ['memory_read', 'memory.read', '读取可用来源；关闭历史时不能读取旧项目历史。', OBJECT({ source_ref: nonempty(), scope: optionalString(), historical: Type.Optional(Type.Boolean()), offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })) })],
    ];
    for (const [name, method, description, schema] of mapping) register(name, description, schema, async (_id, args, signal, ctx) => invoke(ctx, method, args, signal));
    const commandSchema = OBJECT({ cmd: optionalString(), argv: Type.Optional(Type.Array(Type.String(), { minItems: 1 })), workdir: nonempty(), env_name: optionalString(), description: optionalString() });
    register('run_command', '通过领域执行服务启动一次需持久日志/观察/取消的命令并返回 run_ref；当前受控命令入口，支持 shell 或 argv，计划可选。', commandSchema,
      async (id, args, signal, ctx) => withApproval(ctx, 'exec.start', { call_id: id, arguments: args }, id, signal));
    register('install_tool', '提出依赖安装方案并等待原授权对象确认；不会因模型声称同意而执行。', OBJECT({ cmd: nonempty(), env_name: nonempty(), description: nonempty(), packages: Type.Optional(Type.Array(Type.String())), verification_commands: Type.Optional(Type.Array(Type.String())), prefix: optionalString() }),
      async (id, args, signal, ctx) => withApproval(ctx, 'install.propose', { call_id: id, arguments: args }, id, signal));
    register('memory_write', '按原策略写 Wiki 或显式偏好；不把偏好当安装授权。', OBJECT({ target: Type.Optional(Type.Union([Type.Literal('wiki'), Type.Literal('preference')])), operation: nonempty(), path: optionalString(), content: Type.String(), expected_hash: optionalString(), scope: optionalString(), source_refs: Type.Optional(Type.Array(Type.String())) }),
      async (id, args, signal, ctx) => withApproval(ctx, 'memory.write', { call_id: id, ...args }, id, signal));
    register('maintenance_submit', '仅在已启用的记忆维护窗口提交当前证据支持的patch；空operations表示无可保存事实。',
      OBJECT({ operations: Type.Array(Type.Record(Type.String(), Type.Unknown())), summary: optionalString() }),
      async (_id, args, signal, ctx) => {
        const value = current(ctx); const candidate = value.maintenance;
        if (!candidate?.outboxId || candidate.applied) throw new Error('No armed maintenance candidate');
        const result = await client(ctx).hostCall('memory.maintenance.apply', { outbox_id: candidate.outboxId, patch: args }, { signal, requestId: value.requestId });
        signal?.throwIfAborted(); if (state(ctx)?.requestId !== value.requestId) throw new Error('Maintenance result belongs to an inactive request');
        publish(ctx, { ...value, maintenance: { ...candidate, applied: true } }); return result;
      });

    pi.on('session_start', (_event, ctx) => { activeContext = ctx; setActiveTools(state(ctx)?.role === 'Generate' ? GENERATE_TOOLS : READ_TOOLS); });
    pi.on('input', async (event, ctx) => {
      if (ctx.isIdle()) {
        try { preparedInputRequestId = (await begin(ctx)).requestId; }
        catch (error) {
          preparedInputRequestId = undefined;
          pi.events.emit('biomind:diagnostic', { code: 'REQUEST_INITIALIZATION_FAILED', message: String(error),
            ...(error instanceof DomainHandshakeTimeoutError ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: error.timeoutMs } : {}) });
          ctx.ui.notify(error instanceof DomainHandshakeTimeoutError
            ? `BioMind 本地服务启动超时（等待 ${error.timeoutMs / 1000} 秒）；本次请求未发送给模型。请重新提交输入重试。`
            : 'BioMind 领域服务未完成请求初始化；本次请求未发送给模型。', 'error');
          return { action: 'handled' };
        }
      } else if (event.streamingBehavior) inputQueue.push({ text: event.text, kind: event.streamingBehavior });
      return { action: 'continue' };
    });
    pi.on('before_agent_start', async (event, ctx) => {
      event.systemPromptOptions.contextFiles = filterNativeContextFiles(event.systemPromptOptions.contextFiles, ctx.cwd, getAgentDir());
      if (preparedInputRequestId !== state(ctx)?.requestId || state(ctx)?.status !== 'active') await begin(ctx);
      preparedInputRequestId = undefined; initialUserPending = true;
      // prompt() captured these options before this hook. Changing only the
      // registry would let the previous Generate loadout survive this request.
      event.systemPromptOptions.selectedTools = canaryTools(READ_TOOLS);
    });
    pi.on('message_start', async (event, ctx) => {
      if (event.message.role !== 'user') return;
      if (initialUserPending) { initialUserPending = false; return; }
      if (!inputQueue.length) return;
      const text = textOf(event.message);
      // Pi drains steering before follow-up, including equal text in both queues.
      let index = inputQueue.findIndex(item => item.text === text && item.kind === 'steer');
      if (index < 0) index = inputQueue.findIndex(item => item.text === text);
      if (index < 0 && new Set(inputQueue.map(item => item.kind)).size === 1) index = 0;
      if (index < 0) { ctx.abort(); throw new Error('Cannot correlate transformed queued input with steer/follow-up identity'); }
      const [queued] = inputQueue.splice(index, 1);
      if (queued.kind === 'followUp') await begin(ctx);
    });
    pi.on('turn_end', async (event, ctx) => {
      // turn_end follows Pi persistence of the complete tool group, so archive
      // identities are actual SessionManager entry IDs, never guessed offsets.
      await archiveTurn(ctx);
      lastOutcome = event.outcome;
      const sources = unique([...exposedSources, ...event.toolResults.flatMap(result => sourceIds(asObject(result).details))]);
      if (exposedSources.length) pi.appendEntry(PROVENANCE, { entryId: event.messageEntryId, sources: exposedSources });
      event.toolResults.forEach((result, index) => {
        const inherited = ['handoff_generate', 'update_plan'].includes(result.toolName) ? exposedSources : [];
        const toolSources = unique([...inherited, ...sourceIds(asObject(result).details)]);
        if (toolSources.length) pi.appendEntry(PROVENANCE, { entryId: event.toolResultEntryIds[index], sources: toolSources });
      });
      const value = state(ctx);
      if (!value || event.outcome !== 'completed' || ctx.signal?.aborted) return;
      if (!value.handoff || value.handoffConsumed) {
        // Ask while the active agent run still owns a cancellable signal. At
        // agent_before_settle that signal can already be gone in Pi 0.87.1.
        if (value.maintenance || event.toolResults.length || ctx.hasPendingMessages()
          || event.message.role !== 'assistant' || event.message.stopReason !== 'stop') return;
        try {
          const fresh = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
          if (!fresh.history_enabled || fresh.memory_mode !== 'ask' || fresh.maintenance?.enabled !== true) return;
          publish(ctx, { ...value, maintenance: { consumed: true } });
          const answer = await ask(ctx, 'question', `maintenance-consent:${value.requestId}`,
            '是否将本轮可复用的公开事实保存到项目记忆？不保存不会影响本轮答案。', ctx.signal, undefined, ['不保存', '保存本次记忆']);
          if (!answer.cancelled && answer.answer === '保存本次记忆') publish(ctx, { ...value, maintenance: { consumed: false, consent: true } });
        } catch (error) {
          if (ctx.signal?.aborted) lastOutcome = 'aborted';
          else pi.events.emit('biomind:diagnostic', { code: 'MEMORY_CONSENT_UNAVAILABLE', message: String(error) });
        }
        return;
      }
      await client(ctx).hostCall('request.begin', { request_id: value.requestId, role: 'generate' }, { signal: ctx.signal });
      setActiveTools(GENERATE_TOOLS);
      const next: RequestState = { ...value, role: 'Generate', handoffConsumed: true };
      pi.events.emit('biomind:state', { ...next, sessionId: sessionId(ctx) });
      ctx.ui.setStatus('biomind', 'Generate · active');
      return { entries: [{ type: 'custom' as const, customType: REQUEST, data: next },
        { type: 'custom_message' as const, customType: 'biomind.handoff', content: JSON.stringify({ request_id: value.requestId, phase: 'Generate', instruction: HANDOFF_INSTRUCTION, goal: value.handoff.goal, manifest: value.handoff.manifest }), display: true,
          details: { sources } }], continue: true };
    });
    pi.on('context', async (event, ctx) => {
      if (state(ctx)?.status !== 'active') { ctx.abort(); return { messages: [] }; }
      if (ctx.signal?.aborted) return { messages: projectHistory(event.messages, ctx.sessionManager.getBranch(), snapshot) };
      try {
        snapshot = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
        if (typeof snapshot.history_enabled !== 'boolean') throw new Error('Domain context snapshot omitted history policy');
      } catch (error) {
        // ExtensionRunner reports thrown hook errors and can keep the original
        // messages. Explicitly abort so an unavailable policy cannot leak them.
        snapshot = { history_enabled: false }; ctx.abort();
        if (!ctx.signal?.aborted) pi.events.emit('biomind:diagnostic', { code: 'CONTEXT_POLICY_UNAVAILABLE', message: String(error) });
        return { messages: projectHistory(event.messages, ctx.sessionManager.getBranch(), snapshot) };
      }
      const messages = projectHistory(event.messages, ctx.sessionManager.getBranch(), snapshot);
      const branchProvenance = provenanceFromBranch(ctx.sessionManager.getBranch());
      exposedSources = unique(ctx.sessionManager.getBranch().filter(entry => entry.type === 'message').filter(entry => messages.some(message => fingerprint(message) === fingerprint(entry.message)))
        .flatMap(entry => branchProvenance.get(entry.id) ?? sourceIds(asObject(entry.message).details)));
      if (messages.some(message => (message as any).role === 'compactionSummary')) {
        const summarySourceRecord = customEntries(ctx.sessionManager.getBranch(), COMPACTION).at(-1);
        exposedSources = unique([...exposedSources, ...(summarySourceRecord ? strings(asObject(summarySourceRecord.data).sources) : ['unknown-history'])]);
      }
      if (messages.some(message => message.role === 'branchSummary')) exposedSources = unique([...exposedSources, 'unknown-history']);
      return { messages };
    });
    pi.on('context_with_system', (event, ctx) => {
      const value = state(ctx);
      const planWithdrawn = value?.planSources?.some(source => !snapshot.history_enabled || (snapshot.revoked_sources ?? []).includes(source)
        || source === 'unknown-history' && (snapshot.revoked_sources?.length ?? 0) > 0);
      const currentContext = { role: value?.role ?? 'Consult', request_id: value?.requestId, plan: planWithdrawn ? undefined : value?.plan,
        preferences: snapshot.preferences, history_enabled: snapshot.history_enabled,
        authorization_refs: snapshot.authorization_refs ?? [],
        revision: snapshot.revision, maintenance: snapshot.history_enabled && value?.maintenance?.outboxId && !value.maintenance.applied
          ? { prompt: value.maintenance.prompt, sources: value.maintenance.sources, instruction: '用户任务已答复。这是一次受限记忆维护；仅可提交有当前公开证据支持的patch或空operations，不运行新分析、不新增任务。' } : undefined };
      const messages = currentNativeProjectContext(event.messages).map(message => message.role === 'system' && message.sections?.['biomind-current']
        ? { ...message, sections: Object.fromEntries(Object.entries(message.sections).filter(([key]) => key !== 'biomind-current')) } : message);
      // A maintenance outbox remains the active phase even after its patch was
      // applied: the following model turn only acknowledges that receipt.
      const roleRules = value?.maintenance?.outboxId ? MAINTENANCE_RULES
        : value?.role === 'Generate' ? GENERATE_RULES : CONSULT_RULES;
      messages.push({ role: 'system', content: '', sections: { 'biomind-current': `${DOMAIN_RULES}\n${roleRules}\n当前有效状态与偏好（每次发送刷新）：\n${JSON.stringify(currentContext)}` }, timestamp: Date.now() });
      return { messages };
    });
    pi.on('session_before_compact', async (event, ctx) => {
      try {
        snapshot = asObject(await client(ctx).call('context.snapshot', {}, { signal: event.signal })) as Snapshot;
        if (typeof snapshot.history_enabled !== 'boolean') throw new Error('Compaction requires an explicit history policy');
      } catch { return { cancel: true }; }
      const branch = ctx.sessionManager.getBranch();
      compactionSources = unique([...provenanceFromBranch(branch).values()].flat().concat(branch.flatMap(entry =>
        entry.type === 'message' ? sourceIds(asObject(entry.message).details) : entry.type === 'custom_message' ? sourceIds(entry.details) : [])))
        .filter(source => snapshot.history_enabled && !(snapshot.revoked_sources ?? []).includes(source));
      // Public preparation data is filtered before Pi's own summarizer runs.
      // No replacement summarizer or hidden model loop is introduced.
      const summaries = projectHistory(event.preparation.messagesToSummarize, branch, snapshot, false);
      const prefix = projectHistory(event.preparation.turnPrefixMessages, branch, snapshot, false);
      const projected = currentNativeProjectContext([...summaries, ...prefix]);
      event.preparation.messagesToSummarize = projected.slice(0, summaries.length);
      event.preparation.turnPrefixMessages = projected.slice(summaries.length);
      if (!snapshot.history_enabled || (snapshot.revoked_sources?.length ?? 0) > 0) {
        event.preparation.previousSummary = undefined;
        // Pi extracted file references before this hook. They can themselves
        // reveal a withdrawn source, so do not carry them into the new summary.
        for (const value of Object.values(event.preparation.fileOps)) if (value instanceof Set) value.clear();
      }
    });
    pi.on('session_compact', event => { pi.appendEntry(COMPACTION, { entryId: event.compactionEntry.id, sources: compactionSources }); });
    pi.on('agent_before_settle', async (event, ctx) => {
      if (isCanaryMode()) return;
      const value = state(ctx);
      if (!value || value.status !== 'active' || value.maintenance?.consumed || event.outcome !== 'completed' || ctx.signal?.aborted) return;
      const fresh = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
      // Auto is explicit policy; ask requires a real UI decision before any
      // maintenance provider request. Manual/disabled never arm automatically.
      if (!fresh.history_enabled || !['auto', 'ask'].includes(fresh.memory_mode ?? '') || fresh.maintenance?.enabled !== true) return;
      if (fresh.memory_mode === 'ask' && value.maintenance?.consent !== true) return;
      publish(ctx, { ...value, maintenance: { consumed: true } });
      try {
        const consent = value.maintenance?.consent === true;
        const branch = ctx.sessionManager.getBranch();
        const start = branch.findIndex(entry => entry.type === 'custom' && entry.customType === REQUEST && asObject(entry.data).requestId === value.requestId);
        const projected = projectHistory(branch.slice(start + 1).filter(entry => entry.type === 'message').map(entry => entry.message), branch, fresh, false);
        const sources = branch.slice(start + 1).filter(entry => entry.type === 'message').filter(entry => entry.message.role === 'user'
          || (entry.message.role === 'toolResult' && !['memory_search', 'memory_read', 'maintenance_submit'].includes(entry.message.toolName)))
          .flatMap(entry => {
            const message = projected.find(candidate => candidate.role === entry.message.role && (candidate.role === 'toolResult'
              ? candidate.toolCallId === (entry.message as any).toolCallId : fingerprint(candidate) === fingerprint(entry.message)));
            return message ? [{ source_ref: `pi:${sessionId(ctx)}:${entry.id}`, kind: message.role === 'user' ? 'user' : 'tool_result', content: textOf(message) }] : [];
          });
        const prepared = asObject(await client(ctx).hostCall('memory.maintenance.prepare', { sources, ...(consent ? { consent: true } : {}) }, { signal: ctx.signal, requestId: value.requestId }));
        if (prepared.status !== 'ready' || typeof prepared.outbox_id !== 'string') return;
        const next: RequestState = { ...value, maintenance: { consumed: true, outboxId: prepared.outbox_id, prompt: String(prepared.prompt ?? ''), sources: prepared.sources, applied: false } };
        setActiveTools(['maintenance_submit']);
        return { entries: [{ type: 'custom' as const, customType: REQUEST, data: next },
          { type: 'custom_message' as const, customType: 'biomind.maintenance', content: '进行已启用的一次记忆维护。仅提交有证据的patch，无法支持时提交空operations。', display: false }], continue: true };
      } catch (error) {
        pi.events.emit('biomind:diagnostic', { code: 'MEMORY_MAINTENANCE_PREPARE_FAILED', message: String(error) });
      }
    });
    pi.on('agent_settled', (_event, ctx) => {
      const value = state(ctx); if (!value) return;
      publish(ctx, { ...value, status: lastOutcome === 'completed' ? 'settled' : lastOutcome });
      pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role, outcome: lastOutcome, scientificSuccess: null });
    });
    pi.on('tool_call', (event, ctx) => {
      const value = state(ctx);
      const allowed = canaryTools(value?.maintenance?.outboxId && !value.maintenance.applied ? ['maintenance_submit'] : value?.role === 'Generate' ? GENERATE_TOOLS : READ_TOOLS);
      if (value?.status !== 'active' || !allowed.includes(event.toolName)) return { block: true, reason: 'This execution entry is not available in the current BioMind role', terminate: true };
    });
    pi.on('user_bash', () => ({ result: { output: 'Shell 快捷入口在 BioMind 模式不可用；请通过领域 run_command 执行并保留授权与作业句柄。', exitCode: 1, cancelled: false, truncated: false } }));
    const guardLifecycle = async (ctx: ExtensionContext, action: string) => {
      try {
        const value = asObject(await client(ctx).call('session.lifecycle', { action }));
        if (typeof value.active_jobs !== 'boolean') throw new Error('Missing owner status');
        if (value.active_jobs) { ctx.ui.notify('当前仍有科学作业；请先观察或明确取消后再更换会话。', 'warning'); return { cancel: true }; }
      } catch { ctx.ui.notify('无法确认领域作业状态，已保留当前会话。', 'warning'); return { cancel: true }; }
    };
    pi.on('session_before_switch', (_event, ctx) => guardLifecycle(ctx, 'switch'));
    pi.on('session_before_fork', (_event, ctx) => guardLifecycle(ctx, 'fork'));
    pi.on('session_before_tree', async (event, ctx) => {
      const blocked = await guardLifecycle(ctx, 'tree'); if (blocked) return blocked;
      let fresh: Snapshot;
      try { fresh = asObject(await client(ctx).call('context.snapshot', {}, { signal: event.signal })) as Snapshot;
        if (typeof fresh.history_enabled !== 'boolean') return { cancel: true }; }
      catch { return { cancel: true }; }
      if (event.preparation.userWantsSummary && (!fresh.history_enabled || (fresh.revoked_sources?.length ?? 0) > 0)) {
        ctx.ui.notify('历史来源已撤回；请关闭分支摘要后切换，避免旧来源进入新的摘要。', 'warning'); return { cancel: true };
      }
    });
    pi.on('session_tree', (_event, ctx) => { setActiveTools(state(ctx)?.role === 'Generate' ? GENERATE_TOOLS : READ_TOOLS); });
    pi.on('session_shutdown', async (event, ctx) => {
      rejectPending('Session connection closed; no approval was granted', sessionId(ctx));
      inputQueue.splice(0); initialUserPending = false; preparedInputRequestId = undefined; activeContext = undefined;
      if (event.reason === 'reload') {
        pi.events.emit('biomind:lifecycle', { sessionId: sessionId(ctx), reason: 'reload', owner: 'connection_retained' });
        return;
      }
      // Disconnect-only: a session/UI shutdown must never cancel scientific jobs.
      const connection = clients.get(sessionId(ctx)); clients.delete(sessionId(ctx));
      await connection?.close({ disconnectOnly: true });
      pi.events.emit('biomind:disconnected', { sessionId: sessionId(ctx), reason: event.reason });
    });
  };
}

export default createBioMindExtension();
