# 阶段 E：C1—C4 实施与离线验收

文档标识：**BM-FAILURE-CLOSURE-1789521763-PHASE-E-V2**。阶段 D 审计提交为 `332d91b6307fde96a6f64b434722fe4067cdc164`。本交付复用 D 的历史见证，没有恢复原会话。

## 结论

已修复具体值准入、正式失败事实首请求投影、同因果累计资源接线，以及 Recovery 的生成/接纳/公开展示边界。四条组合链同时验证了**可恢复错误最终 Commit**与**持续调查有界停止**。

最终生产字节上，阶段 E **149 个唯一测试通过**；相关既有回归 **78 个唯一测试通过**。V01—V20 均有本轮通过的消费者证据。74 行对照为 40 项 FIXED_AND_VERIFIED、27 项 PRESERVED_VERIFIED、4 项 STATIC_ONLY、3 项 NOT_APPLICABLE；没有把历史缺陷见证改称修复通过。[验收矩阵](acceptance_V01_V20.tsv)、[74 行对照](boundary_coverage_74.tsv)、[唯一结果及命令](final_results.json)。

**累计资源与 8,192 生成上限：MECHANISM_READY / DEFAULT_NOT_ACTIVATED。**真实配置入口为 `BIOMIND_RECOVERY_RESOURCE_POLICY=phase_e_candidate_v2`，未设置时为 `disabled`。本轮仅在隔离测试中显式选择候选值，没有修改部署配置。C1/C2 和有界公开副本已接入；不能据此声称后续默认运行已启用新累计预算。

## 实际生产修改

| 切片 | 实现与消费者 |
|---|---|
| C1 | `plan_bindings` 的 operation binding rule 是角色政策唯一来源。具体标识/路径片段拒绝 `$NAME`、`${NAME}` 及嵌入形式；普通 literal、opaque JSON、脚本变量、原有精确 ref/模板保持原义。公开 requirements、examples、进展反馈、env 解析及 attester 共用政策。snpEff 三个相关 operation 的 genome_version 声明具体路径片段角色。错误返回原 Generate 局部纠正，模型不能用 action 自报 role 绕过。 |
| C2 | `recovery_projection` 从可信 producer 的正式字段提取拒收检查：nested validation、真正的 parser receipt、失败 attestation journal、Supervisor 状态、CAS、安装回执等。索引先于背景，最多展开 12 项，保留总数、省略范围/pointer、未知码及 unknown。没有从脚本或任意 JSON 猜根因，没有改变完成裁决或路由。 |
| C3 | `Schema2RecoveryNode.run` 在首次业务/维护调用前建立现有 incident 的父账本。`ContextLineageStore` 复用现有 claim 锁与原子文件写入，物理派发前预留、返回后按 attempt 结算。业务、纠正、可见 retry、压缩分片共同记账；缓存输入不扣除，缺失用量不填 0。重入先消费 completed/UNKNOWN，计数变化不会授权重发。 |
| C3 收束 | 现有 loop 保留轮次/工具/重复限制，增加完整决定请求的输入、输出、时间及轮次预留；收束仍调查则 typed blocked，硬预算外不调用总结模型。明确未派发才能释放预留。已完成合法响应可在用量到界后正常接纳；取消/权限/作用域仍独立生效。 |
| C4 | Recovery 实际调用传递 `max_tokens`，取显式用户较小值，冲突参数拒绝；compactor 保留自身容量。流中按父绝对 deadline 和接收字符限停止，半工具不派发。公开说明是独立有界副本，处理跨 chunk/未闭合 analysis 标签及代码例子；原始协议不被展示截断。迟到 usage 可结算，迟到 directive 不接纳。 |

未新增 Controller、binder、BudgetManager、纠错 Agent；未合并 Gate/Supervisor；未关闭核验或扩大候选次数。`managed_stop.py` 仅提交资源停止不再调用总结模型的一行增量，原有两处 prose overlay 未收编。

## 四条组合链与调用数

以下均是生产准入/审批/attester/投影/loop/Commit 接线，外部模型由确定性替身替换，科学叶子只运行新建的微型合成进程。不是科学 E2E。

| 链 | Generate | Recovery 模型 | 证据工具 | approval / Execute / Commit | 结果 |
|---|---:|---:|---:|---|---|
| 1：坏 scalar → 局部纠正 | 7 | 0 | 0 | 6 / 6 / 6 | completed；坏候选自身三项均 0 |
| 2：合法声明、实际缺文件 → 新候选 | 7 | 1 | 0 | 7 / 7 / 6 | completed；失败 prepare 不提交，新 attempt 重新批准 |
| 3：有用多页 → 原 attempt 重核验 | 0 | 5 | 11 | 0 新审批 / 0 新 Execute / 1 Commit | 两次 Supervisor 审查；Commit 重放不重复写入 |
| 4：持续读页不作决定 | 2 | 4 | 3 | 2 / 2 / 1 先前 Commit | 持久 blocked；0 额外总结模型；重入 0 新模型 |

链 1/2 的整图另有 Consultant 1、Planner 1、Reviewer 6 次替身调用；链 4 为 1/1/1。非 snpEff 的独立 `fixture/bundle` Skill、六个新 operation、`dataset_label` 和新目录布局也完成了链 1/2：分别 6 和 7 次 Execute、均 6 次 Commit。[完整前后计数](before_after.json)，原始请求/回执见 [证据索引](evidence_index.json)。

## 修改前后上下文与压缩接线

同一 nested-validation 合成场景：D 的首请求 **43,337 字符**，缺具体拒收码；E 的首请求 **44,091 字符**，包含原始 `WORKSPACE_MUTATION_NOT_ATTESTED`。两边均 1 模型调用、0 新 Execute。新增 754 字符用于首因与索引，不能说每条请求都变短。[逐组件及实际首请求](first_request_before_after.json)。长 stdout 仍按既有机制保留，失败索引排在它之前；本轮没有把原始归档全量展开进新的索引。

压缩合成场景保留 2 次业务请求、3 次维护调用。维护用量 810、业务 210，父账本唯一 5 个物理 attempt 合计 **1,020 synthetic tokens**，没有再加一遍子汇总。

| 上下文组成 | 首业务请求 | 压缩触发前候选上下文 | 摘要应用后的下一请求 |
|---|---:|---:|---:|
| System | 9,906 | 9,906 | 9,906 |
| 冻结 seed | 3,542 | 3,542 | 3,542 |
| 原选定材料 | 490 | 490 | 490 |
| assistant/tool 尾部 | 0 | 9,089 | 0 |
| 压缩摘要 | 0 | 0 | 139 |
| 合计字符 | 13,938 | 23,027 | 14,077 |

这不是仅检查“生成过摘要”：从实际 episode archive 提取下一条请求，重新计算 wire fingerprint，与父物理 reservation 对上；摘要 `SUMMARY ENTERS NEXT REQUEST` 确实在下一请求中，CAS epoch 也对应。[压缩应用证据与完整 messages](compaction_application.json)。取消后恢复 active epoch 不重复压缩，不重发 UNKNOWN 业务请求。

持续新页使用同一 D 合成 provider 行为（每次报告 100,200 tokens）：D 在 12 模型/12 工具后触发轮次上限，报告 1,202,400 synthetic tokens；E 显式候选 policy 下为 4 模型/3 工具、400,800 synthetic tokens。正确第四轮决定也能通过，未用“更早 blocked”替代恢复。以上不是实际费用、真实缓存率或节省比例。

## 候选政策与验证边界

候选 soft/hard 为 300k/450k tokens、120/180 活动秒，保留 30 秒决定机会，输出 8,192，接收 131,072 字符，公开说明 1,200 字符。父单调时钟计模型、工具、维护和本地准备；正常退出保存实际 elapsed，重启不能复原的活动区间保留有界不确定预留，不累计整个离线时段。预算不用于中止已批准的科学进程。

**负向校准结果必须保留：**现协议允许的 128,000 字符多语指令，经本地估算为 72,744 tokens，容不进 8,192。解析协议没有被缩窄；超量/length 返回按原规则拒收，远端是否服从参数尚未验证。此候选不能直接宣称完成真实校准。[实际配置、限制与状态](candidate_policy.json)。

`/exit`、LF、CRLF、空白包围，以及已有 quit 别名在实际 `interactive_mode` 中零任务派发、零模型、零 Execute；多行正文/代码块内出现 `/exit` 保留为普通输入。13 种输入均捕获回执；审批等待中的退出另外通过。模型、工具、压缩及普通 Commit 取消交错均有离线证据；取消前已 Commit 的历史保留，取消后结果不能覆写状态。

## 失败记录与历史勘误

探索性旧回归并非全绿。旧 40 ms provider 夹具、缺 current attempt 的 inspection 夹具、`/data` 写入、被离线保护拒绝的嵌套 CLI 子进程，以及旧单次压缩/候选计数假设均保留失败记录。相应本轮边界有当前正式入口测试，未放松离线保护。部分旧失败在原 D export 定点复现；P16 的旧 40 ms 变体仍失败，等价 Generate 消费断言在 10 秒独立夹具通过，不混称原测试已通过。[失败账本](failure_ledger.json)、[D 勘误](phase_d_errata.md)。

## 源码、提交与可复现性

生产最终提交：`c6ea92c1940d5cda50683442a51162f7014e5cdd`。前置逻辑提交：

- C1：`0013eeb3d85c33bcb6fafcb6782c874d356e25da`
- C2：`cf77c53a9eb62d81f8377de850dda3b82d06b368`
- 公开示例/正式来源补齐：`01eea17373f65bea46136132dd76d30500b9534d`

[逐提交文件范围](commits.json)。交付文档及组合测试单独提交，最终完整 hash 在最终回复给出。未 push。

三个最终导出的**全部生产文件字节相同**，汇总签名 `e5bb4292c06c008bead5afa5758323dac6f7936a1ebd099d1eed503d6886ac8d`；最后增加的审批/旧 Generate 边界测试在对应最终测试字节上重跑。每个导出的源码/fixture 哈希及实际加载位置见 [loaded_sources.json](loaded_sources.json)。

验证基线包含用户先存 overlay，**不是 clean commit 可独立部署声明**。58 个受保护 Python 文件按初始指纹核对无漂移；配置只核对元数据，没有读取凭据/启动脚本。测试所需已有未跟踪 fixture 与安全源码 overlay 的归属/指纹保留，未提交它们。复现需要这些同字节 overlay；真实配置由测试框架替换为 inert config。[复现方法](reproduce.md)。

真实模型 canary、真实缓存复测、科学 E2E、真实外部 step_worker：**NOT_RUN**。远端取消/停费：**UNKNOWN**。本轮已停止，不恢复历史科学任务；[后续 canary 方案](canary_NOT_RUN.md)尚未执行。
