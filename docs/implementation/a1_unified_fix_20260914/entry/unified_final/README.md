# F4/F5 统一候选复验

- [F4 与旧输入/审批保护](regression_evidence.json)：final01，49 PASS，包含 6 个真实 F4 图分支与 43 个未修改旧保护断言；六份 F4 回执各有 94 个实际加载模块，全部匹配 manifest。
- [F5 完整终端矩阵](final02_pty/evidence.json)：final02，14 PASS；全部为实际内核 PTY，包含 plain 模式，每场 117 个已加载模块与 manifest 相符，父/子测试 SHA 同为 `cba6d096ef71ecf860491a0f15a43cca24f63d76a6a1ccb7743c3cffc0980538`，整个导出运行前后无变化。
- [旧 CLI 公共调用方补验](final04_cli/verification.json)：final04，25 PASS，包含 24 项原 V2 display 断言与 1 项原 model-progress 测试（四模式）；这是额外组件/事件接线回归，原 49/14 计数不变。
- [final05 受影响组件重验](final05_cli/verification.json)：model-progress 的 1 个原测试、四模式再次 PASS。其余 F4/F5/display24 的 [有界源码与调用证明](candidate05_reuse.json) 保留历史加载 hash，未冒称重新执行。
- [final06 当前复用证明](candidate06_reuse.json)：重新核对 F4/F5/display24，并证明 final05 model-progress 组件没有进入唯一变化的候选进展函数；实际加载与运行版本继续分别登记。
- [final08 F5/旧 CLI 当前复用证明](candidate08_display_reuse.json)：报告 origin 新分支只处理无 packet 的 Generate 边界，14 场 F5 及旧 CLI25 均未进入。F4 不在此复用结论内，由主账本在 final08 重跑 49 项。
- [版本对应](final01_final02_equivalence.json)：343 个生产/公开资源逐一全同；final02 仅改变父测试第二次输入的同步与元数据，并新增本组未调用的 F2 测试。
- [final03 helper scope 复用证明](helper_scope_reuse.json)：相对已测 final02，生产变化仅 `step_effects.py` 的 scope 检查及其辅助函数；其他实际加载模块、13 个入口片段和该模块其他函数全同 hash。14 场实际图更新止于 PlanReview，F4 仅走 AskUser/终态，均未进入变化函数的 Generate 调用路径。
- [保留的 final01 PTY 结果](final01_pty/evidence.json)：包含一次父注入器拿旧 ready frame 确认新输入导致的 rc=-15，以及父端限定等待新 pending_id 后的成功结果。生产正确拒绝早到 Enter，未修改生产确认保护。之后 14 个场景已全部在 final02 重跑。

每场 `result.json.gz` 包含完整冻结正文和实际 view、输入动作时刻、正式 receipt；`terminal.raw.gz` 保留原始终端流；`durable.json.gz` 是该次全新合成任务的持久状态。压缩文件和解压原文的 SHA 均登记在 evidence。没有原 session、真实 provider/API 或正式科学数据。

本组没有 Generate 派发，provider 仅 Consultant/Planner，执行次数及代码审批数为零。后续仅涉及 `_assert_helper_scope` 的 F2 修订没有进入本组 PlanReview/AskUser 调用边界；保留 actual loaded 的 final01/final02 hash，后续精确源差异由统一主账本说明，不把后来的文件 hash 写回本次动态记录。
