import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { StringDecoder } from 'node:string_decoder';

const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const text = (value: string) => ({ content: [{ type: 'text', text: value }] });
const tool = (id: string, name: string, args: object) => ({ content: [{ type: 'toolCall', id, name, arguments: args }] });
// Read only descendants of this suite's relay processes; never scan unrelated
// jobs or record argv/environment. Start ticks prevent PID-reuse attribution.
type ProcessRow = { pid: number; parent: number; start: string; state: string; comm: string; rssPages: number; fdCount: number };
const observedProcesses = new Map<number, string>();
async function inspectProcess(pid: number): Promise<ProcessRow | undefined> {
  try {
    const raw = await readFile(`/proc/${pid}/stat`, 'utf8'), end = raw.lastIndexOf(')');
    const fields = raw.slice(end + 2).split(' ');
    return { pid, parent: Number(fields[1]), start: fields[19]!, state: fields[0]!, comm: raw.slice(raw.indexOf('(') + 1, end),
      rssPages: Number(fields[21]), fdCount: (await readdir(`/proc/${pid}/fd`)).length };
  } catch { return undefined; }
}
async function processSnapshot(root?: number): Promise<ProcessRow[]> {
  const queue = root ? [root] : [], seen = new Set<number>();
  while (queue.length) {
    const pid = queue.shift()!; if (seen.has(pid)) continue; seen.add(pid);
    const row = await inspectProcess(pid); if (!row) continue;
    observedProcesses.set(pid, row.start);
    try { queue.push(...(await readFile(`/proc/${pid}/task/${pid}/children`, 'utf8')).trim().split(/\s+/).filter(Boolean).map(Number)); } catch { /* child exited */ }
  }
  const result: ProcessRow[] = [];
  for (const [pid, start] of observedProcesses) {
    const row = await inspectProcess(pid);
    if (row?.start === start) result.push(row); else observedProcesses.delete(pid);
  }
  return result;
}
async function launch(steps: any[] | ((cwd: string) => any[] | Promise<any[]>), inheritHistory = false,
  extraConfig: (cwd: string) => Record<string, unknown> = () => ({})) {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-host-'));
  const fixture = path.join(cwd, 'fixture.json'), config = path.join(cwd, 'config.json');
  const script = typeof steps === 'function' ? await steps(cwd) : steps;
  script[0] = { ...script[0], trace_path: path.join(cwd, 'provider-requests.jsonl') };
  await writeFile(fixture, JSON.stringify(script)); await writeFile(path.join(cwd, 'preference.md'), '合成偏好');
  const registryPath = path.join(cwd, 'registry.json');
  await writeFile(registryPath, JSON.stringify({ environments: { fixture: { status: 'verified',
    prefix: '/public/home/liusiqi/anaconda3/envs/biomind', language: 'python', packages: [] } }, tools: {} }));
  await writeFile(path.join(cwd, 'samples.tsv'), 'id\n样本一\n');
  await writeFile(config, JSON.stringify({ providerFixture: fixture, preferencesPath: path.join(cwd, 'preference.md'),
    agentDir: path.join(cwd, 'agent'), dataRoot: path.join(cwd, 'domain'), registryPath,
    ...(inheritHistory ? {} : { historyEnabled: false }), ...extraConfig(cwd) }));
  const processSamples: { phase: string; processes: ProcessRow[] }[] = [];
  if (process.env.BIOMIND_PI_TEST_EVIDENCE) processSamples.push({ phase: 'before-launch', processes: await processSnapshot() });
  const launchedAt = performance.now(); let readyReceivedMs: number | undefined;
  const waitObservations: { timeout_ms: number; elapsed_ms: number; outcome: string; matched_frame_type?: string }[] = [];
  const child = spawn('/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
    ['-B', path.join(runtime, 'testing/posix-pipe-relay.py'), '/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
      '-B', path.resolve(runtime, '../../bio_agents.py'), '--engine', 'pi', '--pi-mode', 'json', '--project-dir', cwd, '--pi-config', config],
    { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', PI_OFFLINE: '1',
      ...(inheritHistory ? { BIOMIND_PI_HISTORY_ENABLED: '1' } : {}) }, stdio: ['pipe', 'pipe', 'pipe'] });
  const frames: any[] = []; const decoder = new StringDecoder('utf8'); let pending = ''; let stderr = '';
  child.stdout.on('data', chunk => { pending += decoder.write(chunk); let end;
    while ((end = pending.indexOf('\n')) >= 0) { const line = pending.slice(0, end); pending = pending.slice(end + 1);
      const frame = JSON.parse(line); if (frame.type === 'ready' && readyReceivedMs === undefined) readyReceivedMs = performance.now() - launchedAt;
      frames.push(frame); } });
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  const exited = new Promise<number | null>(resolve => child.once('exit', resolve));
  let sampling = false;
  const processTimer = process.env.BIOMIND_PI_TEST_EVIDENCE ? setInterval(() => {
    if (sampling) return; sampling = true;
    void processSnapshot(child.pid).then(processes => {
      processSamples.push({ phase: 'running', processes });
      if (processSamples.length > 40) processSamples.splice(1, 1);
    }).finally(() => { sampling = false; });
  }, 250) : undefined;
  async function wait(predicate: (frame: any) => boolean, timeout = 20000) {
    const started = performance.now(), end = started + timeout;
    const observation = { timeout_ms: timeout, elapsed_ms: 0, outcome: 'error', matched_frame_type: undefined as string | undefined };
    try {
      while (performance.now() < end) {
        const found = frames.find(predicate);
        if (found) { observation.outcome = 'matched'; observation.matched_frame_type = found.type; return found; }
        if (child.exitCode !== null) { observation.outcome = 'child_exited'; throw new Error(`Host exited ${child.exitCode}: ${stderr}`); }
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      observation.outcome = 'deadline';
      throw new Error('Host response deadline; frames=' + JSON.stringify(frames.slice(-4)) + '; stderr=' + stderr);
    } finally { observation.elapsed_ms = performance.now() - started; waitObservations.push(observation); }
  }
  return { child, frames, cwd, wait,
    send(value: object) { child.stdin.write(JSON.stringify(value) + '\n'); },
    async close(name: string) {
      child.stdin.end(); let timer: ReturnType<typeof setTimeout>;
      const code = await Promise.race([exited, new Promise<never>((_, reject) => { timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('Host did not exit on EOF')); }, 5000); })]).finally(() => { clearTimeout(timer); clearInterval(processTimer); });
      const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
      if (directory) {
        await mkdir(directory, { recursive: true });
        let providerRequests: unknown[] = [];
        try { providerRequests = (await readFile(path.join(cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); } catch { /* startup/zero-call failure */ }
        processSamples.push({ phase: 'after-relay-exit', processes: await processSnapshot() });
        await writeFile(path.join(directory, name + '-raw.json'), JSON.stringify({ cwd, transport: 'POSIX anonymous pipes via Python relay; Node parent socketpair compatibility not covered', frames, providerRequests, stderr, exitCode: code, processSamples,
          startup: { ready_received_ms: readyReceivedMs ?? null }, waitObservations }, null, 2));
      }
      return code;
    } };
}

test('real host + same extension + real Python: Consult, correlated question, handoff, files and next Consult', async () => {
  const h = await launch([text('咨询中文答复'), tool('question-1', 'ask_user', { question: '输入清单是什么？' }),
    tool('handoff-1', 'handoff_generate', { goal: '读取用户清单' }),
    tool('read-1', 'read', { path: 'samples.tsv' }), text('读取两行，未执行科学作业'), text('下一轮纯答复')]);
  try {
    // The fresh first host reached ready after the previous 20 s cold-start
    // window on this server. Only this startup gets a bounded 60 s allowance;
    // every business response below retains the existing 20 s deadline.
    const ready = await h.wait(frame => frame.type === 'ready', 60000); assert.match(ready.extension_sha256, /^[a-f0-9]{64}$/);
    h.send({ type: 'prompt', requestId: 'consult-1', text: '只咨询' });
    const first = await h.wait(frame => frame.type === 'result' && frame.requestId === 'consult-1');
    assert.equal(first.model_conclusion, '咨询中文答复'); assert.equal(first.scientific_success, null);
    h.send({ type: 'prompt', requestId: 'work-2', text: '请先问我再读取清单' });
    const question = await h.wait(frame => frame.type === 'question');
    assert.equal(question.requestId, 'work-2'); assert(question.businessRequestId); assert(question.id);
    const before = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length, before);
    h.send({ type: 'ui_response', id: 'wrong-id', value: 'invalid' });
    await h.wait(frame => frame.code === 'STALE_UI_RESPONSE');
    h.send({ type: 'ui_response', id: question.id, value: 'samples.tsv' });
    const second = await h.wait(frame => frame.type === 'result' && frame.requestId === 'work-2');
    assert.equal(second.runtime_status, 'completed'); assert.equal(second.extension_sha256, ready.extension_sha256);
    const read = h.frames.find(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolName === 'read');
    assert(read); assert.equal(read.event.isError, false); assert(JSON.stringify(read).includes('样本一'));
    h.send({ type: 'prompt', requestId: 'consult-3', text: '下一次普通输入' });
    await h.wait(frame => frame.type === 'result' && frame.requestId === 'consult-3');
    const states = h.frames.filter(frame => frame.type === 'biomind_state');
    assert.equal(states.at(-1).data.role, 'Consult');
    assert.equal(h.frames.filter(frame => frame.type === 'extension_error').length, 0);
  } finally { assert.equal(await h.close('host-domain'), 0); }
});

test('runtime JSON handshake deadline reaches the real service and reports timeout without sending to the provider', async () => {
  const h = await launch([text('must not be requested')], false, () => ({ handshakeTimeoutMs: 1 }));
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
    h.send({ type: 'prompt', requestId: 'short-deadline', text: '初始化超时不得发送模型' });
    const result = await h.wait(frame => frame.type === 'result' && frame.requestId === 'short-deadline');
    assert.equal(result.runtime_status, 'error'); assert.equal(result.model_conclusion, '');
    const diagnostic = h.frames.find(frame => frame.type === 'diagnostic' && frame.code === 'REQUEST_INITIALIZATION_FAILED');
    assert.equal(diagnostic?.reason, 'DOMAIN_HANDSHAKE_TIMEOUT'); assert.equal(diagnostic.timeoutMs, 1);
    assert.equal(h.frames.some(frame => frame.type === 'event' && frame.event.type === 'message_start'
      && frame.event.message.role === 'assistant'), false);
    await assert.rejects(readFile(path.join(h.cwd, 'provider-requests.jsonl')), { code: 'ENOENT' });
  } finally { assert.equal(await h.close('host-startup-timeout'), 2); }
});

test('real product queue consumes steer within the request and follow-up as a new Consult without a durable ACK', async () => {
  const h = await launch([
    { ...text('合成流等待队列'), chunks: ['合成流', '等待队列'], delay_ms: 150 },
    { ...text('已接收当前约束'), expect_user_contains: '补充当前约束' },
    { ...text('后续独立咨询答复'), expect_user_contains: '后续独立咨询' },
  ]);
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'queue-root', text: '开始当前咨询' });
    await h.wait(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant');
    h.send({ type: 'steer', id: 'steer-one', text: '补充当前约束' });
    h.send({ type: 'follow_up', id: 'follow-one', text: '后续独立咨询' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.model_conclusion, '后续独立咨询答复');
    assert.equal(result.usage.tokens.total, 6);
    const states = h.frames.filter(frame => frame.type === 'biomind_state').map(frame => frame.data);
    const ids = [...new Set(states.map(state => state.requestId))]; assert.equal(ids.length, 2);
    assert(states.every(state => state.role === 'Consult'));
    assert.equal(h.frames.filter(frame => frame.type === 'result').length, 1);
    assert(!h.frames.some(frame => ['accepted', 'ack', 'durable_ack'].includes(frame.type)));
    assert(!h.frames.some(frame => frame.type === 'error' || frame.type === 'extension_error'));
  } finally { assert.equal(await h.close('host-queues'), 0); }
});

test('real product EOF leaves its already approved worker with the original Python owner until terminal', async () => {
  const h = await launch(cwd => [tool('eof-handoff', 'handoff_generate', { goal: '启动短合成作业后断开客户端' }),
    tool('eof-worker', 'run_command', { argv: ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python', '-I', '-u', '-c',
      'from pathlib import Path;import time; p=Path("eof-count");p.write_text(p.read_text()+"x" if p.exists() else "x");print("EOF_WORKER_STARTED",flush=True);time.sleep(3);Path("eof-finished").write_text("done");print("EOF_WORKER_FINISHED",flush=True)'],
      workdir: cwd, env_name: 'fixture', description: '仅合成短作业' }), text('已启动，客户端随后断开。')]);
  let runRef = ''; let logPath = '';
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'eof-request', text: '启动一次合成短作业' });
    const approval = await h.wait(frame => frame.type === 'approval');
    h.send({ type: 'ui_response', id: approval.id, value: 'approve' });
    const result = await h.wait(frame => frame.type === 'result');
    const run = result.execution_facts.find((value: any) => value.run_ref); assert(run); runRef = run.run_ref;
    h.send({ type: 'job', id: 'eof-status', action: 'status', run_ref: runRef });
    const status = await h.wait(frame => frame.type === 'job_result' && frame.id === 'eof-status');
    assert.equal(status.result.terminal, false); assert.equal(status.result.owner_available, true);
    logPath = status.result.log_refs.stdout; assert(logPath.startsWith(h.cwd + '/'));
  } finally { assert.equal(await h.close('host-worker-eof'), 0); }
  let exit: any;
  for (let index = 0; index < 120; index++) {
    try { exit = JSON.parse(await readFile(path.join(path.dirname(logPath), 'exit_status.json'), 'utf8')); } catch { /* owner still running */ }
    if (exit) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert(exit); assert.equal(exit.returncode ?? exit.exit_code, 0);
  assert.equal(await readFile(path.join(h.cwd, 'eof-count'), 'utf8'), 'x');
  assert.equal(await readFile(path.join(h.cwd, 'eof-finished'), 'utf8'), 'done');
  const log = await readFile(logPath, 'utf8'); assert.match(log, /EOF_WORKER_FINISHED/);
  const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
  if (directory) await writeFile(path.join(directory, 'host-worker-eof-terminal.json'), JSON.stringify({ runRef, exit, log,
    launchCount: 1, note: 'Actual approved original owner completes after product EOF; no new service or model was used to query it' }, null, 2));
});

for (const mode of ['ask', 'auto', 'disabled', 'ask-cancel'] as const) {
  test(`real host memory maintenance ${mode}: same Pi extension, real Python Wiki/outbox and counted model calls`, async () => {
    const saves = mode === 'ask' || mode === 'auto';
    const h = await launch([text('合成公开事实：夹具只含三行。'), ...(saves ? [
      tool('maintenance-write', 'maintenance_submit', { operations: [{ op: 'create_page', path: 'methods/pi-full-chain.md',
        title: '合成记忆链路', content: '夹具只含三行，未经科学验证。' }], summary: '仅合成记忆' }), text('记忆已按回执保存。')] : [])],
      false, cwd => ({ historyEnabled: true, memoryMode: mode === 'ask-cancel' ? 'ask' : mode,
        memoryRoot: path.join(cwd, 'memory'), wikiRoot: path.join(cwd, 'wiki') }));
    try {
      await h.wait(frame => frame.type === 'ready');
      h.send({ type: 'prompt', requestId: 'memory-request', text: '仅记录合成公开事实：夹具只含三行，不运行科学作业。' });
      const calls = () => h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length;
      if (mode === 'ask' || mode === 'ask-cancel') {
        const question = await h.wait(frame => frame.type === 'question');
        assert.equal(calls(), 1);
        await new Promise(resolve => setTimeout(resolve, 120)); assert.equal(calls(), 1);
        h.send({ type: 'ui_response', id: question.id, value: mode === 'ask' ? '保存本次记忆' : { cancelled: true } });
      }
      const result = await h.wait(frame => frame.type === 'result');
      assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
      assert.equal(calls(), saves ? 3 : 1); assert.equal(result.usage.tokens.total, (saves ? 3 : 1) * 2);
      const toolEnds = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end');
      assert.equal(toolEnds.length, saves ? 1 : 0);
      if (saves) {
        assert.equal(toolEnds[0].event.toolName, 'maintenance_submit'); assert.equal(toolEnds[0].event.isError, false);
        const paths = await readdir(path.join(h.cwd, 'wiki'), { recursive: true });
        const matches = paths.filter(value => value.endsWith('pi-full-chain.md')); assert.equal(matches.length, 1);
        assert((await readFile(path.join(h.cwd, 'wiki', matches[0]), 'utf8')).includes('夹具只含三行'));
        const domainFiles = await readdir(path.join(h.cwd, 'domain'), { recursive: true });
        const outboxes = domainFiles.filter(value => value.includes('/outbox/') && value.endsWith('.json'));
        assert.equal(outboxes.length, 1);
        const outbox = JSON.parse(await readFile(path.join(h.cwd, 'domain', outboxes[0]), 'utf8'));
        assert.equal(outbox.status, 'succeeded'); assert(outbox.public_sources.length > 0);
      } else {
        const domainFiles = await readdir(path.join(h.cwd, 'domain'), { recursive: true });
        assert.equal(domainFiles.filter(value => value.includes('/outbox/') && value.endsWith('.json')).length, 0);
      }
    } finally { assert.equal(await h.close('host-memory-' + mode), 0); }
  });
}

test('headless shutdown ignores later prompt in same input chunk and exits', async () => {
  const h = await launch([text('must not be called')]);
  try {
    await h.wait(frame => frame.type === 'ready');
    h.child.stdin.write('{"type":"shutdown"}\n{"type":"prompt","text":"ignored"}\n');
  } finally { assert.equal(await h.close('host-shutdown'), 0); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'agent_start').length, 0);
});

test('real host archival projection uses the existing SQLite writer while history reads stay disabled', async () => {
  const h = await launch([text('公开合成回答；没有科学运行。')], false,
    cwd => ({ archivePath: path.join(cwd, 'fresh-archive.sqlite') }));
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'archive-request', text: '公开合成问题。' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed');
    const receipts = h.frames.filter(frame => frame.type === 'archive_receipt');
    assert.equal(receipts.length, 2);
    for (const receipt of receipts) {
      assert.equal(receipt.status, 'enqueued'); assert.equal(receipt.durable, false);
      assert.match(receipt.eventId, /^pi:/);
    }
    h.send({ type: 'settings', id: 'archive-settings' });
    const settings = await h.wait(frame => frame.type === 'settings_result' && frame.id === 'archive-settings');
    assert.equal(settings.history_enabled, false);
  } finally { assert.equal(await h.close('host-archive'), 0); }
  const { stdout } = await promisify(execFile)('/public/home/liusiqi/anaconda3/envs/biomind/bin/python', ['-B', '-I', '-c',
    'import json,sqlite3,sys,time\nrows=[]\nfor _ in range(100):\n try:\n  with sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True) as c:\n   c.row_factory=sqlite3.Row; rows=[dict(r) for r in c.execute("SELECT * FROM session_events ORDER BY id")]\n except sqlite3.OperationalError: pass\n if len(rows)>=2: break\n time.sleep(.05)\nprint(json.dumps(rows,ensure_ascii=False))',
    path.join(h.cwd, 'fresh-archive.sqlite')], { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8' } });
  const rows = JSON.parse(stdout); assert.equal(rows.length, 2);
  assert.equal(rows[0].event_type, 'user_message'); assert.equal(rows[1].event_type, 'assistant_message');
  assert(JSON.stringify(rows).includes('公开合成回答'));
  const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
  if (directory) await writeFile(path.join(directory, 'host-archive-persisted.json'), JSON.stringify({ rows,
    note: 'Actual original SQLite archive after EOF drain; entirely synthetic content; enqueue alone was not considered durable' }, null, 2));
});

test('headless malformed protocol exits nonzero without model request or legacy fallback', async () => {
  const h = await launch([text('must not be called')]);
  try {
    await h.wait(frame => frame.type === 'ready');
    h.child.stdin.write('not-json\n');
  } finally { assert.equal(await h.close('host-malformed'), 2); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'agent_start').length, 0);
});

test('real product top-level frame failure aborts a pending question and exits nonzero without continuation', async () => {
  const h = await launch([tool('failure-question', 'ask_user', { question: '等待真实用户输入' })]);
  h.child.stdin.on('error', () => { /* oversized frame can close the peer during a pending write */ });
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'failure-pending', text: '先询问，等待时模拟传输故障' });
    await h.wait(frame => frame.type === 'question');
    h.child.stdin.write('x'.repeat(4 * 1024 * 1024 + 1));
  } finally { assert.equal(await h.close('host-pending-frame-failure'), 2); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length, 1);
  assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 0);
  assert(!h.frames.some(frame => frame.type === 'result' && frame.runtime_status === 'completed'));
});

test('real product delivers an existing file fact and an explicit missing file error without inventing download paths', async () => {
  const h = await launch(cwd => [tool('delivery-handoff', 'handoff_generate', { goal: '核实合成文件存在性' }),
    tool('delivery-existing', 'read', { path: 'samples.tsv' }),
    tool('delivery-missing', 'read', { path: 'missing-result.tsv' }),
    text(`已读取 ${path.join(cwd, 'samples.tsv')}。missing-result.tsv 不存在，没有生成或下载该文件。`)]);
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'delivery-request', text: '核实 samples.tsv 和 missing-result.tsv，仅报告实际文件事实。' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
    const tools = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end');
    const existing = tools.find(frame => frame.event.toolCallId === 'delivery-existing');
    const missing = tools.find(frame => frame.event.toolCallId === 'delivery-missing');
    assert(existing); assert.equal(existing.event.isError, false);
    assert(JSON.stringify(existing).includes(path.join(h.cwd, 'samples.tsv')));
    assert(missing); assert.equal(missing.event.isError, true);
    assert.match(JSON.stringify(missing), /FileNotFoundError/);
    assert(result.model_conclusion.includes(path.join(h.cwd, 'samples.tsv')));
    assert(result.model_conclusion.includes('不存在'));
    assert(!/https?:\/\//.test(result.model_conclusion));
    const calls = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(calls.length, 4);
    const finalContext = JSON.stringify(calls.at(-1).context);
    assert(finalContext.includes('FileNotFoundError')); assert(finalContext.includes('样本一'));
  } finally { assert.equal(await h.close('host-file-delivery'), 0); }
});

test('real product reads synthetic historical evidence without replaying or changing old permission, run or output facts', async () => {
  let seed: any;
  const h = await launch(async cwd => {
    const { stdout } = await promisify(execFile)('/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
      ['-B', path.join(runtime, 'tests/seed_history_fixture.py'), '--cwd', cwd],
      { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' } });
    seed = JSON.parse(stdout);
    return [tool('historical-search', 'memory_search', seed.search_params),
      tool('historical-read', 'memory_read', seed.read_params),
      text('P16D_HISTORICAL_SENTINEL 是已有合成记录；本轮只读，没有重放旧作业。')];
  }, false, () => {
    // The seed's direct-domain-only skillsRoot is not a product host setting.
    const { skillsRoot: _seedSkills, ...config } = seed.configOverrides; return config;
  });
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'historical-read-only', text: '只读查阅 P16D_HISTORICAL_SENTINEL 对应的已有合成历史，不执行旧作业。' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
    const ends = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end');
    assert.equal(ends.length, 2); assert(ends.every(frame => frame.event.isError === false));
    assert.deepEqual(ends.map(frame => frame.event.toolName), ['memory_search', 'memory_read']);
    const requests = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(requests.length, 3); assert(JSON.stringify(requests.at(-1).context).includes('P16D_HISTORICAL_SENTINEL'));
    assert(!h.frames.some(frame => frame.type === 'approval'));
  } finally { assert.equal(await h.close('host-history-read-only'), 0); }
  const { stdout } = await promisify(execFile)('/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
    ['-B', path.join(runtime, 'tests/seed_history_fixture.py'), '--verify', seed.manifest_path],
    { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' } });
  const verified = JSON.parse(stdout);
  assert.equal(verified.status, 'UNCHANGED'); assert.equal(verified.launch_count, 'x');
  const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
  if (directory) await writeFile(path.join(directory, 'host-history-facts-unchanged.json'), JSON.stringify({ seed,
    verified, before: JSON.parse(await readFile(seed.manifest_path, 'utf8')) }, null, 2));
});

test('real headless control: settings, approved worker, observation and scoped cancel without extra model calls', async () => {
  const h = await launch(cwd => [tool('control-handoff', 'handoff_generate', { goal: '启动合成标准库等待作业' }),
    tool('control-run', 'run_command', { argv: ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python', '-I', '-u', '-c',
      'from pathlib import Path;import time;Path("start-count").write_text("x");print("synthetic-worker",flush=True);time.sleep(20)'],
      workdir: cwd, env_name: 'fixture', description: '合成等待作业；测试宿主随后取消' }), text('已启动合成作业，等待宿主观察')], true);
  let completed = false;
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'settings', id: 'settings-initial' });
    const initial = await h.wait(frame => frame.type === 'settings_result' && frame.id === 'settings-initial');
    assert.equal(initial.status, 'ok'); assert.equal(initial.history_enabled, true, 'explicit env switch survives omitted config');
    h.send({ type: 'settings', id: 'settings-off', history_enabled: false, memory_mode: 'disabled' });
    const settings = await h.wait(frame => frame.type === 'settings_result' && frame.id === 'settings-off');
    assert.equal(settings.status, 'ok'); assert.equal(settings.history_enabled, false);
    h.send({ type: 'prompt', requestId: 'control-request', text: '运行一次合成等待作业' });
    const approval = await h.wait(frame => frame.type === 'approval');
    h.send({ type: 'ui_response', id: approval.id, value: 'approve' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
    assert(result.usage.tokens.total > 0);
    const run = result.execution_facts.find((value: any) => value.run_ref);
    assert(run?.run_ref);
    const execution = h.frames.find(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolName === 'run_command');
    assert(execution); assert.equal(execution.event.isError, false);
    // A handle alone is insufficient: a reconciliation failure can also have
    // one. Require the real worker's marker before exercising live-owner scope.
    let started = false;
    for (let index = 0; index < 100; index++) {
      try { started = await readFile(path.join(h.cwd, 'start-count'), 'utf8') === 'x'; } catch { /* worker may still be starting */ }
      if (started) break;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert(started, 'the actual approved executor must have started the worker');
    const count = () => h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length;
    const before = count();
    h.send({ type: 'prompt', requestId: 'rejected-new-request', text: '活作业期间的新请求应明确拒绝并保留原owner' });
    const rejected = await h.wait(frame => frame.type === 'result' && frame.requestId === 'rejected-new-request');
    assert.equal(rejected.runtime_status, 'error'); assert.equal(count(), before);
    h.send({ type: 'job', id: 'list', action: 'list' });
    const list = await h.wait(frame => frame.type === 'job_result' && frame.id === 'list');
    assert.equal(list.status, 'ok'); assert.equal(list.result.active_jobs, true);
    h.send({ type: 'job', id: 'logs', action: 'logs', run_ref: run.run_ref });
    const logs = await h.wait(frame => frame.type === 'job_result' && frame.id === 'logs');
    assert.equal(logs.status, 'ok'); assert(JSON.stringify(logs.result).includes('synthetic-worker'));
    assert.equal(typeof logs.result.next_cursor, 'string');
    h.send({ type: 'job', id: 'logs-next', action: 'logs', run_ref: run.run_ref, after_cursor: logs.result.next_cursor });
    const nextLogs = await h.wait(frame => frame.type === 'job_result' && frame.id === 'logs-next');
    assert.equal(nextLogs.status, 'ok'); assert(!String(nextLogs.result.stdout_delta ?? '').includes('synthetic-worker'));
    h.send({ type: 'job', id: 'cancel', action: 'cancel', run_ref: run.run_ref });
    const cancelled = await h.wait(frame => frame.type === 'job_result' && frame.id === 'cancel');
    assert.equal(cancelled.status, 'ok');
    let terminal: any;
    for (let index = 0; index < 100; index++) {
      h.send({ type: 'job', id: 'status-' + index, action: 'status', run_ref: run.run_ref });
      const frame = await h.wait(value => value.type === 'job_result' && value.id === 'status-' + index);
      assert.equal(frame.status, 'ok');
      if (frame.result.terminal) { terminal = frame.result; break; }
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert(terminal); assert.notEqual(terminal.exit_code, 0);
    assert.equal(await readFile(path.join(h.cwd, 'start-count'), 'utf8'), 'x');
    assert.equal(count(), before, 'host observation and cancellation do not call the model');
    completed = true;
  } finally {
    const code = await h.close('host-job-control');
    if (completed) assert.equal(code, 2, 'a rejected ordinary request remains a nonzero runtime outcome');
  }
});

async function p06fFiles(root: string): Promise<string[]> {
  try { return await readdir(root, { recursive: true }); }
  catch (error: any) { if (error.code === 'ENOENT') return []; throw error; }
}
async function p06fPermissions(cwd: string): Promise<any[]> {
  const root = path.join(cwd, 'domain');
  const states = (await p06fFiles(root)).filter(value => value.endsWith('/plan_state.json'));
  const rows: any[] = [];
  for (const file of states) {
    const state = JSON.parse(await readFile(path.join(root, file), 'utf8'));
    rows.push(...Object.values(state.task_state?.execution_permissions?.requests ?? {}));
  }
  return rows;
}
async function p06fFacts(name: string, facts: unknown): Promise<void> {
  const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
  if (directory) { await mkdir(directory, { recursive: true }); await writeFile(path.join(directory, name + '-facts.json'), JSON.stringify(facts, null, 2)); }
}

test('P06f actual host allows scoped native read and rejects hallucinated bash/write/edit', async () => {
  const secret = 'P06F_READ_FIXTURE_MUST_NOT_ENTER_MODEL';
  const original = 'P06F_EDIT_ORIGINAL';
  const h = await launch(async cwd => {
    await writeFile(path.join(cwd, 'builtin-read-fixture.txt'), secret);
    await writeFile(path.join(cwd, 'builtin-edit-target.txt'), original);
    return [tool('builtin-handoff', 'handoff_generate', { goal: '验证未注册入口不执行' }),
      { content: [
        ...tool('builtin-bash', 'bash', { command: 'printf bypass > builtin-bash-marker' }).content,
        ...tool('builtin-read', 'read', { path: path.join(cwd, 'builtin-read-fixture.txt') }).content,
        ...tool('builtin-write', 'write', { path: path.join(cwd, 'builtin-write-marker'), content: 'bypass' }).content,
        ...tool('builtin-edit', 'edit', { path: path.join(cwd, 'builtin-edit-target.txt'), oldText: original, newText: 'BYPASSED' }).content,
      ] }, text('内置入口不可用；没有执行这些操作。')];
  });
  try {
    await h.wait(frame => frame.type === 'ready');
    h.send({ type: 'prompt', requestId: 'p06f-builtins', text: '仅测试未注册工具会被拒绝，不允许绕过领域入口。' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
    const errors = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end'
      && ['bash', 'write', 'edit'].includes(frame.event.toolName));
    assert.equal(errors.length, 3);
    for (const row of errors) { assert.equal(row.event.isError, true); assert.match(JSON.stringify(row.event.result), /not found/i); }
    assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 0);
    assert.deepEqual(await p06fPermissions(h.cwd), []);
    const files = await readdir(h.cwd);
    assert(!files.includes('builtin-bash-marker')); assert(!files.includes('builtin-write-marker'));
    assert.equal(await readFile(path.join(h.cwd, 'builtin-edit-target.txt'), 'utf8'), original);
    const providerRequests = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(providerRequests.length, 3); assert(JSON.stringify(providerRequests).includes(secret));
    assert(errors.every(row => JSON.stringify(providerRequests.at(-1).context).includes(`Tool ${row.event.toolName} not found`)));
    await p06fFacts('host-p06f-builtins', { status: 'PASS', errors, markerFilesAbsent: true, editUnchanged: true,
      scopedReadContentPresent: true, permissions: [], providerCalls: providerRequests.length });
  } finally { assert.equal(await h.close('host-p06f-builtins'), 0); }
});

for (const decision of ['approve', 'deny'] as const) {
  test(`P06f actual host explicit memory_write ${decision} uses correlated human approval and original Wiki store`, async () => {
    const marker = `P06F_EXPLICIT_MEMORY_${decision.toUpperCase()}`;
    const filename = `explicit-${decision}.md`;
    const h = await launch([
      tool('explicit-memory-handoff', 'handoff_generate', { goal: '提出显式Wiki写入并等待用户批准' }),
      tool('explicit-memory-write', 'memory_write', { target: 'wiki', operation: 'create', path: 'methods/' + filename,
        content: marker + '；合成事实，科学结果未评估。' }), text('只依据本次显式记忆写入回执回答。'),
    ], false, cwd => ({ historyEnabled: true, memoryMode: 'manual', memoryRoot: path.join(cwd, 'memory'), wikiRoot: path.join(cwd, 'wiki') }));
    try {
      await h.wait(frame => frame.type === 'ready');
      h.send({ type: 'prompt', requestId: 'p06f-explicit-memory-' + decision, text: '只提出显式Wiki写入，等待用户审批；不要运行可选自动维护。' });
      const approval = await h.wait(frame => frame.type === 'approval');
      assert.equal(approval.toolCallId, 'explicit-memory-write');
      assert.equal(approval.payload.payload.kind, 'memory_write'); assert.equal(approval.payload.payload.human_required, true);
      assert.equal(approval.businessRequestId, approval.payload.request_scope.request_key);
      const before = await p06fPermissions(h.cwd); assert.equal(before.length, 1); assert.equal(before[0].status, 'pending');
      assert.equal((await p06fFiles(path.join(h.cwd, 'wiki'))).filter(value => value.endsWith(filename)).length, 0);
      h.send({ type: 'ui_response', id: approval.id + '-stale', value: 'approve' });
      await h.wait(frame => frame.code === 'STALE_UI_RESPONSE');
      assert.equal((await p06fPermissions(h.cwd))[0].status, 'pending');
      h.send({ type: 'ui_response', id: approval.id, value: decision });
      const result = await h.wait(frame => frame.type === 'result');
      assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
      const ends = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end');
      assert.deepEqual(ends.map(frame => frame.event.toolName), ['handoff_generate', 'memory_write']);
      assert.equal(ends[1].event.isError, false);
      assert.equal(ends[1].event.result.details.status, decision === 'approve' ? 'written' : 'denied');
      assert.equal(h.frames.filter(frame => frame.type === 'question').length, 0, 'manual mode cannot start maintenance consent');
      const files = await p06fFiles(path.join(h.cwd, 'wiki'));
      const written = files.filter(value => value.endsWith(filename)); assert.equal(written.length, decision === 'approve' ? 1 : 0);
      if (decision === 'approve') assert((await readFile(path.join(h.cwd, 'wiki', written[0]), 'utf8')).includes(marker));
      const permissions = await p06fPermissions(h.cwd); assert.equal(permissions.length, 1);
      assert.equal(permissions[0].payload.kind, 'memory_write');
      assert.equal(permissions[0].status, decision === 'approve' ? 'allowed' : 'denied');
      assert.equal(Boolean(permissions[0].claim), decision === 'approve');
      const outboxes = (await p06fFiles(path.join(h.cwd, 'domain'))).filter(value => value.includes('/outbox/') && value.endsWith('.json'));
      assert.equal(outboxes.length, 0, 'explicit memory_write must not be counted as maintenance');
      const requests = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
      assert.equal(requests.length, 3);
      await p06fFacts('host-p06f-explicit-memory-' + decision, { status: 'PASS', decision, permissions, written,
        originalWikiContent: decision === 'approve' ? await readFile(path.join(h.cwd, 'wiki', written[0]), 'utf8') : null,
        maintenanceOutboxes: 0, providerCalls: requests.length, toolNames: ends.map(frame => frame.event.toolName) });
    } finally { assert.equal(await h.close('host-p06f-explicit-memory-' + decision), 0); }
  });
}

for (const carrier of ['shell', 'argv'] as const) for (const decision of ['approve', 'deny'] as const) {
  test(`P06f actual host helper ${carrier} -u ${decision} remains behind original formal command approval`, async () => {
    const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
    let helper = ''; let marker = '';
    const h = await launch(async cwd => {
      helper = path.join(cwd, 'synthetic helper.py'); marker = path.join(cwd, 'helper-marker');
      await writeFile(helper, 'import argparse\nfrom pathlib import Path\np=argparse.ArgumentParser();p.add_argument("--marker",required=True);a=p.parse_args()\nf=Path(a.marker);f.write_text(f.read_text()+"x" if f.exists() else "x")\nprint("P06F_REAL_HELPER_FINISHED",flush=True)\n');
      const argv = [python, '-I', '-u', helper, '--marker', marker];
      const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
      return [tool('helper-handoff', 'handoff_generate', { goal: '按真实授权执行一次合成helper' }),
        tool('helper-command', 'run_command', { ...(carrier === 'argv' ? { argv } : { cmd: argv.map(quote).join(' ') }),
          workdir: cwd, description: '只写合成marker的真实helper；-u和含空格路径必须保留' }),
        text('按真实helper回执说明结果，不推断科学成功。')];
    });
    try {
      await h.wait(frame => frame.type === 'ready');
      h.send({ type: 'prompt', requestId: `p06f-helper-${carrier}-${decision}`, text: '运行合成helper前必须等待我的明确授权。' });
      const approval = await h.wait(frame => frame.type === 'approval');
      assert.equal(approval.toolCallId, 'helper-command'); assert.equal(approval.payload.payload.kind, 'command');
      assert.equal(approval.payload.payload.human_required, true); assert.equal(approval.payload.payload.cwd, h.cwd);
      const actualArgs = approval.payload.payload.arguments;
      if (carrier === 'argv') { assert(actualArgs.argv.includes('-u')); assert(actualArgs.argv.includes(helper)); }
      else { assert(actualArgs.cmd.includes("'-u'")); assert(actualArgs.cmd.includes("'" + helper + "'")); }
      assert(!(await readdir(h.cwd)).includes('helper-marker'));
      const before = await p06fPermissions(h.cwd); assert.equal(before.length, 1); assert.equal(before[0].status, 'pending');
      h.send({ type: 'ui_response', id: approval.id, value: decision });
      const result = await h.wait(frame => frame.type === 'result');
      assert.equal(result.runtime_status, 'completed'); assert.equal(result.scientific_success, null);
      const toolEnd = h.frames.find(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolName === 'run_command');
      assert(toolEnd); assert.equal(toolEnd.event.isError, false);
      let terminal: any;
      if (decision === 'approve') {
        const run = toolEnd.event.result.details; assert(run.run_ref);
        for (let index = 0; index < 100; index++) {
          h.send({ type: 'job', id: 'helper-status-' + index, action: 'status', run_ref: run.run_ref });
          const observed = await h.wait(frame => frame.type === 'job_result' && frame.id === 'helper-status-' + index);
          assert.equal(observed.status, 'ok');
          if (observed.result.terminal) { terminal = observed.result; break; }
          await new Promise(resolve => setTimeout(resolve, 20));
        }
        assert(terminal); assert.equal(terminal.exit_code, 0); assert.equal(await readFile(marker, 'utf8'), 'x');
        assert(String(terminal.stdout).includes('P06F_REAL_HELPER_FINISHED'));
      } else {
        assert.equal(toolEnd.event.result.details.status, 'denied'); assert(!toolEnd.event.result.details.run_ref);
        assert(!(await readdir(h.cwd)).includes('helper-marker'));
        assert.equal((await p06fFiles(path.join(h.cwd, 'domain'))).filter(value => value.endsWith('observation_intent.json')).length, 0);
      }
      const permissions = await p06fPermissions(h.cwd); assert.equal(permissions.length, 1);
      assert.equal(permissions[0].status, decision === 'approve' ? 'allowed' : 'denied');
      assert.equal(Boolean(permissions[0].claim), decision === 'approve');
      const requests = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
      assert.equal(requests.length, 3, 'host observation cannot request another model call');
      await p06fFacts(`host-p06f-helper-${carrier}-${decision}`, { status: 'PASS', carrier, decision, permissions,
        helperPath: helper, helperSource: await readFile(helper, 'utf8'), terminal: terminal ?? null,
        marker: decision === 'approve' ? await readFile(marker, 'utf8') : null, providerCalls: requests.length });
    } finally { assert.equal(await h.close(`host-p06f-helper-${carrier}-${decision}`), 0); }
  });
}
