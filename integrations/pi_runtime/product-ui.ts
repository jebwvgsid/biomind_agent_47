/** BioMind presentation over Pi's existing editor/session owner; no model or domain writes. */
import { truncateToVisualLines, type ExtensionAPI, type ExtensionContext } from '@earendil-works/pi-coding-agent';

export interface BioMindProductState {
  role?: 'Consult' | 'Generate';
  status?: 'active' | 'settled' | 'aborted' | 'error';
}
export type BioMindProductStateGetter = (ctx: ExtensionContext) => Readonly<BioMindProductState> | undefined;

const WELCOME = 'BioMind · /help 帮助 · /details 详情 · /exit 退出';
const FIELD_LIMIT = 512;
// Remove complete terminal escape sequences before remaining controls. Fields are display copies only.
const ESCAPES = /\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\[[0-?]*[ -/]*[@-~]|[@-_])|\x9d[^\x9c]*(?:\x9c|$)/g;
function field(value: unknown, fallback = '未提供'): string {
  if (typeof value !== 'string' || !value) return fallback;
  const text = value.slice(0, FIELD_LIMIT * 4).replace(ESCAPES, '')
    .replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return fallback;
  const characters = Array.from(text);
  return characters.length > FIELD_LIMIT ? characters.slice(0, FIELD_LIMIT - 1).join('') + '…' : text;
}

function status(ctx: ExtensionContext, getState: BioMindProductStateGetter): string {
  const state = getState(ctx);
  if (state?.status === 'aborted') return '已中断';
  if (state?.status === 'error') return '错误';
  if (state?.status === 'settled') return '本次请求已结束';
  if (state?.status === 'active' && state.role === 'Consult') return '咨询中（Consult）';
  if (state?.status === 'active' && state.role === 'Generate') return '执行中（Generate）';
  return ctx.isIdle() ? '待输入' : '处理中';
}

function modelText(ctx: ExtensionContext): string {
  const model = ctx.model;
  return model ? field(model.provider) + '/' + field(model.id) : '未选择';
}

/** Only current display rows are computed; no transcript, request state or width history is retained. */
function view(lines: () => string[], maxRows: number) {
  let disposed = false;
  return {
    render(width: number): string[] {
      if (disposed || !Number.isFinite(width) || width < 1) return [];
      const columns = Math.max(1, Math.min(512, Math.floor(width)));
      // A wide Chinese glyph cannot fit in one column. Keep the terminal contract at that width.
      if (columns === 1) return ['…'];
      const rows: string[] = [];
      for (const line of lines()) {
        const bounded = field(line, '');
        if (!bounded) continue;
        const wrapped = truncateToVisualLines(bounded, FIELD_LIMIT * 2, columns, 0).visualLines;
        rows.push(...wrapped.slice(0, maxRows - rows.length));
        if (rows.length === maxRows) break;
      }
      return rows;
    },
    invalidate() { /* Pi requests a new projection on its normal render cycle. */ },
    dispose() { disposed = true; },
  };
}

export function installBioMindProductUI(pi: ExtensionAPI, getState: BioMindProductStateGetter,
  options: { commands?: boolean } = {}): void {
  const install = (_event: unknown, ctx: ExtensionContext) => {
    // RPC may have dialog UI, but owns no terminal components.
    if (ctx.mode !== 'tui' || !ctx.hasUI) return;
    // A display-only scalar projection; the Pi branch remains the authority.
    // Redraws of an unchanged leaf must not allocate the complete branch again.
    let cachedSession: string | undefined, cachedLeaf: string | null | undefined;
    let cached: BioMindProductState | undefined;
    const displayedState = () => {
      const session = ctx.sessionManager.getSessionId(), leaf = ctx.sessionManager.getLeafId();
      if (session !== cachedSession || leaf !== cachedLeaf) {
        const current = getState(ctx);
        cached = current ? { role: current.role, status: current.status } : undefined;
        cachedSession = session; cachedLeaf = leaf;
      }
      return cached;
    };
    ctx.ui.setHeader(() => view(() => [WELCOME], 3));
    ctx.ui.setFooter(() => view(() => [
      `BioMind · ${status(ctx, displayedState)} · ${field(ctx.cwd)}`,
      `模型 ${modelText(ctx)} · 思考 ${field(ctx.thinkingLevel)}`,
    ], 4));
    ctx.ui.setWidget('biomind-input', () => view(() => {
      const idle = ctx.isIdle();
      // getEditorText expands all paste markers. A label must never copy a
      // potentially large draft during every terminal render.
      return [idle ? '─ 你的输入' : '─ 草稿区 · 尚未提交的输入',
        idle ? '准备就绪 · 输入任务开始对话' : status(ctx, displayedState)];
    }, 2), { placement: 'aboveEditor' });
    ctx.ui.setTitle('BioMind · ' + field(ctx.cwd));
  };

  // In Pi 0.87.1 session_start covers startup/reload/new/resume/fork; there is no session_switch hook.
  pi.on('session_start', install);
  pi.on('session_tree', install);

  // The product's native host opts in. Headless callers retain their existing
  // prompt/slash-command semantics instead of silently swallowing UI commands.
  if (!(options.commands ?? process.env.BIOMIND_PI_UI_COMMANDS === '1')) return;

  // Verified against 0.87.1's builtin slash-command list: these names are not reserved.
  // Keep /quit, model/login/branch commands, editor and all keybindings with their original Pi owner.
  pi.registerCommand('help', {
    description: 'BioMind 帮助',
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      ctx.ui.notify('BioMind 帮助\n输入生信问题、任务或清单路径开始对话；执行提案需你确认。\n/status 查看当前状态\n/permissions 查看项目访问范围及设置方法\n/details [展开|收起] 查看工具详情\n/biomind-job list | status <run_ref> | logs <run_ref> | cancel <run_ref> 查看或明确取消作业\n/biomind-memory status 查看记忆设置\n/login deepseek 保存到 Pi 用户级模型配置（跨工作目录复用）\n/new 新会话 · /resume 恢复 Pi 会话 · /tree 会话分支；会话恢复不等于接管已有科学作业\n/exit 退出\nEsc 中断当前对话等待；科学作业需用作业命令明确取消。\n/hotkeys 查看当前快捷键。', 'info');
    },
  });
  pi.registerCommand('status', {
    description: '查看 BioMind 当前状态',
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      ctx.ui.notify(`BioMind 状态：${status(ctx, getState)}\n底座：Pi\n项目目录：${field(ctx.cwd)}\n模型：${modelText(ctx)}\n思考：${field(ctx.thinkingLevel)}\n/permissions 查看目录范围及设置方法`, 'info');
    },
  });
  pi.registerCommand('permissions', {
    description: '查看项目访问范围及设置方法（只读）',
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      ctx.ui.notify(`项目访问范围\n项目目录：${field(ctx.cwd)}\n普通文件工具在此目录及其子目录内访问；私密文件和受保护路径仍受限制，Skill 等宿主指定的只读来源另行处理。\n写入和命令执行仍遵循各自授权；选定项目目录不等于自动批准。\n更改范围：退出后在终端运行 biomind --project-dir '/绝对/项目目录'。不指定时使用启动目录。\n输入和输出应位于所选项目目录内；更换目录后请核对相对路径。\n当前没有分别配置输入只读目录、输出可写目录的统一入口。此命令只查看，不修改权限。`, 'info');
    },
  });
  pi.registerCommand('details', {
    description: '展开或收起工具详情（只读）',
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
}
