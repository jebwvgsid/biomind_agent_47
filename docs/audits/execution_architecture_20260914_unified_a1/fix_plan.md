# 统一实施单（本轮仅设计）

本单依赖 README、interfaces.tsv、guards_and_exits.tsv、verification.json 的固定证据。不是生产修复授权。先在当前分支与保护 overlay 上按文件重核受影响字节；未变的 237 文件旧验收复用，不再次启动全节点审计。不得回退 S1—S3、Plan 资源关联/主动结束/取消、审批与输入保护。

## 依赖顺序

`F1 公开协议与示例 → F2 审批前 helper/动作物化` 与 `F3 格式候选/进展` 必须作为一个可交付组合；完成后才能宣称事故链修复。`F4 AskUser 回调异常出口`、`F5 Plan 正文接线`可平行实现，但最终共用 F6 完整组合验收。每片有独立提交，不把中间片称为完整 FIX PASS。

### F1：同一模式的动作协议、helper事实协议与恢复投影

问题：IG-D2、IG-D3。当前普通 native 要 raw JSON，helper 同时要求可执行块加说明；恢复 seed 又失去完整 native 例子。action JSON 和 stdout effect JSON 是两个不同接口。

边界：`prompts/manager.py:get_generate_v2_prompt`、`agent_core/step_effects.py:declared_effect_emission_contract/effect_template_provider_view`、`bio_agents.py` Generate request 装配（42245、42272、42299）、`agent_core/workflow/generate_handoff.py:start_generate_after_recovery` 及现有局部纠正消息装配。以对应作用域的现有协议选择为唯一来源，不新增全局合同/Controller。

实施：helper 规则只描述 helper 的输入和声明用途；顶层格式服从当前 Generate mode。普通、retry_step 后、局部纠正均投影同一完整的可解析 action 示例及停止示例。示例须来自当前正式构造，包含该步 operation、公开资源 handle 与完整 facts 分类，例如 `produces_observations → identity → value`。模型给观察值，框架落实身份；禁止测试或执行器补 action_type、伪造 True、把 planned state 当 observation。

兼容：保留当前明确支持的 native code/argv 与旧脚本入口，不接受任意 `action_json` 包装作为猜测性 fallback。若最终产品决定公开包装，必须同步 parser/示例/版本，不能靠模型猜。现有 publication=True 子集语义不变；workspace mutation、produced binding 继续走自己的 attester/Commit，不向 helper 偷加科学成功权。

验收：从三种真实 provider request 提取完整公开例子，原样进真实 parser 和本步准入。flat facts、缺 action_type、截断、混合多动作、未知 identity 必须精确拒收；合法 False、空列表、partial、blocked 不被变成成功。解析失败反馈必须属于动作层，不伪称执行证据错误。

### F2：所有支持动作载体在审批前完成同一 helper 物化

问题：IG-D1。合法 argv-only 的 marker 目前从未传给物化器。

边界：`bio_agents.py:_extract_structured_action_result/_structured_action_from_object_result（21653）`、native Generate 43889—43908 的物化与候选构造、`agent_core/step_effects.py:materialize_effect_template`、现有 action/script approval definition 构造及执行 wrapper。普通和恢复不分两套 helper。

实施：复用现有正式支持的动作载体解析，将可确定的脚本正文和 argv 执行定义在审批前一起物化。code 与 argv 同时存在时按既有约定校验一致性；不能只更新 code 而实际执行 argv 旧体。仅处理已经支持的明确 argv 语义；不可确定的动态命令载体给可纠正错误与合法替代写法，不做全代码语义扫描或任意 shell 解包。Python helper 必须进入实际 Python 解释器作用域；R 不能由 Python glue 冒充通过。

冻结与授权：记录批准正文、执行 wrapper、资源/template 清单各自 hash 及关联。它们本来不是同一对象，不要求所有 hash 相等；执行定义必须引用同一批准对象和冻结 helper。审批后改变 helper/代码/环境定义必须撤销旧资格并重新审批。新状态与旧批准脚本不静默迁移；旧已执行 attempt 只作为历史证据。

保留保护：路径授权、只读输入、expected_sha256、一次派发、scope/版本核验、真实 process receipt、timeout/cancel/unknown 区分。不得为修 helper 添加同名空函数，不改变原观察真假。科学依赖 registry 空值保持 UNKNOWN。

验收：native argv-only、code、Bash/Python 正例及可用时 R 代表；嵌套解释器作用域、标记缺失/重复、code/argv不一致、批准后修改负例。测试 shell 明确禁登录 profile，使用新合成进程；完整执行链保留实际批准体与 wrapper 回执。宿主 native `bash -lc` 的环境/profile 语义需记录，不能用隔离替身推出真实依赖已验证。

### F3：坏格式也能产生稳定拒收回执；有效纠正与真重复分开

问题：IG-D4、IG-D5，以及 downstream progress witness 的两层口径冲突。

边界：`bio_agents.py:_candidate_admission_rejection_update`（16101、16525—16533）、native parser rejection（43987）、`agent_core/plan_bindings.py:binding_input_progress_facts`、`agent_core/workflow/candidate_repair.py`、`agent_core/candidate_repair_policy.py`、`agent_core/recovery.py:4928` 的 generated-semantic 分支、`agent_core/managed_artifacts.py:managed_action_fingerprint` 的调用资格。

实施：非法候选使用现有 malformed/candidate 指纹路径，不把尚未通过 parser 的 env/list/null 交给只接受合法动作的 `dict(env)`。优先保存原 parser 的字段/类型错误；诊断或指纹失败不能遮住首因。候选记录保留业务原文摘要、parser选中片段、结构化候选、脚本与错误目标的来源，不输出私有 reasoning。

在原节点/原账本内统一“是否还能纠正”口径：表示层（能解码、外壳/action_type/字段类型）的有效改善不得被空 binding 增量覆盖；binding 纠正仍需真实 binding facts；脚本变化不自动证明科学进展。稳定比较去掉系统生成 UUID/临时包装，保留义务、权限、程序及可信事实版本；已见 A/B/A 与真重复要有界。不得仅按错误码变化续命，也不得仅因脚本相同停掉合法外壳修正。

预算兼容：Generate 4 是额外调用；Planner 4 含首轮，Reviewer 首次+2，保持各自单位/作用域。沿用现有持久 ledger、claim、receipt 幂等，不开新总预算、不增额、不把旧 stopped scope 自动复活。旧因果 attempt 和 consumed Recovery packet不得借给新候选当执行依据。若必须调整持久分类字段，读取旧记录仍按原 cap/已消费数，不撤销历史事实。

验收矩阵：JSON不可解析→可解析但待修外壳；缺action_type→补齐但env值待修；env父为list/null/字符串；相同脚本而外壳已合法；不同错误码但完全无新信息；A/B/A；只换UUID；精确事件重投；独立进程重载；最后一个合法机会可接纳；剩余额度主动blocked；取消后late-valid/invalid/blocked均不能获新审批资格。必须断言下一次实际请求与实际派发次数，不能只断言某错误码被换掉。

### F4：AskUser 回调抛错与语义拒收的出口一致性

问题：R01。缺答复回执的 `_semantic_harness_error` 带语义诊断，当前已经正确 END；不得修改该保护。相邻回调直接抛错经 `profile_typed_node → early_harness_error`，合法 harness_error 不带 semantic_state，当前路由默认投向 Consultant，`terminal_boundary` 因非 END 不收尾。两个错误生产者必须分别验证，不能只看路由函数推断缺回执路径失败。

边界：`agent_core/workflow/runtime_graph.py:route_ask_user_outcome`；只有必要时调整现有 `early_nodes.TypedAskUser` 的诊断投影。复用既有 harness_error → END → managed stop，不增加新终止权。

正反验收：真实装配的回调 OSError/其他普通异常应得到 `FRONT_HALF_NODE_HARNESS_ERROR → END` 且无下一 Consultant/provider dispatch；缺答复 `ASK_USER_ANSWER_RECEIPT_MISSING` 仍正确失败；`question_missing` 仍可回 Consultant；有真实匹配 question/answer-turn receipt 时按现有 semantic_state 继续；跨question、陈旧答复、EOF/取消不得成为批准。不要把普通科学未决当内部错误。

### F5：Plan 默认显示同一冻结审批正文

问题：E07/entry 默认正文见证。当前 compact 只显示摘要和 F2。

边界：`bio_agents.py` PlanReview 40888 附近，`agent_core/cli_review.py:ReviewDisplay`、`agent_core/cli_plain.py:render_review`、`agent_core/cli_terminal.py:prepare_review`。保持原 Plan 编译、冻结、审批 receipt/CAS 与输入 owner。

实施：给既有显示对象明确 Plan 类型，将其完整 `body` 默认呈现为可滚动正文；正文内容来自同一 frozen review 对象，包含输入/输出/步骤/依赖/范围/参数/未决/停止条件，不另调模型写摘要。正文完成显示后才开放该对象确认；F2/详情保留，不能换一份重新生成的 Plan。

验收须真实 PTY 加 plain 降级：compact 默认正文、F2同源、长正文/中文/空格、resize、反馈与拒绝、明确确认、换行/paste/queued input、EOF/取消、scope在等待中更新。只准当前 owner/current submission 生效；保留当前明确输入行为（含正式等待窗口内显式回车的既有语义），EOF、空白粘贴、历史排队换行与查看详情不能被当作该确认。旧 PF/LIFE/INPUT/CLI 断言原样保留。静态 UI 字符串或本轮 witness 不算 UI FIX PASS。

### F6：统一验收与验收盲区关闭

首要组合是实际 snpEff 六种操作的机制链：check contigs → prepare mutable layout/bindings → build ready state → annotate read-only binding → summary observation/artifact → final deliverables。使用固定 provider 和显式科学替身；从用户事实、正式 handoff/Plan/审批开始，真实 parser、Generate、脚本审批、执行、effect parser、attester、Reviewer parser、CompletionGate、Commit、持久化、最终报告都参与。不得直接塞 done/ready/approved/succeeded。

本轮六个 inspect 的完整机制正例与单步 prepare 正例可作为各接口基线，不能拼成已经通过上述六科学操作。新增 combined case 必须验证同一 mutable identity 的两个真实状态推进、annotation 的 required_state、下游一次激活、全部 required 成果与报告归属。

并行负链必须包含：真实 rc失败→Recovery retry_step→表示修正→新审批→实际成功；partial/False/blocked 留可信事实而不科学Commit；Reviewer一次纠正成功、持续坏格式、合法科学否决（科学进程不重跑）；重评/重载不刷新同源额度；重复Commit/report；lost-result不盲重跑；取消前/await中/保存前及late result；报告冲突/部分交付/receipt恢复。

hook 组合要求：用真实构造器及当前 schema2 装配，固定外部服务、新存储，启用实际支持的 archive/summary/context 回调。覆盖正常、None、异常、超时/取消，证明可选说明不改已冻结科学事实，也不重启业务。legacy compression 和 schema2 PhaseContextCompactor 分列，禁止拿前者 PASS替代后者。保留本轮 watchdog/selector事件、fixture错误与之前9个旧fixture失败；不改旧断言或skip把全仓涂绿。

最终验收门槛：F1—F5所有合法正例与必要反例通过，F6正常/恢复/受阻/主动结束/取消/重载/报告组合通过；发布“FIX PASS”必须附各新进程加载manifest、执行与Commit/report receipts、无假前置说明。组件通过不能代替图通过，图通过不能代替真实模型或科学通过。

## 后续真实验证的独立授权

| 层 | 当前权限 | 输入/命令/输出与限额 |
|---|---|---|
| 1 确定性公共接口 | 本轮可执行 | 本目录新测试，已知Python，export_and_run新/tmp根，真实provider/science=0；本轮已执行结果见verification |
| 2 完整框架机制 | 本轮可执行；生产修改需另授权 | 新合成输入、固定provider、tiny进程；F6组合每场景独立watchdog，不以外部watchdog作生产停止；不复用原session |
| 3 真实模型隔离任务 | NOT_AUTHORIZED/NOT_RUN | 另批provider/model及凭据注入方式；新task/root，小合成输入；建议最多1个六步正常+1个恢复任务，预先冻结各节点现有调用额度和总费用上限；逐Plan/动作正式确认，无科学外部工具 |
| 4 指定科学验证 | NOT_AUTHORIZED/NOT_RUN | 另批工具/环境prefix/输入路径/输出新根/资源上限及原任务是否重开；先指定小科学任务，再正式任务；保留Part2.vairant_annotation原字面；每步正式审批，禁覆盖旧结果 |

层3/4缺少具体provider、费用/资源上限、正式能力回执与新任务输出根，必须在其执行授权时填定，不能让本审计替用户作预算决定。本轮提交不自动接续它们，也不取消其他已授权并行任务。
