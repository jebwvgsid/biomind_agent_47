# session_1789312388 跨层故障链审计

审计时间：2026-09-13 至 2026-09-14（Asia/Shanghai）。**AUDIT STATUS: COMPLETE，限本规格的静态证据与有界离线复现；PRODUCTION FIX NOT IMPLEMENTED。** witness PASS 表示当前缺陷被稳定复现，不表示生产修复通过。本轮没有修改生产代码、正式 prompt 或既有测试，没有恢复原任务、访问原数据库、原始 FASTQ/FASTA、秘密配置或启动脚本，没有真实模型/API/科学工具调用，没有覆盖现场报告。

## 结论

这是四层不同的问题，不能合并为“缺 Picard”或“重试太少”：

1. **原始触发条件**：批准脚本在其实际 Bash 进程中找不到独立 `picard` 命令入口，进入依赖缺失分支。只能证明这个入口不可解析；不能证明 Picard jar、GATK 的字典能力或其他环境不可用。
2. **失败分支的新错误**：首次 JSON 构造表达式少一个 `}`，先发生 SyntaxError，随后空参数引发 JSONDecodeError。重试修好了表达式，但统一 publisher 要求同时提供全部 `produces_artifacts` 身份与实际路径，拒绝了仅含 blocked side-effect 的事实，仍未发射任何结构化证据。
3. **最后停止**：一次完整但格式错误的 Generate 响应用掉 representation 修正的唯一额度；后一次响应以 `finish_reason=length`、8192 output tokens 结束，累计成为 ordinal=2/limit=1，未审批、未执行就停止。此 limit 来自生产代码分支，持久通用候选上限实际上是 4。
4. **收尾附加错误**：终态没有沿已消费的 Recovery 回执投影旧执行，混用了旧失败与新候选的 scope；随后执行详情比较得到 scope mismatch。三份指定报告已有受保护文件，当前写入没有可验证的同源同内容 receipt，全部拒写。

独立旁支：预检首次语义回复为合法 JSON，但每条 `issue_dispositions` 多出 `confidence`，严格 schema 拒收；重评后原执行提交一次。诊断工具读取的是**索引集合目录**，不是损坏的 FASTA，也不是误读日志。正式输入恰好五项；“6 个”是 Plan 标题错误。倍性、物种/组装没有明确确认记录，calling 尚未执行。

## 基线、现场与证据口径

- BASE HEAD：`60db1e4b8961d9eed7d36c312e79a75885e33dfa`；分支 `refactor/consultant-controller-authority`；提交树 `e3def852be4cdca4bb328a1ddeea85f412d596a5`。FINAL HEAD 是本目录文档提交，完整 hash 见本次交付回复；没有回退或 push。
- 开工 index 为空。已有 437 条状态：36 modified、33 deleted、368 untracked；都属于原现场，未收编。五个已知相交 overlay 为 `prompts/manager.py`、`agent_core/node_context.py`、`agent_core/planner_context.py`、`agent_core/provider_message_safety.py`、`agent_core/workflow/managed_stop.py`。离线导出记录这五个原字节，其余生产文件取 BASE 提交树。`verification.json` 记录检查前后的保护与测试版本。
- 原 session 的 runtime identity 确实记录 disk HEAD 为本 BASE，但 **loaded_revision=UNKNOWN**。安全 identity 对象 `state_contract/objects/f3959988aa4ffb0c226a096a1b0db8692cc253c7912a55074296a576b11a56b4.json` 的 `bio_agents.py`、publisher、managed_stop、supervisor、prompt 源码文本 hash 与当前文件相同，且有已加载 callable 的独立 digest。源码快照相同不自动证明整个进程的 Git revision；本轮没有补造这一结论。
- 上述 identity 没有外部 agent_shim 的源码或加载回执；其历史字节来源仍为 UNKNOWN。本次离线验证使用显式安全导出的仓库源码和已登记 Skill 资源，没有将外部 shim 当作已核实的部署层。
- 三个 active Skill 的冻结文本与当前对应文本逐字 hash 相同：reseq-variant-workflow 1.0.0、wgs-short-read-mapping 1.1.1、wgs-gatk-snp-indel 1.1.0。bcftools Skill 仅 candidate，未变为 active；计划内 `bcftools stats` 的用途是统计，不能据此认定换成另一套 calling。
- 本目录 `evidence.json` 是定点投影：原始源路径、字节数/hash、JSON pointer 所需字段、回执、stderr、模型回复长度/hash/协议形状；不复制模型未分流长正文或私有 reasoning。`project_evidence.py` 只使用标准库静态读取，禁止业务恢复。没有把原脚本作为可执行 fixture。

本文中 **S** = `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789312388`；**R** = `/public/home/liusiqi/03.毕设/Biomind_v1`；**D** = `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-WGS-005/medium`。精确路径/hash 见 [evidence.json](evidence.json)，结论与源码位置见 [findings.tsv](findings.tsv)。时间表使用 UTC+8；events.jsonl 中为 UTC，不能直接按字符串混排。

证据分级：矩阵事实行是 VERIFIED_FACT；原终端提示仅是 TERMINAL_FACT，Recovery 自然语言属于 MODEL_DIAGNOSIS，未直接用作根因或复制为补丁。风险与共享调用方推论属于 ENGINEERING_JUDGMENT，三个修复切片与 future_fix_acceptance 列属于 PROPOSED_FIX；材料不足处明确 UNKNOWN。

## 实际时序与去重计数

业务 Execute dispatch **3 次**（三个不同 attempt / 三条 execution_records）；成功 1、失败 2。输入检查另有 **5 个 reader 子进程**的 progress/pid 回执。这不是全 session 的 OS fork 总数：shell wrapper、Consultant 环境检查等不等于业务 Execute，已有材料不能提供全进程树总计。

模型上下文目录共 **22 个 call_id**：router 1、Consultant 4、Planner 3、Generate **5**、Recovery **6**、Reviewer 2、terminal summary 1。run_metrics 的 llm=21 未包括最后 summary。Recovery 节点 **3 次**，接纳 `revalidate` **1**、`retry_step` **2**；Recovery 本地工具 **3 次**（failure.inspect 两次、artifact.inspect 一次失败）。Generate 候选拒收 **2 次**、局部修正调用 **1 次**；不是两次额外科学执行。确认回执 **4 次人工、0 次自动**：Plan 1、脚本 3。

| UTC+8 / 阶段 | 实际事件与身份 | 原始证据 |
|---|---|---|
| 23:14:01–23:15:22 Planner | 初稿与第二稿分别发生 filtered_snp_vcf、variant_stats 的 `TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL`；第三稿 13 步通过，plan_9065d92f0857f119ece4 / plan_v1 | S/context_trace/planner_initial-0007-*/request.json messages[6]；0008 request messages[8]；S/plan_state.json approval_record |
| 23:16:15 Generate 0009 → 23:16:30–41 Execute | action_11c7864c3923d7b8；attempt_41188cceef15bbdcaad7；rc=0；五项有界观察通过，非全文件验证 | executions/execution_records.jsonl 第 1 行；attempts/*/exit_status.json；preflight observation |
| 23:16:44 Reviewer 0010 | JSON 完整，issue row 多 `confidence`；gate passed=true，但 effective_disposition=assessment_pending | supervisor_reviewer-0010-*/response.json；causal packet `4a8572…` source.payload.completion_report |
| 23:16:54–23:17:22 Recovery 0011/12/13 | failure.inspect；一次缺 schema_version 的 native 提案拒收，精确反馈后接纳 revalidate | events.jsonl 第 73、82–93 行附近；typed_recovery_receipts/recovery-directive-fb4738e74e977f7609bda17c-01 |
| 23:17:24 Reviewer 0014 → CommitStep | 同一 attempt 重评有效；`commit-step-8dfad1824858d03445200781`，仅提交 01.input_check 一次 | S/plan_state.json task_state.typed_step_commit_receipts；accepted refs 含原 attempt |
| 23:17:35 Generate 0015 → 23:18:31–32 Execute | action_d7c9d8045bdabc17；attempt_d9036d7cbe68e8f79b4f；rc=1 | executions 第 2 行；approved.sh:67–74、stderr.log |
| 23:18:36–57 Recovery 0016/17 | failure-db51437c6892ab9db94b6a4b；failure.inspect 与 artifact.inspect；后者目录格式不支持；接纳 retry_step | events 第 101–115 行；typed_recovery_receipts/recovery-db51437c6892ab9db94b6a4b-01 |
| 23:19:04 Generate 0018 | 完整 JSON `{script: …}`，无支持的 executable block；候选拒收 event `891a1a…`，ordinal=1/limit=1 | response.json；candidate_repair_state.events |
| 23:19:23 Generate 0019 → 23:21:01–03 Execute | 格式纠正为单 shell；recovery_action_11bfb7221b1f8959；attempt_d957b4b5226dd056dac8；rc=1 | executions 第 3 行；generated.revision-1.sh、approved.sh:73–75、stderr.log |
| 23:21:07–37 Recovery 0020 | failure-bdad478e902fd428e35bfcd2；接纳 retry_step；实际新 Generate messages 含两个旧 attempt 与身份错误事实 | events 第 116–125 行；generate-0021 request；receipt directive-bdad478e902fd428e35bfcd2-01 |
| 23:21:44–23:22:21 Generate 0021 | finish_reason=length；8192 output tokens；30796 字符/31336 UTF-8 bytes；无已闭合 shell 结束标签；候选未审批执行 | manifest annotations.provider_attempt；run_metrics llm_f1fcafdf5358；response 只保留形状/hash |
| 23:22:24–31 Stop / report | event `da15b2…` ordinal=2/limit=1；END；summary provider 正常结束，但 execution identity 空、详情 scope mismatch，三报告拒写 | task_state.managed_run_stop；terminal_summary-0022 request；report_receipts |

批准与执行正文没有被静默互换。三份批准脚本 SHA256：

| attempt | generated = approved SHA256 | executed wrapper SHA256 |
|---|---|---|
| 41188cceef15bbdcaad7 | 710e58922314a173a34e56b17afcf6989a11101aaed333c7ea7706b3f27bcf68 | c75d68b2d0f8b8fd935ed135b7384fe1861264217037884e18d52b3d8155f1e7 |
| d9036d7cbe68e8f79b4f | 4020460aa0a8abad65e34253ad2812eba0200aae94c092b80680af12636989a9 | 09c8c0e8c3fb14e0c48cdfabe77717d1f86fdc892a5d7caa4049a6044b0b196f |
| d957b4b5226dd056dac8 | f8129c330d03271b3ade8c0b0637477f170cc3f49d959f03aa20610d78049561 | 73de814776ea32cb824449bf2d86761b1a4b09bd66a5fca8eee14ea1cf2ed95e |

wrapper 与正文 hash 理应不同：wrapper 第 6 行执行 `/usr/bin/bash -o pipefail '…/approved.sh'` 并写独立 stderr/exit receipt。实际 action_hash、pid/pgid 和审批 identity 已投影到 evidence.json；未读取运行环境 manifest。最后候选没有批准或 execution wrapper。

## A–G 定点结论

### A. 命令入口与能力

**VERIFIED_FACT**：两个批准脚本都先创建 INDEX_BUNDLE 目录，再 `command -v bwa/samtools/picard`；stderr 只列 picard。成功分支明确是 `samtools faidx`、`bwa index`、`picard CreateSequenceDictionary`，没有在本步骤调用 MarkDuplicates/sambamba markdup。因此这里的 Picard 检查确实对应字典创建，不能把这次故障说成“误检查下一步去重依赖”。依赖缺失时连可以独立完成的 faidx/BWA 工作也没有启动，这是整步 all-or-nothing 脚本设计。

| step / capability | 选定命令 | 证据与可替代边界 |
|---|---|---|
| 02 / FASTA 随机访问索引 | samtools faidx | command-v 通过；本次未调用。真实子命令/版本能力 UNKNOWN |
| 02 / BWA reference index | bwa index | command-v 通过；本次未调用。不是已生成科学索引 |
| 02 / sequence dictionary | picard CreateSequenceDictionary | 独立入口找不到；jar、GATK 子命令、Java/其他环境能力 UNKNOWN |
| 03/04 / BAM 去重 | 后续 mapping 才需要确定实现 | 没有执行；sambamba markdup 不是字典替代 |

actual wrapper 未激活 bio_bwa 环境；`expected_runtime_contract` 虽为 passed，其 bindings/executables/versions 全空，不构成科学命令能力检查通过。既有 Consultant runtime_evidence 只证明一个环境 prefix AVAILABLE，另两条 bio_gatk prefix 检查 UNKNOWN；不是 Picard 子命令快照。此处没有新的环境探针，也未读取 PATH 值。

**PROPOSED_FIX**：由现有 Generate 明确选择已核验且兼容的字典实现，按现有 Skill/环境绑定与审批重新提交执行定义；把依赖限定到实际阶段。GATK/Picard 支持创建字典、samtools 有 dict 子命令，只是一般能力依据，不证明本机可用。[GATK 工具文档](https://gatk.broadinstitute.org/hc/en-us/articles/360037422891-CreateSequenceDictionary-Picard)、[samtools dict](https://www.htslib.org/doc/samtools-dict.html)；[sambamba markdup](https://lomereiter.github.io/sambamba/docs/sambamba-markdup.html) 说明的是 BAM 去重。没有擅自安装或自动替换。

### B/C. blocked publisher 矛盾与脚本检查边界

`plan-artifact-b4a2996a00517f619eb4fb51` 是 Plan 声明的 required、model_selectable、plan_local **目录**，语义为 reference_index_bundle（BWA 索引、faidx、字典集合），不是一个 JSON manifest，也不是已验证科学索引。空目录的存在不能代表该语义完成。

第二次真实 supplied facts 等价于：

```json
{"explicit_side_effects":{"create_reference_indexes":{
  "reference_resource_ref":"<本次批准的参考路径>",
  "index_artifacts":[],"status":"blocked","missing_commands":["picard"]
}}}
```

没有完整 payload 行产生：publisher 在 `$.facts.produces_artifacts` 的 exact set 比较就抛出 `actual=[] / expected=[plan-artifact-b4a2996a00517f619eb4fb51]`，两次 parser emission_count 都是 0。第一次还没到这一比较，表达式的缺 `}` 导致空字符串进入 json.loads。外层 `bash -n` 两版均合法；`execution/script_validation.py:10` 对 Bash 只做 Bash 语法检查，没有编译内嵌 Python。当前 serializer 模板确实已经来自同一个生产函数，问题不是“完全没有 helper”，而是每个分支仍手拼参数 JSON，且 helper 对 blocked 没有适用的部分事实入口。

**VERIFIED_FACT / 反例**：

- 只给 side-effect → `EFFECT_FACT_IDENTITIES_MISMATCH`。
- 补产物 identity/status=blocked、不给 path → `EFFECT_FACT_FIELDS_MISSING`。
- 给真实空目录 path、两组 status=blocked → 可以序列化/解析，但 matcher 把 blocked 列为失败状态，**不会完成**。这不等于必须伪装成成功才能保存；当前接口能够记录“真实存在但 blocked 的目录”，却不能方便表达完全没产物的受阻尝试。
- 直接按已批准身份发射仅 side-effect 的 canonical JSON，parser 能保存，matcher 仍不通过。说明拒收首先在 serializer 的全量义务，不能以此删除 parser/gate 身份核验。
- `index_artifacts=[]` 作为“没有生成任何索引”的事实有明确语义，已满足“字段存在”要求；它不满足科学索引成功条件。上游声明保留 planned，Execute 的实际产物与验证记录必须独立。
- rc 非零与缺少可信 structured evidence 时，TypedExecute 路由 execution_failed→Recovery；自由 stdout `STAGE_BLOCKED` 不会授权成功或正式提交。已有 Recovery `blocked` 可以走停止与独立报告，组合 A 验证了这个出口，但这不等于自动识别/成功发布缺依赖事实已修好。

最小设计：在现有声明约束下允许**受来源核验的部分/受阻事实排放**；所有提供的身份必须仍已声明，所有实际提供的事实仍验证字段，不给未产物强造 produces_artifacts/path。保持缺产物对 CompletionGate 的否决。复用 serializer 生成固定序列化调用，避免多处 Python -c 拼括号；只对明确可抽取的固定 Python 做编译或分支测试，不声称 Bash 检查涵盖动态内嵌代码。[Python compile 文档](https://docs.python.org/3.12/library/functions.html#compile) 仅支持语法编译这一层结论。

### D. Reviewer、native schema、诊断工具

Reviewer 0010/0014 均可 `json.loads`，均为 ACCEPTABLE；区别是首次 issue rows 有第四个字段 confidence。`semantic_completion.py:126` 要求行的 keys 恰好等于 issue_id/disposition/explanation；`supervisor.py:654` 把失败折叠为空解析结果，再形成 assessment_pending/reviewer_unavailable。**不是服务器离线或 JSON 被截断。** 原字段错误没有作为同层精确纠正回执保留，是后续修复点。

真实 revalidate 带原 invocation 登记的 inspection refs；未重跑进程，CommitStep 恰好一次。native 提案缺 schema_version 是 `submit_recovery_directive` 自身格式拒收；schema 来源 `RecoveryDirective.REQUIRED` 与 `native_submission_schema` 一致，随后 ToolMessage 中有准确 STAGE_FIELD_REQUIRED(schema_version)。当前公开分流没有回退；T16 再验 native 拒收不公开未分流全文且下一 provider request 保留纠正链。

artifact.inspect 请求为 `artifact:plan-artifact-b4a2996a00517f619eb4fb51`。`recovery_tools.py:600–643` 对所有 artifact 读入口先要求普通文件，目录触发 ARTIFACT_FORMAT_UNSUPPORTED；这个目标同样不能直接 artifact.read。已登记的普通文本日志可用现有定点读取，T15 分别验证目录拒收与新日志可读，不能把目录错误报告为数据损坏。

新增有界旁支 witness：持续无效评审下，三次选择当前合法 inspection refs 的 `revalidate` 均被运行时接纳；三次 reviewer、一次预检执行、零 Commit。`RecoveryRuntime.BUDGETED_ACTIONS` 不含 revalidate，每次新的 inspection refs 又可能构成“新证据”。测试在第三次主动停下，**只证明该窗口没有同层/该预算收敛，不宣称全局 deadline 无限或已修复**。应在现有 reviewer/assessment 生命周期内提供同源有界纠正，避免把所有 schema 错误递交执行后 Recovery。

### E. 完成回执与两个修正计数层

最后 provider receipt：stream_completed=true、completion_marker_seen=true、finish_reason=length；37.44 秒，8192 output tokens；无异常、取消或超时，SDK retries=0，application transport max_attempts=2 但实际只发生 attempt ordinal 1。正常完成传输与完整业务响应不同，adapter `public_response.py:96–113` 据 finish_reason 判 incomplete，早于脚本准入。

**UNKNOWN**：谁最终设定了 8192 的远端上限（服务默认、客户端默认或未记录配置）。当前 Generate 构造/bind 没有显式 output max_tokens，安全 manifest 也未保存该有效参数，不能把 observation 的 8192 偷换成已确认的配置值。本次无 context-window-exceeded 证据，不能称为上下文窗口耗尽。

| 事件 | scope / 计数结果 | 是否真实执行 |
|---|---|---|
| 891a1a3d8038cd060f30aa69 | 同 task revision、plan revision/version、step；representation；ordinal=1，limit=1，progress=true；payload_repairs +1 | 否；后续 0019 是唯一局部格式修正 |
| da15b222d0dcbf3e8a20eb08 | 相同持久 scope，跨新的 Recovery/Generate invocation；ordinal=2，limit=1，progress=true；preaction_rejections 总计2 | 否；直接停止 |

`bio_agents.py:16463` 从同 scope 的事件最大 ordinal+1；`:16497` 对 representation 写定 1，其他域才调用 `candidate_repair_limit`（默认/本次持久值都是4）。`workflow/execution.py:1071` 的 TypedGenerate 读取可信 receipt，取 min(self.repair_limit, receipt_limit)，没有再次按 UI 红字扣同一事件。组合 B 经真实 producer 重复投递两个 rejection，各自重载后 events/budgets 不变。通用4不是旧建议，也不是本次 representation 的生效值。

model_turns/tool_calls/no_progress_rounds=0 来自最后构造的局部 stop episode，无完整 Recovery episode；不能表示 session 没有模型/工具调用。修复应保留正确作用域与去重，区分 provider incomplete、完整格式错误、代码语法/语义修正、执行后 Recovery，不靠每次 reset 或把1机械改4。

### F. 因果身份与报告

身份没有从执行账本删除，也没有在 Recovery→Generate 整体丢失。最后 Generate request 明确含两个失败 attempt。准确边界是：

1. `recovery_runtime.py:530–547` 接纳后把 active packet 消费并清空活动指针，保留 `typed_recovery_receipts.failure_packet_ref`、`consumed_failure_packet_refs` 与旧 packet。这是正确生命周期推进，不能为报表恢复旧活动权威。
2. `plan_tracker.py:4884–4887` 在候选终止时对兼容 primary failure 写 execution_started=false，但同一对象仍含第一次/最近失败 attempt，是**字段 scope 混合**的最早写入点。
3. `workflow/execution.py:150` 的终态适配只带兼容 primary/latest 与预算，未形成消费回执指向的因果执行投影；`managed_stop.py:299–316` 只取 active packet，且 Generate/no_execution 分支明确清空 record/packet。随后 `_associated_attempts` 收到空 packet，旧执行未进入 origin/associated_attempts。当前候选无 attempt 正确，旧因果引用缺失错误。
4. `managed_stop.py:591` 又用上次 Execute facts 与新 stop 的空 attempt scope 比较，得到 EXECUTION_FACT_SCOPE_MISMATCH/EXECUTION_DETAILS_UNAVAILABLE。无须改执行权威或删除此核验；应比较因果执行的独立引用。
5. 未投影的旧 inspection failure 导致“本次没有工具读取错误”；这里只能描述末次局部 scope，不能覆盖 events 第105行。terminal summary manifest 的 step_id 还显示 03.mapping_s1，而冻结 stop 为02；03未执行，这属于通用观测 metadata 使用后继选择器的范围差异，不是实际重入 mapping。

报告保护符合当前代码：`runtime_recovery_reporting.py:97–134` 只允许 new atomic link，或有匹配 path/body sha256/实际内容的 prior receipt。即使同一 session 没有持久 receipt 的孤立文件，也不能按 mtime 推定所有权。重复投递同源同内容可复用；不同内容或丢失 receipt 继续拒绝，T13/T14 已测。

| 目标 | 原文件 SHA256 | 已核实归属信息 |
|---|---|---|
| D/report.md | 56e0281f8a28b6489e425638801329371d9e90b71c3d7b8405028a97bf430153 | 内有旧 stop 2b6896ff3f2e7b620de8a618663dc8effff86041cfb41f5375f336ca187ee108，与本次 fc0138…不同；step=preflight_inputs，attempt_9cc8b380a125cb445f3e |
| D/reproduce.sh | 5aef1f18531a47157c20e82ce20b1b813f2b1b3f92a01cd8584b83ac0073848d | 明确相同旧 stop，记录旧失败/未验证正文；只读未执行 |
| D/stage_status.tsv | e161ee9d2326b6c95c19a985486287e7aee8df9cbb8e669440b8fd39950ef21d | 无 session/stop marker；不同旧 step 命名；准确所属 session UNKNOWN |

没有用 mtime 单独认主，也没有去扫描其他 session。三份文件均无可供当前 stop 复用的同源 receipt；当前指定路径交付全部失败。`_report_targets` 只解析已有输出合同，当前绝对目标没有 session 独立文件回退策略；S/plan_state.json 的 managed_run_stop 与 context_trace 仍保存诊断。失败报告器本身不依赖13.reporting已完成，组合 A/persistent 已向新授权的 report.md 实际落盘；这不代表当前 D 路径完成交付。

后续设计应复用现有内部诊断归档，若现有授权确实涵盖 session 独立失败报告位置则给出该路径与 receipt，同时仍标明指定 D 交付冲突。没有授权则不写新位置。reproduce 应区分已执行失败正文、证据引用与未执行候选，默认不得伪造成功命令链。

### G. 科学前瞻与 Plan 纠正

正式 task_spec.inputs 只有四个 FASTQ 和一个 FASTA，均 user_explicit，01.inputs 恰好引用这五个身份；全部 downstream inputs 属于这五个、Plan 上游产物或正式输出。没有引用禁止旧 zip/genomeDB 或隐藏答案；禁止语句中的提及不是输入。13步主链按 mapping→去重BAM→GVCF→联合分型→过滤→统计，producer/consumer 身份可对齐；05.bam_list 供 reporting，calling 显式读取两份 BAM，不能仅因列表未供calling就判数据流错误。

参数 `{}`、confirmed_decisions `[]`、unresolved_questions `[]`；Plan 11/13 的成功条件却提到二倍体假设、物种/组装未确认。现有证据没有单独倍性确认，通用 Plan 同意回执不能伪装成新的生物学事实。这是后续 calling 前须澄清的决策；本次尚未执行到它，不能宣称已经产生错误基因型。

两次 shadow 冲突从最后 provider messages 可核实为先 filtered_snp_vcf、再 variant_stats。`plan_artifact_refs` 逐个抛出冲突，使一次候选中的确定冲突需多轮纠正；最终保留一份 Plan，不证明旧 TaskSpec 控制面复活。可在原 validator 聚合当前确定的冲突路径，不放宽身份保护。

## 共享调用方、最小修复切片与组合验收设计

### 切片1：真实受阻事实闭环

范围：`step_effects._emit_declared_rows/_declared_emission_rows/declared_effect_emission_contract`、必要的现有 Execute/Recovery 事实消费及当前 mapping Skill 示例。公共 helper 还被 `workflow/result_resolution.py:98` 的局部结果接纳/整理使用，必须保证其成功产物身份/path核验不退化。Generate 普通与 Recovery 两条消息装配共用同模板。

设计：允许已声明身份下的实际部分/blocked事实，保存原rc、来源与缺失集合；实际产物为空时不填 produces_artifacts 成功证明。由已有 Recovery 正式选择 blocked/ask_user/已授权独立工作，复用现有 stop/report。领域依赖分阶段、字典能力与去重能力分开。禁止 rc 白名单、仅匹配 echo、全局自动替代工具或改 Plan/gate 放行。

验收：T03/04/05/07/08/09/18；组合A中**合法 blocked 发布与准确收尾**须在实施后新增正向验收。本轮 A 的 publisher 仍失败，后续 blocked 是 fake provider 的明确决定；不叫修复完成。

### 切片2：局部生成与评审的有界恢复

范围：`public_response.project_generate_response`、Generate 现有候选 receipt/policy、`script_validation.check_script_body` 的限定 Python 检查、`supervisor.LLMReviewer._parse_decision` 与现有语义回复协议/有界同层重评。保留 provider completion 与具体 schema error 到反馈，固定serializer调用覆盖缺依赖/incomplete分支。完整但格式错在原 Generate 局部纠正；评审 schema 问题在现有 Reviewer 生命周期处理，不重跑科学命令，不加新旁白模型。

预算仍从持久同源 receipt 消费和去重，作用域明确；不得用 transport retry、同层格式修正和执行后 Recovery 混合计费，也不得每次 invocation 重置。补齐 effective output-limit receipt 的可观测性后再选择输出策略；不能凭8192改配置。本轮持续重评 witness 需要此切片覆盖。

验收：T01/02/06/10/11/16，组合A预检重评一次提交、B完整格式错与length分别可追踪；新旧事件与进程计数准确。

### 切片3：因果身份与真实交付

范围：`plan_tracker.record_preaction_contract_failure` 的兼容字段 scope、TypedGenerate terminal payload、`managed_stop.build_managed_stop/finalize_managed_stop`、`inspection_stop._report_targets` 与公共 `_write_report` 的归属/receipt恢复。成功、运行时依赖恢复、inspection stop 三个 writer 调用方都受影响。

复用已有 consumed Recovery receipt→packet→attempt 链，分别投影 causal_execution / current_candidate / stop_reason / report_delivery，不重新激活旧 packet，不给候选借 attempt。报告当前scope读取错误与累计已知工具错误分开；原子写入竞争、丢receipt、部分写入不能通过覆盖来解决。用已经授权的内部归档保全事实，指定交付失败保持可见。

验收：T12/13/14 和组合B；同源重投、异源保护、无候选执行、无虚假路径；不依赖13.reporting成功。多进程真实杀机/断电矩阵未运行，属于实施后验收边界。

三切片均需后续生产实施授权。本次不提交任何生产补丁。当前 Skill 增量仅需准确能力/命令、分支示例和待确认参数说明，保持单 Plan 与 typed routing；无需 Controller、语义 binder、平行 TaskSpec、全局文件配对器或独立命名模型。

## 离线验证与既有修复边界

见 [test_matrix.tsv](test_matrix.tsv)、[verification.json](verification.json)。调用已有 `tests/cli_model_progress_support.launch`，实际 biomind Python，已安装 CLI 依赖，双 guard、原 pytest fd capture、持久 fatal 出口；没有改环境或捕获方式。新临时根在 `/tmp/biomind-cross-layer-audit-20260913`，每次失败保留原目录。科学工具都是明确 stub；小 reader 和生产 executor/parser/typed graph/审批hash/gate/CommitStep/预算存储/报告 writer 为真实代码。

最新对应验收共 **21 个不同用例 PASS**：组件/既有公开投影 14（4.53s）、四种生产图组合 4（56.13s）、补充 scope/正式 gate 3（6.20s）。这些是 baseline witness 和保持边界的回归，**FIX PASS=0**。图组合的 fake stage 响应分为 A=8、B=6、persistent=7、persistent_revalidate=9（合计30）；这是显式计数的 Consultant/Planner/Generate/Reviewer/Recovery，不包含未独立计数的 fake router。另有 T16 fake provider 4、工具stub 2。四种图实际执行新小脚本合计5次，预检helper内 reader 9次；组件新小脚本6次。真实 BioMind模型/API/科学工具调用均0。

保留的失败账本：graph-results-1 为3个驱动契约断言失败（revalidate 缺新证据 refs；B脚本未引用批准输入）；graph-results-2 为1个驱动身份假设失败（observation-only不产生假定的兼容artifact primary failure）；graph-results-3 为1个驱动断言失败（已消费packet不应仍是active）；均只修新测试驱动，未改生产。scope-gate-results 为 pytest rc=4、零测试运行：指定既有测试原本为 untracked，提交树导出没有该文件；scope-export-2 显式复制该安全既有测试的原字节后3项通过，**既有文件未修改或提交**。所有原日志与未通过断言的账本保留；本轮没有 SIGSEGV，不能据此宣布此前原生崩溃已修复。

命令形态（每个结果目录、完整 argv、rc 和阶段账本在 verification.json）：

```bash
python tests/export_cross_layer_audit.py /tmp/biomind-cross-layer-audit-20260913/component-export-1
# 后续相同树可用 --reuse-export <已验证导出>，新目标目录，逐文件验证 hash。
python - <<'PY'
from pathlib import Path
from tests.cli_model_progress_support import launch
raise SystemExit(launch(
    Path('/tmp/biomind-cross-layer-audit-20260913/accepted-export'),
    Path('/tmp/biomind-cross-layer-audit-20260913/graph-results-4'),
    ['-q', 'tests/test_cross_layer_graph_audit.py'],
    fatal_diagnostics=True, installed_cli=True))
PY
```

另两组实际 `launch` 参数分别为：`final-export` → `component-results-2`，pytest `-q tests/test_cross_layer_audit.py tests/test_recovery_public_rejection.py::test_native_rejection_projection_and_history[compact]`；`scope-export-2` → `scope-gate-results-2`，pytest `-q tests/test_cross_layer_audit.py::test_T12_legacy_primary_execution_flag_changes_scope tests/test_step_effect_runtime.py::RuntimeEffectEvidenceTests::test_artifact_effect_retains_physical_and_provenance_validation tests/test_step_effect_runtime.py::RuntimeEffectEvidenceTests::test_plain_stdout_and_success_status_do_not_fabricate_observation`。全部使用相同 `fatal_diagnostics=True, installed_cli=True`。重新运行须选择新的结果目录，避免覆盖旧账本。

测试/导出切片提交：`5f89f1d633ce02d6cac5ac0b13e285168462ac9a`，仅新增 `tests/export_cross_layer_audit.py`、`tests/test_cross_layer_audit.py`、`tests/test_cross_layer_graph_audit.py`。文档切片只含本目录六文件。测试固定的 BASE 仍是 `60db1e4b8961d9eed7d36c312e79a75885e33dfa`；提交后如复用旧导出，显式传 `--tree 60db1e4b8961d9eed7d36c312e79a75885e33dfa --reuse-export <已验证旧导出>`，不要把新 Git 树误称为旧测试版本。

旧 FIX-R/G/O/F 回执 `docs/implementation/generate_execute_recovery_boundary_20260911/README.md` 已说明 Bash 检查不证明动态内层科学正确性、旧报告冲突仍未解决。已有 FIX-O 覆盖 observation-only 的负面值，未覆盖“required bundle 没生成仍需发布blocked”的组合；FIX-F 有当前执行/授权父执行投影，却在新候选终止时没有保留该父链。CLI/AP/OBS/输入生命周期没有被本次 audit 改动，也不能把本次缺陷扩大为“以前修复全失效”。

不能从本轮得出的结论：真实 Picard/GATK/samtools版本与替代可用性、真实生物学倍性、历史完整 Git加载版本、8192有效配置来源、旧报告准确 session owner、全部 Skill 端到端科学正确性、原生 SIGSEGV 已修复。剩余项目需要相应材料或另行授权的环境/科学核验，不应继续恢复原WGS来填表。
