# D3 / C3：Recovery 调查预算、重入与压缩

阶段 D 定点审计与 E 候选设计；未修改生产代码、配置、原测试或历史证据。当前调用路径以阶段 D 工作区字节为准，不以旧会话结论替代本次核验。动态结果由统一 guarded `/tmp` 导出运行生成；本节的“缺口见证通过”表示复现现状，不能视为修复通过。

## 1. 本次事故与预算的关系

`session_1789521763` 是一个 Recovery episode 的连续调查：6 次完成的模型响应、1 次取消的请求，9 次只读工具成功调用；全部完成响应的 `finish_reason=tool_calls`。没有提交或接纳 RecoveryDirective，也没有进入 revalidate 外循环。step_attempt 页面依次推进到 660/719 行，不能称为重复读取同一页或 7 次工具异常。

历史已核验 usage 输入 **496,449**、输出 **14,836**，已知合计 **511,285**；取消的第 7 次 usage **UNKNOWN**。输入占已知合计 97.1%。六次响应虽然含冗长自语，但大头是逐次重新提交的上下文；不能把约 51 万已知 Token 全归为长输出。现有 12 次模型、24 次工具上限均未达到；新页的 source/hash 属于新观察，因此连续无进展计数清零。新页不会清零模型或工具总次数，但当前 business loop 没有同因果累计 Token 上限。

| Recovery 请求 | 输入 | 输出 | 累计已知总量 | 工具数 | 响应字符 | 耗时秒 |
|---|---:|---:|---:|---:|---:|---:|
| 0016-c52435e83e2c | 59,155 | 7,676 | 66,831 | 3 | 30,946 | 37.362 |
| 0017-5490a078f86e | 75,366 | 2,401 | 144,598 | 2 | 9,734 | 15.529 |
| 0018-64a5c625e9a2 | 84,849 | 2,713 | 232,160 | 1 | 11,479 | 15.522 |
| 0019-a145e7a0fbe7 | 89,419 | 186 | 321,765 | 1 | 297 | 4.168 |
| 0020-ff6310d60249 | 92,507 | 146 | 414,418 | 1 | 135 | 6.481 |
| 0021-885358279950 | 95,153 | 1,714 | 511,285 | 1 | 6,709 | 11.672 |
| 0022-9ede006341a4 | UNKNOWN | UNKNOWN | ≥511,285 | 0 | 未形成完整响应 | 0.460，取消 |

历史依据复用 `/tmp/session_1789521763_recovery_loop_diagnosis.json` 的 rows、source_files 和原始 hash；其原文索引位于 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789521763/context_trace/` 对应各 `recovery_turn-*` 目录中的 request/response/manifest。最终第 7 次请求含 27 条消息、322,966 个 content 字符，并保留六轮普通 assistant content 中的 `<analysis>` 文本。该会话未发生压缩。以上历史统计未通过真实重跑补造。

## 2. 当前注册入口与预算调用图

```text
bio_agents.py:2922 创建 Schema2RecoveryNode
  ├─ pending_runtime_evidence_request
  │    recovery_node.py:1651 → bio_agents.py:19675 _recover_runtime_evidence
  │      → RuntimeEvidenceRecovery.recover/_recover
  │      → 已持久化 episode/actions/inspection/retry/deadline → 安全 inspector
  │      → evidence_ready 回同一 Generate；否则 typed runtime stop
  │      （此分支不调用 Recovery 模型，不进入下面 12-turn loop）
  └─ FailurePacket 主入口
       durable stop / project seed / revalidation_stop_reason
       → restore active compacted epoch 或 causal-parent receipt business_resume
       → RecoveryAgentLoop.ainvoke(budget_state)
          call_model：无进展 / tool / turn 上限 → capacity measurement
            ├─ should_compact → 节点归档 → prepare_compaction（独立 maintenance）
            │    → 候选校验 → archive + CAS 激活 → 原业务计数继续
            ├─ hard capacity block → typed inspection stop
            └─ provider adapter → provider physical attempts
                 → assistant checkpoint / usage / turn_count / strict reception
                 ├─ 合法 directive → archive → RecoveryRuntime.run → receipt CAS
                 ├─ 只读工具 → 每工具 preflight/count → observation checkpoint
                 │    → 新 source/hash 重置 no_progress → 下轮 call_model
                 ├─ 无提交/无效协议 → 至多一次纠正，再拒收
                 └─ CancelledError → 传播；不会形成正常 episode 返回
```

完整回路的生产所有者是 `agent_core/workflow/recovery_node.py`、`recovery_agent.py`、`recovery_runtime.py`；不需要另造 BudgetManager。单次请求传输由 `bio_agents.py:3551` 的 `_schema2_recovery_provider_invoke` 接入现有 `_ainvoke_llm` / provider_attempt；不要重复计上层 logical request 与其下层 physical attempt。

## 3. 当前计数、重置与持久化边界

| 机制 / 当前值 | 生产检查与计数点 | 重置 / 续接 | 适用范围与限制 |
|---|---|---|---|
| business model turns 12 | `recovery_agent.py:394,910,1177`；发请求前检查，完整返回后加一 | `ainvoke:1688` 从 budget_state 初始化；新页面不重置 | 完成轮次上限，不是物理请求或累计 Token 上限；取消/传输失败不加 completed turn |
| 全部只读工具 24 | `recovery_agent.py:1350` 起，每个工具 dispatch 前检查并加一 | `_resume_state` 保存；多工具轮逐个计，不按一轮一次 | 失败工具仍占次数；超出部分不执行 |
| 同规范化工具请求最多 2 | 同处 canonical `{tool,args}` 哈希；第 3 次 dispatch 前拒绝 | request count 续接；换 tool-call ID 不能绕过；换页属于不同参数 | 防止完全重复请求；并非新页面总量预算 |
| 连续无新观察 2 轮 | `recovery_agent.py:1580` 用 source_ref + content_sha256，且 error 为空 | 一轮中任意新成功观察使 no_progress=0 | 测的是“读到了新内容”，未证明该内容有助决策，更不等于科学进度 |
| 格式纠正 1 次 | `recovery_agent.py:1310`；轮次/tool/no-progress 剩余允许时纠正 | `format_correction_count` 保存在 resume/receipt | 纯正文不能无限自动纠正；`RECOVERY_RESPONSE_INCOMPLETE` 不允许纠正复用残片 |
| 单响应 128,000 字符接收上限 | `recovery_reception.py:14`；`recovery_agent.py:1197` 后检查 | 每响应独立 | 在完整响应接收后拦截；不能代替流中生成预算。流 / finish_reason / timeout 见 D4 |
| 上下文 capacity | `recovery_agent.py:922`、`context_compaction.py:470` | 每次派发前重新估算；压缩改变 epoch 和上下文 | 输入窗口容量，不是历史累积费用。output reserve 默认 8,192 是容量预留，不等于 provider 已配置输出硬上限 |
| loop transport | `recovery_agent.py:987–1095`；standalone 默认 1，生产 provider_invoke 由下层拥有重试 | `_resume_state.transport_attempt_count` 续接，但统计 adapter 调用 | 生产最多 2 个 application physical attempts 由 `bio_agents.py:3655,18204` 管；SDK 隐藏内部重试不靠 loop 次数证明 |
| 整段调查时间 / 只读工具等待 | `recovery_agent.py:1415` 直接调用 dispatch_full；异步返回直接 await，未加 loop 总 deadline | 普通 business resume 没有已用总时间计数 | 不等于 provider 单请求没有 timeout；不能用 provider deadline 或工具次数证明“调查+工具+压缩”总耗时已受限。该工具等待为静态边界，未执行真实慢 I/O |
| 接纳修复动作 2 次 / incident | `recovery_runtime.py:88,268`；budgeted actions 为 retry_step/revise_plan/run_diagnostic_probe/organize_result/revalidate | durable typed_recovery_budget/progress/receipts；同 decision 幂等 | 指令接纳预算，不会在此前的纯调查轮扣减；本次零指令不能触发该预算 |
| revalidate 无变化停止 | `recovery_runtime.py:59,73`，节点模型前调用 | attempt + validation_digest + gate + disposition 指纹；忽略新报告 ID 等表面变化 | 需要先前已接纳 revalidate；不把 receipt 缺失或 report_id 变化当科学新证据。V16 与 D2 合并 |

### 压缩已有独立 maintenance 预算，不能写成“压缩完全无上限”

`context_compaction.py:2809–2889 prepare_compaction` 当前真实实现：

- 本次事务候选修复 `max_attempts` 只允许 1 或 2；层级所有 map/selector 调用走同一个 budgeted_compactor。
- maintenance 限制为 `min(64, DEFAULT_HIERARCHICAL_MAX_MODEL_CALLS=256)` 个模型回调、`2 × effective_context_limit_tokens` 累计预留/实报 Token、300 秒累计事务时间。层级深度另有 16 上限。
- 每次调用前预留估算输入 + output reserve；usage 未返回时保留 reservation，状态 UNKNOWN，不当作 0。有正整数 total_tokens 时以实报替换预留。
- compactor await 有剩余 300 秒 `asyncio.wait_for`；失败/取消在 finally 更新耗时及调用记录。当前 maintenance 调用记录是 compactor callback 层，不是所有下层 physical attempt 的统一去重账本。
- `recovery_node.py:1803,1852,1996,2165` 将 maintenance 与 business resume 跨 epoch 传递、归档，CAS 成功后续接。压缩不会清零业务轮次/tool/repeat/no-progress。失败后仍安全的原上下文最多两次 bypass，每次仅一轮；硬 capacity block 直接 stop（`:2090`）。

因此新增需求是把这些既有计数联结为同因果累计资源视图，并完善 durable reserve/settle，而不是删掉已有 maintenance 重新实现一套。当前 business 与 maintenance 各有账本，但没有以同一累计 Token/总墙钟额度共同批准下一次调查、压缩和 retry。

### 普通取消与已经压缩 CAS 的恢复不能混为一谈

节点 `_restore_active_context`（`recovery_node.py:1126`）能从验证通过的 active epoch pointer/archive 恢复累计 business 状态；`tests/test_recovery_compaction_production.py:1086` 已有 crash-after-CAS 正例，恢复时不重播工具，最终 turn=2/tool=1。相邻因果续接可从匹配 parent packet / recovery decision receipt 取 business_resume（`:1766`）。

但是无 active compaction、无已接纳 directive receipt 的普通取消路径会从 `budget_state={}` 开始。Loop 在 provider await 捕获取消后传播（`recovery_agent.py:1038`），没有完成常规返回；节点没有把之前 assistant/tool checkpoint 事件重构成 budget_state 的通用路径。D3 run02 见证以同一 synthetic durable/seed 连续进入真实节点两次，每次先成功 inspect 再取消，观察到每次 2 个 provider 叶子调用、共 2 次 inspect、相同 epoch:0、PlanState save_count=0。这是 node/loop 的逻辑计数与工具重播边界；没有使用生产 transport adapter，因此**不能推出生产重复物理发请求或重复计费**，也不证明本次历史会话实际重入过。

生产 adapter 另有关键保护：`bio_agents.py:18351` 的 `recovery_transport_claim` 在同 semantic invocation / transport hash 已完成时直接回放 durable response（`status=completed`），不会新发物理请求；取消/耗尽也由现有 ledger/fence 管理。未来必须把该已有物理账本作为同因果累计成本的来源，而不能只累加重播后的 loop logical request usage。D3 的取消见证不替代 D4 的 adapter/claim/reload 动态验收。

### pre-generation runtime-evidence 分支

`runtime_evidence_recovery.py:44` 的 policy 默认上限：3 actions、2 inspections、1 transient retry、单检查 60 秒、episode 180 秒；max_llm_decisions=2/max_explanation_calls=1 是协议字段，此当前服务没有模型派发。episode identity 由 scope digest 生成（`:273`），更换请求 ID 不重开预算；持久化起点用于计算原始墙钟（`:342`），不是每次 recover 重新计时。

在每个 inspector dispatch 前（`:519–574`）检查 task 的可选 runtime_evidence_actions_remaining、原始总 deadline、action/inspection 上限；先 increment 与保存 started receipt，再用 min(单检查限额, episode 剩余) wait_for。取消留下已计数 started receipt，未到 lease 不能重复；只在 safe_repeat 且 retry 余额允许时将过期调用记为 interrupted_unknown 并重试一次。已有 `tests/test_runtime_evidence_recovery.py` 包含 changed_request_id、reopened_deadline、interrupted_unknown、dispatch_receipt 前置与 task budget 正反例；本轮仅定点复用源码/既有验证，不把未重跑的整文件宣称本轮通过。

C3 可以让其 episode/inspection receipts 作为共享资源视图的子项目；模型用量为“不适用/无模型派发”，检查耗时/次数仍计入。应沿用它现有授权、scope、dispatch receipt 与重试上限，不把该确定性服务改造成另一个模型调查循环。它不等同原 Execute 故障的 FailurePacket 入口。

## 4. 离线见证与复用边界

新见证位于 `../offline_witnesses/test_d3_budgets.py`。真实边界包括生产 RecoveryAgentLoop / strict reception / Schema2RecoveryNode / PhaseArchiveStore / compaction candidate 校验 / RecoveryRuntime receipt CAS；仅 provider 返回、安全证据叶子和合成 PlanState 存储替身。没有模型/API/科学工具调用。

| 行 | 新见证 | 需要观察的现状 |
|---|---|---|
| V09 | distinct_pages | 默认累计 12 轮 / 12 工具后 stop；no_progress=0；合成已报告 1,202,400 Token 不导致额外 Token stop，证明缺少 business 累积成本限制 |
| V10 | repeated_request / multiple_tools | 相同规范请求第三次被拦；换 ID 无用；两轮多工具按 5 次预算只 dispatch 5 个 |
| V12 | read_errors / pure_prose / missing_usage / local_transport_retry | 工具错误占预算并触发 no-progress；纯正文只纠正一次；无 usage 的已观察请求数仍保留；standalone transport 两 attempt 只算一完成轮 |
| V13 | no_reserved_final_decision | 第 3 次读后到 turn=3 的硬限额会直接 stop，第四次原本合法 directive 未派发；证明现有硬限额有效、尚无软收尾保留位 |
| V14 | explicit_resume / cancel_before_archive / compaction_ledgers | 显式 resume 维持累计且不重复 usage；普通取消缺 receipt 的逻辑重入边界；真实节点压缩前后 business 与所有 map/merge/final callback 分账并持久化 |
| V15 | three_useful_pages | 三个不同有用页后第四轮合法 retry_step 被真实 parser 接纳为候选；不声称仅凭 parser 即已 Commit |
| V16 | 本节静态 + D2/current原验证 | revalidate 指纹、incident action budget、旧 evidence 重新核验路径；此处不重复制作科学假执行 |

统一 run02 中 D3 为 10 通过、2 个见证前提失败：V09 错把 `{usage_metadata:{...}}` 当 total_tokens 顶层字段；V14 沿用旧一发 compactor 替身，而当前真实 compaction 会发 `map_source_segment`，替身未实现协议，产生两次拒绝后走安全 fallback。失败回执保留；仅修见证字段读取，并替换为阶段 B 已有支持 map/merge/final 的 provider 叶子。run03 定点复测这两项均通过，故最终 **12/12 当前行为见证通过**，不需要重跑另外 10 项。

run03 压缩确证为 2 个 business 调用 + **3 个 compactor 调用（map、map、final）**，每个合成 usage=270，maintenance 已知累计 810；唯一 `recovery_compaction_applied` 事件、active CAS pointer、下一条真实业务 wire 含 `SUMMARY ENTERS NEXT REQUEST` 均通过断言。V09 的 12 个业务调用合成实报累计 1,202,400，仍只由 turn limit 收束。原始回执为 `/tmp/bm_failure_closure_d_run02/results/pytest-results.json`、`/tmp/bm_failure_closure_d_run03/results/pytest-results.json` 及各自 `d3_budget_witnesses/`；本阶段统一总报告将其随运行 manifest 归档。

仅 Recovery 最终有合法 directive 不足以证明压缩成功；修正见证之后才满足实际摘要应用与子调用计账证据。最终通过的含义仍是现状已查清：V09 累积成本、V13 软收尾为**已复现缺口**；V14 普通重入是限定 node/loop 逻辑边界，不等同物理重复收费。

合法恢复对照复用 `docs/implementation/a1_unified_fix_20260914/f6/verification.json`（22/22，通过范围明确为列出的框架机制）及 `f6/final_cases/recovery/`。已定点核验其中 recovery case 为 PASS：recovery=1/generate=10/reviewer=6，实际合成执行返回码 `[7,0,0,0,0,0,0]`，6 个 Commit receipts，FinalAudit=`task_verified`、task_status=`completed`。这是真实图/审批/校验/Commit 的合法恢复正例，provider 和科学输入仍是明确替身；F6 合成输出无真实 provider usage，不能据此声称某累计 Token 数已足够所有合法调查。它与新 V15 的三页正例共同约束 E 的预算设计。开发目录 `final/final08_runs/protocol-helper-shared` 的中途 recovery 快照不作为成功回执。

## 5. C3 最小修复切片设计（E 待授权、待测）

### 5.1 复用当前计数结构

1. 以已有 causal incident + source packet lineage 持有 ledger，将 business_resume_state、typed receipt、provider semantic invocation/attempt ID、compaction request ID 联结；不以 message ID、页号、context epoch 或模型措辞新建额度。
2. 模型、compactor、允许的 retry 在实际 dispatch 之前持久化 reservation；response/failed/cancelled 以同一物理 attempt ID settle 一次。上层 logical request 只做归属索引，不再加一次 usage。未知 usage 保留保守预留并标 UNKNOWN；缓存命中的输入也占 context 和累计 Token，金额若计算需另列 hit/miss 单价，不能靠 cache 将 Token 当零。
3. 同因果恢复从 durable ledger 读已用/保留量；active compaction、directive 续接、普通取消/异常后的重入一致，不从空 budget 开始。将已有 runtime-evidence started receipt 模式复用于这一缺口。已 stop 的同因果入口仍走现有 durable stop replay。
4. 保留三个不同概念：`read_progress`（新页/hash）、`investigation_usefulness`（新证据是否支持/排除当前因果）、`decision_progress`（接纳合法 directive / 原核验变化）；其中任何一个都不清零累计资源。V15 允许先调查，不能要求每页立即产出 directive。
5. 同一 incident 的新增 packet、diagnostic continuation、revalidation 继承额度。真正新独立故障需有可信 lineage/新的执行或验证因果；用户明确加预算是新授权记录+预算版本，不修改历史用量。增加页面/重新启动不是预算授权。

### 5.2 可验收的候选参数及历史重算

以下是 **E 的候选实验配置，不是本轮已应用值，也不是经真实流量验证的最终默认值**：

| 项目 | 候选值 | 依据 / 接纳限制 |
|---|---|---|
| 同因果累计输入+输出 Token | soft 300,000；hard 450,000 | 历史第 4 轮累计 321,765 已超过 soft；第 5 轮后 414,418，下一轮加输入 95,153 与现有 8,192 output reserve 将达 517,763，应在 dispatch 前拦。采用派发前 estimator；这里以历史实报输入回算，不冒充预估误差已校准 |
| 模型轮与工具数 | 现有 hard 12 turns / 24 tools 保留；soft 8 turns / 18 tools | 避免直接把正常多页调查收紧成一两次；主要提前截断本例的是费用阈值。V15 三页后第 4 轮决定有空间；超过 soft 的唯一收尾轮仍计入原 hard |
| 同因果机器活动墙钟 | soft 120s；hard 180s | 本例 Recovery 约 101.592s，本阈值不会伪称已阻止它；用于慢流/慢工具/重试合计，含 compaction。单请求 deadline ≤ parent 剩余；外部人类等待另存墙钟口径，不通过重入延长已用机器时间 |
| 最终合法指令机会 | 在原预算内保留 1 次 | 达到任一 soft 后，只允许一次短而合法的决策提交；预留估算当前 input + 已配置 output reserve；工具/格式纠正/summary 不得再多开超额轮 |
| compaction | 继续使用现有 64 callbacks / 2×context / 300s 子上限；同时受 parent 剩余额度限制 | effective limit 取子限制和 parent 剩余较小者；无剩余额度时不能以“为省 context”无限付费压缩；不增加独立可充值预算 |
| provider retry | 沿用现有最多 2 physical attempts 与 durable transport fence | 两次都在 parent 中按各自 reservation/usage 计费；不能只收成功调用，也不能把一次逻辑重试重复计两层 |

回算：第 4 轮结束触发 soft 后，第 5 次收尾的估算代理上界为 321,765 + 92,507 + 8,192 = **422,464**，低于 450,000，可留一次合法决定机会。如果第 5 次仍只分页，立即以已有 typed stop 收束，不再派发第 6 次；不能让模型持续自选“还需要页”。第 5 次若合法选择 revalidate，仍必须通过现有 action budget、fingerprint、admission，不能因收尾预留获得科学执行许可。真实 estimator误差、provider 输出参数、流中硬停由 V12/D4 与 E 正反例校验后才能定默认。

新 V15 见证采用每轮 synthetic usage 100,200，三页 + 合法指令合计 400,800；这能验证候选 450,000 并非在该构造上只会 stop，但它**没有在本轮实现候选策略**，不能标成新预算验收成功。E 需把同一有效页/有效最终指令 fixture 接在真实新 budget guard 后，另加 1) hard 前合法指令成功，2) 只分页收尾到限 stop，3) 小输入的更多有用页，4) 一次必要 compaction 后成功，5) 缺 usage 仍有保守预算 的对照。F6 全图旧成功是执行链合法性对照，不能替代新的多页+budget 正例。

### 5.3 停止结果与未决点

soft/hard stop 均沿用现有 inspection_stop / managed stop producer，保留原科学 FailurePacket。原始科学故障原因与“本轮调查预算耗尽/流中取消/未知 usage”应分字段；不要把系统停止重写为新科学失败。止损报告直接使用已归档事实和 deterministic projection；不能在 hard limit 之后为写总结偷偷再开模型。

尚需 E 验收：普通取消前的 durable reserve 能否避免同因果累计恢复漏洞；同时触发多预算时单一 stop receipt/CAS 幂等；活跃流总 deadline 与 physical retry 保守收费；跨压缩及并发 late callback 的不重计/不重开；缺证据投影时的 revalidate 是否只重跑核验、不重复科学 Execute。这些不是 D 阶段实施承诺，具体生产切片等待用户确认。
