# Plan 驱动验收、同任务产物复用与 50,000 输出上限

本轮实施依据是用户关于其他阶段 5 万输出上限、仅 Plan 特殊声明才深检、减少重复验证和 Recovery 冗长输出的讨论，以及后续“帮我开始修改”的授权。未重开 GWAS 全面审计。

## 实施结果

- Consultant、Planner 初次/修正、Generate、Supervisor、压缩调用及终止摘要的通用模型客户端输出上限为 50,000 token；输入预算的输出预留同步。更严格的已知 provider 能力限制保留。上下文总量与已有压缩机制继续沿用；5 万是上限，不是要求生成的长度。独立 Recovery 入口也不能通过旧环境变量把上限升到 5 万以上。
- 批准的 Plan 步骤可声明 `validation_level: basic/artifact/scientific`；缺省 basic。存在性、对象种类、必需声明、归属、进程结果和显式谓词继续检查。只有显式 scientific 才请求科学模型复核；不再从 Skill、任务关键词、日志关键词自动追加。明确的非空样本、Q 映射等谓词没有删除。
- 默认允许在相同 task/revision/Plan/version/step/契约/输入范围内，重验前次成功进程留下的候选产物。输入和输出的受控快照必须一致且完整；越界链接、外来产物、回滚失效或变化不复用。Plan 可对产物声明 `freshness: current_attempt` 强制本次生成；该字段进入审批与语义哈希。生产尝试 ID/action 与本次 acceptance attempt 分开记录，重验不记为本次新增产物。
- 执行器在代码审批、旧文件保护之后创建正式产物的缺失父目录，不提前创建产物本体。修复参考 helper 的正式 ID/访问别名混用（含副作用 index_artifacts），以及 Plan-local role 位于 semantic_view 内时被误读的问题。解析器继续拒绝未声明身份。
- Recovery 提示在证据足够时立即提交语义修复指令，不反复重述、不起草替换脚本、不为产生文件变化而 touch/重建。缺少正式提交的反馈要求复用已有诊断。Generate 负责脚本修正，原代码审批保留；没有另加字数门。
- Generate 截断显示 `GENERATE_RESPONSE_INCOMPLETE` 并保留原因；即使表面上已有完整代码块也不执行截断回复。删除同一执行核验失败的重复笼统显示，展示首个具体问题。

## 基线、累计源码与提交边界

起点 HEAD：`a489d0044877a5d73727e6353a95bbeff60514d2`。原 index 为空，工作区有大量原有 dirty/untracked/overlay。

实施中定点纳入同时新增的 workspace-agent 入口：command_tools.py、state.py、bio_agents.py，以及 tool_loop.py、workspace_agent.py、workspace_files.py、test_workspace_agent.py。它们作为前置 overlay 保留，不把无关改动收入本轮提交。模型替身现在通过真实 request_execution 工具交接进入原生产链。

最终累计源码：`/public/home/liusiqi/03.毕设/biomind_audit_exports/plan_validation_20260918/final_cumulative_source`；逐文件哈希：`delivery-source-manifest.json`；合并前置 overlay：`integration_baseline_source` 和 `integration-baseline-manifest.json`。共 954 个冻结源/fixture 文件，manifest SHA-256：`f9f8f5ace69c1da0b4ee3d50d23128d789accc105a6e351cd9315051295f0936`。

仅提交下列实施增量，原 staged/dirty/untracked 不整体提交。recovery_agent.py 与 config/config.py 的交叠区手动精确分片并保存三份源码与哈希回执。完整提交 hash、index/worktree 分片与前置字节哈希在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/plan_validation_20260918/implementation_commit.json`。单独 checkout 本次 commit 不等于上述含前置 overlay 的已测累计源码。

## 验证

同一最终累计源码：**924 次通过，839 个不同测试，1 项跳过**；各正式组退出码 0，源码校验均未变化。

| 验收组 | 通过 | 跳过 |
|---|---:|---:|
| regression-graphs | 100 | 0 |
| regression-runtime | 385 | 0 |
| regression-recovery | 68 | 0 |
| plan-revision | 5 | 0 |
| provider-terminal | 129 | 0 |
| workspace-entry-isolated | 15 | 0 |
| changes | 190 | 1 |
| compaction-regression | 32 | 0 |

完整逐组命令与结果位于 `/public/home/liusiqi/03.毕设/biomind_audit_exports/plan_validation_20260918/delivery-validation/<group>/invocation.sh`、`results.json`、`summary.json`、`stdout.log`。可用 `/public/home/liusiqi/03.毕设/biomind_audit_exports/plan_validation_20260918/run_selected.py` 对导出的冻结源码在新 /tmp 目录复跑；默认仅运行本轮 changes 组，参数可选择表中任意组。

自动链覆盖真实 Planner 本地分配 → Generate → 代码审批 → Execute 前后快照/登记 → Supervisor/CompletionGate → Commit → 下游消费；产物由生产 Execute 发现，测试没有手工注入 produced_artifacts 来宣称链路通过。包含目录/空文件的未变化重试、强制本次生成的拒收、参考 helper 完整链，以及 finish_reason=length 的局部重提。真实 SDK 使用 MockTransport 检查最终请求中的 max_tokens；未调用外部 API。

真实本机执行仅限 /tmp 中批准的合成 shell fixture（mkdir/test/printf 等）及 helper 的 Python 本体。原生 BWA/samtools/Picard 与 Inspector 的外部执行叶子有明确替身；未进行真实主机环境/bwrap 验收，不能据此宣称原 GWAS/WGS 完成。没有安装、升级、改共享环境/注册表或主机 bwrap，没有恢复原 session、覆盖原三份报告或 push。

workspace 前置 overlay 的 15 项隔离测试通过；其需要真实受控命令的设施冒烟用例未运行，创建 .env 样例的用例与本轮 blanket 禁读写保护冲突，未降低保护来通过它。早期宽泛选组被停止，未计入正式验收。

另外 6 项旧压缩测试失败已在修改前冻结源码上逐项复现，证据位于 `/public/home/liusiqi/03.毕设/biomind_audit_exports/plan_validation_20260918/baseline-check/`；未为了通过它们降低生产压缩约束，也未把全部仓库测试宣称为全绿：

- `tests/test_schema2_context_compaction.py::AssemblyAndAttemptTests::test_bounded_second_attempt_gets_precise_capacity_feedback`
- `tests/test_schema2_context_compaction.py::AssemblyAndAttemptTests::test_soft_threshold_noop_and_trigger_paths`
- `tests/test_phase_context_compaction_runtime.py::PhaseContextCompactionRuntimeTests::test_extreme_input_uses_bounded_lossless_map_merge_before_selector`
- `tests/test_phase_context_compaction_runtime.py::PhaseContextCompactionRuntimeTests::test_hard_block_with_two_invalid_attempts_raises_exact_failure`
- `tests/test_phase_context_compaction_runtime.py::PhaseContextCompactionRuntimeTests::test_phase_local_selected_summary_keeps_original_identity_across_epochs`
- `tests/test_phase_context_compaction_runtime.py::PhaseContextCompactionRuntimeTests::test_tool_call_and_results_are_an_indivisible_protocol_group`

## 精确文件范围

- `agent_core/completion_gate.py`
- `agent_core/consultant_finalize.py`
- `agent_core/evidence_projection.py`
- `agent_core/genomics_reference_bundle.py`
- `agent_core/llm_safety.py`
- `agent_core/managed_artifacts.py`
- `agent_core/node_context.py`
- `agent_core/plan_contract.py`
- `agent_core/plan_tracker.py`
- `agent_core/planner_output_policy.py`
- `agent_core/workflow/commit_step.py`
- `agent_core/workflow/context_compaction.py`
- `agent_core/workflow/managed_stop.py`
- `agent_core/workflow/recovery_agent.py`
- `agent_core/workflow/recovery_reception.py`
- `agent_core/workflow/recovery_runtime.py`
- `bio_agents.py`
- `config/config.py`
- `prompts/manager.py`
- `skills/genomics/wgs-short-read-mapping/scripts/prepare_reference.py`
- `supervisor.py`
- `tests/offline_managed_bootstrap.py`
- `tests/planner_fix_acceptance/test_planner_recorded_workflow.py`
- `tests/planner_truncation_acceptance/test_planner_output_policy.py`
- `tests/test_gwas_input_observation_graph_s3.py`
- `tests/test_gwas_runtime_auto_chain.py`
- `tests/test_gwas_s4_recovery_integration.py`
- `tests/test_inspection_workflow.py`
- `tests/test_managed_artifact_runtime_reconciliation_v3.py`
- `tests/test_plan_local_preaction_convergence_v2.py`
- `tests/test_plan_validation_owned_retry.py`
- `tests/test_recovery50000_budget_boundary.py`
- `tests/test_recovery50000_diagnostics_contract.py`
- `tests/test_recovery50000_local_repair.py`
- `docs/implementation/plan_validation50000_20260918.md`
- `docs/implementation/plan_validation50000_20260918_results.json`
