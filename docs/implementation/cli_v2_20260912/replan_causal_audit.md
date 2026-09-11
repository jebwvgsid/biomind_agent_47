# session_1789142344 重规划因果审计

本轮只读审计确认了实际路由链：Recovery 原生提交 `action=revise_plan`，接纳后生成 `plan_revision_required`，生产条件边对应 Planner。更重要的问题在交接处：重入 Planner 的真实请求没有携带该修订指令或本次失败原因，而是 `create_complete_plan / attempt=1`；它返回的首步再次使用 `inspect + purpose=probe + plan.input_check_report`。这不是仅凭终端相邻阶段推断的结果。

另一个独立发现是 `value_present=false` 被 Recovery 解释成产物失败原因，但实际正式 gate 拒绝的是仍处于 `expected` 的 provenance 产物。脚本已经写出文件，结构化 parser 也接受了 marker；当前基线代码会因 `purpose=probe` 且未允许 probe 作为正式 producer 而跳过产物版本登记。不能以“补一个 value 字段”作为已经确认的修复。

## 范围与版本

- 审计基线：`e75b95ecce117e9128c8b9079e554f6332c0f3b1`，分支 `refactor/consultant-controller-authority`。开始时 staged 为空；父任务记录的原有 437 项 dirty/untracked 继续保护。本产物不暂存或提交其他文件。
- 静态证据根 `S`：用户指定的 `benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789142344`，位于仓库相邻的毕设目录。下面 `S/...` 都指这一根，不是全用户目录搜索。
- 先列文件名、大小、mtime，再用标准库 `pathlib/json/hashlib` 读取必要字段。没有导入 BioMind、调用 session/state/business loader、打开原数据库、执行原脚本、读取认证/环境配置或敏感环境变量。
- Generate 0009 与重入 Planner 0013 的 manifest 均记录 `disk_head_at_capture=e75b95ec...`、`loaded_revision=UNKNOWN`、`loaded_code_identity=214a5ef099b5a55486e9484f51d0869edfe52383ec1cb012577c9d9fba3654e5`。关联 dirty 为 `agent_core/workflow/managed_stop.py`、`prompts/manager.py`。不能把磁盘 HEAD 等同于已加载代码的 Git revision。
- 本报告的源码行号固定指 `e75b95ec...` 提交内容，避免与并行 CLI 修改的工作区行号混淆。运行时记录提供 callable identity，但本次未证明全部已加载函数与该 Git tree 逐字相同。
- 原 S1 终端附件在本子任务材料中缺失，原 UI mode、尺寸以及 S1 的逐字显示顺序保留 `UNKNOWN`。规格中转述的 S1 不是独立原始证据。本审计使用 session 中实际 receipt，且发现了 S1 结束之后的取消记录。

配套 `replan_evidence_chain.tsv` 给出每个箭头的证据与源码条件；`replan_evidence_projection.json` 保存 14 个读取文件的 SHA-256/大小/mtime，以及最小脱敏字段。它不包含完整原脚本、原 stdout、FASTQ 内容或全量 session。

## R1：路由是否合理，是否需要改 Plan

**事实：实际提交和接纳均是 revise_plan。** `S/context_trace/events.jsonl` 第 95 行的原 native `submit_recovery_directive` 参数，与 `plan_state.json.task_state.typed_recovery_receipts` 中存下的 directive 完全相同：

```text
directive_id = recovery-directive-236963ff06d2d48b67f3fe75
action = revise_plan
reason_code = CONTRACT_DRIFT
target_step_id = 00.input_check
directive_digest = sha256:fa6adf7edc29de0015d3c7bdb2a45777ad008ad2eafa78d98651e230a19bd26a
typed outcome = plan_revision_required
```

第 97 行是 `candidate_validated`，当时 `directive_adopted=false` 是接收层状态；第 99 行在 `2026-09-11T16:03:41.136716+00:00` 记录 `recovery_directive_admitted`。持久化 typed outcome 含相同决策、失败 packet/attempt scope 与预算 receipt（第 1 次，剩余 1 次）。不能拿前一个接收状态否认后续实际接纳。

`agent_core/workflow/recovery_runtime.py:326` 将明确的 `revise_plan` 映射为 `plan_revision_required`；`runtime_graph.py:134` 的 `route_recovery_outcome` 将其映射到 `planner`，完整图在第 439 行绑定该条件边。随后真实 Planner 0013 manifest/request/response 均存在。没有证据表明 UI 文案、旧 route 字段或异常 fallback 把另一个 directive 偷换成 Planner。

**需要协调执行定义：YES；需要重写整个流程：未证实。** 批准 snapshot 中 `00.input_check` 同时声明 `purpose=probe`、能力 `genomics.wgs.gatk.snp.indel.inspect` 和正式文件输出 `plan-artifact-f060198a60d77f58de3bdc70`。本次 action 保存的能力合同明确要求 “produces no durable output file”，且 `produces_artifacts=[]`。这是实际合同冲突，不是从 inspect 名字猜出来的。至少要协调能力、purpose、输出声明和下游消费者；仅重试同一脚本或添加 marker.value 无法解决这个定义冲突。但不据此授权删除用户所需证据文件，也未证明必须重写整条 WGS 流程。

**主要交接缺口：修订内容未进入实际 Planner 请求。** Planner 0013 的三个消息已逐字段核对：phase seed 仅含 `consultation_handoff / schema_version / selected_skill_manifest`；semantic history 是原 conversation、TaskSpec、Skill/capability 与 approved plan；`current_focus.planning` 为：

```json
{"mode":"create_complete_plan","attempt":1,"instruction":{"return_complete_plan":true}}
```

请求不含本次 directive id、`CONTRACT_DRIFT`、`plan_revision_required`、本次 gate 错误或修订指令首句。对应模型 response 的首步改名 `step.00.input_check`，但仍为相同 inspect 能力、`purpose=probe`、`outputs=["plan.input_check_report"]`。这是已记录的请求/响应事实；不是用模型未来行为补足因果链。

源码也有相符缺口：`bio_agents.py:38451` 只取 `planner_feedback`，第 38456 行把非 pending feedback 清空；本次 durable feedback 已是 `applied`。`agent_core/node_context.py:2518` 在没有现行 planning failure/override 时生成上述默认完整规划 focus。`PlannerSeed`（`stage_contracts.py:623`）有 planning_constraints/evidence_refs 容器，但当前请求没有 Recovery 修订信息。应独立补审“已接纳 Recovery outcome → 带 scope 的 Planner 修订输入”，而非修改 UI 或仅改候选计数。具体历史函数分支没有独立执行 trace，故源码解释标为强支持推断；请求缺失本身为事实。

## R2：产物 identity 与 value_present 在哪里变化

1. 批准 Plan snapshot 与实际 action/attempt 关联一致。generated.py 与 approved.py 都是 9,595 字节，SHA-256 为 `2485883c65d4a002034d38fbaa998e5ee726b7ed4b50b0f7bc342f89c3a47884`；实际 wrapper 为 2,136 字节，SHA-256 为 `0e9ed672a2d1ea3200b301b2f2b3a3a9837bd17c4c788c93cfe9b4ffaf492bb8`。执行记录中的 lineage 与静态文件哈希吻合，没有发现批准后脚本被显示层改写。
2. 原 stdout marker 的 artifact 条目是 `{identity: plan-artifact-f060..., path: <SESSION>/plan_artifacts/plan-artifact-f060...}`；`validation_observation` 另有 value。stdout 为 4,353 字节，SHA-256 为 `bb419fbdb6ab6178dda912e0e5a37b5ec2c92d0989b214f27af9de0ac669f74b`，与 parser receipt 完全相同。receipt `emission_count=1 / status=parsed / issues=[]`。原 stderr 为空。
3. 记录中的文件存在，size=7,733，`current_attempt_owned=true / content_valid=true`，reconciliation=`accepted`，`validated_artifacts` 也包含该路径。未观察到收集器把真实文件丢失或绑定到另一个 attempt。
4. `bio_agents.py:49721` 构造 `effect_evidence.produces_artifacts` 时用的是 `validated_formal_artifacts` 路径字符串列表，不是原 marker 字典。实际记录正是该路径列表。`semantic_completion.py:1177` 的 `_derive_report_facts` 对列表重新投影，第 1240 行为所有无 value 的条目标记 false，产生报告中的 `{identity:<绝对路径>, value_present:false}`。这解释了 identity 的形态变化；它并非“path 被显示 renderer 改成 identity”。
5. 正式 gate 的 `invalid_artifacts` 是 provenance ID `artifact_e387b60256f769a22354`，其 head 为 `artifact_e387b60256f769a22354@expected`，`status=expected`、producer/validation 为空；attempt 的 `produced_artifact_ids` 与 `artifact_version_ids` 均为空。`CompletionGate.validate_step_evidence`（`completion_gate.py:2489`）在第 2533 行按 `status != valid` 判 invalid，未读取 `value_present`。non-artifact 的 observation 已满足，missing_effects 为空。
6. 基线 `plan_tracker.py:2667` 的 `execution_finished` 分支在第 2676 行把 `purpose=probe` 识别为 probe；第 2754 行在 `is_probe and not allow_probe_producer` 时不创建正式产物版本。本次 execution purpose 就是 probe，artifact 定义未开启 allow_probe_producer。这一源码条件与保留 expected 的静态状态吻合。`validation_recorded` 只提升 attempt 已登记的 artifact versions，因此无法凭局部 VALID 补出被跳过的版本。

因此直接根因链应表述为：**批准的 capability/purpose 与正式 artifact effect 冲突 → probe provenance 规则不登记正式产物版本 → gate 看到 expected 而拒绝**。执行返回到登记分支的历史逐指令 trace 不存在，故该中间分支为源码与现场字段共同支持的推断；文件存在、parser 接受、版本缺失和 gate 拒绝均为事实。

上游防线也值得独立审查：`step_effects.py:1050` 的 `_unsupported_explicit_effects` 在第 1065 行刻意跳过 produces_artifacts，交给 artifact-flow 身份检查。批准 snapshot 的冲突确实穿过了规划编译。不能据此全局禁止 inspect 写任何诊断 scratch；本案依据是这份明确的选中能力合同和正式输出声明。

## R3：缺依赖与 proceed 为什么同时出现

批准脚本 `probe_tools()` 第 64 行使用进程继承 PATH 的 `shutil.which`。wrapper 记录的解释器后缀是 `envs/biomind/bin/python`，未记录 conda activate/run 或 PATH 切换。原 marker 显示 gatk/bcftools/picard 未找到；每项都记录了确切 probe_command 和 affected_stage。它证明的是本次探测环境，不能推广为服务器没有安装。

`main()` 第 191–197 行仅把参考缺失、FASTQ 缺失和配对未确认加入 blocking；missing_tools 在第 188 行单独计算，没有加入 blocking。第 208 行只依据 blocking 决定 proceed，所以两者会同时出现。这里存在原脚本语义缺口，而不是 renderer 隐藏了一个已有 blocked 状态。原任务的依赖缺失 stage-blocked 约束可在 durable planner_feedback 第 4 条确认，不能把它降格为可忽略建议。

另一方面，目标 `bio_gatk` 的两条既有 runtime evidence 都是 `inspection.status=execution_error / coverage_complete=false`，environment prefix availability=`UNKNOWN`，原因 `inspection_execution_error`。当前 inspect action 的 runtime binding manifest 是空 bindings，expected executable 列表为空。这不能证明后续实际 GATK 能力环境也缺这些工具。独立业务修复应先补目标有效环境的事实边界，再按用户要求明确受影响 stage 的 blocked/unknown 和缺失命令；本轮未安装、切换环境或运行任何检查命令。

## R4：为何 VALID 后又 Recovery

现存 completion report 明确区分了层级：

| 层级 | 本案记录 | 能否证明完成 |
|---|---|---|
| 进程 | rc=0 / execution_status=succeeded | 仅退出事实 |
| 局部 ValidationEvidence | VALID / returncode_passed=true / evidence_complete=true | 不是正式 step commit |
| 原文件收集与 parser | 文件存在、reconciliation accepted、parser parsed | 不是 provenance artifact adoption |
| 正式 StepEffect gate | step_effect_incomplete / passed=false / invalid expected artifact | 正式拒绝 |
| Completion report | deterministic_veto=true / effective_disposition=inspection_required | 进入 Recovery，不能 CommitStep |
| Recovery | 明确 revise_plan 被接纳 | 才能表述为正在修订计划 |

`supervisor.py:2394` 输出的是 `ValidationEvidence.status`；`runtime_graph.py:105` 将正式 `inspection_required` 映射到 Recovery。该状态并不自相矛盾，容易误导的是没有 scope 的成功标签。UI 可将局部 VALID 标为局部检查，并把正式失败原因来自 gate/report 的事实单独显示，不能把 Recovery 的诊断自然语言升级为正式核验。

由于原 S1 未提供，未在本次读取的结构化字段中独立重建 S1 那条字面 `ok=False`。已有目标环境 UNKNOWN/execution_error 与其可能相关，但不能断言是同一个生产者或时点；应保留这项缺口，而非删除环境失败提示。

## R5：前轮 CLI 是否引入调用或路由

实际提交链为 CLI-1 `fe73e9b0ef1e427e6a6597d21cb0403cf3f84b3b`、CLI-2 `1818f3e1c8cfe30d29b570aede25663734d7c152`、CLI-3 `54b6025bb96e65efc70d8017bb63dbc8ad73bd2d`，再到本次基线文档提交。

对比此前审计提交 `8769e3a4...` 与本案 disk base，runtime_graph、recovery_runtime、early_nodes、phase_subgraph、node_context、plan_tracker、completion_gate、step_effects、semantic_completion 这 9 个相关业务文件的 Git blob 完全相同，完整 blob ID 见 projection。这支持路由映射、probe artifact 规则和 Planner 默认 focus 并非 CLI-1/2/3 新增。CLI 确实修改了公开字段 prompt、传输副本和显示出口，不能把“renderer 等价”扩大为“真实模型决策必然不变”。

本案 Recovery 记录是 2 次原有业务模型轮次、1 次真实 failure.inspect、0 次记录到的额外旁白调用。没有记录实际 evidence.read 调用；directive 引用的那些 inspection refs 同时存在于可读目录/seed，不能把引用数当工具执行数。根任务的 UI 开关等价验收另行报告；本静态审计不冒充真实模型对照实验。

## R6：后续是否又执行

S1 之后有实际 Planner 0013 请求和完整 response；仅“新 Planner 候选 1”不代表任务重启。该 epoch 有 parent_epoch，局部 call ordinal=1；原批准 Plan revision 仍为 1，没有新的批准或 CommitStep receipt。

最后 `plan_state.json.task_state.status=cancelled`，取消回执时间为 `2026-09-12T00:05:19+08:00`，source=`asyncio_cancel`、caller boundary、initiator=`unknown`、`command_started=false`，active_provider_ref 指 Planner 0013。现有 execution_records 仅 1 行，provenance attempts 仅 1 项，未见重入后的第二次执行。可以说“提供的后续材料结束于该取消状态”，不能说谁点击了取消、一直卡住、无限循环或 WGS 已成功。

## 独立修补建议与限制

1. 优先核对已接纳 Recovery 的 scoped 修订语义如何进入 Planner 的实际 provider request，保留失败 packet、决策和批准基线关联，测试发生修订后仍保留修订范围。此项是业务交接补丁，不能混入 CLI 变更。
2. 审核 Planner 编译时 capability 的无 durable-output 约束、purpose=probe 与正式 produces_artifacts 的一致性；在批准前明确拒绝/要求协调冲突。不能自动删除报告输出、降低 CompletionGate，或直接打开 allow_probe_producer 绕过现有隔离。
3. 修正诊断解读：把 provenance 未登记原因与报告层 value_present 分开，不能以给 artifact 添 value 作为解决判据。
4. 独立修正 preflight 缺依赖语义与目标环境不确定性；明确依赖探测的 scope，按原约束记录受影响阶段。不得用当前 PATH 结果断言所有执行环境缺失。

验证方式：运行标准库静态提取脚本 `python /tmp/biomind_route_audit_20260912/project.py`，对 14 个安全源文件生成 allowlist projection，并复核读取前后 SHA-256 一致（`SOURCE_HASHES_UNCHANGED=True`）；验证原 native directive 等于持久化 directive、stdout hash 等于 parser receipt、批准脚本 lineage hash 与静态文件一致。未运行业务函数、pytest、真实模型/API、WGS、原 session 或数据库。本审计没有修改任何生产、测试、业务合同、路由或 CompletionGate 文件。
