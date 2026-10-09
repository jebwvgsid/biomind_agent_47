/** Offline scripts choose the tools. Real local effects are asserted separately;
 * none of these fixtures proves autonomous model behavior or scientific quality. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test, { type TestContext } from 'node:test';
import { requestFromBranch } from '../extensions/biomind.js';
import { call, createHarness, FakeDomain, text } from './extension-harness.js';

type Frame = Record<string, any>;
type Harness = Awaited<ReturnType<typeof createHarness>>;
const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
const content = (value: any): string => typeof value === 'string' ? value : (value ?? []).filter((part: Frame) => part.type === 'text').map((part: Frame) => part.text).join('\n');
const mainRules = (context: Frame): string => context.messages.findLast((message: Frame) => message.role === 'system' && message.sections?.['biomind-current'])?.sections['biomind-current'] ?? '';
function toolResult(context: Frame, id: string): Frame {
  const result = context.messages.findLast((message: Frame) => message.role === 'toolResult' && message.toolCallId === id);
  assert(result, `Missing actual Pi tool message ${id}`);
  return result;
}
function receipt(context: Frame, id: string): Frame { return JSON.parse(content(toolResult(context, id).content)); }
function healthy(h: Harness, t: TestContext, layer: string) {
  assert.deepEqual(h.errors, []);
  assert.deepEqual(h.events.filter(event => event.type === 'message_end' && event.message?.role === 'assistant' && event.message.stopReason === 'error'), []);
  assert.equal(requestFromBranch(h.manager.getBranch())!.role, 'Main');
  t.diagnostic(`${layer}; tool choices and final wording are scripted, not evidence of real-model compliance.`);
}

test('main prompt offline scripted choice: discussion can settle without an execution, plan or question gate', async t => {
  const h = await createHarness({ name: 'main-prompt-discussion', executionProfile: 'native', script: [context => {
    assert.match(mainRules(context), /只要求讨论、方案或审计时，不擅自实施修改或启动正式分析/);
    assert(JSON.stringify(context).includes('只讨论可行性'));
    return text('SYNTHETIC_DISCUSSION_ONLY: 可以先核对输入范围与验证方法；本轮没有实施或正式分析。');
  }] });
  try {
    await h.prompt('只讨论可行性并给出审计方案，不要实施修改或启动正式分析。');
    assert.equal(h.requests.length, 1);
    assert(!h.events.some(event => event.type === 'tool_execution_start'));
    assert(!h.busEvents.some(row => row.channel === 'biomind:ui_request'));
    assert(!h.domain.calls.some(row => /^(exec\.|install\.|files\.(?:write|edit)|memory\.write)/.test(row.method)));
    const state = requestFromBranch(h.manager.getBranch())!;
    assert.equal(state.plan, undefined); assert.equal(state.handoff, undefined); assert.equal(state.status, 'settled');
    healthy(h, t, 'Real Pi/production extension routing; no tool execution, synthetic provider/domain');
  } finally { await h.close(); }
});

test('main prompt offline scripted choice plus real write: explicit file generation needs no repeated question and does not execute reproduce.sh', async t => {
  const cwd = await fs.mkdtemp(path.join(os.tmpdir(), 'biomind-main-direct-write-'));
  const filename = path.join(cwd, 'reproduce.sh'), executionMarker = path.join(cwd, 'script-executed');
  const body = `#!/bin/sh\nprintf synthetic > ${quote(executionMarker)}\n`;
  const h = await createHarness({ name: 'main-prompt-direct-file', cwd, executionProfile: 'native', script: [
    context => { assert.match(mainRules(context), /信息足够就推进/); return call('write-reproduce', 'write', { path: filename, content: body }); },
    context => { assert(!toolResult(context, 'write-reproduce').isError); return text('SYNTHETIC_REPORT: reproduce.sh 已保存，未执行。'); },
  ] });
  try {
    await h.prompt(`请直接生成 ${filename}；仅保存以下脚本，不执行，也不用再询问确认。\n${body}`);
    assert.equal(await fs.readFile(filename, 'utf8'), body);
    await assert.rejects(fs.access(executionMarker), { code: 'ENOENT' });
    assert.deepEqual(h.events.filter(event => event.type === 'tool_execution_start').map(event => event.toolName), ['write']);
    assert(!h.busEvents.some(row => row.channel === 'biomind:ui_request'));
    assert(!h.domain.calls.some(row => row.method.startsWith('exec.') || row.method.startsWith('install.')));
    assert.equal(requestFromBranch(h.manager.getBranch())!.plan, undefined);
    healthy(h, t, 'Real Pi native write and file-content/non-execution witnesses; synthetic provider/domain');
  } finally { await h.close(); }
});

test('main prompt offline scripted ORA choice: reuse registered metadata, retain input QC and do not insert fgsea or routine probes', async t => {
  const cwd = await fs.mkdtemp(path.join(os.tmpdir(), 'biomind-main-ora-'));
  const inputs = path.join(cwd, 'gene-input.tsv');
  await fs.writeFile(inputs, 'gene_id\tin_universe\tselected\nG1\ttrue\ttrue\nG2\ttrue\tfalse\n');
  const domain = new FakeDomain();
  domain.receipts.set('environments.select', { availability: 'AVAILABLE', binding_ref: 'synthetic-base-r-binding',
    healthcheck_status: 'not_run', selected_candidate: { env: 'fixture-r', status: 'registered', versions: { R: 'unknown' } } });
  domain.receipts.set('exec.start', { run_ref: 'synthetic-ora-run', status: 'exited', terminal: true, exit_code: 0, scientific_validation: 'not_assessed' });
  const h = await createHarness({ name: 'main-prompt-ora-reuse', cwd, domain, executionProfile: 'native', script: [
    context => { assert.match(mainRules(context), /适用登记直接使用，不例行重复探测/); return call('ora-input-qc', 'read', { path: inputs }); },
    context => {
      assert.match(content(toolResult(context, 'ora-input-qc').content), /G1\s+true\s+true/);
      return call('ora-environment', 'select_environment', { env_name: 'fixture-r', requirements: { tools: ['Rscript'] } });
    },
    context => {
      const selected = receipt(context, 'ora-environment');
      assert.equal(selected.healthcheck_status, 'not_run'); assert.equal(selected.selected_candidate.versions.R, 'unknown');
      return call('ora-run', 'run_command', { argv: ['Rscript', 'synthetic-base-r-ora.R'], workdir: cwd, binding_ref: selected.binding_ref });
    },
    context => { assert.equal(receipt(context, 'ora-run').scientific_validation, 'not_assessed'); return text('SYNTHETIC_REPORT: 输入材料已读取；合成命令回执不代表科学验证通过。'); },
  ] });
  try {
    await h.prompt('普通 base-R ORA，不使用也未要求 fgsea。先核对给定基因与背景表，按适用登记执行，不做例行环境体检。');
    const names = h.events.filter(event => event.type === 'tool_execution_start').map(event => event.toolName);
    assert.deepEqual(names, ['read', 'select_environment', 'run_command']);
    assert.equal(domain.calls.filter(row => row.method === 'environments.select').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert(!domain.calls.some(row => row.method === 'exec.probe' || row.method === 'environments.locate' || row.method.startsWith('install.')));
    const execution = domain.calls.find(row => row.method === 'exec.start')!.params.arguments;
    assert.equal(execution.binding_ref, 'synthetic-base-r-binding');
    assert.doesNotMatch(JSON.stringify(execution), /fgsea|which|--version|healthcheck|import/);
    healthy(h, t, 'Real Pi native input read; synthetic registry/managed-run receipts and scripted ORA choices, no R analysis');
  } finally { await h.close(); }
});

test('main prompt offline scripted check: explicit R/fgsea check is retained despite registration and failure prevents the scripted analysis', async t => {
  const domain = new FakeDomain();
  domain.receipts.set('environments.select', { availability: 'AVAILABLE', binding_ref: 'synthetic-r-check', healthcheck_status: 'not_run' });
  domain.receipts.set('exec.probe', { run_ref: 'synthetic-explicit-check', status: 'failed', terminal: true, exit_code: 7,
    stdout: 'R_CHECK_SYNTHETIC', stderr: 'FGSEA_CHECK_SYNTHETIC_FAILED', scientific_validation: 'not_assessed' });
  const h = await createHarness({ name: 'main-prompt-explicit-check', executionProfile: 'native', domain, script: [
    call('explicit-env', 'select_environment', { env_name: 'fixture-r' }),
    context => {
      assert.match(mainRules(context), /用户明确要求的检查仍须先完成/);
      assert(JSON.stringify(context).includes('检查失败立即停止'));
      return call('explicit-r-fgsea-check', 'probe_command', { argv: ['Rscript', '-e', 'stopifnot(requireNamespace("fgsea", quietly=TRUE))'],
        workdir: '/synthetic/project', binding_ref: receipt(context, 'explicit-env').binding_ref });
    },
    context => { assert.equal(receipt(context, 'explicit-r-fgsea-check').exit_code, 7); return text('SYNTHETIC_REPORT: 明确检查失败，按条件停止；没有正式分析或重装。'); },
  ] });
  try {
    await h.prompt('即使环境已登记，也先检查 R/fgsea；检查失败立即停止，不安装或正式分析。');
    const check = domain.calls.find(row => row.method === 'exec.probe'); assert(check);
    assert.equal(check.params.arguments.environment_diagnostic, undefined, 'explicit requested checks do not become unsolicited environment diagnostics');
    assert(!domain.calls.some(row => row.method === 'exec.start' || row.method.startsWith('install.')));
    assert.deepEqual(h.events.filter(event => event.type === 'tool_execution_start').map(event => event.toolName), ['select_environment', 'probe_command']);
    healthy(h, t, 'Real Pi tool dispatch and original user/receipt preservation; synthetic R/fgsea result, no R check or model-quality claim');
  } finally { await h.close(); }
});

test('main prompt scripted reporting with real native shell: trailing rc zero and an old artifact do not change the underlying failure evidence', async t => {
  const cwd = await fs.mkdtemp(path.join(os.tmpdir(), 'biomind-main-pipeline-'));
  const old = path.join(cwd, 'prior-result.tsv'), current = path.join(cwd, 'current-result.tsv'), attempts = path.join(cwd, 'attempts');
  const oldBody = '# prior-run=2026-09-26; explicitly old, not produced by this test\ngene\tvalue\nG1\t999\n';
  await fs.writeFile(old, oldBody); await fs.utimes(old, 1600000000, 1600000000);
  const before = await fs.stat(old);
  const failedAnalysis = `/bin/sh -c ${quote(`printf x >> ${quote(attempts)}; printf 'SYNTHETIC_ANALYSIS_FAILED\n' >&2; exit 7`)}`;
  const raw = `${failedAnalysis} | tail -n 1; printf 'WRAPPER_TAIL_RC_0\n'`;
  const wrapped = `set -o pipefail; ${failedAnalysis} | tail -n 1; analysis_rc=$?; printf 'WRAPPER_SUMMARY_SHOWN\n'; exit "$analysis_rc"`;
  const h = await createHarness({ name: 'main-prompt-real-pipeline', cwd, executionProfile: 'native', script: [
    call('raw-pipeline', 'bash', { command: raw }),
    context => {
      const result = toolResult(context, 'raw-pipeline'); assert(!result.isError);
      assert.match(content(result.content), /SYNTHETIC_ANALYSIS_FAILED/); assert.match(content(result.content), /WRAPPER_TAIL_RC_0/);
      assert.match(mainRules(context), /管道末尾 rc 不能单独证明成功/);
      return call('preserved-pipeline', 'bash', { command: wrapped });
    },
    context => {
      const result = toolResult(context, 'preserved-pipeline'); assert.equal(result.isError, true);
      assert.match(content(result.content), /SYNTHETIC_ANALYSIS_FAILED/); assert.match(content(result.content), /code 7/);
      return call('read-prior-artifact', 'read', { path: old });
    },
    context => { assert(content(toolResult(context, 'read-prior-artifact').content).includes(oldBody.trim())); return text('SYNTHETIC_REPORT: 末尾包装成功不代表分析成功；旧文件来自先前运行，本次没有新结果。'); },
  ] });
  try {
    await h.prompt('仅用合成失败进程对比裸管道与保留原退出码包装；核对旧产物来源，不要重装，也不要把旧文件算成本次结果。');
    assert.equal(await fs.readFile(attempts, 'utf8'), 'xx', 'exactly the two explicitly scripted comparison commands ran');
    assert.equal(await fs.readFile(old, 'utf8'), oldBody); assert.equal((await fs.stat(old)).mtimeMs, before.mtimeMs);
    await assert.rejects(fs.access(current), { code: 'ENOENT' });
    assert.deepEqual(h.events.filter(event => event.type === 'tool_execution_start').map(event => event.toolName), ['bash', 'bash', 'read']);
    assert(!h.domain.calls.some(row => row.method.startsWith('install.')));
    healthy(h, t, 'Actual local shell exits, native Pi error receipts, old artifact bytes/mtime and missing current output; reporting remains synthetic');
  } finally { await h.close(); }
});
