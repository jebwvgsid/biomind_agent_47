# 实际入口、前段接线、显示与附加回调审计

本分册只读安全源码，并以新 `/tmp` 导出、新状态、新模型替身验证回调边界。未读启动脚本、配置、环境、旧业务数据库或原 session loader；未执行原脚本。当前源码默认、隔离配置和事故进程实际配置是三个不同事实。**事故进程加载版本、环境开关和外部能力全部 UNKNOWN，不能把本次构造结果投射到历史进程。**

## 从注册反推活动链

`bio_agents.py:54704` `_runtime_main` → `main:54731` `run_terminal(_runtime_main)` → 真实 `BioAnalystAgent.__init__` → `interactive_mode:54175` → `_run_agent_interrupt_loop:53920` → `go` → `_app_for_task:53142`。非 TTY 的 interactive 在构造 agent 前退出；`demo` 是另一个公开入口，本轮未执行。未检验外围启动脚本。构造函数同时组装旧图与完整 schema2 图，不能把“同时被构造”当作“事故同时执行”。

`workflow/outcomes.py:523` 从 durable/state 的 routing schema 选择 sticky 路由，无版本默认 legacy；`_app_for_task` 返回 `schema2_app` 或 `app`。完整 schema2 在 `runtime_graph.py:357` 正式注册14节点；`build_schema2_runtime_graph:160` 只是独立测试运行段，其边界默认空函数并 END，不可替代完整生产图证明。旧图 `_profile_node` 和旧字段路由仅在 legacy 图中可达，不能笼统叫死代码；schema2 从 `_schema2_early_sources` / `_schema2_runtime_sources` 获取原始领域函数，再经 Typed adapter、profiler、causal failure binder、phase 子图和 `observe_managed_node` 组装。

早期节点不依赖模型返回的 `next_step` 决定 schema2 路由。`TypedConsultant` 以真实已提交的 handoff/readiness 或 consultation lifecycle 产生 `planning_ready` / `answer_ready`；后者直接 END，表示无执行问答结束。`TypedPlanReview:981` 要求当前 submission scope、acceptance receipt 和物化 Plan；单独出现 `approved` 字符串返回 `PLAN_REVIEW_ACCEPTANCE_RECEIPT_MISSING`，不是授权。正式入口的 graph 构造与 handler 清单由新增测试的 `entry-assembly-archive.json` 动态记录。

**AskUser 的两种错误生产者有独立差异。** 本轮真实 adapter 证明缺答复 receipt 的 `_semantic_harness_error / ASK_USER_ANSWER_RECEIPT_MISSING` 带 semantic_state 诊断对象，实际路由 END 且失败收尾，属于正确保护。回调直接抛 OSError 时，构造器装配的 `profile_typed_node → early_harness_error` 产生不带 semantic_state 的 `FRONT_HALF_NODE_HARNESS_ERROR`，路由默认回 Consultant，terminal_boundary 返回空。根 `runs/route-02/ask_user_route_failure.json` 已用同生产装配证明此差异；首轮静态假设错误已在 route-01 原失败中保留。后续只修回调异常出口，保留缺答复的拒收与合法 question_missing 返回 Consultant。

## 已实施部分与本轮缺口

已实施 S1—S3 / Plan / CLI / 输入 / 审批的原断言未改。根账本 `baseline/prior_evidence_reuse.json` 比较当前加载候选字节与此前验证清单，复用其可核实部分；旧 fixture 的失败和替身边界仍保留，不把历史 PASS 当真实模型/科学验证。

**默认 Plan 正文仍有真实缺口。** `plan_review` 从当前 persisted Plan 调用 `render_plan_review_content`，为显示生成同一份 `review_content`（`bio_agents.py:40888`），审批输入后重载并比较 submission ID/currentness，再经过 CAS 保存审批。显示层 `ReviewDisplay` 冻结正文和正文 SHA-256，但 `cli_plain.py:199` 仅在 `/details` 或 verbose 才显示正文，`cli_terminal.py:215` 也仅将 summary 放入普通历史。新增阻断见证通过意味着确认该缺口存在，不意味着 UI 已修复。

后续最小接线：由既有 PlanReview 构造的同一 `ReviewDisplay.body` 作为 Plan 默认可滚动正文；保留 F2 详情、原输入 owner、`frame_rendered/input_ready` 顺序、编辑/换行/bracketed paste、q/取消和显式确认语义。禁止从别处重新渲染另一份 Plan 供审批。给 Plan 显示对象明确 stage/type，而非从中文标题猜类型；只在既有 `cli_review.py` / `cli_terminal.py` / `cli_plain.py` 与调用点接线。正文显示前不得允许确认；查看/返回不得批准；不同候选在 pending input 期间不得替换正文。当前 payload 向 `_prompt_user` 只传正文前2000字，完整显示靠同 owner 的 ReviewDisplay，验收要核对二者同 scope 和同正文来源。本轮没有改 UI 或增加协议。

## 回调装配边界

* 无条件构造的附加项：`VibeThinkingHandler` 与 `ProfilingCallbackHandler` 注册到模型工厂（`2627`）；前者 `on_llm_new_token` 明确 no-op，公开输出由 invocation-scoped ProviderStreamObserver/complete-turn 出口负责。真实服务商 SDK 是否调用全部 callback 不在隔离替身能力内。
* SessionArchive：`acquire_observation_writer` 注册进程内 writer，`_observe_session_memory:3954` 只作有界 enqueue，失败/丢弃记录 warning，不授权、不改变业务状态。`aclose` 释放引用并有界 drain，另有 atexit best-effort。本次通过 guard 的独立进程中，真实 writer 在新 sqlite 路径成功，None/dropped/抛错是可选叶子失败见证。
* 旧 memory compression：`_profile_node:24894` 在 Consultant/Generate 后 dispatch `maybe_compress`；生产 `MemoryManager._summarize_with_prompt:413` 对模型 Exception/None 进入 fallback excerpt，并仍归档。这不是“该回调关闭”的源码证据，也不是“事故因上下文丢失”的证据。schema2 handlers 没经过这条 legacy wrapper。新增组件测试以明确 synthetic 阈值触发原 dispatch 和真实 fallback，未把该路径叫作事故路径。
* schema2 compaction：构造器把 `_schema2_context_compactor_invoke` 接入 PhaseContextCompactor、Recovery，Consultant/Planner 子图 `compaction_handler` 受 `SCHEMA2_CONTEXT_COMPACTION_ENABLED` 控制。本次 bootstrap 显式 False；开启后的完整 phase contract/预算回归本分册未覆盖，实际进程开关 UNKNOWN。主审计必须保留该独立未覆盖项。
* terminal summary：构造器无条件绑定 recovery transport 的 `max_tokens=1500`，源码 policy 默认 enabled/30秒，但 `_finalize_managed_terminal` 仅在真实已终止且非取消/waiting/paused 的出口调用；`managed_stop` 再做 scope、durable claim、预算、一次性 reservation 检查。它不能改变业务停止事实。新增五种真实 transport 见证覆盖正常、外部抛错、None、超时 cleanup、取消。实测外部 ConnectionError 会解包重抛，None 原样返回而非 transport error；`managed_stop.py:755` 的真实消费方在 `response.content` 处捕获并记 `SUMMARY_RESPONSE_INVALID`（消费方这一结论为静态证据）；这是正式装配组件测试，不是手塞一个终止状态后声称全链成功。
* Recovery event sink：`RecoveryAgentLoop._emit_event:850` 同步或异步消费 sink，取消/KeyboardInterrupt 向上抛，普通异常包装为 `RecoveryAgentError(RECOVERY_EVENT_PERSISTENCE_FAILED)` 向上抛；`_ainvoke_llm.progress:18198` 对 recovery/summary 的 sink 异常与普通节点不同：单次 recovery 分支会抛出。完整 payload/action scope/event ID 的动态合法性应看主审计 Recovery 见证，不能由“事件有打印”证明已提交证据。
* TaskSummary/optional Wiki：`_finalize_task_memory:4034` 先写 profiler，再观察 archive/写摘要/可选 wiki；后几者有 catch，但 profiler 写位于 try 外。outer `_finish_managed_terminal_result:53909` 对 profiler/结果渲染失败保留已冻结事实。旧 graph fixture 显式换掉 `_observe_session_memory`、`_finalize_task_memory`、`maybe_compress`，所以它的 E2E PASS 不覆盖此组回调。本轮不会自动重构上下文系统。

## 统一实施依赖与组合验收

前段已验证且未变部分复用证据。先完成主审计发现的公共 Generate/helper/Recovery 协议一致性修复，再以真实 normal/recovery/局部纠正链验证；Plan 显示改动只依赖既有 frozen review 作用域，不应重做 Plan 编译或审批授权。输入与事件测试应与这批修复一起运行，而不是每个后段错误再启动一次独立审计。

组合验收至少包含：默认 compact Plan 正文与 F2 相同 hash（plain/TTY/降级）；未展示完成不能确认；审批途中 scope 更新拒绝旧输入；换行、粘贴、/details、反馈与 q；真实无执行问答不创建执行授权；正经 handoff 经 PlanReview；可选 archive 异常不改业务状态；summary 正常/坏返回/超时/取消、重载不重复调用；真正取消的 late result 不进入 Commit；保护已有报告，记录 created/failed/skipped 三种回执而不互换。当前新测试只确认各自声明的组件边界，其余见主账本覆盖/未覆盖状态。

## 本轮验证回执

10个独立组件用例全部取得 pytest call/teardown PASS、进程 rc=0 与 SOURCE_UNCHANGED。汇总见 `evidence.json`，每个case保存实际测试文件hash、导出manifest、生产模块加载路径/hash及结果，跨轮次的测试字节区别明确记录。三次早期批跑在async close线程回调等待中TIMEOUT，不能计PASS；之后以既有测试同类1ms selector pulse处理隔离调度，再逐case运行。首次memory输入全落在keep_recent_tokens保护范围内，因此未触发摘要，标fixture NOT_TRIGGERED；另两次新测试对ConnectionError/None传输预期不符，已记录原始失败并改为当前行为见证。没有修改任何生产文件或旧测试断言。
