# Plan 规划权 V2：实施回执

本轮增加显式 `framework.generic.execute`，合理的 Bash/Python 步骤不必冒充领域 capability；未知能力不会自动转入通用执行。新 v3 Plan 以 `resource_uses` 单次表达资源读取/变更和需要时的 helper role，旧输入列表与资源侧关联改为内部只读投影。TaskSpec 已确定的输出义务、路径策略和固定路径不再重复填写。每步三 Skill、纠正 READ 后必须补叙述理由的偏好降为建议；未启用的 batch/workflow 字段可省。

不同步骤命名、同操作多个实例、通用只读、带内部临时文件的通用写入，以及不同拆分/合并组织已通过隔离完整链。保留的硬边界仍拒绝未知对象、无来源的必要输入、缺生产者或状态依赖、越权资源/工具、输出义务降级、审批失效及没有真实证据的成功。此次削减限定在 Plan 与直接消费者；没有宣称全架构 Controller 职责剥离。

## 第 10 节逐项结果

| 字段 | 实际结果与证据 |
|---|---|
| IMPLEMENTATION_STATUS | **PARTIAL**：授权范围的生产实现和隔离机制验收已完成；一处 V2 公开投影改动依赖先存未提交函数，无法不收编该 overlay 而独立进入生产提交。其工作树改动及精确 patch 保留，不能把裸 HEAD 称为已验收的版本。 |
| BASE_HEAD / PROTECTED_OVERLAY | BASE `81d10c58a236f44c7987f1913190e6927ce23248`，tree `d53c5ca94271ce4c6a5a0c2d7305238c1e955adf`。实际基础为安全工作树 + 原 overlay，见 baseline/start.json、source_manifest.json 与 protection/。目录或 Git HEAD 不作为实际加载证明。 |
| FINAL_PRODUCTION_COMMITS | `fda82d91a12c4965723dc8482529a0991788d59b`（主体源码、公开 prompt 及对应测试）；`1ace58d1fe3a04d4c0e40648a0349aa8a6126a5a`（通用工具不匹配复用已有 typed exit）；`5673b236c80a694e27e72e7b0f90f0a9946bf4b9`（公开示例与真实当前 mode/tool/resource 准入同步）。逐文件范围见 commit_files.tsv。 |
| ARTIFACT_COMMITS | 新测试载体/隔离 fixture 已提交 `4d0d9c546a401819dc9af1cb93f355600e3bc256`；本回执所属材料提交的完整 hash 由最终交付与 Git parent 链给出；同源示例与新停止测试已随 `5673b236c80a694e27e72e7b0f90f0a9946bf4b9` 提交。材料提交仅本分册和原主单末尾增量。 |
| FINAL_TEST_SOURCE_SIGNATURE | 最终文件数、完整安全导出签名与生产资源签名见 verification.json、final_export_manifest.json。逐场实际导出版本和实际加载模块另存；较早场景使用有限差分证明复用，不描述成全部同版本重跑。 |
| RETIRED_OR_DOWNGRADED_GATES | 专属领域 capability 不再垄断执行；v3 不再强制双写 inputs/role_associations、TaskSpec 输出义务、未使用的 workflow/batch 字段；每步三 Skill 与删除不可用 READ 后的文字解释要求降为 warning。既已退役的脚本命名门不计为本轮删除。详见 resource_rule_disposition.tsv 与 public/rule_disposition.tsv。 |
| HARD_BOUNDARIES_RETAINED | 精确身份与引用、被选 Skill 范围、未知 capability/tool、不可变/可变资源权限、输出生产者、状态依赖、TaskSpec 必需义务和路径、证据/attester、批准对象和 receipt/CAS、旧 reader 兼容拒收、预算/去重/主动停止/取消、报告保护。没有整体关闭校验或将旧偏好迁到 Generate/Commit。 |
| SINGLE_SOURCE_FIELDS_AND_REMOVED_DUPLICATES | v3 的 `steps[].resource_uses[{resource_ref,access,role?}]` 是可编辑读取/变更来源，内部 inputs/reverse roles 精确派生；TaskSpec required/path_policy/exact path 冻结读取，仅未定文件名由 Plan 补充。outputs 的生产事实、workspace 的状态变更、执行后的 observation/evidence 是不同事实，继续保留。显式 optional 仍需 `required:false`，v3 未标可选的本地产物为必需。 |
| EXPLICIT_GENERIC_ROUTE_RESULT | PASS（限定框架机制）：公开选择 `framework.generic.execute`，Bash/Python 均经原 parser、工具/身份约束、代码审批、批准文件、实际解释器/helper、执行、证据与 Commit。空领域 Skill 选择可完成正式 Plan 和两个 Python 小进程；不增加 fallback capability。 |
| PUBLIC_SCHEMA_PROMPT_AND_ACTUAL_REQUEST_RESULT | 19 个唯一公开协议用例 PASS。实际 Planner request 内原样完整例通过完整准入；正式旧引用错误的具体公开字段、空值和 not_evaluated 进入下一实际请求，随后 v3 候选完成 8 Commit/FinalAudit。精确公开路径豁免，任意同名控制字段仍拒绝。messages 是应用实际调用边界，不冒称真实线上 wire 请求。 |
| ADVISORY_NONBLOCKING_RESULT | 完整 advisory_context 图 PASS：同一通用步骤实际带 4 个 Skill，上下文未被截断；Planner=1、Plan 审批=1、修正=0、Recovery=0、执行/Commit=8，FinalAudit task_verified。READ 解释降级另有组件证据；不将一个 warning 变成隐藏确认或额外预算消耗。 |
| LEGACY_COMPATIBILITY_AND_APPROVAL_RESULT | 合法 v2 原表达保留，v3 禁止混写退役可编辑字段；v3 reader 最低 1.2.3，旧角色格式仍 1.2.2。相同冻结 Plan 的正文、F2/details、当前 owner、CAS、明确 Enter、EOF/空白粘贴/历史排队输入保护均有真实 PTY/plain 14 场景 PASS。旧批准计划不自动迁移。 |
| GENERIC_READONLY_FULL_CHAIN_RESULT | 新完整图中真实通用读小进程，声明的观察由 helper/attester、Reviewer、gate 接纳后 Commit；另有无 observation 的 rc0 反例被阻止。完整图报告见 graph/verification.json。只读是正式声明与被测动作行为，不代表验证了任意代码的 OS 系统调用沙箱。 |
| GENERIC_LOCAL_WRITE_FULL_CHAIN_RESULT | generic_split 完整 8 步 PASS，真实创建与 stat 临时文件、原子改名到已声明输出；内部临时路径不强制增加 Plan output。批准正文记录动作，输出内容和身份进入真实证据、Commit/FinalAudit。 |
| RENAMED_REPEATED_OPERATION_RESULT | renamed_repeated 7 个异名实例 PASS；同一 check operation 两次使用不同 step_id，实际审批、执行、Commit 各 7。按 current_step_id 读取实际实例，不将 operation 当步骤身份。 |
| ALTERNATIVE_PLAN_TOPOLOGY_RESULT | generic_split 8 步、generic_merged 7 步均完整 PASS；合并动作实际解析合成 ANN 并生成摘要和报告，非六个 inspect 拼装。保留任务义务的前后候选对照见 graph/candidates；旧侧如仅保留 admitted snapshot 与 digest，明确不是丢失的原 provider response。 |
| MISSING_REFERENCE_DEPENDENCY_AUTHORITY_NEGATIVES | 资源/通用组件 36 PASS；不存在引用、缺依赖/生产者、错误 helper role/access、未知能力、工具错配、路径或 required 降级均有反例。实际图依赖失败后不生成后继产物，工具错配在代码审批前拒收。 |
| FOURTH_VALID_REPLY_AND_FORMAL_STOP_RESULT | 首轮正式 STOP、前三次坏候选后第四合法候选、第四正式 STOP 均 PASS；没有第五次调用，未改生产预算。第四合法例准确限于审批待定；完整下游由独立合法图验证，不以待审批代替完整链。 |
| RECOVERY_RELOAD_CANCEL_RESULT | generic_recovery：首次真实 rc7 后 Recovery、重新 Generate/审批，10 次执行、9 Commit、FinalAudit task_verified；generic_blocked：真实依赖 rc23，4 Commit 后受阻且不造成功。首末 Commit 由生产 reader 重载后重放同输入，receipt/代次不变。旧 late-result、取消、重载预算/去重回归见 protection_runs。重载是同进程 production reader，不冒称独立进程整图恢复。 |
| REQUIRED_DELIVERY_AND_FALSE_SUCCESS_NEGATIVES | 无 observation/空证据/缺必需依赖/空 summary、必需义务 False、Reviewer veto/exhaust 等真实图拒绝成功；诊断性 False 按本义务可成立。FinalGate 删除实际状态链或 attested mutation 的反事实均拒绝，不能仅凭目录存在视为已完成。 |
| AFFECTED_F6_AND_PROTECTION_REGRESSIONS | 新图 8 场、旧 F6 22 场均按各自明确断言和版本登记；保护 155 个唯一 pytest ID 为 154 PASS + 1 已有版本断言 FAIL。另增两 mode 停止与两实际请求示例共 4 个唯一用例 PASS；全体 244 个唯一 ID 为 243 PASS + 1 已有 FAIL，另列 PTY/plain 14 场景。不能将 rerun、subTest、多个阶段回执重复计数；旧见证 PASS 也不表示历史业务问题被修复。 |
| EXISTING_FAILURES_PRESERVED | WGS 测试旧断言要求全部 Skill version=1.1.0，BASE 和 final 文件实际既为 1.1.3/1.1.1，旧断言保留。早期真实失败、看门狗超时、宿主中断、驱动未派发、格式错配均留原目录，不以删除断言/skip/预置状态刷绿。旧主单的其他既有失败保持其原范围。 |
| ORIGINAL_F1_F6 | 原实施及原限定证据保留；本轮新增受影响的实际回归，不改写原 22/22 为真实模型或科学成功。 |
| REAL_MODEL_RELIABILITY | NOT_RUN / NOT_AUTHORIZED。固定 responder 对反馈作条件响应不等于模型自主纠正，不报告规划成功率。 |
| SCIENTIFIC_E2E | NOT_RUN / NOT_AUTHORIZED。科学叶子为合成算法与文件，未执行真实工具或读取正式科学数据。 |
| ORIGINAL_SESSION_RESUMED | NO。所有动态回放用新隔离任务和合成数据；没有加载/恢复原 session。 |
| PROTECTED_DIRTY_STAGED_UNTRACKED_PRESERVED | YES，最终保护账本见 protection/final_integrity.json。未 reset/clean/stash，未收编旧 hunk，未要求工作树 clean；敏感配置只见 Git 状态元数据，没有读取或散列其内容。 |
| PUSH | NOT_PERFORMED。 |
| REMAINING_BLOCKERS_AND_LIMITS | 公开投影的先存未提交函数相交是唯一生产提交隔离阻塞；精确增量见 protection/planner_context_v2_over_protected_base.patch。已有 WGS 断言失败独立保留；旧 F6 原始业务 response 未保留时仅能比 admitted snapshot。历史真实运行加载仍 UNKNOWN。外部 provider/科学能力和任意 syscall 隔离不在本轮授权/验证范围。 |

## 实际下游证据及版本适用性

`graph/runs/` 与 `regression_runs/` 保存实际 provider 应用请求、批准记录、approved/executed 文件、执行回执、证据、gate、Commit、持久化与三个报告；分册有逐步骤义务映射。`final_test_cases.json` 按唯一 pytest ID 汇总，`regression_final_verification.json` 不把较早加载的 bio_agents 哈希改写成最终哈希。

current07—09 的生产资源字节相同；current10 的生产变化是通用解释器错配异常分支返回既有 typed exit 字符串。新错配场景单独回归，普通/恢复/局部纠正相关的 44 项组件追加回归；最终完整正常、恢复和受阻图重跑。旧 22 图使用领域 capability，实际 scope 证明不进入这个通用错配分支。比较证明见 `protection_runs/current10_vs09_exit_literal_proof.json` 和各分册依赖账本。这是限定影响分析复用，不是最终版本全量单次重跑。

所有图均说明替身边界：固定 provider 业务回复、合成科学叶子和必要的外部 preflight/capability 观测；TaskRouter 的 fixture intent_admission 亦明示。Plan、审批、parser/helper、批准载体、真实小进程、attester、Reviewer parser、gate、Commit、重载与 report 走实际生产代码。没有预塞 ready/approved/succeeded 充当全链。

## 未收编的生产增量

`agent_core/planner_context.py` 的 `provider_correction_feedback` 基础函数在本轮 BASE 已来自先存未提交 overlay，HEAD 中没有该 52 行函数。V2 改动在函数内部，无法单独提交又不把旧函数骨架收入本轮。工作树保留完整已测代码；本分册保存 BASE 保护 patch、V2 相对增量、文件 SHA256 及相交位置。其他受保护文件的可分离 V2 hunk 已通过临时 index 提交，原 hunk 仍留工作树。没有为获得 clean Git 状态挪动或重写生产函数。

此限制只影响这处提交归属，不阻止已授权测试与其他逻辑提交。最终代码使用者必须理解：已测候选 = 生产提交 + 先存保护 overlay + 这处 V2 精确增量；单独检出提交不是本次通过的加载集合。

最终公开协议补验还发现两处被旧固定回复遮住的缺口：脚本模式停止示例使用原生 JSON 的 discriminator；generic Bash 步骤的示例固定为 Python。已只在 `generate_output_examples` 中同步当前 mode/tool。Bash 表示示例安全引用已公开资源，以明确 exit 1 结束，尚不实现业务、不开文件、不产生事实；Python 例也在科学实现前明确失败。二者用于证明完整表达可准入，不能当作执行成功。真实科学替身下的通用完整执行另由完整图证明。

current14 的 5 项定向测试全部 PASS：两 mode 的两个公开停止位置原样经真实 parser、Bash/Python 从实际请求取原例到真实 action_ready、工具错配后原样使用实际 STOP 例到原生 `GENERATE_STOP_REQUESTED`。停止图未发生代码审批/执行/Commit，示例准入两图明确停在代码审批之前；没有用它们替代既有完整链。current12 的旧 39 F1/F2 全 PASS，final14 后续只补 generic Bash 示例中的公开资源引用，旧组实际 scope 不进入该分支，证明另列。current12 两个新示例用例的失败（Bash 缺资源引用、Python 新测试字段误读）和 current13 未派发状态保留。完整原因、实际 request 指针、原回执和新断言见 graph/late_public_protocol_defects.json。
