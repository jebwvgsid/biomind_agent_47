# A3 新运行作用域的独立审查与修复

本节承接 R1/D3 后的独立代码审查。审查对象为本轮 `prepare_generation`、`_not_dispatched`、固定 runtime probe 和 Generate 接线；未恢复旧 session、安装软件或调用真实模型。

## 发现及修复

本轮中间实现仅凭同 Skill/capability 的历史 `installation_receipt.status=available`，或同 step 的历史 `retry_step` directive，即可退役后来产生的失败 attempt。隔离进程中使用真实 `save_plan_state/load_plan_state`、正式批准的新 Plan、`record_provenance_event` 构造了两个可复现见证：旧工单目标与当前 request 不同仍被接纳；`active_recovery.status=completed`、旧 revision 和无关 evidence 仍被接纳。修复前见证的 2 个断言通过表示缺口确实存在，不是成功验收。

修复后，安装工单携带 `runtime_generation_source_scope(durable)` 返回的冻结源身份：task、plan、version、revision、step、epoch、审批作用域摘要、批准的 PlanSnapshot 引用以及确切 source attempt 集合。`prepare_generation` 要求当前 request 的全文 hash 与 receipt 相同，且 request/attempt/target/Skill/capability/source 字段逐项相同；工单源身份必须仍等于当前 durable 身份。安装父入口负责首次冻结且不重铸旧工单，receipt 生产端负责透传该身份。

Typed Recovery 复用正式 `generation_provenance_for_dispatch`、`RecoveryExecutionEnvelope.from_mapping`、`project_corrective_generate_seed` 和 `current_generation_causal_reference`，检查 active dispatch、不可变 envelope、采纳的 directive/outcome、已消费 causal packet 和源 attempt。该源必须是当前最新未退役 attempt，claim 必须处于尚未采用 action 的合法状态。退役事件记录授权摘要，后续 attempt 不得复用同一授权。正式批准的新 Plan 版本仍可独立提供修订授权；无版本或外 plan 的 attempt 不提供该例外。

生产 action、原 manifest、批准记录与执行历史均不补写新的 runtime 事实。核心 provenance transition 将结束的 attempt 标记 FAILED，事件保留来源，新的 epoch 才能重新取得 required runtime facts。重复同一次准备保持幂等。

## 证据

- `evidence/runtime_authority_before_witness.py`、`runtime_authority_before_results.json`、`runtime_authority_before_test.log`：中间缺口的两个真实 store 见证。
- `tests/test_unified_runtime_generation_authority.py`：16 个回归，包括旧回执拒绝、8 类 request/receipt/source 不一致、当前安装的一次有效授权、正式 RecoveryRuntime 采纳的正向路径、源 attempt/envelope/directive/状态损坏拒绝及后续 attempt 重放拒绝。
- 与本轮 `tests/test_unified_runtime_preparation.py` 的 12 项共同执行，**28 passed**；结果及测试源 hash 见 `evidence/runtime_authority_receipt.json`。无被测核心函数替身；外部 probe 使用已有边界夹具。测试在 `/tmp/biomind-r1-tXsGVj/review-candidate` 中，通过现有 `offline_managed_bootstrap.py` 新进程隔离，设置 `BIOMIND_DISABLE_DOTENV=1`。
- 旧 empty-required action → 正式 Plan revision → 新 scope → runtime evidence 接纳的测试发生在尚无新 attempt 时；该单测不声称已经完成新 candidate 的 Generate、审批或真实科学执行，后者由独立集成链验证。

## 过期 runtime fact 的后续最小修复

发现时，`action_runtime.py` 和 `runtime_evidence.py` 均与本轮 prerequisite source hash 一致。已有 Java home 派生兼容逻辑存在过期旁路：显式 `runtime.java.bin` 事实过期后，其 evaluation 为 UNKNOWN 且 `evidence_ids=[]`；另一个仍有效的 `JAVA_HOME` 事实可仅凭 `bin/java` 存在且可执行重新导出 ready 的 `runtime.java.bin`。阻止 UNKNOWN 的现有条件要求 `evidence_ids` 非空，因而没有挡住该情形。

`evidence/runtime_expiry_witness.py` 使用真实 immutable evidence 创建、接纳、过期投影与 registry materialization，修复前隔离测试 **1 passed** 证明该既有行为；这里的通过表示风险见证成立。

随后按统一任务 R2 的当前事实同权威要求修复了两个已有读取条件：`build_runtime_binding_registry` 和 Recovery `_assessment` 在 UNKNOWN 的既有 evidence_ids 判断之外，也保留 `reason_code=evidence_expired_or_future` 的显式失效结论。未增加事实存储、cache、字段或 reader gate；没有直接 bin 声明时，合法 Java home 兼容仍然有效。新观察重新验证显式 bin 后可正常解析，未让过期历史永久封锁新事实。

新增 `tests/test_unified_runtime_expiry.py` 的 5 个场景覆盖 expired、future、显式 UNKNOWN、无 bin 声明和新事实续验；每个场景同时验证真实 evidence 创建/接纳/投影、两种 registry 正式输入、实际 required resolver 和 Recovery assessment。与既有 action-scoped runtime bindings、runtime evidence records、runtime evidence recovery 三套测试合跑 **68 passed，5 subtests passed**。结果、测试命令和源码 hash 见 `evidence/runtime_expiry_fixed_receipt.json`。

固定 probe wrapper 超时只终止直接子进程的另一发现已交主 agent 修复，由主 agent 的实际 probe/sandbox 验证记录覆盖。
