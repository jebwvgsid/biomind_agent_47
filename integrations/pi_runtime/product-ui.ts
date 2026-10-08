/** BioMind presentation over Pi's existing editor/session owner; permission actions use host callbacks. */
import { SettingsManager, getAgentDir, type ExtensionAPI, type ExtensionContext } from '@earendil-works/pi-coding-agent';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { argumentCompletions } from './instructions.js';
import { installProductFooter } from './product-footer.js';

export interface BioMindProductState {
  role?: 'Main' | 'Consult' | 'Generate';
  status?: 'active' | 'settled' | 'aborted' | 'error' | 'paused';
}
export type BioMindProductStateGetter = (ctx: ExtensionContext) => Readonly<BioMindProductState> | undefined;
export interface BioMindBackendState {
  state: 'starting' | 'ready' | 'unavailable' | 'failed' | 'closed';
  stage?: 'startup' | 'runtime';
  message?: string;
}
export interface BioMindProductDetails { memory?: string; instructions?: string; authentication?: string; backend?: BioMindBackendState; }
export const PRODUCT_RESUME_HINT = '恢复会话请在终端运行 biomind --resume 选择历史，或 biomind --session <会话ID>。恢复会话不会修复执行保护错误；临时会话无法恢复。';
export interface BioMindPermissionUI {
  status(ctx: ExtensionContext): Promise<Record<string, any>>;
  revoke(ctx: ExtensionContext, grant: Record<string, any>, listing: Record<string, any>): Promise<Record<string, any>>;
}
/** Ledger values are display copies; querying this page never creates a grant. */
export function formatPermissionStatus(value: Record<string, any>): string {
  if (value.execution_profile === 'native') return 'execution:native · 普通工具使用当前账号权限，不提供额外目录沙箱。\nauto/manual 和目录 grant 不适用于普通工具。';
  const grants: Record<string, any>[] = Array.isArray(value.grants) ? value.grants : [];
  const rows = ['BioMind 数据范围授权（原权限账本）', `项目：${field(value.project_root)}`,
    `会话：${field(value.session_id)}`, `当前分析请求：${field(value.request_scope?.request_key ?? value.request_scope)}`,
    `审批：${field(value.approval_mode?.mode,'manual')} · ${field(value.approval_mode?.lifetime,'request')} · 来源 ${field(value.approval_mode?.source,'默认人工')}`,
    `任务：${value.paused ? '已暂停；有效范围不会代替恢复决定' : '未暂停'}`,
    'request 仅当前业务；session 包含恢复同一持久会话，不含 /new、fork 或其它项目。'];
  if (!grants.length) rows.push('当前无外部范围授权。');
  for (const grant of grants) rows.push(`${field(grant.grant_id)} · ${grant.revoked ? '已撤销' : grant.active ? '有效' : '失效'} · ${grant.access==='read_write'?'读写':'只读'} · ${field(grant.lifetime,'request')} · ${field(grant.root)} · ${grant.target_kind === 'directory' ? (grant.recursive ? '目录含子目录' : '目录不含子目录') : '精确文件'} · ${grant.ordinary_operations_active ? '范围内普通操作免逐次确认' : '动作按当前审批模式确认'}`);
  // A ledger listing proves file-read grants only. Formal protection is a
  // separate launch capability and must not be inferred from a grant/status flag.
  rows.push('范围许可与动作审批独立；正式执行以对应启动回执为准。目录命令读取为有界文件快照，后续新增文件不可见。');
  rows.push('撤销只移除此 grant 来源，其它有效来源仍可覆盖。已打开句柄/运行作业不立即失效；停止使用 /biomind-job cancel <run_ref>。');
  if (value.running_jobs?.length) rows.push('当前运行作业（撤销不会自动停止）：'+value.running_jobs.map((r:unknown)=>field(r)).join(', '));
  rows.push('申请：/biomind-approval add <提案JSON>；撤销：/permissions revoke <grant_id>。查看本页不会授权。');
  return rows.join('\n');
}

const WELCOME = 'BioMind · 科研分析助手';
const FIELD_LIMIT = 512;
// Remove complete terminal escape sequences before remaining controls. Fields are display copies only.
const ESCAPES = /\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\[[0-?]*[ -/]*[@-~]|[@-_])|\x9d[^\x9c]*(?:\x9c|$)/g;
function field(value: unknown, fallback = '未提供'): string {
  if (typeof value !== 'string' || !value) return fallback;
  const text = value.slice(0, FIELD_LIMIT * 4).replace(/\p{Surrogate}/gu, '').replace(ESCAPES, '')
    .replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return fallback;
  const characters = Array.from(text);
  return characters.length > FIELD_LIMIT ? characters.slice(0, FIELD_LIMIT - 1).join('') + '…' : text;
}

function status(ctx: ExtensionContext, getState: BioMindProductStateGetter): string {
  const state = getState(ctx);
  if (state?.status === 'paused') return '任务已暂停';
  if (state?.status === 'aborted') return '已中断';
  if (state?.status === 'error') return '错误';
  if (state?.status === 'settled') return '本次请求已结束';
  if (state?.status === 'active') return '处理中';
  return ctx.isIdle() ? '待输入' : '处理中';
}

function modelSelected(ctx: ExtensionContext): boolean {
  return !!ctx.model && !(ctx.model.provider === 'unknown' && ctx.model.id === 'unknown');
}

function backendLabel(detail: BioMindProductDetails): string {
  switch (detail.backend?.state) {
    case 'ready': return '直接输入问题即可开始';
    case 'failed': return `${detail.backend.stage === 'runtime' ? '后台连接中断' : '后台初始化失败'}：${field(detail.backend.message, '请查看错误提示')}`;
    case 'unavailable': return '后台连接已断开，正在收集诊断；分析输入已停止';
    case 'closed': return '后台已关闭；分析输入已停止';
    default: return '正在初始化后台，请稍候；尚未接受分析输入';
  }
}
function modelText(ctx: ExtensionContext): string {
  const model = ctx.model;
  return model && modelSelected(ctx) ? field(model.provider) + '/' + field(model.id) : '尚未选择';
}

/** Display-only preview of an accepted user message; never rename or rewrite the session. */
function questionPreview(content: unknown): string | undefined {
  const text = typeof content === 'string' ? content : Array.isArray(content)
    ? content.find(part => part?.type === 'text' && typeof part.text === 'string')?.text : undefined;
  return typeof text === 'string' ? field(text, '') || undefined : undefined;
}

function elapsedLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const mm = String(Math.floor(seconds / 60) % 60).padStart(2, '0');
  const ss = String(seconds % 60).padStart(2, '0');
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Public pi-tui exports, resolved from Pi's installed dependency (no extra install). */
interface LayoutTools {
  visibleWidth(text: string): number;
  truncateToWidth(text: string, width: number, ellipsis?: string): string;
  wrapTextWithAnsi(text: string, width: number): string[];
}
let layoutTools: Promise<LayoutTools> | undefined;
function getLayoutTools(): Promise<LayoutTools> {
  return layoutTools ??= (async () => {
    const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
    return await import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href) as LayoutTools;
  })();
}
function quietPresentation(ctx: ExtensionContext): { enabled: boolean; source: string } {
  if (process.env.BIOMIND_PI_QUIET_STARTUP !== 'native') return {
    enabled: process.env.BIOMIND_PI_QUIET_STARTUP === '1', source: '宿主显示投影',
  };
  const settings = SettingsManager.create(ctx.cwd, getAgentDir(), { projectTrusted: ctx.isProjectTrusted() });
  const project = settings.getProjectSettings(), global = settings.getGlobalSettings();
  const source = ctx.isProjectTrusted() && typeof project.quietStartup === 'boolean' ? 'Pi 受信项目设置'
    : typeof global.quietStartup === 'boolean' ? 'Pi 用户设置' : 'Pi 默认';
  return { enabled: settings.getQuietStartup(), source };
}

/** Trusted layout stays intact; only the interpolated fields pass through field(). */
function welcomeRows(ctx: ExtensionContext, detail: BioMindProductDetails, width: number, height: number, tools: LayoutTools,
  sessionRow: (stop?: number) => string): string[] {
  const fit = (text: string, columns = width) => tools.truncateToWidth(text, Math.max(1, columns), '…');
  const column = (label: string, value: unknown, stop = 10) => {
    const prefix = label + ' '.repeat(Math.max(1, stop - tools.visibleWidth(label)));
    return prefix + fit(field(value, '未核对'), width - tools.visibleWidth(prefix));
  };
  const project = field(path.basename(ctx.cwd)), model = modelText(ctx);
  const instructions = field(detail.instructions, '未核对');
  const memory = field(detail.memory, '未知');
  const readiness = backendLabel(detail);
  if (height < 12 || width < 32) {
    // At extremely small sizes, keep a usable native editor ahead of decoration.
    return [readiness, sessionRow(6), `项目 ${project}`].slice(0, Math.max(1, height - 8)).map(text => fit(text));
  }
  if (width < 72 || height < 22) {
    const summary = (label: string, value: string) => {
      const prefix = label + '  ', indent = ' '.repeat(tools.visibleWidth(prefix));
      const available = Math.max(1, width - tools.visibleWidth(prefix));
      const wrapped = tools.wrapTextWithAnsi(value, available);
      return [prefix + (wrapped[0] ?? ''), ...(wrapped.length > 1 ? [indent + fit(wrapped.slice(1).join(' '), available)] : [])];
    };
    return [WELCOME, column('项目', project, 6), sessionRow(6), column('模型', model, 6),
      ...summary('说明', instructions), ...summary('记忆', memory), '',
      '/project  /instructions', '/login /model /new /resume /name',
      detail.backend?.state === 'ready' ? '输入 / 搜索全部命令' : readiness].map(text => fit(text));
  }
  const fields = [column('项目', project), sessionRow(), column('目录', field(ctx.cwd)), '',
    column('模型', model), column('认证', field(detail.authentication, '未核对 · 连通性未验证')),
    column('说明', instructions), column('记忆', memory)];
  if (width < 96 || height < 28) return [WELCOME, '', ...fields, '', '常用操作  /login  /model',
    '/project  /instructions  /biomind-memory status', '/new  /resume  /name', '',
    `输入 / 搜索全部命令 · ${readiness}`].map(text => fit(text));
  return [WELCOME, '', ...fields, '', '常用操作',
    column('/login', '登录模型服务或配置 API Key', 26),
    column('/model', '选择模型', 26),
    column('/project', '查看、创建、打开分析项目', 26),
    column('/instructions', '全局说明与项目说明', 26),
    column('/biomind-memory status', '查看记忆设置', 26),
    column('/new  /resume  /name', '新会话 / 恢复 / 改名', 26), '',
    '输入 / 搜索全部命令', readiness];
}

export function installBioMindProductUI(pi: ExtensionAPI, getState: BioMindProductStateGetter,
  options: { commands?: boolean; details?: (ctx: ExtensionContext) => BioMindProductDetails; permissions?: BioMindPermissionUI } = {}): () => void {
  // Display-only lifecycle scalar. Pi owns the editor, branch and accepted messages.
  let active: { session: string; hasConversation: boolean; firstQuestion?: string; redraw: () => void } | undefined;
  let startedAt: number | undefined, finishedAt: number | undefined;
  let ticker: ReturnType<typeof setInterval> | undefined;
  const refresh = () => active?.redraw();
  const stopTicker = () => { if (ticker !== undefined) clearInterval(ticker); ticker = undefined; };
  const resetActivity = () => { stopTicker(); startedAt = undefined; finishedAt = undefined; };
  const activityText = () => startedAt === undefined ? undefined
    : `${finishedAt === undefined ? '本轮运行' : '本轮用时'} ${elapsedLabel((finishedAt ?? performance.now()) - startedAt)}`;
  const install = async (_event: unknown, ctx: ExtensionContext) => {
    if (ctx.mode !== 'tui' || !ctx.hasUI) return;
    // A historical active marker cannot prove this process is still running it.
    resetActivity();
    const tools = await getLayoutTools();
    const quiet = quietPresentation(ctx);
    const branch = ctx.sessionManager.getBranch();
    const firstUser = branch.find(entry => entry.type === 'message' && entry.message.role === 'user');
    const presentation = { session: ctx.sessionManager.getSessionId(),
      firstQuestion: firstUser?.type === 'message' && firstUser.message.role === 'user'
        ? questionPreview(firstUser.message.content) : undefined,
      hasConversation: branch.some(entry =>
        entry.type === 'message' || entry.type === 'compaction' || entry.type === 'branch_summary'
          || entry.type === 'custom_message' && entry.display), redraw: () => {} };
    active = presentation;
    let cachedSession: string | undefined, cachedLeaf: string | null | undefined;
    let cached: BioMindProductState | undefined;
    let titleSession: string | undefined, titleLeaf: string | null | undefined, cachedTitle = '';
    const sessionTitle = () => {
      const session = ctx.sessionManager.getSessionId(), leaf = ctx.sessionManager.getLeafId();
      if (session !== titleSession || leaf !== titleLeaf) {
        // The native getter scans session_info entries; a rename advances the leaf.
        cachedTitle = field(ctx.sessionManager.getSessionName(), '');
        titleSession = session; titleLeaf = leaf;
      }
      return cachedTitle;
    };
    const displayedState = () => {
      const session = ctx.sessionManager.getSessionId(), leaf = ctx.sessionManager.getLeafId();
      if (session !== cachedSession || leaf !== cachedLeaf) {
        const current = getState(ctx);
        cached = current ? { role: current.role, status: current.status } : undefined;
        cachedSession = session; cachedLeaf = leaf;
      }
      return cached;
    };
    const details = () => {
      try { return options.details?.(ctx) ?? {}; }
      catch { return { memory: '未知（读取失败）', instructions: '读取失败', authentication: '未核对' }; }
    };
    const emptyView = () => ({ render: (_width: number): string[] => [], invalidate() {} });
    // A header scrolls with native resource summaries. The factory widget stays
    // next to the real editor, so loaded Skills do not push the welcome offscreen.
    ctx.ui.setHeader(emptyView);
    // Pi still owns the counters; only their presentation changes.
    installProductFooter(ctx, tools, field);
    ctx.ui.setWidget('biomind-input', (tui, theme) => {
      let disposed = false;
      presentation.redraw = () => tui.requestRender();
      return {
        render(width: number): string[] {
          if (disposed || !Number.isFinite(width) || width < 1) return [];
          const columns = Math.max(1, Math.min(512, Math.floor(width)));
          const height = Math.max(1, tui.terminal.rows);
          if (columns === 1) return ['…'];
          const detail = details();
          const sessionRow = (stop = 10) => {
            const prefix = '会话' + ' '.repeat(Math.max(1, stop - 4));
            // Native UUIDv7 sessions created close together share a timestamp prefix.
            const id = Array.from(field(ctx.sessionManager.getSessionId(), '未知')).slice(-8).join('');
            const title = sessionTitle() || presentation.firstQuestion
              || (presentation.hasConversation ? '未命名对话' : '新对话');
            const suffix = ` · ${id}`;
            const available = columns - tools.visibleWidth(prefix) - tools.visibleWidth(suffix);
            // Reserve the ID before shortening long CJK/emoji names, including on narrow screens.
            return available > 0 ? prefix + tools.truncateToWidth(title, available, '…') + suffix
              : tools.truncateToWidth(`会话 ${id}`, columns, '…');
          };
          const full = !quiet.enabled && !presentation.hasConversation;
          const elapsed = activityText();
          const rows = full ? welcomeRows(ctx, detail, columns, height, tools, sessionRow) : height < 12
            ? [detail.backend?.state === 'ready' ? elapsed ?? sessionRow(6) : backendLabel(detail), '输入 / 搜索命令'].slice(0, Math.max(1, height - 8)) : [
            `BioMind · ${elapsed ? elapsed + ' · ' : ''}${field(path.basename(ctx.cwd))} · ${detail.backend?.state === 'ready' ? status(ctx, displayedState) : backendLabel(detail)}`,
            sessionRow(6),
            `模型 ${modelText(ctx)} · 说明 ${field(detail.instructions, '未核对')}`,
            `记忆 ${field(detail.memory, '未知')} · ${ctx.isIdle() ? '输入 / 搜索命令' : '草稿区 · 尚未提交'}`,
          ];
          return rows.map(line => {
            const bounded = tools.truncateToWidth(line, columns, '…');
            // Theme styling is trusted; never run a composed layout through field().
            return full && (line === WELCOME || line === '常用操作') ? theme.bold(bounded)
              : full && line.startsWith('输入 /') ? theme.fg('dim', bounded) : bounded;
          });
        },
        invalidate() {},
        dispose() {
          disposed = true;
          if (active === presentation) { resetActivity(); active = undefined; }
        },
      };
    }, { placement: 'aboveEditor' });
    ctx.ui.setTitle('BioMind · ' + field(ctx.cwd));
  };
  pi.on('session_start', install);
  pi.on('session_tree', install);
  pi.on('message_start', (event, ctx) => {
    if (ctx.mode !== 'tui' || event.message.role !== 'user' || active?.session !== ctx.sessionManager.getSessionId()) return;
    if (!active.hasConversation) active.firstQuestion = questionPreview(event.message.content);
    active.hasConversation = true;
    active.redraw();
  });
  pi.on('agent_start', (_event, ctx) => {
    if (ctx.mode !== 'tui' || !ctx.hasUI || active?.session !== ctx.sessionManager.getSessionId()) return;
    // Automatic retry/compaction and queued continuations are one continuous
    // Pi activity. A later agent_start must not reset an already-running clock.
    if (startedAt !== undefined && finishedAt === undefined) return;
    startedAt = performance.now(); finishedAt = undefined;
    active.redraw();
    ticker = setInterval(() => active?.redraw(), 1000);
    ticker.unref();
  });
  pi.on('agent_settled', (_event, ctx) => {
    if (ctx.mode !== 'tui' || active?.session !== ctx.sessionManager.getSessionId() || startedAt === undefined || finishedAt !== undefined) return;
    // agent_end can precede retries and continuations; settled is the final edge.
    finishedAt = performance.now(); stopTicker(); active.redraw();
  });
  pi.on('session_shutdown', () => { resetActivity(); active = undefined; });

  // The product's native host opts in. Headless callers retain their existing
  // prompt/slash-command semantics instead of silently swallowing UI commands.
  if (!(options.commands ?? process.env.BIOMIND_PI_UI_COMMANDS === '1')) return refresh;

  // Verified against 0.87.1's builtin slash-command list: these names are not reserved.
  // Keep /quit, model/login/branch commands, editor and all keybindings with their original Pi owner.
  pi.registerCommand('help', {
    description: 'BioMind 帮助',
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      ctx.ui.notify('BioMind 帮助\n输入生信问题、任务或清单路径开始对话；执行提案需你确认。\n/status 当前状态 · /project 项目目录与创建入口\n/instructions [global|project] 查看实际说明来源并编辑；保存后 /reload 刷新\n/permissions 项目访问范围 · /details [展开|收起] 工具详情\n/biomind-job list | status <run_ref> | logs <run_ref> | cancel <run_ref> 查看或明确取消作业\n/biomind-memory status 查看作用域；history on/off 仅本次；default on/off 保存启动默认\n/login 原生认证 · /model 选择模型；本地配置存在不等于连接已验证\n/new 新会话 · /resume 恢复 Pi 会话 · /tree 会话分支；跨项目恢复请按提示在系统终端重启\n系统终端命令：biomind project list | create <目录> | open <目录>（有历史时可选择新建或恢复）\nbiomind --resume（或 -r）启动时直接选择历史对话\n/exit 退出\nEsc 中断当前对话等待；科学作业需用作业命令明确取消。\n/hotkeys 查看当前快捷键。', 'info');
    },
  });
  pi.registerCommand('status', {
    description: '查看 BioMind 当前状态',
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      let detail: BioMindProductDetails;
      try { detail = options.details?.(ctx) ?? {}; } catch { detail = { memory: '未知', instructions: '加载失败' }; }
      const sessionPath = (value?: string) => value ? path.resolve(ctx.cwd, value) : undefined;
      const quiet = quietPresentation(ctx);
      const currentStatus = detail.backend?.state === 'ready' ? status(ctx, getState) : backendLabel(detail);
      ctx.ui.notify(`BioMind 状态：${currentStatus}\n计时：${activityText() ?? '本轮尚未开始'}\n底座：Pi\n项目目录：${field(ctx.cwd)}\n会话：${field(ctx.sessionManager.getSessionId())}\n会话文件：${field(sessionPath(ctx.sessionManager.getSessionFile?.()), '尚未写盘或临时会话')}\n会话目录：${field(sessionPath(ctx.sessionManager.getSessionDir?.()), '未知')}\n模型：${modelText(ctx)}\n思考：${field(ctx.thinkingLevel)}\n说明：${field(detail.instructions, '未核对（/instructions）')}\n记忆：${field(detail.memory, '未知（/biomind-memory status）')}\n认证：${field(detail.authentication, '原生 /login；未验证连通性')}\n欢迎设置：quietStartup=${quiet.enabled} · ${quiet.source}\n/permissions 查看实际数据范围、有效期和审批模式`, 'info');
    },
  });
  pi.registerCommand('permissions', {
    description: '查看当前请求的真实只读授权；revoke <grant_id> 撤销',
    getArgumentCompletions: argumentCompletions(['status', 'revoke ']),
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      try {
        if (!options.permissions) throw new Error('权限账本查询未连接');
        const parts = args.trim().split(/\s+/);
        if (args.trim() && !(parts.length === 1 && parts[0] === 'status') && !(parts.length === 2 && parts[0] === 'revoke')) throw new Error('用法：/permissions [status|revoke <grant_id>]');
        const listing = await options.permissions.status(ctx);
        ctx.ui.notify(formatPermissionStatus(listing), 'info');
        if (parts[0] !== 'revoke') return;
        const grant = (Array.isArray(listing.grants) ? listing.grants : []).find((entry: any) => entry.grant_id === parts[1] && entry.active && !entry.revoked);
        if (!grant) throw new Error('未找到当前有效的 grant 来源');
        const answer = await ctx.ui.select(`撤销此授权来源：${field(grant.root)}\n其它有效来源仍可能覆盖该路径。已运行科学作业保留，需要停止请明确取消作业。`, ['取消', '明确撤销该授权']);
        if (answer !== '明确撤销该授权') return;
        await options.permissions.revoke(ctx, grant, listing);
        ctx.ui.notify(formatPermissionStatus(await options.permissions.status(ctx)), 'info');
      } catch (error) { ctx.ui.notify(`权限操作失败：${String(error)}`, 'error'); }
    },
  });
  pi.registerCommand('details', {
    description: '展开或收起工具详情（只读）',
    getArgumentCompletions: argumentCompletions(['展开', '收起', 'on', 'off']),
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      if (ctx.mode !== 'tui') { ctx.ui.notify('工具详情展开仅适用于终端界面。', 'info'); return; }
      const choice = args.trim();
      if (choice && !['展开', '收起', 'on', 'off'].includes(choice)) {
        ctx.ui.notify('用法：/details [展开|收起]；查看详情不构成批准。', 'info'); return;
      }
      const expanded = choice ? ['展开', 'on'].includes(choice) : !ctx.ui.getToolsExpanded();
      ctx.ui.setToolsExpanded(expanded);
      ctx.ui.notify(`工具详情已${expanded ? '展开' : '收起'}；查看详情不构成批准。`, 'info');
    },
  });
  pi.registerCommand('exit', {
    description: '退出 BioMind',
    handler: async (_args, ctx) => { ctx.shutdown(); },
  });
  return refresh;
}
