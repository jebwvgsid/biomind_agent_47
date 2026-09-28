import test from 'node:test';
import assert from 'node:assert/strict';
import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { installBioMindProductUI, type BioMindProductState } from '../product-ui.js';

interface Component { render(width: number): string[]; invalidate(): void; dispose?(): void }
function fixture(commandsEnabled = true) {
  const handlers = new Map<string, (event: unknown, ctx: ExtensionContext) => void>();
  const commands = new Map<string, { handler(args: string, ctx: any): Promise<void> }>();
  const notices: string[] = [];
  const created: Component[] = [];
  let header!: Component, footer!: Component, widget!: Component;
  let state: BioMindProductState | undefined;
  let idle = true, editor = '', expanded = false, shutdowns = 0;
  let leaf = 0, projections = 0;
  const writes: string[] = [];
  const ui = {
    setHeader(factory: () => Component) { header = factory(); created.push(header); writes.push('header'); },
    setFooter(factory: () => Component) { footer = factory(); created.push(footer); writes.push('footer'); },
    setWidget(key: string, factory: () => Component, options: unknown) {
      assert.equal(key, 'biomind-input'); assert.deepEqual(options, { placement: 'aboveEditor' });
      widget = factory(); created.push(widget); writes.push('widget');
    },
    setTitle(title: string) { writes.push('title:' + title); },
    getEditorText(): never { throw new Error('render must not expand the draft'); },
    getToolsExpanded() { return expanded; },
    setToolsExpanded(value: boolean) { expanded = value; },
    notify(text: string) { notices.push(text); },
  };
  const ctx = { mode: 'tui', hasUI: true, cwd: '/synthetic/科研工作区',
    model: { provider: 'fixture', id: 'scripted' }, thinkingLevel: 'off',
    ui, isIdle: () => idle, shutdown() { shutdowns++; },
    // A UI command or render must never request work, credentials, or session persistence.
    get modelRegistry(): never { throw new Error('credential access forbidden'); },
    sessionManager: { getSessionId: () => 'synthetic-session', getLeafId: () => String(leaf) },
  } as unknown as ExtensionContext;
  const pi = new Proxy({
    on(name: string, handler: (event: unknown, context: ExtensionContext) => void) {
      assert(!handlers.has(name)); handlers.set(name, handler); return () => handlers.delete(name);
    },
    registerCommand(name: string, command: { handler(args: string, context: any): Promise<void> }) {
      assert(!commands.has(name)); commands.set(name, command);
    },
  }, { get(target, key) {
    if (key in target) return target[key as keyof typeof target];
    throw new Error(`non-UI API access forbidden: ${String(key)}`);
  } }) as unknown as ExtensionAPI;
  installBioMindProductUI(pi, () => { projections++; return state; }, { commands: commandsEnabled });
  return { handlers, commands, notices, created, writes, ctx,
    start: () => handlers.get('session_start')!({ type: 'session_start', reason: 'startup' }, ctx),
    header: () => header, footer: () => footer, widget: () => widget,
    setState: (value: BioMindProductState | undefined) => { state = value; leaf++; },
    setIdle: (value: boolean) => { idle = value; }, setEditor: (value: string) => { editor = value; },
    expanded: () => expanded, shutdowns: () => shutdowns,
    projections: () => projections,
  };
}
const contents = (component: Component, width = 120) => component.render(width).join('\n');

test('BioMind UI reads current role, status, model and input without copying domain state', () => {
  const f = fixture(); f.start();
  assert.match(contents(f.header()), /BioMind · \/help 帮助 · \/details 详情 · \/exit 退出/);
  assert.match(contents(f.footer()), /待输入/);
  assert.match(contents(f.footer()), /fixture\/scripted/);
  assert.match(contents(f.footer()), /科研工作区/);
  assert.match(contents(f.widget()), /你的输入/);
  const state: BioMindProductState = { role: 'Consult', status: 'active' };
  f.setState(state); f.setIdle(false); f.setEditor('下一条中文任务');
  assert.match(contents(f.footer()), /咨询中（Consult）/);
  assert.match(contents(f.widget()), /草稿区 · 尚未提交/);
  const count = f.projections();
  for (let i = 0; i < 20; i++) { contents(f.footer()); contents(f.widget()); }
  assert.equal(f.projections(), count, 'unchanged leaf must not rescan the branch on redraw');
  state.role = 'Generate'; f.setState(state); assert.match(contents(f.footer()), /执行中（Generate）/);
  for (const [value, label] of [['settled', '本次请求已结束'], ['aborted', '已中断'], ['error', '错误']] as const) {
    state.status = value; f.setState(state); assert.match(contents(f.footer()), new RegExp(label));
  }
  (f.ctx as any).model = { provider: 'other', id: 'current-model' };
  (f.ctx as any).thinkingLevel = 'high';
  assert.match(contents(f.footer()), /other\/current-model/);
  assert.match(contents(f.footer()), /思考 high/);
  assert(!contents(f.footer()).includes('scripted'));
  assert(!/费用|成本|科学成功|任务正式完成|Step|Commit|Plan/.test(contents(f.footer())));
});

test('BioMind local commands only notify, toggle details or request Pi shutdown', async () => {
  const f = fixture();
  assert.deepEqual([...f.commands.keys()], ['help', 'status', 'permissions', 'details', 'exit']);
  assert.deepEqual([...f.handlers.keys()], ['session_start', 'session_tree']);
  await f.commands.get('help')!.handler('', f.ctx);
  await f.commands.get('status')!.handler('', f.ctx);
  assert.match(f.notices[0], /BioMind 帮助/); assert.match(f.notices[1], /BioMind 状态：待输入/);
  await f.commands.get('permissions')!.handler('', f.ctx);
  assert.match(f.notices.at(-1)!, /科研工作区/);
  assert.match(f.notices.at(-1)!, /--project-dir/);
  assert.match(f.notices.at(-1)!, /只查看，不修改权限/);
  await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), true);
  await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), false);
  await f.commands.get('details')!.handler('展开', f.ctx); assert.equal(f.expanded(), true);
  await f.commands.get('details')!.handler('unknown', f.ctx); assert.equal(f.expanded(), true);
  assert.match(f.notices.at(-1)!, /不构成批准/);
  await f.commands.get('exit')!.handler('', f.ctx); assert.equal(f.shutdowns(), 1);
  assert.equal(f.writes.length, 0, 'commands do not install or mutate editor/session components');
});

test('BioMind components bound narrow-width output and strip untrusted terminal controls', () => {
  const f = fixture();
  (f.ctx as any).cwd = '/synthetic/\x1b]0;forged-title\x07中文🧬e\u0301\x1b[2J\x00\n' + '长'.repeat(20000);
  (f.ctx as any).model = { provider: 'fixture\x1b[31m', id: 'scripted\x1b]52;c;BAD\x07' };
  f.start();
  for (const component of [f.header(), f.footer(), f.widget()]) {
    for (const width of [1, 2, 12, 40, 120, 60, 120]) {
      const lines = component.render(width);
      assert(lines.length <= 4);
      assert(Buffer.byteLength(lines.join('\n')) < 8192);
      assert(!/[\u0000-\u001f\u007f-\u009f]/u.test(lines.join('')));
      assert(!lines.join('').includes('\ufffd'));
      if (width === 1) assert.deepEqual(lines, ['…']);
    }
    assert.deepEqual(component.render(0), []);
    assert.deepEqual(component.render(Number.NaN), []);
  }
  assert(!contents(f.footer(), 512).includes('forged-title'));
  assert(!contents(f.footer(), 512).includes('BAD'));
  for (const title of f.writes.filter(value => value.startsWith('title:'))) {
    assert(!/[\u0000-\u001f\u007f-\u009f]/u.test(title));
  }
});

test('session replacement installs fresh read-only views; disposed views do not render', () => {
  const f = fixture(); f.start();
  const previous = f.header(); previous.dispose!();
  (f.ctx as any).cwd = '/synthetic/new-session';
  f.handlers.get('session_start')!({ type: 'session_start', reason: 'resume' }, f.ctx);
  assert.deepEqual(previous.render(100), []);
  assert.match(contents(f.footer()), /new-session/);
  f.setState({ role: 'Consult', status: 'settled' });
  f.handlers.get('session_tree')!({ type: 'session_tree', newLeafId: 'new-leaf' }, f.ctx);
  assert.match(contents(f.footer()), /本次请求已结束/);
  assert.equal(f.created.length, 9);
});

test('JSON and RPC sessions do not install terminal components or replace their input owners', async () => {
  const headless = fixture(false);
  assert.equal(headless.commands.size, 0, 'native UI commands must not consume headless prompts');
  for (const [mode, hasUI] of [['json', false], ['rpc', true]] as const) {
    const f = fixture(); (f.ctx as any).mode = mode; (f.ctx as any).hasUI = hasUI;
    f.start(); f.handlers.get('session_tree')!({}, f.ctx);
    assert.equal(f.writes.length, 0);
    await f.commands.get('details')!.handler('', f.ctx); assert.equal(f.expanded(), false);
    await f.commands.get('exit')!.handler('', f.ctx); assert.equal(f.shutdowns(), 1);
  }
});
