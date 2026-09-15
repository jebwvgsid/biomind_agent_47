# 阶段 A：逐请求 Token 与压缩应用审计

## 这部分证据说明了什么

主会话 `session_1789471955` 的近 300 万 Token 是大量重复携带恢复证据和压缩请求的实际已返回 usage 累计；没有证据支持把它解释为一次巨大请求、科学分析量或重复汇总造成的虚高。已知总量 **3,071,038**，其中 Recovery 诊断 **1,683,961（54.83%）**，压缩 **900,143（29.31%）**，两者合计 **84.14%**。

压缩确实调用了模型，也得到很多分段摘要；但**没有一个压缩事务通过最终协议核验并激活新 epoch**。前两事务各自耗尽两次最终方案提交机会，继续放行一次未压缩的安全范围内诊断请求；下一轮又超过软阈值，再做整套压缩。第三事务在分段摘要阶段被用户取消。不能把 43 次压缩物理请求误称为 43 次独立压缩失败。

主账本：[request_ledger.tsv](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/session_1789471955_phase_a_20260915/request_ledger.tsv)；汇总：[usage_summary.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/session_1789471955_phase_a_20260915/usage_summary.json)；原始证据索引、逐消息组成、拒收回执和离线验证：[compaction_evidence.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/session_1789471955_phase_a_20260915/compaction_evidence.json)。

## 1. 账本口径与物理请求可见性

主会话共有 79 个 trace invocation：

- 72 个带唯一 `provider_attempt_id` 的物理 attempt 记录：71 completed、1 cancelled；所有已捕获 ordinal 为 1，未观察到应用层传输重试。SDK 内部重试不可观测，各阶段配置不同，见下文。
- 6 个 completed 调用有响应快照与 profiler usage，却没有统一物理 attempt 记录：初始 router 1、Supervisor 5。
- 取消后追加的 `task_router-0079-fe0d6a64b302` 是 failed 逻辑 trace，不能凭 trace 就确认新的物理 HTTP 请求。
- 77 个 completed usage 与 profiler 一一对应；已提供 attempt usage 的 71 个还逐项核对输入/输出/total 相等。

每行保存逻辑调用 ID、可用的物理 attempt ID、SDK 重试可见性、开始/结束、模型/节点/步骤、usage/cache、请求及响应路径与哈希。`lc_run--...` 是 SDK 回调标识，不冒充服务端 HTTP request ID。`sdk_internal_retry_observable=false` 的场景不写“确定无隐藏请求”。

SDK 策略按阶段区分：5 个 Consultant attempt 的 manifest 与日志均写 `sdk_max_retries=1`（例如 `logs/biomind_1030526.log:432`）；Planner/Generate/Recovery/compactor 已捕获 attempt 均写 0。账本并列保留日志值、行号；核对后 `sdk_policy_conflicts` 为空。**不能概括为所有调用都关闭 SDK 重试**，不能凭配置或无应用层 retry 就断言 SDK 实际没有重试，也不能据此凭空加算隐藏 usage。

只把每个返回 usage 计算一次。`manifest.annotations.provider_attempt`、`events.jsonl`、phase archive 的同一 usage 是重复视图，不相加。`0078` 取消时没有 usage；`0079` 失败也没有 usage，账本留空/UNKNOWN，不填 0，不计入已知总量。**3,071,038 是可核验的已知量，不能声称涵盖未返回的计费。**

| 调用类型 | 已知 usage 调用数 | prompt | completion | total | 缓存命中率 |
|---|---:|---:|---:|---:|---:|
| Router | 1 | 933 | 101 | 1,034 | 82.32% |
| Consultant | 5 | 91,128 | 5,391 | 96,519 | 81.61% |
| Planner | 3 | 94,980 | 11,505 | 106,485 | 63.61% |
| Generate | 6 | 160,750 | 6,827 | 167,577 | 1.91% |
| Supervisor | 5 | 110,345 | 4,974 | 115,319 | 9.40% |
| Recovery 诊断 | 15 | 1,678,449 | 5,512 | 1,683,961 | 68.86% |
| Context compactor | 42 | 822,567 | 77,576 | 900,143 | 0.762% |
| 合计 | 77 | **2,959,152** | **111,886** | **3,071,038** | **44.3024%** |

缓存命中 **1,310,976**，未命中 **1,648,176**。77 行的状态都是 `partially_reported_derived`：缓存命中 telemetry 从 provider/SDK 响应得到，未命中按 `prompt - hit` 推导。42 条压缩完成事件还保留了 `usage_metadata.input_token_details.cache_read`。汇总层写 `reported` 不等于每个字段都是服务端原始返回值。实现位置：`execution/profiling.py:185-239`。缓存 token 已包含在 prompt 和 total 内，不再加一次，也不能从 total 扣掉后称为“实际使用”。没有历史价格或账单，不推算费用。

## 2. 三个压缩事务：生成、拒收、应用分开核验

所有时间为 UTC；当地时间加 8 小时。下表区间按首个 provider request_started 至最后一个 attempt_completed，未含事务开始后首次分段准备时间；不能拿此时长替代整段 Recovery 耗时。

| 事务 | 调用范围 | 构成 | 已知 total | provider 活跃秒之和 | 首末 attempt 墙钟秒 | 结果 |
|---|---|---|---:|---:|---:|---|
| `cmp-a615...` | 0034–0046 | 11 次 map + 2 次最终方案 | 314,541 | 149.321 | 200.059 | 两次方案拒收，未应用 |
| `cmp-ef1b...` | 0048–0063 | 14 次 map + 2 次最终方案 | 351,407 | 169.494 | 236.722 | 两次方案拒收，未应用 |
| `cmp-c13b...` | 0065–0078 | 14 次 map：13 完成 + 1 取消 | 234,195 | 131.274（含取消已观察段） | 220.684 | 尚未到最终方案，未应用 |

42 个 `recovery_compaction_completed` 是单次 provider 调用完成；不是 42 个成功激活事务。四次最终方案的拒收有不可变归档和响应原文双证：

1. `0045`：`mode=normal` 却摘要 10 个 `selected=true` 单元。回执：`COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE`。
2. `0046`：已经改 `capacity_pressure`，但多输出顶层 `selection_rationale_note`。回执：`CONTEXT_SCHEMA_UNKNOWN_FIELD`。
3. `0062`：`material_retrieval_state` 条目缺少 `source_unit_id`。回执：`CONTEXT_FIELD_REQUIRED`。
4. `0063`：反馈已指出 `source_unit_id`，条目仍缺这个字段，仍同错。不能把这两次解释为 provider 超时。

完整拒收证据：

`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/phase_archive/objects/12/12c7ee9faf7d5a727d8491f3d178e449e449f3316bb22e4088cbf51954d54090.json`

该对象 `summary.context_compaction_failures` 累积两个事务的两次方案拒收，均 `CONTEXT_COMPACTION_ATTEMPTS_EXHAUSTED`，均 `fallback=safe_uncompacted_request`。首个边界档案 `objects/40/40f7d892140310009b47d701c5adf46db79e4806db53390be5a9bd79be235f08.json`；第一次失败后的档案 `objects/a9/a9c7df6d459a125782929c0af6587b8c9b504a80573ea3146056bdc4785a61c0.json`。

**未应用的四重证据：**

- 该 task 的 phase archive 索引无 `recovery-context-epoch` 候选归档；有 6 个普通 recovery episode 档案。
- 终态 `plan_state.task_state` 无有效 `recovery_context_compaction` active pointer。
- 所有 15 个业务 Recovery 请求仍在各自 seed 的 `context-epoch:0`，没有压缩后的新 epoch。
- `0047` 的前 13 条消息与 `0064` 的前 13 条 JSON 消息值完全一致；后者只是追加 assistant 和 tool 消息，未注入上述压缩摘要。实际 provider prompt **178,311 → 185,407**，并没有下降。

因此这里不是“摘要成功应用后又被旧上下文覆盖”；观察到的原因是**最终候选从未通过准入，按未压缩 fallback 继续**。不能只用“epoch 没变”作唯一证据，必须结合拒收回执、active pointer 和后续消息原文。

## 3. 输入为何增长：递归携带 causal_history 的原文

四个 Recovery episode 首次实际 provider prompt 为 **61,868 → 111,065 → 146,341 → 178,311**。最后两个请求的内部预算使用 `cl100k_base_reference`，估算与 DeepSeek provider usage 不同，不混成同一种 token。软触发阈值 170,000，safe_input 187,712，目标 130,000；第四 episode 初始预算 173,076，尚非 hard block，因此失败后允许未压缩调用。

| 首次请求 | messages content 字符总和 | 其中 causal_history 消息字符 | 历史 body 的 parent_evidence 字符 |
|---|---:|---:|---:|
| 0015 | 194,453 | 无 | — |
| 0024 | 347,860 | 100,481 | 30,757 |
| 0029 | 459,551 | 208,404 | 180,285 |
| 0047 | 558,696 | 300,088 | 288,631 |

`0029` 的历史 `parent_evidence` 内含上一代 `causal_history`，条目长 100,502 字符；`0047` 又包含 208,425 字符的上一代历史条目。连同 validation、execution、script、completion、semantic_decision 原文，再封装为新历史，构成已经观察到的递归增长。不是仅凭字符串反复出现猜测。当前实现 `agent_core/workflow/inspection_evidence.py:415-428` 明确将 `causal_history` 也加入 `parent_evidence`。历史证据必须保持可读可追溯，但每个新请求把所有祖先原文嵌套重发并非必要。

最后业务请求 `0064` 共 16 条消息、content 总计 578,165 字符：system 8,254；seed 22,014；Skill 35,394；其余 10 条 inherited evidence 中 causal_history 300,088、plan 85,508；新增 assistant 100 + tools 17,980、1,389。详细每条消息标签、opaque ref、字符数、哈希见 `compaction_evidence.request_message_indexes`。字符统计不包括完整 HTTP wrapper/tool schema，不能代替 provider tokens。

## 4. 最后请求与原文位置

原文已经存在于 task 的 `context_trace/<call_id>/request.json`，本审计提供绝对路径和 SHA256 索引，不执行其中任何命令：

- 最后业务诊断：`recovery_turn-0064-99a0cdf59b59`，仍旧 epoch，是第 15 个 Recovery 调用；此后只有压缩调用，没有新的业务生成。
- 取消当时最后请求：`schema2_context_compaction-0078-5cbfacffc6f4`，map 请求、2 条消息、26,062 content 字符，usage UNKNOWN。
- 取消后退出输入相关逻辑 trace：`task_router-0079-fe0d6a64b302`，1 条 human 消息、2,996 content 字符，failed；物理 dispatch/usage 不可由快照证明。退出行为的详细时间由主审计等待章节核对。

上述 `request.json` 是捕获的 provider-visible message 快照，不是服务端原始 HTTP 抓包；provider/model 参数与策略另见 manifest，未知字段不补造。

## 5. 已证原因、推断与最小修复切片

**已证原因：** inherited evidence/causal_history 原文递归增长；四次最终压缩方案违反实际接收协议；两个事务失败后均放行一个未压缩请求，重新超过触发阈值后又重新做 map；第三轮被取消，历史不存在成功应用压缩的证据。

**有直接代码支持、仍需修复设计确认的因素：**

- 压缩 system prompt 只枚举顶层字段，描述 retrieval entry 时列出 ref/hash/coverage/skill_id，却未给完整内层结构。validator 要求 `source_unit_id/residency/reason/summary/missing_details/retrieval_ref` 等字段；历史 `0062/0063` 恰缺首个必填字段。这支持“发送/接收协议未完整对齐”的修复方向，不证明模型一定能因补 schema 就成功。
- `_build_compaction_request` 将所有 selected inherited descriptor 都标成 `selected_skill_material`；本次 11 个单元含 Skill 1、普通恢复证据 10。normal 模式的 selected 摘要限制因此覆盖所有 evidence，需校正类型与保留策略，不能为通过而移除安全准入。
- `recovery_node.py:2070-2106` 的“一次 fallback”对每次失败都重新授予；业务请求后重新触发时仍处于 epoch 0，累计 map 工作重新展开。已有每事务两次最终计划预算，但缺少适合这个历史链路的累计成本/重复失败收敛约束。

最小切片候选（本轮不实施）：

1. 由同一 typed schema 生成最终压缩输出协议和每条 field-path 错误反馈；内层 retrieval state 完整可见；针对这四份真实响应离线回归，保持 fail-closed。
2. 在保留历史 opaque refs 与可逆读取的前提下，消除 causal_history 的递归原文嵌套；正确区分 Skill material 与普通 diagnostic evidence。验证覆盖/事实可恢复性，避免只按字符裁剪。
3. 以 seed/source 内容哈希复用已验证 map 结果；对同一事务链设置累计压缩调用/耗时/成本上限和重复失败终止语义。触发软阈值与 hard block 分开，保留有效的 archive→CAS 激活底座。
4. 统一补齐 Router/Supervisor 的物理 attempt 和 cache provenance；取消/失败保留 UNKNOWN，汇总显示已知量与缺失量。

## 6. 复用、离线验证与对照边界

复用既有 `docs/fixes/biomind_context_continuity_compaction_20260906.md` 指向的 source-unit、范围 map、两次方案重试、archive/CAS 和 fallback 基础。旧文中的 fake compaction 成功不能证明本次成功，也不沿用旧会话事故结论。

本次 stdlib 提取脚本做必要增量验证：77 行双源 usage 与缓存恒等式、物理 ID 唯一性、42 次 compactor usage 事件去重对账、13 条旧前缀不变、无 active pointer/新 epoch。另从当前 `context_compaction.py` AST 隔离提取三个纯数据校验类及其纯 helper，**未导入 BioMind、未实例化 provider/archive、未执行历史分析代码**；对 `0046/0062/0063` 的实际响应复现相同错误码/路径，`0045` 通过实际 parser 后核对生产 selected/mode 拒收谓词。此复现说明当前纯 parser 与历史回执相符，不声称替代整个在线事务。

对照 `session_1789464978` 单列 [control_request_ledger.tsv](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/session_1789471955_phase_a_20260915/control_request_ledger.tsv)：14 条 profiler usage 合计 343,462，另 terminal_summary-0015 manifest 明确返回 4,276、未纳入该 profiler 汇总；故对照已知总量 **347,738**，最后一条 cache 未保存，缓存率只对有 cache 的 14 条计算。主会话没有这种 completed usage 漏行。这个差异只说明对照的统计缺口，不能拿它解释主会话近 300 万。

所有已读证据文件的绝对路径、bytes、SHA256 收在 `compaction_evidence.source_files`；phase archive 专项列表为 `phase_archive_objects_read`。仍缺真正 HTTP 请求/账单、0078/0079 的未知 usage、未捕获 SDK 内部请求信息。没有通过真实重跑补历史。
