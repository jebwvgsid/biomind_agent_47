/** Pi native tools; only bash has a thin optional per-call runtime reference. */
export const NATIVE_TOOLS = ['read', 'write', 'edit', 'bash', 'ls', 'find', 'grep'] as const;
export function nativeTools(names: readonly string[]) {
  if (!names.length || names.length === 1 && names[0] === 'maintenance_submit') return [...names];
  return [...new Set([...names.filter(name => !['rg', 'request_path_access'].includes(name)), ...NATIVE_TOOLS, 'run_command'])];
}
export const NATIVE_RULES = `当前 execution:native。普通 read/write/edit/bash/ls/find/grep 使用 Pi 原生工具及当前操作系统账号权限，不提供额外目录沙箱。跨目录普通操作无需 BioMind 文件、目录或命令审批，不要求计划或输出合同；目录变更不改变项目、资源加载、会话或记忆归属。
相对路径按工具 cwd 解析，shell 的 cd 只影响该次调用。裸 bash 使用原生宿主环境，带 binding_ref 的 bash 仅绑定该次调用；用户 ! 保持原生行为。probe_command 有时间/输出预算，不是强制只读。安装仍须原安装意图确认；native 不把权限参数伪装成沙箱保障。`;
