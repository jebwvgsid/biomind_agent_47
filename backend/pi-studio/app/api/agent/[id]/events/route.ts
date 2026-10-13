import { resolveSessionPath } from "@/lib/session-reader";
import { getRpcSession, startRpcSession, type AgentEvent } from "@/lib/rpc-manager";
import { isBioMindRemoteProfile } from "@/lib/biomind-profile";
import { isBioMindProjectAllowed } from "@/lib/remote-security";
import { boundedEventStream } from "@/lib/bounded-event-stream";

export const dynamic = "force-dynamic";

const OMITTED_EVENT_TYPES = new Set(["turn_start", "turn_end", "tool_execution_update"]);

/**
 * v1（默认）：裁剪流，投喂旧 ChatWindow/useAgentSession（消息快照模型）。
 * v2（?v2=1）：全量透传，投喂 percho Transcript reducer（平滑流式/折叠组/错误卡/todo 面板）。
 * 通过 query 切换，两条路径并存，互不干扰；旧 UI 行为完全不变。
 */
function toClientEvent(event: AgentEvent, v2 = false): AgentEvent | null {
  if (v2) return event; // 全量透传：保留 turn_start/turn_end/tool_execution_update/assistantMessageEvent 等增量事件
  if (OMITTED_EVENT_TYPES.has(event.type)) return null;
  if (event.type === "message_update") {
    const clientEvent = { ...event };
    delete clientEvent.assistantMessageEvent;
    return clientEvent;
  }
  if (event.type === "agent_end") return { type: "agent_end" };
  return event;
}

// GET /api/agent/[id]/events - SSE stream of agent events
export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const url = new URL(req.url);
  const v2 = url.searchParams.get("v2") === "1";

  // Fast path: already-running session
  let session = getRpcSession(id);
  if (!session || !session.isAlive()) {
    if (isBioMindRemoteProfile()) return Response.json({ error: "HISTORY_READ_ONLY", code: "HISTORY_READ_ONLY", readOnly: true }, { status: 409 });
    const filePath = await resolveSessionPath(id);
    if (!filePath) {
      return new Response("Session not found", { status: 404 });
    }
    try {
      ({ session } = await startRpcSession(id, filePath, undefined));
    } catch (error) {
      return new Response(`Failed to start agent: ${error}`, { status: 500 });
    }
  }

  if (isBioMindRemoteProfile()) {
    if (!isBioMindProjectAllowed(session.cwd)) return Response.json({ error: "PROJECT_NOT_AUTHORIZED" }, { status: 403 });
    const owned = session;
    const stream = boundedEventStream({ signal: req.signal, initial: { type: "connected", sessionId: id, profile: "biomind", automaticReplay: false },
      subscribe: (emit) => {
        // Subscribe first: any pending UI/event created while the owner answers
        // get_state must also reach this stream. The adapter emits that snapshot.
        const off = owned.onEvent(emit);
        void owned.send({ type: "get_state" }).catch(() => emit({ type: "resync_required", reason: "owner_state_unavailable", automaticReplay: false }));
        return off;
      } });
    return new Response(stream, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store", Connection: "keep-alive", "X-Accel-Buffering": "no" } });
  }

  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      const encode = (data: unknown) => {
        const text = `data: ${JSON.stringify(data)}\n\n`;
        controller.enqueue(encoder.encode(text));
      };

      // Send initial connected event
      encode({ type: "connected", sessionId: id });

      const unsubscribe = session.onEvent((event) => {
        const clientEvent = toClientEvent(event, v2);
        if (clientEvent) encode(clientEvent);
      });

      // Heartbeat every 30s to prevent server/proxy timeout (Next.js default ~120-150s)
      const heartbeat = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(":\n\n"));
        } catch {
          // controller already closed
        }
      }, 30_000);

      // Cleanup when client disconnects
      const cleanup = () => {
        clearInterval(heartbeat);
        unsubscribe();
        controller.close();
      };

      // Detect client disconnect via abort signal
      req.signal?.addEventListener("abort", cleanup);
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
