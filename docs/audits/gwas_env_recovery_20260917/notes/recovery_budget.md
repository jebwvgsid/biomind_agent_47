# Recovery 完整请求预算、引用与持久读取增量审计

本记录对应增量任务书 §8。结论是：本例的 `RECOVERY_SEED_CAPACITY_EXCEEDED` 来自独立的 32,000 **字符**入口限制，发生在完整请求 token 预算之前。当前已有完整请求计量、首次继承材料压缩、后续调用复核、分段压缩与有限停止机制。不能把这些能力统称为“没有压缩”。本次还确认两处边界缺口：压缩输出预留与最终调用参数不一致；大工具结果归档所返回的引用不在 Recovery 正式读取目录中。

本轮只审计。生产代码、原 session、科学数据及环境均未修改，未调用模型/API，未执行原失败动作。取证代码只在 `/tmp/biomind-recovery-budget-audit-ca_01bht` 的副本中运行。冻结基线为 `82c27867d862ef32d657fbc6162c70d4bc06492c`。本记录源码行号对应本轮冻结源码；相关 Recovery 六文件与 9/17 共同实际前置基线逐字节相同，见 [source comparison](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_source_comparison.json)。因此以下边界不能归因为本轮环境/binding 修改新引入。

## 1. 原现场与离线假设必须分开

原现场 `session_1789648505` 的日志 `biomind_3621046.log:5658` 为 `chars=32097 limit=32000`，Recovery model turn 和 tool call 均为 0。应用日志支持 `context_limit_tokens=200000`，日志模型标识为 `deepseek-v4-flash`。200,000 是应用配置；本审计未确认厂商实际上下文容量，也没有厂商 tokenizer 或 API 用量回执。

独立重建从只读 JSON 取得原失败 packet、inspection registry 和 8 个 immutable objects。最终停止后的 current plan record hash 已不同，因此只在**内存副本**中用 packet 所指向的 immutable `recovery_inspection.plan` 中 approved plan 复原故障时 PlanSpec；重新计算的 hash 与 packet 的 `sha256:4ac35c245b352112e9c1559c41b927dbf6ec4d17373c55f1b1e759014c474ccc` 精确相等。没有 `load_plan_state` 的迁移写入，也没有恢复运行旧 session。

原门见证调用真实 `project_recovery_seed`、registry 构建、freezer 和 `project_recovery_seed_for_provider`，复现同一错误。随后唯一假设是临时把隔离进程中 `RECOVERY_SEED_HARD_LIMIT_CHARS` 放宽，便于观察后续真实组装、预算和归档；生产文件未改，finally 恢复 32000。模型叶子仅捕获 messages、bind_tools schemas 并返回一个不采纳、不执行的 blocked candidate。此假设**不是原现场已发送的请求**，也不是实际 SDK 序列化/HTTP 请求。

完整证据：[capture](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_capture.json)、[完整假设请求](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_hypothetical_provider_request.json)、[summary](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_budget_summary.json)、[原取证方法保全](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_prior_seed_measurement.json)、[输入 hash](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_input_manifest.json)。所有生产模块来自冻结副本且 hash 匹配。最后再次验证原始 plan_state 字节 hash 未变。

## 2. 实际组装顺序与两套容量边界

源码顺序如下，不能简化为“先只装 seed，再去读 plan”：

1. `recovery_node.py:1772` 投影 RecoverySeed；`:1785` 构建有 scope 的 inspection registry。
2. `:1796` 调 `_initial_inherited_materials`，它在 `:1074` 起取得 selected Skill 材料，并用正式 `InspectionObjectReader` 读取本故障的 execution/script/action/task_context/plan/parser 等正文；`:1800` 正规化材料，`:1805` 尝试现有活动 context epoch 恢复。
3. `:1883` 创建 `RecoveryAgentLoop`；其构造器绑定 11 个工具定义。`:1892` 调 loop，进入 `recovery_agent.py:1787` 的 seed 投影与字符门。
4. `recovery_agent.py:317–380` 冻结 provider seed 后 `_canonical`，超过 32000 字符即抛错。此处不看应用 token 配置，也不调用 compactor。16000 target 仅用于容量遥测，不是自动缩减算法。
5. 只有通过该门，才进入首个 `call_model` 的 `_context_budget_snapshot`（`:705`、`:963`）。预算包括 System、frozen seed、继承材料、局部消息、工具 schemas 等。每次后续调用均重新计量。
6. token 压力会返回 compaction boundary 给 node。node 先归档 episode（`:1934`），构造 source manifest（`:2045`）并绑定 `source_archive_ref`（`:2059`），再 `prepare_compaction`（`:2076`）。安全失败回退与硬停止在 `:2165` 后处理。

因此应用的 200k token 配置与压缩开关确实存在，但原现场从第 4 步退出，未抵达第 5–6 步。日志没有 Recovery provider trace 与此吻合。

## 3. seed 的精确组成

真实投影为 **32,097 字符 / 32,953 UTF-8 字节**，canonical SHA-256 为 `6cc4ed37aea1c457b87203fb752b9c4b0ee1af714a63620ff3e504077cce83cd`。六项精确加总：

| 部分 | 字符 |
|---|---:|
| inspection_manifest | 17,250 |
| failure_packet | 10,240 |
| task_semantic_slice | 2,626 |
| current_step | 761 |
| selected_skill_manifest | 712 |
| 其余字段及 JSON 结构 | 508 |
| 合计 | **32,097** |

seed 自身不是完整 TaskSpec + 完整 plan。`task_semantic_slice` 是切片；完整 TaskSpec 和 plan 在独立继承消息中。另外 failure projection 中 diagnostic facts 实测 8,589 字符，其中 result realizations fact 7,020；包含 5 个 declared results 与 10 个不同路径的 observed candidates，不能把它们当作十份相同错误文字删除。这一实际 packet 不含 `input_bindings` 字段。

## 4. 完整首请求到底有多大

越过字符门的**离线假设**共 **16 messages + 11 tools**。loop 所见 canonical message 字符量 270,965。11 个 tool definitions canonical 共 5,382 字符。下表使用真实 `_context_budget_snapshot`，不是把字符数当 token：

| 计量部分 | 估计 token |
|---|---:|
| System | 3,782 |
| tools schema | 1,794 |
| frozen inputs | 12,064 |
| inherited materials | 77,117 |
| local messages / pending results / provider wrappers | 各 1，共 3 |
| 总输入 | **94,760** |
| 预留输出 | 8,192 |
| 估计 margin | 4,096 |
| safe input = 200000 − 8192 − 4096 | **187,712** |
| 压缩触发 / 目标 | 170,000 / 130,000 |
| 当前估计余量 | 92,952 |

计量器为 `mixed_script_reference_fallback` / `ascii3-cjk1.25-other2-v1`，明确为 estimated；代码在 `context_compaction.py:397` 起。隔离运行未加载可用 tokenizer cache，使用项目现有 fallback。遥测另报 uncertainty 23,883；它并非自动加到 admission 的额外扣额。即便当前估计加此 uncertainty，本例仍明显低于 safe input，但这不能替代厂商 tokenizer 精确证明。SDK role 包装、传输层附加参数等没有实际 HTTP 抓取；`provider_wrappers` 当前传入空对象，项目用 margin 承担未知开销。缓存 token 仍计入输入，未扣除。

真实预算认为本假设首请求无需压缩：一次离线响应、零工具调用。不能据此宣称真实厂商接受、真实 Recovery 策略正确或修复已执行。direct-loop 见证未安装外层 cumulative incident ledger；该 ledger 是另一个累计资源边界，不等于单请求 context admission。

内容最大的继承消息是完整 plan（message content 135,297 字符）；task_context 18,752；script 12,268；execution 7,037；5 个 selected Skill 消息分别 4,952 / 5,977 / 4,343 / 6,000 / 5,204。另有 action 2,189、parser receipt 1,953，以及 3 个“正文未产生”的 placeholder。System 11,250；seed 加 XML 包装 32,149。逐消息 hashes 和大小见 summary 的 `message_parts`。

原 plan 正文 canonical 134,567，其中 approved_plan 134,303。它不是仅 1,251 字符的 plan_document，还含 compiled_dataflow 33,596、plan_resource_bindings 31,449、steps 27,491、plan_artifacts 18,126、plan_compilation_receipt 9,480 等。task_context canonical 18,014，包括完整 TaskSpec 15,937 与 consultant handoff 1,786。精确字段大小见 `body_parts`。这些是比 seed 字符门更大的完整请求来源。

## 5. 27 个引用逐项审查与重复边界

逐项 ref_id、digest、版本/selection、scope、allowed_tools、可读声明、实际回读结果见 [27 行矩阵](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_reference_matrix.tsv)。该矩阵描述投影前的 raw registry manifest；下面字符表和重复数则专门计量 `_inspection_manifest` 处理后的 **provider seed 表示**。两者身份相同，字段裁剪/编码不同，不能混算。27 个 ref_id 全部唯一，没有重复 identity 可以直接删行。

| 类别 | 条数 | canonical 各行合计字符 | 作用与本次实证 |
|---|---:|---:|---|
| state_contract_inspection | 11 | 9,096 | task/plan/step/attempt/episode + object revision；8 个正文实读、3 个不可解析占位 |
| managed_artifacts | 9 | 4,988 | 4 个输入与 5 个 expected 输出；这里只核 metadata，未据 readable=true 推断文件内容有效 |
| selected Skill resources | 5 | 2,327 | 各 Skill 身份独立；真实 carry 得到 5 份 exact selected material；没有另跑 SkillService read |
| causal failure packet | 1 | 424 | 当前 outcome 权威入口，failure.inspect 专用 |
| opaque execution evidence | 1 | 387 | 明示 resolvable=false/readable=false，无 locator，不伪造为可读文件 |

行合计 17,222 加数组逗号/括号 28，等于 17,250。投影前 raw manifest 的对应分组行字符为 inspection10,399、artifact6,113、Skill2,917、failure549、opaque488，不是组成32097的数值；summary 用 `raw_...` 与 `provider_projected_...` 分开存储，并另存 [provider projected seed](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_provider_projected_seed.json)。5 个未发布输出明确 `source_uncertain=true`、`diagnostic_only=true`、`formal_consumption_allowed=false`，不得因缩减 manifest 丢失这些限制。输入 artifact version 空缺与 filesystem_readability=NOT_CHECKED 也必须诚实保留，不可补造 producer attempt。

可确认的表示重复是同一行 top-level 与 scope 中的同值字段：readable 20 次（303 字符）、source_object_id 11 次（493）、allowed_tools 11 次（363）、unavailable_reason 3 次（150），合计 1,309 字符（不计分隔逗号）。11 个 inspection 还重复共同 task/plan/step/attempt/episode scope。它们属于可采用共享 envelope 的**表示重复**；这些字段仍用于 reader scope 和 durable index 校验，不能在权威对象中直接删掉。

完整 plan 中也有真实相同子对象：例如 510 字符的 vcf_samples resource 在 compiled_dataflow.resources.9、plan_resource_bindings 的 artifact ID 索引与 `plan.vcf_samples` 别名索引中精确重复。summary 存了 >=500 字符的所有相同 hash/路径组。它们是正式结构的多索引表示，不宜反向改写 approved PlanSpec；可以只改善 provider 展示并保留正式原件回读。

正文之间也有必要交叉：seed 当前 step/失败事实、plan 当前 step、script/execution、step_attempt snapshot。关系不同，不以文本相似度合并成一个“事实”。generation_context（141,366 字符）虽列为 ref，可正式分页读取，但当前默认首轮继承列表不把它整份带入；本次没有发现把整个 Generate 对话历史直接拷给 Recovery。

## 6. 真正的持久读取与权限

在进程 A 中用正式 `InspectionObjectReader.read_body/read` 对复制持久库读取，再彻底退出，进程 B 重新 bootstrap/import，从磁盘重新构造 seed/scope/reader。8 个对象正文 canonical hash、字节数和内容完全一致：step_attempt、execution、script、action、task_context、plan、parser_receipt、generation_context。前 8 行分页响应均包含 total_lines、cursor、digest、object_revision、scope，可继续渐进读取。

validation、completion_report、semantic_decision 是 registry 中的 metadata-only 条目，均按真实 reader 返回 `INSPECTION_REF_NOT_RESOLVABLE: NOT_APPLICABLE_NOT_PRODUCED`。这是未产生，不是压缩把已存在正文丢了。两个进程都得到同样结果。

真实 reader 的负例同时成立：错 attempt → `INSPECTION_REF_SCOPE_MISMATCH`；object revision 改成 2 → `INSPECTION_OBJECT_REVISION_MISMATCH`；allowed_tools 改为 artifact.read → `INSPECTION_TOOL_NOT_ALLOWED`；digest 改动 → `INSPECTION_REF_INDEX_MISMATCH`。源码 `inspection_evidence.py:481–543` 在 opening body 前验证 descriptor，从 durable plan_state 重新确认 current packet/index，再读取 hash-addressed object。未用内存缓存代替持久权限。

本故障中的 rc=0 能在 step_attempt/execution 原件精确找回；`OBSERVED_ARTIFACT_CONTENT_INVALID` 能在 step_attempt 原件找回。此处只验证事实未丢，未把 validator 拒绝改为成功，也未用压缩摘要确认 Q 行与样本对应。具体见 [restart evidence](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_restart.json)。

新增缺口：一个 304,119 字符的 synthetic 工具观察走真实 `RecoveryObservationStore`，归档正文 hash `a3235ad07097caafcc6af4e8375ead281c4c8d2f814ab1151cc83c48bf64c82d` 在重启后仍完全可由 `PhaseArchiveStore.read` 取得；然而返回给模型的 `phase-archive:...` 引用不在当前 registry，正式 `evidence.read` 与 `step.inspect` 都报 `INSPECTION_REF_NOT_DECLARED`。同进程与重启结果相同。这证明“正文落盘”与“当前 Recovery 能按允许工具回读该引用”不是同一验收点。原工具若已有另一个 declared ref，可能重读原 ref；本见证只对新 observation archive 所宣传的回读句柄确认缺口，未泛化为所有工具结果不可恢复。

## 7. 首次、后续、压缩自身与停止边界见证

以下均为隔离 synthetic 压力，未把原 session 当作输入运行动作。core 包括真实 Recovery loop、预算计量、工具观察 externalization、archive、compaction request/validator；模型/超大工具返回叶子是明确替身。

| 见证 | 实测结果 | 说明 |
|---|---|---|
| 小 seed + 首次巨大 inherited material | 279,843 input >187,712；0 business model call，返回 token_pressure | 首次继承材料已有预算/压缩入口 |
| 单大工具返回 304,119 字符 | 第一次 7,833；归档后下一次 8,892；2 次模型入口、2 次预算检查 | 24k 单观察/96k 累计字符策略与 2,048 excerpt 有效；不等于正文可正式回读 |
| 同一工具增长跨 synthetic 较小容量 | 首次可入，下一次预算拒绝；仅 1 次模型入口 | 后续调用会重查，不会只查第一次 |
| 固定必要上下文已超 synthetic safe | 必要 7,833 > safe2,712；0 model call，RECOVERY_FIXED_CONTEXT_TOKEN_LIMIT | 不无限压缩 System/固定 seed |
| 压缩请求自身过大 | 真实 episode 先归档；source adapter 正规化后 283,921；2 段 map + selector | 现有 hierarchy 可处理超大 compactor wire |
| 无效 selector | 缓存已校验 map 后 selector 正好 2 次，CONTEXT_COMPACTION_ATTEMPTS_EXHAUSTED | 重试有限且不会重跑成功 maps |
| maintenance 已用满 400k | 0 次调用，COMPACTION_MAINTENANCE_BUDGET_EXHAUSTED | 累计维护资源不能重置后继续 |

首轮压力与正规化 request 的估算差别（279,843 / 283,921）来自真实 compaction source-message 封装，不是二者混用或厂商 usage。分段结果：map wires 分别 187,086 / 99,512，selector 11,972；都小于当时 core safe187,712。源串 816,029 字符分为 `[0,533835)` 和 `[533835,816029)`，无缝覆盖、重构 hash 校验通过；最终重组请求估计 7,898。共 3 次假 compactor，maintenance charged323,146。此例不需 merge，不能宣称已动态覆盖多层 merge；该分支代码有收敛/层数/调用约束。

源码 `context_compaction.py:1929–1977` 计量完整 compactor messages；`:2812` 起先计初始输入，普通 wire 自身不安全才分段 map→必要 merge→original-unit selector。要求已有 source_archive_ref，每段与 parent identity/hash/coverage 对齐；`validate_compaction_plan`、`assemble_compacted_epoch` 校验 identity、source manifest、tool-pair 分组、selected material 的 retrieval state，并在输出 epoch 再计量。维护额度最多 64 次、400k token reservation（本 policy）、300s；每操作最多两次。node 同时限制 overflow epochs=2，软失败只有仍 safe 才可有限回退，硬超限停止。业务 turn/tool counters 不因压缩刷新。

本次没有让完整 node CAS 激活压缩 epoch 后继续真实业务，也没有把 summary 质量当成由结构验证保证；上述成功只证明容量、覆盖、句柄和结构分支。实际 rc/validation/Q 语义不应依赖假 compactor 为本见证写的摘要，修改后要把关键因果事实保持 exact，并另做 semantic assertions。

## 8. 压缩输出预留不一致：有具体边界依据

`config/config.py:255` 和 `ContextBudgetPolicy` 默认 outputreserve8192。`bio_agents.py:2640` 的 base `llm_kwargs` 没有 max_tokens，`:18194–18207` 在 Recovery resource scope 的 context_compaction 阶段，若 client 没有整数 output cap，使用 **16000**，并 `bind(max_tokens=resource_output)`。该最终选择未回写 compactor admission policy。

上节真实 core 放行的第一段 wire 估计 187,086；加 16,000 的可能输出为 **203,086**，超过应用 200,000，未计其他 wrapper。用同样 margin4096 正确预留16000时 safe 应为179,904，而当前为187,712，差7,808。它不是本现场 seed 错误的原因：原现场压缩 API 调用为0。它是由现有代码参数和具体 admitted wire 共同支持的潜在后续边界缺口；未在真实 SDK/厂商验证输出达到上限。

有效模型是否存在厂商更小容量、客户端解析结果、tokenizer 偏差依然需要明确记录为 UNKNOWN。将 app limit 改大或把 32k 改成200k字符都不能修正此参数不一致。

## 9. S4 函数级修改计划（本轮不实施）

本切片复用现有 Recovery/context_compaction/inspection 机制。无新 global cache、全图 gate、并行 Recovery controller 或独立字符预算。与前轮 R1 rollback、A3 installation authority、runtime fact resolver、科学 validator 无生产修改重叠；仅回归保证这些权威状态不受 context 表示变化影响。与本轮 S5 的因果分类、S1 环境闭环、S2 消息/动作、S3 输入规格通过最终集成见证汇合。

| 函数/边界 | 最小变更方向 | 必须保留 / 验收 |
|---|---|---|
| `project_recovery_seed_for_provider` / `recovery_seed_capacity` | 移除作为 dispatch 前置拒绝条件的旧32k字符容量；freezer/身份校验保留，字符数降为遥测；完整请求由已有 token admission 决定 | 原32097 seed 能抵达完整预算，不截断 packet 或身份；固定必要上下文超限仍有限诊断退出 |
| `_context_budget_snapshot` / `call_model` | 复用唯一组件会计，对 first/next request 都记录 actual assembled inputs + tools + effective output cap；必要时显式加入已知provider wrapper | 不以 token_usage 或缓存减法代替估计；context不足不派发；保留输入/输出 cap来源与估计不确定性 |
| `_initial_inherited_materials` / `_normalize_inherited_materials` | 保留 immutable 原件、source ref/hash/coverage；Plan/TaskSpec 的 provider 表示只减少重复索引/已 exact 出现的相同内容，或通过现有 material residency/compactor 选择 | full PlanSpec 不改写，身份/权威不由摘要决定；应先测量，再决定是否需要缩减，不给本例凭空降到16k |
| registry 的 `provider_manifest` + seed 的 `_inspection_manifest` | 若需表示去重，使用显式版本的共享 scope envelope + 逐ref差异，权威 registry 与 locator保持原样；保留全部27身份、权限和 unavailable 状态 | 可以无损还原原manifest语义；错scope/version/digest仍拒绝；未发布产物不可正式消费 |
| `RecoveryObservationStore.externalize` 调用点 / `RecoveryLocalToolDispatcher` registry 接线 | 给每个新归档观察发布现有 scoped reader 可解析的受限句柄，绑定同task/plan/step/attempt/episode、content hash与范围；复用 observation store 与检查目录，不开放任意phase archive读取 | 返回ref在新进程可通过正式tool分页读回精确正文；跨episode/错误hash/任意路径拒绝；旧对象immutable |
| `_invoke_compactor` / `_ainvoke_llm` 输出策略边界 + `prepare_compaction` admission | dispatch前取得唯一effective output cap；用同一个cap推导business和compactor的safe输入、maintenance reservation并绑定最终client参数 | 8192/16000两种cap同源；无确定provider容量保持UNKNOWN；所有map/merge/selector各自检查完整wire |
| `_build_compaction_request` / `validate_compaction_plan` / `assemble_compacted_epoch` | 复用 existing archive、SourceManifest、MaterialRetrievalState、tool-protocol groups、hash/coverage和epoch CAS；只补上述回读句柄断点及明确hard facts需要 | rc0、validation拒绝、未Commit、Q correspondence未证实保持exact因果事实；summary不得升级approval或artifact权威 |

实现顺序建议：先让本例通过统一完整请求 admission 并记录 effective output policy；再修 observation scoped readback；最后做必要的 provider 表示去重。不要先重构整个 schema 或降低上下文到人为小配额。任何目录表示去重须有 schema/version 兼容，旧 durable refs 不迁移、不改 hash。

修改前保留本见证的原门失败和旧缺口证据；修改后同源测试应验证：①32097原seed完整首请求；②首次 inherited超限、单大tool、下一次越界；③compactor wire + final outputcap不能超effective capacity，分段/invalid retry/maintenance终止；④27项授权语义、8个正文在重启前后正式tool一致；⑤新observation正式回读可用并拒绝错scope；⑥old action/attempt/evidence、approval、binding、Commit状态未变化；⑦精确rc0/validationfalse/Qunknown跨压缩保留。均使用离线模型叶子，不以完整业务真实模型运行替代这些断言。

回滚应按上述有限提交逆序撤回 provider表示、scoped观察句柄接线、统一admission/output policy；归档与旧source objects都是追加记录，不删除历史object/receipt。停止修复可恢复旧dispatch行为，但原32k故障会再次出现，需在回滚记录中明确。所有正式反例都不得通过删掉权限检查、改旧packet/hash、跳过compactor validator或给无限预算“修绿”。

## 10. 复现与证据限制

运行入口是 [recovery_budget_reconstruction.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/witnesses/recovery_budget_reconstruction.py)。stage 用 `/usr/bin/python3 -I -S`；capture 和 restart 各用新隔离Python进程、`env -i`、临时HOME/TMPDIR、`BIOMIND_DISABLE_DOTENV=1`，复用已审查 offline bootstrap 的网络/子进程/真实项目读取与写入拦截。bootstrap提供配置替身，没有 import 真实config/startup或任何key文件。大工具与compactor仅返回离线合成值，测量/存储/reader/权限/压缩核心未替身。

[recovery_evidence_pack.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/witnesses/recovery_evidence_pack.py) 用 stdlib 将结果、完整假设请求、旧方法、来源hash与27行矩阵保全进本审计目录，并再次验证原plan_state与冻结source。源manifest与输入manifest不含密钥。运行假设只在隔离内存改变旧char门，不提供对生产迁移/部署的授权。

复现持久性补充：stage 已移除对旧 `/tmp/.../candidate` 的依赖，候选完全来自持久 `current_source`；唯一额外静态依赖 offline bootstrap 已保存至 `biomind_audit_exports/gwas_env_recovery_20260917/recovery_static_dependencies/`，按保存的 manifest 校验 hash。旧量测与方法优先取本审计 evidence，旧 `/tmp` 仅为缺少持久量测时的备用。将 PRIOR 指向不存在目录并禁止读取旧临时量测目录后，新 stage 仍成功；新候选中此前实际导入的 78 个模块逐个与原 capture hash 相同。此次只验证 staging，没有重跑 capture、模型或科学探测；现有 worker AST 未变。见 [stage 复验](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_stage_rebuild.json) 与 [静态依赖](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/recovery_static_dependencies.json)。summary 同时记录原 capture witness hash 和仅 bootstrap 改动后的 hash。

新复现使用 `recovery_budget_reconstruction.py stage` 返回的 sandbox 路径，再分别运行 `capture --sandbox PATH`、`restart --sandbox PATH`；打包入口接受 `recovery_evidence_pack.py --sandbox PATH`，也兼容原位置参数。不需要硬编码本次 `/tmp` 路径。输入仍使用本记录指明的只读原 JSON/immutable objects，不通过恢复 session 来生成。

实测局限：未调用真实provider或SDK wire，未验证DeepSeek实际容量/精确token数；未逐个读取9个科学artifact内容；未另行对5个Skill执行service read；未动态覆盖多层merge和完整node压缩CAS恢复。以上均保留为明确验收边界，不计入已通过项。原site故障中的环境原因、GWAS输入形状与rc0分类由本轮其他notes独立审计，本记录只保护这些因果事实不被上下文管理改写。
