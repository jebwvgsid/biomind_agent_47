# 最小反向变体接线

新增 `tests/plan_live_review_20260915/test_f6_variant.py`，复用本轮新 `AdmissionReplay`，后者继承已提交 F6 的真实构造器和 model leaf。对照候选仍来自旧 `F6Graph.plan()`；变体保留六种 operation、capability、输入输出、配置产物、workspace mutations、effects 和检查项，只把 prepare 的公开 `step_id` 值改为 `runtime_healthcheck`，同时改 build 的 dependency 与 prepare 的 role association。正式字段继续是 `step_id`。

入口保持真实新任务持久化、Consultant handoff、Planner request、节点 invocation 注册/消费、LLM transport、native parser、wire binding、resource compilation、step effects、dataflow、candidate validation 与 PlanReview。保留真实 `_prompt_user` 与输入回执，只在外部 `safe_input` reader 抛正式 `HumanInputUnavailable`，使图停在 `approval_pending`；不直接写入 approved、ready 或 succeeded，不触及 Generate、科学执行或 Commit。图中使用的新输入声明只供隔离任务构造，不读取原事故数据。

必须同时满足：一次 Planner 调用、无 rejection、`plan_review_ready`、两份 compilation/dataflow 回执为 compiled、真实 PlanReview reader 被调用、六步全部保留、prepare 编译后的 step ID 是新键、build dependency 指向新键、零 Generate/Execute/Commit。只测 validator 返回真不够。

上述两例已在本轮 export02 上实际通过，源码签名 `003eba3d627b37cb918191b8937cdfc234f8e33614c48b62294802f3514c21ce`，详见 `verification.json`。没有把尚未执行的后续接线计入这两例。

这个范围有意止于正式 PlanReview。旧 F6 的 `current_operation()` 实际返回 `current_step_id`，`action()` 再用它查 `OPS.index()` 与 approved step。因此旧下游测试夹具假设 ID 与 operation 相等。继续验证改名后的全图，未来须在新的夹具读取同一实际冻结 step 中的 `operation`，按真实 step identity 对齐对应 binding，而不修改批准对象、不直接添权威状态。这是后续验收夹具扩展，不构成本轮生产修复。

同一能力用于两步、额外合法 readonly check、关联缺失/跨步骤/跨 capability/父资源错误，以及 P_live/P_min 的差分由本轮主 `test_admission_replay.py` 负责或在主矩阵明确标注。此子测试不重复构造它们，也不把只测 alias 的两例扩大为这些组合已经通过。

旧 F6 的人工 provider 不会请求更多 Skill 上下文，不会从反馈决定修改哪一引用；因此真正模型的 Plan 修订能力仍需独立验证。当前补充不接真实 provider/API，不恢复原 session。
