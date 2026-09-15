# 主会话等待与取消退出审计（阶段 A）

主会话：`session_1789471955`。所有正文时间为 2026-09-15、Asia/Shanghai（UTC+08:00）。本切片只读取本会话历史证据和现有源码；没有启动 BioMind、导入生产模块、执行模型/API 或生信操作。本切片未用对照会话推导主会话行为。

## 这次后段在做什么

末次科学命令是 19:44:53 开始的第 4 次 `prepare_database`，记录执行耗时 2.110 秒、退出码 0。此后到 19:56:55 用户取消，没有新的科学命令回执。系统在同一 Recovery episode 中反复调用上下文压缩；压缩之间只有两轮 Recovery 模型请求和三个短时证据工具调用。后段持续消耗时间的主要区间是压缩调用及其本地准备，不能描述成“生信分析仍在运行”或“provider 挂了十多分钟”。

`context_trace/events.jsonl` 第 217 行 Recovery 开始于 19:45:16.398；profiler 对应 Recovery 节点持续 699.768 秒，直到取消。三组压缩包围区间共 669.334 秒，约占该节点时间的 95.7%。其中 43 个物理 compactor attempt 的已记录边界耗时合计 450.089 秒，其余包围时间包括本地准备/调用间空隙，不能进一步全部归为 CPU、锁等待或网络。

## 主要区间

| 起止时间 | 秒 | 核验解释 | 原始依据 |
|---|---:|---|---|
| 19:42:58.294–19:44:45.463 | 107.169 | 第 8 个输入请求，`code_review` 已可输入后等用户决定；不是 provider 或命令等待 | 主日志 16472–16473 |
| 19:44:53–19:44:56 | 2.110 | 最后一条科学命令；起止字段分别取 started_at/finished_at，耗时列采用独立记录值，不以秒级字段相减替代 | execution_records.jsonl 第 6 行 |
| 19:45:17.726–19:48:39.699 | 201.973 | 第一组压缩，13 个物理请求完成 | events.jsonl 218–282；cmp-a6155857… |
| 19:48:39.957–19:48:55.465 | 15.508 | Recovery 模型第 1 轮的外层边界，随后两个证据工具均很短 | events.jsonl 283–295 |
| 19:48:57.132–19:52:58.361 | 241.229 | 第二组压缩，16 个物理请求完成 | events.jsonl 296–375；cmp-ef1ba8fd… |
| 19:52:58.749–19:53:07.211 | 8.462 | Recovery 模型第 2 轮外层边界；随后 `skill.search` 为 0.070 秒 | events.jsonl 376–385 |
| 19:53:09.691–19:56:55.822 | 226.131 | 第三组压缩，13 个请求完成，第 14 个取消 | events.jsonl 386–454；cmp-c13b88d1… |
| 19:56:03.492–19:56:17.481 | 13.989 | 主日志没有行；前一 compactor 已完成，下一请求尚未开始，无法再细分本地原因 | 主日志 18479–18480 |
| 19:56:55.656–19:56:56.533 | 0.878 | caller cancellation 到任务取消事件持久化 | provider attempt 与 events.jsonl 455 |
| 19:57:06.030–19:57:10.758 | 4.728 | 取消后新的 `/exit\n` 输入误入 triage；该路由快照最终是 CancelledError | task_router-0079 manifest/response |

上述行存在包含关系，不可把秒数直接相加。完整 126 行区间表在 `wait_intervals.tsv`，包括 72 个有 provider-attempt telemetry 的请求、15 个 Recovery 工具、6 条科学命令、10 个输入窗口和主要日志空隙。后段科学命令结束至取消期间，没有长工具执行：全会话 15 个 Recovery 工具合计 1.071 秒，最长 0.218 秒。

### Provider 活动与“没有新事件”的区别

72 个有完整 attempt 边界的请求中，最长是 `schema2_context_compaction-0062`：19:52:13.594–19:52:38.605，25.011 秒；首 callback 在 3.269 秒，最终累计 21,006 个 content 字符，最大 payload gap 为 0.744 秒。主日志期间只记 `stream_active` 和完成，出现 21.627 秒无日志行，仍有 callback 汇总证明该时段包含持续返回数据。不能把这个无日志窗口当成无响应。

同一 telemetry 集合中，最大首 callback 等待 16.084 秒（Recovery 0018），最大 payload gap 2.666 秒（Recovery 0026）。这些字段是适配器观测，不是服务端内部推理时间；headers 在该适配器中为 `NOT_OBSERVABLE`。最后 compactor 的 total/idle/first-chunk timeout 配置均为 600 秒，但实际 2.011 秒就被 caller 取消，没有命中该超时。

`context_trace/events.jsonl` 是选择性事件流，不覆盖所有普通 Generate/Execute/人工确认。其 19:34:59–19:38:46 的 227 秒缺行、19:42:29–19:45:16 的 167 秒缺行，都可以由主日志中的活动解释，不构成死锁证据。13.989 秒本地无遥测窗口则保留为未知，未补造历史原因。

## 总时长统计口径

`run_metrics.json` 的历史 profiler 汇总为：

| 历史字段 | 值 |
|---|---:|
| total_wall_seconds | 1,381.699 秒 |
| human_confirmation_wait_seconds | 155.328 秒，7 次确认 |
| agent_active_seconds_excluding_human_wait | 1,226.371 秒 |
| command_execution_seconds | 8.066 秒，6 条命令 |
| llm_call_seconds | 760.682 秒，77 条 profiler LLM 记录 |
| 算术余额 wall − human − command − llm | 457.623 秒 |

余额不是“纯 CPU/无效等待”测量，而是该 profiler 口径外的残差，包含调度、状态、协议及其他未单列工作；LLM 记录也不等于本切片 72 个有 attempt telemetry 的集合。压缩、节点、provider 三种时间互相包含，不能累加。严格按主日志 `input_ready → decision_received` 量出的 7 个运行中窗口合计 154.281 秒，与 profiler 155.328 秒相差 1.047 秒，说明输入展示/收尾计时边界不同，不能假称逐毫秒完全一致。

任务开始前 initial prompt 的 79.565 秒，以及取消后请求 9、10 的 3.541 秒和 4.734 秒，单列于区间表，不计入上述任务运行确认耗时。

## 取消与退出的证据链

1. 最后原任务请求：`schema2_context_compaction-0078-5cbfacffc6f4`，attempt `provider-attempt-284b0e168be1-1-4d7dfac2`。19:56:53.646 启动；19:56:55.655690 请求取消，19:56:55.657145 cleanup 完成（1.455 毫秒）；19:56:55.657209 attempt 终态 `CANCELLED / PROVIDER_CALLER_CANCELLED / CLEANUP_CONFIRMED`。没有 content/reasoning/tool callback，`late_chunk_count=0`、usage `UNKNOWN`。不能将未知 usage 填为零。
2. 19:56:55.821911 `recovery_compaction_cancelled`；19:56:56.533231 `task_user_cancelled`，source `asyncio_cancel`、owner `interactive_user`、reason `CLI Ctrl+C`、command_started=false。state_generation 从 157 到 158。`run_metrics` status、final_result 均为 cancelled，ended_at 19:56:56。
3. 19:56:57.269 已回到 interactive prompt；因此取消传播并未长期卡在原任务 provider。后续 durable state 的 `runtime_state.status=cancelled`，但兼容顶层 `execution_status=in_progress`，存在读视图一致性风险；不能仅据旧顶层字段断言仍在运行。
4. 原任务取消后 19:57:03.076791，持久化的 U2 原始输入精确为 `/exit\n`（见 object `b3a4853b…` 的 raw_user_submission；head `6df0a206…`）。这是独立新交互，绝非最后 compactor 的迟到模型结果。
5. 19:57:06.030418 新 `task_router-0079-fe0d6a64b302` 请求形成，唯一 human message 为路由提示加原始任务、再追加 U2 `/exit\n`。其 response 是 `CancelledError`、manifest failed、完成于 19:57:10.758285。主日志 19:57:09.875 有 HTTP 200，但缺少可关联的 provider-attempt 身份；只能视为时间上与此新路由相符，不能强归给被取消的 0078 或断言远端最终完成。
6. 19:57:10.950 再回 interactive prompt，19:57:15.684 记录新的 decision_received 后日志结束。没有定位到操作系统退出码、shell 返回标记、终端关闭回执或全进程树清理回执。因此结论止于“原任务取消已记录；退出文本引发了额外路由；第二次输入后的 OS 退出行为不可核验”。

最后 compactor 的 `stream_close_supported=false`，没有 stream close 的请求/完成时间。`CLEANUP_CONFIRMED` 是本地 provider adapter 的清理结果，不能扩写成“远端模型已停止/无费用”或“整机所有进程已退出”。历史记录没有该 compactor 的迟到 chunk、进一步科学执行或对原任务结果的迟到覆盖；但日志观测终点之外不作保证。取消后的 conversation/state 写入属于 U2 新交互，不应自动称为迟到科学产物写回。

## `/exit` 的已证实原因与最小修复切片

现有 `bio_agents.py:54363` 先判断 `"\n" not in user_input`，再对 `user_input.strip().lower()` 匹配退出指令。实际输入 `/exit\n` 因尾随换行失败，落到普通任务入口（约 54682 行）。triage 约 31696 行再拼接持久化的全部 user turns；因此最终路由消息出现“原任务 + /exit”，不需要假设草稿污染。

纯离线见证从源码 AST 只提取该 `if` 的布尔表达式，不导入 BioMind。结果：`/exit` 匹配；`/exit\n`、`/exit\r\n` 均不匹配；真正多行 `task text\n/exit` 也不匹配。历史输入与此反例一致。重放脚本 `build_wait_evidence.py` 已执行通过，输出见 `evidence_wait.json.offline_exit_witness`。

建议独立修复：在控制命令入口对整个输入仅做首尾空白规范化，只有规范化后完整等于受支持控制指令时才处理；仍保留真正多行正文中的指令样本为普通文字。在分类之前处理退出，增加零模型调用的边界断言，并给退出写可核验的关闭结果。另一个独立小切片是让所有任务状态读视图采用 canonical runtime cancellation。上述建议均未修改生产代码，也不涉及前端开发。

压缩主因及“完成是否应用到后继 Recovery 请求”的判断由压缩应用审计切片负责，本等待切片只证明后段实际时间去了哪里，避免用 compaction_completed 直接代替 applied。
