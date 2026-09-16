# 最小共享边界修复与 V01—V20 验收映射

**阶段 E 设计，未实施。**本轮授权仅阶段 D。以当前工作树为基线，保护 `managed_stop.py` 等先存 overlay；后续提交不得收编它们。旧 F1—F6、Generate/Plan/审批、普通缺失 observation warning、取消和上下文独立性继续保留。

## 一、四个逻辑包与修改边界

| 包 | 现有文件/函数 | 最小生产增量 | 必须保留 / 不做 |
|---|---|---|---|
| C1 值语义与准入 | `plan_bindings._validate_binding_value_contract/public_binding_requirements/binding_input_progress_facts`；`workspace_attestation._render_relative_template`；同源 operation binding rule / Skill 公开声明；相关 `step_effects.generate_output_examples` | 声明该 binding 是具体标识/路径片段还是普通字面值；按同一规则校验、发布 schema 与具体 field_path 反馈，接原 Generate 局部纠正。执行仍消费已批准值派生的 env，intent 与 attester共享同一冻结值 | 不按变量名、step名、数据库名特判；不全局禁 `$`；不扫描或执行脚本推值；不修改旧批准/intent；保留 code/argv/helper 共同载体边界 |
| C2 首轮失败事实 | `workflow/execution.TypedExecute.run`、`workflow/inspection_evidence.prepare_inspection_evidence/read_execution_facts`、`workflow/recovery_projection._runtime_payload/_diagnostic_facts/build_causal_failure_record`，复用 `evidence_projection` | 从已有可信 producer 读 scope 合法的核验事实，前置有限错误索引、expected/observed证据和 pointer；覆盖 rc0后置失败、不同 ValidationEvidence 状态、Commit/安装结果各自来源。继续生成同一 FailurePacket | 不扩大 read 上限或递归全文；不把 missing 当 false；不从脚本文本声称文件实际存在；未知 code保留原文；不增平行 CompletionReport |
| C3 调查累计预算 | `workflow/recovery_agent.RecoveryAgentLoop/_resume_state`、`recovery_node.Schema2RecoveryNode.run`、`recovery_runtime`，既有 provider attempt/transport 和 compaction maintenance receipt 的直接接线 | 在已有因果身份下汇总调用/输入/输出/unknown reserve/机器时间；dispatch 前持久预留，唯一物理 attempt 结算；新页、epoch、重入不清零；soft 预留一次决定机会，hard 走原 stop | 不再造 BudgetManager、Agent 或因果 ID；不把逻辑请求和物理请求重复计数；不降低全局压缩阈值补成本；不以 blocked 代替成功恢复验收 |
| C4 生成/展示/取消边界 | `bio_agents._build_recovery_llm/_schema2_recovery_provider_invoke/_ainvoke_llm`；`provider_attempt.run_provider_attempt`；`recovery_agent` 公开 explanation producer；必要时同一 cancellation receipt 消费者 | Recovery 专属生成量参数真实进入最终 kwargs；绝对请求 deadline受父剩余资源约束；超长/不合约普通正文及时停止或受控缓冲。展示与原始协议解析分离；不完整/取消/迟到指令不能接纳 | 不把原生 reasoning当公开文本；不做全局正则删标签/吞代码示例；不为摘要再调模型；不把本地取消回执当远端停止计费证明；保留原CLI/审批输入 |

### C1 需要明确的最小接口决定

现有 `type=string` 无法区分表达式意图和真的名为 `${LITERAL_TEXT}` 的文件。阶段 D 不能用字符猜测抹掉该歧义。建议只在现有 operation binding rule 增加具体值角色（如 `binding_value_roles: {name: concrete_identifier}`），由引用此值的冻结 workspace contract与公开 schema 同源消费；普通 literal/opaque JSON维持原义。具体角色保留有限、明确的未解析表达式拒绝规则；如业务必须允许“占位符形状的字面标识”，需要该角色的明确 literal policy，不能偷偷 expandvars。这个接口范围要随阶段 E 确认，不能把它称为当前已实现行为。

静态准入只能证明声明符合协议；任意脚本仍可能覆盖 env，所以后验核验不可删除。运行后才知道的值当前没有任意 stdout→scalar binding 自动通道；先通过既有批准范围的观察建立事实，再由后续候选明确消费。不得为本次顺手构建 DSL/runtime expression resolver。

### C2 统一事实格式和有界索引

最小信息为：`source/stage/scope`、process started/rc、validation status、Commit状态、拒收错误及具体检查、期待值、捕获的观察及 ref/pointer、unknown项。错误很多时要保留关键失败索引、省略数与定点读取位置；当前 `16 facts` 或 `12 findings` 的简单截断不能作为首因可见性保证。可复用一个 scoped 检查摘要函数，替换直接消费者重复挑选字段的代码；不删除 raw archive。

### C3/C4 候选配置与计数口径

候选值详见 [D3 第5节](sections/d3_budgets.md)：soft/hard 300k/450k累计输入+输出、120/180秒机器活动、原12轮/24工具硬顶保留、原预算内预留一次决定。它们是历史回算和离线正例校准起点，**没有应用或验收为新默认值**。历史第4轮后 321,765，第5次输入加8,192预留为422,464；第5次仍只读页则停止。不能拿这些反事实数值宣称实际节省了Token。

生成量建议先与现有8,192输出预留对齐做 E 实验，再用最大合法 directive/工具参数回归调定；只有实际 provider kwargs 和 length负例通过，才能接受该参数。普通正文公开长度/等待应另设节点级约束，不等同整个协议长度。原 per-request total timeout已存在，应用 parent剩余时间应复用它，而非再加可由chunk续期的idle计时器。

未知 usage保留UNKNOWN或预留；缓存hit不从容量/累计Token里扣除。物理 retry按唯一attempt计一次，SDK不可观测内部重试明确未知；transport缓存重放不会被当作新物理调用。人类审批等待独立，Recovery预算不用于随意杀批准的长时科学进程。

## 二、V01—V20 完整映射

本轮结果分“现状缺陷见证”“当前保护见证”“旧限定证据复用”“E待实施”；pytest通过不等于修复通过。最终执行计数见 verification.json，原失败不隐藏。

| ID | D 实际见证或复用 | 当前结论 / E 必须补的断言 |
|---|---|---|
| V01 | D1 三标识×两语法×三载体；非snpEff `prepare_bundle`；坏值到真实审批图 | **缺陷在**；E应在审批/执行/Recovery调用前精确拒收，换名称仍生效 |
| V02 | D1 正常显式值、中文空格、美元/花括号字面、支持模板和opaque JSON | 当前可用；E按角色保留，不能全局禁美元符号 |
| V03 | D1 code/argv/both，原F2载体/helper与Generate协议验收 | 共享路径现可达；E三个载体相同语义检查且原helper不回退 |
| V04 | 新坏handle→原Generate纠正→6批准/6执行/6Commit；未解析scalar→审批现状图 | 旧局部纠正闭环PASS；**scalar自动纠正链未实现**，E需bad→good的真实图，不能预填approved |
| V05 | D2真实F6合法binding，rc0但删必需文件→真实attester→Execute失败→首Recovery | 硬核验保留，**首轮事实缺失在**；E首wire必须含错误/来源，无该步Supervisor/Commit |
| V06 | D2真实Execute后置失败、Supervisor concern、真实Commit拒绝；旧F6 rc7恢复；缺attempt经真实profile转harness END | 各owner分开；当前 malformed raw text夹具已有结构化回执，未形成parser failure，不能冒称覆盖。E补真实parser failure、validator exception、CAS conflict首wire正反例 |
| V07 | 历史错误在step_attempt第518+行，0016首wire缺失；D2当前源→wire重现；源码facts上限 | **有损摘要在**；E错误顺序/长日志变体须不遗漏关键检查且不全文注入 |
| V08 | D2未知validator code嵌套同样丢失；D1 scope/引用拒绝；旧inspection权限/过期证据 | 未知code不应崩溃但当前嵌套仍遗漏；E实际缺失/过期/无权访问各写unknown/拒绝，不造actual |
| V09 | D3真实loop连续新页直到12轮/12工具，合成1,202,400 reported tokens | 次数硬顶有效、累计成本cap缺；E soft/hard下总量不重置，无后继科学Execute |
| V10 | D3相同请求换call ID仍第三次拒、多工具逐个计费、工具错误 | 当前重复/工具总量保护有效；E加统一资源账本不破坏 |
| V11 | D3纯正文最多一次格式纠正；D4正文<128k公开、>128k完整接收后拒 | 现有协议stop有效，生成成本/公开正文边界缺；E流中约束和provider max参数实际生效 |
| V12 | D4总时限、持续chunk、length/半工具、缺usage、取消后完整迟到结果；D3工具错/transport retry | 当前部分协议不接纳，绝对超时存在；E父预算联动/缺usage预留/每个物理重试结算待测 |
| V13 | D3硬轮数结束未为下一合法directive留位 | soft-close尚缺；E收束时合法directive正式接纳，仍只调查则typed终止且不加“最后总结”调用 |
| V14 | D3显式resume计数、真实node compaction/CAS/摘要下一wire、普通取消后逻辑重入；旧阶段B压缩CAS回放 | maintenance现有账本保留；E父子统一资源/物理dedup/无receipt重载。逻辑重播不等于重复物理计费 |
| V15 | D3三个有用页后第四轮合法指令；旧F6恢复7执行6Commit | 当前可调查并给出合法决定；E把同一正例放到新budget guard下继续成功，不能只测blocked |
| V16 | 当前warning/revalidate-stop 11项回归；旧阶段B read→revalidation→Commit及原attempt不变 | 当前局部修复仍有效；E首轮投影补齐后不得重复科学副作用，原attempt复核/Commit需真实接线 |
| V17 | D1六种真实attestation scope错配；现有approved normalization drift检查 | 保护组件通过；E后改action/binding的审批/执行全图变体仍须补，不回写历史 |
| V18 | D4跨chunk/未闭合analysis、字面代码示例、native reasoning和原始协议保真 | 原生reasoning隔离有效、普通正文标记仍展示；E限定Recovery公开说明的buffer/parser，不能全局正则吞代码 |
| V19 | D4 provider取消/迟到/UNKNOWN；本次历史cancelled+terminal restored；旧阶段B /exit变体和压缩取消 | 本地各scope分开；tool/model/compaction/普通Commit前的全交错组合仍需E补。远端停费及本次OS退出码UNKNOWN |
| V20 | D1非snpEff通用bundle多名称组件；新坏handle恢复全链；D2真实拒收图；旧22图限定结果 | 机制不应依赖chr1/operation同名；E将C1修正及C2修复成功链组合到不同工作区/step/ID，真实canary另授权 |

## 三、阶段 E 开工和组合验收顺序

1. 核对实际HEAD及目标函数新改动，保护overlay；冻结本单确认的字段角色与预算政策。
2. C1/C2分别做小逻辑修改与定点回归，复用真实F6框架。先让错误能被纠正和首因可见，再加成本收束。
3. C3/C4接已有provider/typed stop/持久receipt，保持生成限制、上下文容量和累计成本三个口径独立。
4. 同一最终生产字节上运行：坏候选修正→批准→Commit；rc0后置失败→首Recovery明确事实→合法纠正→新审批→Commit；有用多页成功；持续分页/长正文/缺usage有界终止；压缩/重入/取消/迟到；/exit换行零模型。
5. 记录每条链的真实owner、首次wire内容、批准/Execute/模型/工具/Commit数量、输入输出估算来源、stop原因和最终持久状态。分逻辑包安全commit，不push。

阶段D不承诺真实模型遵约率、实际缓存收益或科学目标完成。真实模型canary、真实缓存率复测、科学E2E均 **NOT_RUN**，需要后续独立授权。
