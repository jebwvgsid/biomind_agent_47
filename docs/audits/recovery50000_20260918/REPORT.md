# Recovery 50,000 与 GWAS/WGS 跨层修复回执

本轮落实 2026-09-18 任务书 P1–P6；依据历史记录做定点核对，未重新开展全仓审计。生产变更、测试和提交清单见同目录 `COMMITS.json`，逐项验收见 `EVIDENCE_MATRIX.tsv`，精确命令及边界见 `VALIDATION.md`。本报告不把软件验收写成原 GWAS/WGS 完成。

## 基线与累计源码

起始分支 `refactor/consultant-controller-authority`，HEAD `4cacc8f7ddd30dfe5334380c7fef52d859b76eae`。起始 index 为空，范围外原 dirty/untracked/删除状态保留。实际生产基线还依赖上轮 S1–S5 累计 overlay，不能仅 checkout 此提交复现。

可复现的源码链是 `/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_s1_s5_20260917/baseline_source/` 加其 `final_changes/`，本轮 `baseline_additions/config/config.py`，再加 `/public/home/liusiqi/03.毕设/biomind_audit_exports/recovery50000_20260918/final_changes/`。先前 926 个文件逐字节一致；补入本轮必要配置源码后基线为 927 个文件。最终 manifest 与保护检查回执放在本轮 export；未包含凭据文件。

每次提交用独立 index，仅提交“起始工作源码到已验证候选”的增量；原工作 overlay 留在工作树，未 `git add .`、reset 或 push。与本轮直接相关、起始未跟踪的 `tests/test_inspection_terminal_workflow.py` 按已验证完整测试文件纳入提交，起始 SHA 和采用范围有独立记录；未顺带收录其他未跟踪文件。提交对象与实际累计测试源码的差异有逐文件散列和 `preserved_overlay` 标记。`tests/test_recovery_schema2_node.py` 的原 overlay 独有测试方法中，本轮必要修改以 `OVERLAY_INCREMENT.json` 保存精确增量和前后 SHA；该增量连同原 overlay 构成已验证累计源码，没有把原 overlay 整段混入生产提交。

## P1：预算、完整重交和恢复

Recovery 默认 `BIOMIND_RECOVERY_OUTPUT_LIMIT_TOKENS=50000`，额外完整重交默认最多两次。请求值、有效上限、预留和 adapter 参数共用现有预算入口；继承客户端的 8,192/16,000 不再暗中截低显式 Recovery 上限。显式配置较小 provider 能力时记录 requested/effective、来源和调整原因。其他节点额度保持原配置。

实际已安装 SDK 会把 `max_tokens` 改名为 `max_completion_tokens`，并丢弃工具上下文中的原生 `reasoning_content`；本轮在已有 DeepSeek adapter 中窄范围修复。真实生产 factory/binding 和 SDK 离线组包证明最终 Chat Completions payload 为 `max_tokens=50000`，所需原生字段保留并计入完整输入。线上 endpoint 是否接受此上限仍为 **NOT_RUN / capability not verified**，不能把组包当成 HTTP 成功。

默认 200,000 窗口、4,096 估算裕量、2,048 提前余量下，safe input 为 145,904，压缩触发上限为 143,856。`118554 + 50000 + 4096 = 172650`，本例不因提高输出上限必然压缩。沿用已有分段、多层合并、CAS 激活及正式引用读取，不新建预算管理器。200,000 是单请求窗口，不能当累计调查额度。

仅 `length/max_tokens/max_output_tokens` 进入完整重交；原始响应先持久化为诊断，provider 历史仅加入合法的简短重交反馈，不塞入半工具消息、不拼接 JSON。完整 JSON 前缀即使看似可解析，只要最终为 length 或缺必要终态，仍零工具执行、零指令接纳。普通 schema 修正、网络重试、取消和过滤沿原路径处理。

累计重交计数、模型/工具进度和已读证据随当前因果调查 checkpoint；分页、工具调用、压缩、Node 重入和跨进程恢复不刷新额度。日志损坏、未配对工具边界及关键 checkpoint 写入失败均停止，不把缺失状态当新额度。已接纳且 source/packet 身份一致的指令复用原回执，不再调用模型或生成第二份接纳。请求绝对期限保持原配置，未改为随 token 延长。

## P2–P3：准确诊断与统一合同

公共修正诊断保留 validator 原 code/detail/source/field/允许形式；只有真实产物错误增加产物修正提示。实际 Generate 链已覆盖“不支持的 if 包装 helper → 原因进入下一次模型 payload → 直接 helper 调用 → 真实观察 → 正式 Commit”。普通合法复合 shell 不受这项特定 helper 证明规则限制。

两个 GWAS Skill 明确输入检查是观察效果，允许 `outputs=[]`；不制造 `00.input_check` 空目录。已经冻结的错误必需目录仍导致拒收，需要原计划修订与审批，未偷偷删除约束。最终三份报告要求保留。Q 行映射 unknown 仍阻止依赖该映射的科学动作。

Gate 消费冻结 Skill 的 `required_fields`，移除没有增加真实性保证的第二套 `passed/receipt` 要求；真实当前 attempt、来源、正式输入/产物关联及独立内容核验继续强制。伪造成功布尔值、错身份、错 M5 和错误关联仍拒收。

## P4–P5：字典和参考准备

字典按原始字节拆字段；记录结构、SN/LN/M5、顺序、完整性和唯一性严格校验，UR 单独诊断。新 v2 合同要求标准百分号编码的最终 `reference.fa` URI，预发布显式读取 staging FASTA，发布后独立复核。中文、空格、`%`、`#` 不靠手工替换。旧 v1 的窄 ASCII 元数据兼容不代表新合同合规；历史裸 UTF-8 UR 进入重生成，坏字节不忽略、不替换、不吞掉整条序列。

注册的 `prepare_reference.py` 接收正式路径/资源引用和已绑定的 BWA、samtools、Picard、Java；控制端提供 task/step/action/attempt 身份。实际 Picard 使用 `R=/O=/URI=`。逐命令保存 argv、stdout/stderr 和原始 rc，必需命令失败就不发布；Killed 没有 OOM 证据时不写成 OOM。

helper 在自身范围内复用 journal、锁和 fsync；正式 Plan 同时声明科学 bundle 和辅助工程证据目录。只复用来源、工具身份、哈希和独立内容均通过的组件。新 staging 整体验证后用同文件系统 no-replace rename 发布；未知归属、竞争发布或 symlink 均保护。发布后、Commit 前中断可重验且零重建，随后仍须正式 Supervisor/Gate/Commit。

真实小参考测试使用已安装四个工具，通过正式 Skill invocation、生产 runner、独立 parser、Gate 与 Commit；数据是独立的两 contig、384 bp 基础设施输入。审批/Reviewer 为明确替身。它不证明本轮自动发现新环境，也不证明原 WGS mapping/calling 成功。既有环境自动发现、binding 失效、安装授权/取消/目标匹配另用原生产链回归保护。未绕过 Inspector 隔离或修改 bwrap。

## P6：报告归属与终态

Consultant 提交正式 TaskSpec、进入 Planner 前，对明确报告目标只读预检。旧文件或无法核验归属时进入已有用户决策流程；不替用户换名、不写目录、不借此获得发布权限。真实最终 writer 仍复查身份、版本及并发竞争。

终态分别呈现当前原始核验问题、进程 rc、已 Commit 进度、Recovery 截断/停止、候选文件和用户报告交付。保留 parser receipt 与失败谓词，不再用“原因尚未定位”遮住已知字典/合同问题，也不拿已解决的 Picard 参数或历史 137 冒充当前原因。内部诊断归档成功和指定报告路径冲突可以同时成立。

## 两个原任务的事实与后续位置

GWAS `session_1789690459`：0 次科学执行、0 个 Commit，发生在候选 helper 验证及修正阶段；尚余修正额度时模型主动停止，未发生关联算法失败。Q 行映射仍 unknown。RAGFlow Key 缺失警告不是此次候选拒收的原因。本轮没有恢复该 session，也没有 p-value、Manhattan/QQ 或候选区间结果。

WGS `session_1789690744`：输入检查已 Commit；参考准备未 Commit。四次参考尝试依次涉及 Killed/旧目录/Picard 参数/最后核验，不能合并为找不到环境。现有候选位于 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789690744/plan_artifacts/reference_index`，对应 `prepare_reference`、`attempt_0a59aafa0e062c05def6`、`plan_b67d28df62a60eb74331` / `plan_v1`。

只读复核确认源和副本一致，2 contig、16,000,000 bases；FASTA、FAI、五个 BWA 组件及字典 SN/LN/M5 内容通过。完整 bundle 仍 **FAIL：REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED**。实际 UR 的 `d5 be` 可严格解码为 U+057E，路径成了 `03.վ` 且仍指向旧 staging；不是“所有非 ASCII 都是无效 UTF-8”。八文件的历史 attempt 归属记录、size/mtime/ctime/64 KiB 前缀哈希一致，历史没有全文件哈希；本轮另记录完整 SHA。未修改或重新归属这些文件，未补写 Commit。未来应按原审批/产物修改路径处理字典及证据合同，新 helper 不会擅自接管无它自身 journal 的旧目录。mapping/calling 未运行。

历史记录使用 openai 兼容层、Chat Completions、`https://api.deepseek.com`、请求 model `deepseek-v4-flash`，thinking disabled；响应实际 model 未记录，为 UNKNOWN。未更换模型或凭名称推断能力。最后 Recovery 原请求 0034 的本地完整估算 118,554，provider 输入 123,449，输出 8,192，finish 为 length；17 条总消息中 local 只有 3 条，其余包括 System、Seed、12 条继承材料。最大继承对象是完整批准 Plan（169,918 字符，内容参考计量 51,168 tokens），还带已编译数据流/绑定/回执，并非只有短 handoff。原正文 30,916 字符，在未闭合 analysis 段截断，没有工具提交。usage 没有正文/原生推理/工具参数 token 分项，分项为 **UNKNOWN**，不把 8,192 全称可见正文或推理。长回复只能确认尚停留在分析段、迟迟未形成正式提交；不能据此断言大部分 token 是原生 reasoning，或把未核实的复制行为当成根因。

0034 总耗时约 42.24 秒，首次可见内容约 6 秒，单请求 timeout 约 599.32 秒；未发现它触及绝对期限。4 个因果 episode 共 12 次 Recovery 调用、13 次检查/读取和 4 个提交调用；其中此前 3 个指令被接纳，另一次提交因 STAGE_FIELD_REQUIRED 拒绝。最后 episode 是 2 次模型、2 次工具。原调查总剩余配额/期限未记录，为 UNKNOWN；resource policy 当时 disabled，不能说累计 token 耗尽。各次重复输入 usage 总和也不是上下文窗口。

## 明确限制

真实 provider 短请求接受 50,000、真实小上限截断再提交以及 50,000 token 完整长响应均未运行：当前允许读取的配置和进程环境没有可用凭据，凭据文件受读取限制。未因此换模型或伪报能力。原 session、共享环境、注册表、bwrap 和旧报告未修改；未运行完整 GWAS/WGS；未 push。

helper 的工程 journal 不产生科学执行授权。现有通用目录 `output_write_guard` 跳过目录，不能将其说成“Commit 后目录防写保护”；已完成步骤的执行仍由正式当前步骤、Plan 审批和既有修改权限约束。历史候选的修复与原任务恢复需要沿原运行入口处理，本轮没有越过该边界。
