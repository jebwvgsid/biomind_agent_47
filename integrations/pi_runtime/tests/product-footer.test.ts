import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import type { ContextUsage, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { installProductFooter } from '../product-footer.js';

const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
const tools = await import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href);
interface Component { render(width: number): string[]; invalidate(): void; dispose?(): void }
function sanitize(value: unknown, fallback = '未提供'): string {
  return typeof value === 'string' && value ? tools.stripTerminalSequences(value).replace(/[\u0000-\u001f\u007f]/g, ' ').trim() : fallback;
}
const usage = (input = 0, output = 0, cacheRead = 0, cacheWrite = 0, cost = 0) => ({
  input, output, cacheRead, cacheWrite, totalTokens: input + output + cacheRead + cacheWrite,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: cost },
});
function fixture() {
  let component!: Component, leaf = 'leaf-1', session = 'session-1', scans = 0, projections = 0, titleReads = 0, redraws = 0, unsubscriptions = 0;
  let entries: any[] = [], context: ContextUsage | undefined = { tokens: 38100, contextWindow: 312000, percent: 12.2 };
  let branch = 'main', name = '科研会话', thinking = 'high', providers = 2, oauth = false, subscription = false;
  let model: any = { id: 'scripted', provider: 'fixture', contextWindow: 312000, reasoning: true };
  let branchChanged!: () => void;
  const statuses = new Map([['biomind-execution', 'execution:native'], ['biomind-clock', '用时 00:03']]);
  const colors: string[] = [];
  const theme = { fg(color: string, text: string) { colors.push(color); return `\x1b[2m${text}\x1b[22m`; } };
  const ctx = { mode: 'tui', hasUI: true, cwd: '/synthetic/科研项目',
    get model() { return model; }, get thinkingLevel() { return thinking; },
    getContextUsage() { projections++; return context; },
    sessionManager: { getSessionId: () => session, getLeafId: () => leaf, getCwd: () => '/synthetic/科研项目',
      getSessionName: () => { titleReads++; return name; }, getEntries() { scans++; return entries; },
      getBranch(): never { throw new Error('Cumulative totals must not use just the selected branch'); } },
    modelRegistry: new Proxy({ isUsingOAuth: () => oauth, getProvider: () => ({ auth: { oauth: { isSubscription: subscription } } }) },
      { get(target, key) { if (key in target) return target[key as keyof typeof target]; throw new Error(`Forbidden credential API: ${String(key)}`); } }),
    ui: { setFooter(factory: any) {
      component = factory({ requestRender() { redraws++; } }, theme, {
        getGitBranch: () => branch, getExtensionStatuses: () => statuses, getAvailableProviderCount: () => providers,
        onBranchChange(callback: () => void) { branchChanged = callback; return () => { unsubscriptions++; }; },
      });
    } },
  } as unknown as ExtensionContext;
  installProductFooter(ctx, tools, sanitize);
  return { component, colors, statuses, ctx,
    rows: (width = 240) => component.render(width).map(row => tools.stripTerminalSequences(row)),
    entries(value: any[]) { entries = value; leaf += 'x'; },
    context(value: ContextUsage | undefined, advanceLeaf = true) { context = value; if (advanceLeaf) leaf += 'x'; },
    model(value: any) { model = value; }, session(value: string) { session = value; },
    thinking(value: string) { thinking = value; }, name(value: string) { name = value; leaf += 'x'; },
    branch(value: string) { branch = value; branchChanged(); }, providers(value: number) { providers = value; },
    oauth(value: boolean, isSubscription: boolean) { oauth = value; subscription = isSubscription; },
    scans: () => scans, projections: () => projections, titleReads: () => titleReads, redraws: () => redraws, unsubscriptions: () => unsubscriptions,
  };
}

test('footer uses real context token counts and cumulative usage from every native usage entry kind', () => {
  const f = fixture();
  f.entries([
    { type: 'message', message: { role: 'assistant', usage: usage(100, 20, 300, 100, .1) } },
    { type: 'usage', usage: usage(30, 3, 20, 10, .2) },
    { type: 'message', message: { role: 'toolResult', usage: usage(40, 4, 30, 20, .3) } },
    { type: 'branch_summary', usage: usage(50, 5, 40, 30, .4) },
    { type: 'compaction', usage: usage(60, 6, 50, 40, .5) },
    { type: 'message', message: { role: 'user', content: 'not usage' } },
  ]);
  const rows = f.rows();
  assert.match(rows[0], /科研项目 \(main\) • 科研会话/);
  assert.match(rows[1], /^上下文 38\.1k\/312k ↑280 ↓38 R440 W200 CH60\.0% \$1\.500/);
  assert.match(rows[1], /\(fixture\) scripted • high$/);
  assert.match(rows[2], /^用时 00:03 execution:native$/);
  assert.doesNotMatch(rows[1], /12\.2%|\(auto\)/);
});

test('footer preserves unknown context after compaction, small counts, and usage warning colors', () => {
  const f = fixture();
  f.context({ tokens: null, contextWindow: 312000, percent: null });
  assert.match(f.rows()[1], /^上下文 \?\/312k/);
  f.context({ tokens: 125, contextWindow: 312000, percent: .04 });
  assert.match(f.rows()[1], /^上下文 0\.1k\/312k/);
  f.context({ tokens: 41200, contextWindow: 312000, percent: 12.2 });
  assert.match(f.rows()[1], /^上下文 41\.2k\/312k/); // Do not reconstruct tokens from a rounded percentage.
  f.context({ tokens: 230000, contextWindow: 312000, percent: 73.7 }); f.colors.length = 0; f.rows();
  assert(f.colors.includes('warning'));
  f.context({ tokens: 290000, contextWindow: 312000, percent: 92.9 }); f.colors.length = 0; f.rows();
  assert(f.colors.includes('error'));
  f.context(undefined); f.model(undefined);
  assert.match(f.rows()[1], /^上下文 \?\/\?/);
});

test('clock redraws, resize, branch and thinking do not rescan; native name and history changes invalidate', () => {
  const f = fixture(); f.rows();
  for (let i = 0; i < 20; i++) { f.statuses.set('biomind-clock', `用时 00:${i}`); f.component.invalidate(); f.rows(40 + i); }
  f.branch('analysis'); f.thinking('off');
  assert.match(f.rows()[0], /\(analysis\) • 科研会话/);
  assert.match(f.rows()[1], /thinking off$/);
  assert.equal(f.redraws(), 1); assert.equal(f.scans(), 1); assert.equal(f.projections(), 1);
  assert.equal(f.titleReads(), 1, 'the native title getter also scans history');
  f.entries([{ type: 'usage', usage: usage(7) }]); f.rows();
  assert.equal(f.scans(), 2); assert.equal(f.projections(), 2);
  f.session('another-session'); f.rows();
  assert.equal(f.scans(), 3); assert.equal(f.projections(), 3);
  f.model({ id: 'another-model', provider: 'fixture', contextWindow: 64000 }); f.context({ tokens: 2000, contextWindow: 64000, percent: 3.125 }, false);
  assert.match(f.rows()[1], /^上下文 2k\/64k/);
  assert.equal(f.scans(), 4); assert.equal(f.projections(), 4);
  f.name('new name'); assert.match(f.rows()[0], /\(analysis\) • new name/);
  assert.equal(f.scans(), 5); assert.equal(f.titleReads(), 5);
  f.component.dispose!(); f.component.dispose!(); f.branch('after-dispose');
  assert.deepEqual(f.component.render(80), []); assert.equal(f.unsubscriptions(), 1); assert.equal(f.redraws(), 1);
});

test('footer keeps the context first on narrow terminals and bounds CJK, emoji and status text using Pi widths', () => {
  const f = fixture();
  f.name('科研🧬 会话 '.repeat(40)); f.branch('分析🧬'.repeat(40));
  f.statuses.set('user-status', '\x1b]52;c;hidden\x07不换行\n状态🧬'.repeat(40));
  for (const width of [1, 2, 8, 18, 24, 40, 80, 120, 240]) {
    const rows = f.rows(width);
    for (const row of rows) { assert(tools.visibleWidth(row) <= width, `${width}: ${row}`); assert.doesNotMatch(row, /\n|\x1b|hidden/); }
    if (width >= 24) assert.match(rows[1], /^上下文 38\.1k\/312k/);
  }
  for (const width of [0, -1, NaN, Infinity]) assert.deepEqual(f.component.render(width), []);
});

test('subscription labeling uses public auth metadata without resolving credentials', () => {
  const f = fixture();
  f.oauth(true, false); assert.doesNotMatch(f.rows()[1], /\(sub\)/);
  f.oauth(true, true); assert.match(f.rows()[1], /\$0\.000 \(sub\)/);
  f.oauth(false, true); assert.doesNotMatch(f.rows()[1], /\(sub\)/);
  f.model({ id: 'kimi', provider: 'kimi-coding', contextWindow: 312000 });
  assert.match(f.rows()[1], /\$0\.000 \(sub\)/);
});
