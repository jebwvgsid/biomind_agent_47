import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir, readdir, chmod, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { StringDecoder } from 'node:string_decoder';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';

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
  extraConfig: (cwd: string) => Record<string, unknown> = () => ({}), reuseCwd?: string, productEntry = false) {
  const cwd = reuseCwd ?? await mkdtemp(path.join(tmpdir(), 'biomind-host-'));
  await mkdir(path.join(cwd, 'home'), { recursive: true });
  const fixture = path.join(cwd, 'fixture.json'), config = path.join(cwd, 'config.json');
  const script = typeof steps === 'function' ? await steps(cwd) : steps;
  script[0] = { ...script[0], trace_path: path.join(cwd, 'provider-requests.jsonl') };
  await writeFile(fixture, JSON.stringify(script)); await writeFile(path.join(cwd, 'preference.md'), '合成偏好');
  const registryPath = path.join(cwd, 'registry.json');
  await writeFile(registryPath, JSON.stringify({ environments: { fixture: { status: 'verified',
    prefix: '/public/home/liusiqi/anaconda3/envs/biomind', language: 'python', packages: [] } }, tools: {} }));
  await writeFile(path.join(cwd, 'samples.tsv'), 'id\n样本一\n');
  await writeFile(config, JSON.stringify({ executionProfile: 'protected', providerFixture: fixture, preferencesPath: path.join(cwd, 'preference.md'),
    agentDir: path.join(cwd, 'agent'), dataRoot: path.join(cwd, 'domain'), registryPath,
    memoryRoot: path.join(cwd, 'memory'), wikiRoot: path.join(cwd, 'wiki'), archivePath: path.join(cwd, 'archive.sqlite'),
    ...(inheritHistory ? {} : { historyEnabled: false }), ...extraConfig(cwd) }));
  const processSamples: { phase: string; processes: ProcessRow[] }[] = [];
  if (process.env.BIOMIND_PI_TEST_EVIDENCE) processSamples.push({ phase: 'before-launch', processes: await processSnapshot() });
  const launchedAt = performance.now(); let readyReceivedMs: number | undefined;
  const waitObservations: { timeout_ms: number; elapsed_ms: number; outcome: string; matched_frame_type?: string }[] = [];
  const child = spawn('/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
    ['-B', path.join(runtime, 'testing/posix-pipe-relay.py'),
      ...(productEntry ? [path.resolve(runtime, '../../bin/biomind')] : ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python', '-B', path.resolve(runtime, '../../bio_agents.py')]), '--engine', 'pi', '--pi-mode', 'json', '--project-dir', cwd, '--pi-config', config],
    { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', PI_OFFLINE: '1', HOME: path.join(cwd, 'home'),
      XDG_CONFIG_HOME: path.join(cwd, 'home/.config'), BIOMIND_PI_PYTHON: '/public/home/liusiqi/anaconda3/envs/biomind/bin/python', BIOMIND_DISABLE_DOTENV: '1', PYTHONDONTWRITEBYTECODE: '1',
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

test('real host + same extension + real Python: one agent, correlated question, files and next request', async () => {
  const h = await launch([text('咨询中文答复'), tool('question-1', 'ask_user', { question: '输入清单是什么？' }),
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
    h.send({ type: 'prompt', requestId: 'consult-3', text: '[新任务] 下一次普通输入' });
    await h.wait(frame => frame.type === 'result' && frame.requestId === 'consult-3');
    const states = h.frames.filter(frame => frame.type === 'biomind_state');
    assert.equal(states.at(-1).data.role, 'Main');
    assert.equal(h.frames.filter(frame => frame.type === 'extension_error').length, 0);
  } finally { assert.equal(await h.close('host-domain'), 0); }
});

test('B1/B3 actual JSON consumes trusted settings/sessionDir and refreshes AGENTS plus preferences without historical rewrites', async () => {
  const h = await launch(async cwd => {
    await mkdir(path.join(cwd,'agent')); await mkdir(path.join(cwd,'.pi'));
    await writeFile(path.join(cwd,'agent/settings.json'), JSON.stringify({defaultProjectTrust:'ask',retry:{enabled:false}}));
    new ProjectTrustStore(path.join(cwd,'agent')).set(cwd,true);
    await writeFile(path.join(cwd,'.pi/settings.json'), JSON.stringify({sessionDir:path.join(cwd,'from-project-settings'),compaction:{enabled:true,keepRecentTokens:1}}));
    await writeFile(path.join(cwd,'AGENTS.md'),'PROJECT_A_ONLY');
    return [text('FIRST_REQUEST'),text('RELOADED_REQUEST'),text('EMPTY_PREFERENCE_REQUEST'),text('SYNTHETIC_NATIVE_COMPACTION_SUMMARY'),text('AFTER_COMPACTION')];
  });
  let savedFile: string | undefined;
  try {
    const ready=await h.wait(f=>f.type==='ready',60000);
    assert.equal(ready.projectTrusted,true);
    assert.equal(ready.sessionDir,path.join(h.cwd,'from-project-settings'));
    await writeFile(path.join(h.cwd,'preference.md'),'GLOBAL_PREF_A');
    h.send({type:'prompt',id:'before',text:'只返回合成见证'});
    await h.wait(f=>f.type==='result' && f.requestId==='before');
    await writeFile(path.join(h.cwd,'preference.md'),'GLOBAL_PREF_B');
    await writeFile(path.join(h.cwd,'AGENTS.md'),'PROJECT_B_ONLY');
    h.send({type:'reload',id:'reload-1'}); await h.wait(f=>f.type==='reload_result' && f.id==='reload-1');
    h.send({type:'prompt',id:'after',text:'只返回新的合成见证'});
    await h.wait(f=>f.type==='result' && f.requestId==='after');
    await writeFile(path.join(h.cwd,'preference.md'),'');
    h.send({type:'reload',id:'reload-2'}); await h.wait(f=>f.type==='reload_result' && f.id==='reload-2');
    h.send({type:'prompt',id:'empty',text:'只返回空偏好见证'});
    const empty=await h.wait(f=>f.type==='result' && f.requestId==='empty'); savedFile=empty.sessionFile;
    h.send({type:'compact',id:'compact-1'}); await h.wait(f=>f.type==='compact_result' && f.id==='compact-1');
    h.send({type:'prompt',id:'after-compact',text:'原生压缩后仅返回合成说明见证'});
    await h.wait(f=>f.type==='result' && f.requestId==='after-compact');
    const calls=(await readFile(path.join(h.cwd,'provider-requests.jsonl'),'utf8')).trim().split('\n').map(l=>JSON.parse(l));
    assert(calls.length>=5);
    const current=(call:any)=>JSON.stringify(call.context.messages.filter((m:any)=>m.role==='system').map((m:any)=>m.sections));
    assert.match(current(calls[0]),/GLOBAL_PREF_A/); assert.match(current(calls[0]),/PROJECT_A_ONLY/);
    assert.match(current(calls[1]),/GLOBAL_PREF_B/); assert.match(current(calls[1]),/PROJECT_B_ONLY/);
    assert(!current(calls[1]).includes('PROJECT_A_ONLY'));
    assert.match(current(calls[2]),/PROJECT_B_ONLY/);
    assert.doesNotMatch(current(calls[2]),/\"role\":\"(?:Consult|Generate)\"/);
    const latestBio=(call:any)=>JSON.stringify([...call.context.messages].reverse().find((m:any)=>m.role==='system' && m.sections?.['biomind-current']));
    assert(!latestBio(calls[2]).includes('GLOBAL_PREF_B'));
    assert.match(current(calls.at(-1)),/PROJECT_B_ONLY/);
    assert(!latestBio(calls.at(-1)).includes('GLOBAL_PREF_B'));
    assert.equal(h.frames.filter(f=>f.type==='ready').length,1);
  } finally { assert.equal(await h.close('startup-settings-instructions'),0); }
  assert(savedFile);
  const resumed=await launch([text('RESUMED_INSTRUCTIONS')],false,()=>({session:savedFile}),h.cwd);
  try {
    const ready=await resumed.wait(f=>f.type==='ready',60000); assert.equal(ready.sessionFile,savedFile);
    await writeFile(path.join(h.cwd,'preference.md'),'GLOBAL_PREF_B');
    resumed.send({type:'prompt',id:'resumed-instructions',text:'恢复同项目后验证当前说明'});
    await resumed.wait(f=>f.type==='result' && f.requestId==='resumed-instructions');
    const calls=(await readFile(path.join(h.cwd,'provider-requests.jsonl'),'utf8')).trim().split('\n').map(l=>JSON.parse(l));
    const current=JSON.stringify(calls.at(-1).context.messages.filter((m:any)=>m.role==='system').map((m:any)=>m.sections));
    assert.match(current,/GLOBAL_PREF_B/); assert.match(current,/PROJECT_B_ONLY/);
  } finally { assert.equal(await resumed.close('startup-resumed-instructions'),0); }
});

test('B1 actual JSON config sessionDir overrides Pi settings and environment', async () => {
  const h=await launch([text('SESSION_DIR_OVERRIDE')],false,cwd=>({sessionDir:path.join(cwd,'explicit-sessions')}));
  try {
    const ready=await h.wait(f=>f.type==='ready',60000);
    assert.equal(ready.sessionDir,path.join(h.cwd,'explicit-sessions'));
    h.send({type:'prompt',id:'persist',text:'保存合成会话'});
    const result=await h.wait(f=>f.type==='result'); assert.equal(result.sessionSaved,true);
    assert(result.sessionFile.startsWith(ready.sessionDir+'/'));
  } finally { assert.equal(await h.close('startup-session-dir'),0); }
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

test('real product queue consumes steer and follow-up as revisions of the same request without a durable ACK', async () => {
  const h = await launch([
    { ...text('合成流等待队列'), chunks: ['合成流', '等待队列'], delay_ms: 150 },
    { ...text('已接收当前约束'), expect_user_contains: '补充当前约束' },
    { ...text('后续独立咨询答复'), expect_user_contains: '后续独立咨询' },
  ]);
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
    h.send({ type: 'prompt', requestId: 'queue-root', text: '开始当前咨询' });
    await h.wait(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant');
    h.send({ type: 'steer', id: 'steer-one', text: '补充当前约束' });
    h.send({ type: 'follow_up', id: 'follow-one', text: '后续独立咨询' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.model_conclusion, '后续独立咨询答复');
    assert.equal(result.usage.tokens.total, 6);
    const states = h.frames.filter(frame => frame.type === 'biomind_state').map(frame => frame.data);
    const ids = [...new Set(states.map(state => state.requestId))]; assert.equal(ids.length, 1);
    assert(states.every(state => state.role === 'Main'));
    assert.equal(h.frames.filter(frame => frame.type === 'result').length, 1);
    assert(!h.frames.some(frame => ['accepted', 'ack', 'durable_ack'].includes(frame.type)));
    assert(!h.frames.some(frame => frame.type === 'error' || frame.type === 'extension_error'));
  } finally { assert.equal(await h.close('host-queues'), 0); }
});

test('real product EOF leaves its already approved worker with the original Python owner until terminal', async () => {
  const h = await launch(cwd => [tool('eof-worker', 'run_command', { argv: ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python', '-I', '-u', '-c',
      'from pathlib import Path;import time; p=Path("eof-count");p.write_text(p.read_text()+"x" if p.exists() else "x");print("EOF_WORKER_STARTED",flush=True);time.sleep(3);Path("eof-finished").write_text("done");print("EOF_WORKER_FINISHED",flush=True)'],
      workdir: cwd, env_name: 'fixture', description: '仅合成短作业' }), text('已启动，客户端随后断开。')]);
  let runRef = ''; let logPath = '';
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
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
      await h.wait(frame => frame.type === 'ready', 60000);
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
    await h.wait(frame => frame.type === 'ready', 60000);
    h.child.stdin.write('{"type":"shutdown"}\n{"type":"prompt","text":"ignored"}\n');
  } finally { assert.equal(await h.close('host-shutdown'), 0); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'agent_start').length, 0);
});

test('real host archival projection uses the existing SQLite writer while history reads stay disabled', async () => {
  const h = await launch([text('公开合成回答；没有科学运行。')], false,
    cwd => ({ archivePath: path.join(cwd, 'fresh-archive.sqlite') }));
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
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
    await h.wait(frame => frame.type === 'ready', 60000);
    h.child.stdin.write('not-json\n');
  } finally { assert.equal(await h.close('host-malformed'), 2); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'agent_start').length, 0);
});

test('real product top-level frame failure aborts a pending question and exits nonzero without continuation', async () => {
  const h = await launch([tool('failure-question', 'ask_user', { question: '等待真实用户输入' })]);
  h.child.stdin.on('error', () => { /* oversized frame can close the peer during a pending write */ });
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
    h.send({ type: 'prompt', requestId: 'failure-pending', text: '先询问，等待时模拟传输故障' });
    await h.wait(frame => frame.type === 'question');
    h.child.stdin.write('x'.repeat(4 * 1024 * 1024 + 1));
  } finally { assert.equal(await h.close('host-pending-frame-failure'), 2); }
  assert.equal(h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'message_start' && frame.event.message.role === 'assistant').length, 1);
  assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 0);
  assert(!h.frames.some(frame => frame.type === 'result' && frame.runtime_status === 'completed'));
});

test('real product delivers an existing file fact and an explicit missing file error without inventing download paths', async () => {
  const h = await launch(cwd => [tool('delivery-existing', 'read', { path: 'samples.tsv' }),
    tool('delivery-missing', 'read', { path: 'missing-result.tsv' }),
    text(`已读取 ${path.join(cwd, 'samples.tsv')}。missing-result.tsv 不存在，没有生成或下载该文件。`)]);
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
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
    assert.equal(calls.length, 3);
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
    await h.wait(frame => frame.type === 'ready', 60000);
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
  const h = await launch(cwd => [tool('control-run', 'run_command', { argv: ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python', '-I', '-u', '-c',
      'from pathlib import Path;import time;Path("start-count").write_text("x");print("synthetic-worker",flush=True);time.sleep(20)'],
      workdir: cwd, env_name: 'fixture', description: '合成等待作业；测试宿主随后取消' }), text('已启动合成作业，等待宿主观察')], true);
  let completed = false;
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
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
    h.send({ type: 'prompt', requestId: 'rejected-new-request', text: '[新任务] 活作业期间的新请求应明确拒绝并保留原owner' });
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

test('P06f actual host allows scoped native read; denied write pauses and prevents the following edit', async () => {
  const secret = 'P06F_READ_FIXTURE_MUST_NOT_ENTER_MODEL';
  const original = 'P06F_EDIT_ORIGINAL';
  const h = await launch(async cwd => {
    await writeFile(path.join(cwd, 'builtin-read-fixture.txt'), secret);
    await writeFile(path.join(cwd, 'builtin-edit-target.txt'), original);
    return [{ content: [
        ...tool('builtin-bash', 'bash', { command: 'printf bypass > builtin-bash-marker' }).content,
        ...tool('builtin-read', 'read', { path: path.join(cwd, 'builtin-read-fixture.txt') }).content,
        ...tool('builtin-write', 'write', { path: path.join(cwd, 'builtin-write-marker'), content: 'bypass' }).content,
        ...tool('builtin-edit', 'edit', { path: path.join(cwd, 'builtin-edit-target.txt'), oldText: original, newText: 'BYPASSED' }).content,
      ] }, text('bash 不可用；原生写入和编辑提案均被拒绝，没有修改文件。')];
  });
  try {
    await h.wait(frame => frame.type === 'ready', 60000);
    h.send({ type: 'prompt', requestId: 'p06f-builtins', text: '仅测试未注册工具会被拒绝，不允许绕过领域入口。' });
    const approval = await h.wait(frame => frame.type === 'approval' && frame.toolCallId === 'builtin-write');
    h.send({ type: 'ui_response', id: approval.id, value: 'deny' });
    const result = await h.wait(frame => frame.type === 'result');
    assert.equal(result.runtime_status, 'aborted'); assert.equal(result.scientific_success, null);
    assert(h.frames.some(frame => frame.type === 'biomind_state' && frame.data.status === 'paused' && frame.data.pauseReason === 'approval_denied'));
    const errors = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end'
      && ['bash', 'write', 'edit'].includes(frame.event.toolName));
    assert.deepEqual(errors.map(frame => frame.event.toolName), ['bash', 'write']);
    for (const row of errors) {
      if (row.event.toolName === 'bash') { assert.equal(row.event.isError, true); assert.match(JSON.stringify(row.event.result), /not found/i); }
      else { assert.equal(row.event.isError, true); assert.match(JSON.stringify(row.event.result), /MUTATION_NOT_COMPLETED: denied/); }
    }
    assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 1);
    assert(!h.frames.some(frame => frame.type === 'event' && frame.event.type === 'tool_execution_start' && frame.event.toolName === 'edit'));
    const permissions = await p06fPermissions(h.cwd);
    assert.equal(permissions.length, 1); assert(permissions.every((row: any) => row.status === 'denied'));
    const files = await readdir(h.cwd);
    assert(!files.includes('builtin-bash-marker')); assert(!files.includes('builtin-write-marker'));
    assert.equal(await readFile(path.join(h.cwd, 'builtin-edit-target.txt'), 'utf8'), original);
    const providerRequests = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(providerRequests.length, 1, 'denial must stop later tools and the next provider turn');
    const read = h.frames.find(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolName === 'read');
    assert(read); assert.equal(read.event.isError, false); assert(JSON.stringify(read.event.result).includes(secret));
    assert(!JSON.stringify(providerRequests).includes(secret), 'read receipt exists but denial stops its next provider delivery');
    await p06fFacts('host-p06f-builtins', { status: 'PASS', errors, markerFilesAbsent: true, editUnchanged: true,
      scopedReadReceiptPresent: true, laterEditNotStarted: true, permissions, providerCalls: providerRequests.length });
  } finally { assert.equal(await h.close('host-p06f-builtins'), 0); }
});

for (const decision of ['approve', 'deny'] as const) {
  test(`P06f actual host explicit memory_write ${decision} uses correlated human approval and original Wiki store`, async () => {
    const marker = `P06F_EXPLICIT_MEMORY_${decision.toUpperCase()}`;
    const filename = `explicit-${decision}.md`;
    const h = await launch([
      tool('explicit-memory-write', 'memory_write', { target: 'wiki', operation: 'create', path: 'methods/' + filename,
        content: marker + '；合成事实，科学结果未评估。' }), text('只依据本次显式记忆写入回执回答。'),
    ], false, cwd => ({ historyEnabled: true, memoryMode: 'manual', memoryRoot: path.join(cwd, 'memory'), wikiRoot: path.join(cwd, 'wiki') }));
    try {
      await h.wait(frame => frame.type === 'ready', 60000);
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
      assert.equal(result.runtime_status, decision === 'approve' ? 'completed' : 'aborted'); assert.equal(result.scientific_success, null);
      const ends = h.frames.filter(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end');
      assert.deepEqual(ends.map(frame => frame.event.toolName), ['memory_write']);
      assert.equal(ends[0].event.isError, false);
      assert.equal(ends[0].event.result.details.status, decision === 'approve' ? 'written' : 'denied');
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
      assert.equal(requests.length, decision === 'approve' ? 2 : 1);
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
      return [tool('helper-command', 'run_command', { ...(carrier === 'argv' ? { argv } : { cmd: argv.map(quote).join(' ') }),
          workdir: cwd, description: '只写合成marker的真实helper；-u和含空格路径必须保留' }),
        text('按真实helper回执说明结果，不推断科学成功。')];
    });
    try {
      await h.wait(frame => frame.type === 'ready', 60000);
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
      assert.equal(result.runtime_status, decision === 'approve' ? 'completed' : 'aborted'); assert.equal(result.scientific_success, null);
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
      assert.equal(requests.length, decision === 'approve' ? 2 : 1, 'host observation cannot request another model call; denial stops provider continuation');
      await p06fFacts(`host-p06f-helper-${carrier}-${decision}`, { status: 'PASS', carrier, decision, permissions,
        helperPath: helper, helperSource: await readFile(helper, 'utf8'), terminal: terminal ?? null,
        marker: decision === 'approve' ? await readFile(marker, 'utf8') : null, providerCalls: requests.length });
    } finally { assert.equal(await h.close(`host-p06f-helper-${carrier}-${decision}`), 0); }
  });
}

test('EXTERNAL_ACCESS local JSON host stdio resumes original scope once and exposes the same permission ledger', async () => {
  // This is local SDK JSON over the actual product stdin/stdout and Python
  // domain. Host choices are synthetic; remote clients/TUI/native RPC NOT_RUN.
  const external = await mkdtemp(path.join(tmpdir(), 'biomind-external-json-'));
  const data = path.join(external, 'Final'); await mkdir(data);
  const leaf = path.join(data, 'leaf.txt'), bud = path.join(data, 'bud.txt');
  const leafText = 'JSON_EXTERNAL_LEAF\n', budText = 'JSON_EXTERNAL_BUD\n';
  await writeFile(leaf, leafText); await writeFile(bud, budText);
  const h = await launch([
    { content: [...tool('json-leaf-stat', 'file_stat', { path: leaf }).content,
      ...tool('json-bud-stat', 'file_stat', { path: bud }).content] },
    tool('json-leaf-read', 'read', { path: leaf }),
    tool('json-bud-read', 'read', { path: bud }), text('Synthetic external input reads completed.'),
  ], false, () => ({ memoryMode: 'disabled' }));
  try {
    const ready = await h.wait(frame => frame.type === 'ready', 60000);
    assert.equal(ready.cwd, h.cwd);
    h.send({ type: 'prompt', requestId: 'external-json-input', text: 'Read these two synthetic external inputs without changing the project.' });
    const approval = await h.wait(frame => frame.type === 'approval' && frame.toolCallId === 'json-leaf-stat');
    assert.equal(approval.requestId, 'external-json-input'); assert.equal(approval.sessionId, ready.sessionId);
    assert.equal(approval.payload.payload.kind, 'file_read'); assert.equal(approval.payload.payload.scope_candidate.root, data);
    assert.equal(approval.choices[0], '拒绝'); assert.match(approval.question, /当前持久会话/);
    assert.match(approval.question, /不含 \/new 或 fork/);
    const staleReplies = [
      { sessionId: 'different-session' },
      { businessRequestId: 'different-business-request' },
      { action_hash: 'different-action' },
    ];
    for (const wrong of staleReplies) {
      const after = h.frames.length;
      h.send({ type: 'ui_response', id: approval.id, ...wrong, value: { choice: 'scope' } });
      await h.wait(frame => h.frames.indexOf(frame) >= after && frame.type === 'error' && frame.code === 'STALE_UI_RESPONSE');
      assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 1);
      assert(!h.frames.some(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolCallId === 'json-leaf-stat'));
    }
    const beforeDecision = (await readFile(path.join(h.cwd, 'provider-requests.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(beforeDecision.length, 1); assert(!JSON.stringify(beforeDecision).includes(leafText.trim()));
    h.send({ type: 'ui_response', id: approval.id, requestId: 'external-json-input',
      sessionId: approval.sessionId, businessRequestId: approval.businessRequestId, action_hash: approval.proposal.action_hash,
      value: { choice: 'scope', requestId: approval.businessRequestId, sessionId: approval.sessionId, action_hash: approval.proposal.action_hash } });
    const result = await h.wait(frame => frame.type === 'result' && frame.requestId === 'external-json-input');
    assert.equal(result.runtime_status, 'completed'); assert.equal(result.sessionId, ready.sessionId);
    assert.equal(result.extension_sha256, ready.extension_sha256);
    assert.equal(h.frames.filter(frame => frame.type === 'approval').length, 1);
    for (const [identity, expected] of [['json-leaf-read', leafText.trim()], ['json-bud-read', budText.trim()]]) {
      const read = h.frames.find(frame => frame.type === 'event' && frame.event.type === 'tool_execution_end' && frame.event.toolCallId === identity);
      assert(read); assert.equal(read.event.isError, false); assert.match(JSON.stringify(read), new RegExp(expected));
      assert.equal(read.event.result.details.write_path, undefined);
    }
    const states = h.frames.filter(frame => frame.type === 'biomind_state').map(frame => frame.data);
    assert(states.length > 0); assert(states.every(value => value.role === 'Main'));
    assert(states.every(value => value.requestId === approval.businessRequestId));
    h.send({ type: 'permissions', id: 'external-ledger', action: 'status' });
    const permissions = await h.wait(frame => frame.type === 'permissions_result' && frame.id === 'external-ledger');
    assert.equal(permissions.status, 'ok'); assert.equal(permissions.result.project_root, h.cwd);
    assert.equal(permissions.result.session_id, ready.sessionId);
    assert.equal(permissions.result.request_scope.request_key, approval.businessRequestId);
    const grants = permissions.result.grants.filter((grant: any) => grant.active);
    assert.equal(grants.length, 1); assert.equal(grants[0].root, data); assert.equal(grants[0].operation, 'file_read');
    assert.equal(grants[0].target_kind, 'directory'); assert.equal(grants[0].recursive, true);
    assert.equal(await readFile(leaf, 'utf8'), leafText); assert.equal(await readFile(bud, 'utf8'), budText);
    assert.equal(h.frames.filter(frame => frame.type === 'extension_error').length, 0);
    await p06fFacts('external-access-local-json', { status: 'PASS', layer: 'real local product SDK JSON stdin/stdout with synthetic provider/host responses',
      real_human_tui: 'NOT_RUN', remote_client: 'NOT_RUN', native_rpc: 'NOT_RUN', real_mcool: 'NOT_RUN',
      project: h.cwd, external_input_directory: data, sessionId: ready.sessionId, businessRequestId: approval.businessRequestId,
      stale_replies_rejected: staleReplies, approvals: 1, source_contents_unchanged: true, permissions, result });
  } finally { assert.equal(await h.close('external-access-local-json'), 0); }
});

// Offline wiring witnesses: answers are scripted, commands and receipts are real.
// These do not establish that a remote model naturally chooses this sequence.
async function latestFixture(cwd: string) {
  const install = path.join(cwd, 'install'), prefix = path.join(cwd, '登记 环境');
  const skills = path.join(install, 'skills'), skill = path.join(skills, 'different-directory');
  await mkdir(skill, { recursive: true }); await mkdir(path.join(prefix, 'bin'), { recursive: true });
  await writeFile(path.join(skill, 'SKILL.md'), '---\nname: native-runtime-fixture\ndescription: synthetic runtime mapping\n---\nUse the registered environment for the requested task. Explicit user QC remains part of the task.\n');
  await writeFile(path.join(skills, '.skill_runtime_registry.json'), JSON.stringify({skills:{'different-directory': {env:'fixture'}}}));
  await writeFile(path.join(cwd, 'runtime-registry.json'), JSON.stringify({environments:{fixture:{prefix,status:'failed'}},tools:{}}));
  const program = path.join(prefix, 'bin/fake-science');
  await writeFile(program, '#!/usr/bin/python3\nimport sys,json\nfrom pathlib import Path\nwith open("invocations.jsonl","a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\nif sys.argv[1:]==["--task"]:\n Path("异常 结果.bed").write_text("chr1\\t10\\t2\\n"); print("ACTUAL_TASK_RC0")\nelif sys.argv[1:]==["--fail"]:\n print("ACTUAL_FAILURE",file=sys.stderr);sys.exit(7)\nelif sys.argv[1:]==["--version"]:print("synthetic-user-requested-version")\nelse:sys.exit(91)\n');
  await chmod(program,0o755);
  // A synthetic install uses the real domain code, while Skills and registries
  // remain fixtures. No real configuration or auth directory is linked.
  for (const name of ['agent_core','execution','memory','utils','package_manager.py','session_database.py']) {
    await symlink(path.resolve(runtime,'../..',name),path.join(install,name));
  }
  return {install, prefix, program};
}
function latestConfig(cwd: string) {return {installRoot:path.join(cwd,'install'),registryPath:path.join(cwd,'runtime-registry.json')};}
const lastTool = (id:string,name:string,args:object) => ({...tool(id,name,args),resolve_last_run_ref:true});
function recordedRun(id:string,argv:string[],cwd:string) { return [
  tool(id,'run_command',{argv,workdir:cwd,env_name:'fixture'}),
  ...[0,1,2].map(i=>lastTool(id+'-wait-'+i,'wait_run',{run_ref:'from-real-receipt',max_wait_ms:30000, ...(i ? {after_cursor:'$last_cursor'} : {})})),
  lastTool(id+'-default','inspect_run',{run_ref:'from-real-receipt'}),
  lastTool(id+'-record','inspect_run',{run_ref:'from-real-receipt',record:'command'}),
  lastTool(id+'-log','read_run_log',{run_ref:'from-real-receipt'}),
]; }
async function finishSyntheticRequest(h: Awaited<ReturnType<typeof launch>>,id:string) {
  const approved=new Set<string>(); const end=performance.now()+90000;
  while(performance.now()<end) {
    for(const f of h.frames.filter(f=>f.type==='approval'&&!approved.has(f.id))) {
      approved.add(f.id);h.send({type:'ui_response',id:f.id,value:'approve'});
    }
    const result=h.frames.find(f=>f.type==='result'&&f.requestId===id);if(result)return result;
    if(h.child.exitCode!==null)throw new Error('product exited during synthetic request');
    await new Promise(resolve=>setTimeout(resolve,25));
  }
  throw new Error('synthetic request deadline: '+JSON.stringify(h.frames.slice(-3)));
}
function toolDetails(h: Awaited<ReturnType<typeof launch>>,id:string) {
  const f=h.frames.find(f=>f.type==='event'&&f.event.type==='tool_execution_end'&&f.event.toolCallId===id);
  assert(f,'missing actual tool result '+id);assert.equal(f.event.isError,false,JSON.stringify(f));
  return JSON.parse(f.event.result.content.find((p:any)=>p.type==='text').text);
}

test('LATEST E14 M01 M02 M06 M08 real biomind skill-only direct execution without implicit probes',async()=>{
  let registryBefore='';
  const h=await launch(async cwd=>{
    await latestFixture(cwd);registryBefore=await readFile(path.join(cwd,'runtime-registry.json'),'utf8');
    return [tool('skill-only','select_environment',{skill_id:'native-runtime-fixture'}),
      tool('direct-run','run_command',{argv:['fake-science','--task'],workdir:cwd}),
      ...[0,1,2].map(i=>lastTool('direct-wait-'+i,'wait_run',{run_ref:'real',max_wait_ms:30000,...(i?{after_cursor:'$last_cursor'}:{})})),
      text('合成命令已结束；没有检查科研产物，不声称科学验证通过。')];
  },false,latestConfig,undefined,true);
  try {
    const ready=await h.wait(f=>f.type==='ready',60000);
    h.send({type:'prompt',id:'latest-direct',text:'用 native-runtime-fixture 已登记环境执行一次合成任务，无需报告或独立环境检查。'});
    const result=await finishSyntheticRequest(h,'latest-direct');
    assert.equal(result.runtime_status,'completed');assert.equal(result.scientific_success,null);
    const selected=toolDetails(h,'skill-only');assert.equal(selected.selected_candidate.env,'fixture');assert.equal(selected.availability,'AVAILABLE');
    assert.equal(selected.skill_resolution.skill_id,'different-directory');
    assert.equal(toolDetails(h,'direct-wait-2').exit_code,0);
    assert.equal(toolDetails(h,'direct-wait-2').scientific_validation,'not_assessed');
    assert.equal(await readFile(path.join(h.cwd,'invocations.jsonl'),'utf8'),'["--task"]\n');
    assert.equal(await readFile(path.join(h.cwd,'runtime-registry.json'),'utf8'),registryBefore);
    assert.equal(await readFile(path.join(h.cwd,'异常 结果.bed'),'utf8'),'chr1\t10\t2\n');
    for(const name of ['report.md','reproduce.sh','stage_status.tsv','manifest.json'])await assert.rejects(readFile(path.join(h.cwd,name)),{code:'ENOENT'});
    const requests=(await readFile(path.join(h.cwd,'provider-requests.jsonl'),'utf8')).trim().split('\n').map(x=>JSON.parse(x));
    const active=JSON.stringify(requests.at(-1).context.messages.filter((m:any)=>m.role==='system'));
    assert.match(active,/适用登记直接使用/);assert.match(active,/不例行重复探测/);
    assert.match(active,/registered 不等于本次检查通过/);assert.match(active,/settled、isError:false、文件存在或管道末尾 rc 不能单独证明成功/);
    assert(JSON.stringify(requests.at(-1)).includes(selected.selected_candidate.source_ref));
    assert.equal(result.extension_sha256,ready.extension_sha256);
    assert.equal(h.frames.filter(f=>f.type==='event'&&f.event.type==='tool_execution_end'&&f.event.toolName==='run_command').length,1);
    const saved=await readFile(result.sessionFile,'utf8');assert(saved.includes(selected.selected_candidate.source_ref));
  }finally{assert.equal(await h.close('latest-direct-product'),0);}
});

test('LATEST R01 R02 R04 R05 R06 M04 real biomind records to requested report and reproduction without rerun',async()=>{
  let report='',reproduce='';
  const h=await launch(async cwd=>{
    const f=await latestFixture(cwd);const quote=(s:string)=>"'"+s.replaceAll("'","'\\''")+"'";
    report=`# 合成报告\n实际执行：fake-science --task，退出码 0；产生异常 结果.bed，未进行科学验证。\nfake-science --fail 实际退出码 7；不计为成功。\ncwd：${cwd}\n登记环境：fixture，${f.prefix}；无版本证据，未探测版本。\n修订 脚本.py 仅保存，未执行。复现脚本是依据真实命令整理版，未独立重跑；失败尝试未串入主流程。\n`;
    reproduce=`#!/bin/sh\n# 整理版：未独立重跑；输入为合成参数，输出为异常 结果.bed；不含安装或环境检查。\nset -eu\ncd -- ${quote(cwd)}\n${quote(f.program)} --task\n`;
    return [tool('report-select','select_environment',{skill_id:'native-runtime-fixture'}),
      ...recordedRun('report-good',['fake-science','--task'],cwd),...recordedRun('report-fail',['fake-science','--fail'],cwd),
      tool('report-output','read',{path:'异常 结果.bed'}),
      tool('revision','write',{path:'修订 脚本.py',content:'print("UNEXECUTED_REVISION")\n'}),
      tool('report-write','write',{path:'用户 报告.md',content:report}),
      tool('reproduce-write','write',{path:'用户 复现.sh',content:reproduce}),text('报告和复现整理已保存；部分步骤失败，修订未执行，未独立重跑。'),
      tool('only-script','write',{path:'仅 复现.sh',content:reproduce}),text('只保存复现脚本，未运行。')];
  },false,latestConfig,undefined,true);
  try{
    await h.wait(f=>f.type==='ready',60000);
    h.send({type:'prompt',id:'latest-report',text:'执行两个合成步骤：--task 和 --fail。读取真实记录，保存用户 报告.md 与用户 复现.sh；修订脚本仅保存不执行。'});
    const result=await finishSyntheticRequest(h,'latest-report');assert.equal(result.scientific_success,null);
    const legacy=toolDetails(h,'report-good-default');assert(!Object.hasOwn(legacy,'cmd'));assert(!Object.hasOwn(legacy,'execution_record'));
    const good=toolDetails(h,'report-good-record'),bad=toolDetails(h,'report-fail-record');
    assert.equal(good.exit_code,0);assert.equal(bad.exit_code,7);
    for(const [v,flag] of [[good,'--task'],[bad,'--fail']] as const){
      assert.equal(v.execution_record.status,'available');const record=JSON.parse(v.execution_record.content);
      assert.equal(record.cwd,h.cwd);assert(JSON.stringify(record).includes(flag));assert(!Object.hasOwn(record,'env'));
    }
    assert.equal(await readFile(path.join(h.cwd,'用户 报告.md'),'utf8'),report);
    assert.equal(await readFile(path.join(h.cwd,'用户 复现.sh'),'utf8'),reproduce);
    const requests=(await readFile(path.join(h.cwd,'provider-requests.jsonl'),'utf8')).trim().split('\n').map(x=>JSON.parse(x));
    const beforeWrite=requests.find((r:any)=>JSON.stringify(r.context).includes('ACTUAL_FAILURE')&&JSON.stringify(r.context).includes('execution_record'));
    assert(beforeWrite,'actual failed run and record must reach model before delivery');
    h.send({type:'prompt',id:'only-reproduce',text:'只将已知合成命令整理为仅 复现.sh，不运行，也不另生成报告。'});
    await finishSyntheticRequest(h,'only-reproduce');assert.equal(await readFile(path.join(h.cwd,'仅 复现.sh'),'utf8'),reproduce);
    assert.equal(await readFile(path.join(h.cwd,'invocations.jsonl'),'utf8'),'["--task"]\n["--fail"]\n');
    for(const name of ['report.md','stage_status.tsv','manifest.json'])await assert.rejects(readFile(path.join(h.cwd,name)),{code:'ENOENT'});
    const evidence=process.env.BIOMIND_PI_TEST_EVIDENCE;if(evidence)await writeFile(path.join(evidence,'latest-deliverables.json'),JSON.stringify({report,reproduce,invocations:['--task','--fail'],note:'scripted content checked against real receipts; no remote model and no reproduction execution'},null,2));
  }finally{assert.equal(await h.close('latest-report-product'),0);}
});
