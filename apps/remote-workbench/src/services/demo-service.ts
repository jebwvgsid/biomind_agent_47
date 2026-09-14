import { approvalFor, initialSessions, makeScript, makeSession, makeSteps, workspaces } from '../demo/data.js';
import type { Activity, ApprovalCommand, CommandReceipt, Resource, Scenario, Session, Snapshot, WorkbenchService } from '../types.js';

const clone = <T>(value: T): T => structuredClone(value);

/** Local simulation only: no fetch, subprocess, filesystem or provider calls. */
export class DemoWorkbenchService implements WorkbenchService {
  private sessions = initialSessions();
  private listeners = new Set<() => void>();
  private receipts = new Map<string, { payload: string; result: CommandReceipt }>();
  private frozen = new Map<string, Session>();
  private serial = 0;

  constructor() {
    for (const s of this.sessions) if (s.connection === 'interrupted') this.frozen.set(s.id, clone(s));
  }
  private get(id: string): Session {
    const session = this.sessions.find(s => s.id === id);
    if (!session) throw new Error('演示会话不存在');
    return session;
  }
  private notify(): void {
    // A broken display subscriber must not undo a decision or lose its receipt.
    for (const listener of this.listeners) {
      try { listener(); } catch { /* Other observers and command receipts remain usable. */ }
    }
  }
  private event(session: Session, type: string, text: string, level: Activity['level'] = 'info'): void {
    session.cursor += 1;
    session.confirmedAt = new Date().toISOString();
    session.events.push({ seq: session.cursor, time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), type, text, level });
  }
  private command(requestId: string, payload: unknown, perform: () => Omit<CommandReceipt, 'requestId'>): CommandReceipt {
    const encoded = JSON.stringify(payload);
    const prior = this.receipts.get(requestId);
    if (prior) return prior.payload === encoded ? clone(prior.result) : { requestId, status: 'rejected', message: '同一请求标识不能用于不同操作。' };
    const result = { requestId, ...perform() };
    this.receipts.set(requestId, { payload: encoded, result });
    this.notify();
    return clone(result);
  }
  listSessions(): Session[] { return this.sessions.map(s => this.snapshot(s.id).session); }
  snapshot(sessionId: string): Snapshot {
    const session = clone(this.frozen.get(sessionId) || this.get(sessionId));
    return { mode: 'demo', session, cursor: session.cursor };
  }
  subscribe(listener: () => void): () => void { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  getRequest(requestId: string): CommandReceipt | undefined { return clone(this.receipts.get(requestId)?.result); }
  createSession(serverId: string, workspaceId: string): Snapshot {
    if (!workspaces.some(w => w.id === workspaceId && w.serverId === serverId)) throw new Error('工作区不属于所选演示服务器');
    const session = makeSession('new', `demo-new-${++this.serial}`, serverId, workspaceId);
    this.sessions.unshift(session);
    this.notify();
    return this.snapshot(session.id);
  }
  sendMessage(sessionId: string, requestId: string, text: string, references: string[]): CommandReceipt {
    return this.command(requestId, { kind: 'message', sessionId, text, references }, () => {
      const s = this.get(sessionId);
      if (s.connection !== 'demo') return { status: 'rejected', message: '演示连接未就绪；草稿已保留。' };
      if (!text.trim()) return { status: 'rejected', message: '请先输入消息。' };
      if (references.some(id => !s.resources.some(r => r.id === id && r.kind === 'input'))) return { status: 'rejected', message: '文件引用不属于当前演示会话。' };
      const queued = !['idle', 'completed', 'stopped'].includes(s.status);
      s.messages.push({ id: requestId, role: 'user', text, references, time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }), delivery: queued ? 'queued' : 'accepted' });
      if (queued) {
        this.event(s, 'message.queued', '补充消息仅在演示队列中，未改变正在执行的脚本。');
        return { status: 'queued', message: '演示消息已排队，未进入当前运行；原型不会自动派发队列。' };
      }
      s.title = text.trim().split('\n')[0].slice(0, 30);
      s.status = 'awaiting_plan'; s.steps = makeSteps(); s.error = undefined; s.elapsed = 0;
      s.script = makeScript(workspaces.find(w => w.id === s.workspaceId)!.path);
      s.runId = `${s.id}-run-${++this.serial}`;
      s.approvals = [approvalFor(s.id, 'plan', this.serial)];
      s.resources.filter(r => r.kind === 'artifact' && r.status === 'verified').forEach(r => r.status = 'expected');
      s.messages.push({ id: `${requestId}-response`, role: 'assistant', time: '现在', text: '已收到你的演示消息。下面展示固定的质控示例计划，请先审核计划；脚本会在下一步单独请求执行审批。' });
      this.event(s, 'plan.proposed', '已生成演示计划，等待人工审核。');
      return { status: 'accepted', message: '演示消息已受理。' };
    });
  }
  submitApproval(command: ApprovalCommand): CommandReceipt {
    return this.command(command.requestId, command, () => {
      const s = this.get(command.sessionId);
      if (s.connection !== 'demo') return { status: 'rejected', message: '连接中断，先重连核对审批状态。' };
      const approval = s.approvals.find(a => a.id === command.approvalId);
      const expectedStatus = approval?.kind === 'plan' ? 'awaiting_plan' : 'awaiting_script';
      if (!approval || approval.version !== command.version || approval.digest !== command.digest || approval.status !== 'pending' || s.status !== expectedStatus) {
        return { status: 'rejected', message: '审批已失效或已处理，请读取当前审批。' };
      }
      approval.status = command.decision === 'approve' ? 'approved' : 'rejected';
      this.event(s, 'approval.decided', `${approval.kind === 'plan' ? '计划' : '脚本'} v${approval.version} ${approval.status === 'approved' ? '已模拟批准' : '已模拟拒绝'}。`);
      if (command.decision === 'reject') {
        s.status = 'idle';
        s.messages.push({ id: `${command.requestId}-reply`, role: 'assistant', time: '现在', text: '已记录演示拒绝，没有开始执行。你可以补充要求，重新生成计划并审核。' });
      } else if (approval.kind === 'plan') {
        s.status = 'awaiting_script';
        s.approvals.push(approvalFor(s.id, 'script', approval.version));
        this.event(s, 'approval.requested', '计划已批准；脚本执行仍需单独审批。');
      } else {
        s.status = 'running'; s.executionCount += 1;
        const next = s.steps.find(step => step.status === 'pending');
        if (next) next.status = 'running';
        this.event(s, 'tool.started', `演示 ${next?.tool || '工具'} 开始；真实命令执行次数始终为 0。`);
      }
      return { status: 'accepted', message: command.decision === 'reject' ? '已模拟拒绝，未执行命令。' : '已模拟批准，未执行真实命令。' };
    });
  }
  requestStop(sessionId: string, requestId: string): CommandReceipt {
    return this.command(requestId, { kind: 'stop', sessionId }, () => {
      const s = this.get(sessionId);
      if (s.connection !== 'demo') return { status: 'rejected', message: '当前连接中断，无法确认停止请求。' };
      if (!['running', 'recovering'].includes(s.status)) return { status: 'rejected', message: '当前演示状态不能请求停止。' };
      s.status = 'stop_requested';
      this.event(s, 'stop.requested', '演示停止请求已受理，等待执行状态回执。', 'warning');
      return { status: 'accepted', message: '已请求停止（演示），尚未确认停止。' };
    });
  }
  reconnect(sessionId: string, afterCursor: number): { snapshot: Snapshot; events: Activity[] } {
    const s = this.get(sessionId);
    if (!Number.isInteger(afterCursor) || afterCursor < 0 || afterCursor > s.cursor) throw new Error('事件游标无效，需要重新读取快照');
    s.connection = 'demo'; this.frozen.delete(sessionId);
    // A fresh snapshot includes all events through cursor; the tail is audit evidence, not re-applied to it.
    s.confirmedAt = new Date().toISOString();
    const result = { snapshot: this.snapshot(sessionId), events: clone(s.events.filter(e => e.seq > afterCursor)) };
    this.notify();
    return result;
  }
  readResource(sessionId: string, resourceId: string): Resource {
    const resource = this.get(sessionId).resources.find(r => r.id === resourceId);
    if (!resource) throw new Error('该资源不属于当前演示会话');
    if (resource.status === 'expected') throw new Error('产物尚未生成，不能预览或下载');
    return clone(resource);
  }
  /** The methods below are demo controls, deliberately absent from WorkbenchService. */
  chooseScenario(scenario: Scenario, serverId: string, workspaceId: string): Snapshot {
    const session = makeSession(scenario, `demo-scene-${++this.serial}`, serverId, workspaceId);
    this.sessions.unshift(session);
    if (session.connection === 'interrupted') this.frozen.set(session.id, clone(session));
    this.notify(); return this.snapshot(session.id);
  }
  disconnect(sessionId: string): void {
    const s = this.get(sessionId); s.connection = 'interrupted';
    this.frozen.set(sessionId, clone(s)); this.notify();
  }
  advance(sessionId: string): void {
    const s = this.get(sessionId);
    if (s.status === 'stop_requested') {
      s.status = 'stopped'; this.event(s, 'run.stopped', '演示执行者确认停止；没有终止任何服务器进程。');
    } else if (s.status === 'running') {
      s.elapsed += 48;
      const current = s.steps.findIndex(step => step.status === 'running');
      if (current >= 0) {
        s.steps[current].status = 'completed';
        this.event(s, 'tool.completed', `${s.steps[current].tool} 演示回执已收到。`);
        if (current < s.steps.length - 1) s.steps[current + 1].status = 'running';
        else {
          s.status = 'completed'; s.resources.filter(r => r.id === 'report' || r.id === 'metrics').forEach(r => { r.status = 'verified'; r.size = r.id === 'report' ? '2.4 MB · 演示' : '186 B · 演示'; });
          this.event(s, 'run.completed', '演示运行返回 completed；报告与指标表返回 verified。');
          s.messages.push({ id: `${s.runId}-complete`, role: 'assistant', time: '现在', text: '演示分析已完成，报告和指标表通过演示交付核验。Q30：94.8%，GC：43.2%。这些是合成数据，不能用于真实科学结论。' });
        }
      }
    }
    this.notify();
  }
  simulateFailure(sessionId: string): void {
    const s = this.get(sessionId); if (s.status !== 'running') return;
    s.status = 'recovering'; s.steps.find(step => step.status === 'running')!.status = 'failed';
    s.error = { node: 'Execute → Recovery', summary: '脚本执行失败，已进入恢复', detail: '演示错误：MultiQC executable not found（exit 127）。已保留输入文件和已完成步骤。', retryable: true, next: '展示恢复方案，再审核更新后的脚本。' };
    this.event(s, 'execution.failed', s.error.detail, 'error');
    this.event(s, 'recovery.started', 'Recovery 正在检查登记的工具路径（演示）。', 'warning'); this.notify();
  }
  proposeRecovery(sessionId: string): void {
    const s = this.get(sessionId); if (s.status !== 'recovering' || s.connection !== 'demo') return;
    const version = Math.max(...s.approvals.map(a => a.version)) + 1;
    s.script = s.script.replace('multiqc results/', '/demo/tools/multiqc results/');
    s.approvals.push(approvalFor(s.id, 'script', version)); s.status = 'awaiting_script';
    s.steps.forEach(step => { if (step.status === 'failed') step.status = 'pending'; });
    s.messages.push({ id: `${s.id}-recovery-${version}`, role: 'assistant', time: '现在', text: '演示恢复方案：使用已登记的 MultiQC 路径。脚本版本已更新，旧审批失效；请审核新脚本。本操作没有安装软件或重新执行任务。' });
    this.event(s, 'recovery.proposed', `恢复脚本 v${version} 等待新审批。`); this.notify();
  }
  addDemoLogs(sessionId: string): void {
    const s = this.get(sessionId);
    for (let i = 0; i < 240; i++) this.event(s, 'tool.stdout', `[演示日志 ${i + 1}/240] 质量检查记录 · sample=demo · 本行是合成输出，不读取服务器日志。`);
    this.event(s, 'future.observation', '收到原型尚不认识的观察事件，按通用活动展示。'); this.notify();
  }
}
