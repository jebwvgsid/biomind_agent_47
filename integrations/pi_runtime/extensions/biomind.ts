/** BioMind's sole Pi extension. Native CLI, SDK and tests load this module. */
import { planStageBoundary, boundaryDrafts, assertStageBoundary, type StageBoundary } from '../stage-context.js';
import { nativeRead } from '../native-read.js';
import { runtimeBash, type RuntimeBashLaunch } from '../native-bash.js';
import { DISCOVERY_TOOLS, discoveryDefinition, nativeDiscovery, type DirectorySnapshot } from '../native-discovery.js';
import { nativeMutation } from '../native-mutation.js';
import { filterNativeContextFiles, currentNativeProjectContext } from '../native-context.js';
import path from 'node:path';
import { homedir } from 'node:os';
import { readFileSync } from 'node:fs';
import { executionProfile, sessionProfile, type ExecutionProfile } from '../execution-profile.js';
import { NATIVE_TOOLS, NATIVE_RULES, nativeTools } from '../native-workflow.js';
import { realpath, stat } from 'node:fs/promises';
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, getAgentDir } from '@earendil-works/pi-coding-agent';
import { randomUUID } from 'node:crypto';
import { configuredProvider, validateModelSelection } from '../model-config.js';
import { Type, type TSchema } from 'typebox';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import type { ExtensionAPI, ExtensionContext, SessionEntry, ToolDefinition } from '@earendil-works/pi-coding-agent';
import { createDomainClient, DomainHandshakeTimeoutError, type DomainOptions, type DomainLifecycle } from '../domain-client.js';
import { canaryTools, installUsageObservation, isCanaryMode } from '../usage-observer.js';
import { toolRenderers, receiptDetails, projectToolReceipt, displayPreview } from '../tool-display.js';
import { installBioMindProductUI, formatPermissionStatus, type BioMindBackendState } from '../product-ui.js';
import { inspectWelcomePreference, inspectNativeWelcomeInstructions, describeWelcomeInstructions, describeWelcomeMemory, describeWelcomeAuthentication,
  type PreferenceStatus, type ProjectInstructionStatus } from '../welcome-status.js';
import { formatApprovalProposal, selectApprovalProposal } from '../approval-display.js';
import { readEditableFile, saveEditableFile, memoryDefault, saveMemoryDefault, argumentCompletions } from '../instructions.js';
import { listProjects, createProject, restartProjectCommand } from '../project-launcher.js';
import { readTargetSessionRoot } from '../native-session.js';
import { loadCompactionInstructions } from '../compaction-addendum.js';

export interface DomainConnection {
  readonly startupFailed?: boolean;
  readonly lifecycle?: DomainLifecycle;
  on?(event: 'lifecycle', listener: (event: DomainLifecycle) => void): unknown;
  off?(event: 'lifecycle', listener: (event: DomainLifecycle) => void): unknown;
  call(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  hostCall(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  close(options: { disconnectOnly: true }): void | Promise<void>;
}
export interface BioMindExtensionOptions {
  domainFactory?: (options: DomainOptions) => DomainConnection;
  executionProfile?: ExecutionProfile;
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
  choice?: 'y' | 'n' | 'scope' | 'edit' | 'cancel' | 'session' | 'read_only' | 'read_session' | 'select' | 'directory_session' | 'adjust';
  selections?: Array<{ index: number; access: 'read' | 'read_write'; lifetime: 'request' | 'session' }>;
  replacement?: Record<string, unknown>;
  cancelled?: boolean;
  action_hash?: string;
}
export interface RequestState {
  requestId: string;
  ownerSessionId?: string;
  role: 'Main';
  status: 'active' | 'settled' | 'aborted' | 'error' | 'paused';
  pauseReason?: string;
  revision?: number;
  userBasis?: Array<{ text: string; revision: number }>;
  boundary?: StageBoundary;
  handoff?: { toolCallId: string; goal: string; manifest?: string; bound?: Record<string, any> };
  handoffConsumed?: boolean; // Read-only legacy field; new requests never create a handoff.
  plan?: Array<{ step: string; status: 'pending' | 'in_progress' | 'completed' }>;
  planSources?: string[]; // Legacy metadata only; never used to withdraw an existing plan.
  maintenance?: { consumed: boolean; consent?: boolean; outboxId?: string; prompt?: string; sources?: unknown; applied?: boolean };
}
interface Snapshot {
  preferences?: unknown;
  approval_mode?: unknown;
  request_paused?: boolean;
  authorization_refs?: unknown[];
  history_enabled: boolean;
  revision?: string;
  diagnostics?: unknown;
  memory_mode?: string;
  maintenance?: { enabled?: boolean };
  archive_enabled?: boolean;
  memory_root?: string | null;
}
const REQUEST = 'biomind.request';
const EMPTY = Type.Object({}, { additionalProperties: false });
// Tool execution consumes validated records; keep schema inference bounded as this catalog grows.
const OBJECT = (properties: Record<string, TSchema>): TSchema => Type.Object(properties, { additionalProperties: false });
const nonempty = () => Type.String({ minLength: 1 });
const optionalString = () => Type.Optional(Type.String());
const asObject = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
const customEntries = (branch: readonly SessionEntry[], name: string) => branch.filter(
  (entry): entry is Extract<SessionEntry, { type: 'custom' }> => entry.type === 'custom' && entry.customType === name);
export function requestFromBranch(branch: readonly SessionEntry[]): RequestState | undefined {
  const raw = customEntries(branch, REQUEST).at(-1)?.data as Record<string, any> | undefined;
  if (!raw?.requestId) return undefined;
  // Legacy entries remain untouched on disk. Their role and pending transition
  // are not executable instructions; user facts still belong to this request.
  const { handoff, handoffConsumed: _consumed, ...record } = raw;
  return { ...record, role: 'Main',
    userBasis: raw.userBasis?.length ? raw.userBasis : handoff?.bound?.user_basis ?? [],
    boundary: raw.boundary?.kind === 'task' ? raw.boundary : undefined } as RequestState;
}
function textOf(message: unknown): string {
  const content = asObject(message).content;
  return typeof content === 'string' ? content : Array.isArray(content) ? content.filter(item => item.type === 'text').map(item => item.text).join('\n') : '';
}
function historicalFacts(message: AgentMessage): AgentMessage {
  if (message.role !== 'custom' || message.customType !== 'biomind.handoff') return message;
  let content = textOf(message);
  try {
    const { instruction: _instruction, phase: _phase, ...facts } = JSON.parse(content);
    content = JSON.stringify({ historical_record: 'prior handoff facts; no transition or execution instruction', ...facts });
  } catch { content = '[历史交接记录，仅保留事实，不触发执行或角色切换]\n' + content; }
  return { ...message, content };
}
/** Only migrate obsolete role-handoff instructions; Pi owns history and summaries.
 * Retrieval settings and source metadata never redact already accepted messages.
 */
export function normalizeLegacyHistory(messages: AgentMessage[]): AgentMessage[] {
  return messages.map(historicalFacts);
}

const MAIN_TOOLS = [...DISCOVERY_TOOLS, 'request_path_access', 'probe_command', 'ask_user', 'select_environment', 'register_environment',
  'inspect_run', 'wait_run', 'read_run_log', 'cancel_run', 'file_stat', 'read', 'memory_search', 'memory_read',
  'update_plan', 'run_command', 'install_tool', 'write', 'edit', 'memory_write'];
const MAINTENANCE_RULES = `当前阶段：记忆维护。仅处理本次已完成对话的记忆维护；不继续或启动分析任务。仅在尚未提交时使用 maintenance_submit 提交有当前证据支持的 patch 或空 operations；提交后核对实际回执并结束。`;
const COMMON_RULES = `你是运行在 Pi 中的 BioMind 单一主 agent，在同一会话完成需求理解、核查、执行、纠错与交付。

【任务推进】
按用户当前请求回答或执行；只要求讨论、方案或审计时，不擅自实施修改或启动正式分析。信息足够就推进；仅对影响结果且无法从已有材料解决的关键歧义，用 ask_user 等待真实回答。遵循最新修订、明确检查和停止条件；历史不自动触发重算，旧偏好和工具输出不是授权。update_plan 是可选笔记，普通问答和文件操作无需科学模板、TaskSpec、计划或交接摘要。

【环境与安装】
优先复用适用 binding_ref，否则 select_environment 查询登记。适用登记直接使用，不例行重复探测，也不借查询、刷新或初始化隐式探测。AVAILABLE 仅表示可选择，registered 不等于本次检查通过，未知能力/版本仍为 unknown。
未命中不等于未安装；必要时 mode=locate 有界定位、register_environment 补登记。用户明确要求的检查仍须先完成，环境复用不代替科学输入 QC。真实缺包仅诊断对应能力，不因取消、超时或输入错误重装或判整个环境损坏。
安装必须必要且已有安装意图确认；安装、目标验证、登记分别按真实回执报告。登记失败只恢复登记；状态未知先对账，不重装。

【执行与恢复】
需持久观察的科学计算用 run_command，保存真实 run_ref 和完整日志，按需查询、等待、读日志或取消；不以 nohup/& 冒充托管作业，不用模型轮询维持 UI 心跳。
复用当前请求与执行 scope 中唯一适用绑定，多绑定时明确引用。环境相关 bash 显式传 binding_ref，绑定仅对该次调用有效；裸 bash 用宿主环境，export/activate 不跨工具继承，用户 ! 不受环境选择影响。
同 call 重放查询原执行，不自动重跑；取消请求不等于已确认清理完成，无法确认保留 unknown。

【验证与交付】
以本次真实回执、产物及任务所需 QC 判断科学结果；settled、isError:false、文件存在或管道末尾 rc 不能单独证明成功。分析包装用 pipefail/PIPESTATUS 或先保存原分析 rc 再显示摘要，不改变裸 shell 默认语义。
核对所需样本、路径、字段与产物来源，旧产物不冒充本次计算，合法空结果按对应格式解释。按用户要求交付报告、reproduce.sh 和状态表；复现脚本生成后不自动执行。只引用真实路径，不把远端文件说成已下载。

【资源与开发】
按需读取 Skill 正文及相对 baseDir 的 references/scripts；脚本资源属于 Skill，输入输出和工作目录属于当前项目。保护既有 dirty 内容，按逻辑单元验证后仅本地提交本轮 delta；未经明确授权不 push、合并、发布或改写历史。`;
const PROTECTED_RULES = `当前 execution:protected。文件读取、写入及命令继续经过原权限账本与执行保护，不能因工具可见或环境 prefix 已解析而视为目录已授权。项目外路径通过原工具申请缺失范围，已有授权可复用；用 request_path_access 汇总真实输入/输出范围，不能用 ask_user 重复代替宿主审批，拒绝后不能换工具、call id 或别名绕过。
read_paths 声明实际文件或小范围子树；只读快照限制及链接拒绝按回执处理，不能把 Conda 根加入分析输入。已选环境使用原只读运行时挂载。external cwd 不隐含可写，也不改变项目、session、request 或资源归属。probe_command 仅用于必要检查，保持只读、网络隔离和有界预算。
受控命令中的 ls/find 只说明当前挂载视图，不能断言宿主未安装。保护前置阻塞先解释原 reason，必要时读取 receipt_ref，不用 echo/ls、删除 read_paths 或裸 shell 绕过；修复后使用新 call 重新授权和 claim。execution_started 只证明包装进程已启动，不能替代业务成功。write_paths 不授予权限或覆盖输入保护；OUTPUT_CONTRACT_CONFLICT 应修订任务要求。`;

export function createBioMindExtension(options: BioMindExtensionOptions = {}) {
  // Host configuration is captured once; project files and model arguments are not inputs.
  const profile = executionProfile(options.executionProfile ?? process.env.BIOMIND_PI_EXECUTION_PROFILE ?? 'protected');
  const native = profile === 'native';
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
    const toolNames = (names: readonly string[]) => canaryTools((native ? nativeTools(names) : [...names]).sort());
    const setActiveTools = (names: readonly string[]) => pi.setActiveTools(toolNames(names));
    const existingSessions = new Set<string>();
    const readySessions = new Set<string>();
    const backendStates = new Map<string, BioMindBackendState>();
    const backendObservers = new Map<string, { connection: DomainConnection; listener: (event: DomainLifecycle) => void }>();
    const notifiedFailures = new Set<string>();
    let backendEpoch = 0;
    let migrationConsumed = false;
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
    let snapshotKnown = false;
    let snapshotFailed = false;
    let instructionsFailed = false;
    let welcomePreference: PreferenceStatus = { status: 'unknown' };
    let welcomeProject: ProjectInstructionStatus | undefined;
    let welcomeEpoch = 0;
    const instructionDrafts = new Map<string, string>();
    const savedInstructions = new Set<string>();
    const savedGlobalInstructions = new Set<string>();
    let lastOutcome: 'completed' | 'aborted' | 'error' = 'completed';
    // Delivery bookkeeping only; messages and stable entry identities stay in Pi.
    // The original archive writer owns deduplication and actual persistence.
    const archiveSubmitted = new Set<string>();
    const state = (ctx: ExtensionContext) => {
      const value=requestFromBranch(ctx.sessionManager.getBranch());
      return value?.ownerSessionId && value.ownerSessionId !== ctx.sessionManager.getSessionId() ? undefined : value;
    };
    const refreshProductUI = installBioMindProductUI(pi, state, { details: ctx => ({
      backend: backendStates.get(ctx.sessionManager.getSessionId()) ?? { state: 'starting' },
      memory: describeWelcomeMemory(snapshotKnown ? snapshot : undefined, process.env, snapshotFailed),
      instructions: describeWelcomeInstructions(snapshotKnown ? asObject(snapshot.preferences) : welcomePreference,
        welcomeProject, savedInstructions, instructionsFailed, savedGlobalInstructions),
      authentication: describeWelcomeAuthentication(ctx),
    }), permissions: {
      status: ctx => observePermissions(ctx, { action: 'status' }),
      revoke: (ctx, grant, listing) => observePermissions(ctx, { action: 'revoke', grant_id: grant.grant_id,
        request_scope: listing.request_scope, policy_revision: listing.policy_revision }),
    } });
    installUsageObservation(pi, state);
    const sessionId = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId();
    const updateBackend = (ctx: ExtensionContext, next: BioMindBackendState) => {
      const id = sessionId(ctx);
      if (next.state === 'failed' && backendStates.get(id)?.state === 'failed') return;
      backendStates.set(id, next);
      if (next.state !== 'ready') {
        readySessions.delete(id);
        setActiveTools([]);
      }
      pi.events.emit('biomind:backend', { sessionId: id, ...next });
      refreshProductUI();
      if (next.state === 'failed' && !notifiedFailures.has(id)) {
        notifiedFailures.add(id);
        ctx.ui.notify(`BioMind ${next.stage === 'runtime' ? '后台连接中断' : '后台初始化失败'}，分析输入已停止：${next.message ?? '原因未知'}`, 'error');
      }
    };
    const unobserveBackend = (id: string) => {
      const observed = backendObservers.get(id);
      if (observed) observed.connection.off?.('lifecycle', observed.listener);
      backendObservers.delete(id);
    };
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
          executionProfile: profile, profileSource: process.env.BIOMIND_PI_PROFILE_SOURCE ?? 'host',
          sessionExisting: existingSessions.has(id),
          migrateProfile: !migrationConsumed && process.env.BIOMIND_PI_MIGRATE_PROFILE === '1',
          python: process.env.BIOMIND_PI_PYTHON, dataRoot: process.env.BIOMIND_PI_DATA_ROOT,
          installRoot: process.env.BIOMIND_PI_INSTALL_ROOT,
          registryPath: process.env.BIOMIND_PI_REGISTRY_PATH, skillsRoot: process.env.BIOMIND_PI_SKILLS_ROOT,
          preferencesPath: process.env.BIOMIND_PI_PREFERENCES_PATH, memoryRoot: process.env.BIOMIND_PI_MEMORY_ROOT,
          wikiRoot: process.env.BIOMIND_PI_WIKI_ROOT, historyEnabled: process.env.BIOMIND_PI_HISTORY_ENABLED === '1' });
        clients.set(id, value);
        migrationConsumed = true;
      }
      if (backendObservers.get(id)?.connection !== value) {
        unobserveBackend(id);
        const connection = value;
        const listener = (event: DomainLifecycle) => {
          const currentContext = activeContext;
          if (!currentContext || sessionId(currentContext) !== id || clients.get(id) !== connection) return;
          if (!['unavailable', 'failed', 'closed'].includes(event.state)) return;
          updateBackend(currentContext, { state: event.state, stage: event.stage, message: event.error?.message });
          if (event.state === 'unavailable') {
            inputQueue.splice(0); preparedInputRequestId = undefined;
            currentContext.abort();
            currentContext.ui.setStatus('biomind-execution', '后台不可用 · 分析输入已停止');
          }
        };
        backendObservers.set(id, { connection, listener });
        connection.on?.('lifecycle', listener);
        if (connection.lifecycle) listener(connection.lifecycle);
      }
      return value;
    };
    const begin = async (ctx: ExtensionContext, text = '') => {
      const value: RequestState = { requestId: randomUUID(), ownerSessionId: sessionId(ctx), role: 'Main', status: 'active',
        revision: 1, userBasis: text ? [{ text, revision: 1 }] : [], boundary: planStageBoundary(ctx.sessionManager.getBranch(), 'task') };
      const ownerSession = sessionId(ctx);
      let connection: DomainConnection | undefined;
      try {
        connection = client(ctx);
        await connection.hostCall('request.begin', { request_id: value.requestId, role: 'main' });
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
      publish(ctx, value); setActiveTools(MAIN_TOOLS); archiveSubmitted.clear(); readVersions.clear(); lastOutcome = 'completed';
      return value;
    };
    const current = (ctx: ExtensionContext) => {
      const value = state(ctx);
      if (!value || value.status !== 'active') throw new Error('No active BioMind request');
      return value;
    };
    const pause = async (ctx: ExtensionContext, reason: string) => {
      const value = state(ctx); if (!value) return;
      publish(ctx, { ...value, status: 'paused', pauseReason: reason });
      lastOutcome = 'aborted';
      try { await client(ctx).hostCall('request.pause', { reason }, { requestId: value.requestId }); }
      finally { await refreshApprovalStatus(ctx); ctx.abort(); }
      ctx.ui.notify('当前任务已暂停；没有终止已运行作业。继续需明确恢复，原拒绝不会变为批准。', 'info');
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
      snapshotKnown = true; snapshotFailed = false;
      return { history_enabled: result.history_enabled, memory_mode: result.memory_mode, revision: result.revision };
    };
    pi.registerCommand('biomind-memory', {
      description: '记忆作用域：status | history on/off | mode ask/auto/manual/disabled | default on/off',
      getArgumentCompletions: argumentCompletions(['status', 'history on', 'history off', 'mode ask', 'mode auto', 'mode manual', 'mode disabled', 'default on', 'default off']),
      async handler(args, ctx) {
        const parts = args.trim().split(/\s+/); let patch: Record<string, unknown> = {};
        let savedMessage = '';
        try {
          if (!args.trim() || parts[0] === 'status' && parts.length === 1) patch = {};
          else if (parts[0] === 'default' && parts.length === 2 && ['on', 'off'].includes(parts[1])) {
            if (ctx.mode !== 'tui' || !ctx.hasUI) throw new Error('保存默认值请使用终端入口');
            const filename = process.env.BIOMIND_PI_CONFIG_PATH;
            if (!filename) throw new Error('未保存：请用 --pi-config 选择既有 BioMind 配置文件');
            if (!await ctx.ui.confirm('保存记忆启动默认值', `${filename}\nhistoryEnabled → ${parts[1] === 'on'}\n仅修改此字段；当前运行开关不变。`)) return;
            await saveMemoryDefault(parts[1] === 'on');
            savedMessage = '启动默认值已保存；当前运行开关不变。重启仍可能受更高优先级启动参数覆盖。\n';
          }
          else if (parts[0] === 'history' && parts.length === 2 && ['on', 'off'].includes(parts[1])) patch = { history_enabled: parts[1] === 'on' };
          else if (parts[0] === 'mode' && parts.length === 2) patch = { memory_mode: parts[1] };
          else throw new Error('用法：/biomind-memory status | history on/off | mode ask/auto/manual/disabled | default on/off');
          const result = await applySettings(ctx, patch), defaults = await memoryDefault();
          ctx.ui.notify(`${savedMessage}项目知识检索：${result.history_enabled ? '开启' : '关闭'}（本次运行）\n启动生效默认：${process.env.BIOMIND_PI_HISTORY_DEFAULT === '1' ? '开启' : '关闭'}；来源：${process.env.BIOMIND_PI_HISTORY_SOURCE ?? '未知'}\n磁盘默认：${defaults.error ?? (defaults.value === undefined ? '未配置' : defaults.value ? '开启' : '关闭')}；${defaults.path ?? '无可写配置，使用 --pi-config 选择'}\n记忆根：${snapshot.memory_root ?? process.env.BIOMIND_PI_MEMORY_ROOT ?? '未配置'}\n记忆整理模式：${result.memory_mode ?? '未知'}；自动整理：${snapshot.maintenance?.enabled ? '按现有策略允许' : '未启用'}\n历史会话与已读工具结果仍保留；关闭只阻止新的检索，不删除历史或让模型忘记已读内容。`, 'info');
          pi.events.emit('biomind:settings_result', { status: 'ok', sessionId: sessionId(ctx), ...result });
        } catch (error) { snapshotKnown = false; snapshotFailed = true; ctx.ui.notify(savedMessage + String(error), 'error'); pi.events.emit('biomind:settings_result', { status: 'error', code: 'SETTINGS_UPDATE_FAILED', message: String(error) }); }
      },
    });
    pi.registerCommand('instructions', {
      description: '查看实际说明来源；global/project 编辑；project-base 编辑被遮蔽的 AGENTS.md',
      getArgumentCompletions: argumentCompletions(['status', 'global', 'project', 'project-base']),
      async handler(args, ctx) {
        if (ctx.mode !== 'tui' || !ctx.hasUI) return;
        const choice = args.trim() || 'status';
        if (!['status', 'global', 'project', 'project-base'].includes(choice)) { ctx.ui.notify('用法：/instructions [status|global|project|project-base]', 'info'); return; }
        try {
          await applySettings(ctx, {});
          const preference = asObject(snapshot.preferences);
          const files = filterNativeContextFiles(ctx.getSystemPromptOptions().contextFiles ?? [], ctx.cwd, getAgentDir());
          const local = files.filter(file => path.dirname(path.resolve(file.path)) === path.resolve(ctx.cwd));
          const effective = local[0];
          welcomeProject = effective ? { path: effective.path, state: effective.content.trim() ? '已加载' : '为空' } : undefined;
          const preferred = typeof preference.preferred_path === 'string' ? preference.preferred_path : process.env.BIOMIND_PI_PREFERENCES_PATH;
          const globalPath = typeof preference.source_path === 'string' ? preference.source_path : preferred;
          const preferenceState = preference.status === 'unreadable' ? '加载失败' : preference.status === 'missing' || preference.exists === false ? '未找到'
            : preference.empty ? '存在为空' : preference.status === 'truncated' ? '已加载（按现有长度上限截断）' : preference.status === 'loaded' ? '已加载（每次请求刷新）' : '未知';
          const overridePath = path.join(ctx.cwd, 'AGENTS.override.md');
          const overrideExists = await stat(overridePath).then(value => value.isFile()).catch(() => false);
          const basePath = path.join(ctx.cwd, 'AGENTS.md');
          const projectPath = effective?.path ?? (overrideExists ? overridePath : basePath);
          const rows = [`项目根：${ctx.cwd}`, `全局偏好：${globalPath ?? '未知'}；来源：${String(preference.source ?? preference.source_kind ?? '未知')}；${preferenceState}`];
          if (preferred && preferred !== globalPath) rows.push(`首选来源：${preferred}（缺失，当前使用回退来源）`);
          for (const file of files) {
            let stateText = file.content.trim() ? '已加载' : '存在为空';
            try { const disk = await readEditableFile(file.path); if (disk.text !== file.content) stateText = '修改后待刷新'; }
            catch { stateText = '加载后文件不可读'; }
            if (file === effective) welcomeProject = { path: file.path, state: stateText };
            rows.push(`Pi 说明：${file.path}；${stateText}${savedInstructions.has(path.resolve(file.path)) ? '；已保存，当前会话待刷新' : ''}`);
          }
          if (!effective) rows.push(`项目根说明：${projectPath}；未加载（未找到或边界过滤，保存后 /reload）`);
          if (overrideExists) rows.push(`同目录 AGENTS.override.md 遮蔽 AGENTS.md；默认编辑 ${projectPath}`);
          rows.push('全局偏好与 Pi 用户级 AGENTS 各自生效；不复制内容。保存与请求生效分别确认。');
          instructionsFailed = false;
          if (choice === 'status') { ctx.ui.notify(rows.join('\n'), 'info'); return; }
          const target = choice === 'global' ? (preference.exists === false ? preferred ?? globalPath : globalPath) : choice === 'project-base' ? basePath : projectPath;
          if (!target) throw new Error('无法确定实际偏好来源；请先检查启动 preferencesPath');
          const before = await readEditableFile(target);
          const shadowed = choice === 'project-base' && overrideExists;
          const detail = `${before.path}${before.symlink ? `\n软链接真实目标：${before.target}` : ''}${shadowed ? '\n此文件被 AGENTS.override.md 遮蔽，修改暂不会生效' : ''}`;
          const draft = await ctx.ui.editor(`编辑说明（Enter 完成，Esc 取消）\n${detail}`, instructionDrafts.get(before.path) ?? before.text);
          if (draft === undefined) { ctx.ui.notify('已取消，未写入说明。', 'info'); return; }
          instructionDrafts.set(before.path, draft);
          if (!await ctx.ui.confirm('确认保存说明', `${detail}\n字符数 ${before.text.length} → ${draft.length}；${draft === before.text ? '内容未变' : draft === '' ? '清空文件' : '替换为编辑器中的内容'}\n保存后当前会话待刷新。`)) { ctx.ui.notify('未保存；草稿保留，重新执行本命令可继续编辑。', 'info'); return; }
          await saveEditableFile(before, draft);
          instructionDrafts.delete(before.path); savedInstructions.add(before.path);
          if ([globalPath, preferred].some(filename => filename && path.resolve(filename) === before.path)) savedGlobalInstructions.add(before.path);
          if (choice !== 'global' && !shadowed) welcomeProject = { path: before.path, state: '已保存，待 /reload' };
          ctx.ui.notify(`已保存：${before.path}。当前会话待刷新；${shadowed ? '此文件仍被 override 遮蔽。' : '使用原生 /reload 刷新。'}不会中断科学作业。`, 'info');
          if (!shadowed && ctx.isIdle() && await ctx.ui.confirm('现在刷新说明？', '使用 Pi 原生 reload；保留当前会话与科学作业。')) await ctx.reload();
        } catch (error) { snapshotKnown = false; instructionsFailed = true; ctx.ui.notify(`说明操作失败：${String(error)}。未保存的草稿仅保留在本次运行，重载或退出前可重新打开。`, 'error'); }
      },
    });
    // Pi may expose a settings-derived relative directory. Preserve its current
    // project meaning before serializing a restart into a different project.
    const sessionDirectory = (ctx: ExtensionContext) => {
      const value = ctx.sessionManager.getSessionDir();
      return value ? path.resolve(ctx.cwd, value) : value;
    };
    const launcherConfig = (ctx: ExtensionContext) => ({ agentDir: getAgentDir(), sessionDir: sessionDirectory(ctx),
      memoryRoot: process.env.BIOMIND_PI_MEMORY_ROOT, wikiRoot: process.env.BIOMIND_PI_WIKI_ROOT, installRoot: process.env.BIOMIND_PI_INSTALL_ROOT,
      launcherPython: process.env.BIOMIND_PI_LAUNCHER_PYTHON });
    const restartOptions = (ctx: ExtensionContext) => ({ configPath: process.env.BIOMIND_PI_CONFIG_PATH,
      sessionDir: sessionDirectory(ctx), node: process.env.BIOMIND_PI_NODE, modelRecord: process.env.BIOMIND_PI_MODEL_RECORD });
    pi.registerCommand('project', {
      description: '项目目录与创建入口；跨项目使用安全重启',
      getArgumentCompletions: argumentCompletions(['list', 'create', 'open']),
      async handler(args, ctx) {
        if (ctx.mode !== 'tui' || !ctx.hasUI) return;
        try {
          let choice = args.trim();
          const listing = await listProjects(launcherConfig(ctx), ctx.cwd);
          const rows = listing.projects.map(project => `${project.root} [${project.sources.join(', ')}；${project.availability}]`);
          ctx.ui.notify(`当前项目：${ctx.cwd}\n${rows.join('\n')}${listing.incomplete.length ? `\n列表不完整：${listing.incomplete.join('; ')}` : ''}\n跨项目请在终端重启；当前输入、会话与科学作业保留。`, 'info');
          if (!choice) choice = await ctx.ui.select('项目操作（取消保留当前会话）', ['list', 'open', 'create']) ?? 'list';
          if (choice === 'list') return;
          if (choice === 'open') {
            const selected = await ctx.ui.select('选择项目；仅显示重启命令', rows);
            if (selected === undefined) return;
            const project = listing.projects[rows.indexOf(selected)];
            if (project) ctx.ui.notify(`请在系统终端执行：\n${restartProjectCommand(project.root, restartOptions(ctx))}`, 'info');
          } else if (choice === 'create') {
            const target = await ctx.ui.input('新项目目录（相对当前项目或绝对路径）');
            if (!target?.trim()) return;
            if (!await ctx.ui.confirm('创建项目', `${path.resolve(ctx.cwd, target)}\n仅创建必要目录、缺失的 AGENTS.md 与目录登记；不启动分析。`)) return;
            const receipt = await createProject(launcherConfig(ctx), ctx.cwd, target);
            ctx.ui.notify(displayPreview(receipt), 'info');
          } else ctx.ui.notify('用法：/project [list|open|create]；系统终端可用 biomind project create/open <目录>', 'info');
        } catch (error) { ctx.ui.notify(`项目操作失败：${String(error)}`, 'error'); }
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
    const observePermissions = async (ctx: ExtensionContext, input: Record<string, unknown>): Promise<Record<string, any>> => {
      if (native && input.action === 'status') return { execution_profile: profile, approval_mode: { mode: 'not_applicable' }, grants: [] };
      if (native) throw new Error('native 普通工具不使用目录 grant；审批操作请在 protected 新进程中进行。');
      const requestId = state(ctx)?.requestId;
      if (input.action === 'status') return asObject(await client(ctx).hostCall('permissions.status', {}, { requestId }));
      if (input.action !== 'revoke' || typeof input.grant_id !== 'string' || !input.grant_id.trim()
          || !input.request_scope || typeof input.policy_revision !== 'string') throw new Error('Invalid permission revocation');
      if (!requestId) throw new Error('Permission revocation requires the original BioMind request');
      return asObject(await client(ctx).hostCall('permissions.revoke', { grant_id: input.grant_id,
        request_scope: input.request_scope, policy_revision: input.policy_revision }, { requestId }));
    };
    const refreshApprovalStatus = async (ctx: ExtensionContext) => {
      if (native) { ctx.ui.setStatus('biomind-approval','approval:N/A'); return observePermissions(ctx,{action:'status'}); }
      try {
        const listing=await observePermissions(ctx,{action:'status'});
        const count=Array.isArray(listing.grants)?listing.grants.filter((g:Record<string,unknown>)=>g.ordinary_operations_active).length:0;
        ctx.ui.setStatus('biomind-approval', `approval:${listing.approval_mode?.mode ?? 'manual'}${count ? ` · 目录免确认:${count}` : ''}${listing.paused ? ' · paused' : ''}`);
        return listing;
      } catch { ctx.ui.setStatus('biomind-approval','approval:unknown'); return undefined; }
    };
    pi.events.on('biomind:permissions_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const result = await observePermissions(activeContext, request);
        pi.events.emit('biomind:permissions_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), result });
      } catch (error) { pi.events.emit('biomind:permissions_result', { id: request.id, status: 'error', code: 'PERMISSIONS_REQUEST_FAILED', message: String(error) }); }
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
      getArgumentCompletions: argumentCompletions(['list', 'status ', 'logs ', 'cancel ']),
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
        'returncode', 'terminal', 'log_refs', 'receipt_ref', 'cancel_requested', 'stop_requested', 'task_cancelled', 'approval', 'approval_id',
        'error', 'reason', 'phase', 'protection_phase', 'execution_started', 'count', 'unique_inode_count', 'affected_capability', 'host_boundary',
        'current_project_root', 'requested_workdir', 'instruction', 'runtime_binding', 'runtime_launch', 'binding_ref', 'binding_scope', 'registration_sources', 'healthcheck_status',
        'path', 'path_samples', 'backend', 'errno', 'probe_exit_code', 'permission_status']
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
      if (!waiter || response.requestId !== waiter.request.requestId || response.sessionId !== waiter.request.sessionId
          || (response.action_hash !== undefined && response.action_hash !== waiter.request.proposal?.action_hash)) {
        pi.events.emit('biomind:diagnostic', { code: 'STALE_OR_UNKNOWN_UI_RESPONSE', id: response.id }); return;
      }
      pendingUI.delete(response.id); waiter.resolve(response);
    });
    const approvalOptions = (proposal: Record<string, unknown>, fine = false) => {
      if (!Array.isArray(proposal.allowed_decisions)) throw new Error('Authorization did not specify allowed decisions');
      const offered = new Set(proposal.allowed_decisions.filter((value): value is string => typeof value === 'string'));
      // The current permission service explicitly supports installation editing
      // before checking its stored y/n list; other proposal kinds cannot edit.
      if (asObject(proposal.payload).kind === 'installation') offered.add('edit');
      const reading = asObject(proposal.payload).kind === 'file_read';
      const paths = asObject(proposal.payload).kind === 'path_access';
      if (paths) offered.add('select');
      const directories = asObject(proposal.payload).directory_targets as Array<Record<string, unknown>> | undefined;
      const directoryLabel = directories?.length === 1
        ? directories[0].access === 'read' ? '允许本会话读取此目录（范围内免逐次确认）' : '允许本会话在此目录工作（范围内免逐次确认）'
        : '允许本会话使用所列目录（范围内免逐次确认）';
      if (offered.has('directory_session') && !fine) return [
        {choice:'n',label:'拒绝'}, {choice:'directory_session',label:directoryLabel},
        {choice:'adjust',label:'调整范围／仅本次'}, {choice:'cancel',label:'取消'},
      ];
      const options = [
        { choice: 'directory_session', label: directoryLabel },
        { choice: 'n', label: '拒绝' }, { choice: 'y', label: paths ? '批准提案范围（当前任务）' : reading ? '只读授权该明确目标（当前分析请求）' : '明确同意执行此提案' },
        { choice: 'scope', label: reading ? '只读授权提案列出的目录及子目录（当前分析请求）' : '明确授权提案中列出的目录范围内文件工具写入' },
        { choice: 'session', label: '批准提案明确目标（当前持久会话，含以后恢复）' },
        { choice: 'read_only', label: '仅批准读取（当前任务）' },
        { choice: 'read_session', label: '仅批准读取（当前持久会话）' },
        { choice: 'select', label: '逐项选择权限子集与有效期' },
        { choice: 'edit', label: '编辑后重新确认' }, { choice: 'cancel', label: '取消' },
      ].filter(option => option.choice === 'cancel' || offered.has(option.choice));
      return [options.find(option => option.choice === 'n') ?? options.find(option => option.choice === 'cancel')!, ...options.filter(option => option.choice !== 'n')];
    };
    async function ask(ctx: ExtensionContext, kind: 'question' | 'approval', toolCallId: string, question: string,
      signal?: AbortSignal, proposal?: Record<string, unknown>, choices?: string[], fine = false): Promise<BioMindUIResponse> {
      if (kind === 'approval') signal = signal ? AbortSignal.any([signal, AbortSignal.timeout(300_000)]) : AbortSignal.timeout(300_000);
      const approval = kind === 'approval' ? approvalOptions(proposal ?? {}, fine) : [];
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
        if (choice === 'adjust') return ask(ctx,kind,toolCallId,question,signal,proposal,undefined,true);
        let replacement: Record<string, unknown> | undefined;
        if (choice === 'edit') {
          const edited = await ctx.ui.input('输入修改后的命令；随后需要重新确认', undefined, { signal });
          if (!edited?.trim()) return { ...request, choice: 'cancel', cancelled: true };
          replacement = { cmd: edited };
        }
        if (choice === 'select') {
          const selections: NonNullable<BioMindUIResponse['selections']> = [];
          const targets=asObject(proposal?.payload).targets as Array<Record<string, any>>;
          for (const [index,target] of targets.entries()) {
            const options=[{label:'跳过此项'} as {label:string;access?:'read'|'read_write';lifetime?:'request'|'session'}];
            for (const lifetime of ['request','session'] as const) for (const access of ['read','read_write'] as const) {
              if (access==='read_write' && target.access!=='read_write' || access==='read' && !target.existed_at_proposal) continue;
              options.push({label:`${access==='read'?'读取':'读写'} · ${lifetime==='request'?'当前任务':'当前持久会话'}`,access,lifetime});
            }
            const answer=await ctx.ui.select(`选择：${target.root}`,options.map(o=>o.label),{signal});
            if (answer===undefined) return {...request,choice:'cancel',cancelled:true};
            const option=options.find(o=>o.label===answer);
            if (option?.access && option.lifetime) selections.push({index,access:option.access,lifetime:option.lifetime});
          }
          return selections.length ? {...request,choice:'y',selections} : {...request,choice:'cancel',cancelled:true};
        }
        return { ...request, choice, replacement, cancelled: choice === 'cancel' };
      }
      return new Promise((resolve, reject) => {
        const abort = () => { pendingUI.delete(request.id); reject(signal?.reason ?? new Error('User interaction aborted; no answer or approval was recorded')); };
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
    async function withApproval(ctx: ExtensionContext, method: string, params: Record<string, unknown>, toolCallId: string, signal?: AbortSignal,
      dispatch?: (method: string, params: Record<string, unknown>) => Promise<unknown>) {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const checkOwner = () => {
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_MUTATION_OWNER');
      };
      // This callback is constructed only inside the extension. Preserve the
      // privileged host transport on both initial calls and approval retries.
      const perform = dispatch ?? ((nextMethod, nextParams) => invoke(ctx, nextMethod, nextParams, signal));
      let value = await perform(method, params);
      checkOwner();
      while (asObject(value).status === 'waiting_approval') {
        checkOwner();
        const proposal = asObject(asObject(value).approval);
        if (!proposal.request_id || !proposal.action_hash || !proposal.request_scope) throw new Error('Incomplete authorization object');
        // Consult the original live ledger immediately before presenting a wait.
        // Another directory decision may already cover this action's own hash.
        const live = asObject(await client(ctx).hostCall('permissions.status', {approval_id:proposal.request_id}, {signal,requestId:ownerRequest}));
        checkOwner();
        if (live.status === 'allowed') { value = await perform(method,params); checkOwner(); continue; }
        if (live.status === 'cancelled' || live.status === 'denied') return live;
        let response: BioMindUIResponse;
        try { response = await ask(ctx, 'approval', toolCallId, formatApprovalProposal(proposal), signal, proposal); }
        catch (error) {
          try { await client(ctx).hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
            request_scope: proposal.request_scope, choice: 'cancel' }, { requestId: ownerRequest }); }
          finally { await pause(ctx, error instanceof Error && error.name === 'TimeoutError' ? 'approval_timeout' : 'approval_interrupted'); }
          throw error;
        }
        checkOwner();
        if (response.choice === 'adjust') response = await ask(ctx,'approval',toolCallId,formatApprovalProposal(proposal),signal,proposal,undefined,true);
        const choice = response.cancelled ? 'cancel' : response.choice;
        if (!choice || !approvalOptions(proposal,true).some(option => option.choice === choice)) { await pause(ctx, 'invalid_approval_response'); throw new Error('Approval requires an explicit offered decision'); }
        value = await client(ctx).hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
          request_scope: proposal.request_scope, choice, ...(response.selections ? { selections:response.selections } : {}), ...(response.replacement ? { replacement: response.replacement } : {}) },
          { signal, requestId: ownerRequest });
        checkOwner();
        if (choice === 'n' || choice === 'cancel') { await pause(ctx, choice === 'n' ? 'approval_denied' : 'approval_cancelled'); return value; }
        if (choice === 'edit') {
          if (asObject(value).status !== 'waiting_approval') return value;
          // Keep the original call identity. Python executes the newly approved
          // object's payload; replaying model/edited arguments must not replace it.
          continue;
        }
        await refreshApprovalStatus(ctx);
        value = await perform(method === 'install.propose' ? 'install.execute' : method, params);
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
    if (native) pi.registerTool(runtimeBash(async (input, signal, ctx) => {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const result = await client(ctx).hostCall('environments.launch', input, { signal, requestId: ownerRequest });
      signal?.throwIfAborted();
      if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_ENVIRONMENT_OWNER');
      return result as RuntimeBashLaunch;
    }));
    // Both metadata and Pi content reads use the same live Pi resource registry.
    // Trusted directories are host-derived and never part of a model schema.
    async function authorizeRead(ctx: ExtensionContext, absolutePath: string, id: string, signal?: AbortSignal,
      method: 'files.read_authorize' | 'files.list' = 'files.read_authorize') {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const checkOwner = () => {
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
      };
      checkOwner();
      const skillDirs = pi.getCommands().filter(command => command.source === 'skill')
        .map(command => path.dirname(command.sourceInfo.path));
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
      checkOwner();
      const receipt = asObject(await withApproval(ctx, method,
        { path: target, call_id: id, trusted_skill_dirs: trustedSkillDirs }, id, signal,
        async (method, params) => {
          checkOwner();
          const result = await client(ctx).hostCall(method, params, { signal, requestId: ownerRequest });
          checkOwner(); return result;
        }));
      checkOwner(); return { ...receipt, requested_path: absolutePath };
    }
    if (!native) for (const kind of DISCOVERY_TOOLS) {
      pi.registerTool({ ...discoveryDefinition(kind, process.cwd()), executionMode: 'sequential',
        async execute(id, args, signal, update, ctx) {
          const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
          const result = await nativeDiscovery(kind, ctx.cwd, {
            list: async (filename, activeSignal) => await authorizeRead(ctx, filename, id, activeSignal, 'files.list') as unknown as DirectorySnapshot,
            authorize: (filename, activeSignal) => authorizeRead(ctx, filename, id, activeSignal),
          }).execute(id, args, signal, update, ctx);
          signal?.throwIfAborted();
          if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
          return result;
        },
      } as ToolDefinition);
    }
    // Reuse Pi's schema, renderer, paging, image handling and errors. Only file
    // access is adapted to BioMind's existing domain boundary.
    if (!native) pi.registerTool({ ...createReadToolDefinition(process.cwd()), executionMode: 'sequential',
      async execute(id, args, signal, update, ctx) {
        const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
        const result = await nativeRead(ctx.cwd, absolutePath => authorizeRead(ctx, absolutePath, id, signal))
          .execute(id, args, signal, update, ctx);
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
        if (result.details?.sha256 && result.details?.write_path) {
          readVersions.set(`${ownerSession}:${ownerRequest}:${result.details.write_path}`, result.details.sha256);
        }
        return result;
      },
    });
    register('file_stat', native ? '按当前账号权限核对文件路径与元数据。' : '核对真实文件路径与元数据；项目外输入建议一次目录会话只读授权；最终范围由宿主明确批准。', OBJECT({ path: nonempty() }),
      async (id, args, signal, ctx) => {
        if (!native) return authorizeRead(ctx, args.path.startsWith('~') ? args.path : path.resolve(ctx.cwd, args.path), id, signal);
        signal?.throwIfAborted();
        const expanded = args.path === '~' ? homedir() : args.path.startsWith('~/') ? path.join(homedir(), args.path.slice(2)) : args.path;
        const filename = path.resolve(ctx.cwd, expanded);
        const info = await stat(filename); signal?.throwIfAborted();
        return { path: filename, realpath: await realpath(filename), size: info.size, is_file: info.isFile(), is_directory: info.isDirectory(), mtime_ms: info.mtimeMs };
      });
    if (!native) for (const kind of ['write', 'edit'] as const) {
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
            snapshot: async absolutePath => await withApproval(ctx,'files.mutation_snapshot', { path: absolutePath,call_id:id },id,signal,hostMutation) as any,
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
        if (response.cancelled || response.answer === undefined) { await pause(ctx, 'question_cancelled'); throw new Error('User cancelled the question; no answer available'); }
        return { question_id: response.id, request_id: response.requestId, question: args.question, answer: response.answer };
      });
    register('update_plan', '可选的当前分支工作笔记，不是执行许可或必须产物合同。', OBJECT({ steps: Type.Array(OBJECT({ step: nonempty(), status: Type.Union([Type.Literal('pending'), Type.Literal('in_progress'), Type.Literal('completed')]) })) }),
      async (_id, args, _signal, ctx) => { const value = current(ctx); publish(ctx, { ...value, plan: args.steps }); return { plan: args.steps, required_for_execution: false }; });
    const runtimeRequirements = OBJECT({ env: optionalString(), prefix: optionalString(), backend: optionalString(), host: optionalString(),
      packages: Type.Optional(Type.Array(Type.String())), tools: Type.Optional(Type.Array(Type.String())), capabilities: Type.Optional(Type.Array(Type.String())),
      versions: Type.Optional(Type.Record(Type.String(), Type.String())) });
    register('select_environment', '从登记或已加载 Skill 需求选择适用运行时，返回本请求 binding_ref。已有适用登记直接使用，不体检、不安装。约束冲突明确报告；未知保持未知。mode=locate 仅做有界静态元数据定位，不建立绑定。run_command 可复用唯一适用绑定；环境相关 bash 必须显式传 binding_ref。',
      OBJECT({ requirements: Type.Optional(runtimeRequirements), skill_id: optionalString(), env_name: optionalString(), runtime_scope: optionalString(), mode: Type.Optional(Type.Union([Type.Literal('select'), Type.Literal('locate')])) }),
      async (_id, args, signal, ctx) => {
        if (Object.keys(args).some(key => !['requirements', 'skill_id', 'env_name', 'runtime_scope', 'mode'].includes(key))) throw new Error('ENVIRONMENT_QUERY_FIELDS_INVALID');
        if (args.mode === 'locate' && (args.skill_id || args.runtime_scope)) throw new Error('ENVIRONMENT_LOCATE_USES_REQUIREMENTS_OR_ENV_NAME_ONLY');
        const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
        const loaded_skills = pi.getCommands().filter(command => command.source === 'skill')
          .map(command => ({ name: command.name.replace(/^skill:/, ''), file_path: command.sourceInfo.path }));
        const { mode, ...selection } = args;
        const result = mode === 'locate'
          ? await client(ctx).call('environments.locate', { requirements: { ...(selection.requirements ?? {}), ...(selection.env_name ? { env: selection.env_name } : {}) } }, { signal, requestId: ownerRequest })
          : await client(ctx).hostCall('environments.select', { ...selection, loaded_skills }, { signal, requestId: ownerRequest });
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_ENVIRONMENT_OWNER');
        return result;
      });
    register('register_environment', '补登记已存在的软件位置和声明能力，不下载安装、不健康检查。写入宿主选定权威；source_note/evidence_refs 是声明来源，不将模型声明升级为验证事实。返回软件/派生项各自状态。',
      OBJECT({ env_name: nonempty(), prefix: nonempty(), packages: Type.Optional(Type.Array(nonempty())), tools: Type.Optional(Type.Array(nonempty())),
        capabilities: Type.Optional(Type.Array(nonempty())), entries: Type.Optional(Type.Record(Type.String(), Type.String())),
        runtime_env: Type.Optional(Type.Record(Type.String(), Type.String())), env_runner: optionalString(), runner_argv: Type.Optional(Type.Array(Type.String())),
        source_note: optionalString(), evidence_refs: Type.Optional(Type.Array(Type.String())) }),
      async (id, args, signal, ctx) => {
        const params = { call_id: id, ...args };
        return native ? invoke(ctx, 'environments.register', params, signal) : withApproval(ctx, 'environments.register', params, id, signal);
      });
    const mapping: Array<[string, string, string, TSchema]> = [
      ['inspect_run', 'exec.status', '查询已有 run_ref 的真实状态；失败作业不等于查询失败。按需用 record=command 读取已记录命令/cwd/环境引用，record=script 和 script_index 读取实际冻结脚本；offset/max_chars 分页，遵守当前请求范围。不触发检查或重跑。', OBJECT({ run_ref: nonempty(), record: Type.Optional(Type.Union([Type.Literal('command'), Type.Literal('script')])), script_index: Type.Optional(Type.Integer({ minimum: 0 })), offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })) })],
      ['wait_run', 'exec.wait', '等待已有作业的有限状态变化；界面心跳不需要模型重复调用。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString(), max_wait_ms: Type.Optional(Type.Integer({ minimum: 0, maximum: 30000 })) })],
      ['read_run_log', 'exec.read_log', '分页读取真实日志；游标是服务返回的原始不透明字符串，完整日志留在原文件。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString() })],
      ['cancel_run', 'exec.cancel', '明确取消指定科学作业；与中断当前对话不同。', OBJECT({ run_ref: nonempty(), reason: nonempty() })],
      ['memory_search', 'memory.search', '按当前历史读取开关和项目 scope 查询长期记忆。', OBJECT({ query: nonempty(), source: Type.Optional(Type.Union([Type.Literal('wiki'), Type.Literal('projects'), Type.Literal('archive'), Type.Literal('current')])), scope: optionalString() })],
      ['memory_read', 'memory.read', '读取可用来源；关闭历史时不能读取旧项目历史。', OBJECT({ source_ref: nonempty(), scope: optionalString(), historical: Type.Optional(Type.Boolean()), offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })) })],
    ];
    for (const [name, method, description, schema] of mapping) register(name, description, schema, async (_id, args, signal, ctx) => invoke(ctx, method, args, signal));
    const pathSchema=OBJECT({paths:Type.Array(OBJECT({path:nonempty(),access:Type.Union([Type.Literal('read'),Type.Literal('read_write')]),target_kind:Type.Union([Type.Literal('file'),Type.Literal('directory')]),recursive:Type.Boolean()}),{minItems:1,maxItems:16}),purpose:nonempty(),lifetime:Type.Optional(Type.Union([Type.Literal('request'),Type.Literal('session')]))});
    if (!native) register('request_path_access','申请数据文件/目录的读取或读写范围。宿主选择最终子集与有效期；不改变项目、输出要求或自动审批模式。',pathSchema,
      async(id,args,signal,ctx)=>withApproval(ctx,'permissions.propose',{...args,call_id:id},id,signal));
    const commandSchema = OBJECT({ cmd: optionalString(), argv: Type.Optional(Type.Array(Type.String(), { minItems: 1 })), workdir: nonempty(), env_name: optionalString(), binding_ref: optionalString(), runtime_scope: optionalString(), requirements: Type.Optional(runtimeRequirements), timeout_ms: Type.Optional(Type.Integer({ minimum: 1 })), description: optionalString(),
      write_paths: Type.Optional(Type.Array(nonempty(),{description:native ? '可选写入意图记录，不授予权限或限制目录视图。' : '本次动作使用的写入文件/目录，相对 workdir；外部范围仍需宿主批准，不改变任务输出要求。'})),
      read_paths: Type.Optional(Type.Array(nonempty(), { description: native ? '可选输入来源记录，不构造沙箱目录视图。' : '只读输入文件/小目录，相对项目；URI 仅对 :: 前文件授权。目录每次最多 256 项、2 秒、总 512 挂载，private 排除、链接拒绝；启动后新增文件不可见。' })) });
    const probeSchema = OBJECT({ ...asObject(asObject(commandSchema).properties), environment_diagnostic: Type.Optional(Type.Boolean({ description: '仅环境故障定点诊断设 true：单次 15 秒、当前 request 累计 30 秒。用户明确检查及科学输入 QC 不设置此标志。' })) });
    register('probe_command', (native ? '有时限的本地程序核查；保留观察取消，不提供强制只读或网络隔离。' : '用户要求或实际错误需要的短程序核查：项目和输入只读、隔离网络、仅临时区可写，仍使用宿主授权和原作业观察取消。') + '默认15秒/64 KiB输出；用户明确检查或QC可用timeout_ms，不设置environment_diagnostic。仅环境故障定点诊断设environment_diagnostic=true，单次15秒、每request累计30秒；正式计算和安装不受诊断短预算影响。', probeSchema,
      async (id, args, signal, ctx) => withApproval(ctx, 'exec.probe', { call_id: id, arguments: args }, id, signal,
        (method, params) => client(ctx).hostCall(method, params, { signal, requestId: current(ctx).requestId })));
    register('run_command', (native ? '启动本地托管命令并返回 run_ref；保留持久日志、观察、取消与重连对账，无 BioMind 目录或命令审批。' : '通过领域执行服务启动受控命令并返回 run_ref，保留持久日志、观察和取消。') + '支持 shell 或 argv，计划可选。显式 binding_ref/env_name 使用准确目标；否则复用本请求和 scope 的唯一适用绑定，多绑定须明确指定。无绑定且无运行时要求时保持宿主执行。', commandSchema,
      async (id, args, signal, ctx) => withApproval(ctx, 'exec.start', { call_id: id, arguments: args }, id, signal));
    register('install_tool', '提出安装方案并等待原意图确认。验证必须检查目标 prefix 内的准确工具；结构化 executable/python_import/r_package 检查不能由任意 echo 成功替代。安装完成但登记失败时只恢复登记。', OBJECT({ cmd: nonempty(), env_name: nonempty(), description: nonempty(), packages: Type.Optional(Type.Array(Type.String())), prefix: nonempty(),
      verification_checks: Type.Array(OBJECT({ kind: Type.Union(['executable','python_import','r_package'].map(v => Type.Literal(v))), tool: nonempty(), entry: optionalString(), args: Type.Optional(Type.Array(Type.String())), modules: Type.Optional(Type.Array(nonempty())), packages: Type.Optional(Type.Array(nonempty())) }), { minItems: 1 }),
      entries: Type.Optional(Type.Record(Type.String(), Type.String())), runtime_env: Type.Optional(Type.Record(Type.String(), Type.String())), env_runner: optionalString(), runner_argv: Type.Optional(Type.Array(Type.String())) }),
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

    pi.on('session_start', async (event, ctx) => {
      activeContext = ctx; snapshotKnown = false; snapshotFailed = false; instructionsFailed = false;
      const id = sessionId(ctx);
      const initialization = ++backendEpoch;
      notifiedFailures.delete(id);
      updateBackend(ctx, { state: 'starting', stage: 'startup' });
      // Only an explicit reload retries a failed initial handshake. Accepted
      // requests and scientific jobs are never recreated or replayed here.
      const previous = clients.get(id);
      if (event.reason === 'reload' && previous?.startupFailed) {
        unobserveBackend(id); clients.delete(id);
        await previous.close({ disconnectOnly: true });
      }
      if (event.reason === 'resume' || event.reason === 'startup' &&
        (process.env.BIOMIND_PI_SESSION_EXISTING === '1' || ctx.sessionManager.getEntries().some(entry =>
          entry.type === 'message' || entry.type === 'custom' && entry.customType === REQUEST))) existingSessions.add(sessionId(ctx));
      {
        if (ctx.hasUI) ctx.ui.setStatus('biomind-execution', '正在初始化运行环境…');
        try {
          const connection = client(ctx);
          const live = asObject(await connection.hostCall('request.status', {}));
          if (initialization !== backendEpoch || id !== sessionId(ctx)) return;
          if (connection.lifecycle && connection.lifecycle.state !== 'ready') {
            // A status response and a pipe failure can arrive in the same turn.
            // The transport refuses this read-only call and waits for close to
            // collect the real failure; do not overwrite unavailable with ready.
            await connection.hostCall('request.status', {});
            throw connection.lifecycle.error ?? new Error('BACKEND_NOT_READY');
          }
          if (live.execution_profile && live.execution_profile !== profile) throw new Error('EXECUTION_PROFILE_RESTART_REQUIRED');
          readySessions.add(sessionId(ctx));
          updateBackend(ctx, { state: 'ready', stage: 'startup' });
        } catch (error) {
          if (initialization !== backendEpoch || id !== sessionId(ctx)) return;
          pi.events.emit('biomind:diagnostic', { code: 'EXECUTION_PROFILE_BINDING_FAILED',
            reason: String(error).match(/EXECUTION_PROFILE_[A-Z_]+/)?.[0], message: String(error) });
          setActiveTools([]); ctx.abort(); ctx.ui.setStatus('biomind-execution', `execution:unbound · requested:${profile}`);
          updateBackend(ctx, { state: 'failed', stage: 'startup', message: String(error) }); return;
        }
      }
      const epoch = ++welcomeEpoch;
      setActiveTools(MAIN_TOOLS);
      ctx.ui.setStatus('biomind-execution', `execution:${profile}${process.env.BIOMIND_PI_BUILD_ID ? ' · build:'+process.env.BIOMIND_PI_BUILD_ID : ''}`);
      ctx.ui.setStatus('biomind-approval', native ? 'approval:N/A' : state(ctx) ? 'approval:unknown' : 'approval:manual');
      if (native && ctx.hasUI) ctx.ui.notify('execution:native · 普通工具使用当前账号权限，不提供额外目录沙箱。', 'info');
      if (ctx.mode !== 'tui' || !ctx.hasUI) return;
      if (event.reason === 'reload') { savedInstructions.clear(); savedGlobalInstructions.clear(); }
      // Consume Pi's loaded metadata once per lifecycle, not on every render.
      try {
        welcomeProject = inspectNativeWelcomeInstructions(ctx.getSystemPrompt(), ctx.cwd);
      } catch { welcomeProject = undefined; instructionsFailed = true; }
      welcomePreference = { status: 'checking' };
      const metadata = await inspectWelcomePreference(ctx.cwd, process.env);
      if (epoch !== welcomeEpoch) return;
      welcomePreference = metadata;
      ctx.ui.setStatus('biomind-welcome', undefined);
    });
    pi.registerCommand('biomind-task', {
      description: '当前任务：continue [指令] | revise <修订> | new <新任务>；继续不会恢复旧批准',
      getArgumentCompletions: argumentCompletions(['continue ', 'revise ', 'new ']),
      async handler(args, ctx) {
        const match = /^(continue|revise|new)(?:\s+([\s\S]*))?$/.exec(args.trim());
        if (!match || (match[1] !== 'continue' && !match[2]?.trim())) { ctx.ui.notify('用法：/biomind-task continue | revise <修订> | new <任务>', 'warning'); return; }
        if (!ctx.isIdle()) { ctx.ui.notify('先暂停当前模型推进；已有科学作业通过 /biomind-job 观察或取消。', 'warning'); return; }
        pi.sendUserMessage(match[1] === 'new' ? '[新任务] ' + match[2] : match[1] === 'continue' ? '继续当前任务。' + (match[2] ?? '') : match[2]!);
      },
    });
    pi.registerCommand('biomind-approval', {
      description: '审批与数据范围：status | paths | add <提案JSON> | revoke <grant_id> | manual | auto [--scope session]',
      getArgumentCompletions: argumentCompletions(['status', 'paths', 'add ', 'revoke ', 'manual', 'auto', 'auto --scope session']),
      async handler(args, ctx) {
        const input = args.trim() || 'status', value = state(ctx);
        if (native) { ctx.ui.notify('execution:native · auto/manual 不适用于普通工具。需要保护时在新进程使用 biomind --execution-profile protected；恢复其它模式会话需显式迁移。', 'info'); return; }
        const mode=input.split(/\s+/)[0]!;
        if (!ctx.isIdle()) { ctx.ui.notify('等待本轮结束或暂停后再更改审批模式。', 'warning'); return; }
        try {
          if (['status','paths'].includes(input)) { const status=await refreshApprovalStatus(ctx); if (!status) throw new Error('无法核验权限状态'); ctx.ui.notify(formatPermissionStatus(status),'info'); return; }
          if (!value) throw new Error('范围或模式修改需先提交任务；查看状态无需开始任务。');
          if (mode==='add') {
            const proposal=JSON.parse(input.slice(3).trim());
            if (value.status==='paused') throw new Error('任务已暂停，请先显式恢复。');
            publish(ctx,{...value,status:'active'});
            try {
              const result=await withApproval(ctx,'permissions.propose',{...proposal,call_id:'host-path-'+randomUUID()},'host-path');
              ctx.ui.notify(JSON.stringify(result),'info');
            } finally {
              if (state(ctx)?.requestId===value.requestId && state(ctx)?.status==='active') publish(ctx,value);
              await refreshApprovalStatus(ctx);
            }
            return;
          }
          if (mode==='revoke') {
            const listing=await observePermissions(ctx,{action:'status'}), grantId=input.slice(6).trim();
            await observePermissions(ctx,{action:'revoke',grant_id:grantId,request_scope:listing.request_scope,policy_revision:listing.policy_revision});
            ctx.ui.notify('已撤销此 grant 来源；其它覆盖来源及运行中作业见下方。\n'+formatPermissionStatus((await refreshApprovalStatus(ctx)) ?? {}),'info');return;
          }
          const parsed=/^(manual|auto)(?: --scope (request|session))?$/.exec(input);
          if (!parsed) throw new Error('用法：/biomind-approval status | paths | add <JSON> | revoke <grant_id> | manual | auto [--scope session]');
          const lifetime=parsed[2] ?? 'request';
          const live = asObject(await client(ctx).hostCall('request.status', {}, { requestId: value.requestId }));
          if (live.request_id && live.request_id !== value.requestId) throw new Error('BRANCH_REQUEST_MISMATCH');
          const paused = !!live.paused || value.status === 'paused';
          if (paused && !ctx.hasUI) throw new Error('REQUEST_PAUSED: host must explicitly resume before changing mode');
          if (mode === 'auto' && ctx.hasUI) {
            const action = (paused ? '恢复当前任务并开启' : '开启')+(lifetime==='session'?'当前持久会话':'当前任务')+'自动审批';
            const choice = await ctx.ui.select((paused ? '任务已暂停：此选择将明确恢复任务，但不会立即推进模型或启动作业。此前拒绝/取消的动作仍须新的人工决定。\n' : '') +
              '自动审批消费当前项目与已批准的数据范围，保持网络隔离。新增范围、升级权限、安装及策略修改仍需人工。拒绝/取消将暂停并关闭 auto。'+(lifetime==='session'?'当前会话包含恢复和后续业务，不含 /new 或 fork。':'新业务使本模式失效。'), ['取消', action]);
            if (choice !== action) return;
            if (paused) {
              await client(ctx).hostCall('request.resume', {}, { requestId: value.requestId });
              publish(ctx, { ...value, status: 'settled', pauseReason: undefined });
            }
          }
          const result = await client(ctx).hostCall('permissions.mode', { mode,lifetime }, { requestId: value.requestId });
          await refreshApprovalStatus(ctx);
          ctx.ui.notify(JSON.stringify(result), 'info');
        } catch (error) { ctx.ui.notify(String(error), 'error'); }
      },
    });
    pi.on('input', async (event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) {
        pi.events.emit('biomind:diagnostic', { code: 'BACKEND_NOT_READY', sessionId: sessionId(ctx),
          state: backendStates.get(sessionId(ctx))?.state ?? 'starting' });
        return { action: 'handled' };
      }
      if (!ctx.isIdle()) {
        if (/^(?:\[新任务\]|新任务[：:])/.test(event.text.trim())) { ctx.ui.notify('当前模型运行中；请结束后明确开始新任务。', 'warning'); return { action: 'handled' }; }
        if (event.streamingBehavior) inputQueue.push({ text: event.text, kind: event.streamingBehavior });
        return { action: 'continue' };
      }
      try {
        let value = state(ctx);
        const newTask = /^(?:\[新任务\]|新任务[：:])/i.test(event.text.trim());
        const resume = /^(?:继续(?:当前任务)?[。.!！\s]*|resume\b|continue\b)/i.test(event.text.trim());
        const pureResume = /^(?:继续(?:当前任务)?|resume|continue)[。.!！\s]*$/i.test(event.text.trim());
        let live:Record<string,any>={};
        if (value && !newTask) {
          live=asObject(await client(ctx).hostCall('request.status',{}));
          if (!live.request_id) {
            if (value.ownerSessionId) throw new Error('AUTHORITATIVE_REQUEST_MISSING: 当前会话账本缺失，请检查存储或明确新任务；不会从聊天恢复批准');
            value=undefined; // Inherited legacy entry is history, not a new owner's request.
          }
        }
        if (!value || newTask) {
          if (!value && requestFromBranch(ctx.sessionManager.getBranch())) ctx.ui.notify('历史已带入，权限未继承；本次输入建立当前会话的新任务。','info');
          value = await begin(ctx, event.text);
        }
        else {
          // Load current backend policy before changing branch state. Never resurrect a grant.
          if (live.request_id && live.request_id !== value.requestId) throw new Error('BRANCH_REQUEST_MISMATCH: 请明确开始新任务，历史权限不自动迁移');
          if (live.requires_new_request) throw new Error('PROFILE_MIGRATION_REQUIRES_NEW_REQUEST: 模式迁移不恢复旧请求，请用 /biomind-task new 明确开始新任务。');
          if ((value.status === 'paused' || live.paused) && !resume) {
            ctx.ui.notify('任务已暂停。使用 /biomind-task continue 明确恢复，或 /biomind-task new 开始新任务。', 'warning'); return { action: 'handled' };
          }
          if (resume && (value.status === 'paused' || live.paused)) await client(ctx).hostCall('request.resume', {}, { requestId: value.requestId });
          // A selected historical Pi branch can lag the live request revision.
          // Advance from the host receipt without importing the abandoned branch.
          const revised = pureResume ? live : asObject(await client(ctx).hostCall('request.revise', {}, { requestId: value.requestId }));
          const revision = Number.isInteger(revised.revision) && revised.revision > 0 ? revised.revision
            : pureResume ? value.revision ?? 1 : (value.revision ?? 1) + 1;
          value = { ...value, status: 'active', pauseReason: undefined, revision,
            userBasis: [...(value.userBasis ?? []), ...(pureResume ? [] : [{ text: event.text, revision }])], maintenance: undefined };
          publish(ctx, value);
        }
        const entries = value.boundary ? boundaryDrafts(ctx.sessionManager.getBranch(), value.boundary) : [];
        if (value.boundary && (!value.boundary.committed || entries.length)) {
          value = { ...value, boundary: { ...value.boundary, committed: true } };
          entries.push({ type: 'custom', customType: REQUEST, data: value });
        }
        preparedInputRequestId = value.requestId; lastOutcome = 'completed';
        await refreshApprovalStatus(ctx);
        setActiveTools(MAIN_TOOLS);
        return { action: 'continue', ...(entries.length ? { biomindBoundary: { leafId: ctx.sessionManager.getLeafId(), entries } } : {}) };
      } catch (error) {
        preparedInputRequestId = undefined;
        pi.events.emit('biomind:diagnostic', { code: 'REQUEST_INITIALIZATION_FAILED', message: String(error),
          ...(error instanceof DomainHandshakeTimeoutError ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: error.timeoutMs } : {}) });
        ctx.ui.notify('本次输入未发送给模型：' + String(error), 'error'); return { action: 'handled' };
      }
    });
    pi.on('before_agent_start', async (event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) { ctx.abort(); throw new Error('BACKEND_NOT_READY'); }
      event.systemPromptOptions.contextFiles = filterNativeContextFiles(event.systemPromptOptions.contextFiles, ctx.cwd, getAgentDir());
      if (state(ctx)?.status !== 'active') { ctx.abort(); throw new Error('REQUEST_NOT_ACTIVE'); }
      preparedInputRequestId = undefined; initialUserPending = true;
      event.systemPromptOptions.selectedTools = toolNames(MAIN_TOOLS);
    });
    pi.on('message_start', async (event, ctx) => {
      if (event.message.role !== 'user') return;
      if (initialUserPending) { initialUserPending = false; return; }
      if (!inputQueue.length) return;
      const text = textOf(event.message);
      const index = inputQueue.findIndex(item => item.text === text);
      if (index < 0) { ctx.abort(); throw new Error('Cannot correlate queued user revision'); }
      inputQueue.splice(index, 1);
      const value = current(ctx);
      const revised = asObject(await client(ctx).hostCall('request.revise', {}, { requestId: value.requestId }));
      const revision = Number.isInteger(revised.revision) && revised.revision > 0 ? revised.revision : (value.revision ?? 1) + 1;
      publish(ctx, { ...value, revision, userBasis: [...(value.userBasis ?? []), { text, revision }] });
    });
    pi.on('turn_end', async (event, ctx) => {
      // turn_end follows Pi persistence of the complete tool group, so archive
      // identities are actual SessionManager entry IDs, never guessed offsets.
      await archiveTurn(ctx);
      lastOutcome = event.outcome;
      const value = state(ctx);
      if (value?.status === 'active' && (ctx.signal?.aborted || event.outcome === 'aborted')) {
        await pause(ctx, 'user_aborted'); return;
      }
      if (!value || value.status !== 'active' || event.outcome !== 'completed' || ctx.signal?.aborted) return;
      {
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
    });
    pi.on('context', async (event, ctx) => {
      if (state(ctx)?.status !== 'active') { ctx.abort(); return { messages: [] }; }
      if (ctx.signal?.aborted) return { messages: normalizeLegacyHistory(event.messages) };
      try {
        const value = current(ctx);
        assertStageBoundary(ctx.sessionManager.getBranch(), value.boundary);
        await client(ctx).hostCall('request.begin', { request_id: value.requestId, role: 'main',
          ...(value.boundary ? { boundary_id: value.boundary.id } : {}), user_revision: value.revision ?? 1 }, { signal: ctx.signal });
        if (value.boundary && !value.boundary.activated) publish(ctx, { ...value, boundary: { ...value.boundary, activated: true } });
        ctx.ui.setStatus('biomind', `${value.role} · ${value.status}`);
        pi.events.emit('biomind:state', { ...value, sessionId: sessionId(ctx) });
      } catch (error) { ctx.abort(); pi.events.emit('biomind:diagnostic', { code: 'STAGE_BOUNDARY_INCOMPLETE', message: String(error) }); return { messages: [] }; }

      try {
        snapshot = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
        if (typeof snapshot.history_enabled !== 'boolean') throw new Error('Domain context snapshot omitted history policy');
        if (snapshot.request_paused) { await pause(ctx, 'backend_paused'); return { messages: [] }; }
        snapshotKnown = true; snapshotFailed = false;
        // A provider-bound context refresh consumes global preferences. Native
        // AGENTS edits remain pending until Pi reloads its own resource snapshot.
        const preference = asObject(snapshot.preferences);
        if (['loaded', 'truncated', 'empty'].includes(preference.status)) {
          for (const filename of [preference.source_path, preference.preferred_path]) {
            if (typeof filename === 'string') savedGlobalInstructions.delete(path.resolve(filename));
          }
        }
      } catch (error) {
        // ExtensionRunner reports thrown hook errors and can keep the original
        // messages. Explicitly abort when current request state cannot be verified.
        snapshot = { history_enabled: false }; snapshotKnown = false; snapshotFailed = true; ctx.abort();
        if (!ctx.signal?.aborted) pi.events.emit('biomind:diagnostic', { code: 'CONTEXT_POLICY_UNAVAILABLE', message: String(error) });
        return { messages: normalizeLegacyHistory(event.messages) };
      }
      return { messages: normalizeLegacyHistory(event.messages) };
    });
    pi.on('context_with_system', (event, ctx) => {
      const value = state(ctx);
      const currentContext = { request_id: value?.requestId, plan: value?.plan,
        preferences: Object.fromEntries(Object.entries(asObject(snapshot.preferences)).filter(([key]) => key !== 'loaded_at')), user_revision: value?.revision, history_enabled: snapshot.history_enabled,
        authorization_refs: snapshot.authorization_refs ?? [], approval_mode: snapshot.approval_mode ?? { mode: 'manual' },
        revision: snapshot.revision, maintenance: snapshot.history_enabled && value?.maintenance?.outboxId && !value.maintenance.applied
          ? { prompt: value.maintenance.prompt, sources: value.maintenance.sources, instruction: '用户任务已答复。这是一次受限记忆维护；仅可提交有当前公开证据支持的patch或空operations，不运行新分析、不新增任务。' } : undefined };
      const messages = currentNativeProjectContext(event.messages).map(message => message.role === 'system' && message.sections?.['biomind-current']
        ? { ...message, sections: Object.fromEntries(Object.entries(message.sections).filter(([key]) => key !== 'biomind-current')) } : message);
      const rules = value?.maintenance?.outboxId ? MAINTENANCE_RULES : `${COMMON_RULES}\n${native ? NATIVE_RULES : PROTECTED_RULES}`;
      messages.push({ role: 'system', content: '', sections: { 'biomind-current': `${rules}\n当前有效状态与偏好：\n${JSON.stringify({ ...currentContext, execution_profile: profile, ...(native ? { approval_mode: 'not_applicable', authorization_refs: [] } : {}) })}` }, timestamp: Date.now() });
      return { messages };
    });
    pi.on('session_before_compact', async event => {
      // Preserve Pi's material, previous summary and file references. The
      // compatibility bridge appends focus to Pi's own history/prefix templates.
      event.preparation.messagesToSummarize = normalizeLegacyHistory(event.preparation.messagesToSummarize);
      event.preparation.turnPrefixMessages = normalizeLegacyHistory(event.preparation.turnPrefixMessages);
      return { customInstructions: await loadCompactionInstructions(event.customInstructions) };
    });
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
        const sources = branch.slice(start + 1).filter(entry => entry.type === 'message').filter(entry => entry.message.role === 'user'
          || (entry.message.role === 'toolResult' && !['memory_search', 'memory_read', 'maintenance_submit'].includes(entry.message.toolName)))
          .map(entry => ({ source_ref: `pi:${sessionId(ctx)}:${entry.id}`, kind: entry.message.role === 'user' ? 'user' : 'tool_result', content: textOf(entry.message) }));
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
      if (value.status === 'paused') {
        // The permission/request remains paused, but the host still needs the
        // terminal model-loop observation to report an aborted turn truthfully.
        pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role,
          outcome: 'aborted', reason: value.pauseReason, scientificSuccess: null });
        return;
      }
      publish(ctx, { ...value, status: lastOutcome === 'completed' ? 'settled' : lastOutcome });
      pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role, outcome: lastOutcome, scientificSuccess: null });
    });
    pi.on('tool_call', (event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) return { block: true, reason: 'BioMind backend is unavailable', terminate: true };
      const value = state(ctx);
      if (native && (NATIVE_TOOLS as readonly string[]).includes(event.toolName) || native && event.toolName === 'file_stat') {
        if (!readySessions.has(sessionId(ctx)) || value?.status === 'paused' || value?.maintenance?.outboxId)
          return { block: true, reason: 'Session is paused or execution profile is not bound', terminate: true };
        return;
      }
      const allowed = toolNames(value?.maintenance?.outboxId && !value.maintenance.applied ? ['maintenance_submit'] : MAIN_TOOLS);
      if (value?.status !== 'active' || !allowed.includes(event.toolName)) return { block: true, reason: 'This execution entry is not available for the current BioMind request', terminate: true };
    });
    if (!native) pi.on('user_bash', () => ({ result: { output: 'Shell 快捷入口在 BioMind protected 模式不可用；请通过领域 run_command 执行并保留授权与作业句柄。', exitCode: 1, cancelled: false, truncated: false } }));
    const guardLifecycle = async (ctx: ExtensionContext, action: string) => {
      if (!clients.has(sessionId(ctx))) return; // No domain owner/jobs exist before the first domain operation.
      try {
        const value = asObject(await client(ctx).call('session.lifecycle', { action }));
        if (typeof value.active_jobs !== 'boolean') throw new Error('Missing owner status');
        if (value.active_jobs) { ctx.ui.notify('当前仍有科学作业；请先观察或明确取消后再更换会话。', 'warning'); return { cancel: true }; }
      } catch { ctx.ui.notify('无法确认领域作业状态，已保留当前会话。', 'warning'); return { cancel: true }; }
    };
    pi.on('session_before_switch', async (event, ctx) => {
      if (event.reason === 'resume' && event.targetSessionFile) {
        try {
          const header = JSON.parse(readFileSync(event.targetSessionFile, 'utf8').split('\n')[0]!);
          const bound = sessionProfile(process.env.BIOMIND_PI_DATA_ROOT ?? path.join(ctx.cwd, '.biomind/pi-domain'), header.id);
          if (bound !== profile) { ctx.ui.notify(`会话绑定 execution:${bound}，当前进程 execution:${profile}；请以 biomind --execution-profile ${bound} --session ${JSON.stringify(event.targetSessionFile)} 启动新进程。`, 'warning'); return { cancel: true }; }
          const root = readTargetSessionRoot(event.targetSessionFile);
          if (await realpath(root) !== await realpath(ctx.cwd)) {
            ctx.ui.notify(`目标会话属于其它项目。当前会话与科学作业保留，请在系统终端重启：\n${restartProjectCommand(root, { ...restartOptions(ctx), session: path.resolve(event.targetSessionFile) })}`, 'warning');
            return { cancel: true };
          }
        } catch { ctx.ui.notify('目标会话目录无法确认；已保留当前会话。', 'warning'); return { cancel: true }; }
      }
      return guardLifecycle(ctx, 'switch');
    });
    pi.on('session_before_fork', (_event, ctx) => guardLifecycle(ctx, 'fork'));
    pi.on('session_before_tree', async (event, ctx) => {
      const blocked = await guardLifecycle(ctx, 'tree'); if (blocked) return blocked;
      const sourceState = state(ctx), targetState = requestFromBranch(ctx.sessionManager.getBranch(event.preparation.targetId));
      const omitted = new Set(sourceState?.boundary?.targets ?? []);
      if (event.preparation.userWantsSummary && (sourceState?.requestId !== targetState?.requestId || event.preparation.entriesToSummarize.some(entry => omitted.has(entry.id)))) {
        ctx.ui.notify('跨任务切换请关闭分支摘要；原始分支仍可查看，旧任务不会被带入目标请求。', 'warning'); return { cancel: true };
      }
      if (event.preparation.userWantsSummary) {
        // Preserve Pi's original replace && nonempty-custom condition: adding
        // default focus must not turn an empty replacement into a template override.
        return { customInstructions: await loadCompactionInstructions(event.preparation.customInstructions),
          replaceInstructions: Boolean(event.preparation.replaceInstructions && event.preparation.customInstructions) };
      }
    });
    pi.on('session_tree', async (_event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) return;
      setActiveTools(MAIN_TOOLS);
      await refreshApprovalStatus(ctx);
    });
    pi.on('session_shutdown', async (event, ctx) => {
      welcomeEpoch++;
      backendEpoch++;
      unobserveBackend(sessionId(ctx));
      updateBackend(ctx, { state: 'closed' });
      rejectPending('Session connection closed; no approval was granted', sessionId(ctx));
      inputQueue.splice(0); initialUserPending = false; preparedInputRequestId = undefined; activeContext = undefined;
      if (event.reason === 'reload') {
        snapshot = { history_enabled: false }; snapshotKnown = false;
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
