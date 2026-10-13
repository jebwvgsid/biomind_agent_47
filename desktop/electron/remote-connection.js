"use strict";
const api = window.bioMindConnection, $ = id => document.getElementById(id);
const fields = ["label", "alias", "host", "user", "sshPort", "localPort", "identityFile", "jump", "servicePath", "serviceConfig", "remotePort"];
let profiles = [], busy = false, pendingFocus = null;
const messages = {
  WORKBENCH_RENDERER_STOPPED: "工作台界面进程已退出，远端会话仍保留。点击连接可重新加载界面，不会重发任务。",
  WORKBENCH_LOAD_FAILED: "工作台页面加载失败。点击连接可重试；若反复出现，请保留 diagnostics.jsonl 以便排查。",
  WORKBENCH_UNRESPONSIVE: "工作台界面暂时没有响应。远端任务继续由服务器持有，点击连接可重新加载界面。",
  disconnected: "未连接", connecting: "正在验证 SSH 连接…", "service-pending": "Slurm 作业排队中，尚未就绪…",
  "service-starting": "服务器服务正在初始化…", "server-starting": "正在检查工作台身份和健康状态…", ready: "已连接 BioMind 服务，正在打开工作台。",
  reconnecting: "连接中断，正在恢复服务连接；不会重发任务。", error: "连接失败",
  installing: "正在安装服务管理脚本并校验服务器配置…", configured: "服务器配置已保存；点击连接后发现或启动服务。尚未就绪。",
  INSTALL_REQUIRES_DISCONNECTED: "请在连接工作台之前进行安装配置。",
  INSTALLER_FILE_CONFLICT: "该服务脚本目录已有不同版本。请为此版本选择新的服务管理脚本目录。",
  PRIVATE_DIRECTORY_OR_FILE_REQUIRED: "服务配置必须保存在仅当前用户可访问的独立目录中。",
  WORKBENCH_BACKEND_BUILD_REQUIRED: "工作台后端尚未完成构建，请先完成本次后端构建。",
  COMPLETE_BIOMIND_MAIN_REQUIRED: "此目录缺少完整 BioMind Main 或已验证的适配入口。",
  INVALID_WORKBENCH_PASSWORD: "首次配置请设置至少 16 字节的工作台口令，不能包含换行。",
  SERVICE_ACTIVE_CONFIGURATION_LOCKED: "已有服务仍运行或状态待确认，未改写服务器配置。",
  SECURE_STORAGE_UNAVAILABLE: "系统加密存储不可用。可取消保存口令后连接，本次不会写入口令明文。",
  SAVED_CREDENTIAL_UNAVAILABLE: "无法解密已保存口令，请重新输入。", INVALID_SERVER_PASSWORD: "请输入服务器工作台口令。",
  SSH_HOST_KEY_REJECTED: "SSH 主机信任校验失败，请核对 Windows 的 known_hosts 记录。",
  SSH_AUTH_FAILED: "SSH 身份验证失败，请核对用户名和私钥。", SSH_UNAVAILABLE: "系统 OpenSSH 不可用。",
  SSH_HOST_NOT_FOUND: "无法解析 SSH 服务器。请核对 Windows SSH 配置中的别名；本次部署使用 yongjiusuo。也可以选择主机地址与用户名。",
  SSH_CONNECTION_TIMEOUT: "SSH 连接超时，请检查服务器地址、网络或 VPN。",
  SSH_CONNECTION_REFUSED: "服务器拒绝 SSH 连接，请核对地址与 SSH 端口。",
  SERVICE_NOT_INSTALLED: "该路径尚未安装服务管理脚本。请展开“首次安装”，载入部署预设并核对，然后点击“安装服务管理脚本并保存服务器配置”。",
  CONNECTION_PRELOAD_UNAVAILABLE: "桌面连接模块未加载。请关闭并重新打开程序；若仍失败，请保留此错误码。",
  CONNECTION_UI_ERROR: "连接界面发生异常，请关闭并重新打开程序；此时不能据此判断服务器状态。",
  CONNECTION_FAILED: "连接操作未完成，请重试并检查下方错误码。",
  INSTALLATION_FAILED: "安装操作未完成，服务器配置是否写入尚未确认，请检查错误码。",
  SETTINGS_LOAD_FAILED: "无法读取已保存的连接配置，请重试或重新打开程序。",
  SETTINGS_SAVE_FAILED: "连接配置未能保存，请检查错误码。",
  PRESET_LOAD_FAILED: "部署预设未能载入，请重试。", KEY_SELECTION_FAILED: "无法打开私钥文件选择窗口，请重试。",
  DISCONNECT_FAILED: "断开操作未确认，请检查连接状态。",
  INVALID_SSH_HOST: "请填写 SSH 别名，或服务器地址与用户名。", INVALID_SSH_USER: "请填写有效的 SSH 用户名。",
  INVALID_SERVICE_PATH: "服务管理器和配置都需要服务器上的完整 Linux 路径。", INVALID_PORT: "端口范围无效。",
  LOCAL_PORT_IN_USE: "Windows 本地端口已被占用，请选择另一个端口。", SERVER_AUTH_FAILED: "工作台口令不正确。",
  SERVER_ID_CHANGED: "服务器身份与保存记录不一致，连接已停止。", INVALID_SERVER_PROFILE: "端点没有返回有效的 BioMind 身份。",
  SERVICE_CONFIGURATION_ERROR: "服务器服务配置不可用，请核对配置路径与安装。", SERVICE_FAILED: "服务器启动失败，请检查服务日志。",
  SERVICE_READY_TIMEOUT: "服务器仍未就绪，请检查排队和初始化状态。", SERVER_READY_TIMEOUT: "工作台健康检查未通过。",
  CONNECTION_CANCELLED: "连接已取消。", CLOSE_WORKBENCH_TO_SWITCH_SERVER: "请先关闭当前工作台，再切换服务器。",
};
function message(text, kind = "info") {
  pendingFocus = null;
  for (const id of ["status", "action-status", "install-status"]) {
    const element = $(id); if (element) { element.textContent = text; element.dataset.kind = kind; }
  }
}
function focusField(id) {
  const element = $(id);
  if (element.disabled) { pendingFocus = id; return; }
  pendingFocus = null; element.focus(); element.scrollIntoView?.({ block: "center", behavior: "auto" });
}
function status(state) {
  let text = messages[state.status === "error" ? state.code : state.status] || `状态：${state.code || state.status}`;
  if (state.status === "error" && /^[A-Z][A-Z0-9_]{1,95}$/.test(state.code || "")) text += ` [${state.code}]`;
  if (state.serviceCode) text += `（${state.serviceCode}）`;
  if (state.jobId) text += ` · 当前作业 ${state.jobId}`;
  if (state.retrying) text += "；软件会继续重连。";
  message(text, state.status);
  if (state.status === "error" && ["INVALID_SERVER_PASSWORD", "INVALID_WORKBENCH_PASSWORD", "SERVER_AUTH_FAILED"].includes(state.code)) {
    focusField("password");
  }
}
function failure(error, fallback) {
  // Never echo an IPC stack, entered password, path or arbitrary rejection text.
  const candidate = error?.code || error?.message;
  status({ status: "error", code: typeof candidate === "string" && /^[A-Z][A-Z0-9_]{1,95}$/.test(candidate) ? candidate : fallback });
}
function result(value) {
  if (!value || value.ok !== true) throw Object.assign(new Error("CONNECTION_OPERATION_FAILED"), { code: value?.code || "CONNECTION_OPERATION_FAILED" });
  return value.value;
}
function requireAPI(method) { if (typeof api?.[method] !== "function") throw new Error("CONNECTION_PRELOAD_UNAVAILABLE"); }
function revealInvalid(element, detail) {
  for (let parent = element.parentElement; parent; parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true;
  message(detail + " [FORM_VALIDATION_FAILED]", "error");
  element.focus(); element.scrollIntoView?.({ block: "center", behavior: "auto" });
  return false;
}
function validate(action) {
  const managed = $("service-mode").value === "managed", alias = $("ssh-kind").value === "alias";
  const selected = ["label", "sshPort", "localPort", "identityFile", "jump", ...(alias ? ["alias"] : ["host", "user"]),
    ...(managed ? ["servicePath", "serviceConfig"] : ["remotePort"])];
  const required = new Set(["sshPort", "localPort", ...(alias ? ["alias"] : ["host", "user"]), ...(managed ? ["servicePath", "serviceConfig"] : ["remotePort"])]);
  if (action === "install") {
    if (!managed) { message("首次安装需要选择“自动发现和启动 BioMind 服务”。", "error"); return false; }
    for (const id of ["backendRoot", "mainRoot", "runtimeConfig", "sessionRoot", "pythonPath", "nodePath", "uploadsRoot", "projectRoots", "password"]) { selected.push(id); required.add(id); }
    selected.push("modelRecordPath", "dataRoots");
    if ($("start-mode").value === "slurm") selected.push("partition", "slurm-time", "cpus", "memory", "account", "qos");
  } else if (action === "connect") selected.push("password");
  for (const id of selected) {
    const element = $(id), value = element.value.trim();
    const label = element.labels?.[0]?.textContent.trim().split(/\n/)[0] || element.closest("label")?.textContent.trim().split(/\n/)[0] || id;
    if (required.has(id) && !value) return revealInvalid(element, `请填写“${label}”。`);
    if (element.getAttribute("type") === "number" && value) {
      const number = Number(value), min = Number(element.getAttribute("min")), max = Number(element.getAttribute("max"));
      if (!Number.isInteger(number) || number < min || number > max) return revealInvalid(element, `“${label}”需要 ${min}–${max} 之间的整数。`);
    }
    if (element.validity?.valid === false) return revealInvalid(element, `请检查“${label}”的格式或长度。`);
  }
  return true;
}
async function action(method, text, fallback, work, validation) {
  if (busy) return;
  message(text, "working");
  let locked = [];
  try {
    requireAPI(method);
    if (validation && !validate(validation)) return;
    busy = true;
    locked = [...document.querySelectorAll("input,select,button,textarea")].filter(element => element.id !== "disconnect").map(element => [element, element.disabled]);
    for (const [element] of locked) element.disabled = true;
    $("connection-form").setAttribute("aria-busy", "true");
    await work();
  } catch (error) { failure(error, fallback); }
  finally {
    for (const [element, disabled] of locked) element.disabled = disabled;
    if (locked.length) { busy = false; $("connection-form").setAttribute("aria-busy", "false"); $("remove").disabled = $("clear-auth").disabled = !$("saved").value; }
    if (pendingFocus) focusField(pendingFocus);
  }
}
function modes() {
  const alias = $("ssh-kind").value === "alias", managed = $("service-mode").value === "managed";
  $("alias-field").hidden = !alias; $("host-field").hidden = alias; $("user-field").hidden = alias;
  for (const element of document.querySelectorAll(".managed-field")) element.hidden = !managed;
  $("remote-port-field").hidden = managed;
  for (const element of document.querySelectorAll(".slurm-field")) element.hidden = $("start-mode").value !== "slurm";
}
function input() {
  const p = Object.fromEntries(fields.map(k => [k, $(k).value.trim()]));
  if ($("ssh-kind").value === "alias") { p.host = ""; p.user = ""; } else p.alias = "";
  if ($("service-mode").value === "managed") delete p.remotePort; else { delete p.servicePath; delete p.serviceConfig; }
  for (const k of ["sshPort", "localPort", "remotePort"]) if (k in p) p[k] = Number(p[k]);
  const prior = profiles.find(x => x.id === $("saved").value);
  if (prior && ["alias", "host", "user", "servicePath", "serviceConfig", "remotePort"].every(k => (prior[k] || "") === (p[k] || ""))) p.serverId = prior.serverId;
  return p;
}
function fill(id) {
  const p = profiles.find(x => x.id === id) || { sshPort: 22, localPort: 30142, remotePort: 30141 };
  for (const k of fields) $(k).value = p[k] ?? "";
  $("ssh-kind").value = p.host ? "host" : "alias"; $("service-mode").value = p.id && !p.servicePath ? "manual" : "managed";
  $("password").value = ""; $("password").placeholder = p.remembered ? "已加密保存，留空复用" : "请输入服务器工作台口令";
  $("remove").disabled = !id; $("clear-auth").disabled = !id; modes();
}
async function refresh(id, populate = true) {
  requireAPI("list");
  const value = result(await api.list());
  profiles = value.profiles;
  $("saved").replaceChildren(new Option("添加服务器", ""), ...profiles.map(p => new Option(p.label, p.id)));
  $("saved").value = id ?? value.lastId ?? "";
  if (populate) fill($("saved").value);
  status(value.state);
}
window.addEventListener("error", () => failure(null, "CONNECTION_UI_ERROR"));
window.addEventListener("unhandledrejection", event => { event.preventDefault(); failure(event.reason, "CONNECTION_UI_ERROR"); });
$("saved").addEventListener("change", () => fill($("saved").value));
$("ssh-kind").addEventListener("change", modes); $("service-mode").addEventListener("change", modes);
$("start-mode").addEventListener("change", modes);
$("focus-password").addEventListener("click", () => {
  message("请在“工作台口令”框填写口令。首次安装请自行设置至少 16 个英文字母、数字或符号；已有服务使用安装时设置的同一口令。它不是 SSH 登录密码。");
  focusField("password");
});
$("load-preset").addEventListener("click", () => action("preset", "正在载入部署预设…", "PRESET_LOAD_FAILED", async () => {
  const value = result(await api.preset()), p = value.profile, setup = value.setup;
  if (value.schema !== "BioMindStudioSetupPresetV1" || !p || !setup) throw new Error("INVALID_SETUP_PRESET");
  $("saved").value = "";
  for (const k of fields) $(k).value = p[k] ?? "";
  $("ssh-kind").value = p.alias ? "alias" : "host"; $("service-mode").value = "managed";
  for (const k of ["backendRoot", "mainRoot", "runtimeConfig", "sessionRoot", "pythonPath", "nodePath", "uploadsRoot", "modelRecordPath"]) $(k).value = setup[k] || "";
  for (const k of ["projectRoots", "dataRoots"]) $(k).value = (setup[k] || []).join("\n");
  $("start-mode").value = setup.mode;
  for (const k of ["partition", "cpus", "memory", "account", "qos"]) $(k).value = setup.slurm?.[k] ?? "";
  $("slurm-time").value = setup.slurm?.time || "";
  modes(); message("已填入本次服务器预设，请核对 SSH 别名、路径与启动方式，并设置工作台口令。尚未安装或启动服务。");
}));
$("install").addEventListener("click", () => action("install", "正在提交服务器安装配置，等待服务器校验；尚未就绪…", "INSTALLATION_FAILED", async () => {
  const setup = Object.fromEntries(["backendRoot", "mainRoot", "runtimeConfig", "sessionRoot", "pythonPath", "nodePath", "uploadsRoot"].map(k => [k, $(k).value.trim()]));
  if ($("modelRecordPath").value.trim()) setup.modelRecordPath = $("modelRecordPath").value.trim();
  for (const k of ["projectRoots", "dataRoots"]) setup[k] = $(k).value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  setup.mode = $("start-mode").value;
  if (setup.mode === "slurm") {
    setup.slurm = Object.fromEntries(["partition", "memory", "account", "qos"].filter(k => $(k).value.trim()).map(k => [k, $(k).value.trim()]));
    if ($("slurm-time").value.trim()) setup.slurm.time = $("slurm-time").value.trim();
    if ($("cpus").value) setup.slurm.cpus = Number($("cpus").value);
  }
  const value = result(await api.install(input(), setup, $("password").value));
  await refresh(value.profile.id, false); status({ status: "configured" });
}, "install"));
$("save").addEventListener("click", () => action("save", "正在保存连接配置…", "SETTINGS_SAVE_FAILED", async () => {
  const value = result(await api.save(input())); await refresh(value.id, false);
  message("连接配置已保存。口令在连接成功后按所选方式保存。");
}, "save"));
for (const [id, method] of [["remove", "remove"], ["clear-auth", "clearAuth"]]) $(id).addEventListener("click", () => action(method, "正在更新保存的连接配置…", "SETTINGS_SAVE_FAILED", async () => {
  result(await api[method]($("saved").value)); await refresh();
}));
$("select-key").addEventListener("click", () => action("selectKey", "正在打开私钥文件选择窗口…", "KEY_SELECTION_FAILED", async () => {
  const value = result(await api.selectKey()); if (value) $("identityFile").value = value;
  message(value ? "已选择 SSH 私钥文件。尚未连接。" : "已取消选择私钥文件。");
}));
$("disconnect").addEventListener("click", async () => {
  message("正在断开桌面连接…", "working");
  try { requireAPI("disconnect"); result(await api.disconnect()); status({ status: "disconnected" }); }
  catch (error) { failure(error, "DISCONNECT_FAILED"); }
});
$("connection-form").addEventListener("submit", e => {
  e.preventDefault();
  void action("connect", "已收到连接请求，正在验证配置并连接服务器…", "CONNECTION_FAILED", async () => {
    const value = result(await api.connect(input(), $("password").value, $("remember").checked));
    $("password").value = ""; await refresh(value.id);
  }, "connect");
});
$("connection-form").addEventListener("invalid", event => {
  event.preventDefault(); revealInvalid(event.target, "此字段未通过格式校验，请检查后重试。");
}, true);
modes();
void (async () => {
  try { requireAPI("onState"); api.onState(status); await refresh(); }
  catch (error) { failure(error, "SETTINGS_LOAD_FAILED"); }
})();
