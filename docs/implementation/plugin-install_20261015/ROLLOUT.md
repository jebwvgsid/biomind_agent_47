# 尚未执行的上线步骤

本轮仅开发、构建、隔离验收和生产只读核验；下面全部 NOT_RUN，不能在未经授权时执行。

剩余一个运维交付步骤：安排获准维护窗口，完成真实目录接管、固定候选 release 激活，并在真实 CLI/Windows Studio 会话验收。它包含以下相互依赖的动作：

1. 以当前生产配置 agentDir `/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.biomind/pi-agent` 为范围，核实所有实际主机、入口、共享资源及在途任务。沿用 `scripts/plugin-maintenance.py audit --plan <审核计划>`，BUSY/不可见都不可假定为空闲。不自动停任务；生产预检只读证据是本任务 production-list.json，legacyUnverified=true。
2. 使用现有维护工具 fence/adopt/status（准确顺序和回退见 `scripts/plugin-maintenance.md`）。每个获准目录接管一次，之后日常安装只使用现有锁；目录或身份改变必须重新核验。激活必须来自本任务提交的干净构建，不带入技能 WIP，不把本次工具提交冒充当前生产 Main 提交。
3. 通过既有 release 交付流程将获准入口绑定到该构建；同一后端安装自包含适配包。核实 CLI 与实际 Studio profile、Main 会话的主机/uid/release/runtime/agentDir/cwd，并捕获加载和最终消息证据；Windows 客户端实操仍必须补测。仅关闭或重建获准的空闲会话进程；不默认重启整个 Studio 服务。

影响范围：被选定的用户级资源对读取该 agentDir 的所有项目会话共享；项目级仅对应获准项目，但资源锁可能包括共享依赖。不会迁移历史、记忆、认证或科研产物。

回退：激活前保存固定 release 与入口身份。若新入口验收失败，按已有维护事务 rollback/restore-entries 和 release 回退流程恢复精确旧入口；等待新会话正常退出，不直接覆盖有活动读取者的包目录。包本地引用可 remove，源目录和历史仍保留。禁止用删锁、伪造标记或恢复 JSON 来声称完整回滚第三方安装脚本。

远程分发是另一项尚未执行的发布工作：本任务没有 npm publish、git push 或远程可访问适配包来源。当前可交付实际 tgz + SHA256，复制到有权限的稳定目录后本地一条命令安装；不得把它宣传为已上线的远程 npm 一条命令安装。
