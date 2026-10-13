/** Browser receipt projection; partial uploads keep successful attachments. */
export interface UploadsResponse {
  project?: string;
  serverId?: string;
  uploaded?: Array<{ name: string; path: string; size: number; kind: string; inputIndex?: number; sourceName?: string }>;
  errors?: Array<{ name: string; error: string }>;
  error?: string;
}

export function readUploadReceipt(status: number, data: UploadsResponse, expectedProject?: string, expectedServer?: string) {
  if (status < 200 || status >= 300) throw new Error(data.error ?? `Upload failed (HTTP ${status})`);
  if (expectedServer !== undefined && data.serverId !== expectedServer) throw new Error('UPLOAD_SCOPE_MISMATCH');
  if (expectedProject !== undefined && data.project !== expectedProject) throw new Error('UPLOAD_SCOPE_MISMATCH');
  if (!Array.isArray(data.uploaded) || data.uploaded.some(entry => !entry || typeof entry.name !== 'string' || !entry.name || typeof entry.path !== 'string' || !entry.path)) {
    throw new Error('UPLOAD_RECEIPT_INVALID');
  }
  const error = data.errors?.length ? data.errors.map(entry => `${entry.name}: ${entry.error}`).join(' · ') : null;
  return { uploaded: data.uploaded, error };
}
