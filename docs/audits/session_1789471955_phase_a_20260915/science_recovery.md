# 阶段 A：实际进度与 prepare_database / Recovery 因果链

主会话：`session_1789471955`。仅使用其历史原始 JSON、回执、请求与响应；以下模型说法均作为待检验陈述。时间表为北京时间。详细逐执行表见 [evidence_science.tsv](evidence_science.tsv)，字段与精确 JSON pointer、文件 SHA-256 见 [evidence_science.json](evidence_science.json)。原始证据根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955`。

## 这次实际做到了哪里

只完成了输入存在/可读性检查及 contig 名称兼容性检查；两项均有 typed Commit。之后 `prepare_database` 执行四次，均进程退出 0，但没有一次完成 Commit。`build_database`、`annotate_variants`、`summarize_ann`、`finalize_outputs` 全为 `pending`。六次执行回执的 `duration_seconds` 总和仅 **8.072 秒**；这不是会话总时长，也不含核验器或模型耗时。

首项记录三个输入可读且非空；第二项的独立 contig parser 记录 VCF header/body、FASTA、GTF 名称均为 `{chr1, chr2}`。这证明命名兼容，不能证明相同 assembly、注释质量或 snpEff 数据库可用。本次审计未重新读取科学输入。

| 执行账本行 | 时间 | 操作 / attempt 尾缀 | 回执结果 | 最终提交 |
|---|---|---|---|---|
| 1 | 19:37:03–04 | check_inputs_readable / `1a07c13b471c90d37154` | rc 0；效果验证通过 | `commit-step-abdbec7a1427502c6813d322` |
| 2 | 19:37:51–53 | check_contig_compatibility / `2808a853340abef30084` | rc 0；独立 parser 与 gate 通过 | `commit-step-48f494e8a6e44d98773dcde3` |
| 3 | 19:38:42–43 | prepare #1 / `010b75bc2bd8ce10d788` | rc 0；三个工作区 postcondition 均 unsatisfied | 无 |
| 4 | 19:40:06–09 | prepare #2 / `37bce55ef62db92f86f1` | rc 0；attester、确定性 validation、gate 均通过；语义审查 inspection_required | 无 |
| 5 | 19:41:35–37 | prepare #3 / `107cc990faf80ec9d36a` | 同上 | 无 |
| 6 | 19:44:53–56 | prepare #4 / `fad0ac69fea46d1d3b0f` | 同上 | 无 |

当前 provenance 中 `annotated.vcf`、`ann_summary.tsv`、`report.md`、`reproduce.sh`、`stage_status.tsv` 都只是 `@expected`，`producer_attempt_id`、`validation_id` 为空。没有本次生成/核验/交付回执。共享输出目录里即使有旧同名文件，也不能归作本次交付。`snpEff.config` 有三条 materialized 记录，亦不等于整个 prepare 已提交或数据库已构建。

## 四次 prepare 为何发生

设正式工作区根 `W = .../plan_artifacts/snpeff/data`；该会话批准契约规定 `data_root = W/data`，后置条件也是相对 W 的 `data/`、`data/genomes/<gv>.fa`、`data/<gv>/genes.gtf`。因此字面 `.../data/data` 在本次契约中是预期位置，不能按目录名重复直接判错。

1. **第一次是已证实的生成动作与契约不一致。** 文件写到 `W/genomes/...` 和 `W/<gv>/genes.gtf`；attester 检查 `W/data/...` 并记录全部缺失。证据为 `attempts/attempt_010.../mutation_intent.json#/receipts/0`，receipt `workspace-receipt-cb788b5cee08db580b4695d9`。首个 FailurePacket 将其笼统标为 `execution_failed`、`EXECUTION_FAILED`，但物理进程实际 rc 0，真实失败在运行后效果认证。
2. **Recovery #1 修正目录，却提出了不一致的 config 指令。** `recovery_turn-0021` 要求文件放 W/data 下，却要求 `data.dir` 指 W；`generate-0022` 照做。随后布局后置条件通过。`supervisor_reviewer-0023` 仍要求补上游 contig、绑定值和 config 内容等证据，遂进入 Recovery #2。
3. **Recovery #2 选择了不能修复审查投影的重复执行。** `recovery_turn-0026` 明言依赖检查已通过、绑定/访问取证问题不能靠重跑消除，同时又提出同动作重跑一次并完成提交。`generate-0027` 再复制同样文件、写同样 config；#2 和 #3 的 stdout SHA-256 完全相同，实际执行回执却是两个独立 attempt，后者复制导致 mtime 再变化。语义审查再次 inspection_required。不能把它写成引擎自动重放同一物理请求：有新的 Recovery 指令、新 Generate、新动作和新执行。
4. **Recovery #3 修正 config 与补充可见证据；随后又被相反的布局判断卡住。** `recovery_turn-0030/0031` 提出 `data.dir=W/data`、补配置行和元数据，`generate-0032` 产出新标签 `tasklocal_chr1_chr2`，#4 执行后 config 实际读回也是 W/data。`supervisor_reviewer-0033` 却称该路径比 `<snpeff_db artifact>/data` 深一层。原始请求同时包含 W 的正式路径和 `data_root: "<snpeff_db artifact>/data"`；字符串代入及回执可反证这个具体理由。

**必要限界：** #2/#3 的 config 与本轮新复制位置不一致是事实，但首轮 W/genomes、W/<gv> 文件仍留在 attester 快照中。故 Recovery 声称“构建必然找不到参考与注释”也超过证据；本次从未执行 build，不能判断真实 snpEff 运行成功或失败。#4 的路径与批准契约一致，同样不能外推科学数据库可用。

`RUNTIME_ACCESS_EVIDENCE_MISSING` 四轮都可见；后三轮同时有 `WORKSPACE_MUTATION_RECEIPT_VALID`、`postcondition_satisfied=true`，所以它不是后三轮确定性否决原因。它表示访问取证缺口，不能反向推断文件未读取。

## 绑定和上游证据投影：有原始证据支撑的增量缺陷

### 绑定值存在，却在审查投影中显示 false

四份 Generate 原始响应都提交了 `genome_version` 的 string value 和 `snpeff_db` 的 public handle。`plan_state.json#/provenance_state/actions/<action>/normalized_action/produced_bindings` 中还保留了系统规范化后的真实 binding values。后三轮 CompletionReport 则展示：

```json
"produces_bindings": [
  {"identity":"genome_version","value_present":false},
  {"identity":"snpeff_db","value_present":false}
]
```

当前源码定位：`agent_core/semantic_completion.py:1280` 的 observed envelope 只合并 execution 与 validation.effect_results；`1324–1331` 对所有非 artifact effect 按是否含 `value` 投影。执行摘要的 binding rows 是 candidate 元数据，省略 value；workspace receipt 也不是 observation value 协议，统一 false 会误导。`agent_core/completion_gate.py:618–630` 则另外读取 immutable action 的 produced_bindings，且注明它们只由 CommitStep 晋升。因此 gate PASS 与审查 false 的来源不同；**不等于 Generate 没填绑定，也不等于绑定已提交**。

### 已 Commit 的 contig receipt 没有送入三个后续审查请求

对 `supervisor_reviewer-0023/0028/0033/request.json` 全部 messages 的离线匹配：独立 parser receipt `contig-receipt-319394754435026b86261be2892fa388502bbd0c1afaecf6cdc876d9f462215b`、validation `validation-1cbb27e5219f0bdb5bd6`、typed Commit `commit-step-48f494e8a6e44d98773dcde3` 均为 **0 次**。审查能看到依赖名称和成功条件，缺少其已验收值的可消费投影。模型因此说“本次有界审查中不可见”有事实依据；将此等同于任务未检查则不成立。

当前源码：`supervisor.py:2370–2405` 构造当前步 CompletionReport 并传入代码/stdout；`agent_core/semantic_completion.py:1240–1433` 构造当前步效果/回执内容。后续 `agent_core/workflow/verifier.py:231–265` 把 `inspection_required` 转成 follow-up outcome、`semantic_completion_rejected`，并把 concerns 放入 `contract_violations`。这解释了确定性通过却进入 Recovery 的真实控制路径。`contract_violations` 中的自然语言仍是模型审查意见，不是新增机器事实。

这些位置是**当前工作区源码对应机制**；运行时加载版本是否逐字相同需结合主审计 runtime identity，不能仅用当前 HEAD 归责。

## Recovery 回合与反馈生命周期

`context_trace/events.jsonl` 的行号为物理行，已写入证据 JSON。

| Episode / failure 后缀 | 开始行 | 模型回合 | 工具内容 | 指令 |
|---|---:|---:|---|---|
| `0e4e660a608aeee9ddf57acb` | 80 | 7 | failure_inspect；6 段 evidence_read | 行 152 采纳 retry_step，修目录层级 |
| `bde41c5ef3f698d27c5911d6` | 153 | 3 | failure_inspect、artifact_inspect、artifact_read | 行 185 采纳 retry_step，重复同动作 |
| `23597e056ad0db9837e4c6df` | 186 | 3 | artifact_read、artifact_inspect；一次缺 schema_version | 行 207 拒收 STAGE_FIELD_REQUIRED；215 候选通过；216 采纳 retry_step |
| `c3df4b71c64f6e50c32af13d` | 217 | 2 | 行 291 failure_inspect；294 config 实读；384 skill_search(data.dir) 返回 NO_MATCH | 无第 4 条采纳指令、无第 5 次 prepare；后续压缩/取消详见主审计 |

三条指令均可在 `task_state.typed_recovery_receipts` 对应到不同当前 attempt/FailurePacket。Generate 0022、0027、0032 的实际请求分别包含当轮 reason_code，不含前一轮 reason_code；没有证据支持“始终使用首轮旧反馈”假设。第三轮缺 schema_version 后，在同一 causal failure 内修正并成功采纳，未替换科学失败身份。

`task_state.causal_failure_packet_lifecycle` 共四次 active、前三次在 `candidate_repair_required` outcome 提交时标 resolved。这是“修复指令已提交”的生命周期用语，不能当作步骤科学完成。三条 budget receipt 分属三个 incident，每个 ordinal 1 / limit 2；本次未命中同 incident 次数上限。建议后续评估稳定的步骤/证据缺口 recurrence，而不能凭本次记录宣称存在无限执行循环。第 4 episode 的后段没有新增科学执行。

## 最小修复切片建议（本轮未实施）

1. **审查证据投影。** binding 由不可变 normalized action 投影 typed candidate value/type/source；与 accepted binding 明确区分。workspace mutation 展示 receipt/attestation 状态，不套 observation 的 value_present。仅按声明依赖附带已 Commit 的 upstream observation/parser/validation refs，避免全量历史再膨胀。离线验收：本次三份报告的绑定与 upstream 矛盾消失，仍不能在未 Commit 时暴露 accepted 值。
2. **配置与布局一致性。** 围绕明确 W、契约 data_root、config data.dir、文件目标构造小型确定性事实/检查，再交语义评审。覆盖 W 本身名为 data 的案例、首轮旧文件残留、多次相同路径、配置值与新暂存位置不同。不要根据字符串 `data/data` 特判通过或失败。
3. **审查要求补证据与动作重试边界。** 对仅缺投影的 inspection 优先使用既有回执/受控读取再核验；新动作须指明可以改变的缺陷或新证据。保留真实布局/config 修改的 retry，抑制为“提交 candidate binding”而原动作重跑。跨 attempt 的不变证据缺口需要 recurrence 账本；不能只在易变化的 incident 内比较。
4. **失败语义与终态交付。** 区分进程成功/效果认证失败、确定性通过/语义待核验；取消输出应消费本次 Commit/执行回执生成部分进度，不能把 lifecycle resolved 或旧共享文件当完成。具体取消与 writer 行为见主报告。

已复用已有 Generate 缺陷类别修复 `5bc79f2` 的检查边界：它修 executable carrier、binding_env 形状说明、候选纠错和终态诊断，没有修改上述 CompletionReport 值/上游投影与 Verifier 语义控制点，不应重新实施同一 Generate carrier 修复。`session_1789456793_review_20260915` 的 helper 案例属于另一会话，未用于解释本次根因。对照 `session_1789464978` 仅确认两次执行、两次 Commit，prepare 未执行通过；与主会话四次 prepare 的路径事实分开处理。

## 离线核验与缺口

`python docs/audits/session_1789471955_phase_a_20260915/verify_science_history.py` **PASS**。仅 Python 标准库读取白名单历史 JSON，验证六次执行/两次 Commit、candidate values 与 false 投影矛盾、上游 receipt 未送达、最后布局代入一致、旧文件残留和无本次最终交付回执。它不 import 生产模块，不读取科学文件，不执行科学脚本，不调用模型/API。运行后只写同目录 `evidence_science.json/tsv`。

未证明：真实 snpEff build/annotate 结果、参考与注释同一 assembly、本次数据库可加载、所有访问行为已独立取证。缺少本次下游执行本来就是历史事实，不能真实重跑补造。当前源码归属与压缩/usage/取消证据由主审计另行核对。
