/** Pi native tools; only bash has a thin optional per-call runtime reference. */
export const NATIVE_TOOLS = ['read', 'write', 'edit', 'bash', 'ls', 'find', 'grep'] as const;
export function nativeTools(names: readonly string[]) {
  if (!names.length || names.length === 1 && names[0] === 'maintenance_submit') return [...names];
  return [...new Set([...names.filter(name => !['rg', 'request_path_access'].includes(name)), ...NATIVE_TOOLS, 'run_command'])];
}
export const NATIVE_RULES = `当前 execution:native。普通 read/write/edit/bash/ls/find/grep 使用 Pi 原生工具及当前账号权限，不提供额外目录沙箱；跨目录普通操作无需 BioMind 文件、目录或命令审批，不要求计划或输出合同。
相对路径按工具 cwd 解析，shell 的 cd 仅影响该次调用；目录变更不改变项目、资源加载、会话或记忆归属。
probe_command 有时间/输出预算，但不保证只读；不得将权限参数描述为沙箱保障。`;
