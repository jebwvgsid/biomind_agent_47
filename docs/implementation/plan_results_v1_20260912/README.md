# Plan 与结果验收 V1

本轮按用户统一规格增量实施，起点是 `refactor/consultant-controller-authority` 的 `51da2f7e680aa041dd7d51364e66c3949822f03e`。保留 CLI V2，不回退旧审计提交，不调用真实模型、原 session/业务数据库或科学流程。实施与验收状态以相邻 acceptance.json 为准。

## 当前结果与边界

- RP-1：已接纳的修订正文、模型诊断和记录事实分别进入当前 Planner 实际请求；候选被拒后保留原因，旧批准不抢占当前修订生命周期；累计调用数不退款。
- EV-1：文件收集、正式登记、完成核验分开。文件 identity+path 不套 observation 的 value 规则；显式 accepted=[]、目标环境 UNKNOWN 和检查范围保留。
- PLAN-1：新 Plan 显式声明必要结果；可选缺失与必需缺失分开，无文件任务通过原 CommitStep/FinalAudit 完成。原明确交付仍交由真实原文和 PlanReview 核对。
- PURPOSE-1：正式检查、validation、record_failure 不再单凭标签被排除；下游 Generate 能看到同一已提交步骤的实测观察。合法空文件按显式合同处理，真实诊断、命令失败和损坏内容保持原边界。
- NAME-1：现有 Recovery 选择实际位置或局部复制，经过真实登记、重验、审批与 Commit；完成后保留原计算来源及局部动作自己的完整记录。精确提交验收见 acceptance.json。
- CRASH：冻结 V2 的原 capture 条件再次 SIGSEGV。两个诊断条件独立通过但未取得 fatal 堆栈；没有足够因果证据修改生产代码。整体稳定性仍为 UNKNOWN，不能把 -s 通过称为修复。

## 事故原因与本轮修复的关系

复用 [前轮证据链](../cli_v2_20260912/replan_evidence_chain.tsv) 和 [定点审计](../cli_v2_20260912/replan_causal_audit.md)：当次正式 Plan 同时要求 observation-only 检查能力和正式中间文件；marker 解析与收集接受之后，正式 provenance 仍是 expected，随后完成 gate 不满足。probe 角色跳过登记有源码支持，但历史材料没有逐语句 trace。之后模型确实发出 revise_plan 并被接纳，typed outcome 和图都进入 Planner；Planner 最终请求缺这次修订内容。`value_present=false` 属后续误导投影，不是补一个 value 就能修复的根因。

这不证明第二次执行、无限循环、全机缺工具或用户取消。原记录支持一次执行、没有 CommitStep，以及发起方未知的后续取消；目标环境检查错误应解释为 UNKNOWN。新离线 fixture 不回写历史记录。

## 真实字段与唯一 Plan

| 语义 | 本次字段 | 边界 |
|---|---|---|
| 新必要结果规则 | metadata.result_contract_version=1 | 只在新 Plan 文档明确采用；旧批准快照与既有默认不重写 |
| 必要文件结果 | proposed_artifacts[].artifact_contract.required / task_output_materializations[].required | 新未声明为 false；原始用户要求仍在真实 Planner/Review 上下文 |
| 描述和来源 | 既有 semantic_view.description / description / source_refs | 不新增 must XML、平行 TaskSpec 或要求登记表 |
| 实际位置可选 | path_policy=model_selectable | 既有 Recovery 明确选择后核对；不凭 basename、mtime 或唯一候选猜测 |
| 精确交付名 | path_policy=managed_exact | 原批准位置；Plan 本地 path 仅安全相对路径且不扩大原 managed root |
| 无文件检查 | outputs=[] + 原 produces_observations | 必须有实际、范围明确的证据；负面检查结论可有效，读取失败不可冒充已检查 |
| 用途 | purpose=probe/validation/record_failure | 描述字段；正式资格看批准 Plan/action/attempt，临时 Recovery 诊断仍不能完成原业务步骤 |

当用户指定其它输出根时，必须复用该根的已有获准 output handle/materialization；新相对 path 不授予访问任意目录的权限。可选文件缺失不阻断，不会因此忽略命令失败、无效内容或真实下游必要输入。

`required` 为 false 的结果若被后续步骤作为硬输入消费，编译返回具体候选反馈，由 Planner 修正整份候选，不静默升为必要。旧无版本标记的批准 Plan 仍按原规则读取，既有 hash、快照、批准正文不迁移为新语义。

## 无文件回答与异名结果

无文件任务从原 typed Commit receipt、semantic consumption、validation 和 attempt 的精确关联读取观察值，负值、零和 null 不等同于“没有结果”。答复构造在任务完成 CAS 前执行；构造失败则不记录任务完成。已有终端出口显示事实结论，没有额外旁白调用，也不把内部 metrics 文件当成用户要求的检查报告。带文件、binding 或 workspace 效果的同一步仍保留这些已接纳事实。

异名接纳复用 `RecoveryDirective.result_selections[]`，每项仅含 `artifact_id`、`observed_path`、`operation`。`revalidate + accept_actual` 用于可选择实际路径；`organize_result + copy_to_exact` 沿原 Generate/审批/Execute 做限定的非覆盖复制和内容比较。主计算的脚本、stdout 和 parser receipt 不改；来源仍是原计算 attempt，局部复制另有 action、attempt、审批和 validation。两者在同一次 Commit 中消费，Plan 只推进一次。最后一步通过既有 Commit/consumption 的完整 scope 读取完成结果，不复用“当前步骤”执行入口授予新的执行权限。

仅接纳当前已观察、明确身份、原许可范围内的普通文件；旧文件、错误身份、歧义候选、源文件变化、目标冲突或格式损坏会被拒绝。配套文件需明确列入选择；未新增配套文件猜测器或科学内容语义 binder。现有模型仍负责判断样本、阶段和方法是否一致，离线假响应不能证明真实模型判断的可靠率。

新合成两步异名链的实际计数如下；Recovery 两次调用分别为已有证据检查和提交决定。表中 Generate 是模型调用数，局部复制虽经过 Generate 节点但不再次调用模型。

| 链路 | Consultant / Planner / Generate / Recovery 调用 | 主计算 / 局部复制 / 下游进程 | 审批 | Commit / FinalAudit |
|---|---|---|---|---|
| 接纳实际路径 | 1 / 1 / 2 / 2 | 1 / 0 / 1 | 2 | 2 / 1 |
| 整理为精确名称 | 1 / 1 / 2 / 2 | 1 / 1 / 1 | 3 | 2 / 1 |
| 无文件检查 | 1 / 1 / 1 / 0 | 检查 1；无复制或下游 | 1 | 1 / 1 |

compact、verbose、off 使用固定业务响应，调用、批准、路由和原执行记录等价。TTY/plain 复用既有选定生产回放；没有重做 CLI V2 终端架构或大型录屏包。

## 验收方法

所有模型响应由 fake provider 提供；只在新建 `/tmp/biomind_plan_results_20260912/` 导出中加载合成状态，使用既有严格 audit guard 和无害小表读取/局部复制进程。提交验收从精确 Git tree 的白名单源码导出，避免依赖原 dirty 测试。测试命令、退出、源码清单签名和历史失败分开记录在 acceptance.json；不同条件的通过数不相加。

```bash
python tests/export_plan_results_fixture.py /tmp/NEW_EXPORT --tree FULL_COMMIT_OR_TREE
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/cli_v2_support.py \
  --export /tmp/NEW_EXPORT --results /tmp/NEW_RESULTS -q tests/TEST_FILE.py
```

RP 实际链穿过 native Recovery 接纳、图边、Planner 请求、候选拒绝再修正、原 PlanReview。无文件链和异名链必须分别穿过生产执行、登记、核验、CommitStep/下游；helper 测试单独通过不替代托管链。compact、verbose、off 的调用、审批、原执行与路由以固定语义输入比较；每个进程包装脚本单独核对 hash，不把不同 scratch 路径的包装 hash 说成相等。

## 保护与未解决项

初始 index 为空，工作区有 437 项既有状态。仅本轮明确文件/函数进 index；相交的 node_context.py 与 prompts/manager.py 从 HEAD 加本轮修改形成提交 blob，原控制字段/Hash prompt 修改保留在工作区，不夹带。其余既有 dirty、untracked、删除均保留；没有 push。

CRASH 见 [有界诊断](crash_notes.md)。没有运行真实 WGS，也不据离线 fake provider 断言模型在任意输入上必然作出正确科学判断。用户显式交付的遗漏通过真实原文保留与现有 PlanReview 核验，未新建自然语言提取器自动补合同。

保留的旧路径包括 legacy provenance 的角色迁移与旧 Plan 的缺省义务；旧 `allow_probe_producer` 不再参与新结果 Plan 的资格判断。仍保留实际安全授权、输入/输出范围、非零退出、格式损坏、必要 evidence 与步骤/任务完成核验。原来已可无文件表达的 typed observation、现有 blocked 收尾报告与正式审批没有重写；修复的是通往这些能力的冲突适配和错误单独否决。

旧版本观察不会仅因步骤名相同自动投影为当前版本已接纳事实；跨 Plan 版本的真实结果复用需要已有明确来源/复用授权，本轮未恢复原任务验证。NAME 的本地整理支持明确普通文件列表，不扩大为目录搬迁、跨许可根移动或任意整理脚本。

## 提交与最终回归

IMPLEMENTATION STATUS: **COMPLETE**（五项业务增量与规定的有界诊断均完成）；整体捕获模式稳定性 **UNKNOWN**，CRASH 未确证修复。

BASE：`51da2f7e680aa041dd7d51364e66c3949822f03e`；生产代码 HEAD：`e7f472b6aa8d41472bf6deeaed3540c6c76643d5`；分支：`refactor/consultant-controller-authority`。下表不包含随后单独提交的本审计材料，材料提交 hash 由最终回执返回。

| 切片 | commit | parent | 文件数 |
|---|---|---|---|
| RP-1 | `ba594513560c6c3d3b6052d732511225acf3dbcd` | `51da2f7e680aa041dd7d51364e66c3949822f03e` | 8 |
| EV-1 | `332f56f008c2c4ad781dcfdb9beb54f751c44dc1` | `ba594513560c6c3d3b6052d732511225acf3dbcd` | 5 |
| PLAN-1 + 必要 PURPOSE-1 | `e18753c0d9c0c87cb2dd78e83bc7b3b0f2edaa2b` | `332f56f008c2c4ad781dcfdb9beb54f751c44dc1` | 14 |
| PURPOSE-1 + 无文件答案/交接 | `3245b3015b0d1ed4c44652726ce45ebba1e6e270` | `e18753c0d9c0c87cb2dd78e83bc7b3b0f2edaa2b` | 12 |
| NAME-1 | `e7f472b6aa8d41472bf6deeaed3540c6c76643d5` | `3245b3015b0d1ed4c44652726ce45ebba1e6e270` | 14 |

完整文件清单、每次实际子命令、tree/源码签名、退出、请求片段和选择 receipt 见 [acceptance.json](acceptance.json)；规则来源见 [来源矩阵](rule_source_and_change_matrix.tsv)。提交源码均从对应 Git tree 白名单导出，子进程与父进程独立检查为 unchanged。

最终代码树上的分组回归如下，不与前面各切片验收或 CRASH 诊断运行重复累计：

| 分组 | 结果 |
|---|---|
| name-candidate-results | 31 passed in 68.53s (0:01:08)；退出 0 |
| final-contract-evidence-results | 41 passed in 2.69s；退出 0 |
| final-managed-purpose-results | 23 passed in 68.48s (0:01:08)；退出 0 |
| final-revision-results | 17 passed, 2 deselected in 12.34s；退出 0 |
| final-cli-cp-results | 1 passed in 22.59s；退出 0 |
| final-cli-generate-results | 5 passed in 44.70s；退出 0 |
| final-cli-recovery-results | 1 passed in 17.31s；退出 0 |
| final-stop-budget-results | 4 passed in 4.76s；退出 0 |

两个旧 projection 用例因硬编码越出 scratch 的路径而明确 deselect；未放宽 guard，新恢复交接链仍启用。CRASH 原 capture 四例中仅三例完成后进程 -11；诊断 capture 和诊断 -s 各四例通过，条件独立，均不构成修复证明。失败迭代与未测试范围保存在 acceptance.json。

真实模型/API/WGS：**NOT RUN**；原任务：**NOT RESUMED**；push：**NO**。
