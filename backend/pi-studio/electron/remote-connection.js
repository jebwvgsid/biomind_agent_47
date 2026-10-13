"use strict";
const zh = navigator.language.toLowerCase().startsWith("zh");
const labels = {
  title:["连接服务器","Connect to a server"], intro:["在本地发送消息，由服务器上的完整 BioMind 会话执行。","Send messages locally to a complete BioMind session on your server."],
  saved:["已保存的服务器","Saved servers"], label:["显示名称","Display name"], alias:["SSH 主机别名","SSH host alias"], local:["本地端口","Local port"], remote:["远端工作台端口","Server workbench port"],
  jump:["跳板机别名（可选，留空复用 SSH 配置）","Jump host alias (optional; blank uses SSH configuration)"], password:["服务器工作台口令（仅保留在本次连接中）","Server workbench password (memory only)"],
  trust:["使用系统已配置的 SSH 身份与主机信任。首次主机信任或 SSH 密钥配置请由管理员完成；这里不会自动接受密钥。","Uses existing system SSH identities and host trust. Ask your administrator to configure keys and initial host trust; this app never accepts unknown keys automatically."],
  connect:["连接","Connect"], save:["保存配置","Save configuration"], disconnect:["断开","Disconnect"], status:["连接状态","Connection status"], new:["添加服务器","Add server"],
  remove:["删除快捷项","Delete shortcut"], clearAuth:["清理认证缓存","Clear authentication cache"], cleared:["认证缓存已清理，本地草稿保留。","Authentication cache cleared; local drafts retained."], removed:["快捷项已删除，本地草稿保留。","Shortcut deleted; local drafts retained."],
  storage:["这两项操作会断开对应连接，保留本地草稿，不撤销服务器权限。修改别名、跳板或端口将使用新的独立存储；旧草稿不会自动迁移。","These actions disconnect the selected connection and retain drafts; they do not revoke server access. Changing the alias, jump hosts or ports uses separate storage; existing drafts are not migrated."],
  lifecycle:["断开只关闭本地展示和隧道。远端会话由服务器持有；重连后先核对状态，不自动重发消息或批准。","Disconnect closes local presentation and the tunnel. The server holds the session. Reconnect checks its state without resending messages or approvals."],
  disconnected:["未连接","Disconnected"], connecting:["正在建立 SSH 隧道…","Connecting SSH tunnel…"], "server-starting":["正在等待服务器工作台…","Waiting for the server workbench…"], ready:["已连接，工作台已打开。","Connected. The workbench is open."], savedOk:["配置已保存（不含口令）。","Configuration saved without the password."],
  SSH_HOST_KEY_REJECTED:["主机密钥校验失败。请核对系统 SSH 信任记录后再连接。","Host key verification failed. Verify system SSH host trust before reconnecting."], SSH_AUTH_FAILED:["SSH 身份验证失败，请检查系统密钥配置。","SSH authentication failed. Check your system key configuration."],
  LOCAL_PORT_IN_USE:["本地端口已被占用，请选择另一个端口。","Local port is already in use. Choose another port."], SERVER_AUTH_FAILED:["工作台口令不正确。","The workbench password is incorrect."], SERVER_ID_CHANGED:["服务器身份与已保存记录不同，请由管理员核对。","Server identity differs from the saved connection. Ask your administrator to verify it."],
  INVALID_SERVER_PROFILE:["该端点未返回完整 BioMind 工作台身份。","The endpoint did not return a BioMind workbench identity."], SERVER_READY_TIMEOUT:["服务器尚未就绪，请检查远端工作台状态后重连。","The server is not ready. Check the remote workbench, then reconnect."],
  CONNECTION_FAILED:["连接未完成，请检查主机别名、端口及已有 SSH 配置。","Connection failed. Check the host alias, ports and existing SSH configuration."], INVALID_SERVER_PASSWORD:["请输入服务器工作台口令。","Enter the server workbench password."],
};
const t = key => (labels[key] || labels.CONNECTION_FAILED)[zh ? 0 : 1];
document.documentElement.lang = zh ? "zh-CN" : "en";
for (const element of document.querySelectorAll("[data-i18n]")) element.textContent = t(element.dataset.i18n);
const api = window.bioMindConnection;
let profiles = [];
const fields = ["label","alias","jump","localPort","remotePort"];
const selected = document.getElementById("saved"), status = document.getElementById("status");
function showState(state) { status.textContent = t(state.status === "error" ? state.code : state.status); }
function profile() {
  const value = Object.fromEntries(fields.map(key => [key, document.getElementById(key).value]));
  value.localPort = Number(value.localPort); value.remotePort = Number(value.remotePort);
  const previous = profiles.find(item => item.id === selected.value);
  if (previous && ["alias","jump","localPort","remotePort"].every(key => previous[key] === value[key]) && previous.serverId) value.serverId = previous.serverId;
  return value;
}
async function refresh(selectedId = "") {
  const result = await api.list(); if (!result.ok) { showState({status:"error",code:result.code}); return; }
  profiles = result.value.profiles; selected.replaceChildren();
  const initial = document.createElement("option"); initial.value = ""; initial.textContent = t("new"); selected.append(initial);
  for (const value of profiles) { const option = document.createElement("option"); option.value = value.id; option.textContent = value.label; selected.append(option); }
  selected.value = selectedId;
  for (const id of ["remove", "clear-auth"]) document.getElementById(id).disabled = !selected.value;
  showState(result.value.state);
}
selected.addEventListener("change", () => {
  for (const id of ["remove", "clear-auth"]) document.getElementById(id).disabled = !selected.value;
  const value = profiles.find(item => item.id === selected.value) || { label:"",alias:"",jump:"",localPort:30142,remotePort:30141 };
  for (const key of fields) document.getElementById(key).value = value[key];
  document.getElementById("password").value = "";
});
for (const [id, action, notice] of [["remove", "remove", "removed"], ["clear-auth", "clearAuth", "cleared"]]) {
  document.getElementById(id).addEventListener("click", async () => {
    if (!selected.value) return;
    const connectionId = selected.value;
    try {
      const result = await api[action](connectionId);
      if (!result.ok) { showState({status:"error",code:result.code}); return; }
      document.getElementById("password").value = "";
      await refresh(action === "remove" ? "" : connectionId); status.textContent = t(notice);
    } catch { showState({status:"error",code:"CONNECTION_FAILED"}); }
  });
}
document.getElementById("save").addEventListener("click", async () => {
  const result = await api.save(profile());
  if (result.ok) { await refresh(result.value.id); status.textContent = t("savedOk"); } else showState({status:"error",code:result.code});
});
document.getElementById("disconnect").addEventListener("click", async () => {
  try { const result = await api.disconnect(); if (!result.ok) showState({status:"error",code:result.code}); }
  catch { showState({status:"error",code:"CONNECTION_FAILED"}); }
});
document.getElementById("connection-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = document.getElementById("connect"); button.disabled = true;
  const locked = [selected, ...["save", "remove", "clear-auth"].map(id => document.getElementById(id)), ...fields.map(key => document.getElementById(key))];
  for (const element of locked) element.disabled = true;
  let password = document.getElementById("password").value; document.getElementById("password").value = "";
  try { const pending = api.connect(profile(), password); password = ""; const result = await pending;
    if (result.ok) await refresh(result.value.id); else showState({status:"error",code:result.code});
  } catch { showState({status:"error",code:"CONNECTION_FAILED"}); }
  finally { password = ""; button.disabled = false; for (const element of locked) element.disabled = false; for (const id of ["remove", "clear-auth"]) document.getElementById(id).disabled = !selected.value; }
});
api.onState(showState); void refresh();
