import { useEffect } from "react";
import { useStore } from "../store";
import { reportRendererIssue, showConnectionSafely } from "../lib/rendererSafety";

export function BioMindConnectionStatus() {
  const status = useStore((s) => s.connectionStatus);
  useEffect(() => {
    let alive = true;
    let epoch = 0;
    const failed = (error: unknown) => {
      if (!alive) return;
      useStore.setState({ connectionStatus: "error" });
      reportRendererIssue("unhandledrejection", "SESSION_RESTORE_FAILED", error);
    };
    const receive = async (state: any, initial = false) => {
      if (!alive) return;
      if (!state || typeof state.status !== "string") throw new Error("SESSION_RESTORE_FAILED");
      const current = ++epoch;
      useStore.setState((s) => ({ connectionRevision: s.connectionRevision + 1 }));
      if (state.status !== "ready") {
        useStore.setState((s) => ({ connectionStatus: state.status || "disconnected", threads: Object.fromEntries(Object.entries(s.threads).map(([id, thread]) => [id, { ...thread, connected: false }])) }));
        return;
      }
      // bootstrap restores the first snapshot. Reconnect restores all backend
      // state before sending can become available; it never replays a prompt.
      if (initial || !useStore.getState().config) return;
      useStore.setState({ connectionStatus: "restoring" });
      try {
        await useStore.getState().restoreRemoteThreads();
        const actual = await window.pi.biomind.getStatus();
        if (!actual || typeof actual.status !== "string") throw new Error("SESSION_RESTORE_FAILED");
        if (alive && epoch === current) useStore.setState({ connectionStatus: actual.status });
      } catch (error: any) {
        if (alive && epoch === current) { failed(error); useStore.getState().pushToast("error", "会话恢复失败；未自动重发任何任务。"); }
      }
    };
    void window.pi.biomind.getStatus().then((state: any) => receive(state, true)).catch(failed);
    const off = window.pi.biomind.onStatus((state: any) => { void receive(state).catch(failed); });
    return () => { alive = false; epoch++; off(); };
  }, []);
  if (status === "ready") return null;
  const label = status === "restoring" ? "正在恢复服务器会话状态…" : status === "error" ? "连接失败；任务状态待核对" : status === "disconnected" ? "已断开；服务器任务保留" : "正在连接服务器…";
  return <div className="biomind-connection-status" role="status"><span>{label}</span><button className="btn" onClick={() => { void showConnectionSafely(); }}>连接设置</button></div>;
}
