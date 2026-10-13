import { RemoteFolderPicker, selectRemoteFolder } from "./components/RemoteFolderPicker";
import { BioMindConnectionStatus } from "./components/BioMindConnectionStatus";
import { BioMindUIStatus } from "./components/BioMindUIStatus";
import { persistWindowThread, reportRendererIssue, showConnectionSafely } from "./lib/rendererSafety";
import { useEffect, useState } from "react";
import { useStore } from "./store";
import { usePiEvents } from "./lib/usePiEvents";
import { TitleBar } from "./components/TitleBar";
import { Sidebar } from "./components/Sidebar";
import { Chat } from "./components/Chat";
import { Preview } from "./components/Preview";
import { Toasts } from "./components/Toasts";
import { ExtUiModal } from "./components/ExtUiModal";
import { Settings } from "./components/Settings";
import { SearchModal } from "./components/SearchModal";
import { PluginsPanel } from "./components/PluginsPanel";
import { AutomationPanel } from "./components/AutomationPanel";
import { Folder, Plus } from "./components/icons";
import { LanguageBridge } from "./components/LanguageBridge";
import { RemotePanel } from "./components/RemotePanel";
import appIconUrl from "../../../resources/biomind.svg";

export default function App() {
  const bootstrap = useStore((s) => s.bootstrap);
  const activeThreadId = useStore((s) => s.activeThreadId);
  const activeThread = useStore((s) => s.activeThreadId ? s.threads[s.activeThreadId] : null);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const connectionStatus = useStore((s) => s.connectionStatus);
  const previewOpen = useStore((s) => s.previewOpen);
  const previewExpanded = useStore((s) => s.previewExpanded);
  const projects = useStore((s) => s.projects);
  const runtime = useStore((s) => s.runtime);
  const theme = useStore((s) => s.config?.theme || "light");
  const language = useStore((s) => s.config?.language || "en");
  const [remoteOpen, setRemoteOpen] = useState(false);
  usePiEvents();

  useEffect(() => {
    void bootstrap().catch((error) => {
      useStore.setState({ connectionStatus: "error" });
      reportRendererIssue("unhandledrejection", "BOOTSTRAP_FAILED", error);
    });
  }, [bootstrap]);

  useEffect(() => {
    if (connectionStatus !== "ready" || activeThreadId?.startsWith("opening-")) return;
    void persistWindowThread(activeThreadId);
  }, [activeThreadId, connectionStatus]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      const resolved = theme === "system" ? (media.matches ? "dark" : "light") : theme;
      document.documentElement.dataset.theme = resolved;
      document.documentElement.style.colorScheme = resolved;
    };

    applyTheme();
    if (theme !== "system") return;
    media.addEventListener?.("change", applyTheme);
    return () => media.removeEventListener?.("change", applyTheme);
  }, [theme]);

  // Ctrl/Cmd+K opens the thread search palette
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        useStore.getState().openSearch();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const newTask = async () => {
    try {
    let cwd: string | null = useStore.getState().activeProjectCwd;
    if (!cwd) {
      const p = await selectRemoteFolder();
      if (!p || Array.isArray(p)) return;
      const actual = await window.pi.app.openProject(p);
      if (typeof actual !== "string" || !actual) throw new Error("PROJECT_DATA_INVALID");
      await useStore.getState().refreshProjects();
      useStore.getState().setActiveProject(actual);
      cwd = actual;
    }
    if (cwd) await useStore.getState().openThread(cwd);
    } catch (error) {
      reportRendererIssue("unhandledrejection", "PROJECT_OPEN_FAILED", error);
      useStore.getState().pushToast("error", "项目或会话未能打开，请重试；没有自动重发任务。");
    }
  };

  return (
    <div className="app">
      <LanguageBridge />
      <RemoteFolderPicker />
      <BioMindConnectionStatus />
      <BioMindUIStatus />
      <TitleBar />
      <div className={`body ${previewExpanded ? "preview-expanded" : ""}`}>
        <Sidebar onOpenRemote={() => { void showConnectionSafely(); }} remoteOpen={remoteOpen} />
        {activeThreadId && activeThread ? (
          <Chat />
        ) : (
          <section className="main">
            <div className="empty-state">
              <div>
                <div className="empty-state-app-icon">
                  <img src={appIconUrl} alt="" aria-hidden="true" />
                </div>
                <h2>BioMind Studio</h2>
                {activeThreadId && !activeThread && <p role="status">当前会话视图尚未恢复，可从左侧项目历史重新打开原会话。</p>}
                <p style={{ maxWidth: 420, margin: "0 auto 16px" }}>
                  连接服务器上的 BioMind。左侧选择 Linux 项目与对话，查看 Main agent 的工具过程和真实产物。
                </p>
                {!runtime?.ok && runtime && <p style={{ color: "#b23a2c" }}>BioMind 尚未就绪：{runtime.error}</p>}
                <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
                  <button className="btn primary" onClick={newTask}>
                    <Plus size={14} /> 新建任务
                  </button>
                  <button className="btn" onClick={() => useStore.getState().openProjectFolder()}>
                    <Folder size={14} /> 打开远端项目
                  </button>
                </div>
                {projects.length === 0 && <p className="muted" style={{ marginTop: 14 }}>尚未打开任何项目。</p>}
              </div>
            </div>
          </section>
        )}
        {previewOpen && <Preview />}
      </div>
      <Toasts />
      <ExtUiModal />
      <SearchModal />
      <PluginsPanel />
      <AutomationPanel />
      <Settings />
      {remoteOpen && (
        <div className="settings-backdrop" onMouseDown={() => setRemoteOpen(false)}>
          <div className="set-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog" aria-modal="true">
            <section className="set-main" style={{ width: "min(900px, 94vw)" }}>
              <header className="set-head">
                <h2>{language === "zh" ? "Android 手机远程控制" : "Android remote companion"}</h2>
                <button className="set-iconbtn" onClick={() => setRemoteOpen(false)} aria-label={language === "zh" ? "关闭远程设置" : "Close remote settings"}>×</button>
              </header>
              <div className="set-body">
                <RemotePanel language={language} />
              </div>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
