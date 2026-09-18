# session_1789741049：Generate 询问与续行修复

生产提交：`155f20a4f3a735f3485398ed9bb965fac40ab3b1`。
基线：`5c95895114dc17f1b649255f13117c72c27edc7e`。
独立候选树：`c8c7ebe4016b350430f06dc471b154877dc9329d`，只包含本轮生产代码和测试变更；最终另附本文及验收回执。

本轮没有恢复原 session、调用真实模型/API、安装软件、运行真实输入预检或 WGS/BWA，也没有 push。这里的通过指离线软件行为验证，不能解释为原任务恢复成功或科学结果正确。

## 原因和生产修改

原会话前三步已提交。第四步 `confirm_ploidy` 是 `ask_user`，合法地没有执行 capability；Generate 在请求组装时却无条件调用执行资源解析，触发 `ACTION_CAPABILITY_SCOPE_MISSING`。同输入本地重建仍失败，最终退出，尚未进入该轮模型调用和 Recovery。这不是本次上下文额度、API 超时或 Recovery 轮数用尽导致的停止。其后终端摘要的连接失败是另一条失败记录。

本轮修改：

- Generate 对内置非执行动作使用真实的“不适用”资源核验状态。执行动作缺少 capability、显式未知 capability 仍拒绝；`ask_user` 不能获得执行脚本的权限。
- 按 task/plan/step/action 和问题内容生成稳定问题标识，并在原候选提交事务中保存问题。typed Generate 交付问题给 `ask_user`，清空执行动作。回答只经正式 `Command(resume=...)` 提交，按步骤和回答证据幂等推进；输入框草稿不会变成用户事实。
- 实际续行回归还暴露了 Plan impact 的缺口：修改后的计划没有新的编译、模型调用和审查提交回执，旧审查身份阻止了后继请求。现复用现有 Planner epoch、call claim、编译器、CAS 和 review submission；重新审批更新后的计划，保留已经完成的步骤。明确的空 Skills 列表保留其声明语义。
- impact 请求使用 Planner 客户端、输出策略和零 SDK 自动重试设置；复用原 epoch 机制，修正同一语义任务不能返还已消费请求，真实 TaskSpec 变化则按现有规则建立新 epoch，避免跨用户确认错误累计次数。无效修正的真实字段错误进入下一次 impact 请求，当前 epoch 额度不足则停止且落盘失败状态。
- 对已使用单引号 heredoc、但用 `"$PY"` 作解释器的候选，明确返回 `EFFECT_HELPER_PYTHON_EXECUTABLE_NOT_LITERAL`。保留已批准 Bash 方式，用明确 Python 解释器和标准输入 heredoc 修正；改成 Python block 仍被工具范围核验拒绝，并保留完整修正指导。未增加 shell 语义 binder，也未放宽资源、批准或结果核验。

此前提交 `81f783eb9b4da4d75d5d35d1279839d855167d01` 是基线 HEAD 的祖先。其 timing 留档、上下文管理和遥测分离修改未回退；没有把旧 200k 配置作为本次根因。

## 实际验证

全部测试使用隔离代码导出和 `offline_managed_bootstrap.py`：禁止联网、真实配置和原任务数据读取；模型为固定返回的替身，执行叶子只写合成观察。计划编译、动作接收、审批对象、候选材料化、typed 路由、步骤提交、用户中断/回答落盘均走真实实现。原始归档候选仅材料化，不执行。

独立提交候选上的 **16 项新增回归全部通过**，另有 **43 项 CLI/响应完整性回归全部通过**。包括实际终端模态中的待提交草稿保护、compact/verbose 事件选择和响应缓存完整性。

| 实际生产图场景 | Generate 请求 | impact 请求 | 执行叶子调用 |
|---|---:|---:|---|
| 三步提交、询问、提交“2倍体”、继续下一步 | 5 | 1 | 前三步各一次，后继一步一次 |
| 中断后重建生产图，复用离线 checkpoint 再回答 | 5 | 1 | 同上，没有重复前三步 |
| 变量解释器 → 错误 Python block → 合法 Bash 修正 | 7 | 1 | 被拒候选不执行，最终四步各一次 |
| impact 修改已批准完成条件被拒，修正后继续 | 5 | 2 | 前三步不重跑，后继一步一次 |
| 当前 TaskSpec 的恢复 epoch 已耗尽 Planner 额度 | 4 | 0 | 只执行前三步，禁止额外 Planner 请求并保存失败 |

正常续行中，初始 Planner 只调用一次；回答进入 Consultant 和后继 Generate 的实际请求。确认参数产生真实 TaskSpec 变化，新 impact epoch 消费一次请求，校正一次无效 impact 时消费两次。重新审查回执属于 task revision 2 / plan revision 2。两个实际 provider dispatch 离线测试分别返回成功和连接异常，均仅调用 Planner 替身一次，未调用普通客户端。

四份原始候选 `generate-0010-31848cdc55d1`、`generate-0011-53e340f7f456`、`generate-0012-7facbdfbed24`、`generate-0013-e850f35a6c9f` 已逐份重放材料化入口；原始 response SHA256 保存在压缩夹具中。合成生产图另验证了错误载体被拒后，准确反馈进入下一次真实请求组装。

扩展测试**不是全绿**，没有删掉失败用例或改写断言掩盖结果：

- 当前工作区扩展运行的 pytest 原始摘要为 `13 failed, 106 passed, 6 subtests passed`。这 13 个失败 nodeid 在修改前保存的同一工作区源码上全部复现。
- 独立候选树扩展运行摘要为 `16 failed, 105 passed, 15 subtests passed`。按 nodeid 去重后涉及 15 个失败入口，均在基线 HEAD 的相同旧测试上复现，失败入口集合完全一致。pytest 摘要包含子测试，不能与去重数混用。
- 两种基线的旧测试和 Skill 定义本来就不同：工作区中已有未提交的测试、协议及 Skill 改动。独立树没有把这些改动一并提交。详细入口、源码签名、原始摘要和对照结果见 `test_results.json`。

扩展运行针对前一独立候选树 `c4f22e39f0a6789e9e54f4352f7061611740072b`。随后仅校正 impact epoch 的语义切换计数边界，并重跑全部 16 项新增入口及 43 项 CLI 回归；没有把先前扩展运行冒充为最终树的全量运行。

## 文件与保护措施

生产修改仅为 `bio_agents.py`、`agent_core/step_effects.py`、`agent_core/workflow/execution.py`。测试、夹具、导出清单及本文档范围见 `file_scope.json`。提交按本轮开始时的文件快照提取增量，并单独在 HEAD 上验证；没有把用户原有同文件改动整文件加入提交。

提交时发现零字节 `.git/index.lock`，mtime 为 22:18:59，早于本轮 22:33:40 的初始索引快照，且没有可见持有者。确认索引仍与快照相同后，将旧锁保留为 `.git/codex-session-1789741049.index-lock.bak`。常规提交的索引刷新等待被取消后，使用已验证树创建本地提交；只更新本轮文件的索引项，不恢复任何工作区文件。

## 未验证项与复现

没有验证真实服务连通性、原 session 的真实恢复、真实科学输入、正式 WGS 结果或多进程崩溃恢复。中断测试是重建生产图并复用离线 checkpointer；没有声称覆盖所有存储后端。Plan impact 本轮完整生产图覆盖的是 `PATCH_CURRENT_STEP` 及其拒绝、额度分支，其他决策组合未逐一完整重放。CLI 验证使用终端组件及离线入口，没有启动真人交互会话。

执行 `bash docs/implementation/generate_interaction_1789741049_20260918/reproduce.sh <提交>` 可导出指定提交并运行新增回归及 CLI 回归。设置 `BIOMIND_EXTENDED_REGRESSION=1` 可重现扩展测试及上述旧失败；测试原始回执写入该脚本创建的 `/tmp/biomind-interaction-1789741049-*` 目录。
