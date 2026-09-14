import { workspaces } from './demo/data.js';
import { DemoWorkbenchService } from './services/demo-service.js';
import type { Panel, Resource, Scenario, Session, WorkbenchService } from './types.js';
import { escape, renderComposer, renderConversation, renderDemoBar, renderDetails, renderSidebar, renderTop } from './ui/components.js';
import type { ViewState } from './ui/components.js';
import { icon } from './ui/icons.js';

const demo = new DemoWorkbenchService();
const service: WorkbenchService = demo;
const state: ViewState = {
  active: 'demo-approval', server: 'lab', workspace: 'rice', panel: 'plan', panelOpen: innerWidth > 1000,
  sidebarOpen: false, search: '', scenario: 'approval', theme: 'light', expanded: new Set(['plan']),
  logLimit: 60, draft: '', references: [],
};
const drafts = new Map<string, { text: string; references: string[] }>();
const app = document.getElementById('biomind-workbench') as HTMLDivElement;
app.innerHTML = '<div class="shell"><aside class="sidebar" aria-label="服务器与会话导航"></aside><header class="topbar"></header><div class="demo-bar"></div><main class="transcript" aria-label="BioMind 对话"></main><aside class="detail" aria-label="会话详情"></aside><section class="composer" aria-label="消息输入"></section></div><dialog id="modal" aria-labelledby="modal-title"></dialog><div class="toast" role="status" aria-live="polite"></div>';
const el = <T extends HTMLElement = HTMLElement>(selector: string): T => app.querySelector<T>(selector)!;
let composing = false;
let requestSerial = 0;
let toastTimer: ReturnType<typeof setTimeout>;
let preview: Resource | undefined;
const requestId = (): string => `demo-request-${Date.now()}-${++requestSerial}`;
const current = (): Session => service.snapshot(state.active).session;

function toast(message: string): void {
  clearTimeout(toastTimer); el('.toast').textContent = message;
  toastTimer = setTimeout(() => { el('.toast').textContent = ''; }, 4300);
}
function preserveDraft(): void { drafts.set(state.active, { text: state.draft, references: [...state.references] }); }
function switchSession(id: string): void {
  preserveDraft();
  const s = service.snapshot(id).session;
  state.active = id; state.server = s.serverId; state.workspace = s.workspaceId;
  state.draft = drafts.get(id)?.text || ''; state.references = [...drafts.get(id)?.references || []];
  state.scenario = ''; state.logLimit = 60; state.sidebarOpen = false; state.expanded = new Set(['plan']);
  render(); el('.transcript').scrollTop = 0;
}
function render(): void {
  const active = document.activeElement;
  const focusId = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active.id : '';
  const selection = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? [active.selectionStart, active.selectionEnd] : undefined;
  const scroll = el('.transcript').scrollTop;
  const detailsScroll = app.querySelector('.detail-body')?.scrollTop || 0;
  const logsScroll = app.querySelector('.logs')?.scrollTop || 0;
  const s = current();
  el('.shell').className = `shell ${state.panelOpen ? '' : 'panel-closed'} ${state.sidebarOpen ? 'sidebar-open' : ''}`;
  document.documentElement.dataset.theme = state.theme;
  el('.sidebar').innerHTML = renderSidebar(service.listSessions(), state);
  el('.topbar').innerHTML = renderTop(s, state);
  el('.demo-bar').innerHTML = renderDemoBar(state);
  el('.transcript').innerHTML = renderConversation(s, state);
  el('.detail').innerHTML = renderDetails(s, state);
  if (!composing) el('.composer').innerHTML = renderComposer(s, state);
  el('.transcript').scrollTop = scroll;
  el('.detail-body').scrollTop = detailsScroll;
  if (app.querySelector('.logs')) el('.logs').scrollTop = logsScroll;
  if (focusId && !composing) {
    const next = document.getElementById(focusId);
    if (next instanceof HTMLInputElement || next instanceof HTMLTextAreaElement) {
      next.focus({ preventScroll: true });
      if (selection && selection[0] !== null && selection[1] !== null && ['text','search','textarea'].includes(next instanceof HTMLInputElement ? next.type : 'textarea')) next.setSelectionRange(selection[0], selection[1]);
    }
  }
}
function send(): void {
  if (composing) return;
  const s = current();
  if (!state.draft.trim() || s.connection !== 'demo') return;
  const receipt = service.sendMessage(s.id, requestId(), state.draft, state.references);
  if (receipt.status !== 'rejected') { state.draft = ''; state.references = []; preserveDraft(); render(); }
  toast(receipt.message); el<HTMLTextAreaElement>('#message-input').focus();
  el('.transcript').scrollTop = el('.transcript').scrollHeight;
}
function showModal(title: string, content: string, footer = ''): void {
  const modal = el<HTMLDialogElement>('#modal');
  modal.innerHTML = `<div class="modal-heading"><h2 id="modal-title">${escape(title)}</h2><span class="tiny-demo">演示模式</span><button class="icon-button" data-action="close-modal" aria-label="关闭窗口">${icon('close')}</button></div><div class="modal-content">${content}</div>${footer ? `<div class="modal-footer">${footer}</div>` : ''}`;
  modal.showModal();
}
function showFilePicker(): void {
  showModal('引用服务器文件', `<p class="muted">从当前演示工作区选择输入文件，只添加远端引用。不会上传或下载原始数据。</p><div class="resource-path">${workspaces.find(w => w.id === state.workspace)!.path}</div>${current().resources.filter(r => r.kind === 'input').map(r => `<label class="file-choice"><input type="checkbox" value="${r.id}" name="reference" ${state.references.includes(r.id) ? 'checked' : ''}>${icon('file', 22)}<span><strong>${r.name}</strong><small>${r.path} · ${r.size}</small></span></label>`).join('')}`, '<button class="secondary" data-action="close-modal">取消</button><button class="primary" data-action="apply-references">添加引用</button>');
}
function showResource(id: string): void {
  try { preview = service.readResource(state.active, id); }
  catch (error) { toast((error as Error).message); return; }
  const r = preview;
  showModal(r.name, `<div class="resource-path">${escape(r.path)}</div><p class="muted">${r.kind === 'input' ? '演示输入资源摘要；未读取服务器文件。' : r.status === 'verified' ? '演示交付核验通过。下列数据全部为合成内容。' : '已生成的演示中间文件，尚未核验。'}</p>${r.id === 'report' ? '<div class="preview-stats"><div><small>演示读取对数</small><strong>24.0 M</strong><em>合成指标</em></div><div><small>演示 Q30</small><strong>94.8%</strong><em>合成指标</em></div><div><small>演示 GC 含量</small><strong>43.2%</strong><em>合成指标</em></div></div>' : ''}<pre class="preview-text">${escape(r.preview)}</pre>`, `<button class="secondary" data-action="close-modal">关闭</button>${r.kind === 'artifact' ? '<button class="primary" data-action="download">'+icon('download',16)+'下载演示摘要</button>' : ''}`);
}
function showStop(): void {
  showModal('请求停止演示任务', '<p>提交后先显示“已请求停止”。点击“模拟停止回执”后才会显示“已停止”。</p><p class="muted">这是本页演示操作，不终止服务器上的任何进程。</p>', '<button class="secondary" data-action="close-modal">返回</button><button class="primary" data-action="confirm-stop">请求停止（演示）</button>');
}
function selectWorkspace(serverId: string, workspaceId: string): void {
  state.search = '';
  const existing = service.listSessions().find(s => s.serverId === serverId && s.workspaceId === workspaceId);
  switchSession(existing?.id || service.createSession(serverId, workspaceId).session.id);
}
app.addEventListener('click', event => {
  const button = (event.target as Element).closest<HTMLElement>('[data-action]');
  if (!button || (button instanceof HTMLButtonElement && button.disabled)) return;
  const { action, id, tab, prompt } = button.dataset;
  const s = current();
  switch (action) {
    case 'new': switchSession(service.createSession(state.server, state.workspace).session.id); break;
    case 'session': switchSession(id!); break;
    case 'send': send(); break;
    case 'starter': state.draft = prompt!; preserveDraft(); render(); el<HTMLTextAreaElement>('#message-input').focus(); break;
    case 'theme': state.theme = state.theme === 'light' ? 'dark' : 'light'; render(); break;
    case 'sidebar': state.sidebarOpen = !state.sidebarOpen; render(); break;
    case 'panel-toggle': state.panelOpen = !state.panelOpen; render(); break;
    case 'tab': state.panel = tab as Panel; state.panelOpen = true; render(); break;
    case 'files': showFilePicker(); break;
    case 'unreference': state.references = state.references.filter(r => r !== id); preserveDraft(); render(); break;
    case 'apply-references': state.references = [...el('#modal').querySelectorAll<HTMLInputElement>('input:checked')].map(input => input.value); preserveDraft(); el<HTMLDialogElement>('#modal').close(); render(); el<HTMLTextAreaElement>('#message-input').focus(); break;
    case 'close-modal': el<HTMLDialogElement>('#modal').close(); break;
    case 'preview': showResource(id!); break;
    case 'download': {
      if (!preview || preview.kind !== 'artifact') break;
      const url = URL.createObjectURL(new Blob([preview.preview], { type: 'text/plain;charset=utf-8' }));
      const a = document.createElement('a'); a.href = url; a.download = `${preview.name}.demo.txt`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); toast('已下载演示文本摘要，未读取服务器产物。'); break;
    }
    case 'approve': case 'reject': {
      const approval = s.approvals.find(a => a.id === id)!;
      const receipt = service.submitApproval({ requestId: requestId(), sessionId: s.id, approvalId: id!, version: approval.version, digest: approval.digest, decision: action === 'approve' ? 'approve' : 'reject' });
      toast(receipt.message); break;
    }
    case 'connect': {
      const result = service.reconnect(s.id, s.cursor);
      toast(`演示重连完成：附着同一运行，补齐 ${result.events.length} 条活动；没有重新执行。`); break;
    }
    case 'disconnect': demo.disconnect(s.id); toast('已模拟断线，保留最近确认状态和草稿。'); break;
    case 'advance': demo.advance(s.id); if (s.connection === 'interrupted') toast('演示后台已推进，重连后获取最新事实。'); break;
    case 'fail': demo.simulateFailure(s.id); break;
    case 'recover': demo.proposeRecovery(s.id); break;
    case 'stop': showStop(); break;
    case 'confirm-stop': el<HTMLDialogElement>('#modal').close(); toast(service.requestStop(s.id, requestId()).message); break;
    case 'long-logs': demo.addDemoLogs(s.id); toast('已追加 240 条合成日志和 1 条未知观察事件。'); break;
    case 'more-logs': state.logLimit += 60; render(); break;
  }
});
app.addEventListener('input', event => {
  const target = event.target as HTMLInputElement;
  if (target.id === 'message-input') {
    state.draft = target.value; preserveDraft();
    el<HTMLButtonElement>('[data-action="send"]').disabled = !target.value.trim() || current().connection !== 'demo';
  }
  if (target.id === 'session-search') { state.search = target.value; render(); }
});
app.addEventListener('change', event => {
  const target = event.target as HTMLSelectElement;
  if (target.id === 'server-select') selectWorkspace(target.value, workspaces.find(w => w.serverId === target.value)!.id);
  if (target.id === 'workspace-select') selectWorkspace(state.server, target.value);
  if (target.id === 'scenario-select') {
    const snapshot = demo.chooseScenario(target.value as Scenario, state.server, state.workspace);
    switchSession(snapshot.session.id); state.scenario = target.value as Scenario; render();
  }
});
app.addEventListener('compositionstart', () => { composing = true; });
app.addEventListener('compositionend', () => { composing = false; });
app.addEventListener('keydown', event => {
  if ((event.target as HTMLElement).id === 'message-input' && event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !composing) { event.preventDefault(); send(); }
  const target = event.target as HTMLElement;
  if (target.getAttribute('role') === 'tab' && ['ArrowLeft','ArrowRight'].includes(event.key)) {
    event.preventDefault();
    const panels: Panel[] = ['plan','script','logs','files'];
    state.panel = panels[(panels.indexOf(state.panel) + (event.key === 'ArrowRight' ? 1 : 3)) % 4];
    render(); el<HTMLButtonElement>(`[role="tab"][data-tab="${state.panel}"]`).focus();
  }
});
app.addEventListener('toggle', event => {
  const target = event.target;
  if (target instanceof HTMLDetailsElement && target.dataset.disclosure && target.isConnected) {
    if (target.open) state.expanded.add(target.dataset.disclosure); else state.expanded.delete(target.dataset.disclosure);
  }
}, true);
service.subscribe(render);
render();
