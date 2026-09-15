# session_1789437662：四次 Planner 调用的静态证据

本分册仅解析指定 session 的公开请求/业务响应、Planner correction context、必要状态与报告 receipt。没有业务加载原 session、执行原脚本、访问声明路径、调用模型/科学工具或修改生产/旧测试。原始 response 快照只有 `content/id/role`；提取的是 `/content` 解码后的完整 UTF-8 业务字节，不是 wire 原包或私有 reasoning。`request.safe.json` 是已记录的消息快照，不能单独证明网络 wire 内容。历史 `loaded_revision=UNKNOWN`；记录的 callable/code hash 与捕获时 disk source hash 分开保留。

## 结论

1. 首轮 **已经声明** `annotate_variants` 的 workspace role association，但 `steps[5].inputs` 只有 `input.variant_vcf`，缺 `plan.snpeff_data_dir`。第二轮的实际修正是补输入读句柄；不是新增 association。三轮的三条 `role_associations` 完全相同。首轮反馈文字聚焦 association，`actual.inputs` 与 `expected.resource_local_refs` 才给出这次实际修正所需的区别。
2. 第二轮完整反馈有 **5 项**：两个 `ACTION_CAPABILITY_SCOPE_MISSING`、一个 `PLAN_STEP_ARTIFACT_EFFECT_UNBOUND`、两个 `PLAN_STEP_EFFECT_UNSPECIFIED`。完整 context、provider feedback、下一实际 request 均保留这五项。终端缩写不能当作完整验收 receipt。
3. 第三轮把八步骤改为六步骤：contig capability 迁入 `preflight_inputs`，prepare capability 迁入 `runtime_healthcheck`，增加 `plan.snpeff_config` 声明和 producer/consumer。旧 `prepare_database` 步骤已不存在，但 workspace 的第一个 association 仍指向它。这是可精确定位的候选身份失配，不是相同 capability 与 role 被无故拒绝。
4. 最早可得的第三轮持久反馈仍有 `actual.step_id=prepare_database`；provider 投影及下一 request 的结构化 `actual` 丢掉此字段，`affected_step_ids=[]`，没有替代 `step_key`。错误 `message` 的 Python dict 文本仍含旧步骤名，所以不能声称信息完全不可见。`expected.capability_id` 在最早可得的持久 context 已缺；**不存在可据此冒称历史原 receipt 保留了 null 的证据**。当前 compiler 的 null 输出与上游去 None 机制应由当前回放/源码分册另行证明。
5. 第四轮是独立 `STOP_PLANNING` 业务响应；claim 的 `outcome=model_stopped`、raw digest、call id 均与第四响应相符。真实 terminal 为 `PLANNER_MODEL_STOPPED`，不是第三候选本身，也不是重复/振荡停止。其“校验器自相矛盾”是模型未核实解释；实际第三候选中的旧步骤名提供了反证。第四次请求仍允许提交合法 Plan；实际返回选择了主动停止。

## 四轮证据链

| 轮次 / trace call | parser / completion | 准入或停止事实 | 下一请求的实际情况 |
| --- | --- | --- | --- |
| 1 / planner_initial-0006-b4d94da2af80 | passed / stop / NORMAL_END | READ_MISSING，定位 annotate_variants 的实际读输入 | 前候选逐字节保留，完整反馈进入 request2；剩余 3 |
| 2 / planner_initial-0007-c675cfa528a5 | passed / stop / NORMAL_END | scope ×2、artifact effect unbound ×1、step effect unspecified ×2 | 前候选逐字节保留，五项反馈进入 request3；剩余 2 |
| 3 / planner_initial-0008-4891ab7de2c8 | passed / stop / NORMAL_END | ROLE_SCOPE_INVALID，定位 proposed_artifacts[0].role_associations[0] | 前候选逐字节保留，反馈进入 request4；剩余 1 |
| 4 / planner_initial-0009-9d7ea8c74d00 | passed / stop / NORMAL_END | STOP_PLANNING → claim model_stopped → PLANNER_MODEL_STOPPED | 本 epoch 共 4 次，无第五次请求；repeat_count=0 |

每轮完整 SHA、original file SHA、message SHA、claim/occurrence receipt、response_integrity 在 `trace_index.json`、`four_call_chain.json` 和每轮目录。所有公开前候选再次进入下一请求时的字节 SHA 相等；三次 `provider_feedback.context.json` 都与下一 request 中 JSON 反馈相等。`feedback_projection_deltas.json` 列出 context 到 provider 的完整字段增删，不只列最后一个错误。

报送 token 用量依次为 input/output：21663/4255、29364/4249、37895/4076、45536/547。四份 completion 均无长度终止。输入准入估计依次 22386、30287、38885、46646，四份 manifest 均 `hard_block=false, should_compact=false`。本地请求上限 16384、`resolved_not_wire_observed`、server effective limit null 是记录本身的限制，不能据此确认服务端实际参数或归因为截断/压缩丢上下文。

## 每轮第一处差异与后续暴露

**第一轮。** `round_1/candidate.parsed.json#/proposed_artifacts/0/role_associations/2` 已是 `annotate_variants + genomics.snpeff_annotation.annotate_variants + artifact.snpeff_data_dir`。但 `/steps/5/inputs` 没有该 workspace 句柄。第一反馈 `expected.access=read`、`expected.resource_local_refs=[plan.snpeff_data_dir]`，且明确后面的 step_effects/dataflow/required_workspace_coverage 未求值。第二候选只有三个顶层差异位置：`public_message`、`steps[5].inputs`、`validation_checklist`；实际 Plan 结构的唯一改变就是补读输入。public_message 声称“补角色关联”与字节差异不一致。

**第二轮。** `preflight_inputs`、`runtime_healthcheck` 是两个 `action=execute, tool=bash, execution_capability_id=''` 的步骤，虽然选中 Skill，却没有选其枚举能力。前者声明 `input_files_present_and_readable`，后者声明 `snpeff_runtime_ready`；它们不是运行结果，亦未被当前 effect compiler 接纳。`prepare_database` 的能力有 `artifact.snpeff_config` 产物义务，但候选 outputs 为空。这些是第二阶段真实发现的新增问题，不是同一错误换码。provider 投影剔除了 `capability_artifact_effects` 字段，但完整 requirements 仍公开 prepare 的 config producer 合同；下一候选确实增加 config 文件声明、prepare 输出和 build/annotate 输入。

**第三轮。** 保留的前三项公开检查要求并未全部消失：`preflight_inputs.success_criteria` 合并了输入存在/可读与 typed contig 检查，`runtime_healthcheck.success_criteria` 包含 runtime 健康检查与数据库布局准备。不能只因标题仍是健康检查而认定 prepare 工作被遗漏，也不能因为枚举能力填了就宣称实际检查执行成功。第三轮原 association 精确值仍为 `prepare_database`；实际步骤集合为 `preflight_inputs/runtime_healthcheck/build_database/annotate_variants/summarize_ann/finalize_outputs`。原父声明 `artifact_contract.resource_kind=mutable_workspace`，role 存在。当前准入四谓词的重放/逐项真假值由上级差分回放负责；本分册的原业务字节支持“关联引用了不存在步骤”。

**反馈断点。** 第三轮 `feedback.context.json#/validation_issues/0/actual/step_id` 和同项 machine_evidence 中该值都在；`provider_feedback.context.json` 与 `round_4/request.safe.json#/messages/8/content` 的相应结构均缺失。requirements 中 role_associations.item.step_id 同样被删。顶层 `not_evaluated` 消失但 machine_evidence 内同名单保留。原 message 字符串仍含 step_id，故最准确表述是“结构化修复目标降级为字符串中的间接线索”。对 `expected.capability_id:null` 的最早历史现场返回值没有原样持久 receipt，本分册不以当前结果填补历史。

**第四轮。** `round_4/business_response.txt` 包含八项未执行工作、前三次尝试的模型描述与主动停止解释。`planning_epoch.static.json#/call_claim/response_integrity` 对应 call 0009、ordinal4、raw digest `6ef8215f...9370`；完整配对在 `four_call_chain.json`。第三拒收仍作为 latest_failure，第一拒收仍作为 primary origin，不应把报告列出的“末次拒收 provider 0008”误认成第四 STOP 缺失。

## 第四请求预算：1 与 0 同时出现，但计数时点不同

`round_4/budget_reconciliation.json` 逐一核对全部 10 条公开消息。该请求保留三份历史反馈：messages[4]/[6]/[8] 的 `planner_calls_total=1/2/3`、`remaining_requests=3/2/1`、`next_candidate_ordinal=2/3/4`；三者均 `nominal_call_limit=4, repeat_count=0, cycle_detected=false, active_stop_available=true`。这些是每次拒收后的预算快照，不是三个同时生效的当前余额。

最后还有独立 system 消息 `messages[9].content`，其 `planner_request_budget` 完整值为 `total_limit=4, requests_already_dispatched=3, current_request_ordinal=4, requests_remaining_after_this_reply=0`。因此第三拒收反馈中的“剩余 1”已由本次第四请求占用；“此回复之后剩余 0”指没有第五次新请求。system messages[0] 同时明说合法第四候选仍可接受，最后的 system instruction 仍允许选择主动 STOP。

第四次 STOP 自述“剩余请求为 0”与这条公开 `requests_remaining_after_this_reply=0` 相容；不能仅凭此认定模型错误读取预算，或代码提前耗尽、强制本轮放弃提交。第四轮实际选择 STOP 是响应事实；它是否受哪个预算措辞影响没有私有推理证据，本分册不推断。请求里 `convergence/no_structural_progress` 只出现在通用 system 规则中，没有当前值或独立 convergence/planner_loop 消息；持久终态的 planner_loop 不冒充本次 provider 可见内容。

## 停止和报告边界

静态状态中 steps 为空、approval_record 为空、can_generate=false、can_request_approval=false、execution_status=not_started；managed stop 同时声明 `no_approved_plan=true, plan_materialized=false`。存储兼容字段 `plan_status=pending_review/approval_status=awaiting_review` 不构成已经通过审核的证明。`execution.executed=false`、attempt_id 空、returncode null、validation/reviewer 未执行、committed=false。

`report.md` 与 `stage_status.tsv` 的实际字节 SHA 与 durable publication/stop receipts 一致，状态 created/published；`reproduce.sh` 被明确 skipped/NO_EXECUTION_ATTEMPT，没有读取或执行任何原脚本。报告正文的 not_attempted 表述标明“截至说明生成时”，最终写入结果以持久 receipt 为准；不把这段早期状态解释成报告最终拒写。没有科学执行、ANN、数据库构建或环境健康检查成功证据。

## 文件入口与限制

- `trace_index.json`：四 call 原来源、精确业务与请求/manifest SHA。
- `round_N/business_response.txt`：精确公开业务字符串；`candidate.parsed.json` 是仅 stdlib JSON 解析，`candidate.context.json` 是当时持久归一化候选，二者不可互换为原稿。
- `round_1..3/{feedback,provider_feedback,occurrence_receipt,response_integrity}.context.json`：完整公开 correction context 投影，`context.source.json` 给对象来源与 JSON pointer。
- `candidate_deltas.json`、`feedback_projection_deltas.json`、`four_call_chain.json`：结构差分、反馈损失、逐轮配对。
- `task_declarations.json`：仅 accepted TaskSpec 声明；inputs 路径只按字符串保存，无访问。现场输出为一个 managed_exact output_dir 加五个 planner_materialized 子输出。`raw_user_submission.txt` 是原用户公开约束文本。
- `runtime_identity.static.json`：原 runtime 对象中的 callable/hash/version 元数据；嵌入源码正文未导出。本分册不把 capture 时 disk source 归为历史 loaded revision。
- `state_boundary.static.json`、`round_4/managed_stop.static.json`、`reports.source.json`：必要静态终态与写入证明。

本分册没有动态测试、没有生产修复，也没有真实 provider/science 再验证。原请求可用性/公开协议与 F6 fixture 的对照、当前真实准入 P_min 回放由同一补充任务的其他分册交付。最早 compiler 异常未经持久序列化之前的完整历史对象未保存，明确保留 UNKNOWN；它不阻塞已记录接口和公开业务字节的审计。
