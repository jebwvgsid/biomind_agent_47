# 环境发现、模型事实、动作与 Installer 增量审计

本项完成附件 §5、§6 及 §11 的相关现状见证，不实施修复。结论基于冻结源码、两个原 session 的只读材料、相同条件 AST 方法见证，以及现有工具的限定只读进程。

## 版本与证据界限

- 修改前实际快照：`/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/prerequisite_source`。
- 本次冻结快照：`/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source`；冻结 HEAD 为 `82c27867d862ef32d657fbc6162c70d4bc06492c`，分支 `refactor/consultant-controller-authority`。完整工作树及 overlay 身份由 [baseline_current.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/baseline_current.json) 管理，不能只用 HEAD 代表现场。
- 目标运行：`session_1789648505`；旧对照：`session_1789628497`。原请求及执行产物证明实际输入输出；日志未提供全部加载模块的精确源码身份，所以不能将当前函数 hash 自动当成原进程加载 hash。
- 本项真实执行导入冻结副本的 56 个模块，逐文件 hash 存于 `environment_results.json#imported_frozen_modules`；BioAnalyst 大文件只 AST 提取指定方法，不导入应用。PackageManager 用 `__new__` 和注册表副本，不执行初始化或修复 shim；验证 `persist=False`。所有 `.env.api_keys` 读取被禁止；第一个见证禁止所有 dotenv，第二个仅读取普通 `.env` 的五个白名单路径字段。
- 未运行原会话、模型/API、安装、GWAS/WGS 全流程。真实样本查询不处理关联分析，不创建 p 值、kinship 或图。

## 发现与归因

| 编号 | 归因 | 核实结果与影响 |
|---|---|---|
| ENV-01 | 既有缺陷 | typed Consultant 将 `require_scoped_evidence=True` 传给 preflight，后者用 `use_cache=False` 调审计。默认环境检查失败后，审计直接返回，连带跳过候选搜索。禁用缓存与禁用发现被耦合。旧快照已经如此；git blame 指向 `e07ab0a1`（2026-09-07，`fix(runtime): preserve scoped Consultant evidence through durable handoff`）。 |
| ENV-02 | 既有缺陷 | 后台生成了运行检查，新 seed 也构造了 `runtime_checks`，但私有循环保留首次 seed；选 Skill 结果仅追加“选择已接受”。后续实际请求没有新检查结果。不是仅凭中间 state 作结论。 |
| ENV-03 | 已修复但覆盖有限 | 新实现给 WGS 明确操作依赖、结构化探测、动作绑定及失效恢复；GWAS 四个 Skill 仍是旧的默认环境/整体 shell healthcheck，未声明当前子操作的 required bindings。新增 Planner 投影诚实输出 `unknown`/`NOT_REQUIRED`，但没有补全这些工具的发现及选择。 |
| ENV-04 | 本轮引入的功能缺口，非本次直接原因 | 旧 Installer 会调用候选搜索；exact 候选通过检查则复用，partial 候选成为可扩充环境提示及后续默认值。新 `_install_subagent_impl` 只取明确工单、证据或 Skill 默认目标，没有等价自动候选入口。旧函数库还在，但调用者已删除。本次 GWAS 未进入 Installer，不能拿这项解释本次 PATH miss。 |
| ENV-05 | 当前已修复的行为 | `SkillManager.get_runtime_env` 由静态 default 优先改为本 Skill 成功登记环境优先。`installed/verified/success` 记录可覆盖默认值；`broken/registered` 不覆盖。本次四 GWAS 均为 broken，前后仍选择相同 default，不是本次回归原因。 |
| ENV-06 | 既有发现与执行衔接缺陷 | 本次 generic Bash 动作自行探测 ambient PATH；已有注册环境的 bcftools 未进入脚本选择。`bindings=[]` 对 generic 合法，不能单凭空表判错；具体问题是依赖未验证或未选择时仍把必需样本文件写空并继续交付，详见输入审计。 |
| ENV-07 | 证据不足，不扩大结论 | 在有限索引内未找到若干关联工具；只在三个 R 环境检查了绘图包。不能断言整台服务器没有工具，也不能由 bcftools 查询成功推导 EMMAX、R 绘图或完整科学流程可用。 |

相同条件见证：旧/新 `_runtime_audit_for_skill` 各覆盖“默认环境不存在、入口不能启动、版本约束失败”三种叶子结果，分别令 `use_cache=False/True`，合计 12 组。False 的候选搜索及候选验证调用均为 0，均返回 `healthcheck_failed/RUNTIME_HEALTHCHECK_FAILED`；True 则会搜索并验证替身候选。三类具体失败文本在 detail 保留，但通用状态码没有区分原因。该见证替换的是外部检查结果，不伪称真实版本检查器已经识别所有版本不匹配。

`_consultant_runtime_preflight`、`_verify_conda_env_presence`、`PackageManager.find_matching_environment` 在两快照逐行完全一致。`_runtime_audit_for_skill` 仅新增 `inspection_probes` 分支；四 GWAS 不走它。seed producer 仅新透传 `coverage/inspection_status`；required-scoped 入口并非本轮新增。[源码比较与 12 组结果](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/environment_results.json)。

## 正式索引、状态与工具事实

普通 `.env` 白名单值：`ANACONDA_ROOT=/public/home/liusiqi/anaconda3`、`BIO_SOFTWARE_ROOT=/public/home/liusiqi/03.毕设/agent_shim`、`BIO_SOURCE_INSTALL_DIR=/public/home/liusiqi/03.毕设/source_tools`、`BIOMIND_SKILLS_DIR=/public/home/liusiqi/03.毕设/Biomind_v1/skills`。所读文件未设置 `BIOMIND_RUNTIME_REGISTRY_FILES`；这不证明原进程从其他启动来源没有设置它。

生产索引为 software registry、Skill runtime registry，以及可选 `BIOMIND_RUNTIME_REGISTRY_FILES`。PM 初始化还读取 `tools_setup.yaml`，本机指定根下该文件不存在。`SkillManager.runtime_registry_file_candidates` 不扫描所有 Conda 环境。审计额外按 Conda 已有正式索引 `/public/home/liusiqi/.conda/environments.txt` 及配置根的直接 env 子目录，合计核查 44 个 prefix 的精确 `bin/<tool>`；再核查已登记 tool source/shim、配置 source 根的同名入口。没有递归全盘搜索。索引列出 `.local/share/mamba/envs/jcvi_so_ss` 等外部 prefix，仅能说明候选存在；当前安装工单与 runtime inspector 的环境名协议不接受任意外部 prefix，不应先登记再发现无法执行。

`software_registry.json` 有 24 个 environment、13 个 tool。`popgen_gwas/popgen_gwas_r` 状态 registered、无验证时间，备注为验证前从 Skill 元数据自动登记；两个配置根下的目录不存在。四个 GWAS 的 Skill 记录均 broken，并有“not available under the configured root”原因。

这两个 registry 不能合并成一个“ready”布尔值：

1. `_verify_skill_runtime` 在真正健康检查前会 `register_environment(status=registered)`；环境不存在时将 Skill record 写 broken，但该 software entry 可仍是 registered。
2. `environment_runtime_available` 排除 broken，再查配置根下目录；这是候选存在性，不验证工具、版本或当前操作。目录存在的 registered entry 可以参加 PM 候选评分，但不因此获得 task-scoped AVAILABLE。
3. `get_runtime_env` 只查看该 Skill 成功登记记录；`sync_with_package_manager` 按 `skill_ids` 同步，不按包交集推导新的 Skill 绑定。因此 bio_gatk 中有 bcftools 不自动成为 GWAS 环境。
4. `find_matching_environment` 接受 exact 与 partial，包交集 ×10、能力交集 ×5、全匹配 +100，返回得分最高的一个环境；不组合多个环境，也不归一 `shell/bash` 等语言差异。候选函数本身没有 healthcheck。partial 甚至很弱的匹配必须继续验证，不能升级成整个 stage 已可用。
5. `_registered_runtime_tools` 只查 registry.tools 的精确工具名，不查各 environment.packages 或 bin。当前独立 tools 表没有 bcftools/plink/emmax/Rscript 注册项；这不等于相应程序不存在。

| 工具或阶段 | 本次事实等级 | 证据与不能推出的结论 |
|---|---|---|
| bcftools 样本列表 | 验证可用，限定操作 | 8 个 indexed bin 候选；真实 PM 从其 registered env 中选 bio_gatk，版本检查 rc0，bcftools 1.24、htslib 1.23.1；真实 runner `query -l` 正式 VCF rc0，215 个样本。其他 7 个候选未执行验证。 |
| PLINK/PLINK2 | 索引范围内未命中 | 44 个 prefix 及所列 source/shim 精确入口未找到；不是服务器全局 UNAVAILABLE。转换阶段仍未具备已验证条件。 |
| EMMAX 与 kinship 入口 | 索引范围内未命中 | 检查 emmax、emmax-intel64、emmax-kin、emmax-kin-intel64；未开展关联计算。 |
| Rscript | 候选存在，部分最小运行通过 | 找到 12 个 indexed Rscript；在 bio_gatk、jcvi_so_ss、bio_scrna_core 用 `--vanilla` 执行只读 namespace 检查均 rc0。 |
| CMplot/qqman | 所测三个环境不能加载 | 三处 `requireNamespace(...,quietly=TRUE)` 均 FALSE；其余 R 环境未测，不能声明全机未安装。没有生成图。 |
| bedtools | 版本入口验证通过 | 10 个候选；hicexplore 的 `--version` rc0、v2.31.1。未验证候选区间实际处理。 |
| LDBlockShow/ShowLDSVG | 索引范围内未命中 | 不推断源脚本、模块或其他未索引位置不存在。 |
| beagle/java/python | beagle 入口未命中；解释器仅候选 | 本轮没有进行 Beagle 或 Java 科学操作验证；helper 的控制器 Python 与科学环境选择是不同合同。 |

完整路径、来源、executable 位、探测 argv/退出值和限制见 [environment_inventory.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/environment_inventory.json)。R 检查使用干净 HOME/TMPDIR 和 `--vanilla`，未读用户启动配置或安装包。

## 后台事实到实际 wire 的断点

冻结源码位置：

| 生产者/接收者 | 具体位置 | 当前输入输出及消费情况 |
|---|---|---|
| runtime requirement | `agent_core/skill_manager.py:761,1769` | 选择 Skill 默认/成功登记 env，产生包、整体 healthcheck、能力声明；本次没有当前操作级 required。 |
| typed seed producer | `bio_agents.py:19992` → `19630` → `19346` | 后台运行检查；False 分支在 `19485` 提前返回，候选分支 `19490` 不执行。 |
| presence | `bio_agents.py:19074`，`package_manager.py:418` | 已配置根且目录缺失时直接返回；本次 12 次 presence 约 0.000 秒没有对应 healthcheck，并非完成了 12 次科学工具验证。 |
| LLMPayload 重建 | `bio_agents.py:34169,34809` | 将 status/reason_code 等投影到 runtime_checks；没带完整 reason、candidate 或 registry detail。 |
| 动态 seed 模板 | `prompts/manager.py:2482,2499` | `<controller_feedback>` 位于动态段。 |
| 实际请求装配 | `bio_agents.py:35105`，`agent_core/workflow/phase_subgraph.py:422,481` | seed 一旦存在就沿用第一次内容；新候选 seed 被舍弃。源码行可能随其他工作树变化，本文以冻结路径和方法 hash 为准。 |
| 本轮动作观察 | `bio_agents.py:37304`；phase 子图 `result.pop("messages")` | 只追加 select_skill AI 文本与 221 字符的接受选择消息，runtime_audit 留在工作状态；summary 返回值丢弃。 |
| Planner | `bio_agents.py:19951` → `agent_core/node_context.py:1561` | 新规划投影按 required binding 展示事实；空 required 不冒充 ready，capability 是 NOT_REQUIRED，总体 unknown。这是一项本轮表示变化。 |

目标 session 的实际 Consultant 请求 0002/0003/0004/0005 分别为 2/4/6/8 条 messages；第一份 human seed hash 始终相同。四次 wire 中 `runtime_checks`、`RUNTIME_HEALTHCHECK_FAILED`、`registered_tools`、`matching_env_candidate`、`software_registry` 计数全部为 0。0004/0005 的 `popgen_gwas` 计数为 4，来自已加载 Skill 文档；声明环境名被模型看到，后台验证结果没有被看到。每个 request 文件 hash、每条 message 长度/hash、短观察摘录已保全于 `environment_results.json#historical`。

隔离使用生产 `PhaseProviderContext.prepare_messages/queue_provider_event` 和真实 scoped-context builder，第一份 seed 后更换 runtime_checks 的第二次请求不含新事实；调用既有 `queue_provider_event` 后第三次请求包含新事实，同一 event_id 重放只出现一次。该试验省略了 payload safety 检查，未模拟模型回答，也不等于生产已经调用该 API。

合适的修复接口是既有因果事件/工具观察追加，保留首次 seed 与局部 transcript，而不是每轮替换全部历史。事件应来自持久 runtime_evidence 的真实 identity/revision，表示检查范围、方法、结果和候选状态；同身份内容冲突不能悄悄覆盖。新的验证要追加新观察并明确 supersedes，不把过期旧 ready 复用为本轮结论。

## 动作与真实进程

本次 `attempt_79e2ff04f7dcfa103d11` 选择 `framework.generic.execute`，runtime manifest 为空。`approved.sh:34` 定义 `command -v` 探针，`:53` 再查 bcftools；命中才 `query -l`，未命中分支创建空的必需样本文件，进程最后仍 rc0。runner 按 approved.sh 启动；没有自动把 default_env 或注册表候选加入 PATH。当前 generic 合同允许这种空 runtime binding，责任不是 resolver 漏拷贝某条 bcftools 记录。

真实可运行分支见证使用现有生产接口：

`PM.find_matching_environment({language:bash,packages:[bcftools]})` → `bio_gatk` → `PM.verify_environment(persist=False)` 版本验证 → generic resolver（空 binding 合法）→ 显式绝对工具路径写入不可变审计脚本 → `build_action_approval_identity/validate_action_approval_identity` → `execution.executor.run_script_path` → 真实 `bcftools query -l`。

该只读查询得到 215 个样本，输出 SHA-256 为 `15139c9237b91c4fbca6daf7a3908d70ca95b30d541bc3db44bc7adcf05ab88c`，与输入审计独立读取 VCF header 的列表一致。正式 VCF hash、脚本原文、进程环境、实际 argv 组合、批准身份、退出值、样本列表和冻结模块 hash 均存于 `environment_results.json#real_process`。registry 前后字节完全相同。

这是现有候选/验证/明确动作/审批身份/runner 可以组成的真实微小进程见证，**审计代码显式接上了候选到脚本的选择**；没有证明当前 Consultant 自动发现成功、真实人机审批交互、Planner/HITL/Commit 全链或 GWAS 已修复。批准对象是隔离审计身份，不能拿来恢复原任务。修改该脚本摘要会被现有审批身份检查拒绝。既有 `run_script_path` 先核对保存脚本 hash，再使用明确 interpreter 和传入环境；将来不能在批准后替换工具或追加另一个 Conda PATH。

按阶段验证很关键：只读样本核验可以采用已验证的 bcftools，也可在合法输入读取接口内读取 VCF header；不应为了该步先要求 EMMAX/R 包齐全。关联阶段仍需正式 Q 样本顺序证据和实际关联工具，不能由样本数相同或上述查询成功升级。不同动作已有独立 manifest、action environment 和批准身份，无需引入全局环境状态机。

历史 `session_1789628497` 只是通过 `shutil.which` 命中 samtools/BWA、未命中 Picard；当时 bindings 也为空，记录未保存 samtools 的命中绝对路径。旧 Consultant 的 bio_wgs 前缀证据和执行时 ambient PATH 是不同路径，不能据今天的 samtools shim→dorado 注册反推历史入口。

## Installer 删除的独立前后见证

旧 `bio_agents.py:52819` 的完整候选 if 语句通过 AST 原样置于隔离函数，输入“声明环境不可用、注册候选 partial、没有 explicit_runtime_repair”。真实旧语句调用 find_matching_environment 一次，输出 `ENVIRONMENT REUSE CANDIDATE`、缺包/缺能力，并将默认目标改为 registered_candidate。新 `_install_subagent_impl:21516` 在相同默认值、无指定目标输入下仍取 declared_missing；整函数没有候选或旧 registered-reuse 调用。验证调用和安装执行不在本见证范围。

旧 `_reuse_registered_runtime_for_install` 函数仍存在且与基线一致，但新父安装入口不调用；新 dependency-preparation AskUser 允许“提供已有环境”，属于用户指定后的复验，不能代替系统发现候选。由此可认定被删除的自动候选功能没有等价接管。不能恢复旧成功后 `_mark_plan_current_done` 或从 partial 候选直接扩大安装授权。候选应在创建/冻结新目标工单之前提出，用户选择或既有正式选择后再沿完整目标、HITL、receipt、A3 走。

[旧新 Installer 与环境优先级见证](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/gwas_env_recovery_20260917/evidence/environment_installer_comparison.json) 保存真实语句位置/hash及结果。`get_runtime_env` 的 verified fixture 旧选 default、新选 registered；broken/registered fixture 两版本都选 default。这项增强应保留。

## 函数级修改切片与合并计划交集

### E1：在既有候选/探测层分离发现与缓存策略

**前后行为。** 当前 False 失败立刻退出；目标是禁用缓存时依然发现并重新验证合法候选，返回“已验证当前操作”“候选未验”“本候选不满足”“验证不完整”等事实，不能把所有失败统称不存在。

**接线。** 修改 `package_manager.py::find_matching_environment/_candidate_tool_paths` 的候选返回和匹配消费，保留 registered source provenance 与缺项；在 `bio_agents.py::_runtime_audit_for_skill/_consultant_runtime_preflight/_consultant_runtime_seed_evidence` 移除“禁用缓存等于不发现”的早退耦合，复用 `RuntimeInspectionAdapter` 与 `runtime_probe_contract` 验证当前明确操作。统一的候选服务归 PM/既有 runtime producer，Consultant、Recovery、Installer 调用同一入口；resolver 仍只消费已接纳事实，不安装、不猜工具。

**范围与兼容。** 先支持当前管理根内、已有合法直接 executable 形式；外部 Conda 索引可以报告候选但不能假装已支持其工单 target。不要在本切片默默扩展任意 prefix 安装协议。不引入平行 registry。保留成功 Skill 注册优先、UNKNOWN/负事实范围和 required 为空合法性。确有多个合格候选且无足够约束才请求选择，不恢复自动复用 partial 即 ready。

**验收。** 同一真实入口覆盖缺目录、目录存在但缺入口、执行失败、版本不符、可用部分候选；False 仍能搜索；单阶段 bcftools 可达真实样本读取，EMMAX/R 未验不被升级；路径已登记但不存在绝不 available。复用现有 runtime_producer/evidence/expiry、unified_runtime_graph 与 dependency_preparation；增加当前 GWAS 合法操作合同或通用动作明确工具选择的真实 graph 验收，不能只跑叶子/AST。

**交集/顺序/回滚。** 对应已合并统一计划 A1/A2 的未完成通用发现部分，保留现有 WGS probes/绑定、A3/授权。E1 与 E2 共享 bio_agents，串行集成；PM 纯候选改动可先独立实现。独立提交为候选服务+producer+相关测试；回滚本切片时恢复发现入口、保留证据存储及 fail-closed，不删除环境。

### E2：把新事实追加到实际 provider wire，并进入当前动作选择

**前后行为。** 当前新事实只在重建 seed；目标是 selected/inspection 产生的持久新证据，通过已有 `queue_provider_event` 或动作 observation 出现在下一次实际请求，含阶段适用范围和未验证项。首 seed 固定原则保持。

**接线。** 在 `bio_agents.py` Consultant seed producer 调用点、select_skill observation、`_format_runtime_audit` 的语义投影处，将检查 event 按持久 evidence identity/revision 追加到 `PhaseProviderContext`；`agent_core/workflow/phase_subgraph.py` 既有 queue/提交 suffix 路径可复用，不另建消息历史。Planner 的 `_runtime_binding_source`/`_compact_planner_runtime_projection` 继续区分 declared、verified、selected。Generate 的合法显式入口/所选 Skill capability 与当前 action manifest 对齐，随后沿既有 `build_action_approval_identity`、`build_exact_action_environment`、`run_script_path` 消费。

**兼容。** 不强迫所有 generic 动作产生文件或 required binding；无外部科学工具的 Bash/Python 保持合法。确实依赖外部工具的动作，由已有明确能力声明或已验证、明确写入批准脚本的合法入口承担选择，不分析自由脚本反推完整依赖。若能力/实现改变则走正式修订；只是同一允许目标的新事实也必须按原 action/epoch/approval 失效规则重生。依赖结果不能在批准后静默改 env。

**验收。** 首 seed 无检查→选择 Skill→实际第二次 wire 含新观察；同 event 重放一次、新验证另一次、scope drift不覆盖旧证据；模型下一步可明确选合法候选→真实审批对象→真实进程；故意改变 approved runtime/代码/env 仍拒绝。保留现有私有 prefix/continuity/compaction 测试，新增当前protocol端到端，不以“state.runtime_audit非空”替代 wire 断言。

**交集/顺序/回滚。** 对应 A1事实传递+A2执行选择的覆盖缺口，复用已完成 A3、helper、approval。与 Recovery 预算切片共享事件大小、第一次 wire计量及引用读取，事件内容必须适度且能查回原证据。E1后集成E2；独立提交 Consultant event projection+wire/action tests，回滚仅取消新增 producer事件接线，不复活共享全历史。

### E3：安装工单创建前复用候选，保留新安装安全边界

在 `bio_agents.py::_publish_runtime_dependency_question/_ask_installation_resolution/_install_subagent_impl` 的新工单目标形成之前调用 E1；在用户选定现有环境后走 readonly verify→双登记→A3，新安装仍走具体命令 HITL。禁止改变已有 execution receipt 的目标或重新发起同工单安装。复用 `installation_request/receipt`、generation_source_scope 与 replay，不恢复旧 `_mark_plan_current_done`。

验收 old partial 见证所指功能被安全接管、候选显示不等于安装授权、provided/manual/registration_retry不受安装禁令阻止、reject/quit准确终止、target不一致拒绝、same-prefix恢复重放不产生新执行，保留 `test_unified_installer_boundary.py` 与真实 `test_unified_dependency_preparation.py`。E3依赖E1；与E2共享bio_agents窄入口串行集成。独立提交候选选择咨询接线+安装边界回归；回滚咨询自动候选功能，不回滚既有收据/审批修复。

## 复现命令与结果

在仓库根使用 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python` 分别执行：

```text
docs/audits/gwas_env_recovery_20260917/witnesses/environment_audit.py
docs/audits/gwas_env_recovery_20260917/witnesses/environment_inventory.py
docs/audits/gwas_env_recovery_20260917/witnesses/environment_installer_comparison.py
```

最终三条命令均退出 0。第一条保存 12 个前后同条件缺陷见证、wire缺失及可用事件API见证、4份实际Consult请求摘要，以及真实bcftools查询。第二条 44 prefix/58工具候选、4次真实只读最小探测；第三条旧partial候选和新明确目标、成功登记优先级对照。结果文件保存脚本hash及运行源hash。初次见证构造中消息缺ID与AST局部变量初始化分别被真实生产检查/Python拒绝，修正仅在见证脚本；不计为产品失败。

本项没有将 shell 命令失败的通用状态代码当作安装缺失，也没有将 R 返回 FALSE、未知 Q 顺序或范围内未命中扩大为完整流程条件。当前最远真实可达是候选验证后的合法明确微小动作；自动 Consultant发现→实际新事实→正式stage选择→模型/审批/Commit全链仍是未来修复验收，不应标记为已通过。
