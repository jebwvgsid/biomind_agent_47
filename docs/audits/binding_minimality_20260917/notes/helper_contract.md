# 固定 helper 调用、直接身份检查与错误纠正审计

审计范围：Q3、Q6 与 Q7 中 helper 相关部分。仓库根路径为 `/public/home/liusiqi/03.毕设/Biomind_v1`；下列 `根/` 均指这个绝对目录。生产源码、Skill、提示词、配置未修改。本轮仅运行无子进程、无模型、无 API 的 13 项隔离函数见证；既有 69 项回归结果为核验复用，未重新执行。见证脚本为 `根/docs/audits/binding_minimality_20260917/evidence/helper_witness.py`，结果为同目录 `helper_witness_results.json`。

## 1. 版本及上一轮回执

已证实：回执 `根/docs/fixes/skill_script_invocation_contract_20260917_results.json` 所列六个 SHA256，均与当前工作区及 `/tmp/skill-invocation-validation-nm1jgfjk/candidate/` 的现存相应文件完全一致。六文件为 `agent_core/skill_script_invocation.py`、`agent_core/skill_resource_binding.py`、`bio_agents.py`、WGS `SKILL.md`、`tests/test_skill_script_invocation.py`、`tests/test_preflight_mapping_commit.py`。完整 hashes 在见证结果 `prior_six_file_hash_verification`。

其中两个新增文件当前仍是 untracked；四个是 tracked modifications。`git diff --stat HEAD -- <六文件>` 仅覆盖四个 tracked 文件，结果 +129/-8；两个新增文件分别 268、363 行，合计 +760/-8。用户转述“约 +503/-12”不是当前六文件的精确 diff，本轮不将它作为精确变更量。无法从当前工作区确定该近似数是否来自较早中间版。

旧 `/tmp/.../candidate/export_manifest.json` 在 `files.agent_core/skill_script_invocation.py` 仍保存早期 hash `4df91b1c7c873a4a98e58c2fdf2ee1ad1b21fb9480b361f72ee2482cd15b6055`，不是最终源码 hash；最终现存源码、提交给用户的六文件回执、测试日志的 SOURCE_UNCHANGED 才是此处交叉核验依据。两个日志 SOURCE_IDENTITY 也不同，说明不是一个从头到尾不变的初始 export manifest。

已证实：`/tmp/skill-invocation-validation-nm1jgfjk/integration-results.json` 包含 69 条 `phase=call,outcome=passed`，无失败 phase；`integration.log` 为 `69 passed in 91.00s`、`SOURCE_UNCHANGED True`。分组是 invocation 40、resource binding 18、preflight directory 5、mapping graph 6。完整 69 个 nodeid 已复制到见证 JSON，不由测试数量推断模型或科学 E2E。

替身边界：`tests/offline_managed_bootstrap.py` 使用合成 config、惰性环境探测、禁止网络及多数子进程；`test_preflight_mapping_commit.py:45` 的 provider 返回事先编写的 Consultant/Planner/Generate 答案，`:111` 的 RecoveryModel 同样是替身；`:135` 审批固定返回 approve；`:141` 执行边界包装后调用真实 `execution.executor.run_script_path`，但强制清洁 PATH 和新建 tiny FASTQ/FASTA。真实部分包括 SkillManager、binding、生产 graph、审批身份、runner、小数据 helper、验证和 CommitStep。directory fixture `test_preflight_directory.py:25` 没有 `type=execute`，故它验证资源/目录权限，不能单独证明 invocation gate。

主审计发现的 WGS 旧 session 捕获为 Skill 1.1.3，当前为 1.1.4；runtime.identity 未含这三个 helper/runtime 模块。本节 69 项的当前六文件匹配，不能倒推科学事故当时已经加载了新契约。

## 2. Q3：真实边界与执行链

已证实调用链：

1. `根/agent_core/skill_resource_binding.py:22 script_catalog` 从 active、当前 step 适用 selected refs 和 registry `_scripts` 产生句柄词汇，不扫描候选代码。
2. `:41 bind_skill_resources` 精确查 handle、已选 Skill snapshot、registered root、当前脚本 bytes；`:91` 调 `根/agent_core/skill_script_invocation.py:22 resolve_script_invocation` 读取 pinned SKILL frontmatter 的显式 `generate.script_invocations[relative_path]`。
3. `根/bio_agents.py:42331 _generate_impl` 对目录每个 handle 单独 resolve，将完整 invocation（含解释器 stat）放进 `<selected_skill_script_bindings>`；prefix 是模型提示，**不是生产已生成完整 action argv**。`build_skill_script_argv(:107)` 在现有生产路径没有调用者，只有定义及 tests。
4. `bio_agents.py:44323` 先 `check_executable_action`；`:44348` 在 Generate admission 调 `bind_action_skill_resources`，后者在 `skill_resource_binding.py:116` 调 `validate_skill_script_invocations`，在审批前拒绝可定位的解释器/参数形式错误。
5. `bio_agents.py:45152` 再调用 `_bind_pending_action`，其中 `:12632` 重复 binding；生成 `formal_skill_resources`、env、execution policy manifest。`agent_core/action_runtime.py:915 build_execution_policy_manifest` 复制全部资源 receipts，`:1119 build_action_approval_identity` 将 policy hash 与脚本、runtime、artifact manifest、cwd 一起纳入批准身份。
6. `bio_agents.py:48836 execute_logic_impl` 调 `validate_action_skill_resources` 重新读资源/构造 receipt，与 frozen receipt 全等比较，不改 script/argv；失败进入 `reject_internal_prelaunch`，并非实际执行失败。`:48990` 从批准 policy 与当前 attempt 派生 evidence dir；`:49003` 写入 runner env，正文中已批准的 `$BIOMIND_PREFLIGHT_EVIDENCE_DIR` 不变。`:49542` 再核对批准正文 hash；`:49574` 将已保存 wrapper、expected sha、env 送 `run_script_path`，后者 `根/execution/executor.py:912` 核对 wrapper hash 后以 bash 启动。

解析分类：

| 操作 | 当前实现及结论 |
|---|---|
| 显式结构字段 / 引用 | frontmatter YAML、skill_resource_refs exact lookup、action argv carrier，允许；不猜科学角色 |
| 明确 carrier 定位 | `agent_core/step_effects.py:3568 executable_carrier`，argv 有效时以 argv 为实际载体；`bash -lc`/Python `-c` 读取对应 operand，普通外部 argv 不读文件 |
| 语法检查 | `execution/script_validation.py:14/:39` 只编译实际 carrier；Bash `-n`、静态 Python fragment compile；报告未检查动态源码、运行依赖、科学有效性，不能等同科学判断 |
| 固定 utility 调用检查 | `skill_script_invocation.py:121 _simple_commands` 对**整个载体**逐字符词法解析、引号/环境变量显式展开；`:213 validate_skill_script_invocations` 用第二个 token 精确匹配已选 helper path、核对 interpreter 和参数 arity；这是代码扫描，但属于显式固定接口识别，不是按变量名/参数位置猜数据角色 |
| 改写/自动科学 binding | 本链无正文改写、无科学环境推断、无从任意脚本猜 inputs/outputs；只在 env 注入明确句柄对应 canonical path |
| 未识别处理 | `SKILL_INVOCATION_UNVERIFIABLE` 是候选拒绝，说明当前检查不覆盖该载体，**不能表述为命令已被证明错误或科学执行无效**；这里确有表达限制 |

支持边界（见证 JSON `witnesses` 为当前真实函数实测）：

| 形式 | 当前结果 | 解释 |
|---|---|---|
| leaf helper direct argv | PASS | argv token 原样保存，不做 shell 重拆 |
| code+argv，code 与 argv 不同 | 验证实际 argv；本轮合法 argv PASS | `code` 不冒充执行事实；旧 tests 也覆盖反向：code 合法、实际 argv 无解释器被拒 |
| 双引号中绑定变量 | PASS | env 中已确定 path；单引号 `$VAR` 保持字面量，不能作为展开 |
| 两行直接 helper 调用 | PASS | 允许 newline，允许少数 `set` 及 `exec`；通过不代表多次调用必要或科学成功 |
| helper 前加 `echo starting` | UNVERIFIABLE | 所有非 set/exec 行都要求为 declared helper；合法 shell 的辅助行也被挡 |
| `bash wrapper.sh` + helper refs | UNVERIFIABLE | 不读 wrapper 内容，也不能证明 wrapper 的实际 helper 调用 |
| `P=...` 后 `"$P"` | UNVERIFIABLE | 只信 action.env 已绑定值，不执行 shell 赋值语义 |
| `;`/`&&`/管道/条件/heredoc/substitution | UNVERIFIABLE | 已有对应 tests；不向任意 shell 解释器扩张 |
| 任意自由科学脚本，无 helper refs | 本 gate PASS/不适用 | 不等于其他授权和科学验证通过 |
| script 被当作 executable | INTERPRETER_REQUIRED | 0644 源码不可直接执行，审批前纠正 |
| batch helpers 的 direct argv | 不受支持 | declared deferred evidence dir 必须为带来源的双引号 runtime variable；literal 或 guessed path 拒绝 |

四个 helper 的显式依赖与真实源码一致：WGS primary `scripts/inspect_wgs_inputs.py:14` import `inspect_fastq_batch.assess_prefix`，`:35–40` 用 `sys.executable` 启动同目录 FASTQ/FASTA leaf；batch `inspect_fastq_batch.py:51–56` 用 `sys.executable` 启动 FASTQ leaf。因此现有 manifest 要 pin 四个程序有依据，但**让模型复制四个句柄不是必要选择**。可以从已声明 delegates 做有界闭包并在审批前绑定；不得推断任意 import、不得隐式安装。现在缺子句柄 `:263–265` 为 DEPENDENCY_UNBOUND，本轮复现。

审批对应执行的已核验旧证据：missing_interpreter scenario 为 Generate 2 次，approve/execute/Commit 各 1，Recovery 0。审批 code sha 和 execution.approved_sha 均为 `24a2f4606910ae822acd22b812e40d856dfc2f9a23e7925812d9a3c0c909ddd9`；wrapper sha 为 `1364fb404a9cfca3f1f1b24b45a13fc61539509fb75e52c49885f10c44a62e78`。测试 `:141–158` 核对 saved approved.sh 等于审批 bytes、runner resources 的实际 env/path/hash 等于审批 policy，wrapper 独立 hash；不同 hash 表示受控 wrapper 包装，不能说审批与 wrapper 文件逐字相同。

## 3. Q6：保留、缩减、删除或合并

| 字段/检查（当前生产消费者） | 建议 | 正常与异常行为案例 |
|---|---|---|
| selected Skill / step scope / exact handle / root 内路径（bind_skill_resources、_registered_source） | 保留硬边界 | 同名 helper 在另一个已选 Skill 可精确查；unknown/越界/symlink escape 必须拒绝而非猜安装根 |
| script 内容 hash、selected Skill revision（_resource_snapshot、receipt 全等及 policy hash） | 保留；同轮去重，不删 prelaunch | 审批前允许新版本进入新 snapshot；审批后脚本 bytes 改变不可沿用旧批准 |
| interpreter launch path | 保留 | venv symlink launch path 与 realpath 不同，不能用 `.resolve()` 改成 base Python；错误 `/bin/sh` 必须在审批前拒绝 |
| interpreter device/inode/size/mtime/ctime/real_path（没有单独字段读取者，但 receipt 全等和 policy hash 真实消费） | 原始 stat 不入模；**不能称死字段直接删除**。建议 stat 用作内部变更信号，身份归并为启动路径及经验证内容/运行定义版本，避免无内容变化误拒 | 本轮同 synthetic executable bytes 只改 mtime 即 SKILL_BINDING_DRIFT，证明全等过严；真正替换 interpreter 仍须阻止。替代内容身份/缓存策略未实施，须单独验证，不能简单删时间戳后宣称同等安全 |
| helper `binding_digest`（receipt 内 `_hash(receipt)`，无独立语义读取，但完整 receipt 和 policy manifest hash 覆盖） | 可删除/合并每行重复 digest，保留一套 canonical resource manifest hash | 删除后改 helper bytes 仍由内容复核及 policy hash 发现；仅删/伪造旧 digest 不能制造新的执行定义。历史 schema1 整体验证必须保持，否则会使旧审批无谓失效 |
| `readable=True` / `executable_bit_required=False`（恒定断言被整体 receipt 比较） | 删持久化布尔量或转类型级说明；保留实际 open/读取和明确 interpreter 校验 | 可读0644 helper应允许；permission denied仍拒绝，不靠旧 `readable=True` 授权 |
| `skill_id + relative_path + handle`、canonical path、registered_root、cwd/namespace/step_id | 合并派生/作用域重复；保留当前权威入口及动作 scope | handle可精确拆skill/path；cwd/step已在批准身份/Plan中，重复拷在每行没有额外科学选择。不同 scope动作不得复用 receipt；旧 schema整体读兼容 |
| `argv_prefix`/`shell_prefix`（prefix提示、builder）、arguments/delegates | arguments/delegates以 pinned contract 为权威；显示 carrier需要的一个prefix，其他按需派生 | 引号含空格/Unicode的prefix必须准确；单纯减少解释器重复字段不改变执行 argv |
| delegated-resource 未手填检查 | 删除要求模型复制 delegates 的硬拦截，换为审批前显式声明闭包 | primary一个handle足够，manifest仍含4资源；声明不存在/循环/越界应具体报错，不静默跳过 |
| 重复 Generate bind（44348、45152→12632） | 归并一次可信 bind receipt；prelaunch保留独立 fresh verify | 同一候选未变不应重扫；code/argv/refs/env/snapshot任一修改必须重新绑定；不能信模型提供内部receipt |
| `UNVERIFIABLE` 与实际违约共用模糊反馈 | 区分覆盖不足和已证明形式错误 | wrapper未识别不能称“错误解释器”；可引导受支持现有 action.argv / 简单 shell，或既有显式文件检查流程 |
| policy/approval/runner hash、真实 observation、验证及 CommitStep | 保留其不同时间边界，不因“都是hash”合并掉 TOCTOU 或真执行证据 | 最终批准的代码只启动一次；helper返回 adverse rc0不意味着observation有效、不得Commit；旧证据已覆盖 |

审批身份是 snapshot，信息重复不必然冗余。真正可合并的是“同一个事实的重复 derived 表达和同一次 candidate 的重复计算”，不是将审批后 fresh filesystem 检查删除。`readable` 等即使没有 `get('readable')` 也参与全对象比较；本建议已考虑这一泛化消费者。

直接成本：四资源 Generate catalog 的真实函数计数为 `_verified_snapshot` 9、`_resource_snapshot` 4；一次 bind 四资源和一次 prelaunch各为 5、4。每 `_resource_snapshot` 都读当前脚本（上限 2MiB）并 hash，cached snapshot仍读；每资源bind还读完整SKILL（同一份被重复读）、解析同frontmatter、stat同Controller Python。`bind_skill_resources:52` 深拷贝 access，resource snapshot新增仅留在 working副本，没有在此函数返回持久缓存。常见 Generate目录→admission→freeze→prelaunch至少4组×4脚本读取；此为源码所示组数及函数计数，非真实请求计时或全应用benchmark。

未发现此 helper binding 链会读大型 FASTQ/FASTA并做全文件hash；它读的是SKILL/helper源码，解释器只stat。实际预检reader有限读取输入并输出检查范围；不能把代码hash与数据全量hash成本混算。其他artifact备份/校验成本不在本子审计结论内。

## 4. Q7：错误包与纠正位置

已核验旧真实生产 graph 包（模型为替身），missing_interpreter与argument_arity均在 Generate 局部修正；未接纳候选未进入 approve/execute。错误包有 task/plan/step、candidate/error fingerprint、previous_candidate、repair ordinal、execution_status=not_started；`exact_violations.detail` 保留具体错误。模型第二请求收到 validator feedback（测试`:94–95`明确assert），它可以看到旧候选和error code。

不足是来源与字段精度：`bio_agents.py:44352` 将 SkillAccessError 包成 PlanBindingError，`:44358–44365` 固定填写 `repair_stage=public_binding_normalization`、`validation_source=agent_core.plan_bindings.normalize_public_action_bindings`，而实际触发者是helper校验器。真实错误示例：

```json
{
  "repair_stage": "public_binding_normalization",
  "validation_source": "agent_core.plan_bindings.normalize_public_action_bindings",
  "execution_status": "not_started",
  "exact_violations": [{
    "code": "SKILL_INVOCATION_INTERPRETER_REQUIRED",
    "detail": "INTERNAL_PROTOCOL / SKILL_INVOCATION_INTERPRETER_REQUIRED: Skill source files are not executables; use the published interpreter + script argv_prefix",
    "owner": "GENERATED_ACTION",
    "message": "Generated action failed deterministic candidate admission."
  }]
}
```

这里 `detail` 的 INTERNAL_PROTOCOL前缀与 owner矛盾；没有稳定 field_path、实际token、expected、helper handle。正确形式错误的可纠正路由已经存在；改进应让 `_fail` / SkillAccessError 或专用诊断投影保留结构化 evidence，经44352传递真实validator来源，而非再开 Recovery。UNVERIFIABLE应写覆盖不足、具体不支持语法及现有可行carrier，不能只有泛码。

本轮unknown_resource函数见证给 `SKILL_RESOURCE_OUT_OF_SCOPE`；主生产admission会同样包装进入既有candidate rejection。环境未确认由runtime-resolution链处理，真实Picard执行失败由Recovery链处理，详见主报告其他样本；本节不把它们伪称为已用helper见证覆盖。

运行错误边界已有回归证据：`--item-timeout-seconds invalid-timeout` 符合arity但argparse在真实helper执行时rc2；旧retry scenario为Generate3、approve2、execute2、Commit1，graph Recovery节点1次，Recovery模型调用2次（第一次directive缺schema被局部修正）。第二次Generate的格式错是本地修正，未构成第三次执行。新的13项见证不调用runner。

纠正旧状态：当前 `_candidate_admission_rejection_update:16310` 清 pending_action；新候选重新 bind，旧bad资源未作为新truth注入。graph缺解释器修正后准确一次正式执行，未见旧binding污染。当前prelaunch receipt仍强制全等，因此已经批准后资源或环境改变会失效；不能静默修订旧批准。对跨步骤binding_env、argv-only syntax/progress回归，交由主报告专门见证，不从这里的简单carrier PASS外推整个graph已覆盖。

## 5. 可评审修改切片（仅建议，未实施）

1. **一次绑定与小投影**：`bio_agents.py:_generate_impl(42331/44348/45152)`、`skill_resource_binding.py:script_catalog/bind_skill_resources/bind_action_skill_resources`、`skill_script_invocation.py:resolve_script_invocation`。按Skill/snapshot一次读并解析contract，组装模型allowlist，candidate阶段只产生一次可信receipt；保留prelaunch fresh读。删除模型视图中的interpreter stat、registered_root/cwd等重复事实，减少同controller prefix重复。provider projection应返回AVAILABLE/CHANGED/UNRESOLVED及可追溯证据。只改提示词不够，当前raw invocation直接deepcopy入模。验收应同时测payload大小、相同candidate调用次数、资源变更仍拒绝、历史snapshot读取。
2. **delegate closure**：上述 binder及`validate_skill_script_invocations:263`，按显式delegates闭包生成审批前资源列表；删“模型必须列齐内部依赖”gate。旧模型四refs仍接受并去重；主helper只选一个时manifest仍4个。验证循环/越界/不存在declared resource、child变更、审批恢复。现有action/argv足以表达leaf helper；batch保留已批准runtime变量的simple-shell，不因此增加全局DSL/renderer或按任意code猜依赖。
3. **结构化helper诊断**：`skill_script_invocation.py:_fail/_validate_arguments/validate_skill_script_invocations`、`bio_agents.py:44352`及candidate rejection适配；保留code、source、field_path、handle、actual/expected、carrier coverage。旧detail仍读。缺解释器/arity仍仅一局部纠正，不计execution attempt；UNVERIFIABLE不是科学失败。只改提示词不能修现有错误包装。
4. **receipt去重与stat误拒**：`skill_resource_binding.py:bind_skill_resources/validate_action_skill_resources`、`action_runtime.py:build_execution_policy_manifest/build_action_approval_identity`。新schema内删除每行binding_digest、恒定布尔值，scope在manifest保存一份；不要就地改旧schema1批准hash。解释器stat比较应另做小变更设计，验证同bytes touch/relocation与真实内容替换，保留venv launch路径。它是现役保守拒绝的证据，不是声称可以无替代直接删所有身份字段。兼容须按schema处理旧完整receipt；遇到真正执行定义变化走现有重新批准机制，不因合法省略显示字段再问用户。
5. **科学表达边界说明**：frontmatter与Skill正文说明固定utility只支持哪些carrier，通用协议保持自由脚本能力。若未来要让固定helper与任意shell任意组合，需要明确组合结构/既有外部文件批准的证据，而不是扩大 `_simple_commands` 成语义normalizer。当前证据不支持新增通用框架；现有argv、一个top-level helper handle和声明参数已覆盖叶子调用，batch剩余deferred值问题是既有执行policy，不能猜attempt路径。

未验证：当前用户科学session恢复、真实模型纠正效果、Picard实际科学索引、重启中审批恢复、解释器内容身份替代方案、全部源代码版本与事故进程一致、任何后续补丁后的回归。上述建议均未改生产实现。
