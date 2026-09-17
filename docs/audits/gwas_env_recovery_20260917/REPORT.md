# GWAS 故障增量审计报告

目标 `session_1789648505`；历史对照 `session_1789628497`。任务日期 2026-09-17。本轮完成审计、隔离现状见证和详细修改计划，**未实施生产修复，未恢复原任务**。

## 1. 已证实的直接故障链

8 步计划获批后，第一步 `check_inputs` 执行原 Bash 初检脚本。进程的 PATH 查不到 bcftools，脚本仍创建空 `vcf.samples.txt`；末尾只打印空文件告警，以 **rc=0** 退出。正式产物核验正确拒收该必需样本文件，具体码为 **OBSERVED_ARTIFACT_CONTENT_INVALID**，步骤 **0 Commit**。随后独立 Recovery 在固定 seed 准入处遇到 **32,097 字符 > 32,000 字符**，在首次模型/工具调用前停止；三份停止报告最终真实交付。关联、绘图和候选区间没有执行。

```text
PATH 未解析 bcftools → 脚本写空必需样本列表 → 进程退出 0
  → 正式核验拒收 → 当前步骤未 Commit
  → Recovery 固定 seed 字符门拒收 → 任务失败
  → 停止报告交付 complete（不代表科学完成）
```

原始执行证据仍记载 `VALIDATION_FAILED` 和具体 `failure_code`，不是全部证据消失。`TypedExecute.run()` 先用“launched command failed”概括 rc0/核验失败；`recovery_projection._failure_identity():1228` 又只查 `error_code`，漏掉已有 `failure_code` 而回退 `EXECUTION_FAILED`，最终产生“脚本执行失败，退出码 0”。`FailurePacket.producer_failure_index` 仍保留核验拒收与目标指针。原始起因和容量停止在持久记录中分别保留，主因映射与显示有误。[精确因果、原文件与真实函数见证](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/notes/failure_delivery.md)

Recovery 本次调查的模型轮次/工具调用为 0；其后另有一次 `terminal_summary` 模型解释请求，不能说整个 session 没有模型调用。attempt 的 `EVIDENCE_COMMITTED` 只表示执行证据已持久化，不等于正式步骤 Commit。

## 2. 版本比较和归因

修改前 A 是 HEAD `73e1f8fbc585c695530fba01d4e5394b99b4cbcc` 加当时实际 dirty/未跟踪源码的可靠快照。交付 B 包含主修复 `2e70f28658b8aee0d7cd9451f7f353f4b4121825`、补充 `d2a35c4834c15373841c5e2969281c11c4db6da7` 和保护 overlay。本轮工作树 C 的 HEAD 是 `82c27867d862ef32d657fbc6162c70d4bc06492c`；对上次最终清单逐项复核，**905/905 文件哈希一致**，旧快照哈希也全部吻合。

原进程 20:34 启动，晚于 20:08 交付；记录行为与当前源码一致。不过原进程没有完整导入源码哈希，不能把 C 的文件身份当成已证明的原进程全部源码身份。比较采用实际快照和同条件分支见证，没有切换工作树历史版本。[版本及 overlay 方法](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/notes/versions.md)

| 归因 | 本轮结论 |
|---|---|
| 既有缺陷 | `use_cache=False` 顺带跳过候选发现；新 runtime 检查落在重建 seed，未追加到实际 Consultant 请求；32k 固定 seed 门抢先终止；核验错误主因映射与 CLI 失真。旧/新快照同条件见证支持这些归因。 |
| 本轮引入的功能回归 | 旧 Installer 的自动候选搜索入口删除，当前明确目标入口和“用户提供已有环境”没有等价接管。旧 partial 原语句会提出注册候选、当前同条件仍取声明默认目标。**本次 GWAS 未进入 Installer，该回归不是此次直接原因。** |
| 已修复但覆盖有限 | 上次已完成 WGS 固定工具探测、binding/安装恢复和部分终态传播；GWAS 的整体环境声明、通用初检工具选择和 runtime 新事实接线仍未贯通。仅 WGS/tiny graph 通过不能证明通用链完成。 |
| 当前已修复/已有有效能力 | 成功 Skill 登记优先级、明确安装目标/授权/取消、binding 回滚失效及 helper 保护应保留；现有完整请求计量和分段压缩确能拦截首轮/后续超预算；空样本拒收和三报告发布成立。 |
| 证据不足 | 原进程完整源码身份、范围外工具/所有 R 包情况、Q 样本顺序、厂商实际 token 上限及当前原报告 writer 重入。具体限制不能扩写成“全机缺工具”或“环境被删除”。 |

具体模型的表头/Q 错误已由现场证实，但没有同条件旧模型重放，不按发生时间认定为 binding 提交导致的回归。

## 3. 环境发现、模型观察和实际动作

`.env` 路径确已配置：软件根 `/public/home/liusiqi/03.毕设/agent_shim`、Conda 根 `/public/home/liusiqi/anaconda3`、源码安装根 `/public/home/liusiqi/03.毕设/source_tools`。软件注册表和 Skill runtime 登记是不同层。`popgen_gwas/popgen_gwas_r` 是验证前自动创建的 registered 记录，配置根下相应目录不存在；四个 GWAS Skill 的登记为 broken。**registered 不是验证可用。**

链路断点是：默认环境检查失败 → `use_cache=False` 分支直接返回 → 已有 PM 候选函数未执行。PM 候选仅给单个最高分 exact/partial，不组合环境；独立 tools 表的精确工具名查找也不等于遍历各环境的软件。之后后台检查结果写到重建 seed，但 phase 保留首 seed；0002–0005 四份实际 Consultant 请求都没有 runtime_checks/失败事实/候选结果，仅从 Skill 文档见到环境名。后续只是“Skill 选择已接受”。[逐函数数据流和真实请求摘要](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/notes/environment.md)

本轮限定索引核查覆盖 44 个 prefix，来自 registry、配置根直接 env 子目录及 Conda 正式前缀索引，并检查登记 source/shim。后两者中的外部 prefix 不等于生产 Inspector/Installer 已支持任意位置；没有全盘递归搜索。

| 所需阶段/工具 | 本轮取得的事实 | 仍未证明 |
|---|---|---|
| 输入读取 bcftools | 8 处候选；bio_gatk 的 bcftools 1.24 / htslib 1.23.1 版本及正式 VCF `query -l` 成功，215 样本 | 其他候选的运行结果、完整变异 QC |
| 转换 PLINK/PLINK2 | 所查索引精确入口未命中 | 不能判全服务器不存在 |
| EMMAX/kinship | 所查索引精确入口未命中 | 没有可用关联工作流证明 |
| Rscript / CMplot / qqman | 12 处 Rscript 候选；所测 3 个环境能运行 R，但两包 requireNamespace 均 FALSE | 其他 9 个 R 环境未测，未画图 |
| bedtools | hicexplore 的版本入口成功，v2.31.1 | 实际候选区间/GFF3 处理 |
| LDBlockShow / ShowLDSVG | 所查索引未命中 | 其他来源及科学操作未验证 |

现有机制已有一条可运行的明确动作分支：真实 PM 找到并验证 bio_gatk/bcftools → 现有 generic resolver 接受空 binding → 在隔离候选中明确绝对工具路径 → 审批身份校验 → 真实 `run_script_path` 只读查询 VCF，rc0/215样本，注册表字节不变。**候选到脚本的选择由审计显式接线**，没有声称自动 Consultant、真实人工审批、Plan/Commit 全链已通过。该见证证明无需机械禁止 `bindings=[]`，也证明未来环境发现必须确实到达候选和进程。

旧 WGS session 用 `shutil.which` 找到 samtools/BWA、没找到 Picard，其 binding 也为空；绝对 samtools 路径未记录，不能由今天的 dorado shim 反推历史。旧的部分 PATH 成功不是注册发现链已完整工作的证据。

## 4. 输入解释与 Planner

只读 VCF header 与表型全文确认：二者均为215个不同样本，ID 集合和顺序完全相同。表型没有表头，原脚本 `NR>1` 丢掉首样本；Q 为215×3纯数值，没有 ID，原脚本第一列提取出的是数值。**Q 顺序仍未知，关联不得继续。**没有把邻近 `.fam`、其他表型、旧图或隐藏答案用作映射依据。

误读来源分层明确：Consult handoff 和 Plan 先假定“提取 Q ID”，同时把 Q 列语义留待更晚询问；实际 Generate 请求没有数据行观察，但已经含有 Skill 的“Q 无样本列不能安全排序”警告；生成脚本仍先跳首行/取首列，再读取 head 摘要。现有 `sort_phenotype.read_table()` 能保留本例215行，未被本步使用；它对原始数值 Q 和未匹配后填 NA 的行为也需要有来源的前置检查，不能当万能修复。

隔离原脚本仅替换输出目录，PATH 放真实标准工具并排除科学工具，重放得到 rc0、0字节 VCF样本、214行表型、215行 Q；四份检查文件哈希与现场一致。另用 Python gzip 只读 header 和真实 bcftools 查询得到相同样本列表哈希，证明有轻量读取办法，但没有改原动作。正式 validator 拒收合理；契约名字为 `file_exists`、format=txt，不能误称专门 nonempty 合同，也不能通过放宽空文件豁免过关。[输入与 Planner 证据及方法](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/notes/input_planner.md)

第一轮 Planner 实际有12项拒收：1个 ask_user 无效果、7个模型声明的必需目录无 producer、4个 capability ID 非规范；第二轮仅1项不受支持的裸副作用名。实际提示已要求 typed evidence_contract，第三轮修正后接纳。真实效果编译器复现三轮边界；没有证据表明它强迫每个只读检查产文件，保留合理 validator。这两轮不是本次执行后停止原因。

## 5. Recovery：32k seed 与完整 token 请求不是同一个对象

原统计方法及本轮独立重建均得到如下固定 seed（JSON canonical，Unicode 字符）：

| 字段 | 字符 |
|---|---:|
| inspection_manifest | 17,250 |
| failure_packet | 10,240 |
| task_semantic_slice | 2,626 |
| current_step | 761 |
| selected_skill_manifest | 712 |
| 其余字段与 JSON 结构 | 508 |
| 总计 | **32,097** |

前两项占85.6%；27个引用身份不同，不能视为27份重复证据直接删除。字段精简需区分 provider 投影与原始 descriptor；不能将原 descriptor 的重复字段尺寸直接算进 17,250。完整 TaskSpec/handoff、Plan 等另行继承，不包含在此32,097内。

在隔离内存中仅越过字符门，使用真实组装器/loop 和假 provider 响应，捕获首次完整请求：16 messages 的 canonical JSON 共270,965字符，另有11个工具定义共5,382字符。生产本地 fallback `mixed_script_reference_fallback / ascii3-cjk1.25-other2-v1` 的计量如下，**不是厂商 tokenizer 精确实测**：

| 完整请求组件 | 本地估算 tokens |
|---|---:|
| 独立 System | 3,782 |
| 固定输入（含 seed 包装） | 12,064 |
| 允许继承材料 | 77,117 |
| 工具定义 | 1,794 |
| 空局部/待返回/包装容器计量合计 | 3 |
| 合计 | **94,760** |

配置上下文总容量200,000；预留输出8,192、估算余量4,096，安全输入187,712；压缩触发170,000、目标130,000、额外 headroom2,048参与触发上限。表内总量剩余安全输入92,952；估算器另报不确定性23,883，这是启发式遥测，不能当厂商误差保证。模型记录为 deepseek-v4-flash；应用配置不证明厂商实际支持相同上限。[完整计量、请求捕获与重建](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/notes/recovery_budget.md)

实际 Node 先构造 seed/inspection registry、读取继承材料并尝试恢复 context epoch；构造 loop 时已绑定11个工具定义。随后 loop.ainvoke 才对 provider seed 执行字符门；通过后装配 loop 消息，进入 call_model 的完整 token 预算检查，必要时返回 Node 做归档/压缩维护，再调用 provider。此次在字符门退出，尚未抵达完整预算和维护。

本轮还验证了现有能力与直接相关缺口：

- **已有能力应复用**：小 seed/大首次继承材料和后续增长都在 provider 前计量；现有 compactor 可将大材料按 source ranges 分段、校验 coverage、重新计量，且有有限维护额度/失败停止。无需重建全局压缩系统。
- **输出预留不同**：预算核心预留8,192，而 `_ainvoke_llm` 的 compactor adapter 在未设置上限时可绑定16,000；隔离分段示例输入187,086加允许输出16,000为203,086，超应用200,000。是潜在边界缺口，本次实际没有 compactor 调用。
- **外置结果无法正式取回**：巨大工具结果的 phase archive 跨进程仍可从 archive store读到，但正式 `evidence.read/step.inspect` 均返回 `INSPECTION_REF_NOT_DECLARED`。不能只把正文移走而不给合法读取入口。
- **引用持久性有区分**：11个 inspection对象中8个可在新进程按原身份/哈希读回；3个原本无正文，不能将其当现成可读证据。错 attempt、revision、tool权限或 digest均被拒，保留这些保护。其余16个引用按各自 artifact/Skill/failure/opaque 权限分类，详见引用矩阵。

最低固定事实应保留 task/Plan/step/attempt/批准范围、实际 rc0、空样本和核验拒收、未 Commit、已采取动作/未解问题及独立停止原因。Q未知不能压缩成样本全部对齐。长列表用既有持久授权引用；固定/首次/后续/压缩自身都要准确计量和有限停止。

## 6. 接下来如何实施

[详细修改计划](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/IMPLEMENTATION_PLAN.md) 给出五个切片的真实文件/函数、接线、现有机制复用、兼容、前置、验收、回滚和独立提交范围：

1. S5 修正主因与停止/交付显示；S4 修复 Recovery 字符门、正式引用读取和一致预算。二者可并行，`recovery_node.py` 串行集成。
2. S1 统一候选发现与按阶段验证，包括安全接回 Installer 候选；S2 通过现有事件追加接口把证据送到实际请求、所选动作和 runner。共享 bio_agents.py 由一名集成人合入。
3. S3 让初检先观察结构，再对齐或保留 Q阻塞；复用 helper和StepEffect，保持空必需样本拒收。

不恢复旧 Installer 自动推进科学步骤，不强制所有通用动作绑定环境或写文件，不新增平行 registry/controller，不修改旧批准快照或执行后静默切换工具。上次已实施 binding回滚/安装授权/取消/目标匹配/helper/Plan/CLI保护继续回归。

## 7. 验证、交付和仍缺的证据

[EVIDENCE_MATRIX.tsv](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/EVIDENCE_MATRIX.tsv) 逐项区分现场事实、前后快照分支、真实受限进程和离线替身。[验证入口与结果](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/VALIDATION.md) 列出精确命令和未运行项。统计和重建脚本均已落在本审计目录，核心证据不只存于 `/tmp`。

本轮得到真实只读 bcftools 进程、完整请求离线捕获、引用跨进程读取、正式核验/typed/因果/CLI重放，以及前后版本比较；它们不等于真实模型恢复、安装、原任务恢复或科学 E2E。主机受控 bwrap 检查此前受限，本轮没有修主机或绕过生产保护；环境微小见证经明确候选直接调用，不能代替自动受控发现主链。

三报告当前字节与持久 published/frozen正文匹配；设备号当前49、历史45不一致，原因未证实，因此**原 writer 当前重新进入是否通过仍未知**。这些限制不妨碍制定上述软件切片；Q来源未知会阻止未来关联分析，厂商容量和真实模型行为需要后续独立验证。

本轮安全提交范围仅 `docs/audits/gwas_env_recovery_20260917/`。生产及既有测试、目标session、注册表和三份原报告通过保护哈希复核；既有 staged/dirty/untracked 不混入审计提交。完成状态为：**审计已完成、详细修改计划已形成；生产修改尚未开始。**
