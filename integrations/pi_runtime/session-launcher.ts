/** Browse native Pi sessions before initializing the BioMind runtime. */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { stripVTControlCharacters } from 'node:util';
import { getAgentDir, initTheme, keyHint, SessionManager, SessionSelectorComponent, type SessionInfo } from '@earendil-works/pi-coding-agent';

type Progress = Parameters<typeof SessionManager.list>[2];
export interface HistorySessionOptions {
  allowAllProjects?: boolean;
  signal?: AbortSignal;
}
export interface HistorySelectorOptions extends HistorySessionOptions {
  onSelect?: (sessionPath: string) => void;
  onCancel?: () => void;
  requestRender?: () => void;
}

function sessionDirectory(cwd: string, sessionDir?: string): string | undefined {
  return sessionDir === undefined ? undefined : path.resolve(cwd, sessionDir);
}

/** Pi owns filtering, parsing and sorting, including shared custom directories. */
export async function listProjectSessions(cwd: string, sessionDir?: string, signal?: AbortSignal): Promise<SessionInfo[]> {
  signal?.throwIfAborted();
  return SessionManager.list(path.resolve(cwd), sessionDirectory(cwd, sessionDir), undefined, signal);
}

async function loadTui() {
  // Resolve Pi's installed TUI dependency through its public package export.
  const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
  return import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href);
}

/** The returned component is independently usable in a TUI or a synthetic test. */
export async function createHistorySessionSelector(cwd: string, sessionDir?: string, options: HistorySelectorOptions = {}) {
  const { KeybindingsManager, TUI_KEYBINDINGS, getKeybindings, setKeybindings } = await loadTui();
  initTheme('dark');
  const existingBindings = getKeybindings();
  // Pi exports its app KeybindingsManager as a type only. Its public TUI manager
  // supplies the small set of documented app actions used by this component.
  let userBindings = {};
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(getAgentDir(), 'keybindings.json'), 'utf8'));
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) userBindings = parsed;
  } catch { /* Missing/invalid preferences use the native shortcuts. */ }
  const bindings = new KeybindingsManager({
    ...TUI_KEYBINDINGS,
    'app.session.toggleSort': { defaultKeys: 'ctrl+s' },
    'app.session.toggleNamedFilter': { defaultKeys: 'ctrl+n' },
    'app.session.togglePath': { defaultKeys: 'ctrl+p' },
    'app.session.delete': { defaultKeys: 'ctrl+d' },
    'app.session.deleteNoninvasive': { defaultKeys: 'ctrl+backspace' },
    'app.session.rename': { defaultKeys: 'ctrl+r' },
  }, { ...existingBindings.getUserBindings(), ...userBindings });
  const withBindings = <T>(operation: () => T): T => {
    const previous = getKeybindings();
    setKeybindings(bindings);
    try { return operation(); } finally { setKeybindings(previous); }
  };
  const controller = new AbortController();
  const directory = sessionDirectory(cwd, sessionDir);
  const allowAllProjects = options.allowAllProjects ?? true;
  const signalFor = (nativeSignal?: AbortSignal) => AbortSignal.any([
    controller.signal, ...(nativeSignal ? [nativeSignal] : []), ...(options.signal ? [options.signal] : []),
  ]);
  const current = (progress?: Progress, signal?: AbortSignal) =>
    SessionManager.list(path.resolve(cwd), directory, progress, signalFor(signal));
  const all = (progress?: Progress, signal?: AbortSignal) => directory === undefined
    ? SessionManager.listAll(progress, signalFor(signal))
    : SessionManager.listAll(directory, progress, signalFor(signal));
  let done = false;
  const removeAbortListener = () => options.signal?.removeEventListener('abort', cancel);
  const complete = (selected?: string) => {
    if (done) return;
    done = true;
    removeAbortListener();
    controller.abort();
    if (selected) options.onSelect?.(path.resolve(selected));
    else options.onCancel?.();
  };
  const component = new SessionSelectorComponent(current, allowAllProjects ? all : current,
    selected => complete(selected), () => complete(), () => complete(),
    () => { if (!done) options.requestRender?.(); }, { showRenameHint: false });
  const list = component.getSessionList();
  // Native defaults include deletion. Remove the write callback as well as its
  // keyboard entry points; never open a SessionManager while merely browsing.
  list.onDeleteSession = undefined;
  list.onRenameSession = undefined;
  if (!allowAllProjects) list.onToggleScope = undefined;
  const nativeInput = component.handleInput.bind(component);
  component.handleInput = data => {
    if (done) return;
    withBindings(() => {
      if (['app.session.delete', 'app.session.deleteNoninvasive', 'app.session.rename'].some(action => bindings.matches(data, action))) return;
      if (!allowAllProjects && bindings.matches(data, 'tui.input.tab')) return;
      nativeInput(data);
    });
  };
  const nativeRender = component.render.bind(component);
  component.render = width => withBindings(() => {
    // Remove the exact public hint emitted by Pi, without touching its private
    // header or duplicating its search, sorting and multi-line list renderer.
    const deleteHint = keyHint('app.session.delete', 'delete');
    const scopeHint = keyHint('tui.input.tab', 'scope');
    return nativeRender(width).map(line => {
      if (line.includes(deleteHint)) {
        line = stripVTControlCharacters(line).split(' · ').filter(part => part !== stripVTControlCharacters(deleteHint)).join(' · ');
      }
      if (!allowAllProjects) {
        if (line.includes(scopeHint)) line = stripVTControlCharacters(line).replace(`${stripVTControlCharacters(scopeHint)} · `, '');
        if (stripVTControlCharacters(line).includes('○ All')) line = stripVTControlCharacters(line).replace(' | ○ All', '');
        line = line.replace(' Press Tab to view all.', '').replace(', or Tab to view all.', '.');
      }
      return line;
    });
  });
  const cancel = () => list.onCancel?.(); // Native callback aborts both loaders.
  options.signal?.addEventListener('abort', cancel, { once: true });
  if (options.signal?.aborted) cancel();
  return {
    component,
    dispose() {
      if (done) return;
      done = true;
      removeAbortListener();
      cancel();
      controller.abort();
    },
  };
}

/** Resolve a selection only; host applies project/mode checks before opening it. */
export async function selectHistorySession(cwd: string, sessionDir?: string, options: HistorySessionOptions = {}): Promise<string | undefined> {
  if (options.signal?.aborted) return undefined;
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('SESSION_MENU_REQUIRES_TERMINAL_USE_SESSION_OR_CONTINUE');
  const { TuiMainScreen, ProcessTerminal, Text } = await loadTui();
  const tui = new TuiMainScreen(new ProcessTerminal());
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  let started = false;
  let stopped = false;
  let resolveSelection!: (selected?: string) => void;
  const selected = new Promise<string | undefined>(resolve => { resolveSelection = resolve; });
  const finish = (sessionPath?: string) => {
    if (stopped) return;
    stopped = true;
    picker?.dispose();
    if (started) tui.stop();
    resolveSelection(sessionPath);
  };
  try {
    picker = await createHistorySessionSelector(cwd, sessionDir, {
      ...options, onSelect: finish, onCancel: () => finish(), requestRender: () => { if (!stopped) tui.requestRender(); },
    });
    if (!stopped) {
      tui.addChild(new Text(`历史对话：${path.resolve(cwd)}${options.allowAllProjects === false ? '（仅当前项目）' : ''}\n输入关键词搜索，Enter 继续，Esc 取消（只读选择）`, 1, 0));
      tui.addChild(picker.component);
      tui.setFocus(picker.component);
      started = true;
      tui.start();
    }
    return await selected;
  } finally {
    picker?.dispose();
    if (started && !stopped) tui.stop();
  }
}
