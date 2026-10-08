/** Real new-request isolation uses Pi's native boundary drafts, never role handoffs. */
import { randomUUID } from 'node:crypto';
import type { SessionBoundaryDraft, SessionEntry } from '@earendil-works/pi-coding-agent';
const object = (x: any): Record<string, any> => x && typeof x === 'object' && !Array.isArray(x) ? x : {};
export interface StageBoundary {
  id: string; kind: 'task' | 'handoff'; targets: string[]; checkpoint: boolean;
  /** Legacy data stays readable but is never activated or repaired. */
  handoff?: { content: string; details: Record<string, unknown> };
  committed?: boolean;
  activated?: boolean;
}
export function planStageBoundary(branch: readonly SessionEntry[], kind: 'task' = 'task'): StageBoundary {
  return { id: randomUUID(), kind, targets: branch.filter(e => e.type === 'custom_message'
    || e.type === 'message' && ['user', 'assistant', 'toolResult'].includes(e.message.role)).map(e => e.id),
    checkpoint: branch.some(e => e.type === 'compaction' || e.type === 'branch_summary') };
}
export function boundaryDrafts(branch: readonly SessionEntry[], boundary: StageBoundary): SessionBoundaryDraft[] {
  if (boundary.kind !== 'task') return [];
  const edits = new Set(branch.flatMap(e => e.type === 'context_edit' && e.replacement === null ? [e.targetId] : []));
  const intent = branch.findIndex(e => e.type === 'custom' && object(e.data).boundary?.id === boundary.id);
  const activated = branch.findIndex(e => e.type === 'custom' && object(e.data).boundary?.id === boundary.id && object(e.data).boundary?.activated);
  // Repair only interrupted new-request commits. Valid queued revisions remain.
  const interruptedErrors = intent < 0 ? [] : branch.slice(intent + 1, activated < 0 ? undefined : activated).flatMap(e =>
    e.type === 'message' && e.message.role === 'assistant' && e.message.stopReason === 'error' && !e.message.content.some(p => p.type === 'toolCall') ? [e.id] : []);
  const drafts: SessionBoundaryDraft[] = [...new Set([...boundary.targets, ...interruptedErrors])].filter(id => !edits.has(id)).map(targetId => ({ type: 'context_edit', targetId, replacement: null }));
  if (boundary.checkpoint && !branch.some(e => e.type === 'compaction' && object(e.details).biomindBoundary === boundary.id)) {
    drafts.push({ type: 'compaction', firstKeptEntryId: null,
      summary: '新请求上下文检查点（宿主生成，非模型语义总结）。先前任务原始记录保留，当前工作仅依据新请求。',
      details: { biomindBoundary: boundary.id, kind: 'task', semanticSummary: false } });
  }
  return drafts;
}
export function assertStageBoundary(branch: readonly SessionEntry[], boundary?: StageBoundary) {
  // Pre-boundary legacy requests retain their existing owner and history.
  if (!boundary || boundary.kind !== 'task') return;
  if (!boundary.committed || boundaryDrafts(branch, boundary).length) throw new Error('STAGE_BOUNDARY_INCOMPLETE');
}
