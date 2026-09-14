# F6 全图机制组合

**final08 的 22 项框架机制用例全部 PASS。** 每项在同一安全导出的独立进程中运行，实际退出码均为 0，运行前后源文件无变化。导出包含 457 个文件，HEAD 元数据为 `dbaa3f8e11bc56524ba2477c986d722657bf2b0c`，另含保护 overlay；完整 signature 为 `332a06e803cba6fef3e54d2648fef1f05703c1f8b3656637725a6a674934c5f9`。这是所列机制的集成验证，不是裸 HEAD 部署证明、真实模型验证或真实科学 E2E。

入口测试：`tests/a1_unified_fix/test_f6_graph.py`；载体补充 `test_f2_approval_graph.py`；IT03 完整停止图 `test_f6_repeat_graph.py`，均位于 `tests/a1_unified_fix/`。只在当前安全源码导出、原有 guard 和清洁环境中运行。最终统一候选的验证记录由本目录账本指向；早期失败回执保留，不能用后续 PASS 覆盖原失败事实。

六步为 snpEff 注册表的六个不同 operation。真实 Consultant handoff、Plan 资源关联/冻结/审批、Generate parser/helper materialization、脚本审批、非登录小进程、effect parser、workspace attester、Reviewer parser、CompletionGate、Commit、FinalAudit、持久化均使用生产实现。prepare/build 共用一个 canonical mutable workspace，分别产生实际布局和新 predictor-shaped 文件；两个 runtime attester receipts 均必须为实际改变且后条件满足。annotate 消费已接受的相同 binding，随后真实读写 ANN 和 summary，最后一个实际批准脚本写 report.md/reproduce.sh/stage_status.tsv。

模型业务回复是显式固定的 `llm_override` 外部叶子。真实 `_ainvoke_node_llm` 与 `_ainvoke_llm` 保留消息检查、注册、传输包装、重试上下文及消费；实际消息摘要和 provider attempt metadata 逐条核对。正常链实际 8 次 node 注册/消费、8 条 transport history；Recovery 链 12 次 node 注册/消费、13 条 transport history（额外一条来自真实 Recovery owner）。科学替身是新建 Python 可执行文件，接收 Java-shaped argv，创建明确带 SYNTHETIC 标记的 predictor/ANN；它不是 Java、snpEff 或真实生物学验证。runtime observation 以生产 `make_runtime_evidence`/`commit_runtime_evidence_state` 记录这两个新替身文件，保留真实注册表、binding resolver 和义务；不证明本机真实 Java 可用。已有安全 fixture 的安装/环境 preflight 外部观察保持替身，绝不运行真实安装、provider 或科学工具。

正常链原生 argv-only 的 marker 由生产 materializer 在审批前处理；测试不展开 helper，也不在审批后改 shell。另一个 Reviewer 纠正变体使用一致的 native code+argv。执行前核对 approved.sh 的审批摘要与实际 wrapper 摘要，只有该新 wrapper 获 guard 放行；子进程明确 `/bin/bash --noprofile --norc -c`，只获 PATH/LC_ALL 和正式 binding 环境，不含 HOME/BASH_ENV/ENV/PYTHONPATH。

停止路径调用真实外层 `_finalize_managed_terminal`，输入是实际图的 snapshot；它自己判定 terminal boundary，冻结原因、调用固定 fact_ids 的 summary 外部回复、持久化并通过正式 publication writer 写三份报告。重复同一外层调用必须不新增模型请求、不重执行、不改文件。保护冲突必须保留新建的既有用户 report 并明确 partial。

archive 方法、task-memory 方法和 legacy compression 方法恢复真实实现；schema2 compactor 在构造前显式启用，spy 包装真实 prepare。正常图可观察 archive/prepare 调用，但其 FinalAudit 直接 END 不调用 `_finalize_task_memory`/TaskSummaryRenderer；不把零调用写成已验证触发。停止 summary 经真实外层 owner 单独触发。1 ms 测试事件循环 pulse 只服务实际回调并一直保留至真实 `aclose` 完成；不替换 production hook、executor 或 close。

科学 E2E 与真实模型为 NOT_RUN。机制通过不提升为科学正确性、真实模型遵循率或部署通过。

开发验证曾有隔离进程在旧 bootstrap 的 70 秒定时全线程栈输出附近返回 -11，另有多用例总时长或 close 的外层超时；原始输出、加载字节和状态分别保存在 development_ledger.json。尚未证实 -11 的根因，不能归因于生产逻辑或已确认的 CPython 缺陷。最终新 F6 fixture 取消该旧定时 dump，保留 fatal handler 和独立外层硬超时，并以事件循环每 10 秒写轻量阶段计数；这是测试诊断适配，不是生产修复或生产停止成功。业务断言不因此放宽。

F2 载体补充在独立 `tests/a1_unified_fix/test_f2_approval_graph.py`：native code-only Python 用 snpEff check；legacy Python/Bash 用现有 WGS inspect capability，使公开 Generate 协议自然选择 script_block_v1。三例均走真实 handoff、Plan、marker 物化、人工审批中断、非登录 approved-file runner、effect/Reviewer/gate/Commit/FinalAudit。它们只要求一项负面诊断，不请求物理报告，不能替代六种 snpEff 的正常链。native Python 的默认解释器命令由生产形成，测试只在事先声明的清洁 PATH 中提供已知解释器；不补 argv。legacy adapter 中的 bash -lc 元数据保持原样，当前执行分支实际运行批准文件，测试检查真实 wrapper 无登录参数；这不证明其他未执行分支安全。

回执入口：[矩阵](matrix.tsv)、[验证账本](final_ledger.json)、[精简 scope/接口证据](compact_evidence.json)、[实际加载 manifest](final_manifests/main.json)。每场 `final_cases/<case>/` 保留进程 rc、pytest receipt、实际模块路径/hash、压缩原始 provider/图/状态记录、原始批准/执行脚本与持久化状态归档；正常与停止输出另存可直接阅读的 `outputs/`。历史独立索引：[candidate01/02](candidate01_02.json)、[candidate03](candidate03.json)、[candidate04](candidate04.json)、[candidate05](candidate05.json)、[candidate06](candidate06.json)、[candidate07](candidate07.json)。01–03 曾替换整个模型派发方法，不能证明真实包装；04 补齐真实包装但未覆盖后来修正的 env 边界；05 保留两项 PASS 与 ABA 新测试反馈数量假设造成的 rc1，停止了未开始的派发。原字节、原退出码及旧测试源码均保留，06保留22项PASS及其后来观察到的origin回落标签；07只导出未运行。各轮不计入final08的22个PASS。

IT-02 的不可解码 raw response 没有可恢复脚本；四轮相同的 `scientific_body_sha256` 只是 fixture 预备修正脚本的输入指纹。进展结论依据实际 parser 的目标事实与下一次真实请求中的反馈：`response → action_type → env` 被逐步解决，binding 轴仍未求值，3 次额外修正后预算剩 1；不依据错误码改变或脚本相同判定科学/语义进展。

本分册不把普通 Generate 的 `candidate_repair_state.provider_claims` 动态重载证明套到 Recovery：后者有自己的 `recovery_state.generation_claim/history`、缓存与 CAS owner。这里的 IT-02 证明真实 Recovery 后三次表示纠正的预算、反馈与成功；独立 Recovery claim/unknown 重载场景由 F3 追加分册记录。Reviewer 耗尽后的 `revalidate` 是对本图真实失败输入调用原构造 Recovery owner、再调用实际 Supervisor 的接口复验；不是额外一次完整图恢复成功。

IT-03 的四个完整图分别覆盖相同坏 raw 重复、A→B→A、env 键重排和新增无关合法字段。它们先经过真实 handoff 与冻结 Plan 审批，然后经正式 parser/共享账本退出到 END，始终没有脚本获准执行、没有 Commit。重复/重排/无关字段场剩余 3 次；ABA 中 B 实际解决 env 对象目标，回到 A 时停止且剩余 2 次。每个模型响应都与正式 StateContractStore 的 completed claim 响应逐字节一致；下一请求按正式 correction number 对齐最新反馈和余量，保留历史反馈。真实外层终态写出三份报告，重复调用不再生成或改写。

最终IT03终态已同时核对 origin.source_stage=generate、source_owner=GENERATED_ACTION；原动作错误、无执行attempt、causal_execution UNKNOWN及剩余预算均保持真实。
