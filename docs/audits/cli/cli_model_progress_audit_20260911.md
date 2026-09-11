# BioMind CLI：模型进度说明与真实工具活动流定点审计

日期：2026-09-11。AUDIT STATUS：**审计完成；目标能力尚未通过验收；生产实现未修改**。

## 1. 结论与证据版本

当前问题同时发生在**生成协议、传输分流、回合出口和终端展示**，不能用更拟人的固定文案解决，也不能概括为“所有模型都没有说明”。

- Consultant、Planner 的主要业务输出是严格 JSON，Generate 是严格 action JSON / 单一脚本块，正式 prompt 禁止块外解释；没有可直接放行的通用公开进度字段。当前上下文工具回合存在，但不等于允许自然语言与业务载荷混写。
- 共享 `VibeThinkingHandler.on_llm_new_token` 对正文 A、B 都只发同一固定活动文字，不转发正文。旧审计中“它展示 reasoning”的描述已不适用于当前源码。Provider observer 只做计数、hash、时间和生命周期；`streaming=True` 与 graph `astream()` 均不能证明公开说明实时显示。
- **Recovery 已有公开说明能力**：native assistant.content、envelope explanation 或 directive.summary 在完整模型回合返回后，经 `recovery_response_received` → 生产 event sink → OutputRouter **发出公开显示事件**，随后进入本地工具回合；实际终端 write/flush 仍可能晚于派发（见第9节）。这是完整回合说明，不是逐 token 显示。该通道应优先复用；现有 raw/display_text 选择和空回合复用旧说明仍有缺陷。
- Execute 已有真实进程启动和退出活动、stdout/stderr 显示副本，`rc=0` 文案正确表示“等待正式核验”。Consultant/Planner 共用 Skill 读取/搜索边界缺独立活动事件，部分 Searching 行在实际调度前产生，不能计成已经运行。
- Renderer 使用全局单个活动槽，缺乏 call/attempt 隔离；普通正文还存在前 80 字去重碰撞。TTY 历史 flush 有 0.1 秒批处理；无 owner 的初始 plain 模式会丢掉 emit-only 活动。需要统一有序显示副本，不能仅新增一个 callback。

仓库：`/public/home/liusiqi/03.毕设/Biomind_v1`。分支：`refactor/consultant-controller-authority`。
BASE HEAD / 审计前 HEAD：`e5d814bd77b44f52aa16fc1fff92982a9aa9d729`。
证据描述的是**该 HEAD 加审计开始时既有 dirty 工作树**，不是一个干净 HEAD 验证声明。初始：36 M、33 D、368 untracked，共 437 项，staged=0。未自动切换分支。

审计期间初次与提交前的 115 份安全源码 hash 对照无变化；导出测试使用当时工作树文件。既有相交修改包括 `prompts/manager.py`、`agent_core/node_context.py`、`agent_core/planner_context.py`、`agent_core/provider_message_safety.py`、`agent_core/workflow/managed_stop.py` 和 config 包；这些修改不归本轮所有，未暂存或提交。完整初始清单另存隔离目录 `baseline.json`；本报告末附范围摘要与源码签名。

## 2. 当前源码主入口与可达性

已确认的入口：`bio_agents.py:54159 main` → `agent_core/cli_terminal.py:298 run_terminal` → `bio_agents.py:54132 _runtime_main` → `interactive_mode:53608` → `_run_agent_interrupt_loop:53358` → `go:53122` → `_app_for_task:52608` → `active_app.astream:53270`。

`run_terminal:300` 在 BIOMIND_UI=plain / TERM=dumb / 非 TTY 时直接 await runtime；正常 TTY 建立 Terminal，原业务协程在一个工作线程内运行。未读取任何实际环境变量值来推断本机用户模式；这些条件来自源码。`_runtime_main:54135` 对非 TTY interactive 会返回，因而不能声称管道运行一定进入业务。

新任务 schema2 由 `workflow/outcomes.py:557` 的 `brand_new` 绑定；缺历史版本字段则维持 schema1（`:523-554`）。生产 `schema2_app` 由 `bio_agents.py:3309` 和 `workflow/runtime_graph.py:355` 构建，14 个节点包含 triage/direct_response/consultant/planner/plan_review/ask_user/install_subagent/generate/step_worker/execute/supervisor/commit_step/recovery/final_audit。legacy 图仍由 `_build_graph:31311` 生成，不能把它的所有分支当成新任务必经路径。

**当前用户宿主实际使用哪个启动命令、UI模式、sticky schema、原 session 中哪段文字：UNKNOWN。** 按授权未读取或执行启动脚本、旧 session/state loader、进程环境或原数据库。本审计确认源码生产接线与条件可达性；没有用文件存在或单测引用冒充用户那次运行证据。

## 3. Q1/Q3：文字来源与失去实时性的环节

逐项来源见同目录 `cli_output_lineage_20260911.tsv`。其中生产时刻一律 UNKNOWN；emitted/displayed 精确时间只用于合成离线案例。

| 环节 | 当前行为 | 对目标的影响 |
|---|---|---|
| Provider / SDK | `deepseek_stream.py:36` 委托父解码并保留非标准 reasoning；不提供专用公开字段 | reasoning 不是公开进度，不可据其 token 数推断公开说明 |
| 共享 token callback | `bio_agents.py:2470-2478` 仅 emit 固定 activity；未解包 ChatGenerationChunk.message | 正文被抑制；wrapped reasoning-only 连此占位也可能不触发 |
| Provider observer | `provider_attempt.py:388,438,480,571` 记录元数据；`_emit:1004` 仅投影生命周期 | 不是正文运输通道；现有 call/attempt 身份可复用 |
| 业务调用 | `_ainvoke_llm:18183-18217` await 完整 BaseMessage | 大部分解析和最终 print 在完整请求后发生，不是节点内部 token 输出 |
| 现有回合 | Consultant/Planner/Generate 按协议处理完整 response；Recovery :1224 已 emit explanation | 各节点能力不同，Recovery可先接好现有出口 |
| graph stream | `go:53270-53271` 只调用 profiler heartbeat | 未把 graph 更新转换为公开文字；不能等同 provider stream |
| OutputRouter | `print_ai:2414` 丢执行标签、去 machine blocks；`page_text:2314` TTY将JSON收录details | 原始正文整体放行会泄露协议；plain对JSON无同等隐藏；非公开诊断 AIMessage 也可能显示“AI Message” |
| 终端 | `cli_display.py:65` 单槽活动/尾部，历史截断8行/1500字符；`cli_terminal.py:134-149` 等0.1秒再write+flush | 接收在工具前不等于实际写出在工具前；并发/迟到事件缺身份仲裁 |
| 日志 | 当前 dirty `config/logger.py:52` → `cli_logging.configure_logging:48`，自有console仅WARNING；INFO/DEBUG按文件级别 | 诊断日志不等于用户段落；外部handler被保留，整进程是否重复输出 UNKNOWN |

`print_ai` 的 AIMessage 类型不证明作者是模型：例如 `_generate_provider_failure_response:15534` 是系统拼接的错误 AIMessage，`:42913` 仍经同一输出入口显示。`print_observation:2441` 内容是后端 observation，不能标为模型诊断。

确认的两处事实文案错误：`provider_attempt._emit:1022` 把首 payload 之前可能发生的 `attempt_progress` 也称“已观测到 provider 活动”（周期触发 `:1307-1321`；Planner启用 `bio_agents.py:18212`）。`:1026` 取 `failure_code`，而记录实际字段是 `failure_type`，使具体失败提示退回泛化文字。应修正事实投影，不需要让模型解释等待。

Generate局部拒绝/修正主要经 `_candidate_admission_rejection_update:15967` 的durable receipt及下轮validator_feedback传递，未在该helper找到独立公开修正段落；其 `Generate lifecycle:45705` 是日志。确定的终止显示例外为 `:45714` 的系统文字“Generate stopped before execution: …”。Planner拒绝显示则由 `:38975` → `_planner_progress:17711` 发出“候选拒绝，…项问题；完整反馈已持久化”。这些都不能当作模型的修正说明。

没有发现当前公开正文在节点结束后通过 sleep 逐字播放的路径；真正已定位的延迟是 provider 完整返回门槛和 renderer 批量 flush。未从旧普通日志倒推延迟。

## 4. Q2/Q4/Q9：逐节点输出协议与错误上下文


| 节点 | 生产接线 / 本轮源码 | 输出/回合形态 | 现有模型文字/最小接入点 |
|---|---|---|---|
| Consultant V2 | `bio_agents.py:3227` `build_scoped_phase_node(consultant)` → domain `consultant:31549`；`:34058` 调 `get_consultant_v2_prompt`；`:34359` `_ainvoke_node_llm` | 同一局部 phase 可连续证据回合（`consultation_continues`，最大 64 local turns）；业务动作在 content JSON 中，不是 native tool-call。`prompts/manager.py:2131-2146` 明确 JSON/no prose | 现有 `task_hypothesis`、query `purpose`、`reason`、handoff `summary`、`question`、`answer` 是模型字段，但语义不同且不是每轮都有进度。`:34637` 取原 JSON，`:34712` 归一 legacy command；`:34715` 仅显示固定 “action parsed; validating”。最小可先只展示已有明确用户字段（按原意），需要通用进度才扩展严格显示 envelope；在完整响应解码后、实际工具派发前发独立显示事件 |
| Consultant escalation / legacy finalize | `bio_agents.py:34051` discovery escalation，`prompts/manager.py:2314`；finalize/repair 分支 `bio_agents.py:32429/34359`，`prompts/manager.py:850/871/1101` | escalation 仍 JSON/no prose；legacy finalize repair 有专用严格 schema。兼容 `_consultant_command_from_json:21912` / `_consultant_json_payload:22059` 能从 fence/XML 提取，但不代表允许松散公开通道 | 保留既有严格业务行为；不要为显示恢复旧 TaskSpec/semantic binder 或扩大 legacy 协议；`summary` 是终态/交接内容，不是自动可流式 narration |
| Planner V2 | `bio_agents.py:3262` phase → `planner:37527` → `:38488` V2 system → `:38778` provider | `planning_continues` 局部证据/候选纠正回合（最大32 local turns）；`:38833` 记录完整 assistant，`:38836` 取 content，`:38858` `parse_planner_outcome_envelope(...allow_legacy_plan=False)`。`prompts/manager.py:1772` raw JSON/no prose | 普通步骤 title、reconciliation instruction、reason 等是模型业务字段；当前无通用公开消息字段。`agent_core/plan_contract.py:598/642` 只接 whole JSON 或完整 whole-response JSON fence；prose 混入会失败。最小分离层必须在 Plan candidate body/hashes 前提取可选显示字段，保持原 candidate 字节/结构意义，不能只是删 prompt 的 no prose |
| Generate V2（脚本） | `bio_agents.py:45507` wrapper → `_generate_impl:40769`；`:1350` `assemble_generate_v2_messages` → `prompts/manager.py:2348`；`:42706` provider | `prompts/manager.py:2564-2595` 单一 shell/python/r block 或 typed Skill JSON 或严格 blocked JSON；脚本外不得有 prose。可 Skill evidence continuation、candidate correction、human review feedback；不是每次都有工具调用 | `blocked.reason` 是公开可解释字段；执行正文无独立说明。`:43252` 仅在完整响应后 `print_ai`，`:43285` 再 strict script parser。`agent_core/controller_audit.py:78` fullmatch 拒绝块外解释。最小扩展须是明确分离的响应 envelope/已验证 transport content item，提取后把原脚本交现有 parser，不能把 UI 文本夹进脚本或改获准代码 |
| Generate V2（semantic action / Skill decision） | 同上；`prompts/manager.py:2702`；`:43191` Skill parser 先于 `:43252` print | 单个 raw JSON；Skill decision 早 return 到 `_handle_skill_node_decision:43210`，没有该处最终 print | 可选显示字段需要在共享严格响应边界提取，正常业务 action/parser/approval继续原样；不能假设 `_extract_structured_action_result:21798` 的旧 XML/fence兼容即公开流接口 |
| Recovery V2 | `bio_agents.py:2872` 构造 `Schema2RecoveryNode` 并注入 event_sink；`recovery_node.py:1775` 构造/调用 `RecoveryAgentLoop`；`recovery_agent.py:816` bind_tools | 真正 native assistant+ToolMessage 图：`:1548-1550` model/tools/finish；`:115` 明确 user-facing diagnosis 并允许 assistant content + exactly one submit tool；inspection 回合可 content+tool_calls；完整响应后 admission，不等整个 phase 完成 | 优先复用 `assistant.content` / `envelope.explanation` / `directive.summary` 和已有 `recovery_response_received:1224`；不新增模型；修复 display_text投影选择、router分类及 flush；原始 content 保持原有审计和上下文，不重复把 UI transcript 回灌 |
| Supervisor V2 / final audit | `bio_agents.py:2867` TypedSupervisor 包装 `supervisor_node.run`，`:2901` TypedFinalAudit 包装 final_audit；`supervisor.py:1249` ainvoke_with_snapshot | `supervisor.py:75-97/151` one-shot semantic reviewer、JSON/no prose；`:1306` parser；不进行 native工具 loop。确定性验证可先直接返回，是否实际调用 LLM取决于证据/策略 | `issue_dispositions[].explanation`、`concerns`、`suggestions` 可复用为“模型评估”完成消息，不应冒充执行前意图。`:644` parser能从fence/braces提取 JSON，外围 prose 被舍弃。`:1313` invalid ignored、无相同重渲染模型调用。无需要求每次验证加 progress LLM |
| Triage / fast router | `bio_agents.py:31317`，`agent_core/router.py:281` classify_task_mode → `:292` ainvoke_with_snapshot | JSON only，不回答用户（`:255`）；短 one-shot，无 tool loop；reason属于分类依据 | 不为形式完整添加 narration调用；已有真实等待/路由事件即可 |
| Direct response | schema2 mapping `bio_agents.py:3306`；domain direct_chat `:31455`，`:31521` provider | 允许自然语言答案，无执行权；读取检索上下文发生在模型调用前，非工具回合；`:31539` 完整响应后 print_ai | 最直接可复用完整用户答案显示；已有 callback不输出正文，需要可识别为普通 public answer 的显示分流；不要套用 script/Plan正文放行规则 |
| Installer（条件路由） | `bio_agents.py:3007` TypedInstallSubagent；domain `:52206` installer_app.astream；`agent_core/installer.py:509` ainvoke_with_snapshot，`:546` inst_execute | prompt `prompts/templates.py:583`；每个 action须代码块(:594)，执行用现存 XML/code fence (:557)，结果回消息再 generate；该 prompt 不等同 V2 Generate JSON-only | 兼容正文可能有说明，但没有明确可靠独立 public槽；必须先识别并保留代码/注册协议，不能全量 stream到公开区。只在实际 runtime install 可达时覆盖；本轮不运行 |
| StepWorker（显式 opt-in） | `bio_agents.py:52334` invoke_step_worker；`:52368` 从 backend last_events/last_transcript 归档；`:3307` graph注册 | backend 契约，是否真实 active由非敏感既有配置/父审计确认；此处只接受 typed outcome | 不推断后端已具备 commentary；任意额外后端通道 UNKNOWN，不建议为此扩大本轮最小修补 |
| Execute / AskUser / PlanReview / CommitStep | typed wrappers及graph注册；非通用模型输出协议 | 分别执行事实、人工等待、批准计划、提交校验后的业务结果 | 显示系统事实即可；不需要新增模型。被称为“工具”的普通内部函数无需全部追踪 |
| 终止总结 / compactor | `managed_stop.py:680` 已有 terminal_summary调用（dirty），`bio_agents.py:3320` compactor adapter | 终止后一次已有有预算summary调用；compactor输出是模型工作上下文而非公开进度 | 终止使用现有已授权路径；模型不可用时确定性事实fallback，不能新增旁白来补等待；compactor不可原样当公开说明 |


### 错误事实进入下一次已有模型请求


| 事实类型 | 生产依据 | 可以确认 / 边界 |
|---|---|---|
| Consultant resource/read/格式失败 | `bio_agents.py:35832-35849` load_error与原assistant作为messages返回；`:34646-34689` contract-repair错误包含errors/预算后返回 | phase `record_assistant_response:636` 保存原AI；`_normalize_turn_messages:703`只保留一次原AI并把System降为Human runtime_observation（`:238`）；`prepare_messages:492`下一请求含suffix；保留错误语义，非只一个retrying |
| Planner parse / length / validation | `bio_agents.py:38837-38875`response_integrity包含finish_reason/parser diagnostic；`:38961` save_correction_context；`planner_context.py:66-93`feedback含validation_issues、requirements、correction_budget、response_completion、length_recovery；`:115`保存messages+rawAI+feedback；`bio_agents.py:38437-38444`恢复 | 具体错误能够进入本来会发生的correct_previous_plan调用；dirty feedback projection`:29-62`仅丢控制元数据、保留诊断值。length不足导致停止时不承诺一定下一轮 |
| Generate syntax/schema/action rejection | `bio_agents.py:45313-45384` protocol_violation含code/message/field_path/observed/expected/stage，进入 candidate_admission_rejection_update；`:41600` action_repair_projection→generate_feedback；`:41963-41998` append validator_feedback；`:42057-42060`冻结下轮wire | 既有repair receipt→当前scope episode；应能准确看到引用错误（全链业务load未运行）；若invalid receipt直接fail closed（`:41572-41598`），没有下轮可解释 |
| Generate approval feedback | `bio_agents.py:41945-41961` 受scope保护的 code_review_feedback decision/guidance/command_started=false event进入同一episode | 人工拒绝不是已执行失败，不新增模型调用来播报；下次本来继续generate时能看见 |
| Generate Skill结果 | `bio_agents.py:42023-42049` skill_tool_result进入episode再freeze | 沿原有业务context进入，显示时不要复制回灌一次 |
| Recovery native工具失败 | `recovery_agent.py:1356-1375`异常生成error/code/detail；`:1449`建立配对ToolMessage；`:1521`返回messages；`:1542`after_tools继续model | 错误事实和合法tool_call_id保留，下次现有模型调用可解释；达到tool/no_progress limit时结束，不保证额外回合 |
| Recovery格式失败 | `recovery_agent.py:1228-1255` 一次format correction，feedback含error_code/stage/detail/remaining budget；native calls全部配ToolMessage，其余HumanMessage | 不是把失败UI吞掉；display errors不能误走此格式预算。`RECOVERY_RESPONSE_INCOMPLETE`不做格式重试 |
| Provider retry/timeout | `bio_agents.py:17781-18400` transport attempt loop固定messages，partial payload不形成有效候选；Consultant`:34416`一次受限重试/ask_user；Planner length有上述独立反馈 | 物理重试本身通常不会给模型追加“网络失败原因”，也不应假称模型重新诊断网络；系统直报已知transport事实。恢复后业务是否再调用取决于原协议 |
| Supervisor invalid advisory / budgets结束 | `supervisor.py:1313`无效advisory变UNKNOWN ignored；`managed_stop.py:680`终态summary已有路径 | 无效advisory不额外修同一JSON；预算耗尽后不能为了说明新增回合；已有summary也可能跳过，系统事实保底 |


当前 dirty `agent_core/provider_message_safety.py:10-16` 已退役为no-op；它不是本轮发现的输出过滤源。NodeContext投影约束provider输入，不是assistant输出过滤器。


## 5. Q5：真实工具活动与审批/验证的区别

| 阶段 | 可复用实际入口 | 当前显示 / 最小接点 |
|---|---|---|
| 模型提出读取/搜索/执行 | Consultant/Planner/Generate 完整 response 的 action dispatch | 仅 proposed；不能把模型一句“已读取”当回执 |
| Skill读取/搜索 | `_handle_skill_node_decision:7764` → `serve_context_request:7885` / `search_skills:7928` | 共用服务前后缺独立显示；在此接最小只读观察器 |
| inspect/glob | Consultant `:34800` / `:34840` | 原结果回后续模型；缺独立开始/返回行 |
| KB SEARCH | `:35510`显示Searching，`:35514`才检查retriever、`:35515`调用 | 已有文字只能算请求；实际启动/不可用从对应分支取事实 |
| Recovery本地工具 | `recovery_agent.py:1317` started → `:1335 dispatch_full` → `:1363` failed / `:1477` completed / `:1343` cancelled | 已有事件和tool_request_id；completed仅代表调用返回，不代表诊断成功/修复验证 |
| 候选保存 | `_persist_bound_action_authority:12257` → `_persist_script_version:12320` | 系统保存模型候选，不是模型编辑生产源码；可发保存回执 |
| 语法校验 | Generate `:43602` / Execute `:47638` → `check_script_body` | 失败在审批/launch前，command_started=False；沿现有local repair事实播报 |
| 审批 | `:47665 middleware.review_code`；middleware.py:1186；`:47677 reject` / `:47722 quit` | 保留自动批准与人工批准条件；显示pending、declined、cancelled来自真实结果 |
| 启动 | `:48074` prelaunch核权 → `:48815 run_script_path` → executor.py:468 subprocess成功 → :486 RUNNING | 只有RUNNING足以称进程已启动；Script路径行和STARTING记录不是启动证据 |
| 返回与stdout | executor.py:199并发排空stdout/stderr；:637 rc文案 | 只报告观察到的单个脚本进程；不解析脚本/stdout来宣称内部命令均已完成 |
| 业务核验 | `:49114 _validate_execution_result` → `:49350 print_observation`；typed verifier.py:127 | rc=0仍可能缺正式observation/产物，不混成成功活动 |
| 提交 | commit_step.py:341验证ValidationEvidence、:508 advance、:582 CAS | 保留唯一正式提交权威；plan_complete还要final audit |

不为展示修改 approved script 的字节、参数、路径或插入 echo。`run_script_path:788-797` 仍核对 exact SHA256。任意长 shell 在只观测一个进程时，只显示这个进程的生命周期。

## 6. Q6/Q8：显示面、身份与异常边界

1. **控制权隔离**：`cli_display.emit:31` 是无控制权显示副本出口。Plan、action、script、approval hash、FailurePacket、正式 observation、typed outcomes 和 CommitStep 继续原流程。`plan_tracker.record_provenance_event:2088` 只接受科学执行生命周期，未知事件 `:2874` 抛错；禁止给它新增 commentary/progress UI事件。
2. **显示异常隔离**：emit捕获普通 Exception，不捕获取消/KeyboardInterrupt。Recovery event sink `bio_agents.py:3405` 先做必须的durable checkpoint，`:3446`单独包住UI；不能把持久化失败也包成可忽略UI错误。整体业务坏响应继续原格式纠正路径，不能为了显示非阻断绕过解析。
3. **身份缺失**：provider已有 task/node/call/semantic_invocation/provider_attempt；Recovery已有 seed/epoch/tool_request_id；execution已有action/attempt/pid。UI却多只收到裸文本；provider attempt 与 execution attempt 必须保留独立字段，不让模型填写身份，不建另一套任务状态。
4. **去重与并发**：`print_ai:2436`用前80字符去重，可丢两条不同说明；print_observation用前60字符同样不具调用身份；DisplayModel即使传了task/attempt也只识别可选key，未按scope隔离。单个tool_tail会被stdout/stderr、两个工具的最后写入覆盖。现有主图多为串行，不据此断言用户线上已经串单。
5. **迟到片段**：ProviderStreamObserver在fence后拒绝旧payload；共享Vibe没有同样fence，离线已复现迟到占位。活动可覆盖approval/cancel的active栏，历史仍保留。需要丢弃旧scope更新，并在同一显示序列内保证说明、关键错误、审批边界不被聚合跨越。
6. **plain与降级须区分**：初始plain没有owner，emit-only tool/activity/tail返回False而调用者未print；只有动态界面出错后仍保留owner的 `fallback=True` 会追加stage/tool_start/tool_end等。redirected输出应最小统一为追加式安全文本；不声称当前已支持完整活动日志。
7. **错误和取消**：真实执行取消继续原进程终止、回收与上层传播；task cancelled不证明所有进程清理完成。executor :700/:746 对部分lifecycle callback有原有Exception抑制，生产映射对PROCESS_CANCELLED与PROCESS_EXITED处理不同；这是播报不能凭空补造durable回执的边界，不在本轮修复业务状态机。pipe关闭在无owner display_print可能抛BrokenPipeError，不能笼统宣称全路径UI失败都被隔离。
8. **安全展示**：现有safe_text去ANSI/OSC/C0/C1，StreamDisplayCopy隐藏machine-marker行；离线合成凭据仍会原样留下，因此它不是密钥脱敏器。新增内容只能走有界允许字段的显示副本，禁止公开原reasoning/敏感请求头/凭据。不能把诊断details整包当安全公开内容。

## 7. 最小实施建议（本轮未实施）

优先级P0：先复用Recovery已有公开说明和真实工具事件，修正本回合display投影、事件身份和终端顺序；并接共用Skill工具边界。Generate、Planner 的协议扩展分开处理，不要求快速纯路由节点为形式完整增加调用。

| 文件/函数 | 最小改动建议 | 无需修改的权威 |
|---|---|---|
| `workflow/recovery_agent.py` model reception、`bio_agents._schema2_recovery_event_sink` | 按本回合source_message_id选说明，使用已有display_text；空说明不复用旧值；转发已有event scope | 原assistant/tool历史、directive解析/admission、Recovery上下文/预算 |
| `bio_agents._handle_skill_node_decision`、consultant inspect/glob/search、候选保存/校验边界 | 接真实调用前后及失败/拒绝的只读显示；成功用已有返回状态，不由文案推断 | Skill授权/绑定、候选hash、审批流程 |
| `bio_agents.VibeThinkingHandler`、`_ainvoke_llm`；`provider_attempt._emit` | callback携带既有call/attempt与fence；仅接协议已允许的公开文字；修正无payload等待和failure_type投影 | provider watchdog、cleanup、SDK重试和私有reasoning归档 |
| `cli_display.emit/DisplayModel/StreamDisplayCopy`、`cli_terminal.receive/_flush_history`、`OutputRouter.print_ai/print_once` | 单一有序输出副本；按事件身份去重；保留说明与工具边界；plain追加；防旧活动覆盖审批/取消；有限允许字段和控制字符处理 | 不接PlanTracker、不写业务状态、不解释route/成功 |
| `execution.executor._notify_lifecycle/run_command` | 在现有launch/return/cancel显示副本中传runtime身份，重复回执不重复计数 | 脚本/argv/hash/权限、正式capture/log/parser、原异常传播 |
| `prompts/manager.py`及各节点相应parser/envelope | 仅在确认无可复用字段的结构化节点，明确新增可选公开字段与正式载荷分离方式 | 获准Planner Plan唯一执行定义；不重建Consultant TaskSpec/Controller路由 |

**可选字段不等于自动流式。** 推荐先选择“完整响应解析成功后、下一次现有工具派发前的公开说明”，沿各节点现存协议做具名可选字段/外层envelope，不使用松散XML或正则拼接。Consultant/Planner：只在完整JSON可解析后取显示字段；显式投影原action/Plan载荷，再走原validator/hash/approval，不能让新字段进入批准对象或新权威上下文。Generate：script-only当前不允许外侧文字；需要明确支持的独立response envelope或现有结构化action的显示字段，提取后保留原脚本**精确字节**及全部语法/权限验证。字段缺失/空/非字符串可以降级不显示；整个JSON或脚本响应坏掉仍走原修正。

若进一步要求单个provider请求内逐token显示：只有可独立识别的公开content通道才能增量转发；native tool-argument增量绝不当说明。没有这种边界的节点先报告能力限制，不能先放行原JSON片段再事后过滤。对已显示后被截断的独立公开片段标记该attempt中断/未完成，不将其当结论；未闭合结构化显示字段不展示。回收/重试要撤销旧流接受资格，已写出的历史不能伪装来自新attempt。

**顺序验收分两层**：事件发出顺序可由现有agent-loop保证，真实屏幕写入还受renderer批处理影响。保留一个不参与业务决策的有界显示队列，完整说明与随后工具事件按同一序列写出，禁止活动先覆盖掉尚未写出的说明。若产品还要求“工具派发之前物理flush”，需单独评估有界flush握手的等待成本和失败降级；当前源码不保证，也不能承诺UI开关具有完全相同时延。

兼容策略：旧response无显示字段仍合法；旧schema1/新schema2各自协议保留；选择feature能力与纯renderer开关分离。回归重点是action/script/Plan字节和语义、approval binding、缺字段/坏响应、provider截断、迟到/并发、pipe、真实取消与业务错误，不做播报语义validator。

## 8. Q7：调用数、token和交互成本

目标新增专门旁白LLM调用 **0**；Recovery现有通道接线、工具活动与renderer修复预计调用数变化0。Consultant/Planner/Generate若在现有业务响应中追加说明，prompt指令和输出说明会增加token，且可能影响结构化响应质量、输出长度/重试概率；无真实provider测试，确切时延/成本/表达质量UNKNOWN，不能称零成本。

不增加report_progress工具诱发额外模型回合，不加定时旁白请求，不对无新模型消息的等待编造第一人称进展。provider transport retry复用同一messages，没有因UI重试行自动给模型注入失败事实；此时由系统说明已知错误和重试状态。工具结果与模型assistant/tool消息只保留原协议一次，UI transcript不再回灌Seed/history/handoff/Plan。

纯UI开/关在固定fixture下应保持请求数、工具动作、路由、审批、重试额度和CommitStep相同。此次组件和原隔离CLI测试覆盖有限边界，**没有重跑完整生产图来宣称全流程等价**。正式能力扩展的prompt变化必须另做验收，不能混进UI开关比较。

## 9. 隔离离线检查与时序证据

隔离目录：`/tmp/biomind_cli_audit_20260911/replay`。显式白名单导出当前安全源码与原CLI测试；未修改既有测试。使用仓库现有export脚本、guard禁止网络/原工作区/启动与.env读取；只import隔离CLI/执行捕获模块，AST原样抽取OutputRouter/Vibe/recovery sink等，注入fake provider、消息/lineage/tool stub。真实Terminal运行在prompt_toolkit Application内，用Capture(DummyOutput)观察write/flush；不启动原_runtime_main/BioAnalystAgent，不加载业务session。现有CLI双管道测试仅启动无害Python小进程，验证raw bytes与显示副本分离。没有科学任务。

使用已有Python `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`；prompt_toolkit来自已有离线缓存 `/tmp/biomind-cli-deps`，未安装/下载依赖。系统 `/usr/bin/python` 缺pytest/prompt_toolkit，已改用上述现有解释器和缓存，不是审计阻碍。

实际命令与结果（无重复计数；export命令中的python实际为上述biomind解释器）：

```text
python -I tests/export_cli_fixture.py /tmp/biomind_cli_audit_20260911/replay
→ 白名单导出成功。

cd /tmp/biomind_cli_audit_20260911/replay
PYTHONPATH=/tmp/biomind-cli-deps PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m pytest -q -p no:cacheprovider \
tests/test_cli_logging.py tests/test_cli_display.py tests/test_cli_terminal.py tests/test_progress_audit.py
→ 26 passed in 5.46s，exit 0（当时自定义文件13项）。

同一环境，仅新增两个已定位边界：
... -m pytest -q -p no:cacheprovider \
tests/test_progress_audit.py::test_provider_progress_without_payload_and_failure_field \
tests/test_progress_audit.py::test_recovery_event_uses_raw_or_stale_explanation_projection
→ 2 passed in 2.23s，exit 0。

同一环境，仅新增初始plain出口验证：
... -m pytest -q -p no:cacheprovider \
tests/test_progress_audit.py::test_plain_without_owner_drops_emit_only_tool_activity
→ 1 passed in 2.06s，exit 0。

python -I -B /tmp/biomind_cli_audit_20260911/transport_probe.py
→ exit 0；3组现状断言：wrapped reasoning、两种公开正文、旧attempt fence/UI缺口。
python -I -B /tmp/biomind_cli_audit_20260911/protocol_offline.py
→ exit 0；script/Planner envelope边界、下一次已有业务请求接收具体错误事实。
```

合计29个pytest用例断言通过（13个原CLI隔离用例、16个本轮定点用例），另2个独立AST脚本通过。**包含确认目标失败的现状witness；不等于29项目标验收通过。** 本轮没有失败的测试命令；测试揭示的产品缺陷列如下。

| 编号 | 本轮已执行 / 结论 | 未覆盖与后续验收 |
|---|---|---|
| A1 | 两段不同公开fixture都仅生成同一callback模板；TARGET FAIL复现 | 实现后逐段回溯response/call；真实模型质量未测 |
| A2 | 四时点记录；callback正文无显示；Recovery实际sink+Terminal写出晚于下一工具stub派发 | 当前仅组件接线/合成时序；全provider→真实agent-loop→终端验收后续进行 |
| A3 | reasoning未泄露，纯callback协议不公开，无新事件status轮询不编进展；plain最终JSON输出缺口已证 | native/text各响应协议的完整敏感信息矩阵未全测 |
| A4 | 既有DisplayModel活动/审批/失败/unknown区分及真实pipe capture测过；未形成统一生命周期ledger | proposed/approval/running/declined/unknown全业务调度联测仅提出 |
| A5 | script/envelope不接受外侧说明；raw与effect parser receipt在原CLI用例中UI开关一致；TTY/plain正文差异已证 | 完整Plan/approval/CommitStep不变未新跑全图 |
| A6 | 普通renderer异常被隔离，取消/KeyboardInterrupt透传；坏协议仍报原parser错误 | 新可选显示字段尚未实现，其缺失/局部损坏预算验收仅提出 |
| A7 | observer fence有效而Vibe旧活动未隔离；前80字去重碰撞、task/attempt无仲裁、tail覆盖已证 | 两个真实并发模型/执行请求端到端未运行 |
| A8 | fixed fixture请求/工具数一致；原CLI隔离interrupt-loop及raw/parser开关一致 | 所有生产图routes/budgets/CommitStep等价仅提出，不声称已测 |
| A9 | callback+自有logger+router/final同fixture不重复该段；普通INFO未进入console | 外部handler、所有生产最终段落组合UNKNOWN；短prefix误删仍存在 |
| A10 | 原CLI审批snapshot/取消用例；当前活动覆盖取消、无owner BrokenPipe边界已证 | OS信号/杀进程/完整管道关闭全链仅提出 |
| A11 | 合成值的控制序列清除通过；合成secret仍保留，凭据脱敏TARGET FAIL | 未来允许字段策略需单独验收；未读任何真实密钥 |
| A12 | AST原phase normalizer+wire builder将具体错误作为runtime_observation交给下一次已有fake请求，assistant保留一次；2业务调用/0旁白 | 全节点事实字段逐项静态核查；真实模型是否准确解释UNKNOWN |

合成四时点摘要（各自以案例启动为零；数值不是生产延迟）：
| 合成路径 | 首provider事件 ms | 首完整公开fixture ms | 实际write / flush ms | 下一工具stub派发 ms |
|---|---:|---:|---|---:|
| callback_only | 95.163 | 120.420 | NOT_OBSERVED | 145.500 |
| recovery_existing_sink | 6.957 | 32.162 | 157.945 / 157.947 | 57.312 |


Recovery案例sink事件在57.242 ms形成，工具stub在57.312 ms派发，写入157.945 ms、flush157.947 ms。这证明当前flush不能保障“文字已写出再派发工具”；不把DummyOutput测量当SSH/像素可见时延。callback-only案例到测试结束仍无公开段落（显示时点NOT_OBSERVED）。完整记录、短transcript、合成值及可重放脚本见第三产物。

## 10. 产物、Git范围与限制

仅提交以下三份新增审计产物：

1. `docs/audits/cli/cli_model_progress_audit_20260911.md`
2. `docs/audits/cli/cli_output_lineage_20260911.tsv`
3. `docs/audits/cli/cli_progress_replay_20260911.txt`

FINAL HEAD：包含这三份产物的审计commit；完整hash在本次最终回复给出。文件不内嵌其自身commit hash，避免不可满足的自引用；可用 `git log -1 --format=%H -- docs/audits/cli/cli_model_progress_audit_20260911.md` 取得完整回执。提交前后核对父提交、文件范围、原暂存区/工作区状态和相交源hash。**审计commit不授予生产修改权限；未push。**

真实模型/API：NOT RUN。原session恢复：NOT RUN。真实科学任务：NOT RUN。生产代码/正式prompt/既有测试修改：NONE。原数据库/业务状态loader：NOT RUN。启动脚本与.env/密钥/认证配置/敏感环境值：未读取。未调用外部消息工具。没有REAL_MODEL_PASS、没有生产延迟/成本保证。

只将既有 `docs/implementation/cli_terminal_20260911/report.md`、`runtime_optimization_20260911/report.md`、`generate_execute_recovery_boundary_20260911/README.md` 用作历史索引/隔离依赖位置线索；不把它们的旧PASS累加为本轮结果。附件提到的8月26日/9月8日原始现场证据未提供可安全静态核对的精确日志路径，本轮不恢复session查找，因此真实模型当时是否输出公开说明UNKNOWN。当前源码与合成fixture足以定位上述接线/协议缺口。

## 11. 初始工作区范围与证据签名

初始暂存区：空。原36个modified路径（均未由本轮改动）：

```text
agent_core/node_context.py
agent_core/planner_context.py
agent_core/provider_message_safety.py
agent_core/workflow/managed_stop.py
config/__init__.py
config/config.py
config/logger.py
config/settings.py
config/startup.py
prompts/manager.py
tests/planner_fix_acceptance/test_planner_recorded_workflow.py
tests/test_bio_agents_memory.py
tests/test_consultant_first_graph_integration_v21.py
tests/test_execution_provenance.py
tests/test_generate_capability_scoped_prompt.py
tests/test_generate_only_output_contract_v2.py
tests/test_generate_provider_payload_safety.py
tests/test_node_context_semantic_continuity.py
tests/test_output_router.py
tests/test_plan_contract_convergence_v2.py
tests/test_plan_lifecycle.py
tests/test_plan_local_preaction_convergence_v2.py
tests/test_recovery_agent_loop.py
tests/test_recovery_agent_seed.py
tests/test_recovery_context_bounds.py
tests/test_recovery_context_p0.py
tests/test_recovery_progressive_skill_access.py
tests/test_recovery_schema2_node.py
tests/test_recovery_tools.py
tests/test_renderer_controller.py
tests/test_renderer_plan_ownership.py
tests/test_run_profiler.py
tests/test_runtime_preflight.py
tests/test_schema2_adapter_semantic_coverage.py
tests/test_supervisor_validation_evidence.py
tests/test_typed_outcomes.py
```

原33个deleted均位于 `docs/audits/architecture/`。原368个untracked按前两级路径计数：

```text
benchmarks/biomind_agent_benchmark: 224
docs/audits: 80
docs/fixes: 8
docs/implementation: 29
tests/test_benchmark_v3_harness.py: 1
tests/test_benchmark_v5_1_semantic_lint.py: 1
tests/test_benchmark_v5_review_assets.py: 1
tests/test_common_runtime_stop.py: 1
tests/test_failure_packet_incident_lineage.py: 1
tests/test_generate_admission_typed_repair.py: 1
tests/test_generate_context_episode.py: 1
tests/test_hash_prompt_policy.py: 1
tests/test_inspection_node_failures.py: 1
tests/test_inspection_terminal_workflow.py: 1
tests/test_mutation_provenance_integration_fix.py: 1
tests/test_phase_context_activation_persistence.py: 1
tests/test_phase_context_compaction_runtime.py: 1
tests/test_phase_provider_prefix_active.py: 1
tests/test_planner_agent_loop.py: 1
tests/test_planner_feedback_boundary.py: 1
tests/test_recovery_compaction_production.py: 1
tests/test_schema2_context_compaction.py: 1
tests/test_schema2_generate_skill_routing.py: 1
tests/test_schema2_integration_a_production.py: 1
tests/test_semantic_completion.py: 1
tests/test_semantic_completion_workflow.py: 1
tests/test_skill_material_continuity.py: 1
tests/test_step_effect_runtime.py: 1
tests/test_step_effect_schema2_graph.py: 1
tests/test_supervisor_context_continuity.py: 1
tests/test_workspace_mutation_provenance.py: 1
```

完整初始清单：`/tmp/biomind_cli_audit_20260911/baseline.json`，SHA256：`748ad0234a49b792358e52a2064940587a2abf7bcf0fea026e94a076fdcb5da8`。原相交文件hash比对覆盖下列115份源码；同时保留导出原测试签名于replay。


```text
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  agent_core/__init__.py
26b487fcd543a4a49a980b0daa67e68e15b3c7933df7b7d4d507314af1abcae4  agent_core/action_runtime.py
32444198ddc371f0b2d2c8d8e40edb2044e63f569ba72527b6807aa7959cc420  agent_core/active_state.py
9ea558583c7b344ed77ddf24341d0d4299c0b4b77d0503b5742c8f1fd917b852  agent_core/approved_resource_locator.py
f23e46c14d4e7596c9a69711330abbd4a80316862ddc86a787ac81ac3db9a0b7  agent_core/candidate_repair_policy.py
3de2c1e33aea27ec69a440656bddcbfa266a5fa1a8c4695affb97a3ebb5d5609  agent_core/cli_display.py
981e05b7c29d30ec536bf53e7b81291409ebd587ca9b17663765204f52a86f82  agent_core/cli_logging.py
8ca8eb64d249b48871be8c6028ad1f9148924c7eca3b60f52149d18f5cff97d3  agent_core/cli_terminal.py
b4f4bf22b221dfc8f6e01c77e8e33d9c4412a1e3e829a76153e5380d0aad3662  agent_core/completion_gate.py
d359659fb23000de7228df34aab446fb1b253bb53dd34569289bd6313bea0637  agent_core/consultant_binding.py
dbf65f17015470d316bc34b7076973480c181b3a44440924b53df40732dd1544  agent_core/consultant_continuation.py
95622ed70f84e107a3d11d6095df6f0d1d3819975ca90953e7f076640a1e1b17  agent_core/consultant_finalize.py
b8df7415e2fb7039db924f80b7abd47acfcf3e25ac635e7a085d7c161ee91b54  agent_core/consultation_state.py
181bebc8844b393a2ee715f51af88ab3139272452e54b895d597d117568caea0  agent_core/context_budget.py
2be1761ded59e3220ae91a71e911a1793792c4ba812ea35c7ce8e7033a8d6d79  agent_core/context_lineage.py
ba8f244a415ddd7cc29be9042918c5f39cfc35728dce25599a83394830e6dba3  agent_core/controller_audit.py
2da1f92d742d1b249f8beb35114317189db7a7f413c1805a8d1c5c8625bd4f94  agent_core/deepseek_stream.py
3be6dcf23934f0ce44de2aaa4577861b48b35919c9dd634876eeef4f7e80b029  agent_core/diagnostics.py
3f6c2e0c9a3d84c05fa26202b5875a1a37bd8cb5b8abbbcb86488fe32adff1ba  agent_core/formal_artifact_resolver.py
2c5a3521736ee808598a5c0a9252a2227611f90441d6537e0424b424e98c3423  agent_core/genomics_contig_parser.py
13acac45e3465abc0517e5fbf63dcb37857851be9a73989d9f84cfe0df961249  agent_core/human_io.py
795c2f3f32867031cc79d9b67e2e2a11f0c5b76bb713809293371e36f51a8b91  agent_core/inspection_readers.py
1c4b40eb49944d14c45591993aaa501de489769298b7e7438f1459487d287891  agent_core/installer.py
b4c14042cac8219f65c80aa5095357ab8cf9578ae3abd4a0965d71d6514aa46b  agent_core/knowledge_conflicts.py
ee97e68b5ec55c72ff08cf01bea2ddfd563aec6211ed2a20ba19a7d27835ec94  agent_core/llm_safety.py
a2843f837813e5aa6581fe6393f1832431e70e19ddfe580d7b5ea6db5a617261  agent_core/managed_artifacts.py
182d9a9c70c565434076bf33f34d621a13ad57a1ac155660d0228c8cccfdd27c  agent_core/middleware.py
692a76bfa677ca769ff91305d28548a3ac17ee42a0d4f54e971eb4468e736818  agent_core/node_context.py
18b6b57c25d708e89ad8da066bb3d178bd1eaa3c629a3c04926485f24598ded5  agent_core/phase_timing.py
aadf5f47ea849ced22b9a2fe8a52b70014ba6b3063076228a8a1e1c0687dd5ff  agent_core/plan_bindings.py
6d52b45d5f59bc73ab14959339efbc0e1f43e3914993fa648000a3cc726f6a26  agent_core/plan_contract.py
6101ce3decfe546db9672853a0c840686d34f50e5f308701839a398a98bd2804  agent_core/plan_tracker.py
febb7b3a772b6532f0a0bffb83d6fde1fc67ff831ea7e4b3d05633690a40fa46  agent_core/planner_context.py
b2c36c94ab355c52701e7217de17f65aa78b1216be437226d0818c9ab4b5f721  agent_core/planner_loop.py
9d937db3705bbda6f18b5e4344a125ab226870c1e7af7b283c925b5f1a5871e2  agent_core/planner_output_policy.py
87e3b67c8a93c12e619e9b3775b79babb0af99912dd4a219bfed2f52367f89be  agent_core/planner_recovery.py
53a866f8e9a0a3b897cb1e90b877272bf9e85d8d9e5b7e92f8690e108d19bc3a  agent_core/provider_attempt.py
fa3ded55b7c5cd1f5f857b114f6f7f4312e885491391f45981e12f14bbd6ec38  agent_core/provider_message_safety.py
b393f7f7872ac4225dcb445e0c50790614a820981c9aedf16b95cf461693dbf0  agent_core/recovery.py
34a7cd4631ce4f3f68cd342006ed6c7dc5e4349922915d821b991467dfc0a13e  agent_core/recovery_contract.py
febe265f9d000477d87511010e2b807de70d1d585814717ae330b4e82c3b2846  agent_core/recovery_evidence.py
8fc7bcc76f25609149c92c47aed7797939440feb961aa756a66389ed5d9d215e  agent_core/recovery_provider.py
9964e558e7aa987db1b08b52b3a61b821ed6a20885d3d6377a756c67ba84f0f9  agent_core/retriever.py
fa3291f87291777cc51ea2537c171ffcc981ae408836ce0769e69c1d592e9a23  agent_core/root_cause.py
aaf2b7889f4a183961da99ed6dcb7230fdaaa9b6b44642eecb79138d3dc07760  agent_core/router.py
0592e54d55b7dc9beaa83d6994a7ed0e2e25c0fed18a64613ff373ac5a054424  agent_core/runtime_evidence.py
86608e805adb614cac97a5e495bbaef0d26ff757ac50198c7a0b68e4f343dae8  agent_core/runtime_identity.py
485229946948afa1bada3494b81f3c70c24d01e9faf9547c773971c7a94045ee  agent_core/runtime_inspection.py
32fb28019e39158ea18ec37f2b046a5c9e4d6c9b3cd463006f78880b7bce4aa5  agent_core/semantic_completion.py
d883268ea852052991a8a3655a687db98d31b4e967a8e25fd12959c641294341  agent_core/skill_access.py
8552c165a255b962d5be9fe5aa3d0e8ebd21ebe9bc02d051ad595f6fbc980b82  agent_core/skill_discovery.py
b2c4f272705be4a587529716a85fbc963f2f23f5c1f76e720ac68af78800f950  agent_core/skill_manager.py
d7fa524260f21c8aaac5b763603e3e6f4569e96bf819b65b25fe907be3c39e0e  agent_core/skill_resource_binding.py
af4f504c0b61267659d3d554b27f9a9eec92a48b35143fbe838e4450ac8c4565  agent_core/state.py
834b87e246fe1d27af8f797a5c39e9f33c6410d7b565d3b4c1ec78bd72ddec28  agent_core/state_contract_store.py
2846a91e2f2710f8ae9f61dba221c3c74bfa6e862d8d398a437417ac6c628eaa  agent_core/step_effects.py
71aea8c9b11f8180d4c7671b237bc4f8eb9c7733977acd4495cb7ff552b3d059  agent_core/task_spec.py
36ad90e542ddf86d45b4173301c8dd422e9a21441529e6d4c678321916be4c3b  agent_core/wiki_lifecycle.py
53c2997ceadcc125740b6e42ff4dc9380cdccafc0c337882d4e843909976e585  agent_core/workflow/__init__.py
a1f9bc42ef3c48120024837566f97155a9d10c9b2fb54e311b0deb908f771b11  agent_core/workflow/candidate_repair.py
5ce1063d194a0eac8a3c48474ac12fc53a3963c541202b051135b034329bc0db  agent_core/workflow/commit_step.py
5025aab95377469cdbcf3903258581caf521b79b775f1e52a86699bb834d2704  agent_core/workflow/context_activation.py
18c6d74884996e88fcb8ff920cc1dfe15db7c795be3b73f42be30581d97a1e6e  agent_core/workflow/context_compaction.py
5ef3687b3afedb77e6141285f91e784cd22737705e7faa06f5d9fef9ce66bf4a  agent_core/workflow/diagnostic_admission.py
369d89b131d591b419da59811db1348e66e0b1c0b95fa1a9ee5f6277e3d74ded  agent_core/workflow/early_nodes.py
9c823dfe058d844ea7292332c33dbd0df35e9a8abb50f31cdf7afddea158fdfd  agent_core/workflow/execution.py
ebf4f8103363b0407780836dd8c8f41f00b37d1beac3d5a4b4cc46747ae134f1  agent_core/workflow/final_audit.py
574f4021c5e9fec4027730babd98ff36f92c85e1b23bd7a630a143cada80fbcc  agent_core/workflow/generate_handoff.py
e3e128e0937bbf0996853ae95d0948850c99d687b8fce2ca55d3e6a221f8aff0  agent_core/workflow/inspection_evidence.py
30018dd7b5a62411b82f444ec7d94f3f11ac28e344de832f6fc9d765f7575afb  agent_core/workflow/inspection_stop.py
5cfe229646d7300ba561eba7d1d964fa678f184bab4a87c78ca7e74d3411a313  agent_core/workflow/managed_stop.py
43a028f37142a625c2522376c91cfa8027aec6c979ac63d5c10b5dc2ded8cf76  agent_core/workflow/outcomes.py
7a3587589f52db7f51435e277928204120cf55c1bccdf51be7ab577be3c538e0  agent_core/workflow/phase_archive.py
f73a7fac6a9b5d78d8520d079a8f424981fda680a69abfac5596e8a279515640  agent_core/workflow/phase_compaction.py
f8be1e09d038f02a2d59cfca20d6e997425549434641b53beb53d90bd4b84eab  agent_core/workflow/phase_inspection.py
635c381be44cf3d152b44a7a04744c79c4de591ba6ece7d33384ed5b7af21392  agent_core/workflow/phase_subgraph.py
14747444e33ef98d1db433c8f21567f736c439b62406afcd92026a91cca13e88  agent_core/workflow/pi_rpc.py
7e9264259265c816b625aa8afec87377ae6361fd944daf16315668e2bdcd6e4c  agent_core/workflow/recovery_agent.py
9fe3239a72e8f8d5c2d26758c47b115ec223071825065e55b2d5496b2a912480  agent_core/workflow/recovery_context.py
39fa29f3232c68d3ccf85f1ebb68089a28f2489dad98de50f94a84ff1a57ee63  agent_core/workflow/recovery_node.py
509ba56cf7c5d83bdf93f81a21652b45212b8a5f2f551cdfd9d4924da86c498b  agent_core/workflow/recovery_projection.py
eed2022f10421177ff87dd95689c98ddb74491496f40520e80f776dfb00aba3d  agent_core/workflow/recovery_reception.py
2efcba0cdb07d2aac0e519778f2ea9c8eda6f3e3b00aaa22540bf3fd030b64c9  agent_core/workflow/recovery_runtime.py
cb6ada3cdbe895c3b71a78c313e709a43d6a591981f47cb36f119fadf946fb52  agent_core/workflow/recovery_tools.py
80e7b63f6a06ea1409b4b3e3ca7e8ee08dc93a6f1ba29243aed33b67eb782e44  agent_core/workflow/runtime_evidence_recovery.py
9224ea8d627dcfa9f939330c2f7a95561ac958156f6283ef5a99ecd52ad368b9  agent_core/workflow/runtime_graph.py
d35d3f7ff4803719555dcfc3e2b16cae9657418fbfb877f2895e69d80a770ed8  agent_core/workflow/runtime_recovery_reporting.py
d812ae23048fbd06a0c549d17626cb91aff8ab6570775bf998be61bc3cacbde4  agent_core/workflow/scoped_context.py
a0d90c6ff68266ec10abb4d1d536f1ca1896d3d31396c245b747c21db6d1b7e1  agent_core/workflow/stage_contracts.py
ce816aceee63691beaa56d2a4463679ab815d4dadc63d45f145fab2fb0e8478d  agent_core/workflow/step_worker.py
41a87dee3256ecb4f1dfc7bca05b91279413e32ea548882db82b38b67f57b1c2  agent_core/workflow/typed_node.py
a0bd2e12dc20b3664fa44da724be144571cd1303c19f61dc1eded32f3d9a668d  agent_core/workflow/verifier.py
2c250c2f86d5aea2da7e450ccd90cc98c0d583812aa22b005768ff9dae9fdc19  agent_core/workflow_manager.py
e6258f64ff48b2a24e62b9265098e81bbd620d49916ec271dae42c344cd1792a  agent_core/workspace_attestation.py
c29b7e2c7454964c9d05b4de77f501c3b627be452064b066410c7ec5b4f22e44  bio_agents.py
bba896c13436a1c4eacec68b946e43676ba03b9c895d90c9dafc8a2ce52115f0  config/__init__.py
ec7d04be7c225a60835fc3a2afea4bc72dcc2c9540cc9de0a3a865a28203caa5  config/config.py
42657408667fce07c0d36869cfdcf1efedf45279c35604de6a046f1cd75e25a0  config/logger.py
cccba67a7def8603ad013bcd38563d8c7532f18c6e652e055a1e2dee68fd40cb  config/reliability.py
9f0a0227cdf57672dcc0d0888438c16b6d35d258611fba44a1bd44b977cf0310  config/settings.py
fb0e1920dd327ba5f73196aac561b8b4637177def0134b9302b0091aa033578c  config/startup.py
98d661dc77aae3bf23d0c2f70638eeb7cc99d9bcda6a6089c090a96e2ba9add5  execution/__init__.py
64a5a738fd6c0340845aff8e5fb3f980d97893cdcc5ac3ccf1713053deefcae0  execution/confirmation.py
81ac2cc01584426135df2df9ea58aec01be79bfe4eefac96eaa8884abb49a534  execution/executor.py
c671ef9c84b89f488304e0f2c9ddf14c87f2e8d0a79aae89adcf1684431b9ebe  execution/monitoring.py
d73e89dbc1c92bf51c905db447226992cebc9528d0b910791a2cf8ae1faafba5  execution/profiling.py
fa410e2dfd8f59614c44e9de924e5e65c61a65217f989b57da6109efb1c083c2  execution/runtime_contract.py
e0bac176375107af091067e8aab041f4fbb42906295d32044868a12c9c7324bf  execution/script_validation.py
7cdf784a7a4510b2667aff9b3fa596430694cc5e9fdf2c28d049dc751d34648a  execution/shell_safety.py
9255efdf2276eadb5ca62eab3baed7a8a99a35e6c17287019392bce90add4dc9  execution/validator.py
71fc9379314f2bd1a0dd5f7829192657e7ddbadd4d30b9c04b8728792f760fc0  prompts/__init__.py
c22928f281543fdb7df2a35464f8bd44e212eb10203b92586f2b268cfb581feb  prompts/context_compaction.py
30fdd6509992f18549d58b863b2685672cd13a98828f253168e01901f279eefe  prompts/manager.py
c8af0020f47de9b28439156bffaffd44d678872c17cc9f970a0b832b7016431d  prompts/templates.py
9ee0c2a4cc836105b6890cbb72131af728b2f4626802eeb450affa42d2c97742  supervisor.py
```
