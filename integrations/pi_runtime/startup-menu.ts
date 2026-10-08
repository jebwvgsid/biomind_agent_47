/** Project selection before Pi opens a conversation or initializes its runtime. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { canonicalProjectRoot, createProject, listProjects, menuPrompt, runStartupSessionMenu,
  validateProjectRoot, ProjectDirectoryError, type ProjectLauncherConfig } from './project-launcher.js';
import { selectHistorySession } from './session-launcher.js';
import { readTargetSessionRoot } from './native-session.js';

export interface StartupMenuSelection {
  cwd?: string;
  session?: string;
  continueSession?: boolean;
  exitCode?: number;
}

function directoryPath(cwd: string, value: string): string {
  const input = value.trim();
  return path.resolve(cwd, input === '~' ? os.homedir() : input.startsWith('~/') ? path.join(os.homedir(), input.slice(2)) : input);
}
function usableDirectory(root: string): boolean {
  try {
    if (!fs.statSync(root).isDirectory()) return false;
    fs.accessSync(root, fs.constants.R_OK | fs.constants.X_OK);
    return true;
  } catch { return false; }
}

/** The host resolves settings afresh for every chosen root; the launch cwd is only a choice. */
export async function runStartupProjectMenu(config: ProjectLauncherConfig, launchCwd: string,
  configForProject: (cwd: string) => ProjectLauncherConfig, options: { selectSession?: boolean } = {}): Promise<StartupMenuSelection> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('PROJECT_MENU_REQUIRES_TERMINAL');
  let notice = '';
  // Cache metadata while navigating back. Only creating a project changes this list.
  let listing = await listProjects(config, launchCwd);
  for (;;) {
    const labels = listing.projects.map(entry => `${entry.root}${canonicalProjectRoot(entry.root) === canonicalProjectRoot(launchCwd) ? '（启动目录）' : ''}${entry.availability !== 'available' ? '（不可用）' : ''}`);
    const selected = await menuPrompt(`选择项目${listing.incomplete.length ? '（部分来源不可读）' : ''}${notice ? `\n${notice}` : ''}`,
      [...labels, '打开其他目录…', '创建项目…', ...(options.selectSession === false ? [] : ['浏览全部历史对话…']), '取消']);
    notice = '';
    if (!selected || selected === '取消') return { exitCode: 0 };
    if (selected === '浏览全部历史对话…') {
      const session = await selectHistorySession(launchCwd, config.sessionDir,
        { allowAllProjects: true, initialScope: 'all' });
      if (!session) continue;
      let cwd = readTargetSessionRoot(session);
      if (!usableDirectory(cwd)) { notice = `历史对话的项目目录不可用：${cwd}`; continue; }
      try { cwd = await validateProjectRoot(config, cwd); }
      catch (error) { if (!(error instanceof ProjectDirectoryError)) throw error; notice = error.message; continue; }
      return { cwd, session };
    }
    let cwd: string;
    if (selected === '打开其他目录…' || selected === '创建项目…') {
      const create = selected === '创建项目…';
      const input = await menuPrompt(`${create ? '创建项目' : '打开其他目录'}：输入目录路径（相对启动目录；Esc 返回）`);
      if (!input?.trim()) continue;
      cwd = directoryPath(launchCwd, input);
      if (create) {
        const confirmed = await menuPrompt(`创建 ${cwd} 与缺失的 AGENTS.md？`, ['返回', '确认创建']);
        if (confirmed !== '确认创建') continue;
        const receipt = await createProject(config, launchCwd, cwd);
        if (receipt.error) { notice = `项目创建或登记失败，请检查目录权限：${cwd}`; continue; }
        listing = await listProjects(config, launchCwd);
      }
    } else {
      const entry = listing.projects[labels.indexOf(selected)];
      if (!entry) continue;
      cwd = entry.root;
    }
    if (!usableDirectory(cwd)) { notice = `目录不可用，请重新选择：${cwd}`; continue; }
    try { cwd = await validateProjectRoot(config, cwd); }
    catch (error) { if (!(error instanceof ProjectDirectoryError)) throw error; notice = error.message; continue; }
    if (options.selectSession === false) return { cwd };
    const projectConfig = configForProject(cwd);
    const previousAgent = process.env.PI_CODING_AGENT_DIR;
    let choice: Awaited<ReturnType<typeof runStartupSessionMenu>>;
    try {
      // Pi's default session directory uses its agent environment. Fixture
      // projects may have separate agents; the host will bind the final one.
      if (projectConfig.agentDir) process.env.PI_CODING_AGENT_DIR = path.resolve(projectConfig.agentDir);
      choice = await runStartupSessionMenu(projectConfig, cwd, { allowBack: true, showEmpty: true });
    } finally {
      if (previousAgent === undefined) delete process.env.PI_CODING_AGENT_DIR;
      else process.env.PI_CODING_AGENT_DIR = previousAgent;
    }
    if (choice.back) continue;
    if (choice.exitCode !== undefined) return { exitCode: choice.exitCode };
    return { cwd, session: choice.session, continueSession: choice.continueSession };
  }
}
