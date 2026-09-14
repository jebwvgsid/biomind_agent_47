/** View data only. These are proposed adapter types, not existing BioMind APIs. */
export type Connection = 'demo' | 'disconnected' | 'interrupted';
export type RunStatus = 'idle' | 'running' | 'awaiting_plan' | 'awaiting_script' | 'recovering' | 'stop_requested' | 'stopped' | 'completed';
export type Scenario = 'disconnected' | 'new' | 'running' | 'plan' | 'approval' | 'recovery' | 'offline' | 'completed';
export type Panel = 'plan' | 'script' | 'logs' | 'files';
export interface Server { id: string; name: string; alias: string; description: string; }
export interface Workspace { id: string; serverId: string; name: string; path: string; }
export interface Message { id: string; role: 'user' | 'assistant'; text: string; time: string; delivery?: 'accepted' | 'queued'; references?: string[]; }
export interface Step { title: string; detail: string; tool: string; status: 'pending' | 'running' | 'completed' | 'failed'; }
export interface Approval { id: string; kind: 'plan' | 'script'; version: number; digest: string; status: 'pending' | 'approved' | 'rejected'; }
export interface Resource { id: string; name: string; path: string; kind: 'input' | 'artifact'; format: string; size: string; status: 'available' | 'expected' | 'unverified' | 'verified'; preview: string; }
export interface Activity { seq: number; time: string; type: string; text: string; level: 'info' | 'warning' | 'error'; }
export interface Session {
  id: string; runId: string; title: string; serverId: string; workspaceId: string;
  status: RunStatus; connection: Connection; confirmedAt: string; cursor: number;
  messages: Message[]; steps: Step[]; approvals: Approval[]; script: string;
  events: Activity[]; resources: Resource[]; executionCount: number; elapsed: number;
  error?: { node: string; summary: string; detail: string; retryable: boolean; next: string; };
}
export interface Snapshot { session: Session; cursor: number; mode: 'demo'; }
export interface CommandReceipt { requestId: string; status: 'accepted' | 'queued' | 'rejected'; message: string; }
export interface ApprovalCommand { requestId: string; sessionId: string; approvalId: string; version: number; digest: string; decision: 'approve' | 'reject'; }
/** Replace this port in stage B. Components never import the BioMind runtime. */
export interface WorkbenchService {
  listSessions(): Session[];
  snapshot(sessionId: string): Snapshot;
  createSession(serverId: string, workspaceId: string): Snapshot;
  sendMessage(sessionId: string, requestId: string, text: string, references: string[]): CommandReceipt;
  submitApproval(command: ApprovalCommand): CommandReceipt;
  requestStop(sessionId: string, requestId: string): CommandReceipt;
  reconnect(sessionId: string, afterCursor: number): { snapshot: Snapshot; events: Activity[] };
  getRequest(requestId: string): CommandReceipt | undefined;
  readResource(sessionId: string, resourceId: string): Resource;
  subscribe(listener: () => void): () => void;
}
