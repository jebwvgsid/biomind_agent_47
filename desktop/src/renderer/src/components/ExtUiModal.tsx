import { useEffect, useState } from "react";
import { useStore } from "../store";
import type { ExtUiRequest } from "../lib/types";

/**
 * Renders the head of the extension-UI dialog queue (select / confirm / input /
 * editor). Fire-and-forget methods (notify / set_editor_text / setTitle) are
 * handled directly in the store and never reach this queue.
 */
export function ExtUiModal() {
  const queue = useStore((s) => s.extuiQueue);
  const activeThreadId = useStore((s) => s.activeThreadId);
  const respond = useStore((s) => s.respondExtUi);
  const item = queue.find((q) => q.threadId === activeThreadId && q.request.method !== "confirm" && q.request.method !== "select");

  if (!item) return null;
  return <Dialog key={`${item.threadId}:${item.request.id}`} threadId={item.threadId} req={item.request} respond={respond} />;
}

function Dialog({
  threadId,
  req,
  respond,
}: {
  threadId: string;
  req: ExtUiRequest;
  respond: (threadId: string, id: string, payload: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(req.prefill || "");
  const method = req.method;
  const language = useStore((s) => s.config?.language || "en");

  // Expiration and authorization remain authoritative on the BioMind server.
  const cancel = () => respond(threadId, req.id, { cancelled: true });

  return (
    <div className="modal-backdrop" onMouseDown={cancel}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-title">{req.title || (language === "zh" ? "BioMind 请求" : "BioMind request")}</div>
        {req.message && <div className="modal-msg">{req.message}</div>}

        {method === "select" && (
          <div className="modal-options">
            {(req.options || []).map((o) => (
              <button key={o} className="modal-option" onClick={() => respond(threadId, req.id, { value: o })}>
                {o}
              </button>
            ))}
          </div>
        )}

        {method === "confirm" && (
          <div className="modal-actions">
            <button className="btn" onClick={() => respond(threadId, req.id, { confirmed: false })}>
              {language === "zh" ? "否" : "No"}
            </button>
            <button className="btn primary" onClick={() => respond(threadId, req.id, { confirmed: true })}>
              {language === "zh" ? "是" : "Yes"}
            </button>
          </div>
        )}

        {method === "input" && (
          <>
            <input
              className="modal-input"
              autoFocus
              placeholder={req.placeholder || ""}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") respond(threadId, req.id, { value: text });
                if (e.key === "Escape") cancel();
              }}
            />
            <div className="modal-actions">
              <button className="btn" onClick={cancel}>
                {language === "zh" ? "取消" : "Cancel"}
              </button>
              <button className="btn primary" onClick={() => respond(threadId, req.id, { value: text })}>
                {language === "zh" ? "确定" : "OK"}
              </button>
            </div>
          </>
        )}

        {method === "editor" && (
          <>
            <textarea
              className="modal-input"
              autoFocus
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") cancel();
              }}
            />
            <div className="modal-actions">
              <button className="btn" onClick={cancel}>
                {language === "zh" ? "取消" : "Cancel"}
              </button>
              <button className="btn primary" onClick={() => respond(threadId, req.id, { value: text })}>
                {language === "zh" ? "保存" : "Save"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
