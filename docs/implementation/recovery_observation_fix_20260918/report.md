# session_1789728208：Recovery 观察结果未进入模型的回归

2026-09-18。针对用户“为什么 recovery 失效”的本地调查及回归修复。没有恢复 session、运行 WGS、调用真实模型/API、安装软件或 push；不修改原会话状态与失败证据。

## 已核实的因果链

1. 本次已经通过输入预检及参考索引准备。`map_s1` 在 18:49:15 返回 137，stderr 显示 BWA 被 Killed；不能把这一步的失败归为 Planner 准备延迟。
2. Recovery 实际执行了 6 轮模型调用和 9 次工具调用。8 条 exec_command 均成功，使用了明确 read_paths，没有因为缩小沙箱挂载而读不到指定日志。
3. 第 5 轮之前，已送入模型的观察包累计 94,388 字符，距离既有 96,000 字符上限只剩 1,612。新增的 timing 字段（扫描根、read_paths、时间戳等）占 25,009 字符；它们本应留在回执/F2/profiler，却被随命令结果完整送入 Recovery。这是上一轮性能修复遗漏的上下文联动。
4. 随后两次针对 resource_evidence.json 的精简读取均执行成功，stdout 分别有 2,330 和 1,534 字符，但模型只收到 `RECOVERY_OBSERVATION_BUDGET_EXCEEDED` 替代消息，回执 source_ref 也丢失。重复读取不会释放累计观察额度，因而又被计为两轮无进展，最终显示 `RECOVERY_NO_PROGRESS_LIMIT`，掩盖直接触发原因。
5. 用户 `/exit` 发生于 18:50:10，晚于 Recovery 18:49:56 的停止；用户退出不是这次 Recovery 自动停止的原因。不存在依据将其归为真实 API 超时。

BWA 被杀原因仍未确定：resource_evidence.json 记录 `root_cause_status=UNCONFIRMED`；已采样 cgroup 的 oom/oom_kill 增量均为 0，采样 RSS 峰值 377,072 KiB 不能排除漏掉瞬时峰值。自动 root_cause_analysis.json 却按 137 给出了 confirmed/resource_exhaustion；该标签缺少杀进程来源证据，不能当作已证实 OOM。还缺少 kernel kill sender、调度器终止原因和完整峰值记录。

## 修复

- `command_execution.command_observation_for_model` 仅移除提供给模型的 timing/activity_id；stdout/stderr、退出码、截断信息、主机/项目、receipt_ref 和日志引用保留。原始持久回执、F2 和 profiler 继续保留完整计时。
- 普通阶段共享工具入口、Recovery 观察包、跨阶段历史证据均使用这个边界，减少重复遥测占用，不缓存事实、不放宽沙箱。
- 真正触及累计观察上限时，停止原因明确记为 `RECOVERY_OBSERVATION_BUDGET_EXCEEDED`，保留成功读取结果的回执引用，避免继续调查两轮后误报无进展。单条结果需缩小时也保留引用。
- 清理上一笔隔离提交在单条/累计限制分支中重复插入的 externalized 回执赋值：这些分支可能没有 externalized 变量。实际工作区当时没有这两段重复代码，但干净检出的边界执行会受影响，因此同时修正提交版本并加入边界测试。
- 未提高字符、模型轮次、工具次数或超时上限。真正新观察的判定、审批和正式执行规则保持。

## 验证与限制

使用本 session 的实际 6 轮模型响应及 9 份原始工具观察，替换 provider/dispatcher 为纯内存回放，经过真实 RecoveryAgentLoop：

| 结果 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 工具调用数 | 9 | 9 |
| 实际到达的模型调用边界 | 6 | 7 |
| 观察累计字符 | 95,063 | 77,444 |
| 终止错误 | RECOVERY_NO_PROGRESS_LIMIT | 无 |
| 最后两次观察进入下一请求 | 否 | 是 |

重放使用测试 causal seed，外层 task/plan/step 字符长度与原 session 有小差异，故其计数不完全等于原归档。第 7 次响应是**模拟的最终决定**，仅验证接收/传递通路，不代表真实模型会作出相同决定，也不授权执行重试或证明 WGS 可成功。

新增回归覆盖：多轮大遥测不占观察额度、真正累计耗尽的准确原因及回执、单条结果缩小但无外置归档时的引用保留、共享阶段返回/历史中的模型数据与本地计时分离。当前工作区 75 passed、隔离提交版本 74 passed，均另有 8 subtests passed；见 test_results.json。

扩大检查还遇到 15 项既有 Recovery 旧测试失败（包括缺失 finish_reason 的旧 provider fixture 和旧软限制断言）；修改前快照复现了完全相同的失败集合，未改旧断言来使其通过。

原始证据根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789728208`。关键文件：context_trace/recovery_turn-0019-8c8bbd5d5d1a/request.json、command_inspection/92093b9cc3104760bb6c2561db54cbf4/receipt.json、command_inspection/0a4be2f1259340ebbbff4ee36ee4c6b9/receipt.json，以及 attempts/attempt_cc24fa476fd9dec2c18a/{stderr.log,resource_evidence.json}。

逐包字符统计见 evidence.json；重放结果见 replay_results.json。完整测试日志、修改前快照和离线重放脚本保存在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/recovery_observation_fix_1789728208`。本次未验证真实模型恢复或 BWA 被杀的外部原因。
