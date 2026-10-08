import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { installBioMindProductUI, type BioMindProductState, type BioMindProductDetails } from '../product-ui.js';

// Reuse the installed public terminal-width implementation; do not approximate CJK/emoji columns.
const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
const { visibleWidth, stripTerminalSequences } = await import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href);
interface Component { render(width: number): string[]; invalidate(): void; dispose?(): void }
function fixture(commandsEnabled = true) {
  const handlers = new Map<string, (event: any, ctx: ExtensionContext) => void | Promise<void>>();
  const commands = new Map<string, { handler(args: string, ctx: any): Promise<void> }>();
  const notices: string[] = [], writes: string[] = [];
  const created: Component[] = [];
  let header!: Component, footer: Component | undefined, widget!: Component;
  let state: BioMindProductState | undefined;
  let detail: BioMindProductDetails = { instructions: '全局偏好已加载 · 项目 AGENTS.md 已加载',
    memory: '项目按需检索开启 · 自动整理关闭', authentication: '已配置 · 尚未验证连通性' };
  let failDetails = false, idle = true, editor = '', expanded = false, shutdowns = 0;
  let leaf = 0, projections = 0, branchReads = 0, titleReads = 0, redraws = 0, session = 'synthetic-session';
  let branch: any[] = [], sessionName: string | undefined;
  const tui = { terminal: { rows: 35, columns: 120 }, requestRender() { redraws++; } };
  // Theme output is trusted styling and must survive field sanitization.
  const theme = { bold: (text: string) => `\x1b[1m${text}\x1b[22m`,
    fg: (_color: string, text: string) => `\x1b[2m${text}\x1b[22m` };
  type Factory = (screen: typeof tui, colors: typeof theme) => Component;
  const footerData = { getGitBranch: () => null, getExtensionStatuses: () => new Map<string, string>(),
    getAvailableProviderCount: () => 1, onBranchChange: (_callback: () => void) => () => {} };
  const ui = {
    setHeader(factory: Factory) { header = factory(tui, theme); created.push(header); writes.push('header'); },
    setFooter(factory: ((screen: typeof tui, colors: typeof theme, data: typeof footerData) => Component) | undefined) {
      footer?.dispose?.(); footer = factory?.(tui, theme, footerData); if (footer) created.push(footer); writes.push('footer');
    },
    setWidget(key: string, factory: Factory, options: unknown) {
      assert.equal(key, 'biomind-input'); assert.deepEqual(options, { placement: 'aboveEditor' });
      widget?.dispose?.();
      widget = factory(tui, theme); created.push(widget); writes.push('widget');
    },
    setTitle(title: string) { writes.push('title:' + title); },
    getEditorText(): never { throw new Error('render must not expand the draft'); },
    getToolsExpanded() { return expanded; },
    setToolsExpanded(value: boolean) { expanded = value; },
    notify(text: string) { notices.push(text); },
  };
  const ctx = { mode: 'tui', hasUI: true, cwd: '/synthetic/科研工作区',
    model: { provider: 'fixture', id: 'scripted' }, thinkingLevel: 'off',
    ui, isIdle: () => idle, isProjectTrusted: () => false, shutdown() { shutdowns++; },
    getContextUsage: () => ({ tokens: 38123, contextWindow: 312000, percent: 12.2 }),
    // Only synchronous metadata is available; the UI cannot resolve credentials.
    modelRegistry: { isUsingOAuth: () => false, getProvider: () => undefined,
      getApiKey(): never { throw new Error('credential resolution forbidden'); } },
    sessionManager: { getSessionId: () => session, getSessionName: () => { titleReads++; return sessionName; }, getLeafId: () => String(leaf),
      getEntries: () => branch, getCwd: () => '/synthetic/科研工作区',
      getBranch: () => { branchReads++; return branch; } },
  } as unknown as ExtensionContext;
  const pi = new Proxy({
    on(name: string, handler: (event: any, context: ExtensionContext) => void | Promise<void>) {
      assert(!handlers.has(name)); handlers.set(name, handler); return () => handlers.delete(name);
    },
    registerCommand(name: string, command: { handler(args: string, context: any): Promise<void> }) {
      assert(!commands.has(name)); commands.set(name, command);
    },
  }, { get(target, key) {
    if (key in target) return target[key as keyof typeof target];
    throw new Error(`non-UI API access forbidden: ${String(key)}`);
  } }) as unknown as ExtensionAPI;
  installBioMindProductUI(pi, () => { projections++; return state; }, { commands: commandsEnabled,
    details: () => { if (failDetails) throw new Error('synthetic read failure'); return detail; } });
  return { handlers, commands, notices, created, writes, ctx,
    start: async (reason = 'startup') => { await handlers.get('session_start')!({ type: 'session_start', reason }, ctx); },
    message: async (role = 'user', content: unknown = []) => { await handlers.get('message_start')!({ type: 'message_start', message: { role, content } }, ctx); },
    header: () => header, footer: () => footer, widget: () => widget,
    setState: (value: BioMindProductState | undefined) => { state = value; leaf++; },
    setBranch: (value: any[]) => { branch = value; leaf++; }, setSession: (value: string) => { session = value; },
    setName: (value: string | undefined) => { sessionName = value; leaf++; },
    setIdle: (value: boolean) => { idle = value; }, setEditor: (value: string) => { editor = value; },
    setDetails: (value: BioMindProductDetails) => { detail = value; }, failDetails: () => { failDetails = true; },
    resize: (columns: number, rows: number) => { tui.terminal.columns = columns; tui.terminal.rows = rows; },
    editor: () => editor, expanded: () => expanded, shutdowns: () => shutdowns,
    projections: () => projections, branchReads: () => branchReads, titleReads: () => titleReads, redraws: () => redraws,
  };
}
const lines = (component: Component, width = 120): string[] => component.render(width).map((line: string) => stripTerminalSequences(line));
const contents = (component: Component, width = 120) => lines(component, width).join('\n');
const historicalMessage = { type: 'message', id: 'old-user', message: { role: 'user', content: [{ type: 'text', text: '历史问题' }] } };
const completeCommands = ['/login', '/model', '/project', '/instructions', '/biomind-memory status', '/new  /resume  /name'];

test('native footer survives welcome/quiet/new/resume; current session uses name, accepted question and stable short ID', async () => {
  const f = fixture(); f.setSession('12345678-90ab-cdef-1234-1234567890ab');
  await f.start();
  const sessionLine = () => lines(f.widget(), 42).find(row => row.startsWith('会话'))!;
  assert.match(sessionLine(), /新对话.*567890ab$/);
  f.setEditor('草稿不应成为会话标题');
  assert.doesNotMatch(sessionLine(), /草稿/);
  await f.message('assistant', [{ type: 'text', text: '模型输出不能改标题' }]);
  assert.match(sessionLine(), /新对话/);
  await f.message('user', [{ type: 'image', data: 'not-a-title' }, { type: 'text', text: '分析项目中的表达差异' }]);
  assert.match(sessionLine(), /分析项目.*567890ab$/);
  await f.message('user', '第二个问题不替换首条问题');
  assert.doesNotMatch(sessionLine(), /第二个/);
  f.setName('手动命名 🧬 中文会话 '.repeat(20) + '\x1b]52;c;SECRET\x07');
  assert.match(sessionLine(), /手动命名/);
  assert.match(sessionLine(), /567890ab$/);
  assert(visibleWidth(sessionLine()) <= 42);
  assert.doesNotMatch(sessionLine(), /SECRET|\x1b/);
  f.setBranch([historicalMessage]); await f.start('reload');
  assert.match(sessionLine(), /手动命名/);
  f.setName(undefined); await f.start('resume');
  assert.match(sessionLine(), /历史问题.*567890ab$/);
  f.setSession('12345678-90ab-cdef-1234-1234abcdef90'); f.setBranch([]); await f.start('new');
  assert.match(sessionLine(), /新对话.*abcdef90$/);
  assert.match(contents(f.footer()!), /上下文 38\.1k\/312k/);
  // The extension mock rejects all persistence/model APIs; this flow is display-only.
});

test('normal 120×35 and 100×30 welcome retains independent title, sections and display-column alignment', async () => {
  const f = fixture(); await f.start();
  for (const [width, height] of [[120, 35], [100, 30]]) {
    f.resize(width, height);
    const rows = lines(f.widget(), width), text = rows.join('\n');
    assert.equal(rows[0], 'BioMind · 科研分析助手');
    assert.equal(rows[1], '', 'brand and project retain a separating blank line');
    assert.equal(rows[5], '', 'project and runtime configuration retain a blank line');
    assert.equal(rows[10], '', 'configuration and common commands retain a blank line');
    assert.equal(rows[18], '', 'commands and input guide retain a blank line');
    assert.deepEqual(rows.slice(-2), ['输入 / 搜索全部命令', '直接输入问题即可开始']);
    for (const label of ['项目', '会话', '目录', '模型', '认证', '说明', '记忆']) {
      const row = rows.find(value => value.startsWith(label))!;
      assert(row, `${label} is present on the welcome itself`);
      const prefix = row.match(/^\S+ +/)![0];
      assert.equal(visibleWidth(prefix), 10, `${label} has a common visual value column`);
    }
    for (const command of completeCommands) {
      const row = rows.find(value => value.startsWith(command))!;
      assert(row, command);
      const description = row.slice(command.length).replace(/^ +/, '');
      assert.equal(visibleWidth(row.slice(0, row.length - description.length)), 26, command);
    }
    assert.match(text, /全局偏好已加载.*AGENTS\.md 已加载/);
    assert.match(text, /项目按需检索开启.*自动整理关闭/);
    assert.match(text, /已配置.*尚未验证连通性/);
    assert(rows.length <= height - 8, 'reserve real editor and native spacers');
    assert(rows.every(row => visibleWidth(row) <= width));
    assert.equal(contents(f.header()), ''); assert.match(contents(f.footer()!), /上下文 38\.1k\/312k/);
  }
  assert.match(f.widget().render(120)[0], /\x1b\[1m/, 'trusted theme styling is retained');
});

test('welcome follows actual conversation acceptance, never BioMind metadata, drafts or menu notifications', async () => {
  const f = fixture();
  f.setBranch([{ type: 'model_change', id: 'model' }, { type: 'custom', customType: 'biomind.request', data: { status: 'settled' } }]);
  f.setState({ role: 'Consult', status: 'settled' }); await f.start();
  f.setEditor('第一行中文草稿\n第二行 🧬');
  await f.commands.get('help')!.handler('', f.ctx); await f.message('assistant');
  assert.match(contents(f.widget()), /^BioMind · 科研分析助手/);
  assert.equal(f.editor(), '第一行中文草稿\n第二行 🧬');
  assert.equal(f.redraws(), 0, 'non-user events do not consume the welcome');
  await f.message(); f.setIdle(false); f.setState({ role: 'Consult', status: 'active' });
  assert.equal(f.redraws(), 1);
  assert(lines(f.widget()).length <= 4);
  assert.match(contents(f.widget()), /处理中/);
  assert.match(contents(f.widget()), /草稿区 · 尚未提交/);
  assert.doesNotMatch(contents(f.widget()), /常用操作|直接输入问题即可开始/);
  assert.equal(f.editor(), '第一行中文草稿\n第二行 🧬');
});

test('compact reads owner state, current model and changed details; completion, cancellation and error stay compact', async () => {
  const f = fixture(); await f.start(); await f.message();
  const state: BioMindProductState = { role: 'Consult', status: 'active' };
  f.setState(state); f.setIdle(false);
  assert.match(contents(f.widget()), /处理中/);
  state.role = 'Generate'; f.setState(state); assert.match(contents(f.widget()), /处理中/);
  for (const [value, label] of [['settled', '本次请求已结束'], ['aborted', '已中断'], ['error', '错误']] as const) {
    state.status = value; f.setState(state); f.setIdle(true);
    assert.match(contents(f.widget()), new RegExp(label));
    assert(lines(f.widget()).length <= 4);
    assert.doesNotMatch(contents(f.widget()), /常用操作|科学成功|任务正式完成/);
  }
  (f.ctx as any).model = { provider: 'other', id: 'current-model' };
  f.setDetails({ instructions: '全局偏好待刷新 · 项目 override 已加载', memory: '项目按需检索关闭 · 自动整理需询问' });
  assert.match(contents(f.widget()), /other\/current-model/);
  assert.match(contents(f.widget()), /待刷新.*override/);
  assert.match(contents(f.widget()), /检索关闭.*需询问/);
  assert.doesNotMatch(contents(f.widget()), /fixture\/scripted|费用|成本/);
});

test('unchanged renders never rescan the branch or read draft; owner display caches by session and leaf', async () => {
  const f = fixture(); await f.start();
  const reads = f.branchReads();
  for (let i = 0; i < 20; i++) contents(f.widget());
  assert.equal(f.branchReads(), reads);
  await f.message(); f.setState({ role: 'Consult', status: 'active' }); contents(f.widget());
  const projections = f.projections();
  for (let i = 0; i < 20; i++) { contents(f.header()); contents(f.widget()); }
  assert.equal(f.projections(), projections); assert.equal(f.branchReads(), reads);
  f.setState({ role: 'Generate', status: 'active' }); contents(f.widget());
  assert.equal(f.projections(), projections + 1);
  contents(f.footer()!);
  const titleReads = f.titleReads();
  for (let i = 0; i < 20; i++) { contents(f.widget()); contents(f.footer()!); }
  assert.equal(f.titleReads(), titleReads, 'native title lookup scans history and must also be cached');
});

test('elapsed activity includes retry/continuations, freezes at settled and resets for the next run', async t => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  const f = fixture();
  t.after(() => { for (const component of f.created) component.dispose?.(); });
  await f.start(); await f.message();
  assert.doesNotMatch(contents(f.widget()), /本轮运行|本轮用时/);
  await f.handlers.get('agent_start')!({}, f.ctx);
  assert.match(contents(f.widget(), 42), /本轮运行 00:00/);
  now = 65000;
  await f.handlers.get('agent_start')!({}, f.ctx); // Automatic retry/continuation.
  assert.equal(f.handlers.has('agent_end'), false, 'a low-level loop end is not settlement');
  assert.match(contents(f.widget()), /本轮运行 01:05/);
  await f.message('user', 'Steering an already running activity does not reset it.');
  assert.match(contents(f.widget()), /本轮运行 01:05/);
  now = 3665000;
  await f.handlers.get('agent_settled')!({}, f.ctx);
  now += 120000;
  assert.match(contents(f.widget()), /本轮用时 1:01:05/);
  await f.handlers.get('agent_settled')!({}, f.ctx); // Duplicate delivery stays frozen.
  await f.commands.get('status')!.handler('', f.ctx);
  assert.match(f.notices.at(-1)!, /计时：本轮用时 1:01:05/);
  await f.handlers.get('agent_start')!({}, f.ctx);
  assert.match(contents(f.widget()), /本轮运行 00:00/);
  now += 2000; f.setState({ status: 'aborted' });
  await f.handlers.get('agent_settled')!({}, f.ctx);
  assert.match(contents(f.widget()), /本轮用时 00:02.*已中断/);
});

test('activity ticks redraw without scanning history and stop after settlement or widget disposal', async t => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  t.mock.timers.enable({ apis: ['setInterval'] });
  const f = fixture();
  t.after(() => { for (const component of f.created) component.dispose?.(); });
  await f.start(); await f.message();
  await f.handlers.get('agent_start')!({}, f.ctx);
  const redraws = f.redraws(), reads = f.branchReads();
  now = 2000; t.mock.timers.tick(2000);
  assert.equal(f.redraws(), redraws + 2);
  assert.equal(f.branchReads(), reads);
  assert.match(contents(f.widget()), /本轮运行 00:02/);
  await f.handlers.get('agent_settled')!({}, f.ctx);
  const frozen = f.redraws();
  now = 5000; t.mock.timers.tick(3000);
  assert.equal(f.redraws(), frozen);
  await f.handlers.get('agent_start')!({}, f.ctx);
  f.widget().dispose!();
  const disposed = f.redraws();
  now = 10000; t.mock.timers.tick(5000);
  assert.equal(f.redraws(), disposed);
  assert.deepEqual(f.widget().render(120), []);
});

test('session changes, reload, shutdown and nonterminal events do not inherit running clocks', async t => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  const f = fixture();
  t.after(() => { for (const component of f.created) component.dispose?.(); });
  await f.start(); await f.message();
  await f.handlers.get('agent_start')!({}, f.ctx);
  now = 11000;
  const previous = f.widget();
  f.setSession('new-session'); f.setBranch([historicalMessage]); await f.start('resume');
  assert.deepEqual(previous.render(120), []);
  assert.doesNotMatch(contents(f.widget()), /本轮运行|本轮用时/);
  await f.handlers.get('agent_start')!({}, f.ctx);
  now += 1000; await f.start('reload');
  assert.doesNotMatch(contents(f.widget()), /本轮运行|本轮用时/);
  (f.ctx as any).mode = 'json';
  await f.handlers.get('agent_start')!({}, f.ctx);
  assert.doesNotMatch(contents(f.widget()), /本轮运行|本轮用时/);
  (f.ctx as any).mode = 'tui';
  await f.handlers.get('agent_start')!({}, f.ctx);
  await f.handlers.get('session_shutdown')!({}, f.ctx);
  assert.doesNotMatch(contents(f.widget()), /本轮运行|本轮用时/);
});

test('new restores welcome; resume/reload and conversation compaction retain compact; disposed widget is inert', async () => {
  const f = fixture(); await f.start(); await f.message();
  const previous = f.widget(); previous.dispose!();
  f.setSession('new-empty'); f.setBranch([]); f.setState(undefined); await f.start('new');
  assert.deepEqual(previous.render(120), []);
  assert.match(contents(f.widget()), /^BioMind · 科研分析助手/);
  for (const reason of ['resume', 'reload']) {
    f.setBranch([historicalMessage]); await f.start(reason);
    assert(lines(f.widget()).length <= 4, `${reason} must respect real old messages without BioMind metadata`);
    assert.doesNotMatch(contents(f.widget()), /常用操作/);
  }
  f.setBranch([{ type: 'compaction', summary: 'native compacted history' }]); await f.start('reload');
  assert(lines(f.widget()).length <= 4);
  f.setBranch([{ type: 'custom_message', display: true, content: 'native visible history' }]);
  await f.handlers.get('session_tree')!({ type: 'session_tree' }, f.ctx);
  assert(lines(f.widget()).length <= 4);
  f.setBranch([{ type: 'custom_message', display: false, content: 'hidden metadata' }]); await f.start('new');
  assert.match(contents(f.widget()), /^BioMind · 科研分析助手/);
});

test('80×24 and 42×20 welcome reflow keeps required state and input space across resize', async () => {
  const f = fixture(); await f.start();
  (f.ctx as any).cwd = '/synthetic/' + '中文🧬e\u0301'.repeat(25);
  (f.ctx as any).model = { provider: 'fixture', id: '中文🧬e\u0301'.repeat(25) };
  for (const [width, height] of [[80, 24], [42, 20], [120, 35], [42, 20], [100, 30]]) {
    f.resize(width, height); const rows = lines(f.widget(), width), text = rows.join('\n');
    for (const label of ['项目', '会话', '模型', '说明', '记忆', '输入 / 搜索全部命令']) assert(text.includes(label), `${width}×${height}: ${label}`);
    assert(rows.length <= height - 8, `${width}×${height} leaves real editor space`);
    assert(rows.every(row => visibleWidth(row) <= width));
    assert(!text.includes('�'));
    if (width >= 80) assert(text.includes('/biomind-memory status'));
    if (width >= 100) for (const command of completeCommands) assert(text.includes(command));
  }
  for (const [width, height] of [[1, 5], [2, 6], [12, 8], [31, 11]]) {
    f.resize(width, height); const rows = lines(f.widget(), width);
    assert(rows.length <= Math.max(1, height - 8));
    assert(rows.every(row => visibleWidth(row) <= width));
  }
  assert.deepEqual(f.widget().render(0), []); assert.deepEqual(f.widget().render(Number.NaN), []);
});

test('external fields lose control sequences while trusted layout whitespace and theme survive', async () => {
  const f = fixture();
  (f.ctx as any).cwd = '/synthetic/\x1b]0;forged-title\x07中文🧬e\u0301\x1b[2J\x00\n' + '长'.repeat(20000);
  (f.ctx as any).model = { provider: 'fixture\x1b[31m', id: 'scripted\x1b]52;c;BAD\x07' };
  f.setDetails({ instructions: '说明\x1b[2J 已加载', memory: '未知\x00\r\n', authentication: '未核对\x1b]52;c;SECRET\x07' });
  await f.start();
  for (const width of [1, 2, 12, 42, 80, 120, 512]) {
    const raw = f.widget().render(width), rows = lines(f.widget(), width), text = rows.join('');
    assert(width < 32 || raw.some(row => row.includes('\x1b[1m')));
    assert(rows.every(row => visibleWidth(row) <= width));
    assert(Buffer.byteLength(raw.join('\n')) < 32768);
    assert(!/[\u0000-\u001f\u007f-\u009f]/u.test(text));
    assert.doesNotMatch(text, /forged-title|BAD|SECRET/);
    assert(!text.includes('�'));
  }
  assert(lines(f.widget()).includes(''));
  assert.match(contents(f.widget()), /项目 {6}/);
  for (const title of f.writes.filter(value => value.startsWith('title:'))) assert(!/[\u0000-\u001f\u007f-\u009f]/u.test(title));
});

test('missing model and failed or unknown details stay truthful and keep native command entrances visible', async () => {
  const f = fixture(); (f.ctx as any).model = { provider: 'unknown', id: 'unknown' };
  f.setDetails({ instructions: '全局偏好为空 · 项目 AGENTS.override.md 已加载', memory: '运行状态未知', authentication: '尚未配置 · /login' });
  await f.start(); let text = contents(f.widget());
  assert.match(text, /模型 +尚未选择/); assert.match(text, /认证 +尚未配置/);
  assert.match(text, /全局偏好为空.*AGENTS\.override\.md 已加载/);
  assert.match(text, /运行状态未知/); assert.match(text, /\/login/); assert.match(text, /\/model/);
  f.setDetails({ instructions: '全局偏好缺失 · 项目说明缺失', memory: '项目按需检索关闭', authentication: '未核对' });
  assert.match(contents(f.widget()), /偏好缺失.*说明缺失/);
  f.failDetails(); text = contents(f.widget());
  assert.match(text, /读取失败/); assert.match(text, /认证 +未核对/);
  assert.doesNotMatch(text, /已联网|已连接|已配置|已加载|检索开启/);
  assert.match(text, /直接输入问题即可开始/);
});

test('BioMind local commands retain permissions, details and shutdown without taking editor ownership', async () => {
  const f = fixture();
  assert.deepEqual([...f.commands.keys()], ['help', 'status', 'permissions', 'details', 'exit']);
  assert.deepEqual([...f.handlers.keys()], ['session_start', 'session_tree', 'message_start', 'agent_start', 'agent_settled', 'session_shutdown']);
  await f.commands.get('help')!.handler('', f.ctx);
  await f.commands.get('status')!.handler('', f.ctx);
  assert.match(f.notices[0], /BioMind 帮助/); assert.match(f.notices[1], /BioMind 状态：待输入/);
  await f.commands.get('permissions')!.handler('', f.ctx);
  assert.match(f.notices.at(-1)!, /权限操作失败/);
  assert.match(f.notices.at(-1)!, /权限账本查询未连接/);
  assert.doesNotMatch(f.notices.at(-1)!, /--project-dir/);
  await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), true);
  await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), false);
  await f.commands.get('details')!.handler('展开', f.ctx); assert.equal(f.expanded(), true);
  await f.commands.get('details')!.handler('unknown', f.ctx); assert.equal(f.expanded(), true);
  assert.match(f.notices.at(-1)!, /不构成批准/);
  await f.commands.get('exit')!.handler('', f.ctx); assert.equal(f.shutdowns(), 1);
  assert.equal(f.writes.length, 0, 'commands do not install or mutate editor/session components');
});

test('JSON and RPC do not install components or replace their input owners', async () => {
  const headless = fixture(false); assert.equal(headless.commands.size, 0);
  for (const [mode, hasUI] of [['json', false], ['rpc', true]] as const) {
    const f = fixture(); (f.ctx as any).mode = mode; (f.ctx as any).hasUI = hasUI;
    await f.start(); await f.handlers.get('session_tree')!({}, f.ctx); await f.message();
    assert.equal(f.writes.length, 0); assert.equal(f.branchReads(), 0);
    await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), false);
    await f.commands.get('exit')!.handler('', f.ctx); assert.equal(f.shutdowns(), 1);
  }
});

test('quietStartup=true hides the full welcome and retains model state and command input guidance', async () => {
  const prior = process.env.BIOMIND_PI_QUIET_STARTUP;
  try {
    process.env.BIOMIND_PI_QUIET_STARTUP = '1';
    const f = fixture(); (f.ctx as any).model = { provider: 'unknown', id: 'unknown' }; await f.start();
    assert.equal(contents(f.header()), ''); assert.match(contents(f.footer()!), /上下文 38\.1k\/312k/);
    assert(lines(f.widget()).length <= 4);
    assert.match(contents(f.widget()), /模型 尚未选择/);
    assert.match(contents(f.widget()), /输入 \/ 搜索命令/);
    assert.doesNotMatch(contents(f.widget()), /科研分析助手|常用操作/);
    assert(f.commands.has('help'));
  } finally { if (prior === undefined) delete process.env.BIOMIND_PI_QUIET_STARTUP; else process.env.BIOMIND_PI_QUIET_STARTUP = prior; }
});

test('native quietStartup respects actual trust, reports source and reads settings only per lifecycle', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-ui-settings-'));
  const agent = path.join(root, 'agent'), cwd = path.join(root, 'project');
  fs.mkdirSync(agent); fs.mkdirSync(path.join(cwd, '.pi'), { recursive: true });
  fs.writeFileSync(path.join(agent, 'settings.json'), JSON.stringify({ quietStartup: false }));
  fs.writeFileSync(path.join(cwd, '.pi/settings.json'), JSON.stringify({ quietStartup: true }));
  const oldDir = process.env.PI_CODING_AGENT_DIR, oldQuiet = process.env.BIOMIND_PI_QUIET_STARTUP;
  try {
    process.env.PI_CODING_AGENT_DIR = agent; process.env.BIOMIND_PI_QUIET_STARTUP = 'native';
    const f = fixture(); Object.assign(f.ctx, { cwd, isProjectTrusted: () => false }); await f.start();
    assert.match(contents(f.widget()), /^BioMind · 科研分析助手/);
    await f.commands.get('status')!.handler('', f.ctx);
    assert.match(f.notices.at(-1)!, /quietStartup=false · Pi 用户设置/);
    Object.assign(f.ctx, { isProjectTrusted: () => true }); await f.start();
    assert.doesNotMatch(contents(f.widget()), /常用操作/);
    await f.commands.get('status')!.handler('', f.ctx);
    assert.match(f.notices.at(-1)!, /quietStartup=true · Pi 受信项目设置/);
    fs.writeFileSync(path.join(cwd, '.pi/settings.json'), JSON.stringify({ quietStartup: false }));
    assert.doesNotMatch(contents(f.widget()), /常用操作/, 'redraw does not re-read settings');
    await f.start(); assert.match(contents(f.widget()), /^BioMind · 科研分析助手/);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(agent, 'settings.json'), 'utf8')), { quietStartup: false });
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(cwd, '.pi/settings.json'), 'utf8')), { quietStartup: false });
  } finally {
    if (oldDir === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = oldDir;
    if (oldQuiet === undefined) delete process.env.BIOMIND_PI_QUIET_STARTUP; else process.env.BIOMIND_PI_QUIET_STARTUP = oldQuiet;
  }
});

test('status renders relative native session paths against the current project', async () => {
  const f = fixture();
  Object.assign(f.ctx.sessionManager, { getSessionDir: () => 'relative sessions', getSessionFile: () => 'relative sessions/synthetic.jsonl' });
  await f.commands.get('status')!.handler('', f.ctx);
  assert(f.notices.at(-1)!.includes(path.join(f.ctx.cwd, 'relative sessions')));
  assert(f.notices.at(-1)!.includes(path.join(f.ctx.cwd, 'relative sessions/synthetic.jsonl')));
});
