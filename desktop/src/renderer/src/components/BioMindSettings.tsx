import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { Close } from "./icons";

const unwrap = (value: any): any => value?.data ?? value;

/** Server-authoritative controls inside flowflic's existing settings layout. */
export function BioMindSettings() {
  const open = useStore((s) => s.settingsOpen);
  const threadId = useStore((s) => s.activeThreadId);
  const config = useStore((s) => s.config);
  const [tab, setTab] = useState("server");
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [scope, setScope] = useState("project");
  const [document, setDocument] = useState<any>(null);
  const [draft, setDraft] = useState("");
  const [receipt, setReceipt] = useState("");
  const epoch = useRef(0);
  const state = unwrap(data?.state) || {};
  const resources = unwrap(data?.resources);
  const modelData = unwrap(data?.models);
  const models = Array.isArray(modelData) ? modelData : modelData?.models || [];
  const capable = (name: string) => {
    const capability = state.capabilities?.[name];
    return capability === true || (capability?.supported === true && capability?.available === true);
  };
  const refresh = async () => {
    const current = ++epoch.current;
    setBusy(true); setError("");
    try {
      const result = await window.pi.biomind.getControls(threadId || undefined);
      if (current === epoch.current) setData(result);
    } catch (caught: any) { if (current === epoch.current) setError(caught?.message || "读取服务器设置失败"); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  useEffect(() => {
    setDocument(null); setDraft(""); setReceipt(""); setData(null);
    if (open) void refresh();
    return () => { epoch.current++; };
  }, [open, threadId]);
  useEffect(() => { setDocument(null); setDraft(""); setReceipt(""); }, [scope]);
  const run = async (command: Record<string, unknown>, after?: (result: any) => void) => {
    if (!threadId) return;
    const binding = epoch.current;
    setBusy(true); setError(""); setReceipt("");
    try {
      const result = unwrap(await window.pi.biomind.control({ threadId, ...command }));
      if (epoch.current !== binding) return;
      after?.(result);
      await refresh();
    } catch (caught: any) { if (epoch.current === binding) setError(caught?.message || "服务器操作状态未知"); }
    finally { setBusy(false); }
  };
  if (!open) return null;
  return <div className="settings-backdrop" onMouseDown={() => useStore.getState().closeSettings()}>
    <div className="set-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="BioMind 设置">
      <aside className="set-side">
        <div className="set-brand"><div><div className="set-brand-title">设置</div><div className="set-brand-sub">BioMind Studio</div></div></div>
        <nav className="set-tabs">{[["server", "服务器与模型"], ["instructions", "说明"], ["skills", "Skills 与工具"], ["memory", "记忆"], ["about", "版本与能力"]].map(([id, label]) => <button className={`set-tab ${tab === id ? "active" : ""}`} key={id} onClick={() => setTab(id)}><span className="set-tab-bar" />{label}</button>)}</nav>
        <div className="set-side-foot"><label className="set-language">窗口主题<select value={config?.theme || "light"} onChange={(event) => { void window.pi.app.setConfig({ theme: event.target.value }).then((next) => useStore.setState({ config: next })); }}><option value="light">浅色</option><option value="dark">深色</option><option value="system">跟随系统</option></select></label><p>主题留在 Windows。模型、说明、Skills 与记忆由服务器 BioMind 管理。</p></div>
      </aside>
      <section className="set-main">
        <header className="set-head"><h2>BioMind 设置</h2><button className="set-iconbtn" aria-label="关闭设置" onClick={() => useStore.getState().closeSettings()}><Close size={18} /></button></header>
        <div className="set-body biomind-settings-body">
          <div className="biomind-settings-actions"><button className="btn" disabled={busy} onClick={() => void refresh()}>刷新服务器状态</button><button className="btn" onClick={() => { void window.pi.biomind.showConnection(); }}>连接设置</button></div>
          {busy && <p role="status">正在等待服务器确认…</p>}{error && <p role="alert">{error}</p>}{receipt && <p role="status">{receipt}</p>}
          {!threadId && <p>选择或新建一个 BioMind 会话后，可读取此项目的实际设置。</p>}
          {tab === "server" && <>
            <h3>当前会话模型</h3><p>{state.model ? `${state.model.provider} / ${state.model.id}` : "尚未读取"}</p>
            <select className="set-input" aria-label="服务器模型" disabled={busy || !threadId || !capable("setModel")} value={state.model ? `${state.model.provider}/${state.model.id}` : ""} onChange={(event) => {
              const model = models.find((item: any) => `${item.provider}/${item.id}` === event.target.value);
              if (model) void run({ type: "set_model", provider: model.provider, modelId: model.id }, () => { void useStore.getState().refreshOpenThreadModels(); });
            }}><option value="" disabled>选择服务器模型</option>{models.map((model: any) => <option key={`${model.provider}/${model.id}`} value={`${model.provider}/${model.id}`} disabled={model.authenticated === false}>{model.name || model.id} · {model.provider}{model.authenticated === false ? " · 缺少服务器凭据" : ""}</option>)}</select>
            <p className="set-hint">操作只作用于当前服务器会话；忙碌时以服务器能力状态为准。</p>
            <h3>运行身份</h3><pre>{JSON.stringify({ project: state.project || state.cwd, session: state.sessionId || threadId, ownerGeneration: state.ownerGeneration, loadedBuild: state.loadedBuild || state.loaded_build }, null, 2)}</pre>
          </>}
          {tab === "instructions" && <>
            <h3>服务器说明</h3><select className="set-input" aria-label="说明范围" disabled={busy} value={scope} onChange={(event) => setScope(event.target.value)}><option value="project">当前项目</option><option value="global">全局</option><option value="project-base">项目基准</option></select>
            <button className="btn" disabled={busy || !threadId || !capable("instructions")} onClick={() => void run({ type: "get_instructions", scope }, (result) => { setDocument(result); setDraft(result.text || ""); })}>读取说明</button>
            {document && <><p className="set-hint">{document.path}</p><textarea className="set-input" rows={12} value={draft} disabled={busy} onChange={(event) => { setDraft(event.target.value); setReceipt(""); }} /><button className="btn primary" disabled={busy || !capable("instructions")} onClick={() => void run({ type: "save_instructions", scope, text: draft, expectedVersion: document.version, create: !document.exists }, (result) => { setDocument(result); setDraft(result.text || draft); setReceipt(result.saved === true ? (result.loaded === true ? "服务器已保存并载入" : "服务器已保存；等待后续载入") : "服务器未确认保存"); })}>保存到服务器</button></>}
          </>}
          {tab === "skills" && <>
            <h3>服务器 Skills</h3><p className="set-hint">仅展示服务器实际发现的资源；启用范围和生效时机由 BioMind 返回。</p>
            {(resources?.skills || []).map((skill: any) => <div className="biomind-resource-row" key={skill.resourceId || skill.path}><label><input type="checkbox" checked={skill.configuredEnabled === true} disabled={busy || !capable("setSkill") || !resources.selectionScopes?.includes("project")} onChange={(event) => void run({ type: "set_skill", resourceId: skill.resourceId, scope: "project", enabled: event.target.checked })} /> {skill.name}</label><p className="set-hint">{skill.path} · {skill.loaded === true ? "已载入" : "载入状态由服务器报告"}</p><p>{skill.description}</p></div>)}
            {!resources?.skills?.length && <p>服务器尚未返回 Skills 列表。</p>}
            <button className="btn" disabled={busy || !capable("reload")} onClick={() => void run({ type: "reload" })}>重新加载服务器资源</button>
          </>}
          {tab === "memory" && <>
            <h3>BioMind 记忆</h3><p className="set-hint">沿用服务器现有记忆和授权机制。</p>
            <label><input type="checkbox" checked={state.memory?.history_enabled === true} disabled={busy || !capable("memorySettings") || typeof state.memory?.history_enabled !== "boolean"} onChange={(event) => void run({ type: "settings", history_enabled: event.target.checked })} /> 保留历史</label>
            <select className="set-input" aria-label="服务器记忆模式" value={state.memory?.memory_mode || ""} disabled={busy || !capable("memorySettings")} onChange={(event) => void run({ type: "settings", memory_mode: event.target.value })}><option value="" disabled>尚未读取</option>{["ask", "auto", "manual", "disabled"].map((mode) => <option value={mode} key={mode}>{mode}</option>)}</select>
          </>}
          {tab === "about" && <>
            <h3>BioMind Studio</h3><p>界面来源：flowflic/Pi-Studio 0.4.6，提交 0fbc8178b80af55bf043465ef36d5bfe8373463c。</p><p>当前连接服务器的既有 BioMind Main 引擎。上游 Pi 1.1.0 的 Codemode、自动更新、桌面远控及独立自动化不在此候选中启用。</p>
            <h3>服务器实际能力</h3><pre>{JSON.stringify(state.capabilities || { status: "尚未读取" }, null, 2)}</pre>
          </>}
        </div>
      </section>
    </div>
  </div>;
}
