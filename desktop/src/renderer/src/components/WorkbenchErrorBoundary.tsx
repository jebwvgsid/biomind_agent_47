import { Component, type ReactNode } from "react";
import { reloadExistingWorkbench, reportRendererIssue, showConnectionSafely } from "../lib/rendererSafety";

/** Last-resort UI must not depend on the store, translations, or failing child. */
export class WorkbenchErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { reportRendererIssue("render", "RENDER_FAILED", error); }
  render() {
    if (!this.state.failed) return this.props.children;
    return <main role="alert" style={{ minHeight: "100vh", boxSizing: "border-box", background: "#f7f8fa", color: "#20242c", padding: "64px 32px", fontFamily: "system-ui, sans-serif" }}>
      <section style={{ maxWidth: 620, margin: "0 auto" }}>
        <h1 style={{ fontSize: 24 }}>BioMind Studio 界面暂时无法显示</h1>
        <p>服务器会话与任务没有因界面恢复而被关闭。重新加载会恢复已有会话状态，不会自动重发任务。</p>
        <p style={{ fontSize: 13 }}>诊断代码：RENDER_FAILED。诊断仅记录错误类型与程序位置，不包含任务内容。</p>
        <div style={{ display: "flex", gap: 12, marginTop: 24 }}>
          <button className="btn primary" onClick={reloadExistingWorkbench}>重新加载工作台</button>
          <button className="btn" onClick={() => { void showConnectionSafely(); }}>连接设置</button>
        </div>
      </section>
    </main>;
  }
}
