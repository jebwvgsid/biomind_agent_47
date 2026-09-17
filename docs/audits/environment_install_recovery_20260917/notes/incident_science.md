# 事故事实、科学依赖、终态及协议补充审计（Q9–Q11）

审计基线由主报告记录；本分项按实际磁盘源码及原 session 证据判断。原始资料仅只读；没有读 `.env.api_keys`，没有导入应用配置，没有真实模型/API、安装、原任务恢复、真实索引或 WGS 计算，没有 Git 提交。由于用户有并发修改，历史错误、2026-09-17 已有 helper 修复和审计时源码分别陈述。

本分项可复核证据：

- [隔离见证与统计代码](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/incident_witness.py)。仅 Python 标准库；从原脚本 AST 只替换两个路径常量；所有科学子进程用替身；CLI 执行当前原函数；directory predicate 执行当前原函数 AST，避免配置初始化。
- [见证结果与历史调用索引](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/incident_witness.json)。JSON 定位 `partial_index_witness.cases`、`cli_witness`、`historical_summary.calls`；3 个现状见证 PASS 不代表修复完成。
- [分项结束源码及旧报告检查点](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/incident_source_checkpoint.json)：10 个相关当前源码 hash/关键函数行号、旧目标 mtime/hash、并发修改与本次 scope 的关联。
- 原 session 根目录：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497`。以下称 SESSION 仅为行文缩写；证据链接均为绝对路径。

## 1. 回到原始证据校准事故

| 顺序 | 历史事实与类型 | 结果、证据定位 |
|---|---|---|
| Planner 1、2 | 两个不同计划候选错误：`TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL`，随后 `PLAN_STEP_EFFECT_UNSPECIFIED` | 第 3 个候选成为获批 18 步计划；`context_trace/planner_initial-0007-154b34f72bde/request.json` 和 `planner_initial-0008-d5e9808aca76/request.json` 保留前次错误反馈；最终 `plan_state.json` 的 `steps`、`approval_record` |
| input_check 首次执行 | 固定 Python 源文件被当可执行文件直接启动，rc=126；一次入口错误 | [approved.sh:5](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_91c8a8eed3745c743d26/approved.sh:5)、同目录 `stderr.log`；未产生正式观察 |
| Recovery 第一次 | 2 个只读调查调用 `failure.inspect`、`skill.resource`；提交一次 `retry_step` | [0010 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0010-3f9930ff15c6/response.json) 的 `tool_calls`；[0011 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0011-23893f300b7b/response.json) 明确只修解释器，原五输入与只读范围保持 |
| Corrective Generate 两个候选 | 0012 返回 action JSON；0013 在脚本块之后附中文；两个 `GENERATE_SCRIPT_BLOCK_INVALID` 均未执行 | [0012 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/generate-0012-fefe90c44169/response.json)、[0013 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/generate-0013-152ebb16a3d2/response.json)。二者已包含 Python 调用，不能说反复忘加解释器 |
| input_check 第二次执行 | 修正候选 0014 通过审批，rc=0，CommitStep 成功 | [approved.sh:4](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_136d6d27ad98b4fb9a72/approved.sh:4)、[checks.json](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_136d6d27ad98b4fb9a72/preflight/checks.json)、`plan_state.json.task_state.typed_step_commit_receipts` |
| prepare_reference | `shutil.which("picard")` 未命中，发出 `status=blocked, missing_commands=["picard"], index_artifacts=[]` 后 rc=1 | [approved.py:79](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/approved.py:79)、[stdout.log:1](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/stdout.log:1)。在第 87 行提前退出，FAI/BWA/dict 命令均未运行；后续 mapping/calling 未运行 |
| Recovery 第二次 | 0017 `failure.inspect` 成功、`step.inspect` 使用 evidence ref 类型错误；0018 改 `evidence.read` 成功；0019 主动提出 blocked 但漏 `schema_version`；0020 补 1 接纳 | [0017 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0017-21dad96cd7b4/response.json)、[0018 response](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0018-f4de4bd43eec/response.json)、[0020 request](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0020-5745b56334d0/request.json) 最后 tool 消息 `STAGE_FIELD_REQUIRED` 与 `model_turns_remaining=9`；最终 `no_progress_rounds=0`。不是预算耗尽 |
| 停止收尾 | 全局 blocked，prepare_reference=running；已解决取证错误仍显示；指定 3 个旧目标冲突，内部归档 complete | `plan_state.json.task_state.managed_run_stop` 的 `report_receipts`、`inspection_observations`、`inspection_failures`、`internal_diagnostic_delivery`；见 Q10 |

input_check 实际仅四个 FASTQ 各 32 条记录的前缀、FASTA 头及首序列字节核验，`full_file_validated=false`。此 scope 不能扩成全部 gzip、配对、序列或参考完整性通过，也没有证据表明本次最终阻塞来自输入损坏。

直接失败与上游缺口：原 action [runtime_env_manifest.json](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/runtime_env_manifest.json) 中 `bindings=[]`，生成脚本依赖裸工具名；本次能证明动作环境 PATH 查找 Picard 未命中，不能证明整台机器无软件或无字典能力。原 PATH 未完整持久化，无法精确回放。当前帮助探测、Java 风险属于当前证据，不能倒推原事故发生了 GATK 失败或当时同一版本一定可用。

阻塞放大因素：冻结计划把 FAI+BWA index+dict 聚合为必须完整完成的 `01.ref`，mapping 消费整个 bundle；不得原地擅自拆依赖。独立问题：终态、最终原因、取证历史和交付可见性。未触发风险：原生成脚本可能把部分索引发成完整 created；下一轮恢复相应步骤前必须处理。

## 2. Q9：冻结计划与真实科学消费者

冻结证据：[plan_state.json](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/plan_state.json)，JSON `steps[*]` / `plan_artifacts.plan-artifact-7c3b64e012cb94ddd0184b27`，以及见证 JSON 的 `historical_summary.steps`。当前 Skill [mapping](/public/home/liusiqi/03.毕设/Biomind_v1/skills/genomics/wgs-short-read-mapping/SKILL.md:340)、[GATK](/public/home/liusiqi/03.毕设/Biomind_v1/skills/genomics/wgs-gatk-snp-indel/SKILL.md:125)。未执行的后续节点没有实际生成脚本，表内区分已批准命令和 Skill/计划要求。

| 能力/阶段 | 已批准或待生成的具体子命令 | 实际消费者和冻结约束 |
|---|---|---|
| FAI | 已批准 `samtools faidx local_ref` | FASTA 随机访问及后续 GATK；和参考序列、contig 名称/长度一致 |
| BWA 索引 | 已批准 `bwa index local_ref`；预期 `.amb .ann .bwt .pac .sa` | BWA-MEM mapping 使用 FM-index；这些不是 GATK sequence dictionary |
| 字典 | 已批准 `picard CreateSequenceDictionary R=... O=...` | GATK 参考要求 `.dict` 与 `.fai`；本任务的完整 reference bundle 也要求它。字典是能力，入口可有不同实现，但需明确版本、参数、参考身份及下游验收 |
| 比对/排序 | 待生成；Skill `bwa mem -R ... | samtools sort ...` | `map_s1`、`map_s2` 各自消费 FASTQ、原参考及完整 bundle；依赖 `prepare_reference` |
| 重复标记/索引 | 待生成；Skill `picard MarkDuplicates I=... O=... M=... CREATE_INDEX=true REMOVE_DUPLICATES=false` | map_* 的正式交付是坐标排序、标记而保留 duplicate 的 BAM/BAI、metrics；替代 dictionary 不解决该 Picard 依赖，不能静默改为另一种去重方法 |
| BAM 验证及统计 | 待生成；Skill `samtools quickcheck -v`, `view -H`, `flagstat`, `coverage`；BAI 缺失时 `samtools index` | 验证/统计 map_* BAM；分别依赖实际子命令支持及已选版本 |
| BAM 列表、倍性 | 待生成；列表构造/用户确认 | build_bam_list 依赖两个 map；confirm_ploidy 依赖 build_bam_list。确认可提前到 Consult/规划，但不能猜倍性；这是效率建议，不是本次失败主因 |
| 每样本 calling | 待生成 `gatk HaplotypeCaller -ERC GVCF --sample-ploidy ...` | call_s1_gvcf、call_s2_gvcf 消费各自 BAM/BAI、参考+bundle 和倍性确认，需同一动作内兼容 Java |
| 小队列联合分型 | 待生成 `gatk CombineGVCFs` → `gatk GenotypeGVCFs` | 冻结选择 CombineGVCFs；输入为真实 GVCF，不能换历史 zip/GenomicsDB，也不能未经修订切到 GenomicsDBImport |
| 分型拆分、过滤 | 待生成 `gatk SelectVariants`、`VariantFiltration`，PASS-only 用 `SelectVariants --exclude-filtered` | split_variants → filter_snp/filter_indel；科学参数/适用性应按已确认约束，不能把新版工具默认值作为任务授权 |
| 变异验证/统计 | 待生成 `gatk ValidateVariants`、`bcftools stats` | validate_variants → summarize_variants；bcftools 用于读实际 VCF 统计，不是第二套 caller |
| 报告、复现、状态表 | 待生成科学收尾；现有 managed stop 独立收尾已触发 | 正常 write_report/write_reproduce/write_stage_status 都依赖 summarize_variants；失败收尾应复用 managed_stop，不另建科学报告流水线 |

科学边界由官方材料支持：BWA 作者 README 要求先建 FM-index 再 mapping [BWA README](https://github.com/lh3/bwa/blob/master/README.md)；GATK 要求同参考 FASTA 的 `.fai`、`.dict` [GATK reference format](https://gatk.broadinstitute.org/hc/en-us/articles/360035531652-FASTA-Reference-genome-format)；Picard MarkDuplicates 的 `CREATE_INDEX` 针对坐标排序 BAM [MarkDuplicates](https://gatk.broadinstitute.org/hc/en-us/articles/360057439771-MarkDuplicates-Picard)。这些说明能力及消费关系，不证明本机兼容性；本轮未运行真实小样科学验证。samtools dict 的本机帮助仅能表明候选可用入口，必须由正式选择及下游验收确认其替代资格。

推荐默认保留一个方便管理的 reference bundle，但把其必需成员、命令成功、来源及参考一致性变成明确验收约束。若用户确实需要字典不可用时先做比对，可经现有 Planner 修订，将 BWA 索引及 GATK 参考辅助文件划成有科学消费者的两个产物/子阶段；收益是减少无关阻塞，成本是计划/审批/产物身份变化、引用路径及复用证据迁移。**不能保证当前 map_* 可继续**：map_* 还需要 Picard MarkDuplicates，且冻结计划当前必须完整 bundle。不要把每个小索引文件机械拆一步。

产物验收已证实的风险：[原 approved.py:102](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/approved.py:102) 未在各命令失败时统一退出；BWA 文件存在就加入 produced，未检查 BWA rc；第 114 行 `if produced:` 即发布整个 bundle 的 artifact 和 `created`。冻结 artifact 是目录；实际 [Recovery request-0019](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/recovery_turn-0019-60d3b3d9f6ab/request.json) `messages[1]` 的 `failure_packet.diagnostic_facts[kind=result_realizations].metadata.result_realizations.declared_results[0].validation_contract_id` 为 `directory_exists`；[completion_gate._contract_exists:1680](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/completion_gate.py:1680) 此分支只核目录存在。

本轮两个现状见证均 PASS：①只有 FAI 成功、BWA 与 Picard 失败，原脚本退出 0 并发布完整 bundle created；②三个命令全 rc=1，只留下 BWA `.amb` 部分文件，仍退出 0 并 created；当前通用 directory predicate 也为 True。见 `incident_witness.json.partial_index_witness`，包含原脚本哈希、仅两个 AST 常量替换、命令回执和全部临时文件名。此处证明了**误报与通用目录验收不足**，没有运行 Supervisor/CommitStep，不宣称框架必然最终提交错误科学结果。必须保留模型科学核验及 CommitStep，并补能力特定完整性约束，不能靠解析任意脚本猜工具/文件依赖。

最低修复验收：每个必需命令 rc 满足工具语义；每个当前所选工具版本的必需成员存在/非空且内部结构合理；FAI/dict/FASTA 的 contig 顺序、名称、长度、必要 checksum 一致；复制参考的身份和来源可追溯；新产物在独立 attempt 暂存区域验收后按现有正式提交进入绑定。历史产物不能仅按存在复用，需既有来源/验收证明，否则只作待核验候选。

## 3. Q10：终态、取证历史、最终原因与交付

原 `plan_state.json.task_state.status=blocked`、`runtime_state.status=blocked`、顶层 `execution_status=failed`；`steps[input_check].status=passed`，`steps[prepare_reference].status=running`，后续 16 步 pending。只有 input_check 正式提交；prepare_reference 的进程 rc=1 已记录，run lease released。显示 running 不证明后台进程还在运行。当前 [managed_stop._save:672](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/managed_stop.py:672) 无条件调用 set_task_status 更新全局；仅有 `candidate_scope_identity` 的停止会同步当前 step。本次 post-execution Recovery stop 没该字段，故保留 running。单一终态写入应使用现有 scope/attempt identity guard 选中确切当前步骤，不能覆盖已经提交步骤或把所有 pending 步骤改成已执行失败。

最终模型 blocked 决定 `MISSING_REQUIRED_CAPABILITY_DICTIONARY_TOOL` 把当时动作找不到 Picard 提升为“缺能力”，事实范围过宽；应显示“prepare_reference 动作未绑定环境，当前 PATH 未发现 picard；已有环境尚待验证”。CLI 不能仅照抄模型科学诊断为已验证事实，但也不能忽略记录中明确 `missing_commands=[picard]`、空 bindings 和退出阶段。应将观测、推断、仍未知项并列体现。

已成功的 inspection：`managed_run_stop.inspection_observations` 记录 `failure.inspect`、`evidence.read`；同一个 evidence ref 的 `step.inspect` 错误保留在 `inspection_failures`。当前 [managed_stop.py:438](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/managed_stop.py:438) 从 causal history 追加错误，无“后来成功/仍阻碍调查”的当前事实投影；[cli_lifecycle.py:159](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/cli_lifecycle.py:159) 无条件将所有历史错误列作当前“恢复取证问题”。不能删历史；应以相同 evidence identity 和后续成功接收建立 resolved 历史投影。首次错误和最后成功是两条不同回执。

当前 [cli_lifecycle._cause_text:114](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/cli_lifecycle.py:114) 对普通 `EXECUTION_FAILED` 固定“具体原因尚未定位”；仅另有特定 effect identity 错误的精确识别。本轮把原 final_result.terminal_diagnostic 输入当前 `terminal_text` 的见证 PASS：仍泛化原原因、仍渲染已解决类型错误、能显示内部归档 complete，却不显示其路径。详见 `incident_witness.json.cli_witness`，这是当前原函数真实调用。

交付与计算必须区分：3 个指定输出 receipt 都 `failed/RUNTIME_REPORT_EXISTING_FILE_PROTECTED`；`required_output_status=conflict`，不是权限系统拒绝。当前 [runtime_recovery_reporting.py:196](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/runtime_recovery_reporting.py:196) 对无本次所有权证明的既有目标拒覆盖，应保留。内部诊断 complete，有 path、sha256、operation_id 和 ownership receipt；[真正本次 report.md](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/state_contract/terminal_diagnostics/8842872c09e3ea7a1ad0418088dc8988bae2426c298202df3f30c02d156db61c/report.md)。内部 reproduce 只是停止诊断脚本，不是完整 WGS 复现成功。

启动时目标冲突预判：本次 plan 早已指定 `report.md/reproduce.sh/stage_status.tsv`，但没有在审批执行前阻止目标所有权冲突；到最终停止 writer 才得到失败回执。现有 `managed_stop._managed_diagnostic_targets` 可落本次 task store 内部唯一目录，应复用并让 CLI 展示路径。改善入口需只读比较目标/ownership、给出本次可达输出选择并在正式输出约束中记录；不可私自改用户 required exact path，也不可默认覆盖旧报告。旧 9 月 8 日文件不能用于解释 9 月 17 日事故。

安装等待本事故没有触发；第二次 Recovery 主动 blocked 不是等待授权状态。本分项不据显示推断安装工作在后台进行。失败收尾已经有 managed stop 且不依赖 summarize_variants，正常计划的报告依赖成功统计是科学流程事实；优先改现有 stop 消费和展示，不另建一套终止报告系统。

## 4. Q11：固定 helper、协议与计量

### 固定 helper：历史入口缺口，当前已局部修复

实际首次 [generate-0009 request](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/generate-0009-7e7691897098/request.json) 中，system 777 字符、Seed 有详细协议与一般 Python 示例，但资源 catalog 只给文件路径/变量，没有该 helper 的明确 interpreter+argv；历史 receipt `readable=true, executable_bit_required=false` 只能证明可读/固定身份。历史脚本为正常 0644 源文件，正确修复是绑定解释器调用，绝非 chmod 全部源码。

当前 mapping Skill 已新增 `generate.script_invocations` 的四个固定 helper，`schema_version=1/runtime=controller_python/arguments/delegates`；[skill_script_invocation.resolve_script_invocation](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/skill_script_invocation.py:21) 固定 `os.path.abspath(sys.executable)`、保留 venv 路径并记录解释器文件身份、argv_prefix/shell_prefix、参数形状；[skill_resource_binding.py:91](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/skill_resource_binding.py:91) 绑定与校验已接入。它只作用于声明的固定 helper，不是科学 bio_wgs 自动激活，也不恢复任意脚本语义 normalizer。[当天既有修复说明](/public/home/liusiqi/03.毕设/Biomind_v1/docs/fixes/skill_script_invocation_contract_20260917.md)。

已有验证记录宣称 69 passed；本轮只复用并校验，未重跑。主 agent 的 [hash 核验](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/reused_helper_validation.json) 显示 6 项中 5 项匹配，`bio_agents.py` 因用户并发改动不匹配。故不能称当前完整集成已重新 69 passed；匹配 helper/binding/Skill/tests 的既有见证仍为局部支持。历史审批绑定旧 Skill revision，新能力必须按现有 revision/审批重新形成动作；不能直接重放旧脚本。

### Generate 协议不是完全缺失；Recovery schema 也不是没告诉模型

当前 [prompts/manager.py:2816](/public/home/liusiqi/03.毕设/Biomind_v1/prompts/manager.py:2816) 首选唯一 JSON display envelope，payload 仅一个 `<shell>/<python>/<r>` 可执行块；[manager.py:2851](/public/home/liusiqi/03.毕设/Biomind_v1/prompts/manager.py:2851) 明确旧单块兼容，也有互斥 typed Skill decision 和 corrective blocked 分支。它们是互斥业务结果，不是允许同时给多个执行动作。当前 [public_response.py:110](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/public_response.py:110) 精确剥离展示 envelope，[controller_audit.parse_executable_script_block:77](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/controller_audit.py:77) fullmatch 拒多块/块外文字。历史 0012/13 请求中同样有单块规则；0012 的 action JSON 没走当前单块协议，0013 块外附言违约，不能归因没有规则。

推荐最小修复：保证 normal/corrective Seed、示例、错误反馈共用同一协议说明与具体合法最小示例，保留兼容单块入口，不预设整体改新协议或放宽 parser。错误反馈明确当前返回的哪个形态不符，以已公开 schema 纠正；不要把重新生成相同执行脚本的格式失败计为一次科学重跑。

当前 [recovery_agent.py:123](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/recovery_agent.py:123) 明确必填 `schema_version/directive_id/task_id/action/summary/reason_code`，实际 0019 request.messages[0].content **也包含该句**。0020 request 最后 tool 错误更提供了完整 schema、精确缺字段 `schema_version`、剩余 corrections/模型轮次。0020 只补 schema_version 后成功；不是新增科学诊断或第 2 次执行恢复失败。

当前 [recovery_reception.adapt_native_version:114](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/recovery_reception.py:114) 已有“唯一版本常量”最小适配：只有 complete native 提交且保留原始 arguments JSON 字符串、可验证无重复键时补唯一值 1，并记录原/适配值，最终仍通过 validator。[recovery_agent.py:1181](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/recovery_agent.py:1181)、[1304](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/recovery_agent.py:1304)。本次持久 reception 有 finish_reason=tool_calls/native_tool，但没有 adaptation，报字段缺失；归档 provider-visible response 仅保留 normalized tool_calls，不能凭它反推当时 SDK 原始 arguments 是否存在。因此实际历史断点缩至 native raw 参数保存/适配准入，需要保留原始 provider 参数的隔离验收；不宜写成“程序忘给默认 1”，也不应无条件补缺字段来抹去协议错误。

### 时间/调用/token 复核

原 metrics 记录墙钟 516.748 秒（保存的 ISO 起止时间只到秒，不能独立反算到毫秒）；人工、命令及模型耗时可逐 event 重算：人工 4 次确认，共 97.097 秒；active 419.651 秒；3 次命令，共 13.317 秒（1.628 + 10.789 + 0.900），成功一次；20 个 llm 事件 218.917 秒。`analysis_planning_overhead_seconds=406.334` 是 `wall-command-human` 的残差，**已经含 LLM 与其它编排，不能再与 llm 时间相加**。节点时间也有嵌套/不同区间，不能汇总成新的墙钟。见 [profiling.py:1754](/public/home/liusiqi/03.毕设/Biomind_v1/execution/profiling.py:1754) 和 `incident_witness.json.historical_summary`。

需要修正旧报告口径：`context_trace` 有 **21 个唯一 request/call ID**，20 个 metrics 事件含 triage 1、Consult 4、Planner 3、Generate 5、Recovery 6、Supervisor 1；第 21 个是 `terminal_summary-0021-98e7373b0c7f`，并非 Supervisor 被漏计。它调用成功、provider receipt 计 input 4,444/output 12/total 4,456 tokens、elapsed 1.812941 秒；metrics 中无此 node 的 llm event。原 metrics token 总计 1,035,909（prompt 995,593、completion 40,316）为该 20 事件口径；加这次终止摘要后可直接核验 **1,040,365 tokens**（input 1,000,037、output 40,328）。只对已归档唯一调用去重，不猜 SDK 不可观测内部请求；不能把 token 当费用。本次终止摘要位于已有总墙钟范围内，不再给墙钟加 1.812941 秒。

`correction_count=0/max_retry_count=0` 不等于没有纠错：[profiling.record_supervisor_verdict:1246](/public/home/liusiqi/03.毕设/Biomind_v1/execution/profiling.py:1246) 只在 Supervisor verdict=RETRY 计 correction。应分别报告：计划候选拒绝 2；Generate 候选拒绝 2；input_check 科学动作正式执行重试 1（两 attempts）；Recovery 提案格式修正 1；2 个 Recovery episode 共有只读工具调用 5，其中有效调查 4、类型错误 1；directive 提交 3 次（1 retry_step 成功、1 blocked 缺字段被拒、1 blocked 成功），不要算作 3 次调查工具。第二 episode `turn_count=4/tool_call_count=3/format_correction_count=1/no_progress_rounds=0`。`runtime_state.budgets` 的部分 counter 只覆盖当时注册的后两次 primary attempt，也不能替代 session execution_records 的 3 次真实命令统计。

Recovery request-0017 为 337,762 字节，0020 为 370,363 字节（数据见调用索引；末次 330,457 个 content 字符）。大上下文成本是可优化线索，不能直接定为根因或推翻独立 Recovery 上下文设计。优先减少可避免的入口错误、格式修正、环境误判；然后基于修复后数据检查 exact selected material/必要依赖闭包、按引用读取与现有 context epoch 压缩，不削减必要调查或科学线程数。

### 并发源码修改边界

分项结束检查确认并发改动集中于 `step_effects.compile_step_effect` 的显式 workspace mutation evidence 校验、`workspace_attestation` 的 Plan 已批准 mutation/Controller 复验、`semantic_completion.evaluate_workspace_mutation_receipts` 的未接纳 receipt 诊断，以及 `bio_agents` 的上述接线。原 prepare_reference `step_effect.workspace_mutations=[]`，bundle 类型为 immutable_artifact，`create_reference_indexes` 在 explicit_side_effects；这些并发改动没有新增索引成员/参考一致性 predicate，也没有修改本项 CLI、managed_stop 或通用 directory predicate。具体 hash/函数行号见 `incident_source_checkpoint.json`。因此本项历史及纯函数见证结论仍成立；当前全应用集成未重跑，不能把旁路未受影响扩大为所有新代码均通过。

## 5. C / D / E 可实施补丁建议

| 补丁 | 真实断点、拟改位置与复用能力 | 修复后验收、兼容与回退 |
|---|---|---|
| C1 终态同步 | `managed_stop._save` 在已有 scope/active attempt 匹配成功后，同步 post-execution terminal 当前 step；复用 set_task_status、原 stop 持久化、CommitStep 已完成事实 | 终止后 current step 无 running；input_check 保留 passed；后续 pending 明确未执行/受依赖阻塞。已有停止历史可只在展示投影归一，避免批量改不可变 attempt。scope 陈旧时拒绝写；回退局部逻辑 |
| C2 原因/取证/路径 | `managed_stop` 的 causal observations/inspection_failures、`cli_lifecycle._cause_text/terminal_text/delivery_text`；以具体执行观测 + bound runtime 诊断区分事实/推断，resolved error 仅留历史；显示 receipt 中本次内部路径 | 原 fixture 被读成功的 evidence 类型错误不再称当前问题；显示 prepare_reference/path miss/未验证已有环境，用户能打开本次报告。不要把模型 blocked 理由变成独立验证；输出保护不变 |
| C3 冲突尽早可见 | Consult/正式输出确定时只读检查目标及 ownership；在批准的输出契约内选择本次目录；复用 managed_stop 内部 fallback 与 publish_report | 旧文件 hash 不变；fixed exact output 未授权改路径仍报告冲突，内部诊断可达；不能以内部归档成功冒充 required 输出交付。新选择走原 Task/Plan/approval，不新建所有权系统 |
| D1 helper 验证收口 | 复用当天已有 script_invocations/绑定/候选校验/执行前重验；本轮无新增生产代码需求，先核并发集成版本 | 对匹配固定 helper 保持 0644 可调用、错解释器/参数/未绑定 delegate 被拒；完整集成再验当前 bio_agents 字节，旧审批重新生成。不能外推解决 bio_wgs |
| D2 协议最小修补 | Generate 共用模板/例子/错误反馈；Recovery 原始 native 参数和 completion metadata 从 provider adapter 保留到 adapt_native_version | 历史两格式错误快速且准确反馈；完整原始 native 漏唯一 schema 常量可适配留痕，其它缺字段/重复键/截断仍拒绝；如无法保留原始证明，保持现有显式 correction。单独提交，可回退模板和适配接线，不全面换协议 |
| D3 科学 bundle 验收（恢复前必做） | Skill capability 明确必需成员及参考身份；生成脚本按契约检查每命令结果；正式 artifact 验收复用现有 validation/CommitStep 扩展能力专属 predicate | 两个 partial fixture 必须不能完整 artifact/Commit；空/部分/陈旧索引均不复用；需要修复后真实小样 FAI/BWA/dict/MarkDuplicates/calling 子命令验证，再授权正式恢复。冻结计划实质变化走 revision+approval；旧产物只以原证据重新验收 |
| D4 可选依赖拆分 | 仅在证明确有并行独立收益后经 Planner 修订 reference/calling 依赖；可提前 ploidy 澄清 | 不承诺现有 map_* 自动解锁，不擅自换 MarkDuplicates 或 caller，不机械拆所有文件。可与 D3 契约一次修订，但 C 与协议修补独立 |
| E 计量与后续优化 | `execution/profiling.py`、terminal_summary 调用统计、候选/执行/提案事件身份去重；保持现有 Recovery 独立上下文/epoch | 21 archived requests 与对应统计可对账；阶段范围、人工等待、重叠耗时、cache token 分列；修复后再比较同等故障的有效调查和 token，不按预算减少来假降成本。建议先计量修补独立提交，环境/helper/协议修复后再做上下文优化 |

未运行项：完整应用导入/真实 worker 恢复（可能触达受限配置及原分析状态）、安装/升级、真实模型 API、真实参考/下游小样、端到端 WGS、Supervisor/CommitStep 的部分索引接受实跑。主要静态和现状见证不代替修复后回归；本轮独立目录只存审计代码和必要摘要，不全量提交原始敏感请求/日志。
