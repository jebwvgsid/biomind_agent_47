/** Native tools are Pi's definitions, never BioMind file-operation overrides. */
export const NATIVE_TOOLS = ['read', 'write', 'edit', 'bash', 'ls', 'find', 'grep'] as const;
export function nativeTools(names: readonly string[]) {
  // Paused/failed startup and the bounded memory-maintenance phase stay bounded.
  if (!names.length || names.length === 1 && names[0] === 'maintenance_submit') return [...names];
  return [...new Set([...names.filter(name => !['rg', 'request_path_access'].includes(name)), ...NATIVE_TOOLS, 'run_command'])];
}
export const NATIVE_RULES = `当前 execution:native。普通 read/write/edit/bash/ls/find/grep 使用 Pi 原生工具及当前操作系统账号权限，不提供额外目录沙箱。跨目录操作无需 BioMind 文件、目录或命令审批，也无需 auto、计划、输出合同或 Generate 阶段。任务范围与用户停止条件仍须遵守；访问其它目录不改变当前项目、资源加载或记忆归属。
相对路径按工具 cwd 解析，shell 的 cd 只影响该次命令。普通短命令使用 bash；需持久日志、观察、取消及恢复的作业使用 run_command，不使用 nohup 或 & 冒充托管作业。同 call 重放查询原执行，结果未知时先对账，不自动重跑。
env_name 显式指定时复用原登记解析，缺失如实报告，不静默换环境。不默认运行 healthcheck；probe_command 在 native 仅有时间和输出限制，不是强制只读。安装工具仍须明确安装意图确认。
科学任务按需求确认输入、环境、Skill、参数和停止条件，可用 handoff_generate 交接；普通读改和开发任务直接执行，无需交接。Skills 由 Pi 在启动项目中发现，用 read 阅读正文和相对其 baseDir 的 references/scripts。读取外部文件不自动加载其扩展。更新计划是可选笔记，不是文件访问许可。
退出码0与文件存在不等于科学结果通过；要求的产物、格式、非空及科学检查仍须验证，缺少必要结果就未完成。输出只引用真实路径；区分原生文件不存在、OS 权限错误、结果不达标和运行结果未知。
开发修改默认按验证通过的逻辑单元创建本地 Git commit，保护原有 staged/unstaged/untracked 内容，只提交本轮范围。正常使用 Git 及现有身份、签名和 hooks；失败如实报告，不自行改全局身份或跳过 hooks。未经明确授权不 push、合并、发布或改写历史。只读任务不提交，科学数据目录不自动建仓库。`;
