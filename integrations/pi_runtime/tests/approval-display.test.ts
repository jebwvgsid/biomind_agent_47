import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { formatApprovalProposal, selectApprovalProposal } from '../approval-display.js';

const choices = ['拒绝', '明确同意执行此提案', '取消'];
function proposal() {
  return { request_id: 'request-fixture', action_hash: 'action-fixture', request_scope: 'scope-fixture', allowed_decisions: ['n', 'y'],
    payload: { kind: 'command', cwd: '/synthetic/分析工作区', human_required: true, source_hashes: { script: 'source-fixture' },
      environment_hash: 'environment-fixture', arguments: { cmd: 'set -x\nmkdir -p results\npython analyze.py "样本 A.mcool"',
        env: { ENV_PREFIX: '/synthetic/env' }, env_name: 'hicexplore', timeout_seconds: 30 } } };
}

test('command approval separates operative fields and preserves the complete original payload', () => {
  const input = proposal(), before = structuredClone(input);
  const main = formatApprovalProposal(input);
  assert.match(main, /工作目录：\/synthetic\/分析工作区/);
  assert.match(main, /软件环境：hicexplore/);
  assert.match(main, /ENV_PREFIX="\/synthetic\/env"/);
  assert(main.includes('命令：\n  set -x\n  mkdir -p results\n  python analyze.py "样本 A.mcool"'));
  assert.match(main, /timeout_seconds/);
  assert.doesNotMatch(main, /environment_hash|source_hashes|human_required|action-fixture/);
  const details = formatApprovalProposal(input, true);
  assert.match(details, /environment-fixture/);
  assert.match(details, /action-fixture/);
  assert.deepEqual(input, before);
});

test('argv, installation and file scope retain the information needed for a decision', () => {
  const argv = formatApprovalProposal({ payload: { kind: 'command', cwd: '/fixture', arguments: { argv: ['python', 'file with spaces.py', 'a\nb'] } } });
  assert.match(argv, /程序及参数（argv）/);
  assert.match(argv, /file with spaces\.py/);
  assert(argv.includes('a\\nb'));
  const install = formatApprovalProposal({ payload: { kind: 'installation', cwd: '/fixture', code: 'install new',
    request: { cmd: 'install new', env_name: 'new-env', prefix: '/fixture/new-env', verification_commands: ['verify new'] } } });
  assert.match(install, /安装软件/); assert.match(install, /命令：install new/);
  assert.match(install, /\/fixture\/new-env/); assert.match(install, /verify new/);
  const file = formatApprovalProposal({ payload: { kind: 'file_write', path: '/fixture/result.txt',
    scope_candidate: { root: '/fixture', operation: 'file_write' }, arguments: { content: 'new text' } } });
  assert.match(file, /\/fixture\/result.txt/); assert.match(file, /scope_candidate/); assert.match(file, /new text/);
});

test('detail navigation never approves and keeps refusal first through repeated views', async () => {
  const input = proposal(), before = structuredClone(input);
  const answers = ['查看校验详情', '收起校验详情', '明确同意执行此提案'];
  let calls = 0;
  const answer = await selectApprovalProposal({ async select(title, options) {
    assert.equal(options[0], '拒绝');
    assert.deepEqual(options.slice(0, 3), choices);
    assert(title.split('\n').length <= 5); assert(title.split('\n').every(line => line.length <= 100));
    return answers[calls++];
  } }, input, choices);
  assert.equal(calls, 3); assert.equal(answer, choices[1]); assert.deepEqual(input, before);
  assert.equal(await selectApprovalProposal({ async select() { return undefined; } }, input, choices), undefined);
});

test('aborting the details view yields no approval and control characters remain visible data', async () => {
  const abort = new AbortController();
  await assert.rejects(selectApprovalProposal({ async select() { abort.abort(); return '查看校验详情'; } }, proposal(), choices, abort.signal),
    { name: 'AbortError' });
  const text = formatApprovalProposal({ payload: { kind: 'command', arguments: { cmd: 'printf x\r\x1b[2J\u202e' } } });
  assert.doesNotMatch(text, /[\r\x1b\u202e]/);
  assert(text.includes('\\u000d\\u001b[2J\\u202e'));
  const env = formatApprovalProposal({ payload: { kind: 'command', arguments: { cmd: 'inspect', env: { MULTILINE: 'first\nsecond' } } } });
  assert(env.includes('MULTILINE="first\\nsecond"'));
});

test('native approval covers underlying welcome rows and keeps framed controls visible across resize and details', async () => {
  const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
  const index = pathToFileURL(require.resolve('@earendil-works/pi-tui'));
  const { visibleWidth, stripTerminalSequences } = await import(index.href);
  const { compositeTuiLine } = await import(new URL('./tui.js', index).href);
  const input = { payload: { kind: 'file_read', path: '/shared/' + '中文路径/'.repeat(25) + 'leaf.mcool',
    project: '/projects/当前项目', scope_candidate: { root: '/shared', recursive: true } } };
  const before = structuredClone(input);
  const offered = ['拒绝', '只读授权该明确目标（当前分析请求）', '只读授权提案列出的目录及子目录（当前分析请求）', '取消'];
  let returned: string | undefined;
  const ui: any = { custom: async (factory: any, options: any) => {
    assert.equal(options.overlayOptions.width, '100%');
    const tui = { terminal: { rows: 24 }, requestRender() {} };
    const theme = { fg: (_: string, s: string) => s, bg: (_: string, s: string) => s, bold: (s: string) => s };
    const component = factory(tui, theme, {}, (value: string) => { returned = value; });
    for (const [width, rows] of [[80, 24], [196, 45], [120, 30], [42, 20], [80, 24]]) {
      tui.terminal.rows = rows;
      for (const key of ['', '\t', '\u001b[6~', '\u001b[5~']) {
        if (key) component.handleInput(key);
        const rendered: string[] = component.render(width);
        assert(rendered.length <= rows - 2);
        assert(rendered.every(line => visibleWidth(line) === width));
        assert(rendered[0].startsWith('┌')); assert(rendered.at(-1)!.endsWith('┘'));
        const screen = rendered.map(line => stripTerminalSequences(compositeTuiLine('WELCOME_LEAK'.repeat(width), line, 0, width, width))).join('\n');
        assert(!screen.includes('WELCOME_LEAK'));
        assert.match(screen, /拒绝/); assert.match(screen, /取消/); assert.match(screen, /Esc\/Ctrl\+C/);
        assert.equal(returned, undefined);
      }
    }
    component.handleInput('\r');
    component.dispose();
    return returned;
  } };
  assert.equal(await selectApprovalProposal(ui, input, offered), '拒绝');
  assert.deepEqual(input, before);
});
