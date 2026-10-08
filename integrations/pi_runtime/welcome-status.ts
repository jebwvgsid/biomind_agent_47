/** Read-only welcome metadata. Domain snapshots and native Pi resources remain the owners. */
import path from 'node:path';
import { realpath, stat } from 'node:fs/promises';
import { readEditableFile } from './instructions.js';
import type { ExtensionContext } from '@earendil-works/pi-coding-agent';

export interface PreferenceStatus {
  status?: string; source?: string; source_path?: string; preferred_path?: string;
  exists?: boolean; empty?: boolean;
}
export interface ProjectInstructionStatus { path: string; state: string; }
export interface MemoryStatus {
  history_enabled: boolean; memory_mode?: string; memory_root?: string | null;
  maintenance?: { enabled?: boolean };
}
type StartupEnvironment = Readonly<Record<string, string | undefined>>;
const secretFile = (filename: string) => path.basename(filename).startsWith('.env')
  || ['auth.json', 'credentials.json'].includes(path.basename(filename));

/** Pi 0.87.1 exposes the effective prompt on lifecycle contexts, and options only on command contexts.
 * Read its native resource wrappers once; retain only path and empty/nonempty metadata. */
export function inspectNativeWelcomeInstructions(prompt: string, cwd: string): ProjectInstructionStatus | undefined {
  const marker = '<project_context>\nProject-specific instructions and guidelines:\n\n';
  const start = prompt.indexOf(marker);
  if (start < 0) {
    if (prompt.includes('<project_context>')) throw new Error('Native instruction metadata format unknown');
    return undefined;
  }
  const end = prompt.indexOf('\n</project_context>', start);
  if (end < 0 || prompt.indexOf(marker, start + marker.length) >= 0) throw new Error('Ambiguous native instruction metadata');
  let remaining = prompt.slice(start + marker.length, end), project: ProjectInstructionStatus | undefined;
  const directories = new Set<string>();
  while (remaining) {
    const match = /^<project_instructions path="([^\n]+)">\n([\s\S]*?)\n<\/project_instructions>(?:\n\n|$)/.exec(remaining);
    if (!match || match[2].includes('<project_instructions') || match[2].includes('</project_context>')) throw new Error('Ambiguous native instruction metadata');
    const filename = match[1], directory = path.dirname(path.resolve(filename));
    if (directories.has(directory)) throw new Error('Ambiguous native instruction precedence');
    directories.add(directory);
    if (directory === path.resolve(cwd) && ['AGENTS.override.md', 'AGENTS.md', 'AGENTS.MD', 'CLAUDE.md', 'CLAUDE.MD'].includes(path.basename(filename))) {
      project = { path: filename, state: match[2].trim() ? '已加载' : '为空' };
    }
    remaining = remaining.slice(match[0].length);
  }
  return project;
}

/** Probe only the configured preference (or its existing Core fallback), never a directory or Wiki. */
export async function inspectWelcomePreference(cwd: string, env: StartupEnvironment): Promise<PreferenceStatus> {
  const root = env.BIOMIND_PI_INSTALL_ROOT;
  const supplied = env.BIOMIND_PI_PREFERENCES_PATH ?? env.BIOMIND_USER_PREFERENCES;
  if (!root && (!supplied || !path.isAbsolute(supplied))) return { status: 'unknown' };
  const preferred = path.resolve(root ?? cwd, supplied || 'user_preferences.md');
  let selected = preferred;
  let source = env.BIOMIND_PI_PREFERENCES_SOURCE ?? (supplied ? 'environment' : 'default');
  const metadata = (): PreferenceStatus => ({ preferred_path: preferred, source_path: selected, source });
  try {
    if (secretFile(preferred)) return { ...metadata(), status: 'unreadable' };
    try { await stat(preferred); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      if (env.BIOMIND_PI_MEMORY_ROOT) {
        selected = path.resolve(cwd, env.BIOMIND_PI_MEMORY_ROOT, 'USER_PROFILE.md'); source = 'core_fallback';
      }
    }
    let info;
    try { info = await stat(selected); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { ...metadata(), status: 'missing', exists: false };
      throw error;
    }
    if (!info.isFile()) return { ...metadata(), status: 'unreadable', exists: true };
    if (secretFile(await realpath(selected))) return { ...metadata(), status: 'unreadable', exists: true };
    if (info.size > 1024 * 1024) return { ...metadata(), status: 'uninspected', exists: true };
    const file = await readEditableFile(selected);
    // Mirror the domain preference's whitespace-empty semantics, retaining no body or hash.
    return { ...metadata(), status: file.exists ? file.text.trim() ? 'available' : 'empty' : 'missing',
      exists: file.exists, empty: file.exists && !file.text.trim() };
  } catch { return { ...metadata(), status: 'unreadable' }; }
}

export function describeWelcomeInstructions(preference: PreferenceStatus, project: ProjectInstructionStatus | undefined,
  saved: ReadonlySet<string> = new Set(), failed = false, savedGlobal: ReadonlySet<string> = saved): string {
  const source = ({ config: '配置', environment: '环境', default: '默认', core_fallback: 'Core回退' } as Record<string, string>)[preference.source ?? ''] ?? '来源未核对';
  const pending = [preference.source_path, preference.preferred_path].some(filename => filename && savedGlobal.has(path.resolve(filename)));
  const loaded = preference.status === 'loaded' || preference.status === 'truncated';
  const state = failed ? '读取或保存失败' : pending ? '已保存，待请求刷新' : preference.status === 'unreadable' ? '读取失败'
    : preference.status === 'missing' || preference.exists === false ? '缺失'
    : preference.empty || preference.status === 'empty' ? '为空'
    : loaded ? preference.status === 'truncated' ? '已加载（截断）' : '已加载'
    : preference.status === 'available' ? '可用（请求时加载）'
    : preference.status === 'uninspected' ? '存在（首页未读取）'
    : preference.status === 'checking' ? '读取中' : '未核对';
  const local = project ? `项目 ${path.basename(project.path)} ${saved.has(path.resolve(project.path)) ? '已保存，待 /reload' : project.state}` : '项目说明未加载';
  return `全局偏好${state}〔${source}〕 · ${local}`;
}

/** Startup configuration is not a running service snapshot. ask is conditional maintenance, not off. */
export function describeWelcomeMemory(snapshot: MemoryStatus | undefined, env: StartupEnvironment, failed = false): string {
  if (failed) return '项目按需检索未知 · 自动整理未知（状态读取失败）';
  const history = snapshot ? snapshot.history_enabled : env.BIOMIND_PI_HISTORY_ENABLED === '1' ? true
    : env.BIOMIND_PI_HISTORY_ENABLED === '0' ? false : undefined;
  const mode = snapshot ? snapshot.memory_mode : env.BIOMIND_PI_MEMORY_MODE ?? (history === undefined ? undefined : 'ask');
  const modeText = ({ ask: '自动整理需确认（ask）', manual: '自动整理关闭（manual）',
    auto: '自动整理开启（auto）', disabled: '整理禁用（disabled）' } as Record<string, string>)[mode ?? ''] ?? '自动整理未知';
  const noRoot = snapshot ? snapshot.memory_root === null : !env.BIOMIND_PI_MEMORY_ROOT;
  const disabled = history === false || noRoot || snapshot?.maintenance?.enabled === false;
  const maintenance = history === undefined ? '自动整理未知' : mode === 'disabled' ? modeText : disabled
    ? `自动整理未启用（${history === false ? '检索关闭' : noRoot ? '无记忆根' : mode ?? '策略'}）`
    : snapshot && snapshot.maintenance?.enabled !== true ? '自动整理未知' : modeText;
  const retrieval = history === undefined ? '未知' : history ? '开启' : '关闭';
  return snapshot ? `项目按需检索${retrieval} · ${maintenance}`
    : `启动配置：检索${retrieval} · ${maintenance}；运行待核对`;
}

/** This public Pi API returns source presence only; never resolve a key, OAuth token or key command. */
export function describeWelcomeAuthentication(ctx: Pick<ExtensionContext, 'model' | 'modelRegistry'>): string {
  if (!ctx.model || ctx.model.provider === 'unknown') return '未核对（先 /model 选择模型）';
  try {
    if (typeof ctx.modelRegistry?.getProviderAuthStatus !== 'function') return '未核对 · 连通性未验证';
    const value = ctx.modelRegistry.getProviderAuthStatus(ctx.model.provider);
    if (value.configured === false) return '尚未配置 · /login';
    if (value.configured !== true) return '未核对 · 连通性未验证';
    const source = ({ stored: '已存配置', runtime: '运行时', environment: '环境', fallback: '提供方配置',
      models_json_key: '模型配置', models_json_command: '密钥命令未执行' } as Record<string, string>)[value.source ?? ''];
    return `配置来源存在${source ? `（${source}）` : ''} · 连通性未验证`;
  } catch { return '未核对（读取失败） · 连通性未验证'; }
}
