"use client";

import { useEffect, useState } from "react";
import { encodeFilePathForApi } from "@/lib/file-paths";
import { useI18n } from "@/hooks/useI18n";

/** Remote read-only preview; never mounts Studio's watchers/Office gateways. */
export function RemoteFilePreview({ filePath }: { filePath: string }) {
  const { locale } = useI18n();
  const zh = locale.startsWith("zh");
  const base = `/api/files/${encodeFilePathForApi(filePath)}`;
  const [state, setState] = useState<{ path: string; text?: string; image?: boolean; notice?: string }>({ path: filePath });
  useEffect(() => {
    const abort = new AbortController(); setState({ path: filePath });
    void (async () => {
      const metaResponse = await fetch(base + "?type=meta", { signal: abort.signal });
      if (!metaResponse.ok) throw new Error();
      const meta = await metaResponse.json() as { mime?: string };
      if (["image/png", "image/jpeg"].includes(meta.mime ?? "")) {
        if (!abort.signal.aborted) setState({ path: filePath, image: true }); return;
      }
      if (meta.mime !== "text/plain" || /\.(?:xlsx?|xlsm|univer|pptx?|docx?|pdf)$/i.test(filePath)) {
        if (!abort.signal.aborted) setState({ path: filePath, notice: zh ? "此格式请下载查看。编辑操作由服务器 BioMind 会话处理。" : "Download this format to view it. Ask the server BioMind session to edit it." }); return;
      }
      const response = await fetch(base + "?type=read", { signal: abort.signal });
      if (!response.ok) throw new Error();
      const data = await response.json() as { content?: string; truncated?: boolean };
      if (!abort.signal.aborted) setState({ path: filePath, text: data.content ?? "", notice: data.truncated ? (zh ? "仅显示文件开头，可下载完整文件。" : "Showing the beginning; download the complete file.") : undefined });
    })().catch(() => { if (!abort.signal.aborted) setState({ path: filePath, notice: zh ? "无法预览，请核对服务器文件权限或尝试下载。" : "Preview unavailable. Check server file permissions or download it." }); });
    return () => abort.abort();
  }, [base, filePath, zh]);
  const current = state.path === filePath ? state : { path: filePath };
  return <div style={{ height: "100%", overflow: "auto", padding: 16, color: "var(--text)" }}>
    <p style={{ color: "var(--text-muted)", fontSize: 12 }}>{zh ? "服务器文件 · 只读预览" : "Server file · Read-only preview"} · <a href={base + "?type=download"} download>{zh ? "下载" : "Download"}</a></p>
    {current.notice && <p role="status">{current.notice}</p>}
    {current.image && <img src={base + "?type=read"} alt={filePath} style={{ maxWidth: "100%", height: "auto" }} />}
    {current.text !== undefined && <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "var(--font-mono)", fontSize: 12 }}>{current.text}</pre>}
  </div>;
}
