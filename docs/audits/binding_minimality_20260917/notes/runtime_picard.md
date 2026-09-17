# Q4 专项：预检 helper 与 prepare_reference/Picard 的运行事实传递

日期 2026-09-17。源码分析对应当前工作树；主审计记录完整 HEAD/BASE 与工作区变化。本分工只新增本文件及 `evidence/runtime_*`；未修改生产代码/Skill/提示词/注册配置，未安装、调用 API 或运行任何科学步骤。以下路径 `R` 均指 `/public/home/liusiqi/03.毕设/Biomind_v1`；历史 `H` 指 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497`。全部行号为本轮读取工作树及原归档文件，不沿用旧报告行号。这里只复用环境审计中与绑定直接有关的证据，不审计全体安装政策。

## 结论与证据等级

**已证实**：`prepare_reference` 的断点不是“完全没有发现环境”，而是**已核验的 `bio_wgs` 前缀事实，没有成为本动作可用的科学入口事实和明确执行调用**。事故的 Consultant inspection 成功产生 `runtime.environment.prefix`，仅有 `RESOLVED_ENV_PREFIX=/public/home/liusiqi/anaconda3/envs/bio_wgs`；capability 的 required/optional 列表均为空，resolver 如约只解析声明的 IDs，故 `runtime_env_manifest.bindings=[]`。Generated Python 用裸名 `shutil.which("picard")`，没有显式选择环境/入口，runner 继承 ambient PATH，结果只有 picard 查找失败；索引尚未开始。

**已证实**：相同的空科学 runtime 列表用于本例只读 helper 是合法的。helper 只需已固定的 controller Python 与已选脚本/授权输入/框架 evidence leaf，不需要 BWA/Picard。原成功重试输出观察、无科学文件产物，CommitStep 正式接受并推进，不能通过要求所有 action 非空 runtime bindings 修复 Picard。

**已证实**：当前 resolver 接收 `requested_optional_ids`，但 `bio_agents.py` 四处生产调用及 `RuntimeEvidenceRecovery._assessment` 均不传；测试会传。只向 Skill 填 optional IDs 不能在现役主链选中它们。必须把正式选择贯通候选、冻结和 prelaunch，或在证明确无需要后删除这个不可达的表面能力；不能承诺只改 Skill 即可恢复。

**当前新增观察、不是历史科学结果**：`bio_wgs/bin/picard` 是符号链接到 `share/picard-2.20.4-0/picard` 的 Bash wrapper。它默认定位本环境 Java 与同目录 JAR，但可被有效 ambient `JAVA_HOME/bin/java` 覆盖。既有只读 help 探测已成功进入 Picard 字典帮助，本轮未重复。绝对 Picard 入口仍不足以固定 Java/环境全部条件；不过不能把此 wrapper 错写为始终从 PATH 找 Java。既有 `bio_gatk/bin/gatk` 单入口发生 Java 61/52 版本冲突，是另一种 launcher 行为，未发生于原 session。

**未验证**：历史完整 PATH/JAVA_HOME、历史 helper 顶层裸 `python` 实际解析路径，以及运行进程当时完整源码 commit。归档能证实 approved/executed 字节、wrapper runner 及子 helper argv，不能由当前源码或当前工具存在倒推出当时进程的全部状态。没有执行参考索引、完整 WGS、任何后续科学兼容性验收。

## 字段的真实语义与链路

| 字段/事实 | 生产与权威 | 入模/实际消费者 | 空值/失效语义 |
|---|---|---|---|
| Skill `runtime.default_env=bio_wgs` | 现役 frontmatter；`SkillManager.get_runtime_requirements`，`R/agent_core/skill_manager.py:761` 优先静态声明，registered record 是补充 | Consultant 候选提示 `BioAnalystAgent._consultant_runtime_context`，`R/bio_agents.py:19942`；healthcheck/发现选择 `_runtime_audit_for_skill`，19314；安装默认也引用 | 是环境声明/发现默认值，不等于激活或 child env。`env_runner=bash` 也不是 conda activation |
| registered `env/status/healthcheck/last_verified_at` | `R/skills/.skill_runtime_registry.json` 当前 WGS status success，2026-09-17T15:03:59；不修改 | 注册摘要可供发现；schema-2 正式绑定消费 task-owned accepted runtime evidence，经 `_runtime_binding_source`（19865）投影 | 注册 success 或旧 `last_success_at` 不能代替当前子命令/版本事实；旧时间/Scope 过期不得承诺可用 |
| `task_state.runtime_evidence` | trusted inspection + task acceptance；原 session 的 WGS 07:03:59 observation、07:04:01 acceptance | `runtime_evidence.evaluate_runtime_evidence:344` 按 task/Skill/host/env/contract/expiry 验证；`project_runtime_audits:379` 只投影 accepted AVAILABLE | UNKNOWN 区别于 UNAVAILABLE；当前读旧事实已经过期，见证按**事故实际时间**投影才得到 prefix ready。Skill revision变化也使 scope不同 |
| `required_runtime_binding_ids` | Skill capability 明示；经 approved capability contract hash固定 | resolver `R/agent_core/action_runtime.py:475` 逐个读取 ready registry，缺失出 `REQUIRED_RUNTIME_BINDING_UNAVAILABLE` | 当前 prepare_reference 空代表**未声明动作 runtime要求**，不是证明无需科学工具，也不是传递途中丢掉列表 |
| `optional_runtime_binding_ids` | Skill capability允许列表；函数参数支持选择 | resolver 534—546 仅解析 `requested_optional_ids` 中已声明项。主执行链没传该参数 | optional未选择合法空；本实现无主链选择通道，属于现役表达缺口 |
| `runtime_bindings/runtime_binding_records/manifest` | registry 将 verified output names 明确映射成 runtime IDs；resolver按单动作形成值和出处快照 | `_bind_pending_action` 12770/12900、12974持久化；Generate 42019及 prelaunch 47872重算比较；manifest hash进入审批身份 | 不应由模型填写内部hash。manifest空可以合法；它不代表真实工具检查完成 |
| action `env` | 模型明确action环境 + 程序按正式来源派生 helper/跨步值变量 | `build_exact_action_environment`，`R/agent_core/action_runtime.py:786`；`execute_logic_impl` 48181；executor接收 49582 | 默认保留ambient；不会把runtime registry/`ENV_PREFIX`自动变成PATH。只改manifest为非空也不会执行选中环境 |
| helper `invocation.runtime=controller_python` | 当前 WGS `generate.script_invocations` 显式机器契约；`resolve_script_invocation`固定sys.executable | `R/agent_core/skill_resource_binding.py:91` 获取契约、101绑定成action；Generate 42340/42347给前缀，prelaunch重验 | helper不是科学runtime；readonly observation不需要伪造产物 |

同一事实在 plan/action/manifest 中的快照有审批与失效用途；现在问题是“有前缀、无科学入口选择”和“可选入口未贯通”，不是应把所有registry隐式注入子进程。继续保留 `build_exact_action_environment` 的显式action边界。

## 样本一：固定 WGS helper 的完整链

1. **注册/选择**：真实脚本在 `R/skills/genomics/wgs-short-read-mapping/scripts/inspect_wgs_inputs.py`。当前 frontmatter 13—54 明示四helper皆 `controller_python`、明确args/delegates；`.inspect` capability 84—103 声明观察且不需要科学runtime。这是当前新增调用契约；原事故第一次候选没有该拦截的效果，不能假称事故时已使用当前契约。
2. **模型输入/输出历史实物**：`H/context_trace/generate-0009-*/request.json` 带script canonical path/BIOMIND_SKILL变量；`H/attempts/attempt_91c8a8eed3745c743d26/approved.sh:4` 直接把 `"$BIOMIND_SKILL_A363F1D9157F7CFC"` 当命令（无Python），`stderr.log:1` Permission denied，exit_status=126。实际executed.sh runner为 `/usr/bin/bash -o pipefail`。后续 generate-0012 为JSON action格式错误、0013为脚本块后文字错误，两者未实际运行；0014 成功。
3. **正确历史消费**：`H/attempts/attempt_136d6d27ad98b4fb9a72/approved.sh:4` 是 `python "$BIOMIND_SKILL_A363F1D9157F7CFC"`，四 `--fastq LABEL PATH`、一个 `--reference LABEL PATH`、`--observation-id validation_observation`、`--evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"`。`runtime_env_manifest.json:3`仍空，这次 rc=0。
4. **当前内部记录**：`bind_skill_resources` (`R/agent_core/skill_resource_binding.py:55`) 固定选中Skill exact snapshot、脚本内容/路径，receipt保存 `invocation` 与 `binding_digest`；`bind_action_skill_resources:101`将path handle写入action.env并检查调用。evidence leaf在真正attempt批准后派生（`R/bio_agents.py:49000`），其变量不是 `binding_env` 的来源映射。不会把 helper Python 等同于 bio_wgs Python。
5. **子进程**：`inspect_wgs_inputs.main` (`R/skills/genomics/wgs-short-read-mapping/scripts/inspect_wgs_inputs.py:26`) 用 `sys.executable` 启动明确选择的 FASTQ/FASTA reader（40）；FASTQ batch同理（`inspect_fastq_batch.py:56`），没有BWA/Picard subprocess。原 `preflight/checks.json` 保存子进程argv；父helper的历史裸python不能仅凭shell wrapper推断，但子reader实际路径可以直接查归档。
6. **验证与提交**：原task_state `typed_step_commit_receipts.commit-step-260039ff93a55114ed4545a2` accepted_binding_refs=[]、accepted_artifact_refs=[]，accepted_effect_refs有validation_observation，completed_step=input_check，successor=prepare_reference。观察仅四FASTQ的32记录前缀及FASTA头/首序列字节，`full_file_validated=false`。既有运行已证明只读动作无持久科学输出合法；本轮没重跑这些输入。
7. **失效**：当前选中Skill/脚本/解释器身份变更导致receipt或批准对象不再匹配，在prelaunch拒绝；解释器/参数表达问题应在Generate局部修正，此点由主审计的helper见证核验。历史126已经在旧会话成功恢复，且当前增加契约，不列作“当前仍无解释器校验”。

## 样本二：prepare_reference/Picard 的具体断点

原候选在 `H/context_trace/generate-0016-d4f379d86176`；最终执行 `H/attempts/attempt_51bd254a3abad5bfdfa0/approved.py`，真实action ID `action_981add843ec62f9d`。

- 原Plan选择 `genomics.wgs.short.read.mapping.prepare_reference`，正式输入reference，输出reference index bundle。当前 frontmatter 104—123 保留相同operation/effects、required/optional均空。`H/plan_state.json` accepted runtime evidence只确认WGS prefix，未产 `PICARD_BIN/JAVA_BIN/SAMTOOLS_BIN/BWA_BIN`，所以不得说“正式运行工具全核验过”。
- Generate当前输入由 `_generate_impl` (`R/bio_agents.py:42019`) resolver及 `project_action_runtime_for_provider`（action_runtime.py:641）生成。全空contract会得到 `current_action_runtime_bindings=[]`，空字段还在provider投影中被省略。历史trace manifest记录 `current_focus.runtime.resolved_runtime` absent 与空 runtime_audit_ref（主报告另提供实际请求摘录）。
- `approved.py:79` 做裸 `shutil.which`；`approved.py:81`—87 发布 `status=blocked, missing_commands=["picard"], index_artifacts=[]` 后 `SystemExit(1)`。还没创建OUTDIR/copy reference/索引。stdout实物保留exact missing。
- 内部 `runtime_env_manifest.json` bindings=[]，approval identity引用该manifest hash。`executed.sh:6` 用 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python approved.py`，Python脚本内subprocess.run未传env（approved.py:60），继承实际action环境。**Controller Python路径不是完整PATH的证明**，但具体which失败是执行事实。
- 当前 prelaunch `execute_logic_impl`（`R/bio_agents.py:47855`）验证manifest、再次resolve/hash比较、path可读可执行；`validate_runtime_paths_prelaunch`仅检查声明路径类型/存在/权限，不检查Java版本或科学subcommand。后面48181仅ambient+明确action.env+controller identity；runtime registry保持授权证据，不注入。
- 实际scientific工具/版本状态已被框架标为UNKNOWN：`failure_evidence.json:17` expected_runtime.coverage 中两项 false，bindings={}，actual_runtime.resolved_executables={}。`PROCESS_EXIT_NONZERO`失败包包含attempt/action/rc/log refs/stdout事实。此处结构passed不等于依赖ready，不能以结构检查“通过”反推科学能力。
- 原Recovery主动选择blocked，未耗尽no-progress预算；不存在已验证Picard显式重绑定后的执行。此前环境分析提出“没有正式恢复链”过宽：**当前实际上有 runtime证据恢复链（见下节），只是空required列表不会触发它，真实PATH miss进入了执行失败Recovery。**

当前Picard wrapper证据：`/public/home/liusiqi/anaconda3/envs/bio_wgs/bin/picard` -> `/public/home/liusiqi/anaconda3/envs/bio_wgs/share/picard-2.20.4-0/picard`。wrapper 17—20选择同env Java；23—25有效JAVA_HOME覆盖；64/66运行同目录picard.jar；5设置LC_ALL，31默认JVM内存参数。wrapper自身还使用bash/dirname/readlink等系统工具，入口绝对路径不能证明所有依赖，但本轮没有扩成通用系统依赖盘点。

复用 `/public/home/liusiqi/03.毕设/biomind_audit_exports/session_1789628497_runtime_probe_20260917.txt`：当前Picard CreateSequenceDictionary help成功（help分支rc1）、samtools dict帮助可用、GATK绝对launcher发生Java版本冲突而显式Java+jar进入help。**仅可用性/帮助证据**；不授权换实现、安装或继续原任务。原结论“当前机器并非已证明没有字典能力”可复用；“所有入口都已在事故时可用”“字典生成成功”不可复用。

## 当前错误包与路由：缺信息不等于未安装

见证 `evidence/runtime_witness.json.required_missing_diagnostics` 是调用现役resolver得到的完整包（合成 required contract，非真实事故错误）：

```json
{"code":"REQUIRED_RUNTIME_BINDING_UNAVAILABLE","execution_capability_id":"genomics.wgs.short.read.mapping.prepare_reference","selected_skill_id":"genomics/wgs-short-read-mapping","binding_id":"runtime.picard.bin","required":true,"failure_scope":"runtime_fact","availability":"UNKNOWN","available_runtime_binding_ids":[]}
```

- **未确认/expired/未选中**：runtime fact UNKNOWN；`_generate_impl` 42863—42892识别required runtime_fact并创建 `pending_runtime_evidence_request`，当前候选清空、execution_started=false，不生成执行失败包；`_recover_runtime_evidence:19760`经既有受控inspection接受事实后恢复同Generate组装（19766—19777）。非required/optional错误并不走该分支，应在补齐optional通道时同步规定错误归属。
- **registered陈旧**：`runtime_evidence.py:351—369`按scope和时间筛选，过期→UNKNOWN reason `evidence_expired_or_future`，不以旧注册success永久授权。若脚本元数据变更，旧scope不匹配，要求重新确认。
- **候选冲突**：evidence signatures不一致→AMBIGUOUS；resolver包保留availability，`RuntimeEvidenceRecovery._recover`（`R/agent_core/workflow/runtime_evidence_recovery.py:491`）返回 `RUNTIME_EVIDENCE_CONFLICT/internal_error`，不是安装。
- **当前PATH miss**：原候选是运行之后 `PROCESS_EXIT_NONZERO`，stderr空但stdout结构化missing精确；正常执行失败Recovery调查。不能将该事实升级为全机不存在。
- **真正无依赖**：`RuntimeInspectionAdapter.inspect`（`R/agent_core/runtime_inspection.py:463`）只有声明negative_token、complete_search=true且负结论完整时产生UNAVAILABLE，否则缺输出为UNKNOWN（462）；runtime recovery返回 `REQUIRED_RUNTIME_FACT_CONFIRMED_UNAVAILABLE/dependency_blocked`（runtime_evidence_recovery.py:498）。是否安装/人工处理走已有授权路线，本轮未审核install_agent全部规则。
- **不兼容**：tool非零/Java stderr是执行或probe失败证据；`runtime_inspection.py:417` probe非零只记 `RUNTIME_INSPECTION_NONZERO_EXIT/execution_error`，不会凭一次命令未找到造UNAVAILABLE。精确Java不兼容仍应从安全限长证据引用给恢复角色，而不是复制全部环境变量。
- **没有覆盖该事实的inspection生产者**：runtime recovery 515—516明确 `RUNTIME_INSPECTION_PRODUCER_MISSING/unresolved`；补required ID却不补显式检查输出会被阻塞在此，不能称“绑定修完”。

错误信息当前已具类型、阶段、Skill/capability/binding、availability、可用IDs和日志ref基础。改进需附已确认环境候选、检查覆盖、为什么UNKNOWN及可请求的既有工具；不应给模型大量digest而只留一个错误码。

## 可合并的一份环境绑定修改切片（草案，未实施）

1. **显式选择入口并贯通**：目标文件 `R/skills/genomics/wgs-short-read-mapping/SKILL.md` 的 `generate.execution_capabilities` /runtime检查声明；`R/agent_core/skill_manager.py:get_runtime_requirements`；`R/agent_core/runtime_inspection.py:RuntimeInspectionAdapter._specification/inspect`；`R/bio_agents.py:BioAnalystAgent._runtime_audit_for_skill/_runtime_binding_source/_build_graph._generate_impl/_bind_pending_action/_build_graph.execute_logic_impl`；`R/agent_core/action_runtime.py:resolve_action_runtime_bindings`。针对本例，确认环境前缀后经明确声明的probe查BWA/samtools/所选字典实现、Java/JAR条件；模型选择科学实现和已核验候选，程序按显式ID精确取事实，不扫描自由脚本猜角色/命令。尽量使用现有 `action.argv/env/code`；不加全局DSL、第二份Plan或后台conda激活。
2. **将optional成为真实选项或删掉死接口**：如果保留可选字典实现，正式候选保存明确选择ID，将同一组 `requested_optional_ids` 贯通Generate resolution、两次 `_bind_pending_action` 及prelaunch、runtime recovery assessment；选中之后其缺事实必须能请求inspection，而不是required=false落入泛型prompt重建。相应schema/示例/历史action读取兼容缺字段为旧行为，旧批准不能静默补选择。不要同时强制要求Picard与另一个互斥实现都存在，也不将所有WGS动作强绑bio_wgs。
3. **只把已确认事实转成候选执行定义**：选定明确Picard wrapper与Java条件后，在审批前构造/要求action明确 `PICARD_BIN`（或绝对argv入口）、确有必要的JAVA_HOME或显式Java+JAR调用。自由科学脚本保持原表达能力，不能由binder识别 `shutil.which("picard")` 再改码。保持`build_exact_action_environment`不自动注入registry，保留同一审批对象的prelaunch重验；科学环境变化应失效重批。
4. **现有事实恢复为一条链**：沿用 `_generate_impl` typed runtime request 与 `RuntimeEvidenceRecovery`，补operation所需coverage/事实，不并入Generate全对话，不恢复旧Controller或语义binder。只改提示词不能补缺少的runtime事实、optional消费者及action传递；只改Skill也不能让空`action.env`自动采用环境。
5. **直接成本可减但先保边界**：当前Generate一次resolver，接纳 `_bind_pending_action` 两次resolver（分别用于候选指纹与最终action ID），prelaunch再一次；每次构建registry、serialize/hash。可在同一候选内共享已核验scope/evidence revision的解析结果，再用最终action ID形成manifest，保留prelaunch freshness/path/批准检查。此链只对小JSON与路径stat，**没有对FASTQ/FASTA全文hash**；不编造耗时收益。删去重复中间快照之前必须检查approval fingerprint依赖，由主Q6矩阵统一决定，不保留两套新旧来源作为长期写入者。

建议行为验收（非本轮已实现）：(a) declared bio_wgs工具存在而ambient PATH miss，生成明确选择候选并审批后启动正确入口；(b) helper保持controller Python，science可不同环境；(c)没有科学runtime的只读helper仍通过且无需科学产物；(d)可选未选择为空合法，已选择缺事实触发受控发现；(e)缺coverage为UNKNOWN+具体producer缺口，完整negative为UNAVAILABLE；(f)Picard wrapper存在但JAVA_HOME冲突需明确修正候选；(g)批准后脚本/入口/环境变更不静默跟随；(h)有效科学内容/产物验收不因runtime结构passed而跳过。

## 本轮隔离见证与边界

- 执行 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B /public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/runtime_witness.py`，输出 `runtime_witness.json`，exit 0；直接导入纯 `action_runtime/runtime_evidence`、读取真实WGS frontmatter及原accepted evidence，显式记录源码SHA256。没有导入 `bio_agents`/配置/API文件，没有subprocess。
- 证明：事故时间投影只有prefix；当前真实preparecontract空→空manifest/空provider runtime；内存合成optional contract默认不选/明确选择可得入口；exact env不注入registry，明确action.env才能携带；ambient JAVA_HOME保留；missing/negative/conflict分别包UNKNOWN/UNAVAILABLE/AMBIGUOUS；过期证据UNKNOWN；当前wrapper的Java覆盖逻辑与Java/JAR文件存在。**合成required/optional contract用于隔离接口语义，不伪装为已实施Skill修复或已接受探测事实。**
- `evidence/runtime_history.json` 对原3attempt的approved/executed字节、manifest、exit/failure包与前次probe进行源路径+SHA256保存；原session只读，未恢复。脚本/日志保存来自历史事实，不意味着当前模型候选重放。
- 未做：新模型请求、真实Python helper输入读取、索引/变异分析、Picard帮助重测、完整科学E2E、生产代码修改、全量安装规则审计或任何自动push。

## 收尾发现的并发环境专项：版本核对与统一复用

收尾时发现另一个并发审计目录。本轮只读复用其 [主报告](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/AUDIT_REPORT.md)、[runtime明细](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/notes/runtime.md)、[baseline](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/baseline.json)、[并发变更记录](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/concurrent_change_checkpoint.json) 和直接runtime结果；没有重做其软件探测、安装政策扫描或测试，也不把外部审计文件纳入本轮写入范围。

版本一致性：该专项 baseline 捕获于 `2026-09-17T08:51:53.684907+00:00`，BASE/开始HEAD同为 `4ea823511328ed4035ec2a99ddf09a3419bb288a`；其541个状态条目是稍晚现场，与本轮开始522项不能混为同一时刻。逐文件复核该baseline与收尾磁盘：`skill_manager.py`、`node_context.py`、`action_runtime.py`、`runtime_evidence.py`、`runtime_inspection.py`、`workflow/runtime_evidence_recovery.py`、WGS `SKILL.md` 的SHA256均相同。`bio_agents.py`从 `75a90a11158304c2a060864de96889c27e4f53917d937b2946ea9377290c0f32` 变成 `c4d81118032f0132a8269cb5344cc1650986d148a8e80bd911906bb304d35883`，不能将全文件旧测试当收尾集成证明；本轮主报告单独记录其并发差异。该专项同样承认原加载版本/历史PATH未完整保存，并未实施生产修复。

**一致、可直接复用**：原Consult已确认prefix但无Picard/Java/subcommand事实；空capability范围依法形成空manifest；default_env不是进程执行承诺；action.env/argv应显式携带已选事实，不做全局PATH注入；UNKNOWN/UNAVAILABLE/AMBIGUOUS须分开；原Recovery主动blocked而非预算耗尽；帮助/合成runner见证不证明科学E2E。专项 [runtime_witness_results.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/runtime_witness_results.json) 的9个PASS均为现状见证，不作为本审计新运行的9项测试。

**对本报告切片A的两项必要补充**（已对照当前源码和原请求）：

1. **Planner语义投影有可定位的上游丢失。** `R/agent_core/node_context.py:_compact_planner_runtime_projection:1561` 只取 `selected_environment/environment/environment_name/conda_env`，不取生产者的 `env/checked_env/declared_env`；可执行入口别名不取 `resolved_runtime_paths`；它从倒序rows取首个非空值，也不是按Skill保留状态。回读三个原请求 `planner_initial-0006-62fd286e6ccb`、`generate-0016-d4f379d86176`、`recovery_turn-0018-f4de4bd43eec`：每个有2次Skill文本bio_wgs，但已验绝对prefix、runtime_evidence、runtime_audit均0次，与专项记录一致。因此本轮“不是resolver把prefix丢了”仍正确，**不能扩大成上游所有投影都没丢事实**。统一切片A应同时修此函数的按Skill精简语义投影，保留已确认路径、coverage与UNKNOWN原因，仍不将完整内部审计hash灌给模型。
2. **执行失败后的恢复须补合法接线。** 现役 `RuntimeEvidenceRecovery._scope`（`R/agent_core/workflow/runtime_evidence_recovery.py:160`）拒绝已被执行拥有的步骤（165—166）；`create_request:255`拒绝空required（261—263）。因此当前已存在的prelaunch证据恢复不能直接用于这个已经执行失败且required为空的动作。统一切片A应从原FailurePacket启动限定发现，提交现有runtime evidence后，经旧执行所有权结束/释放、正式计划范围复核或必要Planner修订，再生成新动作/新批准/新attempt；保留已经Commit的input_check。不能删除not_dispatched保护、修改旧批准动作或把UNKNOWN直接送安装器。

差异不是相互冲突的补丁：环境专项覆盖全安装职责、授权及终态问题；本binding审计只纳入上述事实投影、显式选择/承载和恢复入口，与原A切片形成一份修改建议。optional-ID解析/schema缺口仍由本审计补充，专项没有证明现役主链能选择optional。选择现有capability+必需facts/argv/env可避免新增机械字段；确需optional接口时必须同步parser/schema/冻结/批准/prelaunch及恢复scope兼容，不仅改一段Skill列表。
