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
- 非登录 SSH 环境没有 Slurm PATH 时，在计划中设置 `schedulerExecutable` 为本集群经核实的 squeue 绝对路径。各宿主执行同一只读查询；路径缺失/不可执行与查询失败分别报告，不静默改用另一命令或跳过 Slurm。不需要在代码写死集群路径或 source 用户 shell 启动文件。
- 代码路径、解释器及 BioMind 命令标记仅表示关联待核实，不能直接认定占用目标资源。人工通过真实入口、运行配置/agentDir、项目和共享锁边界确认无关后，才可将完整 PID/startTicks/executable/commandSha256/bootId 身份条目纳入对应 host 的 reviewedUnrelatedProcesses；工具不会自动生成或批准豁免。实际目标资源引用（含 maps/environment）、不可读字段和身份变化始终阻塞，精确复核也不能覆盖。无直接引用不等于无关或空闲。重启、换命令或宿主重启后必须重新复核。
- 系统自身不可转储的单个 SSH、systemd PAM 或钥匙环辅助进程另有严格的 `reviewedSystemHelpers` 复核记录：必须人工核实并绑定 host/boot/PID、内核凭据及 UID 映射、命令哈希、启动身份、cgroup、精确父链和正常系统服务来源，附核验文件 SHA256。每次审计重新取得这些事实并匹配完整指纹；工具不自动批准记录。SSH/PAM 必须匹配未 exec 的 fork 与可信系统服务父链；钥匙环必须同时匹配标准 D-Bus transient unit、实际 secrets owner PID、系统只读程序及唯一 IPC_LOCK 文件/进程能力。用户 transient unit 本身不要求 root 所有，但不能单独证明身份。仅在该 PID 的 cwd EACCES 路径使用此证据，不重试失败字段；其余可见 fd/maps/environment 与 argv 资源引用始终优先阻止接管。未读字段在输出中仍明确未读，所有子孙继续逐个核查。其他不可读或身份变化仍 BLOCKED。
- 审核完成后冻结计划。事务开始后改计划会被拒绝；不要编辑事务 JSON。确需改范围时先恢复旧入口，使用新的 stateDir 和新的计划。
- `stateDir` 独立私有目录，存放入口备份和事务记录，不是插件注册库。不要删除它；已接管 worker 会验证它。
- 为保留 Windows 已保存连接的 servicePath/connectionId，原 `server-service.py` 入口可以显式配置 `replacement: {"kind":"service-manager","serviceRoot":"/固定服务提交/server","python":"/固定/python"}`。只接受提交目录名与 `SERVICE-RELEASE.json` 的 sourceCommit/root/status 一致且所有文件 SHA256 匹配的服务包，必须包含 `legacy_idle_stop.py` 等现有管理组件。重定向只完整转发参数和环境给固定管理器，不启动服务、不改配置或连接身份。没有该声明的旧管理入口保持围栏；原 CLI replacement 行为不变。

## 执行顺序

以下 `$plan` 是已审核计划的完整路径；`$maint` 是本次固定 release 的脚本路径。必须在真实服务主机执行。

1. `python "$maint" audit --plan "$plan"`：只读主机/作业/writer 核查。BUSY 不授权强停。当前已有任务正常完成后才进入维护。
2. `python "$maint" fence --plan "$plan"`：先保存全部入口备份，再原子写维护围栏。中途错误可重试 fence，也可 restore-entries；不会重写旧业务代码或包目录。
3. 在工作台确认无运行/排队/等待授权任务，关闭空闲 owner；按现有运维方式正常退出旧服务。**工具没有“杀全部进程”或自动停服务操作。** 围栏状态下旧窗口仍可能连接正在运行的旧服务，所以该服务必须正常退出。若任一任务未结束，保持等待或 restore-entries 取消窗口。
4. `python "$maint" adopt --plan "$plan"`：在既有 gate/resource 独占锁内，重新执行所有主机审计和 Slurm 查询。BUSY/不可读/未知主机均在资源写入前拒绝。通过后仅写接管标记和外部事务记录。
5. `python "$maint" status --plan "$plan"` 必须 committed；可选 `activate` 将计划内 CLI 改到固定 release/config，并激活显式声明的固定服务管理器重定向。未声明 replacement 的旧管理器保持围栏。管理器重定向保留原 Windows servicePath；服务重绑和启动仍须遵循现有维护流程。
6. 用 `verify-studio-core.py` 检查实际远端 profile + 包接口身份，再恢复已有会话、执行 Windows 验收。核验成功不等于已执行真实模型或 Windows 测试。

## 失败与回退

- pending / RESOURCE_ADOPTION_RECOVERY_REQUIRED：新 worker 明确阻塞。解决磁盘/权限等原因后 `repair --plan ...` 会重新审计、核对声明和围栏、继续提交。包和配置没有被接管工具改写。
- ADOPTION_SETTINGS_CHANGED / ADOPTION_MARKER_CHANGED / ADOPTION_ENTRY_CHANGED：保留现场，拒绝覆盖新内容。设置变更可 `rollback` 后重新拟订计划；标记/入口被第三方改写须先核实冲突，不自动覆盖。
- `rollback --plan ...`：需没有持新共享锁的 worker；先核对全部入口及保存的围栏字节，在同一维护锁内把已激活的 CLI/管理器重新围栏，再恢复本次接管凭据。重复调用保持幂等；未知入口修改在写前拒绝。不会“还原”并覆盖用户新 settings、包版本、会话或产物；后续 `restore-entries` 恢复准确的原入口。
- `restore-entries --plan ...`：仅 fencing/fenced/rolled-back 可用，恢复准确的旧符号链接或入口字节。旧服务按原配置正常 ensure，Windows 选旧配置。已 committed 必须先等待新任务正常完成并 rollback。
- 事务/配置提交失败时，文件锁随实际进程退出释放；持久 pending 标记使后续加载不能假就绪。禁止删除锁文件或伪造 born-guarded 标记绕过检查。
- 实际包操作失败使用已有 `biomind plugins --json` 的 repair，接管 repair 只恢复接管事务，两者不混用。

目录接管完成不代表旧源码已可删除。S2 退役仍需独立检查所有消费者；不在本流程内自动删除。

## 逐主机诊断（审计输出 V2）

`audit` 会尝试计划中的每个主机，即使前一个失败。`expectedHosts` 是计划范围，`checkedHosts` 是完成检查的主机，`incompleteHosts` 是无法完整检查的主机；`coverageComplete` 只指计划内检查是否完整，不代替 `hostInventoryConfirmed` 的清单审核。

各 `reports` 保留检查阶段、已观察到的旧进程、未知进程、writer 主机和 Slurm 作业 ID/状态/节点列表。`checks` 中 NOT_RUN 或 BLOCKED 不能当作空闲；未查到作业状态用 null，不能写 0。发现未列入计划的 writer 宿主明确 BLOCKED。Slurm 在途作业即使节点尚未分配也阻止接管。

`unreadableProcesses` 补充失败的 /proc 字段、errno 和已读到的 PID/startTicks/父 PID；不包含原始命令或异常文字。这些记录仍阻止接管，不能凭进程名称推断无关后直接忽略。`schedulerExecutable`、`schedulerPathSource`、`schedulerExitCode` 区分命令解析和实际调度器查询是否完成。

SSH 失败分别报告主机密钥未信任/已改变、认证、解析、超时、拒绝连接或网络失败；远端 Python 已执行但命名空间/Slurm/计划检查失败会保留对应代码。原始 stderr、banner、命令内容、环境变量、任务和凭据不输出。SSH 仍要求 BatchMode 和严格主机密钥校验。

任一主机不可见时总状态 `BLOCKED / ADOPTION_HOST_AUDIT_INCOMPLETE`（exit 2），保留其他主机 BUSY/clear 结果；完整检查发现占用为 BUSY（exit 3）。不能用删除主机、删除 writer、取消 Slurm 检查或伪造进程豁免来消除错误。只读 audit 不创建维护事务，发生审计错误无需先运行 repair；修好可见性后重跑即可。真正 adopt 仍在围栏及全部锁内进行全新审计，不接受保存的旧报告作为通行凭据。

维护工具可作为有独立版本、源码提交与 SHA256 的小型固定工具包交付；它复用同一源码中的 guard/adoption 模块和同一锁协议，不改变运行核心。此时 `$maint` 指向工具包脚本，计划的 `replacement.release` 仍指向已验证的服务核心。不能把工具包的提交号写成已连接 Main 的提交号。
# 2026-10-10：停止旧会话后的只读复核

`collect-maintenance-review.py --plan <draft.json> --output-directory <private-evidence-dir>`
会先运行当前审计，再用计划中 squeue 同目录的 scontrol 查询实际作业身份、
展开分配节点，并对包含这些节点的临时审查计划重新检查。临时计划的三个
确认/批准字段始终 false；它不能作为 fence/adopt 的批准计划。节点不可访问、
作业身份变化或查询失败会保留缺口，不会归类为“无人占用”。

审计新增 `processEvidence`：分别标明 cwd、命令参数、文件描述符指向代码还是
实际插件资源，提供解释器执行的脚本路径及其进程身份；不可读进程保留已取得
的身份和角色提示。角色提示不是豁免，代码路径引用不等于模型会话数。原有
未知项、不可读项、作业、writer 阻塞规则不变。诊断不输出完整命令参数、环境、
提示词、凭据或打开文件的具体名字，也不读取作业脚本内容。

用户明确允许停止旧会话后，可以使用独立的 reviewed-stop 工具结束它们；不必
等待它们空闲。该授权不改变主机清单、资源接管和正式入口切换的审核状态。

历史清单不自动证明某节点使用目标资源；应以目标入口、writer 声明、调度分配和真实资源绑定核定范围。缺少关联证据时保持范围 UNKNOWN，不自动删主机或请求无限重试。旧 worker 未加入 flock 协议时，空的新锁仍不是排他证明。
