# F6 通过候选与真实 Plan 差异复核

旧 F6 的 22 个通过用例证明了固定合法 Plan 下的执行、证据、恢复与结束组合；没有验证 Planner 能从任务和反馈中修好 Plan。正常用例只调用 Planner 一次，固定 provider 不解释请求，直接返回人工构造的六步业务 JSON。它把每一步的 `step_id` 设置为该步 `operation`，并用同一组常量生成依赖与 workspace 关联，因此无法产生本次“步骤改名后 role association 仍指向旧键”的错误。

本目录仅增加补充证据，没有修改生产、旧测试或旧结果。真实 session 的四候选、P_min 及公共规则差分由同一补充复核的主目录负责；此处不据 F6 的通过次数推断真实模型、真实科学执行或现场进程加载版本通过。

- [P_test.raw.json](P_test.raw.json)：可直接回放的完整旧 F6 正常业务候选，7,601 字节；SHA-256 `dc029d40dd47fef04b5066bb4d1bca1a890cf144b0899d5f7f789b1b663a9306`。
- [P_test.reconstruction.json](P_test.reconstruction.json)：原始独立 response 文件没有保留，正式 claim 的 `raw_ref` 为空。本文件由已检查的旧 `F6Graph.plan` 纯函数与当时实际请求内公开能力声明重建，摘要与原正式 `call_claim.raw_digest` 完全一致；不是把重建文件称为历史原文件。
- [P_test.planner_request.json](P_test.planner_request.json)：原始保存的真实 Planner 请求；来源为 `f6/final_cases/normal/f6-normal-full.json.gz` 的 `/requests/1`，含三条完整 message。另存每条 message 的原始文本、去 phase seed 后的公共上下文、正式 claim 与实际 transport 接收回执。
- [artificial_fields.tsv](artificial_fields.tsv)：人工预填字段及公开来源；[steps.tsv](steps.tsv)：六步的输入、输出、能力和资源授权。
- [coverage_blindspots.tsv](coverage_blindspots.tsv)：旧覆盖与剩余盲区；[replay_design.md](replay_design.md)：本轮正例及后续最小接线。
- [source_provenance.json](source_provenance.json)：旧提交/加载基线、材料位置和摘要；[reconstruct_p_test.py](reconstruct_p_test.py)：不导入生产的只读重建核验。
- [verification.json](verification.json) 与 [run02](run02)：新增两例均通过，18.53 秒、实际 rc=0、运行前后源字节不变。每例各一次真实 Consultant 与 Planner transport，均被正式消费；一次真实 PlanReview 输入请求；资源和数据流均 compiled，结局为 `approval_pending`，零 Generate/Execute/Commit。两例的实际返回 raw 与各自正式 claim 摘要一致。

字段需要分清：正式候选是 `steps[].step_id`；反馈中的 `step_key` 表示该字段的值，不能把候选字段改成 `step_key`。原实际请求的 system message 已写明此映射以及“Use your actual declared step IDs”。新正例只修改 `step_id` 的值并同步关联与依赖。

原正常 F6 已有真实六种 operation、prepare/build 对同一 mutable identity 的两次推进、annotation 的 read 权限、配置产物和五份物理输出。这里所补的是命名与公开候选的准入边界；不把它们重复归为未实现功能，也不把原科学替身当真实 snpEff。

本轮正例证明当前正式准入允许 `step_id` 与 `operation` 不同，前提是依赖和 role association 引用实际声明的步骤 ID。它没有运行改名后的下游，也没有验证真实模型能自主完成该修正。

主复核另发现结果模式差分：旧 P_test 没有 `result_contract_version`，且 `plan.db`、`plan.config` 均未写 `required`。将它放入现场的 `output_dir` 与五个 `planner_materialized` 输出上下文，并明确五项 `task_output_materializations` 和 `result_contract_version=1` 后，Root02 得到三项 `PLAN_OPTIONAL_RESULT_REQUIRED_BY_CONSUMER`。现场 P_live/P_min 已将这两个中间产物声明为 required。Root03 补齐两项 `required=true` 后已通过完整 PlanReview 准入；未经上下文适配的原 P_test 控制仍被 `TASK_OUTPUT_MATERIALIZATION_MISSING` 拒绝。证据见主复核 [run03_variants](../replay/run03_variants)。这解释了旧 F6 默认结果模式的覆盖差异，并非生产修复或真实模型通过。

[followup_master_proposal.tsv](followup_master_proposal.tsv) 按依赖列出并入原 F1/F3/F6 的五项后续：精确 Plan 拒收诊断、公开 step 引用投影、step_id/skill_ids 示例、分离步骤身份与 operation 的 F6 修订组合、另行授权的有限真实 Planner 验收。每项生产状态均为 `NOT_IMPLEMENTED`，不新增架构或重开全节点审计。
