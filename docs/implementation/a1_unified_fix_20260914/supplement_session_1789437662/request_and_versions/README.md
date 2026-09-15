# 实际 Planner 请求、公开规则与版本对照

本分册只核对 session_1789437662 的四次已记录公开请求、相关安全源码与原验收证据。生产只读；没有原 session 业务加载、科学文件读取、真实 provider/science 调用或审批。请求是应用保存的消息快照，不冒称网络 wire 原包。逐轮业务响应、parser、拒收与 STOP 配对见 [事故证据](../incident/findings.md)，本分册聚焦模型实际拿到了什么、哪里被投影削弱。

**生成修正候选所需的核心规则已在完整请求中，但机器反馈有三个具体缺口，另有一个非本次触发的模板身份错误。** 不能说所有字段都丢失，也不能以规则存在便认定真实模型纠正可靠。最终停止响应不是长度截断；四轮没有发生已记录的压缩或候选丢失。

| 边界 | 实际公开内容与结论 |
| --- | --- |
| 步骤身份 | system 第 160 行是 task-specific `step_id`，第 166 行另有枚举 `operation`；第 103–113 行明确把反馈 `step_key` 用作 association 的 `step_id`，并要求实际声明的步骤 ID。步骤名可以不同于 operation，不能取能力后缀猜身份。 |
| 角色与输入 | system 第 98–115 行要求 association 恰含 `step_id/capability_id/role`，共享工作区按每步显式关联；同时要求 inputs 指向所选 `plan.*` 资源。首候选已有 annotation 关联，实际缺的是 `inputs` 中的 `plan.snpeff_data_dir`。第二候选修了这个输入，三条关联未变。 |
| 能力与检查义务 | 实际上下文公开完整六项 capability；Skill 全文同时公开输入/健康检查义务。独立 `preflight_inputs`、`runtime_healthcheck` 不是枚举 capability。把必要检查放进受支持步骤须保留实际义务，不能任意贴能力或删检查。具体合并后的可满足性由根分册同准入回放验证。 |
| 配置产物 | prepare capability 明确 `produces_artifacts=[artifact.snpeff_config]`，并另列 bindings、observation、workspace mutation；system 明确 outputs 表示产物。第二拒收反馈有完整五项，且后续 requirements 仍带配置产物合同。第三候选增加配置声明及 prepare/build/annotation 的生产消费引用。 |
| 第三候选 | 已无 `prepare_database` 步骤，prepare 能力在 `runtime_healthcheck`，association 却仍引用旧步骤。system 的实际步骤规则、完整前候选以及错误 message 中旧引用都还在，足以定位这一字段修正；结构化反馈却没有直接说“引用步骤不存在”。 |
| 停止 | system 明确无有依据修正时可主动 STOP，也允许合法第四候选通过；request4 的 messages[8] 旧反馈 `remaining_requests=1` 指本次回复机会；messages[9] 最新独立 system 明确 `current_request_ordinal=4`、`requests_remaining_after_this_reply=0`，指本次回复后无第五次请求。二者一致，STOP 描述 0 不构成误读预算证据。不存在“一定报三次错便停止”的规则。STOP 的合规接纳与科学任务完成分开。 |

精确请求路径、消息 SHA、JSON 反馈、正文摘录和逐项覆盖见 [request_evidence.json](request_evidence.json)、[request_rules.tsv](request_rules.tsv)。所有请求此前的固定 seed、完整 Skill、完整 assistant 候选及纠正反馈均逐字节保留，仅每次预算提示更新；四份 manifest 的 `should_compact=false`、`hard_block=false`、`optional_compression_applied=false`，无 truncation 记录。名义字符预算超出不等于实际截断。

## 需要并入原实施主单的窄修项

1. **保留公开角色引用的结构化字段。** `planner_resource_requirements` 在 `agent_core/plan_bindings.py:153` 生成三字段 item，但 `agent_core/planner_context.py:47` 的递归投影沿 `agent_core/node_context.py:82` 的控制键规则删除 `step_id`。R2/R3/R4 的 requirements item 均仅余 capability/role；第三反馈的 `actual.step_id`、`machine_evidence.actual.step_id` 也丢失，没有替代 step_key。应在既有公开反馈边界保留或明确映射合法候选步骤引用，并保持 schema、实际值、修复目标一致；不得全局放行内部身份，也不新增 binder。
2. **分别描述缺读输入和缺关联，并给作用域失败的实际谓词。** `agent_core/plan_bindings.py:553–570` 把 READ_MISSING 和 ASSOCIATION_MISSING 都配同一句补 association 说明。此次关联本已存在；真正可操作线索在 actual.inputs 与 expected.resource_local_refs。`plan_bindings.py:471–479` 对“引用步骤不存在”则只给总 SCOPE_INVALID，未给父资源、被引用步骤、查表命中状态和具体失败条件。应补当前已有诊断的必要局部字段，保持原 scope gate。验收包括已有正确关联但缺 inputs、完全缺关联、引用不存在、能力不符、Skill 不符、角色不符、父类型不符；下一真实请求中的结构化反馈必须保留差异。
3. **修正身份占位符。** 实际 system 第 175 行、`prompts/manager.py:1961` 把 `skill_ids` 写成 `["<selected-capability-id>"]`，但消费者 `plan_bindings.py:475` 要求 capability 的 `selected_skill_id` 在 steps[].skill_ids 中。应写 canonical selected Skill ID；capability ID 留在 execution_capability_id。三份现场候选已经使用正确 `genomics/snpeff-annotation`，所以这是可证明的公开模板错误，**不是本次已证触发原因**。验收用完整正式请求检查两种身份的不同字段，错误互换继续拒绝。

第三反馈 `expected.capability_id` 在最早持久 context 已缺失，不能冒称捕获到了历史 null。当前同源码查表空会产生 None，`plan_tracker.py:759` 的 `_semantic_diagnostic_value` 删除非豁免空值，可以解释后续现象；根代理当前正式准入回放已捕获最早返回的 null，历史原始异常对象仍未提供。这与实际已捕获的 `actual.step_id` 在 feedback→provider 投影中丢失是两种证据等级。错误 message 的文本仍保留旧 `step_id`，不能写成模型完全没有收到它。原 OR 条件在第一项 `None != association.capability_id` 为真后短路；对同一入参计算的 Skill 成员条件也不成立，只能称反事实逐项真值，不能说该次实际执行了两个失败谓词。

根代理的同入口差分回放已证明 P_min 只把 `proposed_artifacts[0].role_associations[0].step_id` 改为 `runtime_healthcheck`，即可通过九段实际准入并到 PlanReview 的 `approval_pending`。因此完整请求中已有满足这个最小改动所需的规则，结构化反馈的缺口仍是应修事项。此结果证明离线可满足性，不能替代真实模型自动修正，也不赋予执行审批或证明科学检查已运行。

## F6 对照与加载证据的范围

现场四轮 system 均与 final08 F6 正常场的实际 Planner system **字节完全相等**：23880 bytes，SHA256 `f36b16645badb2e0d2a06c2cc42b72f2850405c1ab41373ccdc9ce15c17c32d8`。两边公开六项 execution_capabilities 的完整列表也相等。现场还额外收到完整 inherited Skill 源正文，33117 bytes，SHA256 `1ce1295a59668778a08ee331280849deabfbacee242a7dffac5ca92a08650d44`，与当前及 final08 的 Skill 文件相等。F6 候选正确不能证明模型会从相同规则自动构造正确字段；F6 没经历 Planner 纠正，也就没有穿过本次三种反馈弱化。人工预填和变体覆盖由 [F6 分册](../f6_comparison/) 登记。

仅重新核对 20 个相关安全文件，当前源码与 final08 manifest 均相同。事故 runtime identity 提供的是“捕获时源码”与“实际加载 callable”两列，未把 disk HEAD 当加载证明。以同 Python 3.12.12、历史 co_filename，仅编译安全源码而不执行/导入生产，按原 `marshal_v2` 算法对比的 9 个关键 code object 全相等：角色编译/要求/读写角色、Plan parser/准入、三段持久诊断及 Planner system builder。详见 [callable_comparison.json](callable_comparison.json)、[versions.json](versions.json)。这排除了这些具体代码对象的历史差异，不能代替整个模块 globals、配置、依赖或部署 revision 证明。没有捕获的 planner_context/node_context 加载字节仍是 UNKNOWN；其实际反馈投影输出已由原请求直接证明。

## 旧字典形状失败

原失败是 `tests/test_generate_provider_payload_safety.py::test_nonempty_capability_uses_complete_production_message_safety` 第 166 行，要求 `GenerateInvocationAudit.to_dict()` 等于仅含四个 True 的整个字典；实际另有 `generate_call_id/scope/source_ref` 三个既有字段。原 final08 失败回执显示四个布尔值匹配、mock provider 调用一次。它属于 Generate 调用计量对象，不是 Plan 的角色/纠正反馈。

BASE `b877b28b2b0366c04e9042bad09e1ee26a138ab0` 与当前的原 class、原失败函数同源比对见 [既有证据](../../regression/unified_final/preexisting_assertion.json)；完整测试文件并非整文件相等，不能扩大该结论。新增 [组件源账本](existing_assertion_sources.json) 提取两版 class 的完整原字节（含 decorator），没有重写它或改旧断言。两项隔离组件见证已 **2/2 PASS，rc=0**，export02 signature `003eba3d627b37cb918191b8937cdfc234f8e33614c48b62294802f3514c21ce`，前后源哈希无变化，见 [实际结果](existing_assertion_run/pytest-results.json) 与 [加载组件清单](existing_assertion_run/loaded_component_manifest.json)。PASS 表示两版都复现原断言失败，不是原回归通过或修复成功。它们运行真实 class 的 mark_provider_call_attempted/to_dict 并执行原字典断言，成功 assembly 标志是明示组件输入，**不声称整条 BASE provider 图重跑**。旧 9 项 candidate scope fixture 失败仍单列，既没有改断言也没有被本组件覆盖。

本分册没有生产修复或真实模型可靠性验证。修订反馈应验证“真实下一请求能拿到精确区分信息”，模型纠正成功率和科学 E2E 仍需后续另行授权的验证。
