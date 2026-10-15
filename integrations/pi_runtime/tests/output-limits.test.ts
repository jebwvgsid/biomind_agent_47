/** Fixed fixtures: real Pi tools and request assembly; scripted provider, no LLM. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createReadToolDefinition, createGrepToolDefinition, createWriteToolDefinition, createEditToolDefinition } from '@earendil-works/pi-coding-agent';
import outputLimits from '../vendor/pi-output-limits/src/index.js';
import { limitToolResult, projectReceipt, measure, type RuntimeConfig } from '../vendor/pi-output-limits/src/biomind.js';
import { runtimeBash } from '../native-bash.js';
import { createHarness, FakeDomain, call, text } from './extension-harness.js';

const limits = { maxBytes: 8192, maxLines: 300 };
const config: RuntimeConfig = { global: limits, tools: new Map(), tailTools: new Set(['bash', 'powershell']) };
const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-output-limits-tests-'));
const metrics: any[] = [];
const event = (name: string, body: string, input: any = {}, details: any = {}, isError = false): any => ({
  type: 'tool_result', toolCallId: 'fixed-call', toolName: name, input, details, isError, content: [{ type: 'text', text: body }] });
const textOf = (value: any) => value.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('');
function apply(value: any, directory = cwd) {
  const projected = limitToolResult(value, config, directory);
  const result = { ...value, ...projected };
  const size = result.details?.outputLimits;
  const raw = textOf(result);
  metrics.push({ tool: value.toolName, inputBytes: Buffer.byteLength(textOf(value)), bodyBytes: size?.bodyBytes ?? measure(raw).bytes,
    bodyLines: size?.bodyLines ?? measure(raw).lines, finalBytes: Buffer.byteLength(raw), finalCodePoints: [...raw].length,
    textBlocks: result.content.filter((b: any) => b.type === 'text').length, truncated: !!projected,
    sources: size?.sources ?? [], isError: result.isError });
  if (projected) { assert(size.bodyBytes <= 8192); assert(size.bodyLines <= 300); }
  assert.equal(result.isError, value.isError);
  return result;
}
function fixture(bytes: number, name: string) {
  const lines = Array.from({ length: Math.ceil(bytes / 100) }, (_, i) => `${String(i + 1).padStart(5, '0')} ${'x'.repeat(92)}`);
  lines[0] = 'HEADER_MARKER_' + name;
  lines[Math.floor(lines.length / 2)] = 'OMITTED_MIDDLE_MARKER_' + name;
  lines[lines.length - 1] = 'TAIL_MARKER_' + name;
  const file = path.join(cwd, name + '.txt'); fs.writeFileSync(file, lines.join('\n'));
  return { file, body: lines.join('\n'), middle: Math.floor(lines.length / 2) + 1 };
}
async function bashResult(command: string, timeout = 5, signal?: AbortSignal) {
  const tool = runtimeBash(async input => ({ command: input.command, env: {} }), () => ({ shellPath: '/bin/bash' }));
  const input = { command, timeout, binding_ref: 'fixture-binding' };
  try {
    const raw = await tool.execute('fixed-bash', input, signal, undefined, { cwd, sessionManager: { getSessionId: () => 'output-limit-fixture', getSessionFile: () => undefined } } as any);
    return event('bash', textOf(raw), input, raw.details);
  } catch (error) { return event('bash', (error as Error).message, input, {}, true); }
}

test('T01 patched configuration uses getAgentDir and trusted project precedence; stable single note', () => {
  const agent = fs.mkdtempSync(path.join(cwd, 'config-agent-')), project = fs.mkdtempSync(path.join(cwd, 'config-project-'));
  fs.mkdirSync(path.join(project, '.pi'));
  fs.writeFileSync(path.join(agent, 'settings.json'), JSON.stringify({ quietStartup: true, outputLimits: { ...limits, tailTools: ['bash', 'powershell'] } }));
  fs.writeFileSync(path.join(project, '.pi/settings.json'), JSON.stringify({ outputLimits: { maxBytes: 4096 } }));
  const before = fs.readFileSync(path.join(agent, 'settings.json'));
  const previous = process.env.PI_CODING_AGENT_DIR; process.env.PI_CODING_AGENT_DIR = agent;
  try {
    const handlers = new Map<string, Function>(); let published: any;
    outputLimits({ on: (name: string, fn: Function) => handlers.set(name, fn), events: { emit: (_name: string, value: any) => { published = value; } } } as any);
    for (const trusted of [false, true]) {
      handlers.get('session_start')!({}, { cwd: project, isProjectTrusted: () => trusted, ui: { notify() {} } });
      assert.equal(published.limits.maxBytes, trusted ? 4096 : 8192);
      assert.equal(published.limits.maxLines, 300);
      const event = { systemPromptOptions: { sections: { existing: 'untouched' } } };
      handlers.get('before_agent_start')!(event); handlers.get('before_agent_start')!(event);
      assert.equal(Object.keys(event.systemPromptOptions.sections).length, 2);
      assert.equal(event.systemPromptOptions.sections.existing, 'untouched');
    }
    assert.deepEqual(fs.readFileSync(path.join(agent, 'settings.json')), before);
    assert(!fs.existsSync(path.join(agent, 'trust.json')));
  } finally { if (previous === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = previous; }
});

test('T02 exact byte/line boundaries, small return identity', () => {
  for (const n of [8191, 8192, 8193]) for (const tool of ['bash', 'ls']) {
    const original = event(tool, 'x'.repeat(n));
    const result = apply(original);
    assert.equal(!!result.details.outputLimits, n > 8192);
    if (n <= 8192) assert.deepEqual(result, original);
  }
  for (const n of [299, 300, 301]) for (const ending of ['', '\n']) {
    const original = event('bash', Array(n).fill('row').join('\n') + ending);
    const result = apply(original); assert.equal(!!result.details.outputLimits, n > 300);
    if (n <= 300) assert.deepEqual(result, original);
  }
});

test('T03 T04 real bash at 20KiB/native threshold/100KiB; omitted source reachable', async () => {
  for (const n of [20 * 1024, 50 * 1024 - 1, 50 * 1024 + 1000, 100 * 1024]) {
    const f = fixture(n, 'size-' + n);
    const raw = await bashResult(`cat '${f.file}'`);
    assert(!raw.isError, textOf(raw));
    const shown = apply(raw);
    assert(textOf(shown).includes('TAIL_MARKER_'));
    assert(!textOf(shown).includes('OMITTED_MIDDLE_MARKER_'));
    const source = shown.details.outputLimits.sources[0]; assert(source.available);
    assert(fs.readFileSync(source.path, 'utf8').includes('OMITTED_MIDDLE_MARKER_'));
    if (raw.details.fullOutputPath) assert.equal(source.path, raw.details.fullOutputPath, 'reuse native source without duplicate');
    else assert.equal(fs.readFileSync(source.path, 'utf8'), f.body);
    const read = createReadToolDefinition(cwd);
    const input = { path: source.path, offset: f.middle, limit: 1 };
    const found = await read.execute('recover', input, undefined, undefined, {} as any);
    assert(textOf(found).includes('OMITTED_MIDDLE_MARKER_'));
  }
});

test('T05 T06 real read: UTF8, non-one offset, missing final newline, native footer and long line', async () => {
  const file = path.join(cwd, 'unicode.txt');
  const lines = Array.from({ length: 2800 }, (_, i) => `第${i + 1}行🧬 ${'内容'.repeat(25)}`);
  fs.writeFileSync(file, lines.join('\n'));
  const read = createReadToolDefinition(cwd);
  for (const offset of [1, 17, 2750]) {
    const input = { path: file, offset };
    const raw = await read.execute('unicode', input, undefined, undefined, {} as any);
    const shown = apply(event('read', textOf(raw), input, raw.details));
    assert(!textOf(shown).includes('\ufffd'));
    if (shown.details.outputLimits) {
      const count = shown.details.outputLimits.bodyLines;
      assert(textOf(shown).includes(`Use read offset=${offset + count}`));
      const next = await read.execute('next', { path: file, offset: offset + count, limit: 1 }, undefined, undefined, {} as any);
      assert(textOf(next).startsWith(lines[offset + count - 1]));
    }
  }
  const long = path.join(cwd, 'long-line.txt'); fs.writeFileSync(long, '前言\n' + '汉🧬'.repeat(3000) + '\nEND');
  const input = { path: long, offset: 2 };
  const raw = await read.execute('long', input, undefined, undefined, {} as any);
  const shown = apply(event('read', textOf(raw), input, raw.details));
  assert(textOf(shown).includes('Line 2 exceeds')); assert(!textOf(shown).includes('Use read offset=2'));
  assert(textOf(shown).includes('splitlines()[1][start:end]'));
  // Native's own first-line-overflow guidance is already small and must pass through.
  fs.writeFileSync(long, '🧬'.repeat(20000));
  const native = await read.execute('native-long', { path: long }, undefined, undefined, {} as any);
  const original = event('read', textOf(native), { path: long }, native.details);
  assert.deepEqual(apply(original), original);
});

test('T07 text blocks share one budget, image position/content and each source preserved', () => {
  const picture = { type: 'image', mimeType: 'image/png', data: 'fixture-image-bytes' };
  const original = event('bash', 'a'.repeat(6000));
  original.content.push(picture, { type: 'text', text: 'b'.repeat(6000) }, { type: 'text', text: 'c'.repeat(6000) });
  const shown = apply(original);
  assert.equal(shown.content[1], picture); assert.equal(shown.details.outputLimits.bodyBytes, 8192);
  assert.equal(shown.details.outputLimits.sources.length, 2);
  assert.equal((textOf(shown).match(/\[pi-output-limits:/g) ?? []).length, 1);
  assert(textOf(shown).includes('c'.repeat(6000)));
});

test('T08 actual nonzero, timeout, cancellation; native error-only source and runtime status survive', async () => {
  const f = fixture(100 * 1024, 'error-output');
  for (const kind of ['nonzero', 'timeout', 'cancel']) {
    const controller = new AbortController();
    const command = `cat '${f.file}'; ` + (kind === 'nonzero' ? 'exit 7' : 'sleep 10');
    const timer = kind === 'cancel' ? setTimeout(() => controller.abort(), 500) : undefined;
    const raw = await bashResult(command, kind === 'timeout' ? 0.3 : 5, controller.signal);
    if (timer) clearTimeout(timer);
    assert(raw.isError);
    assert.equal(raw.details.fullOutputPath, undefined);
    const shown = apply(raw), body = textOf(shown);
    assert(body.includes(kind === 'nonzero' ? 'Command exited with code 7' : kind === 'timeout' ? 'Command timed out' : 'Command aborted'));
    assert(body.includes('BioMind execution:'));
    assert(body.includes('fixture-binding'));
    const source = shown.details.outputLimits.sources[0];
    assert.equal(source.scope, 'native full output');
    assert.equal(fs.readFileSync(source.path, 'utf8'), f.body);
    const receipt = JSON.parse(body.match(/BioMind execution: (\{[^\n]+\})/)![1]);
    assert.equal(receipt.status, kind === 'nonzero' ? 'exited' : kind === 'timeout' ? 'timed_out' : 'cancelled');
    if (kind === 'nonzero') assert.equal(receipt.exit_code, 7);
    else assert.equal(receipt.exit_code, undefined, 'do not invent a termination exit code');
  }
});

test('T09 structured fields preserve JSON, state, binding and source; pages are not recut', () => {
  const value = { status: 'exited', exit_code: 7, run_ref: 'run-fixture', binding_ref: 'binding-fixture',
    stdout: 'a'.repeat(10000), stderr: 'b'.repeat(10000), runtime_binding: { binding_ref: 'nested-binding' },
    candidates: Array.from({ length: 1000 }, (_, id) => ({ id, description: 'item '.repeat(20) })) };
  const shown: any = projectReceipt(value, 'probe_command', cwd, limits);
  const parsed = JSON.parse(JSON.stringify(shown));
  for (const key of ['status', 'exit_code', 'run_ref', 'binding_ref', 'runtime_binding']) assert.deepEqual(parsed[key], (value as any)[key]);
  assert(Buffer.byteLength(parsed.stdout + parsed.stderr) <= 8192);
  assert.deepEqual(JSON.parse(fs.readFileSync(parsed.output_preview.source, 'utf8')), value);
  assert.equal(value.stdout.length, 10000, 'original record is immutable');
  const page = { status: 'read', stdout_delta: '汉'.repeat(2000), stderr_delta: '', next_cursor: 'opaque-signed',
    delta_byte_ranges: { stdout: [0, 6000] }, log_refs: { stdout: '/unchanged' } };
  assert.deepEqual(projectReceipt(page, 'read_run_log', cwd, limits), page);
});

test('T11 user quotation and actual native write/edit are unchanged', async () => {
  const answer = { status: 'answered', answer: '用户原话🧬'.repeat(5000) };
  assert.equal(projectReceipt(answer, 'ask_user', cwd, limits), answer);
  const file = path.join(cwd, 'write.txt'), body = 'original-write-🧬'.repeat(2000);
  const write = createWriteToolDefinition(cwd), edit = createEditToolDefinition(cwd);
  const written = await write.execute('write', { path: file, content: body }, undefined, undefined, {} as any);
  assert.equal(fs.readFileSync(file, 'utf8'), body);
  assert.equal(limitToolResult(event('write', textOf(written)), config, cwd), undefined);
  const edited = await edit.execute('edit', { path: file, edits: [{ oldText: body, newText: body + '\nchanged' }] }, undefined, undefined, {} as any);
  assert.equal(fs.readFileSync(file, 'utf8'), body + '\nchanged');
  assert.equal(limitToolResult(event('edit', textOf(edited)), config, cwd), undefined);
});

test('T12 native grep truncation warning and honest saved-return scope', async () => {
  const file = path.join(cwd, 'matches.txt'); fs.writeFileSync(file, Array.from({ length: 500 }, (_, i) => `match${i} ${'x'.repeat(700)}`).join('\n'));
  const tool = createGrepToolDefinition(cwd);
  const raw = await tool.execute('grep', { pattern: 'match', path: file, limit: 100 }, undefined, undefined, {} as any);
  const shown = apply(event('grep', textOf(raw), { pattern: 'match', path: file, limit: 100 }, raw.details));
  assert(textOf(shown).includes('matches limit reached'));
  assert(textOf(shown).includes('Some lines truncated'));
  assert(textOf(shown).includes('not all matches'));
  assert(shown.details.outputLimits.sources[0].scope.includes('upstream already-truncated'));
  const found = await tool.execute('target', { pattern: '^match250 ', path: file }, undefined, undefined, {} as any);
  assert(textOf(found).includes('match250'));
  const protectedReturn = event('rg', 'match: '.repeat(3000), {}, { discovery: { incomplete: true, reasons: ['Search entry limit reached'] } });
  protectedReturn.content.push({ type: 'text', text: '[Search incomplete: Search entry limit reached]' });
  assert(textOf(apply(protectedReturn)).includes('[Search incomplete: Search entry limit reached]'));
});

test('T13 private exclusive spill files, unique concurrent calls, failure and stale original source', async () => {
  const rows = await Promise.all(Array.from({ length: 16 }, async () => apply(event('bash', 'x'.repeat(20000)))));
  const paths = rows.map(row => row.details.outputLimits.sources[0].path);
  assert.equal(new Set(paths).size, paths.length);
  for (const file of paths) { assert.equal(fs.statSync(file).mode & 0o777, 0o600); assert.equal(fs.statSync(path.dirname(file)).mode & 0o777, 0o700); }
  const failed = apply(event('bash', 'x'.repeat(20000)), path.join(cwd, 'absent-parent'));
  assert(textOf(failed).includes('could not be saved')); assert.equal(failed.details.outputLimits.sources[0].path, undefined);
  const missing = apply(event('bash', 'x'.repeat(20000) + '\n\n[Showing lines 1-2 of 100. Full output: /nonexistent-output-fixture]\n\nCommand exited with code 7', {}, {}, true));
  assert(textOf(missing).includes('Original source unavailable'));
  assert(missing.details.outputLimits.sources[0].scope.includes('upstream already-truncated'));
  assert(textOf(missing).includes('Command exited with code 7'));
});

test('T14 real Pi provider request assembly, source recovery, second prompt, disabled rollback', async () => {
  const f = fixture(20000, 'assembly');
  const old = process.env.PI_CODING_AGENT_DIR; process.env.PI_CODING_AGENT_DIR = cwd;
  fs.writeFileSync(path.join(cwd, 'settings.json'), JSON.stringify({ outputLimits: { ...limits, tailTools: ['bash', 'powershell'] }, quietStartup: true }));
  const h = await createHarness({ name: 'output-limits-assembly', cwd, nativePrompt: true, executionProfile: 'native',
    pluginFactories: [{ name: 'pi-output-limits-patched', factory: outputLimits }],
    script: [call('real-bash', 'bash', { command: `cat '${f.file}'`, binding_ref: 'fixture-binding' }),
      (context) => {
        const result: any = context.messages.find(m => m.role === 'toolResult');
        assert(result); assert(!textOf(result).includes('OMITTED_MIDDLE_MARKER_assembly'));
        const source = textOf(result).match(/Read source: ([^\n]+\/output\.log)\./)![1];
        return call('source-read', 'read', { path: source, offset: f.middle, limit: 1 });
      }, text('Recovered middle marker using original source.'), text('Next request retains only previews.')],
    settings: { compaction: { enabled: false } } });
  h.domain.receipts.set('environments.launch', (params: any) => ({ command: params.command, env: {} }));
  try {
    await h.prompt('Find the fixed middle marker without rerunning any producer.');
    await h.prompt('Keep the same output rules.');
    assert.deepEqual(h.errors, []);
    assert.equal(h.requests.length, 4);
    const second: any = h.requests[1], third: any = h.requests[2], fourth: any = h.requests[3];
    assert(!JSON.stringify(second).includes(f.body));
    assert(JSON.stringify(third).includes('OMITTED_MIDDLE_MARKER_assembly'));
    assert(!JSON.stringify(fourth).includes(f.body));
    for (const request of h.requests) {
      const encoded = JSON.stringify(request);
      assert.equal(encoded.split('普通文本工具结果正文默认最多 300 行或 8192 字节').length - 1, 1);
      assert.equal(encoded.split('大文件或长工具输出按需检索、分段读取或在计算端统计筛选').length - 1, 1);
      assert(encoded.includes('You are an expert coding assistant operating inside pi'));
      assert(encoded.includes('binding_ref')); assert(encoded.includes('Be concise in your responses'));
    }
    assert.equal(h.domain.calls.filter(c => c.method === 'environments.launch').length, 1);
    assert(h.domain.calls.some(c => c.method === 'output.configure' && c.params.limits.maxBytes === 8192));
    if (process.env.BIOMIND_OUTPUT_LIMITS_EVIDENCE) fs.writeFileSync(path.join(process.env.BIOMIND_OUTPUT_LIMITS_EVIDENCE, 'provider-requests-mock.json'), JSON.stringify(h.requests, null, 2));
  } finally { await h.close(); if (old === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = old; }
  const off = await createHarness({ name: 'output-limits-disabled', cwd, nativePrompt: true, executionProfile: 'native', script: [text('disabled')] });
  try {
    await off.prompt('Check plugin disabled.');
    assert(!JSON.stringify(off.requests).includes('普通文本工具结果正文默认最多'));
    assert(off.domain.calls.some(c => c.method === 'output.configure' && c.params.limits === null));
    assert.equal(fs.readFileSync(f.file, 'utf8'), f.body);
  } finally { await off.close(); }
});

test('T09 T11 actual BioMind formatter: structured JSON and long user answer reach provider intact', async () => {
  const old = process.env.PI_CODING_AGENT_DIR; process.env.PI_CODING_AGENT_DIR = cwd;
  const answer = '真实用户回答🧬'.repeat(1800);
  const domain = new FakeDomain();
  domain.receipts.set('environments.select', { status: 'available', binding_ref: 'actual-fixture-binding',
    candidates: Array.from({ length: 1000 }, (_, id) => ({ id, description: 'environment '.repeat(40) })), registration_sources: { registry: '/fixture/registry.json' } });
  const h = await createHarness({ name: 'output-structured-and-answer', cwd, domain, executionProfile: 'native',
    pluginFactories: [{ name: 'pi-output-limits', factory: outputLimits }],
    script: [call('env', 'select_environment', {}), call('ask', 'ask_user', { question: 'Fixture user answer?', method: 'editor' }), text('answer received')],
    settings: { compaction: { enabled: false } } });
  h.bus.on('biomind:ui_request', request => h.bus.emit('biomind:ui_response', { ...(request as object), answer }));
  try {
    await h.prompt('Only run the synthetic fixture checks.');
    assert.deepEqual(h.errors, []); assert.equal(h.requests.length, 3);
    const env: any = h.requests[1].messages.find(m => m.role === 'toolResult');
    const parsed = JSON.parse(textOf(env));
    assert.equal(parsed.binding_ref, 'actual-fixture-binding'); assert.equal(parsed.status, 'available');
    assert(parsed.candidates.length < 1000); assert(fs.existsSync(parsed.output_preview.source));
    const user: any = h.requests[2].messages.find(m => m.role === 'toolResult' && m.toolName === 'ask_user');
    assert.equal(JSON.parse(textOf(user)).answer, answer);
  } finally { await h.close(); if (old === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = old; }
});

test.after(() => {
  const directory = process.env.BIOMIND_OUTPUT_LIMITS_EVIDENCE;
  if (directory) { fs.mkdirSync(directory, { recursive: true }); fs.writeFileSync(path.join(directory, 'text-metrics.json'), JSON.stringify({ fixtureRoot: cwd, metrics }, null, 2)); }
});
