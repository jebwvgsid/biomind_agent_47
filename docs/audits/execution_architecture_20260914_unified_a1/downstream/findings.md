# 后段接口审计（A1）

后段并非一条“脚本 rc=0 就 done”的链。当前活动 typed graph 由 `TypedExecute → TypedSupervisor → CommitStep → TypedFinalAudit` 消费严格 outcome；Recovery 通过 `RecoverySeed / FailurePacket / RecoveryDirective / GenerateSeed` 接回对应所有者。`interfaces.tsv` 登记 19 个接口，`guards.tsv` 登记 16 组现有保护与退出。这里没有新增全局 validator、Controller 或生产修改。

## 已实施且不能回退的边界

- `CommitStep.run` 重验同一 task/plan/version/step/action/attempt/validation、state generation、报告/决策/消费令牌，重新构造 CompletionReport 和 CompletionGate，再在单次 CAS 中接纳绑定、完成 attempt、推进一步。它已存在“旧 commit 回放不得清除新步 live handoff”的 successor projection 保护。
- Reviewer 已有准确字段错误反馈、两个本地纠正机会以及持久 assessment identity。相同事实只换 report/inspection 包装不增加配额；其 `assessment_pending + automatic_review_stopped` 路由到 END，保留已执行事实，不能 Commit。
- 负面事实可由声明子集 publisher 输出；缺失 artifact 不需制造目录占位。`False` observation 的存在不等于科学通过，实际 predicate 和语义报告另判。
- Generate script 模式已有有界完整 `{"type":"blocked","reason":"…"}` 主动停止对象；正式代码识别后写 `GENERATE_STOP_REQUESTED`。这是现有非成功出口，不能再把“主动结束”写成全部尚未实现。其普通 JSON action/Recovery 对应出口仍需保持公开文案一致。
- 状态报告已有 intended/prepared/published receipt 与 inode/hash 所有权核验；现有文件保护是必要防覆盖，不应引用旧拒写报告来替代本次 `created` 事实。

## 当前新增接缝与验收盲区

1. **候选进展有两个相互不一致的口径。** `bio_agents.py:16525` 的非冻结 binding 分支把候选指纹变化 **或错误指纹变化** 标成 `progress_proven=True`；`agent_core/recovery.py:4928` 的 generated-semantic 分支仍只比较候选指纹，相等便 `corrective_generate_no_progress`。因此同一事件能同时记录 progress=true 和 identical/no_progress 终止。新增组件 witness 验证此矛盾和同事件 replay 不重复扣额；这不代替事故原始候选指纹分析，也不声称换错误就是有效修正。绑定输入已有 `binding_input_progress_facts`，但通用代码/表示修正没有统一的“原始业务候选→parser投影→已修违反项/新事实”记录。
2. **helper 发布口和消费入口不齐。** 普通 script-block 路径在候选 SHA/审批前执行 `materialize_effect_template`；structured JSON 入口也会接收共享 helper 使用说明，需由事故审计核对其实际展开位置。后段新增独立合法 prepare 脚本直接发布实际新文件检查产生的 JSON evidence，以免 helper 前段阻断遮住 workspace attestation；该路线既不修改 helper，也不能算 marker 协议修复通过。
3. **六次 inspect 成功不代表 snpEff 六种科学能力全通过。** 新六步机制链逐步让真正新 tiny process 读取前驱 counter 再写后继，走真实审批、执行、effect parser、Reviewer parser、gate、Commit 和 FinalAudit。它只证明连续真实提交与 FinalAudit 接线。snpEff 的 prepare/build 唯二 workspace mutation、accepted binding、artifact、required_state 消费需单列。新 prepare 用合法独立前端构造和真实新 layout 验证；build/annotate 仍受未提供的实际科学 runtime 限制，静态核验准备状态→就绪状态→只读消费，不编造 ready 或成功证据。
4. **旧测试名不能当证明。** `test_schema2_production_graph.py` 用 handlers 直接构造 action_ready/succeeded/verified，仅证明路由。`test_step_effect_runtime.py::_effect_state` 直接写 `EVIDENCE_COMMITTED/VALIDATION_PASSED`，属于 gate 组件用例。`test_workspace_binding_attestation.py` 的所谓 e2e 先造 attempt 状态、fake executor，部分还 fake FinalAudit，只可证明独立 attester/Commit 组件。`test_plan_resource_fix.py::generate_binding_request` 把前一步 done、当前 ready 设为隔离前置，只证明 request 和绑定规范化，不能说完成真实 prepare/build/annotate。旧断言均保留。

## 接线修复设计及组合验收

最小顺序是共享 helper 的全部候选入口适配 → 完整业务候选/原始 response/parser 诊断的同源持久记录 → 当前局部 progress/停止策略校准 → downstream 正反组合验收。涉及 `bio_agents.py` Generate 三入口、`agent_core/step_effects.py`、`agent_core/workflow/candidate_repair.py`、`agent_core/recovery.py` 及现有公开 prompt 片段；正式修改待统一实施授权。

不要削弱 `CommitStep`、workspace attestation 或 report 所有权来“打通”。验收必须同时具备：正常 marker/structured/recovery/local-repair 同一 helper SHA；真实语法修正继续、同错误换措辞不算修正、同脚本在真实前置事实改变后允许继续；真正重复只扣一次 delivery 并准确显示预算；真实 prepare attester→绑定接纳→build mutation→required_state 消费；假签名/错目录/旧 attempt/negative observation 不能 Commit；Reviewer 格式错误在本地纠正，持久耗尽不能由 revalidate 换壳清零；主动结束独立于次数耗尽。

## 覆盖语义

本轮新增运行与失败原始记录见 `evidence.json`。旧组合仅复用根审计 `baseline/prior_evidence_reuse.json` 已证明字节未变的实际执行回执，不能笼统复用较旧 cross-layer HEAD。成功 FinalAudit 与物理 report 是两件事：六步机制链只有可选 report，没有声称正常路径生成该文件；主动停止用正式 outer finalizer 写真实报告。

固定 provider/Reviewer 是显式替身；新 counter/新 layout 是科学能力替身。生产 parser、审批、tiny executor、证据发布/解析、gate、Commit、持久化与报告函数真实执行。复用 fixture 禁用了 session memory/compression、wiki，并对科学 runtime readiness 使用替身；因此不声称本测试覆盖真实 CLI、所有启用 hook、真实模型质量或真实科学 E2E。启用 hook 与过去关闭的 context 系统调用边界由主审计独立登记。
