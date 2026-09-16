# D2 — producer 到首个 provider messages

基线为当前工作树，不把历史 F6 固定叶子结果当作所有新生成变体已通过。本文行号在 protection_before.json 的源码字节上定位。

## 正式注册与入口全集（本问题域）

`BioAnalystAgent.__init__`（bio_agents.py:2915–3030、3055、3348）注册真实 `TypedExecute`、`TypedSupervisor`、`CommitStep`、`Schema2RecoveryNode`，对 execute/supervisor/commit/install 包装 `bind_causal_failure_node`。`build_schema2_production_graph`（runtime_graph.py:368）注册它们；不是仅运行较小的测试图。源码枚举见 boundary_coverage.tsv 的 D2 行。

- Generate 的候选表示/parser/脚本检查失败由本地纠正处理；无依据将它们送入 Recovery 会改变 owner。`runtime_evidence_required` 是特例：带 `pending_runtime_evidence_request`，在 `Schema2RecoveryNode.run:1651` 转入原 `_recover_runtime_evidence`，不走通用 FailurePacket 调查。
- Execute：`execution_failed`（进程非零或 rc0 且 validation.failed）、`diagnostic_completed`、`pre_execution_rejected` 且 owner 为 VERIFICATION/DEPENDENCY → Recovery。其余 pre-execution owner 各回 Generate/Planner/AskUser 或 END。
- Supervisor：rejected、incomplete_evidence、diagnostic_evidence_recorded、verification_error、inspection_required、correction_required、assessment_pending → Recovery；`assessment_pending + automatic_review_stopped` 直接 END。`completion_gate_outcome` 是 binder 的来源别名，不是另一个注册节点。
- Commit：commit_conflict、contract_rejected → Recovery。框架 harness_error 直接 END，不应伪造一次 Recovery 模型调查。
- Install：installation_failed → Recovery；status 已 terminal 时 END。`TypedInstallSubagent.classify:1162` 从当前运行能力健康检查/步骤状态产生结果；本轮不运行安装器。
- Triage 恢复分类和 AskUser 的 recovery_pending 重入原 Recovery；外部 step_worker 的结果先进入 Supervisor。重入应消费已有因果 packet，不能靠扫描旧 outcome 字段选错误。
- Planner、PlanReview、FinalAudit 没有本轮通用 Recovery 直连入口。保留其职责；FinalAudit 的失败如何对用户报告不扩大成本轮科学终审审计。

未知未来 outcome 类型由 typed schema 拒收，与“已有 outcome 内出现未知 validator 错误码”不同。后者必须保留，不应增加错误码白名单。

## 本次确定性有损边界

1. 科学进程/文件后验核验在 `bio_agents.py` 的 Execute 实现中形成 `execution.validation`，完整允许事实保存在 `execution.recovery_facts`。历史对象 `5df089285635ab44dcd80e1b4d181178ee45aa1098d0db882f95550f96154c48` 的 `/payload/validation/errors` 有两条 workspace 错误；`/payload/validation/workspace_mutation_findings` 有期望路径、after 快照与失败条件。
2. `TypedExecute.run:1352` 正确把 rc0 + validation.passed=false 路由到 execution_failed。它的失败 payload 没有单独投影嵌套 validation；summary 仍是通用 launched command failed。
3. `build_causal_failure_record:1524` 持久化原 source 的引用，调用 `_runtime_source → _runtime_payload:661` 生成紧凑失败视图；白名单没有 validation/errors。`_diagnostic_facts:1215` 读取 returncode、result_realizations、顶层 contract_violations 和 CompletionReport，但不读取 `execution_record.validation.errors`。**错误在这里未进入可见事实，而不是被 provider 消息裁剪器后来删掉。**
4. `project_recovery_seed:2284 → project_recovery_seed_for_provider:315 → RecoveryAgentLoop.initial_state` 将这个不完整 FailurePacket 放进 `<recovery_seed>`。`Schema2RecoveryNode` 再接 `_schema2_recovery_provider_invoke → _ainvoke_llm` 实际派发。历史 0016 messages 缺决定性错误，保存的检查对象却有；这是源和最终 wire 的双端见证。
5. inspection_evidence.py:234 会按冻结 ref 读回完整 execution facts，其中含 validation，并保存为 step_attempt 可读对象。这说明证据存在；不需要再次科学执行。该对象 pretty 文本 validation 位于第 518 行，顺序分页第 5 轮才见到错误。

## 其他入口的共有风险与已覆盖行为

`TypedSupervisor.run` 对 INVALID 将 validation.errors 提升到 contract_violations；该路径通过现有投影能够前置错误。对 INCOMPLETE_EVIDENCE 则放在 missing_evidence，对 VALIDATION_ERROR 则留在 validation_evidence.errors；这些字段不在当前紧凑 runtime payload 白名单中。因此不能以“监督审查路径已修”概括所有 verifier 状态。

语义 concern 路径产生 CompletionReport、SemanticDecision、binding/receipt findings，前轮 A1/阶段 B 在这一路径的事实投影继续存在。Commit 的 reason_code、failed_invariant、CAS expected/current generation 在共有白名单中，也不同于本次嵌套执行核验缺失。Install 的 status 会前置，但 evidence 内的具体健康检查信息没有对应摘要；其实际在线安装模型首请求本轮 UNKNOWN，源码路径可达已确认。

`_diagnostic_facts.add_fact` 最多 16 项，多个 findings/路径等各有截断。当前“有界”不等于“错误很多时首因仍可达”：没有一个共享的关键失败索引明确记载省略数、优先级及定点指针。C2 应补索引与 producer 失败项顺序语义，不提高全局 read 或把原始 stdout 全量注入。

## 最小 C2 修改设计（未实施）

- 在既有 execution facts/validation producer 与 `recovery_projection` 之间提供同源、有界的失败检查视图。字段最少包括 stage/source、process started/rc、validation status、Commit状态、error code/message、failed check、declared/expected、捕获的实际观察及 ref/pointer。
- 共用一个受作用域约束的检查摘要函数，给 execution.validation、ValidationEvidence 与 CompletionReport 的直接消费者使用；不另建一份可独立改写权威的报告。
- `actual` 仅来自 after snapshot/parser/receipt；只有 code 时写“脚本声明”，缺数据明确 unknown。不要让 `after_evidence.root`（根目录）冒充某个期待文件的实际对应路径。
- 保留原消息及未知错误码、错误顺序/关键索引。避免在 Execute、Recovery、Supervisor 各自复制一套错误码分类 if。
- 保留 gate、Approval、Commit/CAS、inspection 权限与历史不可变性；不将真实后置条件失败改 warning。

## 离线见证及适用范围

`offline_witnesses/test_d2_first_request.py` 通过既有 A1 guarded export 运行：

- 真实 TypedExecute/binder/RecoveryLoop/消息构造，安全已记录执行叶子返回 rc0 + nested validation；普通和未知错误码均观察其首请求可见性。此例没有运行 attester，不能单独冒充 V05。
- 真实 Supervisor 处理 concern、缺 attempt、损坏 effect stdout；真实 Commit 拒绝无 eligible verification；分别记录 actual producer outcome 与首条模型叶子 messages。
- 扩展原 F6 图的 provider 叶子，让合成 prepare 脚本退出 0 但移除必需文件；真实审批、tiny 进程、attester、TypedExecute、binder、Recovery 消息与指令接纳均运行。期望见证为进入 Recovery 前无该步骤 Supervisor、无该步 Commit、错误在源端存在却首请求缺失。

见证中 `DEFECT_PRESENT` 表示当前缺陷被复现；它的 pytest 断言通过不表示生产缺陷已修。完整首次消息只包含隔离合成资源，原会话仅归档必要脱敏摘要。V06 尚无当前真实 Commit CAS 冲突、安装故障和所有框架注入的逐项图重跑，明确为源码核对/旧限定证据复用；阶段 E 组合验收须补齐。
