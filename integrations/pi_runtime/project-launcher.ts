/** Thin directory metadata and pre-session menu. Pi owns all session discovery. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { ExtensionInputComponent, ExtensionSelectorComponent, getAgentDir, initTheme, SessionManager } from '@earendil-works/pi-coding-agent';
import { listProjectSessions, selectHistorySession } from './session-launcher.js';

const runFile = promisify(execFile);
const installation = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
export interface ProjectLauncherConfig {
  agentDir?: string; sessionDir?: string; memoryRoot?: string; wikiRoot?: string;
  installRoot?: string; launcherPython?: string; python?: string; configPath?: string;
}
export interface ProjectEntry {
  root: string; sources: string[]; availability: 'available' | 'missing' | 'unavailable' | 'not_directory';
  projectIds: string[];
}
export interface ProjectList { projects: ProjectEntry[]; incomplete: string[]; }
export function canonicalProjectRoot(root: string): string {
  const absolute = path.resolve(root);
  try { return fs.realpathSync(absolute); } catch { return absolute; }
}
function availability(root: string): ProjectEntry['availability'] {
  try {
    if (!fs.statSync(root).isDirectory()) return 'not_directory';
    fs.accessSync(root, fs.constants.R_OK | fs.constants.X_OK); return 'available';
  } catch (error) { return (error as NodeJS.ErrnoException).code === 'ENOENT' ? 'missing' : 'unavailable'; }
}
function launcherPath(config: ProjectLauncherConfig) { return path.join(path.resolve(config.agentDir ?? getAgentDir()), 'biomind-launcher.json'); }
function readLauncherRoots(config: ProjectLauncherConfig): string[] {
  const filename = launcherPath(config);
  if (!fs.existsSync(filename)) return [];
  const stat = fs.statSync(filename);
  if (!stat.isFile() || stat.size > 1024 * 1024) throw new Error('LAUNCHER_METADATA_INVALID');
  const value = JSON.parse(fs.readFileSync(filename, 'utf8'));
  if (!value || !Array.isArray(value.recentProjectRoots) || value.recentProjectRoots.some((root: unknown) => typeof root !== 'string' || !path.isAbsolute(root))) throw new Error('LAUNCHER_METADATA_INVALID');
  return value.recentProjectRoots;
}
async function wikiMetadata(config: ProjectLauncherConfig, cwd: string, operation: 'roots' | 'register', project?: string): Promise<any> {
  if (!config.memoryRoot) return { status: 'missing', roots: [] };
  const args = ['-m', 'agent_core.pi_launch_metadata', operation, '--memory-root', path.resolve(cwd, config.memoryRoot)];
  if (config.wikiRoot) args.push('--wiki-root', path.resolve(cwd, config.wikiRoot));
  if (project) args.push('--project', project);
  const result = await runFile(config.launcherPython ?? process.env.BIOMIND_PI_LAUNCHER_PYTHON ?? 'python3', args,
    { cwd: config.installRoot ?? installation, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, timeout: 15000, maxBuffer: 1024 * 1024 });
  return JSON.parse(result.stdout);
}
export async function listProjects(config: ProjectLauncherConfig, cwd: string, signal?: AbortSignal): Promise<ProjectList> {
  const projects = new Map<string, ProjectEntry>(); const incomplete: string[] = [];
  const add = (root: string, source: string, projectId?: string) => {
    if (typeof root !== 'string' || !path.isAbsolute(root)) { incomplete.push(`${source}: invalid directory metadata`); return; }
    const key = canonicalProjectRoot(root);
    const entry = projects.get(key) ?? { root, sources: [], availability: availability(root), projectIds: [] };
    if (!entry.sources.includes(source)) entry.sources.push(source);
    if (projectId && !entry.projectIds.includes(projectId)) entry.projectIds.push(projectId);
    projects.set(key, entry);
  };
  add(path.resolve(cwd), 'current');
  try {
    const registered = await wikiMetadata(config, cwd, 'roots');
    if (registered.status === 'unavailable') incomplete.push('memory_project_roots: unavailable');
    for (const entry of registered.roots ?? []) add(entry.root, 'memory_project_roots', entry.project_id);
  } catch { incomplete.push('memory_project_roots: unavailable'); }
  try { for (const root of readLauncherRoots(config)) add(root, 'launcher'); }
  catch { incomplete.push('launcher: unavailable'); }
  // listAll(customDir) is the installed public API. In the native default layout,
  // enumerate only immediate session directories under this configured agentDir.
  // Pi reads its own JSONL metadata; no transcript is returned to the menu/model.
  const nativeRoot = path.join(path.resolve(config.agentDir ?? getAgentDir()), 'sessions');
  const directories: string[] = [];
  try {
    if (config.sessionDir) directories.push(path.resolve(cwd, config.sessionDir));
    else if (fs.existsSync(nativeRoot)) {
      fs.accessSync(nativeRoot, fs.constants.R_OK | fs.constants.X_OK);
      directories.push(...fs.readdirSync(nativeRoot, { withFileTypes: true }).filter(entry => entry.isDirectory() || entry.isSymbolicLink()).map(entry => path.join(nativeRoot, entry.name)));
    }
  } catch { incomplete.push('Pi sessions: unavailable'); }
  for (const directory of directories) {
    signal?.throwIfAborted();
    try {
      if (!fs.existsSync(directory)) continue;
      fs.accessSync(directory, fs.constants.R_OK | fs.constants.X_OK);
      for (const session of await SessionManager.listAll(directory, undefined, signal)) add(session.cwd, 'pi_session');
    } catch { incomplete.push(`Pi sessions: unavailable (${directory})`); }
  }
  return { projects: [...projects.values()].sort((a, b) => a.root.localeCompare(b.root)), incomplete };
}
export interface ProjectCreateReceipt {
  root: string; directoryCreated: boolean; instructionsCreated: boolean;
  registration: 'existing' | 'memory_project_roots' | 'launcher' | 'failed'; error?: string;
}
const projectInstructions = '# 项目说明\n\n物种：待确认\n样本：待确认\n参考版本：待确认\n输入路径：待确认\n研究目标：待确认\n\n分析前按当前任务核对适用信息；本文件不包含已验证科研结论。\n';
export async function createProject(config: ProjectLauncherConfig, cwd: string, target: string): Promise<ProjectCreateReceipt> {
  const root = path.resolve(cwd, target);
  const receipt: ProjectCreateReceipt = { root, directoryCreated: false, instructionsCreated: false, registration: 'failed' };
  try {
    const existing = fs.lstatSync(root, { throwIfNoEntry: false });
    if (existing && !fs.statSync(root).isDirectory()) throw new Error('PROJECT_NOT_DIRECTORY');
    if (!existing) { fs.mkdirSync(root, { recursive: true }); receipt.directoryCreated = true; }
    const instructions = path.join(root, 'AGENTS.md');
    if (!fs.lstatSync(instructions, { throwIfNoEntry: false })) {
      fs.writeFileSync(instructions, projectInstructions, { flag: 'wx' }); receipt.instructionsCreated = true;
    }
    const known = await listProjects(config, cwd);
    const match = known.projects.find(entry => canonicalProjectRoot(entry.root) === canonicalProjectRoot(root));
    if (match?.sources.some(source => source !== 'current')) { receipt.registration = 'existing'; return receipt; }
    const registered = await wikiMetadata(config, cwd, 'register', root);
    if (['registered', 'existing'].includes(registered.status)) { receipt.registration = 'memory_project_roots'; return receipt; }
    if (registered.status !== 'missing') throw new Error('PROJECT_REGISTRATION_UNAVAILABLE');
    // Only a previously unlisted empty project without a usable Wiki registry
    // needs this minimal path-only launcher metadata. Existing sources stay authoritative.
    const filename = launcherPath(config); const roots = readLauncherRoots(config);
    if (!roots.some(entry => canonicalProjectRoot(entry) === canonicalProjectRoot(root))) roots.push(root);
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    const temporary = `${filename}.${process.pid}.tmp`;
    try {
      fs.writeFileSync(temporary, JSON.stringify({ recentProjectRoots: roots }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, filename);
    } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
    receipt.registration = 'launcher';
  } catch { receipt.error = 'PROJECT_CREATE_OR_REGISTRATION_FAILED'; }
  return receipt;
}
function shellQuote(value: string) { return `'${value.replaceAll("'", "'\"'\"'")}'`; }
export function restartProjectCommand(root: string, options: { configPath?: string; sessionDir?: string; session?: string; node?: string; modelRecord?: string } = {}): string {
  const args = ['biomind', 'project', 'open', root];
  for (const [flag, value] of [['--pi-config', options.configPath], ['--session-dir', options.sessionDir], ['--session', options.session], ['--pi-node', options.node], ['--pi-model-record', options.modelRecord]]) if (value) args.push(flag!, value);
  return args.map(shellQuote).join(' ');
}
async function menuPrompt(title: string, options?: string[]): Promise<string | undefined> {
  // Resolve the installed dependency through Pi, using package public exports;
  // no dependency install, copied picker, private source import or session.
  const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
  const { TuiMainScreen, ProcessTerminal } = await import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href);
  initTheme('dark');
  const terminal = new ProcessTerminal(); const tui = new TuiMainScreen(terminal);
  return await new Promise(resolve => {
    let done = false;
    const finish = (value?: string) => { if (done) return; done = true; component.dispose(); tui.stop(); resolve(value); };
    const component = options ? new ExtensionSelectorComponent(title, options, finish, () => finish(), { tui })
      : new ExtensionInputComponent(title, '目录路径', finish, () => finish(), { tui });
    tui.addChild(component); tui.setFocus(component); tui.start();
  });
}
/** Select before opening a session so the host can bind its project and execution profile. */
export async function runStartupSessionMenu(config: ProjectLauncherConfig, cwd: string,
  options: { resume?: boolean; allowAllProjects?: boolean } = {}): Promise<{ session?: string; continueSession?: boolean; exitCode?: number }> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('SESSION_MENU_REQUIRES_TERMINAL');
  if (options.resume) {
    const session = await selectHistorySession(cwd, config.sessionDir, { allowAllProjects: options.allowAllProjects ?? false });
    return session ? { session } : { exitCode: 0 };
  }
  const sessions = await listProjectSessions(cwd, config.sessionDir);
  // A fresh project can start immediately. Do not create an empty saved session just to show a menu.
  if (!sessions.length) return {};
  for (;;) {
    const action = await menuPrompt(`项目对话：${cwd}（${sessions.length} 个历史会话）`,
      ['新建对话', '继续最近对话', '选择历史对话…', '取消']);
    if (!action || action === '取消') return { exitCode: 0 };
    if (action === '新建对话') return {};
    if (action === '继续最近对话') return { continueSession: true };
    const session = await selectHistorySession(cwd, config.sessionDir, { allowAllProjects: false });
    if (session) return { session };
    // Escape from history returns to the actions, making a new conversation easy to choose.
  }
}
export async function runProjectCommand(config: ProjectLauncherConfig, cwd: string, command: string, target?: string): Promise<{ exitCode?: number; cwd?: string }> {
  if (command === 'list') {
    process.stdout.write(JSON.stringify({ type: 'project_list', ...(await listProjects(config, cwd)) }) + '\n'); return { exitCode: 0 };
  }
  if (command === 'create') {
    if (!target) throw new Error('PROJECT_DIRECTORY_REQUIRED');
    const receipt = await createProject(config, cwd, target);
    process.stdout.write(JSON.stringify({ type: 'project_created', ...receipt }) + '\n'); return { exitCode: receipt.error ? 2 : 0 };
  }
  if (command !== 'menu') throw new Error('PROJECT_COMMAND_INVALID');
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('PROJECT_MENU_REQUIRES_TERMINAL_USE_PROJECT_LIST_OR_OPEN');
  const list = await listProjects(config, cwd);
  const labels = list.projects.map(entry => `${entry.root} [${entry.sources.join(', ')}; ${entry.availability}]`);
  const selected = await menuPrompt(`项目${list.incomplete.length ? '（部分来源不可读）' : ''}`, [...labels, '创建项目…', '取消']);
  if (!selected || selected === '取消') return { exitCode: 0 };
  if (selected === '创建项目…') {
    const directory = await menuPrompt('创建项目：输入目录（Esc 取消）');
    if (!directory?.trim()) return { exitCode: 0 };
    if (await menuPrompt(`创建 ${path.resolve(cwd, directory)} 与缺失的 AGENTS.md？`, ['取消', '确认创建']) !== '确认创建') return { exitCode: 0 };
    return runProjectCommand(config, cwd, 'create', directory);
  }
  const entry = list.projects[labels.indexOf(selected)];
  if (!entry || entry.availability !== 'available') throw new Error('PROJECT_DIRECTORY_UNAVAILABLE');
  return { cwd: canonicalProjectRoot(entry.root) };
}
export async function maybeSuggestProjectRoot(config: ProjectLauncherConfig, cwd: string): Promise<string | undefined> {
  const canonical = canonicalProjectRoot(cwd);
  const list = await listProjects(config, cwd);
  const candidates = list.projects.filter(entry => entry.availability === 'available' && entry.sources.some(source => source !== 'current') && canonical.startsWith(canonicalProjectRoot(entry.root) + path.sep));
  if (!candidates.length) return cwd;
  const choices = [`保留启动目录：${cwd}`, ...candidates.map(entry => `使用已登记项目根：${entry.root}`)];
  const selected = await menuPrompt('当前目录位于已知项目内，请明确项目根', choices);
  if (!selected) return undefined;
  const index = choices.indexOf(selected); return index > 0 ? canonicalProjectRoot(candidates[index - 1].root) : cwd;
}
