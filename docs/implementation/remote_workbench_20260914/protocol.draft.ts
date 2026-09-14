/** PROPOSAL ONLY — no corresponding routes are implemented in BioMind.
 * Public transport records, not TaskSpec or a replacement runtime controller.
 * IDs/identities are issued and validated by the server; the UI echoes them.
 */
export type RunStatus = 'idle' | 'running' | 'waiting_plan' | 'waiting_script' | 'recovering'
  | 'stop_requested' | 'stopped' | 'completed' | 'failed' | 'interrupted' | 'unknown';
export interface Scope { workspace_id: string; session_id: string; }
export interface Cursor { epoch: string; seq: number; }
export interface RunFact {
  run_id: string; owner_id: string; status: RunStatus;
  last_confirmed_at: string; source: string; reason?: string;
}
export interface ApprovalFact {
  approval_id: string; kind: 'plan' | 'script'; version: string;
  identity: string; // Opaque binding of authoritative lineage/environment/script identity.
  run_id: string; status: 'pending' | 'approved' | 'rejected' | 'superseded';
  review_text: string; review_complete: boolean; // Incomplete review must disable decisions.
  script_resource_id?: string;
}
export interface ResourceFact {
  resource_id: string; display_name: string; relative_path: string;
  kind: 'input' | 'artifact' | 'script' | 'log';
  status: 'available' | 'expected' | 'generated' | 'verified';
  byte_size?: number; content_type?: string; can_preview: boolean; can_download: boolean;
  verification_source?: string;
}
export interface PublicMessage {
  message_id: string; role: 'user' | 'assistant'; text: string;
  timestamp: string; resource_ids: string[];
}
export interface SessionSnapshot extends Scope {
  protocol_version: 1; revision: string; cursor: Cursor;
  title: string; run: RunFact | null; messages: PublicMessage[];
  plan: { revision: string; review_text: string; steps: { id: string; title: string; status: string }[] } | null;
  approvals: ApprovalFact[]; resources: ResourceFact[];
  capabilities: { submit: boolean; observe: boolean; stop: boolean; preview: boolean };
}
export interface PublicEvent extends Scope {
  protocol_version: 1; cursor: Cursor; event_id: string; run_id: string | null;
  type: string; timestamp: string; public_payload: Record<string, unknown>;
  category: 'fact' | 'observation';
}
export interface Receipt {
  request_id: string; status: 'accepted' | 'rejected' | 'pending' | 'unknown';
  code?: 'RUN_BUSY' | 'OWNER_BUSY' | 'APPROVAL_STALE' | 'REQUEST_CONFLICT'
    | 'SCOPE_DENIED' | 'SNAPSHOT_REQUIRED' | 'STOP_UNCONFIRMED' | 'UNSUPPORTED';
  message: string; run_id?: string; resulting_revision?: string;
  // accepted means request receipt only; never implies executed/completed/stopped.
}
export interface MessageCommand extends Scope {
  request_id: string; expected_revision: string; expected_run_id: string | null;
  text: string; resource_ids: string[];
}
export interface ApprovalCommand extends Scope {
  request_id: string; run_id: string; approval_id: string;
  kind: 'plan' | 'script'; version: string; identity: string;
  decision: 'approve' | 'reject'; feedback?: string;
}
export interface StopCommand extends Scope { request_id: string; run_id: string; reason: string; }
export interface ResourceChunk { bytes: Uint8Array; next_offset: number | null; truncated: boolean; content_type: string; }
/** Native client owns authentication and SSH. UI receives only these public values. */
export interface RemoteWorkbenchPort {
  listWorkspaces(): Promise<{ workspace_id: string; display_name: string; display_path: string }[]>;
  listSessions(workspaceId: string, pageCursor?: string): Promise<{ items: { session_id: string; title: string; run: RunFact | null }[]; next_cursor?: string }>;
  createSession(workspaceId: string, requestId: string): Promise<{ receipt: Receipt; session_id?: string }>;
  snapshot(scope: Scope): Promise<SessionSnapshot>;
  events(scope: Scope, after: Cursor, signal: AbortSignal): AsyncIterable<PublicEvent>;
  send(command: MessageCommand): Promise<Receipt>;
  decide(command: ApprovalCommand): Promise<Receipt>;
  stop(command: StopCommand): Promise<Receipt>;
  receipt(requestId: string): Promise<Receipt>;
  resources(scope: Scope): Promise<ResourceFact[]>;
  content(scope: Scope, resourceId: string, offset: number, limit: number): Promise<ResourceChunk>;
}
