# session_1789521763 故障闭环增量审计 — 阶段 D

文档标识：**BM-FAILURE-CLOSURE-1789521763-V1**。本轮只执行阶段 D，未实施阶段 E。BASE为 `118b28e5f320e5d150a5db3eca163a2417218f08`，分支 `refactor/consultant-controller-authority`，以当前安全工作树和已有 overlay 做离线核验。完整审计提交 hash 由 Git 与交付回执给出；本目录的 `FINAL` 指生产源码状态，仍与 BASE 安全源码字节相同。

## 这次到底发生了什么

本次完成输入检查和 snpEff 环境确认，prepare脚本退出0，但工作区核验拒收，尚未建库/注释/统计/交付。模型把 `${GENOME_VERSION}` 作为string值声明；静态准入接受并冻结它，脚本实际写入 `chr1` 路径。核验器按冻结声明检查 `${GENOME_VERSION}` 路径，目录条件通过、两项文件条件失败。不是Supervisor对chr1命名的语义意见，也不是已完成注释却不交付。

执行原始核验错误存在，但没有进入第一次实际Recovery请求。一个Recovery episode中发起7次记录明确的调用，6次完成，第7次用户取消；9次证据工具均成功，页码推进到660/719，没有重新执行prepare、没有revalidate、没有compaction。6次已报告输入496,449、输出14,836，共511,285 tokens；输入占97.1%，不能把全部费用归为59,300字符的长输出。取消请求usage未知。历史必要摘要及来源hash见 [historical_evidence.json](offline_witnesses/historical_evidence.json)。

已有12轮/24工具、重复请求、格式纠正和每请求总超时；新页不会重置轮次/工具总数，但会重置无新观察计数。当前缺少同因果累计业务Token/机器时间与维护子调用的统一派发预算。长篇普通content在inspection回合被当公开说明；请求虽含简短说明要求，模型未遵守。以上不等于已经证明数学意义的无限循环。

## 统一缺口表：保留五类结论

| 项 | 分类 | 确定结论 | 下一阶段边界 |
|---|---|---|---|
| FC-C1 | 旧修复已生效但未覆盖此入口 | 具体路径片段的string仍可带未解析表达式；新变量名、非snpEff结构和三种载体均可重现 | 现有binding契约的字段角色、共享准入、同源公开schema/反馈；先明确literal歧义 |
| FC-C2 | 旧修复已生效但未覆盖此入口 | Execute的nested validation未被`_runtime_payload/_diagnostic_facts`前置；Supervisor不同状态/安装nested evidence另有字段遗漏风险 | 同一FailurePacket的producer/投影边界；首因索引与定点ref，不扩全文 |
| FC-C3 | 旧修复已生效但未覆盖此入口 | 调查次数有限、累计费用未统一；新页不断出现仍可能付出很高成本；没有soft决定预留 | 现有loop/resume/physical attempt/maintenance账本共同预留和结算 |
| FC-C4 | 旧修复已生效但未覆盖此入口 | 原生reasoning/提交公开投影受保护，但普通inspection正文可长篇显示；未发现Recovery显式生成量参数 | 原provider配置、总deadline和公开说明producer；原始协议独立校验 |
| FC-M1 | 模型真实偏离 | 实际声明使用未解析值；实际普通content不遵守简短中文说明提示 | 不能只加强提示词；确定性边界应可纠正/有界停，不保证模型从此不犯错 |
| FC-P1 | 当前已修/保护有效 | 普通缺失observation warning需明确模型审查；同事实revalidate停止；native reasoning隔离、半工具不执行、迟到取消拒接纳、真实workspace后验拒收仍有效 | 本轮11项既有warning/stop回归通过，D4保护见证通过；保持这些行为 |
| FC-R0 | 当前有回归：未获证实 | 没有证据可把本次四类遗漏整体归为前轮代码回退。开发运行里的夹具失败单独登记 | 不把fixture换行、旧compactor协议前提或测试watchdog算成生产已证根因 |
| FC-U1 | 事实不足 | 远端生成是否停止/是否继续计费、本次OS退出码、所有资源清理交错均未证实；实际服务是否执行输出参数未知 | 保留UNKNOWN；本地provider清理、task cancelled、Terminal restored不能升级成远端保证 |

`in_flight/dispatched_unknown` transport journal是现有UNKNOWN派发隔离，可阻止重派发；同attempt另有本地cancelled事件。不能仅据journal字段认定新回归或远端仍在运行。普通取消后的node逻辑状态重入见证，也不能推断重复物理收费：生产transport有completed响应回放。

完整范围见 [boundary_coverage.tsv](boundary_coverage.tsv) 和四张事实图：

- [D1 声明、准入、冻结与后验核验](sections/d1_bindings.md)
- [D2 正式注册来源到首条实际请求](sections/d2_first_failure.md)
- [D3 调查预算、重入、压缩及候选额度](sections/d3_budgets.md)
- [D4 生成参数、流、公开输出与取消范围](sections/d4_output.md)

## 已有实现与本轮增量

复用[原主单](../README.md)、[F6最终22/22回执](../f6/verification.json)及LIVE-PLAN前提说明。原图确实包含真实协议/parser、审批、合成进程、attester、gate、Commit/CAS和报告；模型与科学叶子替身不构成真实生成变体验收。原F6恢复正例是1 Recovery、10 Generate、7合成执行、6 Commit，非未完成的开发快照。

保留阶段A审计 `de7037f7a522e5a7350c07b6349b80965b8f49ed`，阶段B `668c1f68fb743d99cf86f85b4372bb673a36bcd8` / `3faac12b9fc7655cec802653367b319b8ac31d35` / `2931895db0c190149e2bbe66f5401080f2d2ff06`，以及当前warning/stop修复 `118b28e5f320e5d150a5db3eca163a2417218f08`。本次不借旧会话的四次prepare或压缩零应用解释新事故，也不把历史设计当所有入口已实现。

## 离线结果与实际图接线

**最终73个唯一新增当前行为见证通过，另11个既有warning/stop回归通过。**缺陷见证的PASS含义是“当前漏洞按预期复现”，不是生产修复PASS。结果按唯一nodeid与对应测试字节去重，重跑不累加。

| 组 | 最终证据 | 实际验证范围 |
|---|---|---|
| D1：41 | run03的39组件 + run02未改变的2图 | 18个表达式/载体变体；合法literal；精确ref；scope拒收。坏scalar到真实第二次审批，0该步执行；坏handle经原Generate局部纠正，6批准/6执行/6Commit |
| D2：7 | run02 | 实际TypedExecute/binder/Recovery首wire；真实Supervisor/Commit/框架END来源；F6 rc0缺文件图：2执行、1Commit、1Reviewer、1Recovery，该prepare未进Supervisor，源有错误但首wire缺失 |
| D3：12 | run02的10项 + run03的2项 | 新页12轮/12工具、合成reported1,202,400 tokens后按轮次停；重复/多工具/纯正文/unknown/retry；多页后合法指令；压缩2业务+3维护调用、维护810合成tokens，CAS激活与摘要进入下一wire |
| D4：13 | run02 | 完整接收后128k检查、原生reasoning与普通正文差异、跨chunk/未闭合/代码示例、total deadline、length半工具与取消/迟到不接纳 |
| 旧warning/stop：11 | run02/loops | 真实Supervisor/Recovery/Commit直接消费者，普通warning接受/拒绝、硬失败不豁免、同因果revalidate停止 |

run01有5通过、1新fixture失败及1图测试watchdog；该图初版没有沿用F6的scoped event-loop service，不能把超时当生产无界结论，加入同一测试服务后run02实际图通过。run02有64通过、9新fixture前提失败；7项为code外围换行规范化与argv原字节的夹具不一致，2项为usage嵌套字段与旧compactor叶子不支持map协议。只修新见证，未改生产/原测试/断言门；原失败保留。run03对受影响39组件+2预算/压缩项41/41通过。精确调用、命令、加载来源和结果见 [verification.json](verification.json)、[offline_witnesses/results_index.json](offline_witnesses/results_index.json) 与压缩证据包。

本轮malformed stdout夹具预先有结构化effect receipt，因此实际仍为inspection_required；没有把它冒充真实parser失败注入。缺attempt的简化状态经真实profile转换为harness END，0 Recovery调用。对应V06等尚缺的全图变体明确留在下一阶段映射里。

## 具体修复与停止条件

[repair_and_acceptance.md](repair_and_acceptance.md) 将C1—C4映射到现有函数及V01—V20，列出可复用边界、需要的新断言和仍为UNKNOWN的入口；不新增Controller、语义binder、全局类型系统或纠错Agent。

阶段 E 候选预算为soft/hard 300k/450k累计Token、120/180秒机器活动，保留12轮/24工具上限，原额度内保留一次决定机会；这些是可评审的实验配置，**尚未采用或验证为生产默认值**。正确恢复到Commit与有用多页调查必须同时通过，不能把更早blocked视为业务修复成功。

生产源码/已有测试/历史文件未修改；新审计文件按明确目录提交，未push。凭据、启动脚本、正式VCF/FASTA/GTF未读取，原session未恢复，无安装、真实模型/API或生信工具执行。真实模型canary、真实缓存效果复测、科学E2E均 **NOT_RUN**。阶段 D 完成后停止，等待阶段 E 明确确认。
