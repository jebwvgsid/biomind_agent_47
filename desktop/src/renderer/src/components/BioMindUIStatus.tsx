import { useEffect, useState } from "react";
import { reloadExistingWorkbench, watchRendererIssues } from "../lib/rendererSafety";

type LocalStatus = { status: "saved" | "pending" | "unsaved"; code?: string };

export function BioMindUIStatus() {
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [issue, setIssue] = useState("");
  useEffect(() => {
    let alive = true;
    let revision = 0;
    const update = (state: LocalStatus) => {
      if (alive && ["saved", "pending", "unsaved"].includes(state?.status)) setLocal(state);
    };
    const off = window.pi.biomind.onLocalUIState((state) => { revision++; update(state); });
    const started = revision;
    void window.pi.biomind.getLocalUIState().then((state) => { if (revision === started) update(state); }).catch(() => {});
    const offIssue = watchRendererIssues((error) => {
      if (error.code === "LOCAL_UI_STATE_UNSAVED") setLocal({ status: "unsaved" });
      else setIssue(error.code);
    });
    return () => { alive = false; off(); offIssue(); };
  }, []);
  if (local?.status !== "unsaved" && !issue) return null;
  return <div className="biomind-connection-status" role="status" style={{ flexShrink: 0 }}>
    <span>{local?.status === "unsaved" ? "本地窗口状态暂未保存；会话与任务仍在服务器，下次可从项目历史打开。" : `界面部分操作未完成（${issue}）。服务器任务未自动重发。`}</span>
    {issue && <button className="btn" onClick={reloadExistingWorkbench}>重新加载界面</button>}
  </div>;
}
