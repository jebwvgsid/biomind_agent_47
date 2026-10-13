import { useEffect, useRef, useState } from "react";
import { Close, Folder } from "./icons";
import { readRemoteDirectory } from "../lib/remoteData";
import { reportRendererIssue } from "../lib/rendererSafety";

type Selection = { initial?: string; resolve: (path: string | null) => void };
let activeRequest: Selection | null = null;

export function selectRemoteFolder(initial?: string): Promise<string | null> {
  activeRequest?.resolve(null);
  return new Promise((resolve) => {
    activeRequest = { initial, resolve };
    window.dispatchEvent(new Event("biomind:choose-remote-project"));
  });
}

export function RemoteFolderPicker() {
  const [request, setRequest] = useState<Selection | null>(null);
  const [directory, setDirectory] = useState("");
  const [pathInput, setPathInput] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<{ name: string; path: string; isDirectory: boolean }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const epoch = useRef(0);
  const browse = async (path?: string) => {
    const current = ++epoch.current;
    setBusy(true); setError("");
    try {
      const result = readRemoteDirectory(await window.pi.biomind.browseProjects(path));
      if (current !== epoch.current) return;
      setDirectory(result.path); setPathInput(result.path);
      setParent(result.parent);
      setEntries(result.entries);
    } catch (caught: any) { if (current === epoch.current) { reportRendererIssue("unhandledrejection", "REMOTE_DIRECTORY_INVALID", caught); setError("无法读取远端目录，请检查连接后重试（REMOTE_DIRECTORY_INVALID）。"); } }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const finish = (path: string | null) => {
    epoch.current++;
    request?.resolve(path);
    if (activeRequest === request) activeRequest = null;
    setRequest(null);
  };
  useEffect(() => {
    const open = () => {
      setRequest(activeRequest); setDirectory(""); setEntries([]);
      if (activeRequest) void browse(activeRequest.initial);
    };
    window.addEventListener("biomind:choose-remote-project", open);
    if (activeRequest) open();
    return () => { window.removeEventListener("biomind:choose-remote-project", open); epoch.current++; activeRequest?.resolve(null); activeRequest = null; };
  }, []);
  useEffect(() => {
    if (!request) return;
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") finish(null); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [request]);
  if (!request) return null;
  return <div className="settings-backdrop" onMouseDown={() => finish(null)}>
    <section className="set-modal biomind-project-picker" role="dialog" aria-modal="true" aria-label="选择远端 Linux 项目" onMouseDown={(e) => e.stopPropagation()}>
      <header className="set-head"><h2><Folder size={18} /> 选择远端 Linux 项目</h2><button className="set-iconbtn" aria-label="取消" onClick={() => finish(null)}><Close size={18} /></button></header>
      <div className="set-body">
        <p className="set-hint">目录来自当前服务器。项目授权与访问范围由 BioMind 检查。</p>
        <form className="biomind-folder-path" onSubmit={(e) => { e.preventDefault(); void browse(pathInput); }}><input className="set-input" aria-label="远端目录路径" value={pathInput} onChange={(e) => setPathInput(e.target.value)} /><button className="btn" disabled={busy}>前往</button></form>
        {error && <p role="alert">{error}</p>}
        <div className="biomind-folder-list" aria-busy={busy}>
          {parent && <button className="btn" disabled={busy} onClick={() => void browse(parent)}>.. 上一级</button>}
          {busy ? <p role="status">正在读取服务器目录…</p> : entries.filter((item) => item.isDirectory).map((item) => <button className="btn biomind-folder-entry" key={item.path} onClick={() => void browse(item.path)}><Folder size={15} /><span>{item.name}</span></button>)}
          {!busy && !entries.length && !error && <p className="set-hint">此目录没有可见子目录。</p>}
        </div>
      </div>
      <footer className="biomind-picker-actions"><button className="btn" onClick={() => finish(null)}>取消</button><button className="btn primary" disabled={busy || !!error || !directory} onClick={() => finish(directory)}>选择此项目</button></footer>
    </section>
  </div>;
}
