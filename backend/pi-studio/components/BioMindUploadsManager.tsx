"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "@/hooks/useI18n";
import { DraggableResizableModal } from "./DraggableResizableModal";

interface Scope { serverId: string; project: string }
interface UploadEntry { name: string; path: string; size: number; mtimeMs: number }
interface UploadStats extends Scope { totalBytes: number; maxBytes: number; files: UploadEntry[]; dir: string }
const bytes = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;

/** Uses the existing project-scoped upload API; never changes storage or evicts references. */
export function BioMindUploadsManager({ scope, onClose, onOpenFile }: {
  scope: Scope; onClose: () => void; onOpenFile?: (path: string, name: string) => void;
}) {
  const { t } = useI18n();
  const { serverId, project } = scope;
  const [stats, setStats] = useState<UploadStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<{ uploaded: string[]; errors: Array<{ name: string; error: string }> } | null>(null);
  const generation = useRef({ value: 0 });
  const readGeneration = useRef(0);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const pending = useRef(new Set<AbortController>());

  const current = useCallback((captured: Scope, version: number) => generation.current.value === version
    && activeScope.current.serverId === captured.serverId && activeScope.current.project === captured.project, []);
  const refresh = useCallback(async () => {
    const version = generation.current.value, read = ++readGeneration.current, captured = { serverId, project };
    const controller = new AbortController(); pending.current.add(controller);
    const timer = setTimeout(() => controller.abort(), 20000);
    setLoading(true); setError(null);
    try {
      const response = await fetch(`/api/uploads?cwd=${encodeURIComponent(captured.project)}`, { signal: controller.signal, cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (data.project !== captured.project || data.serverId !== captured.serverId || !Array.isArray(data.files)) throw new Error(t("uploads.scopeMismatch"));
      if (current(captured, version) && readGeneration.current === read) setStats(data);
    } catch (cause) {
      if (current(captured, version) && readGeneration.current === read) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      clearTimeout(timer); pending.current.delete(controller);
      if (current(captured, version) && readGeneration.current === read) setLoading(false);
    }
  }, [serverId, project, current, t]);

  useEffect(() => {
    const controllers = pending.current;
    const lifetime = generation.current;
    lifetime.value++; setStats(null); setReceipt(null); setUploading(false);
    void refresh();
    return () => { lifetime.value++; for (const controller of controllers) controller.abort(); controllers.clear(); };
  }, [refresh]);

  const upload = async (files: File[]) => {
    if (!files.length || uploading) return;
    const captured = { ...scope }, version = generation.current.value;
    const form = new FormData(); form.set("cwd", captured.project);
    for (const file of files) form.append("files", file);
    const controller = new AbortController(); pending.current.add(controller);
    const timer = setTimeout(() => controller.abort(), 60000);
    setUploading(true); setError(null); setReceipt(null);
    try {
      const response = await fetch("/api/uploads", { method: "POST", body: form, signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (data.project !== captured.project || data.serverId !== captured.serverId || !Array.isArray(data.uploaded) || !Array.isArray(data.errors)) throw new Error(t("uploads.scopeMismatch"));
      if (!current(captured, version)) return;
      // HTTP 207 is a partial receipt: display both committed names and rejected names.
      setReceipt({ uploaded: data.uploaded.map((entry: UploadEntry) => entry.name), errors: data.errors });
      await refresh();
    } catch (cause) {
      if (current(captured, version)) setError(`${t("uploads.outcomeUnknown")} ${cause instanceof Error ? cause.message : String(cause)}`);
    } finally {
      clearTimeout(timer); pending.current.delete(controller);
      if (current(captured, version)) setUploading(false);
    }
  };
  const button = { padding: "4px 8px", fontSize: 12, color: "var(--text)", background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)" } as const;
  return <DraggableResizableModal title={t("uploads.title")} hint={t("uploads.remoteScope")} onClose={onClose} width={720} height={560}>
    <section style={{ display: "flex", flexDirection: "column", gap: 12, padding: 16, height: "100%", minHeight: 0 }} data-testid="biomind-uploads" data-server-id={scope.serverId} data-project={scope.project}>
      <div style={{ fontSize: 12, overflowWrap: "anywhere", color: "var(--text-muted)" }}><div>{scope.serverId}</div><code>{scope.project}</code><p>{t("uploads.remotePolicy")}</p></div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <label style={button}>{t("uploads.selectFiles")}<input aria-label={t("uploads.selectFiles")} type="file" multiple disabled={uploading} onChange={event => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ""; void upload(files); }} /></label>
        <button type="button" style={button} disabled={loading} onClick={() => void refresh()}>{t("uploads.refresh")}</button>
        {stats && <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{bytes(stats.totalBytes)} / {bytes(stats.maxBytes)}</span>}
      </div>
      {loading && <p role="status">{t("uploads.loading")}</p>}
      {error && <p role="alert" style={{ color: "var(--text)", fontSize: 12 }}>{error}</p>}
      {receipt && <div role="status" style={{ fontSize: 12 }}><strong>{t("uploads.partialResult")}</strong><ul>{receipt.uploaded.map(name => <li key={`ok:${name}`}>{name} — {t("uploads.uploaded")}</li>)}{receipt.errors.map((entry, index) => <li key={`error:${index}`}>{entry.name}: {entry.error}</li>)}</ul></div>}
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {stats?.files.length === 0 && <p>{t("uploads.empty")}</p>}
        {stats?.files.map(file => <div key={file.name} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
          <div style={{ flex: 1, minWidth: 0, fontSize: 12, overflowWrap: "anywhere" }}><div>{file.name}</div><span style={{ color: "var(--text-muted)" }}>{bytes(file.size)} · {new Date(file.mtimeMs).toLocaleString()}</span></div>
          {onOpenFile && <button type="button" style={button} onClick={() => onOpenFile(file.path, file.name)}>{t("uploads.preview")}</button>}
          <a style={button} href={`/api/uploads?cwd=${encodeURIComponent(scope.project)}&download=${encodeURIComponent(file.name)}`} download={file.name}>{t("uploads.download")}</a>
        </div>)}
      </div>
    </section>
  </DraggableResizableModal>;
}
