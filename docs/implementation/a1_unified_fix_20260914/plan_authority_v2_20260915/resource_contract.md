# 步骤资源声明：本轮实现与验证边界

当前候选实现采用 `metadata.resource_contract_version=3`。模型在步骤上唯一声明 `resource_uses:[{"resource_ref":"精确已声明句柄","access":"read|mutate","role":"可选的所选 helper 精确角色"}]`。资源声明不再要求回填该步骤的 ID 和 capability，读使用不再要求在 `inputs` 重复一遍。公开 v3 wire 若仍出现 `steps[].inputs` 或资源侧 `role_associations`，会明确报告退役字段，不吞掉其可能表达的冲突。

旧 v2/无 v3 标记的支持范围继续走原来的显式关联和读输入检查，陈旧关联仍拒收。没有把 `prepare_database` 或其他 operation 映射到任何新步骤名，也没有根据路径、扩展名或描述选择资源。新 v3 的 `resource_ref` 必须从当前 authoritative handles 或当前候选的 proposed_artifacts 精确解析。

`step_resource_inputs(step)` 只取明确 `access=read` 的声明；`project_step_resource_usage(step)` 产生既有消费者所需的只读输入视图。`normalize_plan_step` 在持久化/重载规范化时从同一来源重算 `inputs/input_contracts`，不允许该派生索引成为另一个模型编辑来源。重新输出 v3 业务候选时去掉派生 inputs。正式 semantic step 和 dataflow 签名包含原 resource_uses，role/access/ref 的变更都会影响执行定义；本轮不迁移已批准对象。

资源编译可由这些显式引用生成内部反向关联，标记 `resource_use_projection_version=3`。它服务既有资源冻结、allocation、helper 参数与 attester，模型没有第二份可编辑资源关联。allocation 只按已分配的精确映射重写 resource_ref 和原 canonical identity；没有新 binder 或额外 Plan。

`outputs` 继续是 immutable artifact 的唯一生产来源。`workspace_mutations` 暂保留状态前提、后置状态和证据合同，它与资源访问是不同事实；其 workspace_ref 必须精确指向本步骤已声明的 mutate 资源或该声明的领域 role。mutate 不能用于 immutable artifact，也不能仅凭访问权限跳过显式状态/证据声明。选用领域 helper 时，其真实 role、access 和资源类型仍校验；通用路径没有被强制填写未选用的领域 role。

正式任务交付的 required、path_policy 与已确定 canonical_path 从现有 CurrentTaskSpec 按精确输出身份读取。v3 Planner 只为尚未确定的文件名提供 `{output_ref,path}`，不再重填 required/path_policy；已有精确路径可省整行 materialization。对尚未确定 canonical_path 的必需文件，planner_materialized 与 model_selectable 两种策略均要求路径选择；缺失选择会被准确拒收，显式降级正式义务也会拒收。新快照冻结同一 TaskSpec 输出的只读投影、版本与路径选择，最低读取版本为 1.2.3；旧 role 格式仍为 1.2.2，不借新版本迁移旧批准。

Plan 内部资源的必要性与 TaskSpec 输出是不同事实：v3 未标可选的声明默认 required=true，只在明确可选时写 required=false。读取、分配与冻结保持同一默认；显式可选资源仍不能供无条件消费者使用。current02 曾沿用旧 explicit-results1 的默认 false，真实图因此拒绝未标可选的 config/workspace；该开发失败保留，生产改回明确的 v3 默认，未让测试 driver 加 required 来涂绿。旧 v2 explicit-results1 的原默认不变。

保留的旧 SCOPE_INVALID 错误增加具体查表和失败谓词，区分不存在步骤、capability 不符、selected Skill 不符、角色未知和父资源类型错误。新错误使用实际 resource_uses 路径，缺 helper role 的反馈不再要求补写已退役 association。readonly dataflow 的错误位置也回到原 resource_uses 项。

删除 READ 后必须在 success_criteria/checklist 写解释才能重试的旧文本门槛，已改为纯 warning `PLAN_CONSUMPTION_CHANGE_ADVISORY`。没有新确认或说明字段；真正的引用、dataflow、用户义务仍决定准入。Planner 第一阶段和最终阶段都必须只将 blocking 问题作为拒收，warning 不影响 convergence fingerprint。三 Skill 的结构数量门槛也删除，Skill 字段类型和已选择真实能力的身份一致性仍检查。

current04 完整 generic_split 已实际执行并 Commit 八步，FinalAudit 随后暴露直接结果消费者缺口：required mutable_workspace 被要求提供 immutable artifact version。`CompletionGate.validate_final` 本轮只在现有结果分支修正消费：既有 `validate_task_evidence` 先重验各步骤当前执行和 attested effects；必需 workspace 另须通过现有 sealed dataflow 状态链检查，其实际 accepted mutation 的 `formal_concrete_path` 必须匹配正式 canonical_path 且该目录存在，才使用 workspace 的证据路径而不要求 immutable version。TaskSpec 明确 workspace 交付仍受同一检查；缺状态链、缺实际 attestation、路径不符或目录消失均不能靠 resource_kind 通过。此段不是新全局 validator，也不把规划链当运行成功。current05 完整图实际 8 次成功执行和 8 次 Commit 后 FinalAudit 通过；对该次真实 durable state 的副本清空 dataflow receipt 得到 plan_workspace_state_incomplete，删去已接纳 mutation 证据得到 plan_result_incomplete，原 durable hash 保持不变。原 current04 终态失败保留。

规则变动清单见 `resource_rule_disposition.tsv`。测试在 `tests/plan_authority_v2_20260915/test_resource_contract.py`，包括身份/依赖/authority、immutable mutation、实际消费无来源、正式交付防降级、默认/显式可选、reader 版本和非阻断建议。BASE 红测、当前导出和正式图结果由根验证账本登记。本文记录实现与范围，不预填整个新契约 FIX PASS；通用路径、公开请求、正式审批/执行/Commit 与受影响 F6 仍需按最终运行结果合并判断。

current05 的资源组件 27 项和通用 runtime 组件 5 项全部通过（rc=0，导出前后源字节不变），原回执见 `resource_runs/current05/`。current04 原三项新增测试接线错误和回执保留于 `resource_runs/current04/`，没有修改旧测试断言。此 32 项只证明组件范围；完整图 workspace 的真实成功与删证据负例由同版 generic_split 单独记录。

公开通用示例的实测又揭示 v3 仍被旧 workflow 的空字段双写要求拦住。本轮在 v3 单 Plan 将 batch_id 与 workflow_stage_ids 改为可省略，显式提供时保留真实类型及引用校验，v2 必填要求不变。新增组件直接读取公开 manager 原例并经正式 envelope/parser/schema 核验；没有为示例补空字段。该窄改已在 current06/current07 的正式组件中通过；真实公开请求的整链结果见根最终账本。

current06 资源组件 31 项与通用 runtime 组件 5 项共 36 项通过（rc=0，源字节未变），包含公开示例原字节、显式错误类型及 v2 旧要求对照，回执在 `resource_runs/current06/`。限定直接消费者复核在 `resource_direct_review.json`；完整图与旧受影响组以根最终验证账本为准。

后续同轮诊断接缝修补：`_planning_failure_machine_issue` 的 durable ABI 现保留精确 `received_id/available_capability_ids/selected_skill_id` 与 validator owner；已有 validator_id/actual_value 继续保留。空 received_id 与空可用列表是诊断事实，不能在通用清理时丢失。只修公开修正依据的连续投影，不增加能力或修改路由权。真实 SkillManager → normalize → durable machine ABI → provider 的贯通验证由 entry 分组登记；因此 current06 是已跑资源证据版本，不冒充该后续诊断源码的最终验证版本。

本分册资源组件的最后实际执行版本为 current07：31 个资源用例加 5 个通用 runtime 用例全通过。后续保护回归汇总在 `protection_runs/README.md`：155 个唯一 nodeid 中 154 PASS、1 个已存在的 WGS 版本断言 FAIL；包含 subTest 的 call 记录口径为 158 PASS、1 FAIL。current10 对仅出口字符串变化另复跑 39 个 F1/F2 及 5 个实际纠正/停止代表全部通过，未将未重跑部分声称为相同模块加载字节。原失败与外部驱动中断证据分别保留。
