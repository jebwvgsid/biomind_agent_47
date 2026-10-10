# 旧 Pi 资源目录一次性接管

此流程只增加既有资源锁的接管凭据；包声明、来源、pin、filters、trust、包文件、原生会话、记忆不迁移、不复制。Pi 仍为 0.87.1。已有 worker 未持新锁时，空锁不证明空闲。

## 维护计划

`plugin-maintenance.py` 读取权限 0600 的 JSON；schema 为 `BioMindPluginAdoptionV1`。必要字段见 `agent_core/pi_plugin_adoption.py:load_plan`，交付目录另附真实路径草稿。

- `agentDir`、`cwd`、`scopes` 是现有 Pi 资源目标。
- `legacyCodeRoots` 包含实际旧 CLI、S2、服务器代码根；`sessionRoots` 列出原生 writer 所在目录。
- `hosts` 必须覆盖实际运行/可能启动这些入口的全部主机。SSH 只用现有 BatchMode 信任。不要把容器内 `/proc` 当作主机视图。
- `entries` 是**业务/包目录之外的正式启动入口**。入口 SHA 来自 `entry-identity --entry ...`。已有 CLI 符号链接由围栏暂时代替，但目标源文件不动。旧服务管理器仅阻止后续 ensure/run，不停止已有服务。
- 不经过这些入口的直接 Python/Node/Pi 启动须在维护期间停用；这是正式入口协调边界，不是操作系统沙箱。
- 审核 `entryInventoryConfirmed`、`hostInventoryConfirmed`、`maintenanceApproved`，只有实际确认才设 true。程序不会替用户自动确认未知入口/主机。
- `scheduler: slurm` 会拒绝任何该用户的在途作业，保守地要求一次空闲维护窗口；不硬编码作业号/计算节点。非调度环境才使用 `none`。
- 未分类 Python/Node 进程须人工确认无关，再将审计输出的完整 PID/startTicks/executable/commandSha256 条目填入对应 host 的 `reviewedUnrelatedProcesses`。已识别的 BioMind、服务器或资源引用不能豁免。进程重启后身份改变须重新检查。
- 审核完成后冻结计划。事务开始后改计划会被拒绝；不要编辑事务 JSON。确需改范围时先恢复旧入口，使用新的 stateDir 和新的计划。
- `stateDir` 独立私有目录，存放入口备份和事务记录，不是插件注册库。不要删除它；已接管 worker 会验证它。

## 执行顺序

以下 `$plan` 是已审核计划的完整路径；`$maint` 是本次固定 release 的脚本路径。必须在真实服务主机执行。

1. `python "$maint" audit --plan "$plan"`：只读主机/作业/writer 核查。BUSY 不授权强停。当前已有任务正常完成后才进入维护。
2. `python "$maint" fence --plan "$plan"`：先保存全部入口备份，再原子写维护围栏。中途错误可重试 fence，也可 restore-entries；不会重写旧业务代码或包目录。
3. 在工作台确认无运行/排队/等待授权任务，关闭空闲 owner；按现有运维方式正常退出旧服务。**工具没有“杀全部进程”或自动停服务操作。** 围栏状态下旧窗口仍可能连接正在运行的旧服务，所以该服务必须正常退出。若任一任务未结束，保持等待或 restore-entries 取消窗口。
4. `python "$maint" adopt --plan "$plan"`：在既有 gate/resource 独占锁内，重新执行所有主机审计和 Slurm 查询。BUSY/不可读/未知主机均在资源写入前拒绝。通过后仅写接管标记和外部事务记录。
5. `python "$maint" status --plan "$plan"` 必须 committed；可选 `activate` 只把计划内 CLI 改到已审核固定 release/config，旧服务管理器仍保持围栏。新工作台使用新服务路径/配置启动。
6. 用 `verify-studio-core.py` 检查实际远端 profile + 包接口身份，再恢复已有会话、执行 Windows 验收。核验成功不等于已执行真实模型或 Windows 测试。

## 失败与回退

- pending / RESOURCE_ADOPTION_RECOVERY_REQUIRED：新 worker 明确阻塞。解决磁盘/权限等原因后 `repair --plan ...` 会重新审计、核对声明和围栏、继续提交。包和配置没有被接管工具改写。
- ADOPTION_SETTINGS_CHANGED / ADOPTION_MARKER_CHANGED / ADOPTION_ENTRY_CHANGED：保留现场，拒绝覆盖新内容。设置变更可 `rollback` 后重新拟订计划；标记/入口被第三方改写须先核实冲突，不自动覆盖。
- `rollback --plan ...`：需没有持新共享锁的 worker，只恢复本次接管凭据，保持入口围栏。不会“还原”并覆盖用户新 settings、包版本、会话或产物。
- `restore-entries --plan ...`：仅 fencing/fenced/rolled-back 可用，恢复准确的旧符号链接或入口字节。旧服务按原配置正常 ensure，Windows 选旧配置。已 committed 必须先等待新任务正常完成并 rollback。
- 事务/配置提交失败时，文件锁随实际进程退出释放；持久 pending 标记使后续加载不能假就绪。禁止删除锁文件或伪造 born-guarded 标记绕过检查。
- 实际包操作失败使用已有 `biomind plugins --json` 的 repair，接管 repair 只恢复接管事务，两者不混用。

目录接管完成不代表旧源码已可删除。S2 退役仍需独立检查所有消费者；不在本流程内自动删除。
