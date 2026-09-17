# §9 故障因果、状态、交付与 CLI 审计

本轮只审计、隔离见证及修改计划；没有修改生产代码、恢复原任务或重写输出。本节中的“见证通过”表示现状行为被复现，不表示故障修复通过。

## 证据与比较范围

- 原 session：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505`。
- 原交付目录：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-GWAS-008/medium`。
- 运行日志：`/public/home/liusiqi/03.毕设/Biomind_v1/logs/biomind_3621046.log`。
- 当前冻结源码：`/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source`；HEAD/overlay 的区分以本轮 `baseline_current.json` 为准，不能把 HEAD 单独等同运行代码。
- 前置实际源码：`/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/prerequisite_source`。
- 持久证据：同目录 `../evidence/failure_recorded.json`、`failure_source_comparison.json`、`failure_witness_results.json`、`failure_current_*.json` 和 `failure_prerequisite_*.json`。原文件路径、哈希、owner、选取字段和见证替身范围都已记录。

当前/前置隔离进程分别复用真实生产函数。原始执行器在见证中仅返回记录；正式产物 reconciliation、TypedExecute、causal packet 构造、current stop 和 CLI 投影没有替身。记录序列化不保留图内 `validation` 字段，见证从实际记录中的契约与观测调用真实 reconciliation，重建同形态 `passed=False/errors` 后交给 TypedExecute；这一重建不是伪称捕获了完整原始图状态。没有实际 shell、模型、API、安装、科学执行或 Commit。合成配置由已有 `tests/offline_managed_bootstrap.py` 提供，实际 config/凭据/原工作区读取在被测进程中被禁止。

## F1：故障链和首次错误码失真（既有缺陷，已证实）

事件身份为 Plan `plan_3a827538750a5159c492` / `plan_v1`，步骤 `check_inputs`，action `action_4ef1b10abd40f94c`，attempt `attempt_79e2ff04f7dcfa103d11`。

| 层 | 记录/真实函数 | 实際事实 |
| --- | --- | --- |
| Execute 进程 | attempt `exit_status.json`、`executions/execution_records.jsonl` | `exit_code=0`、`process_status=succeeded`；stdout 记载本进程 PATH 未找到 bcftools、创建空 VCF 样本文件、最后输出 INPUT_CHECK_DONE；不证明服务器无 bcftools。 |
| 即时失败快照 | `BioAnalystAgent._build_graph.execute_logic_impl` 内 `persist_immediate_failure_snapshot`，当前 `bio_agents.py:50369`、调用 `50644` | `failure_evidence.json.diagnostic_code=VALIDATION_FAILED`，`exit_code=0`。原文件没有被后来阶段改写成 EXECUTION_FAILED。 |
| 正式产物核验 | `managed_artifacts.reconcile_runtime_outputs`，当前 `agent_core/managed_artifacts.py:1883`、拒收点 `1987` | `status=recovery`；唯一 violation 为 `OBSERVED_ARTIFACT_CONTENT_INVALID`，目标为 `vcf.samples.txt`，0 字节。 |
| 执行结果记录 | 当前 `bio_agents.py:50984`、`51080` | `failure_code=OBSERVED_ARTIFACT_CONTENT_INVALID` 已存在；图内 `validation.passed=False`；持久 ExecutionRecord 的 `execution_status=succeeded` 是进程层近似字段，不能用于认定正式步骤完成。 |
| typed adapter | `TypedExecute.run`，当前 `agent_core/workflow/execution.py:1362`、`1375` | `rc=0` 但 `validation.passed=False` 进入现有 `execution_failed` 路由，仍带完整 `execution_record`。summary 使用 “A launched command failed after immutable evidence persistence.”，此处已把执行/核验的文案混称，但尚未产生大写 `EXECUTION_FAILED` 错误码。 |
| 首次大写错误码 fallback | `build_causal_failure_record` 当前 `recovery_projection.py:1611` 调用 `1646` → `_failure_identity` 的 `1228` | 仅执行 `record.error_code or payload.error_code or 'EXECUTION_FAILED'`。本例前两项均空；忽略已经存在的 `record.failure_code` 和 `validation.passed=False`，因此 `FailurePacket.error_code` 首次变为 EXECUTION_FAILED。 |
| Recovery/停止/CLI | packet → `build_inspection_stop` → `build_managed_stop` → `projection` → `terminal_text` | 原始 `origin.error_code=EXECUTION_FAILED`、`origin.returncode=0` 被保留，CLI 因此输出“脚本执行失败，退出码 0”。 |

这里不是完整核验证据丢失。`FailurePacket.diagnostic_facts[kind=producer_failure_index]` 保留 `validation_acceptance=false`、`process.return_code=0`、具体 violation 和 `/execution_record/validation/errors/0`、`/execution_record/runtime_output_reconciliation/violations/0` 两个来源指针。错误在主因映射和消费者文案，不应新建全局错误路由，也不应恢复 Generate 全历史。另一个容易误读的状态是 attempt `EVIDENCE_COMMITTED`，它表示执行证据已持久化，不是步骤 Commit；本例没有 `typed_step_commit_receipts`，8 步中 `check_inputs=failed`，其余 7 步仍为 pending。

前置/当前 `_failure_identity`、`build_causal_failure_record`、`_producer_failure_index`、完整 `TypedExecute` AST 均一致；同一记录输入在前置与当前生产核心见证中都得到上述结果。这是既有缺陷，没有本轮引入回归的证据。`bio_agents.py:51200` 另有后期 diagnostic fallback，但本例 `failure_code` 非空，因此不是本例首次 EXECUTION_FAILED 来源。

## F2：核验拒收成立，不能靠放宽空文件通过（既有有效保护）

真实 reconciliation 重放与原 `runtime_output_reconciliation` 全对象相等，含 fingerprint。所需 VCF 样本列表为空，当前初检没有完成，不应进入样本对齐、关联或科学完成。

必须准确描述契约：样本产物声明的是 `required=true`、`format=txt`、`validation_contract_id=file_exists`、`result_contract_version=1`，不是一个名为 nonempty 的独立契约。现实现仅给 `file_exists` 且 format 属于 `'', file, text, data, output` 的显式普通文件允许空内容，`txt` 不在豁免集合；`_observed_file_metadata` 对普通 0 字节文件给 `content_valid=False`，随后拒收。因此本例拒收符合当前规则且阻止了不完整样本清单冒充完成，但 `file_exists` 名称与格式分支的含义存在解释负担。未来输入/Skill切片应明确样本清单的实际验收条件；不能通过把本例 format 改成 text 或增加 txt 空豁免来“修复”。合法允许空文件的现有协议仍须保留。

本节不把所有 stdout 断言视为科学证据，也不认定 Q 的 215 行证明样本顺序。具体输入误读原因由输入审计主线提供。

## F3：Recovery 停止触发独立保存，但阶段/量值没有进入简洁 CLI（既有缺陷/覆盖不足）

日志 `5648–5658` 保留真实异常调用链：`TypedRecovery._run_investigation` → `RecoveryAgentLoop.ainvoke` → `project_recovery_seed_for_provider` → `RECOVERY_SEED_CAPACITY_EXCEEDED: chars=32097 limit=32000`。后续 outcome 是 `internal_protocol_error`，该阶段没有发出 Recovery 模型请求或工具调用；之后 `terminal_summary` 在日志 `5790` 单独发出了一次解释请求，不能把它算成 Recovery 调查，也不能说本 session 全程没有模型请求。完整预算原因由预算主线复核。

持久 stop 同时保存：

- `origin`：原始 attempt / rc0 / 当前被错误映射的 EXECUTION_FAILED。
- `stop_reason.error_code=RECOVERY_SEED_CAPACITY_EXCEEDED`、`detail='chars=32097 limit=32000'`。
- `stop_reason.model_turns=0`、`tool_calls=0`；日志和入口源码提供“本次进入 provider 前停止”的独立证实，不能仅靠缺失 audit 默认成 0 推断。

原始起因与停止触发在存储上没有相互覆盖。缺口在于：

1. `recovery_node.py:1750` 的局部 `fail` 在 exception 时只写 code/detail；`inspection_stop.py:274` 期待的 `failure_stage` 因而为空。
2. `inspection_stop._execution`（当前 `167`）主要读 completion report / validation evidence，未消费已经有的 execute producer rejection index，故本例 `validation_status=unknown`。事实本来足以知道核验已拒收。
3. `managed_stop.build_managed_stop` 当前 `555–567` 只有已采纳的历史 receipt 或 `model_turns>0` 才建立具体 usage scope；首次入口失败得到 `recovery_usage.scope=unknown`。保持“未知全会话累计”是对的，但缺少明确的本次准入阶段信息。
4. `cli_lifecycle._stop_text` 当前 `134` 没有容量错误的映射；简洁 CLI 只显示通用停止提示和错误码，不显示 seed、32,097 字符、32,000 字符上限。`_cause_text` 也没有核验失败分支。

旧版 `_stop_text`、`inspection_stop._execution` 和 `build_inspection_stop` AST 与当前一致。此前合并修复已改善 exact attempt 终态、历史 inspection 错误解除和显式 `BIOMIND_EFFECT_EVIDENCE_JSON` 中的 missing_commands，但本例是 tabular stdout 和产物核验错误，不能声称旧修复覆盖这条 GWAS 通用路径，也不能把受限覆盖说成新增回归。

## F4：三份停止报告真实交付，GWAS 未完成（当前已实现；再入所有权有证据限制）

| 文件 | 当前字节 | 当前 SHA-256 |
| --- | ---: | --- |
| stage_status.tsv | 1,028 | `1d3e17a1f8eb28f8fb9fd3da7e0e1ae98ab738cca002967d02a63734fcbc29ab` |
| reproduce.sh | 947 | `1275ce3763117ab1e281d81e87158a4067d6130f06a89dd56f3a9643074d9229` |
| report.md | 4,963 | `9de89cfce59e8316cd472870c3a02cd221a5a7b4d373be8cb5ccdca8b0d2f2df` |

三文件都是普通文件，内容匹配持久 `frozen_report_bodies`，哈希匹配 `report_receipts.status=created` 与 `report_publications.status=published`。publication owner 是本次 `managed-stop:a1c497e60c109ec6d00689d8f6d32539232486562d8b79e17faba15b70b06307`，task/stop/operation_id/authorization_ref/body_ref 关系一致。publication 的 `prepared` 与最终 receipt 的 ownership 对象完全一致。不是旧文件受保护导致报告未交付，也不是 writer 未进入。

**现场所有权复核的限制**：当前 stat inode 与三个历史 receipt 相等，但当前审计进程设备号是 49、历史 receipt.device 是 45。`ownership_matches=false` 已保存在证据，原因尚未证实；不能宣称当前原路径 writer 再入会通过。未调用原 writer、未删除或修改任何文件。临时 `.runtime-report-proof-*` 已不存在，符合 `_write_owned_report` 当前 `177–190` 在 durable published 后用已确认 receipt 取代并清理临时 proof 的实现，单凭 proof 不存在不能判未交付。

`report_delivery_status` 正确给 complete；`scientific_completion=not_completed`，步骤 Commit=0。CLI 显示当前三报告绝对路径。`reproduce.sh` 是禁止科学重放的停止说明，保存原 executed.sh 引用并以失败退出，不是成功 GWAS 的复现流程。冻结正文内较早的“文件交付：not_attempted（截至本次说明生成时…最终交付以持久 receipt 为准）”不覆盖后来完成的 receipt。当前文件交付成立不证明科学结果、样本映射或关联分析成立。

## S5：最小可实施修改切片（本轮未实施）

当前具备决定修复范围的证据；不需要等原任务恢复或真实模型调用才能改错误映射。S5 可与预算 S4 并行设计/局部验证，`recovery_node.py` 的 fail 边界由一名集成者串行合并。无需新增 action 类型、全局 schema、安装权威、binding 规则或图边。

| 函数级位置 | 修改行为与接线 | 保留/兼容边界 |
| --- | --- | --- |
| `agent_core/workflow/execution.py::TypedExecute.run` | 继续用合法 `execution_failed` 路由；把 rc0+validation false 的 summary 改为进程已正常退出、核验拒收。必要的显式 error_code 透传只用现有 payload 字段，不替换旧 outcome enum。 | 非零失败、诊断、readonly/simple_execute、取消、安装请求及 prelaunch 分支不扩范围；没有外部科学依赖的动作不新增 binding 门槛。 |
| `agent_core/workflow/recovery_projection.py::_failure_identity`、`build_causal_failure_record` | 使用同一 exact execution record 中已存在的显式错误、`failure_code`/结构化拒收原因；若明确 validation false 而无具体码则 `VALIDATION_FAILED`；只在无更具体结构化错误时保留兼容 fallback。summary 由已验证拒收记录表达，而非固定“A launched command failed”。复用 `_producer_failure_index` 的明确生产结构和指针，不扫描自由日志猜测。 | 不改写旧 immutable packet、attempt evidence 或哈希；已有历史 `EXECUTION_FAILED` 保留原记录，读侧只能基于同作用域结构化证据补充解释。source/attempt/incident/version/authorized refs/CAS 约束不变。 |
| `agent_core/workflow/inspection_stop.py::_execution` / `build_inspection_stop`，`managed_stop.py::build_managed_stop` | 在原来源为 execute 且同 attempt 的情况下消费现有 `producer_failure_index.validation_acceptance`，显示已失败而非 unknown；将原始核验原因与停止触发分别投影。 | 不因 rc0、完整报告或局部核验就置步骤已提交；completion gate 和 Commit 继续唯一决定科学完成。保留 exact ownership、已提交成果和当前/历史取证分类。 |
| `agent_core/workflow/recovery_node.py::_run_investigation.fail`、`_inspection_stop_result`（与 S4 集成） | 复用 `terminal_error.failure_stage/detail` 与既有 episode audit，输出当前被计量对象、数量、单位、上限及准入阶段。只有调用阶段事实证明未入 provider 时显示本次 0；不把默认缺值补成已知累计 0。预算具体准入/压缩逻辑归 S4。 | 独立 Recovery System/局部上下文不变；既有原始故障和已有 terminal_error 不互相覆盖，后续归档失败仍是 secondary failure。 |
| `agent_core/cli_lifecycle.py::_cause_text`、`_stop_text`、必要的 `terminal_text` | 核验失败显示“进程退出码 0；产物核验未通过（具体目标/错误码）；步骤未提交”；容量停止显示“Recovery 固定 seed 在首次请求前为 32,097 字符，上限 32,000 字符”。使用实际结构化计量；旧 receipt 缺阶段时如实未知。 | 保留 delivery_text 的真实成功 receipt 路径和 protected/internal 逻辑；不刷完整内部事件，不从 raw stdout 反推全环境状态。 |

实施顺序：先修 causal producer 映射及 typed 文案，再接既有 stop/CLI；与 S4 约定阶段、计量字段由谁提供。S5 不需要改 `bio_agents.py` 的科学执行器；若 S4 因预算捕获必须改该共享大文件，由同一集成者一次合并，禁止两人分别重写核心闭包。无需重做已有 report publication、helper、binding 失效、Installer 人工授权、generation epoch 或 exact ownership 机制。

未来验收采用本轮见证作修复前回归基线，新增有意义的修复后断言：

1. rc0 + 实际空必需样本文件 → 真实核验拒收，typed 路由仍进入 Recovery，主因保留核验错误；Recovery 容量失败不得覆盖主因；0 Commit。
2. rc非0 的真实进程失败仍准确显示执行失败；无输出/不依赖科学工具的合法动作保留；明确允许空的 file_exists 文件仍按既有契约处理。
3. 同 scope 的 producer index 确认核验失败时 stop/CLI 不说 unknown；旧缺证据 receipt 仍说未知；另一 attempt/已终止旧 episode 的 index 不可挪用。
4. 首次准入超容量与后续工具增长超容量都显示当前阶段/对象/单位，分别来自 S4 真正的请求计量；压缩失败和模型/工具调用次数不得伪造。
5. 核验失败 + Recovery 入口失败 + 三报告成功落地，在隔离目录调用真实报告 writer 后文件/hash/owner一致，CLI 同时给出原始核验失败、容量停止、未 Commit、file delivery complete。另保留旧文件保护与内部报告 fallback 的原回归。
6. 复用 `tests/test_unified_terminal_facts.py`、`tests/test_failure_packet_incident_lineage.py`、现役 typed outcomes/Recovery projection/managed stop/报告发布回归；具体冻结名单由最终统一验收选择，不能把旧 WGS 总数当本路径证据。

建议独立逻辑提交范围：S5 的上述生产文件与对应定点回归/文档；S4 共享 `recovery_node.py` 只由统一集成提交一次。本轮仅提交审计文件由 root 处理，本子任务不 git commit。将来若失败，撤回该逻辑提交，不回写原 session、旧 evidence 或已发布报告；不要连带回退已实施 binding/helper/Installer/CLI 保护。

## 本轮验证结果与未证明项

当前冻结源码 3 个现状见证通过（1.14s）；前置源码 2 个同输入见证通过（1.11s，CLI 当前投影用例未在前置运行）。两个进程均 `SOURCE_UNCHANGED True`，生产导入文件 hash 与各自源快照匹配。原 session 及三报告合计 160 个受保护文件在见证前后哈希不变。日志及行号是当前冻结源码/当前有效运行日志定位；session 未记录完整源码 manifest，因此 AST/函数行为对照证明缺陷在前置/当前存在，不把它夸大成逐文件证明运行进程的精确源码身份。

未证明：真实模型恢复、真实安装、原 writer 跨设备号再入、原任务重跑或 GWAS 科学 E2E。它们未运行，且不影响上述有限错误归因修复的实施决策。报告交付成功不能证明这些效果。

## 从持久快照重新复现

完整入口为 `witnesses/run_failure_witness.py`，不依赖前述已经存在的 `/tmp` 候选或 session 副本。它从 `failure_witness_results.json` 中记录的持久 `current_source` / `prerequisite_source` 重新分阶段复制源码，排除 config、startup、Skill、原测试和符号链接；使用实际仓库中哈希已锁定的 `tests/offline_managed_bootstrap.py` 与未改动的 `failure_witness.py`。每份旧见证实际导入的源码都先核对原哈希；输入快照重新从只读 session 捕获并核对已记录文件哈希。源码或 incident 变化会显式失败，而非拿新版本冒充原见证。

在仓库根目录执行本轮实际使用的完整命令：

```bash
python docs/audits/gwas_env_recovery_20260917/witnesses/run_failure_witness.py --output docs/audits/gwas_env_recovery_20260917/evidence/failure_reproduction.json
```

这条命令已完成，当前 3 项、前置 2 项现状见证全部通过，两个子进程 `SOURCE_UNCHANGED True`，160 个原 session/报告文件哈希不变。证据包含精确 `command_argv`、从空环境构造的 `clean_environment`、完整候选哈希清单、测试结果和函数见证结果。已有输出受 `open(..., 'x')` 保护；再次运行应省略 `--output`（自动输出到新的临时目录），或提供新的审计证据文件名，不覆盖本轮记录。这次重新运行的 3/2 与此前 3/2 是同一批见证的复验，不能加成 10 个独立用例。

仅检查隔离建树与源码身份、不运行被测模块时可执行：

```bash
python docs/audits/gwas_env_recovery_20260917/witnesses/run_failure_witness.py --stage-only
```

每次都会创建新的 `/tmp/biomind-gwas-failure-reproduction-*`。默认被测解释器为已存在的 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`；可用 `--python` 指定已有兼容解释器，脚本不安装任何依赖。launcher/capture 只使用标准库；BioMind 仅在隔离 bootstrap 子进程中导入。替身仍仅是记录执行器返回及 bootstrap 的惰性配置/禁用环境自动探测；真实核验器、typed adapter、因果 packet 构造、现有状态投影及 CLI 全部保持原样。复现不触发任何真实命令、模型/API、安装、科学执行或原报告 writer。
