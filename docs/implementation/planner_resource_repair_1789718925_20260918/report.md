# Planner 资源声明修复回执

日期：2026-09-18。对象：`session_1789718925`。这是软件修复与离线回归记录，不是 WGS 分析完成报告。

## 完成结果与范围

当前工作区已修复新产物/工作区混淆的误导性反馈、资源用途逐条首错返回、成功候选重复编译资源绑定，以及规划停止显示和指定目录停止材料交付。四份归档候选按原样通过真实 Planner 源节点的请求、接纳、持久反馈和下一轮重入路径重放；完整修正版一次进入待审批状态。没有自动修改错误候选使其通过。

验证结果：**181 passed，6 subtests passed，1 个已复现的基线失败用例 deselected**；新增 **11** 项用例全部通过。当前工作区包含已有未提交修复；这不是干净 HEAD/全仓库测试通过声明。没有调用真实模型/API，没有运行 BWA、完整 WGS、安装软件或恢复原 session。没有 push。

## 基线、隔离提交与保留改动

- 起始 HEAD：`29c7b3ace09dcf6c62c647c6bcbb89273479265d`。
- 分支：`refactor/consultant-controller-authority`。
- 起始 index 为空；`git status --short` 记录 68 个修改项、36 个删除项、96 个未跟踪条目（含折叠目录，并非精确文件数）。完整原始状态及编辑前文件摘要见 [baseline.json](baseline.json)。

| 逻辑提交 | 完整 hash | 文件范围 |
| --- | --- | --- |
| 资源诊断与机械事实精简 | `7f11f3ce756ff9d6bfd8d717a12dd25340806687` | `agent_core/plan_bindings.py`；`agent_core/plan_contract.py` 的 5 行绑定复用 API；`bio_agents.py`；`prompts/manager.py`；`tests/test_planner_wgs_resource_repair.py` |
| 规划预算与停止交付 | `14a9e08ce7853e4807235cec478732c40c60e032` | `agent_core/planner_recovery.py`；`agent_core/cli_lifecycle.py`；`agent_core/workflow/inspection_stop.py`；`agent_core/workflow/managed_stop.py` |

`plan_contract.py` 中“身份诊断与资源诊断合并”的本次改动，直接依赖此前未提交的 `_collect_candidate_artifact_ref_keys` 和独立 action 诊断。其补丁无法独立应用到起始 HEAD；为避免夹带此前改动，该部分**已经实施、测试，仍保留在工作区，未纳入上述代码提交**。见 [overlapping_plan_contract.patch](overlapping_plan_contract.patch)，其基底是本次开始时的工作区加上已隔离的 5 行 API 修改。不能把它直接当作针对纯 HEAD 的补丁。

[task_only.patch](task_only.patch) 保存本次 8 个生产文件相对于编辑前工作区的完整差异。对另外 7 个生产文件，在临时副本中反向应用本次提交后，内容均与编辑前快照逐字节一致，证明没有把此前改动混入提交或丢弃；证据见 [receipt.json](receipt.json)。没有回滚、重置或清理既有 dirty/untracked 文件。

## 定点核对结果

项目根目录为 `/public/home/liusiqi/03.毕设`；代码目录为其下 `Biomind_v1`。原始五个科学输入的路径语义均保留，回归没有打开 FASTQ/FASTA 内容，也没有借用其他数据作为科学依据。所有可写测试状态和报告位于临时目录。

本次四轮在 Plan 阶段停止，实际 4 次 Planner 请求，0 次 Generate、0 次科学执行、0 步正式提交。响应完整，没有本次输出截断的证据。

归档所含 reseq 路由、mapping、GATK、bcftools 四份 Skill 内容哈希均与当前文档一致；本次没有修改这些 Skill。原交接仅选中路由与 mapping，GATK/bcftools 是已加载备选。完整修正版的离线交接明确选择 GATK，并同步正式已选 Skill 记录；直接只改单个选择列表会被已有交接一致性检查退回 Consultant，这个保护保留。

`genomics.wgs.short.read.mapping.prepare_reference` 的真实契约为：

- 新不可变产物接口：`reference_indexes`、`reference_preparation_evidence`。
- `workspace_mutations=[]`，没有要求已有可变工作区，也没有供 `resource_uses.role` 使用的工作区角色。
- 有固定的参考索引验证观察和建索引副作用。证据目录是新生成的正式工程证据产物，既不是框架隐藏日志，也不是已有工作区。
- mapping 主执行能力的 BAM/索引/指标产物同样通过 `outputs` 声明；创建时写入文件不改变接纳后的不可变产物类型。

第四稿两个 `REQUIRED_WORKSPACE_UNCOVERED` 均来自 `prepare_reference`：`plan.reference_bundle` 和 `plan.reference_prep_evidence` 被错误声明为工作区，并分别补了 `$.steps[2].step_effect.workspace_mutations[0/1]`。所选能力要求的 workspace role **不存在**。冲突链为：错误 mutate → 错误改型及状态声明 → 与能力明确的空 mutation 契约冲突 → outputs 被清空 → 工作区覆盖、产物效果和证据契约衍生失败。新反馈直接定位这两个主冲突，不再要求补工作区来修复它们。

## 最终声明方式与精简矩阵

| 信息 | 模型仍需表达 | 程序承担/保留的检查 |
| --- | --- | --- |
| 新文件或目录产物 | `outputs` 中的具体引用；新 plan-local 对象的语义与物化约束 | 已有新产物默认类型为 immutable；编译正式产物效果，不要求重复 mutate/state 声明 |
| 真实输入读取 | `resource_uses` 的 `read` 和明确引用 | 保留身份、生产者、先后依赖及可用状态检查 |
| 真实工作区修改 | 明确工作区、`mutate` 用途；必要的角色选择 | 保留 workspace 类型、能力范围、状态和证据核验 |
| 唯一能力拥有的转换 | 选择正确能力及资源角色 | 可省略重复 `workspace_mutations`；已有 StepEffect 编译器从该能力取得唯一转换；显式矛盾仍拒绝 |
| 开放通用能力的工作区转换 | 明确状态前后条件与受支持证据契约 | 缺少转换或证据不能获准；不能把所有工作区检查删掉 |
| 唯一 operation、固定观察/副作用 | 能力选择；有歧义时仍需业务选择 | 既有编译器已能补齐唯一事实。本次提示词去掉重复填写诱导，不任意取多个接口中的第一项 |
| ask_user | 未决问题及依赖关系 | 保留既有内建用户输入效果推导，无需伪造文件输出 |

实际退出或合并的路径：

1. `compile_plan_resource_bindings` 的 v3 `resource_uses` 首错返回改为跨步骤收集独立问题；每条保留 step、ref、字段位置、actual、接口/角色及来源。
2. 新产物的 mutate 冲突先结合能力和 outputs 定性，不再只反馈“改成 mutable_workspace”。未知 ref 暂停依赖其类型的判断，仍检查其他已知资源。无效输出角色不再派生额外工作区别名冲突。
3. `bio_agents.py` 删除 `validate_wire_and_bind` 成功后的第二次相同 `compile_plan_resource_bindings` 调用和无用导入；复用首次结果。分配与后续 StepEffect/数据流核对仍在原接纳入口内完成。
4. v3 已选角色明确拥有状态转换时，不再要求模型重复填写同一转换。需要自行声明的真实变更仍保留该要求。
5. Planner wire 主例删除四组重复效果占位字段；提示说明 outputs、目标目录探查和内容 READ 的区别。

**不适用/未虚构的删除**：当前代码没有“所有目录自动改成 mutable_workspace”的全局规则；不可变类型默认值、唯一 operation/固定效果推导、staging/materialization 都已存在。本次复用它们，没有新增平行资源 registry、第二份 TaskSpec、Shell 语义推断器或 Controller。

## 四稿前后对照

| 候选 | 修改前真实入口反馈 | 修改后原样重放 |
| --- | --- | --- |
| 1 | 只返回首个 `PLAN_RESOURCE_USE_ROLE_INVALID` | 同一轮返回 8 个无效角色位置和 18 个新产物 mutate 冲突，含索引、S1/S2 BAM 及其余受影响产物 |
| 2 | 只返回首个 `PLAN_RESOURCE_KIND_INVALID` | 18 个输出用途冲突一次给出；expected 指向能力输出接口/outputs，而非仅要求工作区改型 |
| 3 | 只返回首个 BAM 类型错误 | 18 个冲突一次给出；参考索引的错误类型及关联状态声明被定位 |
| 4 | 两个 workspace uncovered，加 StepEffect、未来读取及覆盖等共 10 项 | 两个参考产物的主冲突；不在这些无效前提上重复衍生 workspace/effect 错误 |

一次按反馈纠正资源声明后，真实剩余问题仍能被发现：`check_output_dir_conflicts` 把未来输出目录内容当 READ、`plan.s1_flagstat`/`plan.s2_flagstat` 缺生产者、`output.filtered_vcf` 缺覆盖。没有预设“资源错误消失就代表整份计划成功”。原第四稿详细 10 项和修改后的全部字段见 [replay.json](replay.json)。

每份候选仍只记一次请求/拒绝；4 份共 4 次，未提高上限。完整诊断进入下一轮真实请求；没有额外模型评审调用。

## 完整 WGS 计划及边界验证

[accepted_wgs_plan.json](accepted_wgs_plan.json) 保存修正版候选与正式编译步骤，真实入口一次接纳为 **pending approval**，没有批准执行。该修正版由离线 provider 送入，生产代码没有自动重写归档候选。

流程为输入核对 → 目录冲突检查/新参考索引 → read group 决策 → BWA mapping/去重 → 倍性决策 → GATK HaplotypeCaller gVCF + 联合分型 → 过滤决策 → GATK 过滤 → 统计和报告。bcftools 只作辅助 VCF 统计，GATK 是唯一 caller；没有同时执行替代调用链并混合结果。未选 caller 缺失不阻断独立 mapping；选中阶段依赖不足仍需 stage_blocked 并解释下游影响。

read group、倍性、过滤阈值没有被写为已确认参数；等待位于首次使用之前。正式 TaskSpec parameters 保持空，候选去除了未经授权的 PL/LB/PU 和硬编码过滤阈值。这些科学含义通过提示与明确计划验证，没有新建任意自然语言阈值扫描器；本次离线结果不保证未来真实模型总会一次生成相同正确计划。

回归还验证：有生产依赖的未来产物可规划；缺依赖、未知 ref 和外任务旧结果引用不能获准；目录探查不声明读取不存在的科学结果。真实工作区的两步状态修改通过实际 Planner 入口：缺转换先拒绝，补齐后接纳，仍要求先前 ready 状态。唯一能力转换可省略重复字段、显式矛盾不能静默改写的编译器回归同时通过。见 [workspace_entry.json](workspace_entry.json)。

## 停止显示与指定目录交付

新预算耗尽回执保留真实已用/上限和末次错误；CLI 显示本例 `4/4`，下一步指向 Plan 候选、资源冲突及规划反馈。缺少计数时显示未知，不生成确切数字，也不再显示无关的 Recovery 累计未知次数。

规划停止 writer 仅从用户明确授权的正式 TaskSpec 输出位置解析 report.md、stage_status.tsv、reproduce.sh，不借用被拒绝候选路径授权。保留原 publisher 的文件归属、已有文件保护、输入重叠与路径保护。无执行的 reproduce.sh 明确写 planning 未接纳/科学命令未运行，正文只有退出语句，不生成可冒充已执行 WGS 的命令。

只读核对指定 `.../BM-REMAIN-WGS-005/medium`：三份文件均已存在，归属其他任务，不能计为本 session 交付。本次没有覆盖这些文件，其 SHA-256 与修改前一致，详见 [delivery.json](delivery.json)。没有恢复原会话或往真实科学输出目录补写测试报告。

离线真实停止 writer 的两个分支已覆盖：空授权目录三份停止材料创建成功；存在他任务文件时保持原样，对外交付标 conflict、内部诊断另记 complete。内部 complete 不代表指定目录完成，也不代表 VCF/科学结果完成。成功计划的所有输出由计划生产者覆盖；已有执行/部分完成停止路径通过相关回归保留进度事实。本次没有证明科学任务成功。

## 实际测试与限制

- 核心接纳、11 项新回归、协议例、workspace attestation、引用/身份、Generate 开关和脚本载体、CLI public response：**162 passed，6 subtests passed**，见 [core_tests.log](core_tests.log)。
- 既有执行/部分完成终态、总结与报告故障路径：**19 passed，1 deselected**，见 [common_tests.log](common_tests.log)。
- deselected 的 `test_secondary_faults_preserve_outer_base[writer_one]` 在编辑前代码快照上也失败：旧测试的单 writer 故障注入未能阻止 report 路径出现；未为通过而放松保护，见 [writer_baseline_test.log](writer_baseline_test.log)。
- 另测旧 SnpEff 资源两组：修改前、修改后均 **18 failed，16 passed**。旧 fixture 使用当前已无的能力及旧周期/取消预期；不是本次新增失败，详见 [legacy_baseline_tests.log](legacy_baseline_tests.log)。最初未提供该旧组要求的 `BIOMIND_RESOURCE_AUDIT_RESULTS`，补齐后才进行有效前后比较。
- 一次合并重跑以 143 退出，未留下完整结论；未将其记作通过。拆分上述两个集合重新完成并以最终日志/JUnit 计数。
- 所有运行均使用 biomind 环境，`BIOMIND_DISABLE_DOTENV=1`，未读取密钥文件。旧代码对照通过临时 import loader 使用编辑前源码，保留原文件路径语义，没有替换当前工作区代码。

先前“333 项通过”包含 Planner 提示/解析及输出、epoch 等边界（如 `test_cli_public_response`、`test_generate_only_output_contract_v2`），不能说完全没有 Planner。但它不等于本次四稿和完整 WGS 的真实接纳覆盖。Generate 开关相关测试覆盖开/关分支，不能把 333 项推断为全在 strict 模式运行。本次保持用户当前 Generate 外包装/helper 检查默认关闭，未扩大成跳过 Plan 接纳。

未运行：真实模型/API、BWA、参考索引构建、VCF calling/filtering、完整 WGS、安装、生产恢复、push、全仓库测试。这里的 pending approval 和离线 writer 成功只证明对应框架行为。
