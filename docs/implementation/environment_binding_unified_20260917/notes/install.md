# 安装边界与原科学步骤恢复

本子任务覆盖 B2/B3 的执行边界、父层目标验证与分阶段登记，以及 B1 工单接线和 B4 人工事件生产闭环。未安装、升级或修改真实软件环境；未调用模型/API、读取凭据或恢复科学 session。

## 实现

- `InstallerState` 与父 `AgentState` 使用同一 `installation_request` / `installation_receipt`；安装工单冻结 request/attempt、实际目标 prefix、当前 Skill/capability、来源、版本、影响、验证命令及所选 Skill 机器契约。不同目标使用不同 Installer checkpoint。
- 复用 `action_runtime.build_action_approval_identity` / `validate_action_approval_identity` 和真实 `HITLMiddleware`。禁用 Installer 的通用 safe 自动批准；`conda run` 也需明确批准。批准身份覆盖完整代码、目标/来源/版本/影响/验证、cwd 和进程 PATH 前缀。代码编辑必须形成新批准对象。
- 拒绝和取消记录原提案身份并终止；同工单不再次询问或执行。已完成命令按相同身份复用执行收据；登记重试不重装。原同步 router/finish 改为 async，安装子图完整调度不再为这些纯状态节点跨线程。
- `register_environment` / `register_tool` 标签只形成登记请求，不能写 registry 或宣称可用。严格来源政策来自冻结工单，不接受模型追加的 contract block。旧工单文本兼容仅取第一条人类工单。
- 父 `_install_subagent_impl` 限制当前 Step 的 Skill/capability、声明工具、禁止环境及实际 prefix；不会继承科学正文。先验证已有目标，必要时才调用 Installer；显式 `target_env` 和当前 capability 进入真实验证。结构化 UNKNOWN/AMBIGUOUS 保持诊断状态，不当作缺软件直接安装。
- `_record_installed_skill_environment` 在目标验证后分别执行 Skill/软件 registry 写入，保存每个 Skill 的独立结果，补偿时跳过已完成阶段。目标、attempt、完整 request hash、所有所需 Skill/capability 验证与两项登记都匹配后，Typed adapter 才给 `capability_available`。
- 安装完成调用统一 `_resume_after_runtime_preparation`；安装器不写科学步骤 `passed`，不接受科学 binding。空 required 的旧契约进入正式 Plan 修订；其他路径由共享 A3 准备新作用域。
- 工单通过共享 `runtime_generation_source_scope` 冻结 Plan/Step/epoch、批准范围和源 attempt 集合，并传入回执。已有工单不能重新盖上当前 scope；若后续科学 attempt/epoch/批准变化，父入口在任何探测或安装前拒绝旧工单。
- `resolution` 接受 `manual_wait`、`manual_done`、`provided_environment`、`registration_retry` 和取消/拒绝事件；人工完成仍强制复验。失败收据包含完整命令、cwd、白名单环境前缀、数值退出码、stdout/stderr、阶段，以及有证据的 permission/network/solver/build 分类；其他原因是 unknown。人工交接明确标注来源/solver/兼容性尚未核验，失败命令只是证据。
- B4 复用现有 `pending_question` 与 AskUser 的不可变用户回答链，明确展示中文解释及枚举；问题绑定完整工单 hash。TypedInstall 的 `user_input_needed` 进入 AskUser；明确回答形成 `resolution_event`，TypedAskUser 的 `installation_pending` 回原安装工单；自由文字继续咨询，不推测完成。换环境保留原科学 source scope，重建目标工单并复验；取消和拒绝停止。

## 验证

隔离候选使用 `tests/offline_managed_bootstrap.py`，新进程内合成配置、拒绝凭据与网络读取，仅允许 `/tmp` 写入。当前专项累计 **66 passed**；完整命令、逐例结果、候选与源码 SHA-256、共享方法 SHA-256 见 `../evidence/install_boundary_results.json`。完整日志见 `../evidence/install_boundary_test.log`。

验证使用真实 LangGraph / MemorySaver / HITL / 批准身份 / Typed outcome。父层自动安装用例只替换模型、用户响应、安装进程及健康检查进程，真实运行安装状态、目标核对、两处登记、持久化和统一恢复入口；还覆盖人工完成、旧 default 成功但新目标失败、登记失败只补登记、重复恢复、错误工具和 prefix、结构化探测保留非必需能力 UNKNOWN。

B4 用例真实构建 schema2 production graph，经过安装→AskUser 实际 interrupt→`Command(resume="manual_done")`→原安装工单复验与登记→共享 A3；夹具仅在这条被测链之前声明已选安装入口，在之后停止于独立科学 Generate 边界。另逐项覆盖六种明确用户事件、三种自由文字、问题/工单不匹配及后续 epoch 拒绝旧安装回执。探测执行边界使用当前 `_run_probe_driver` 接口；严格结果保留各项 reason_code，解析和 capability 选择为真实实现。

保留现有测试，更新 1 项 Installer 与 5 项 memory 测试的旧源码断言，使其对应新的唯一目标验证/登记/恢复职责；未删断言求绿、批量 skip 或替换被测核心为预设成功。

共同最终候选还需主任务的累计集成验收。本子任务的成功恢复科学 fixture 使用空 required 的正式修订分支；含非空 required 的 A3 新作用域、binding 接纳与 Commit 联合验收由主任务完成。本专项 66 PASS 不替代最终累计版本与最终 Skill revision 的联合验收。

## 安装入口补充修复（基于 2e70f28）

最终核查确认此前存在可达性缺口：required runtime 缺证据时 Generate 在模型调用前进入 Recovery，合法 UNKNOWN 最终 `unresolved`、已确认 UNAVAILABLE 最终 `dependency_blocked` 均停止，不能到达模型的安装工单协议。现在父 Recovery 只在这两种合法未派发停点，通过真实 `_load_request` 核验当前 episode/scope，再使用现有 AskUser 问题槽展示“提供已有环境／准备安装提案／取消”。提问前只保存工单提案；明确用户选择才生成同一 `installation_request`。UNKNOWN、检查失败及权限问题仍不能被称为缺软件。普通文字继续咨询，不自动安装。

`prepare_installation` 事件必须来自当前持久化问题回答；它仅允许进入 Installer 生成具体提案，来源、目标、版本和命令仍逐项 HITL。新增三例真实父子 LangGraph 中断/恢复覆盖批准、拒绝、取消；未批准时执行次数为零。这条真实链发现外层中断 checkpoint 的旧 receipt 会覆盖子图已落盘阶段证据，父层已改为优先读取严格匹配作用域的持久化 receipt。安装自身已消费的 A3 新 epoch 只通过共享 `installation_generation_replay` 做精确幂等续接；新 attempt、Plan 或批准变化仍拒绝旧工单。

环境选择文案和准入统一为当前配置管理根下的环境名；不支持的绝对外部 prefix、`base` 或无管理根的 registry 回退在探测与登记前拒绝。当前 Inspector 的实际 prefix 仍用于精确目标校验，没有扩展外部 prefix 的科学启动协议。

补充候选专项累计 **72 passed**，运行中源码不变；命令、方法/文件 SHA-256 与逐例结果见 `../evidence/install_dependency_entry_results.json`，日志见 `../evidence/install_dependency_entry_test.log`。实际 required missing→依赖选择→同 prefix 准备→A3 新 epoch→证据接受→科学批准/微型执行/Commit 的联合测试由 `test_unified_dependency_preparation.py` 单独记录，不把本专项边界通过替代最终联合回归。

## 最后权限与交互边界复核

独立对照审查发现安装禁令曾在只读复验和取消之前执行，导致任务声明 `forbidden_behavior.runtime_install` 后，“提供已有环境”也被误拒、取消被误报 blocked。已将该现有政策判断移到目标验证之后、真正进入 Installer 之前；`provided_environment`、`manual_done`、`registration_retry` 只复验/登记，不被安装禁令阻止；合法工单的取消/拒绝保持准确终态。目标、Skill/capability、来源 scope 和 Skill 的禁止环境约束仍保留。

另将已有执行收据的同工单问题限定为复验、补登记、人工等待/已有环境或退出；问题文本、展示选项与明确事件解析均不再提供 `prepare_installation`，未执行的工单仍可选择。失败后的同工单不清空 execution、不生成新 attempt、不重新运行已记录命令；不支持的回答继续现有咨询解释。

新增 7 项真实父层/AskUser 回归后，专项累计 **79 passed**；禁安装但可读操作的 5 类结果、真实安装仍被禁止的 0-provider/0-review/0-runner 边界，以及失败收据保持等值/不重执行均通过。命令、逐例结果和受测/工作区方法 SHA-256 见 `../evidence/install_policy_review_results.json`，日志见 `../evidence/install_policy_review_test.log`。最终全候选结果仍以主报告累计终检为准。
