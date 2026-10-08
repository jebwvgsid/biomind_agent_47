/** Bounded native tool presentation. Pi still owns transcript/history and Python owns full logs. */
import { truncateToVisualLines, type ToolDefinition } from '@earendil-works/pi-coding-agent';

export const TOOL_DISPLAY_LIMITS = Object.freeze({ textBytes: 8192, logicalLines: 48,
  renderedLines: 32, renderedBytes: 65536, width: 512, cachedWidths: 1, previewNodes: 256 });
const OMITTED = '… [显示已截取；完整回执保留在会话中，日志见 log_refs / read_run_log 游标分页]';
interface DisplayPressure { renderCount: number; textBytes: number; logicalLines: number; renderedLines: number;
  renderedBytes: number; cachedWidths: number; width: number }
const pressureKey = Symbol.for('biomind.pi.tool-display-pressure.v1');
const pressureHost = globalThis as typeof globalThis & { [pressureKey]?: DisplayPressure };
const pressure = () => pressureHost[pressureKey] ??= { renderCount: 0, textBytes: 0, logicalLines: 0,
  renderedLines: 0, renderedBytes: 0, cachedWidths: 0, width: 0 };
/** Scalar high-water marks only, shared across Pi's resource reload module instances. */
export function getDisplayPressure(): DisplayPressure { return { ...pressure() }; }
const PRIORITY = ['status', 'error', 'reason', 'phase', 'execution_started', 'receipt_ref', 'run_ref', 'count',
  'host_boundary', 'affected_capability', 'run_status', 'terminal', 'exit_code', 'returncode',
  'cancel_requested', 'stop_requested', 'task_cancelled', 'cleanup_confirmed', 'approval_id',
  'request_id', 'action_hash', 'request_scope', 'allowed_decisions', 'log_refs',
  'next_cursor', 'delta_byte_ranges', 'unread_log_bytes'];

/** UTF-8 prefix, never split a code point; terminal controls are data, not instructions. */
export function boundedDisplayText(input: string, bytes: number = TOOL_DISPLAY_LIMITS.textBytes,
  lines: number = TOOL_DISPLAY_LIMITS.logicalLines): string {
  const suffixBytes = Buffer.byteLength(OMITTED) + 1;
  let out = '', used = 0, count = 1, truncated = false;
  for (const character of input) {
    const safe = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/u.test(character) ? '' : character;
    const size = Buffer.byteLength(safe);
    if (used + size > Math.max(0, bytes - suffixBytes) || safe === '\n' && count >= lines - 1) { truncated = true; break; }
    out += safe; used += size; if (safe === '\n') count++;
  }
  return truncated ? out + '\n' + OMITTED : out;
}

/** Inspect only a bounded prefix of objects. Never stringify an entire tool argument/log first. */
export function displayPreview(value: unknown): string {
  let remaining = TOOL_DISPLAY_LIMITS.textBytes, nodes = 0;
  const pieces: string[] = [];
  const append = (text: string) => {
    if (remaining <= 0) return;
    const part = boundedDisplayText(text, Math.max(256, remaining));
    // Keep traversal allocation bounded even for large strings or arrays.
    pieces.push(part); remaining -= Buffer.byteLength(part);
  };
  const visit = (item: unknown, depth: number) => {
    if (remaining <= 0 || ++nodes > TOOL_DISPLAY_LIMITS.previewNodes) return;
    if (typeof item === 'string') { append(item); return; }
    if (item === null || typeof item !== 'object') { append(String(item)); return; }
    if (depth > 5) { append('[…]'); return; }
    if (Array.isArray(item)) {
      for (let index = 0; index < item.length && remaining > 0 && nodes < TOOL_DISPLAY_LIMITS.previewNodes; index++) {
        append(index ? ', ' : '['); visit(item[index], depth + 1);
      }
      append(']'); return;
    }
    const object = item as Record<string, unknown>;
    // Facts come first. In particular log text must not push cancellation/exit or log locations out.
    for (const key of PRIORITY) if (Object.hasOwn(object, key) && remaining > 0) {
      append(key + ': '); visit(object[key], depth + 1); append('\n');
    }
    for (const key in object) {
      if (!Object.hasOwn(object, key) || PRIORITY.includes(key)) continue;
      if (remaining <= 0 || nodes >= TOOL_DISPLAY_LIMITS.previewNodes) break;
      append(key + ': '); visit(object[key], depth + 1); append('\n');
    }
  };
  visit(value, 0);
  return boundedDisplayText(pieces.join(''));
}

/** Only one bounded input and one width's bounded rows are retained by each component. */
export class BoundedToolView {
  #text: string;
  #width = -1;
  #rows: string[] = [];
  constructor(text: string) { this.#text = boundedDisplayText(text); }
  setText(text: string) {
    const next = boundedDisplayText(text);
    if (next !== this.#text) { this.#text = next; this.invalidate(); }
    return this;
  }
  invalidate() { this.#width = -1; this.#rows = []; }
  render(width: number): string[] {
    const effective = Math.max(1, Math.min(TOOL_DISPLAY_LIMITS.width, Math.floor(width)));
    if (effective === this.#width) return this.#rows;
    const rows: string[] = []; let bytes = 0, omitted = false;
    for (const line of this.#text.split('\n')) {
      // The temporary upstream Text sees at most 8 KiB, never the original receipt.
      const wrapped = truncateToVisualLines(line || ' ', TOOL_DISPLAY_LIMITS.textBytes, effective, 0).visualLines;
      for (const row of wrapped) {
        if (rows.length >= TOOL_DISPLAY_LIMITS.renderedLines - 1 || bytes + Buffer.byteLength(row) > TOOL_DISPLAY_LIMITS.renderedBytes - 1024) { omitted = true; break; }
        rows.push(row); bytes += Buffer.byteLength(row);
      }
      if (omitted) break;
    }
    if (omitted) rows.push(truncateToVisualLines('… [显示截取；日志见 log_refs]', 1, effective, 0).visualLines[0] ?? '');
    this.#rows = rows; this.#width = effective;
    const counters = pressure(), metrics = this.getMetrics();
    counters.renderCount++; counters.width = effective;
    for (const key of ['textBytes', 'logicalLines', 'renderedLines', 'renderedBytes', 'cachedWidths'] as const) {
      counters[key] = Math.max(counters[key], metrics[key]);
    }
    return rows;
  }
  /** Counts only; useful for native pressure diagnostics without retaining transcript data. */
  getMetrics() { return { textBytes: Buffer.byteLength(this.#text), logicalLines: this.#text.split('\n').length,
    renderedLines: this.#rows.length, renderedBytes: this.#rows.reduce((n, row) => n + Buffer.byteLength(row), 0),
    cachedWidths: this.#width < 0 ? 0 : 1, width: this.#width }; }
}

function view(text: string, previous: unknown) {
  return previous instanceof BoundedToolView ? previous.setText(text) : new BoundedToolView(text);
}
const TOOL_LABELS: Record<string, string> = {
  ask_user: '需要你的回复', handoff_generate: '进入执行', update_plan: '更新执行笔记',
  select_environment: '选择软件环境',
  run_command: '执行命令', probe_command: '只读核查', install_tool: '安装提案', inspect_run: '查看作业',
  wait_run: '等待作业', read_run_log: '读取作业日志', cancel_run: '取消指定作业',
  file_stat: '查看文件', read: '读取文件', write_file: '写入文件',
  memory_search: '检索记忆', memory_read: '读取记忆', memory_write: '更新记忆', maintenance_submit: '提交记忆维护',
};
/** Display copies only: status/exit are receipts, never scientific success. */
export function toolDisplayTitle(name: string, details?: unknown, partial = false): string {
  const label = TOOL_LABELS[name] ?? name;
  if (partial) return `${label} · 处理中`;
  if (!details || typeof details !== 'object') return label;
  const data = details as Record<string, any>;
  const facts = data.execution_facts && typeof data.execution_facts === 'object' ? data.execution_facts : data;
  const status = String(facts.status ?? data.status ?? '');
  const states: Record<string, string> = { waiting_approval: '等待你确认', running: '运行中',
    completed: '执行已结束', succeeded: '执行已结束', failed: '执行失败', error: '错误',
    cancelled: '已取消', denied: '已拒绝', pending: '等待中', stopped: '已停止',
    stage_blocked: '执行被阻塞', not_run: '未执行' };
  const code = facts.exit_code ?? facts.returncode;
  const shown = status === 'stopped' || status === 'cancelled' && facts.run_ref
    ? facts.terminal === true && facts.cleanup_confirmed === true ? '已停止' : '已请求停止；终态待确认' : states[status] ?? status;
  return [label, shown, code !== undefined && code !== null ? `退出码 ${code}` : ''].filter(Boolean).join(' · ');
}

/** A compact diagnostic has a reserved receipt line; long logs cannot hide it.
 * Absent launch facts stay unknown. True means wrapper start, never scientific exec.
 */
export function blockedDisplay(details: Record<string, any>): string | undefined {
  const facts = details.execution_facts && typeof details.execution_facts === 'object'
    ? details.execution_facts : details;
  if (facts.status !== 'stage_blocked' && facts.status !== 'not_run') return undefined;
  const short = (value: unknown, limit = 220) => boundedDisplayText(String(value), 4096, 2).split('\n')[0].slice(0, limit);
  const knownPrelaunch = facts.execution_started === false && facts.host_boundary === 'before_launch_delegation';
  const launch = knownPrelaunch ? '本次目标命令未启动' : facts.execution_started === true
    ? '执行包装进程已启动；业务 exec 未确认' : Object.hasOwn(facts, 'execution_started')
      ? `execution_started=${short(facts.execution_started)}` : '启动事实：unknown';
  return [
    ...(facts.reason === 'writable_workdir_outside_project' ? ['历史回执：工作目录与当前会话项目不匹配。当前版本支持外部 cwd 范围申请；核验版本后以新动作申请，项目归属保留。'] : []),
    `原因：${Object.hasOwn(facts, 'reason') ? short(facts.reason) : 'unknown'}`,
    ...(typeof facts.current_project_root === 'string' ? [`当前项目：${short(facts.current_project_root, 1024)}`] : []),
    ...(typeof facts.requested_workdir === 'string' ? [`请求工作目录：${short(facts.requested_workdir, 1024)}`] : []),
    `错误：${Object.hasOwn(facts, 'error') ? short(facts.error) : 'unknown'}`,
    `阶段：${Object.hasOwn(facts, 'phase') ? short(facts.phase) : 'unknown'}${typeof facts.protection_phase === 'string' ? ' / ' + short(facts.protection_phase) : ''}`,
    `${launch}；exit_code=${Object.hasOwn(facts, 'exit_code') ? short(facts.exit_code) : 'unknown'}`,
    `原始回执：${Object.hasOwn(facts, 'receipt_ref') ? short(facts.receipt_ref, 700) : 'unknown'}`,
    ...(typeof facts.count === 'number' ? [`扫描命中：${facts.count} 个路径条目`] : []),
    ...(typeof facts.run_ref === 'string' ? [`run_ref：${short(facts.run_ref)}`] : []),
    ...(typeof facts.instruction === 'string' ? [`处理方式：${short(facts.instruction, 1024)}`] : []),
  ].join('\n');
}
export function toolRenderers(name: string): Pick<ToolDefinition, 'renderCall' | 'renderResult'> {
  return {
    renderCall(args, _theme, context) {
      const text = displayPreview(args);
      return view(toolDisplayTitle(name) + '\n' + (context.expanded ? text : boundedDisplayText(text, 2048, 8)), context.lastComponent);
    },
    renderResult(result, options, _theme, context) {
      const details = result.details && typeof result.details === 'object' ? result.details : undefined;
      // Error receipts may carry details={} while their actual error is in content.
      const error = context.isError;
      const text = error || !details || !Object.keys(details).length ? displayPreview(result.content) : displayPreview(details);
      const title = error && !options.isPartial ? `${TOOL_LABELS[name] ?? name} · 调用失败` : toolDisplayTitle(name, details, options.isPartial);
      const blocked = !error && details ? blockedDisplay(details) : undefined;
      return view(title + '\n' +
        (options.expanded ? text : blocked ?? boundedDisplayText(text, 2048, 8)), context.lastComponent);
    },
  };
}

/** Do not duplicate each log page in both content and details of persisted Pi entries. */
export function projectToolReceipt(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const object = value as Record<string, unknown>;
  if (typeof object.next_cursor !== 'string' || !object.log_refs || typeof object.log_refs !== 'object') return value;
  const refs = object.log_refs as Record<string, unknown>;
  const result = { ...object };
  for (const name of ['stdout', 'stderr']) if (typeof refs[name] === 'string' && typeof object[name + '_delta'] === 'string') {
    // Cursor pages remain byte-for-byte intact. Drop only the redundant cumulative
    // head/tail copy, which is not a page and would be reinjected on every observation.
    delete result[name];
  }
  return result;
}

export function receiptDetails(value: Record<string, unknown>): Record<string, unknown> {
  const result = { ...value };
  for (const key of ['stdout', 'stderr', 'stdout_delta', 'stderr_delta']) if (typeof result[key] === 'string') {
    result[key + '_display'] = boundedDisplayText(result[key] as string, 2048, 12);
    delete result[key];
  }
  return result;
}
