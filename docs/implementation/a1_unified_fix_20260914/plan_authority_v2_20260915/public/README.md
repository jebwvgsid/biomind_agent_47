# Public Planner contract and feedback

本分册限本轮直接活动的 Planner 请求、纠正反馈和执行步骤资源投影。它不声称完成全架构语义剥离，也不改变旧审计、旧失败或真实模型的历史材料。

新公开约定使用 `metadata.resource_contract_version=3`。模型在步骤上以 `resource_uses` 一次声明读取或变更；`resource_ref` 按正式 artifact handle 精确往返，`access`/领域 helper 的可选 `role` 保持原义。`outputs` 继续声明不可变产物生产，workspace 的状态变更事实继续独立。兼容读取合法 v2 原表达；v3 不接纳旧 `inputs` 或资源侧关联混写。

已有 TaskSpec output 的 `required`、路径策略与固定路径按精确身份读取并随审批冻结。公开 v3 materialization 只补未定的相对路径。新 `plan.*` 产物按自己的 artifact contract 处理；v3 未标可选时默认为必需，只有可选项需显式 `required:false`。Skill ID、capability ID、operation 与步骤实例 ID 分别说明；通用 Python/Bash 路径必须显式选择 `framework.generic.execute`。

反馈投影只对正式资源 validator 的精确公开路径保留候选自己写出的 `step_id`，包括不存在的值；不会先验证成功再回显。`not_evaluated`、null 和空语义值保留。其他内部 task/Plan/approval/attempt 身份以及同名任意嵌套字段继续经过原控制元数据过滤。失败 focus 与下一请求使用同一投影。

公开完整例子只描述“创建一份新合成记录并检查它是否可读”的任务。测试从真实 Planner request 提取例子原字符串，再交给真实 parser、准入和 PlanReview。独立完整图以空 Skill 选择执行两个真实 Python 小进程，计算可读性结果，再走证据、Commit 与 FinalAudit。provider 是明示的固定响应替身；该合成任务不证明真实模型规划能力或科学工具能力。

`runs/current04` 保存共享冻结导出 bf53d050… 的首轮结果：公开组件 12 PASS；四图均 FAIL。旧引用纠正图在第一次拒收后构造 focus 时，被总 LLMPayload scanner 再次拒绝公开 `actual.step_id`；另三图在空 Skill 池的旧 Planner 早退处结束，Planner provider 调用数为 0，因此没有测试到原样例子或第四请求目标。组件通过不能覆盖这些真实接缝。

在此证据后，scanner 的窄修复复用同一正式投影决定，只豁免两处 Planner focus 中的精确公开引用位置，并增加真实 LLMPayload/兼容视图及四类非法控制字段反例。根代理负责空 Skill 池早退。

`runs/current05` 的 44073bcd… 导出已有 13 组件 PASS；纠正图取得实际第二请求中的旧错误引用，随后以新 v3 候选完成 8 次审批、执行、Commit 和 FinalAudit `task_verified`。这证明了固定模型与明示合成科学替身下的真实接口链，不是实际科学验证。

同轮原样公共例、空 Skill 完整执行例及两个第四请求例仍 FAIL：新 v3 Plan 尚被旧 `batch_id`/`workflow_stage_ids` 必填要求拒收。后两例的第三回合测试断言因此没有见到预期 unknown-capability 反馈，不能登记为预算误停。原样例不补无意义空字段；根代理协调 v3 将不用的 workflow 字段改为可省，显式提供时保留类型/合法引用校验，v2 不改变。修后结果见后续冻结导出，历史失败保持原样。


`runs/current06` 保留三类不同结果：13 公开组件与真实首轮 STOP 共 14 PASS，原样例子到真实审批待定 PASS；空 Skill 执行图因新增测试驱动错误寻找 `approved.sh` 而非正式 `approved.py` FAIL，目标小进程尚未运行；第四请求两图发现正式 machine ABI 丢弃 capability 诊断字段，目标第四回复尚未到达；八步纠正图为测试 watchdog 180 秒超时，最终加载回执缺失，不能视为生产停止或 PASS。下一候选修复了公开 ABI 和新增驱动的真实批准引用接线，没有给例子补空 workflow 字段。

`runs/current07` 使用冻结导出 `a913b08a…`，已落盘 13 公开组件、真实首轮 STOP、原样例子审批待定、空 Skill 两步 Python 完整图、第四次合法候选及第四次正式 STOP 的 PASS 回执。完整例从正式持久化回调取得 `approved.py` 引用，验证脚本内容哈希、批准哈希和实际包装器哈希后由真实 executor 执行；无科学就绪预置。第四请求例检查真实第三请求中的 unknown-capability 值与可用列表，第四个完整候选到达审批待定；独立 STOP 例保持非成功终端，二者均未发第五请求。八步纠正图的首次运行遭外部工具宿主中断，缺退出回执，原因 UNKNOWN；新隔离目录 `public_correction_retry01` 重跑取得 PASS，完成 8 次真实审批、执行和 Commit，FinalAudit 为 `task_verified`。共 19 个唯一 pytest ID 在独立 run 中 PASS，不能描述成一次合跑。原 14 场终端保护矩阵也在该冻结候选上独立全部 PASS，见 `pty/current07/evidence.json`。每场父子 harness 同哈希，117 个实际加载模块与 manifest 一致，实际正文 SHA256、Plan 版本、submission、owner 与正式回执逐项保留。该旧 UI harness 固定整段 LLM 调用，只验证真实 UI/PlanReview/CAS；provider 叶子传输由前述新图另行覆盖。

Git 边界：`planner_context.py` 的 provider projector 基础整体来自受保护的先存 overlay，本轮修改落在其内部。当前运行候选可测试，但不能把旧函数骨架无声纳入本轮提交。可分离逻辑与测试已随 `fda82d91a12c4965723dc8482529a0991788d59b` 提交，提交未改变被测源码字节；该受保护函数内的本轮差分保留为精确 patch，未收编旧骨架。最终以根账本记录的提交范围和 overlay patch 为准。

07→08、07→09 的实际加载依赖与选定测试逐文件同字节证明分别保存在 `runs/current07/current08_dependency_reuse.json`、`runs/current07/current09_dependency_reuse.json` 及 `pty/current07/` 同名文件。新增测试文件或显式非 autouse 插件不在这些调用的加载边界内。复用不会把 07 的实载哈希改写为 08/09 的新实载回执。

旧 F2 三载体组件图亦完成 3/3 PASS，见 `carrier_regression.json`：native Python 与 legacy Python 实跑 current09，legacy Bash 实跑 current10，均取得 135 个实际加载模块与各自导出字节匹配。只读明确的新合成输入，并经过真实批准文件、helper、executor、证据、gate 与 Commit。native 使用 snpeff 领域能力，两个 legacy 使用 WGS inspect；三者均不进入 current10 新修的 generic-language-mismatch 分支。current10 后续复用以根最终 AST/调用边界账本为准，不把前两项改写成 current10 实跑。
