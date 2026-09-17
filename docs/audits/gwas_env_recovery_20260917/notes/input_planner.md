# 正式输入、初检脚本与 Planner 拒收

复核版本、原始文件身份及可重放方法见 `evidence/input_planner.json`、`witnesses/input_planner.py`。本节仅分析正式输入，未分析其他 benchmark、隐藏答案、旧图或科学测试夹具。

## 已取得的证据

| 对象 | 观察 | 不能推导的结论 |
|---|---|---|
| `Real_snp.vcf.gz` | Python 标准库 gzip 只读至 `#CHROM`，215 个不同 ID；独立 bcftools query 得到同一列表哈希 | 未检查全部变异记录、完整压缩流或 marker QC |
| `Real_trait.C16_0.table` | 215 行、每行两列、第二列均可解析为数值；首行是首个 VCF 样本及其表型值，无表头；ID 集合与顺序均与 VCF 一致 | 数值格式本身不证明全部生物学背景、缺失编码或建模参数 |
| `Real_snp.3.Q` | 215×3、全部数值、没有样本 ID 或生成顺序记录 | 不能由行数相等或邻近文件同名推断 Q 行顺序；关联仍缺条件 |
| `Real.gff3` | 本轮仅核实存在及文件大小，原脚本读取开头三行 | 未验证参考版本、染色体匹配、候选区间或注释科学结果 |

四个输入的绝对根为 `/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part6.GWAS/data`。仅列举该目录的直接文件名以查找显式来源说明，没有把相邻 `.fam`、其他表型或其他目录当成新的正式输入。四个给定文件未提供 Q 行与样本的映射来源。因此“Q 顺序未确认”是此次可证明的边界，不能声称服务器任何位置都没有来源。

## 错误第一次出现在哪一层

1. 实际 Consultant 只有 Skill 搜索、加载、选择和提交 handoff，没有正式文件内容观察。`consultant-0005-cd07abb1e4ce/response.json` 的 handoff 已要求“比对…Q 矩阵样本 ID”，同时又把“Q 矩阵列数与样本 ID 列”保留为未决事实。它没有证明 Q 已有 ID。
2. 首个 Planner 候选把 `plan.q_samples` 声明为必需文件，“从 Q 矩阵提取的样本 ID 列表”；把 ID 对齐排在询问 Q 列语义之前。这个计划过早假定了输入结构。三个候选都保留了此假设。不能把“计划批准”当作输入语义已验证。
3. `generate-0009-bd793155d91b/request.json` 没有实际首样本或 Q 首行数值，包含完整 Skill 中的警告：`Q matrix lacks sample column → covariates cannot be sorted safely → paste validated sample IDs before use`。所以不是 Skill 指示“纯数值第一列就是 ID”，也不是运行结果被正确观察后才判断的。
4. 具体丢首行的实现首次出现在实际生成的 `approved.sh:65`：`awk 'NR>1 ...'`，并在第 76 行把首行输出为 `PHENOTYPE_HEADER`。`approved.sh:66` 无条件提取 Q 第一列为 ID。后面的 `head` 发生在解释和写结果之后，没有先观察再判断。不能由缺少 provider 中的原始行，机械要求 Consultant 读取所有科学文件；可由合法初检阶段产生结构事实。
5. 现有 `skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py:16` 的 `read_table()` 支持本例无表头表型，隔离调用保留 215 行；本次 Generate 没使用它。该 helper 本身会把原始数值 Q 的首列当键，且 `main():143` 仅警告缺样本、后续可填 NA，因而也不能在没有验证样本身份时盲用它。此为相邻既有风险，本 session 未调用 helper，不能列为直接原因。

以上前置源码与 GWAS Skill/helper 在保存的修复前快照中已有；本次具体模型输出属于已证实的现场缺陷，缺乏同条件旧模型重放，不能仅按发生时间认定由 binding 提交引入。

## 空必需产物及只读边界

实际脚本 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505/attempts/attempt_79e2ff04f7dcfa103d11/approved.sh:52` 在 `bcftools` 不可从当前 PATH 找到时进入 else，`: > "${VCF_SAMPLES}"` 创建空文件。`set -e` 不会把打印 `EMPTY_ARTIFACT` 变成错误；末尾成功的 printf 使进程退出 0。这是显式脚本行为。

隔离见证使用原批准脚本，仅替换两个输出目录，PATH 内只放真正的 mkdir/stat/head/cat/awk/wc。没有运行科学工具，没有伪造版本或样本。得到 rc=0，四份检查文件的 SHA-256 均与历史现场相同：VCF 样本 0 字节、表型 214 行、Q 215 行、依赖表全部 missing。见证成功指复现缺陷，不能写成修复通过。

正式 validator 拒收空样本文件是正确边界，详见 failure_delivery.md。实际契约名称为 `file_exists`、format 为 `txt`，不要把它描述成显式声明了名为 nonempty 的合同；当前实现对该类型检查了非空。本例必须保留非空及样本身份验证，不能扩大“空文件存在即完成”的豁免。

轻量替代已由本轮审计证明可行：Python gzip 读取 VCF header 不依赖 bcftools；现有 bio_gatk bcftools 也能只读列出样本。但本轮没有将任一方法写入原动作。未来应在初检候选中选择并验证适用的读取办法，或者产出“未完成/阶段依赖阻塞”观察，不能用空样本列表代替结果。

步骤标题的“只读”指正式科学输入：四个 resource_uses 均为 read。最终 Plan 同时声明 `00.input_check`、`logs` 和三个样本文件输出；实际审批也展示了将创建目录和提取结果。因此不能仅凭标题认定写入越权。初检最小事实可用已有 observation 协议表达，并不要求所有只读动作写文件。

## Planner 前两轮的窄范围复核

终端只显示前几个错误码，实际第一轮有 **12 个 issue**：1 个 ask_user 无可解释效果，7 个模型自己声明为 required 的目录没有 producer，4 个执行能力 ID 使用了 `/` 形式而非 Skill 提供的规范点号 ID。任务级 report/reproduce/stage_status 当时已由最终报告步骤覆盖；这些拒收不是强迫每个输入步骤增加文件。

第二轮 `explicit_side_effects=["user_decision_request"]` 不是该通用 capability 声明的效果，也没有内联证据合同。第三轮加入 `question/requested_slots/answer` 的 typed evidence_contract 后接受。使用真实 `compile_step_effect()`、原三份 ask 步骤和空能力注册表的隔离见证依次复现 `PLAN_STEP_EFFECT_UNSPECIFIED`、`STEP_EFFECT_PROTOCOL_INVALID`、无错误；它不是整个 Planner 重放。第二轮 `complete=true` 仅表示效果非空，仍带阻塞 issue，不能当作准入通过。

三个实际 Planner 请求均明确说明“不为只读检查虚构文件”和“内联副作用须有 typed evidence_contract”。因此当前证据支持保留这些合理检查；提示样例可做后续易用性改善，不以重试本身证明 validator 有缺陷。这两轮都在批准之前解决，与此次执行后停止无直接因果关系。

## 对修改计划的约束

- 在已有初检/Skill 能力中形成有来源的结构观察：表头存在性、ID 列、重复/缺失 ID、Q 是否带 ID、顺序依据。先观察，再声明或生成对齐后的科学输入；不硬编码 215 或某工具名。
- 复用现有 helper，但其无表头识别不能被描述为适用于一切表头；需要显式列选择/可解释的观察和不确定状态。Q 没有可信映射时进入阶段阻塞/现有 ask_user；本轮不向用户询问科学参数、不恢复原任务。
- 生成阶段消费正式观察和当前阶段依赖；没有满足必需样本产物的能力时明确失败/阻塞。保持 validator，不能增加通用脚本语义解析器替模型推断完整逻辑。
- 未来回归同时验证原始无表头表型、带表头表型、重复或不匹配 ID、无 ID Q、已提供可信映射 Q、缺少读取工具和轻量只读办法；只用正式输入验证本事故事实，合成数据仅用于软件边界测试。
