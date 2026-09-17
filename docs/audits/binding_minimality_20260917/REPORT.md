# BioMind binding 机制与精简优化审计

2026-09-17；审计范围按用户附件及随后明确授权执行。**本报告是当前实现审计和修改草案，未实施生产修复。** 没有修改生产代码、现役 Skill、提示词、环境配置，没有安装、调用真实模型/API、恢复原科学任务或重跑 WGS。

最小必要 binding 是：**模型明确选择的业务值/资源/运行入口，加上程序验证后的来源、适用范围和一次有效执行/提交关联。** 不需要模型复制内部身份、机械来源映射、helper 内部依赖句柄或已确定的上游消费引用；但删除入模信息与删除内部检查是两件事。本轮发现可以实际删减的对象包括未被单独消费的 Skill receipt `binding_digest`、程序可派生的重复输入字段、同一候选内重复解析；解释器时间戳硬比较可改为触发进一步核验，但替代身份设计尚未验证；每项的前提和兼容路径见 Q6 和修改切片。不能将这些简化扩成从自由代码猜环境、文件角色或科学语义。

优先处理两处直接影响执行/恢复的断点：`prepare_reference` 的环境事实传递，以及上游回滚未清除后代 accepted bindings。事故已确认 `bio_wgs` 前缀，但没有确认并选择动作所需的科学入口；空 capability runtime 要求既不触发发现补齐，也不把该环境带进进程。其次是 helper 契约的表达限制和错误归属，再减少重复上下文。预检 helper 的空科学 runtime、无文件输出本来就是合法行为。

## 版本、边界与交付索引

| 对象 | 已核验事实与限制 |
|---|---|
| 审计开始 HEAD / 比较 BASE | `4ea823511328ed4035ec2a99ddf09a3419bb288a`；本报告 BASE 定义为开始时 HEAD，用于比较工作区，不伪称其他开发分支的共同祖先 |
| 分支 / 父提交 | `refactor/consultant-controller-authority`；父提交 `443ed8cd26b1362f2f553ec6a1ee7cafceb0e992`；未配置 upstream |
| 现场 | 开始时暂存区为空；522 个既有 changed/untracked 路径逐文件记录 SHA256 或 absent，含大量既有文档删除/迁移及测试修改。收尾发现并发修改，见下节；未 reset、清理、恢复或纳入本轮提交 |
| 当前有效源码 | 使用当前工作区字节，包括未跟踪的 `agent_core/skill_script_invocation.py`。初次见证与收尾并发检查分别固定hash，不能只检出 HEAD 就复现本轮发现 |
| 当前加载入口 | `bio_agents.py:2895` 从 `config.config.SKILLS_DIR` 构建 SkillManager；默认值是仓库 `skills`（`config/config.py:420`），支持环境覆盖。本轮隔离构造明确使用该绝对目录；不宣称正在运行的其他进程已重载这些修改 |
| 历史 WGS | `session_1789628497` 的 generate-0009 manifest 捕获磁盘 HEAD 同上，但 `loaded_revision=UNKNOWN`，Skill `1.1.3`；当前为 `1.1.4`。请求没有新的 interpreter invocation。归档 source allowlist 与当前比较只有 `bio_agents.py` 不同，但该 allowlist **不含** action_runtime/skill_resource_binding/skill_script_invocation，不能补证完整历史实现 |
| 历史成功跨步 | `session_1789563666` 用于证明真实值/资源被提交和消费；其完整加载 Git revision 未证明。与当前生产函数隔离见证分开 |
| Git 交付 | 仅提交本审计目录；完整提交 hash 由任务最终回执提供。提交不包括上述既有生产修改，不 push；保护结果见 `evidence/workspace_protection.json` |

入口材料：

- [字段/检查矩阵](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/FIELD_CHECK_MATRIX.tsv)：生命周期、读取者、决策责任、删除/保留依据及行为案例。
- [helper 调用与身份核查](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/notes/helper_contract.md)、[运行环境核查](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/notes/runtime_picard.md)、[跨步骤值核查](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/notes/cross_step.md) 是主报告的定位明细；结论以本报告的统一切片为准。
- [现场基准](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/baseline.json)、[历史/当前版本对照](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_versions.json)。所有 evidence 内保存原证据绝对路径；历史原目录只读。

### 上轮六文件与 69 tests 回执

**已证实**审计开始时六个文件的 SHA256 与 `docs/fixes/skill_script_invocation_contract_20260917_results.json`、现存 `/tmp/skill-invocation-validation-nm1jgfjk/candidate` 最终字节逐一相同。该时点四个 tracked 文件相对 HEAD 为 `+129/-8`；两个未跟踪新文件为 268+363 行，按新增文件计总计 **+760/-8**。因此“六文件”得到证实，“约 +503/-12”不能作为该时点 HEAD diff 统计复用，也没有找到这六文件已提交的独立 commit。收尾并发改变了bio_agents，不能把6/6或69项旧结果外推到最后整个工作区。历史初始 export_manifest 有旧 hash，最终回执/文件/日志应分别看待。

原 `integration-results.json` 存在 **69 个 call passed** 和日志 `69 passed in 91.00s`；测试名称、hash 和 graph 结果已保存到 [helper_witness_results.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/helper_witness_results.json)。这是离线 source export、合成配置、临时输入、替身模型/审批/runner 等边界下的结果，不是 69 次真实模型/WGS 验证。本轮核验后复用，未把旧 69 tests 说成本轮重跑。新执行见证见末节。

### 收尾并发变更检查

17:07左右的保护检查发现非本审计的workspace mutation/revalidation修改：`bio_agents.py`、原先clean的plan_tracker/semantic_completion/step_effects/workspace_attestation、新workspace_revalidation及相关测试/文档；另出现环境安装恢复专项审计目录。完整新增状态和bio_agents相对初次hash对应副本的差异保存于 `evidence/concurrent_status_delta.json`、`concurrent_change.patch`、`concurrent_source_checkpoint.json`，未修改或提交这些生产文件。

六文件当前复核为5/6仍同回执，bio_agents从`75a90a11158304c2a060864de96889c27e4f53917d937b2946ea9377290c0f32`变为`c4d81118032f0132a8269cb5344cc1650986d148a8e80bd911906bb304d35883`。差异是workspace admission接收approved_step及执行后controller workspace revalidation，不是本轮helper或runtime修复。17项跨步纯路径已在最终并发版本重验，2个rollback缺陷仍复现，原结果保持不变；结果见`evidence/cross_witness_final_checkpoint.json`。成本变化见下文/跨步附录；原见证不覆盖新的整图集成。源码行号对应各证据记录的hash版本；并发新增行导致后续位置平移时，应按函数名和最终checkpoint定位，不能把初次行号当成最终字节身份。

13项helper纯函数见证也在最终相关源码上重新满足预期，调用次数不变；输出`evidence/helper_witness_final_checkpoint.json`明确记录旧六文件只匹配5份，没有把旧69项套用为最终整图通过。主报告和TSV内bio_agents/step_effects定位已按最终字节重新映射；初次专项记录保留各自hash时点，新增最终复核说明差异。

新出现的[环境安装恢复专项](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/AUDIT_REPORT.md)已按同一HEAD和相关模块hash核验并复用其直接binding结论；未重做其安装审计，也不把它的文件纳入本提交。其9个runtime现状见证不计入本轮新运行数。两份报告共享环境函数的建议已统一为切片A，补充事实投影和执行失败后合法恢复接线，详见运行环境附录最后一节。

## Q1：哪些不同的东西叫 binding

**已证实**至少有以下用途，不能用同一个“绑定必须非空”规则处理。分类只用于解释，不建议四套新 schema。

| 用途 | 模型需要决定 | 程序根据权威事实维护 | 进入实际执行的部分 |
|---|---|---|---|
| 动作运行环境 | 需要哪种科学能力、选择哪个已验证入口/环境 | capability 范围、accepted runtime evidence、路径状态、manifest/审批快照 | action 的具体 argv/code/env；registry/manifest 本身不激活环境 |
| 输入、产物和可变工作区 | 资源用途、显式 public handle、预期效果 | canonical identity/revision、批准路径、来源和实际产物验证 | 具体文件路径、明确 env/argv；新产物声明不代表文件已存在 |
| Skill helper | 显式选择 helper、业务参数 | 选中 Skill 版本、源码路径/内容、解释器、已声明 delegates、receipt | interpreter+script+argv，及 BIOMIND_SKILL_* 路径变量 |
| 跨步骤值 | 数据库名等尚未确定业务值，或选定明确资源引用 | 公布来源映射、类型校验、冻结、Commit 身份、下游 exact reference | 派生的 action env/命令值；binding须为当前Plan作用域的已接纳值；计划依赖可达性由独立门槛约束 |
| 审批/上下文身份 | 不由模型填写 | revision、digest、attempt、批准对象及失效关系 | 是程序门槛，不是科学参数 |

`binding_env` 是 **参数名 → 来源对象的映射**，不是进程环境变量，也不是 Conda 配置。业务值在 `produced_bindings`，进程环境在 `action.env`；Skill `BIOMIND_SKILL_*` 是路径注入，`BIOMIND_PREFLIGHT_EVIDENCE_DIR` 是批准后按真实 attempt 派生的受控位置。三个对象不能互相代填。

现役 `public_handle_v2` 允许省略 `binding_env`：程序依据批准 binding rule 派生；显式填写的每一项必须与已公布 source object 相符；正确子集可由程序补全。legacy full 协议不因此静默获得新默认。合法完整样本、合法省略/显式映射和错误值对照保存在 cross 样本/见证中。典型错误是把 `"task_local_genome_v1"` 直接写进 `binding_env.GENOME_VERSION`，而该位置需要来源结构。

Plan/TaskSpec 对资源和效果的声明是权威；action 是待执行候选；manifest 是冻结快照；Commit 是接受事实。保留这些状态边界有必要，但不需要让模型在每层独立重写同一身份。当前确有机械重复：Generate 重抄 `plan_binding:<name>` 消费引用；helper 重抄 delegates；action.env 与 frozen effective values 保存同值。前两项可在审批前派生，后一项需保留执行快照并禁止独立写入漂移，不能随意删掉历史验证入口。

“必需”须逐项说明：required runtime 是 capability 的前置事实要求；required artifact 是批准输入/输出义务；跨步 consumed binding 是上游提交依赖；delegate 显式重复列举是当前实现门槛；inode/mtime 是身份变化证据。它们不是同一种科学需求。矩阵列出了各字段生产者、读取函数、模型可见性、空值合法条件及失效处理。

## Q2：模型实际读写什么

### 实际请求与当前构造分开

本轮读取了 WGS 四个角色的**真实 request.json**，没有用当前磁盘模板倒推历史请求；又调用当前生产 SkillManager、SkillAccessService、script_catalog、bind_skill_resources 重建 helper 动态目录。后者只替换六个 reader-limit 配置常量，未启动 BioMind/模型/executor，见 [context_audit.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_audit.py)。

| 角色与代表请求 | 实际看到 | 要填写 / 不应填写 |
|---|---|---|
| Consultant `consultant-0005` | system、phase seed、Skill 搜索/全文工具结果、前轮选择、资源/环境检查结果 | 选择/事实采集/已有工具动作和 planning handoff；不建立第二份正式 Plan，不填写执行 binding/审批摘要 |
| Planner `planner_initial-0008` | 单一规划协议、handoff、两个 inherited Skill、此前两份被拒 Plan 及具体反馈 | 完整 Plan 的 resource_uses/效果/控制依赖/科学参数/待确认问题；canonical identity、revision、hash 程序生成 |
| Generate `generate-0009` | 777 字符 system + 84,076 字符 seed、当前 observation 模板/输出协议、两个 inherited Skill；helper 路径目录 | 该例选 helper、参数和 script/display envelope；不要求 produced_bindings/binding_env，因为是只读 observation；实际误把源码当命令 |
| Generate `generate-0016` | 当前参考索引步骤及输入/输出、Skill、effects；runtime bindings 空 | 具体科学命令、显式工具/环境和真实效果发布逻辑；科学事实不能由候选声明替代 |
| Recovery `recovery_turn-0017` | 独立 RecoverySeed、因果 failure、工具 manifest、两个 Skill、只读 inherited evidence；其中一份完整 plan evidence 为 183,445 字符 | 有证据的诊断、读取请求和语义提案；不写执行脚本/路由/状态。未继承 Generate 完整私有对话 |

当前调用入口：[bio_agents.py:42333](/public/home/liusiqi/03.毕设/Biomind_v1/bio_agents.py:42333) 直接把整个 `invocation` 放入 Generate catalog；[prompts/manager.py:2587](/public/home/liusiqi/03.毕设/Biomind_v1/prompts/manager.py:2587) 构造 seed；[step_effects.py:3294](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/step_effects.py:3294) 构造共同示例；实际 Recovery agent policy 在 [workflow/recovery_agent.py:72](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/workflow/recovery_agent.py:72)，不能拿 legacy recovery 模板代替它。

当前 **Generate catalog 的 interpreter_identity 确实会入模**：real_path/device/inode/size/mtime_ns/ctime_ns，四个 helper 重复四份；同一入口另有 interpreter、argv_prefix、shell_prefix。程序消费身份有理由，但模型抄命令不需要这些原始值。常规 runtime provider projection 已做字段白名单，helper invocation 是旁路整块复制，应该使用同样的白名单原则。没有证据证明历史 WGS 请求已带这些新增字段：原请求此字段计数为 0。

Recovery 可经 inherited execution/policy evidence看到部分内部 digest；本样本 `binding_digest` 出现两次。这不等于所有角色都默认接收完整新 invocation，也不等于应该把 Recovery 并回 Generate。应投影“资源变化/证据过期/来源未确认”等决策信息，具体原始证据保留按引用读取。

### 量化与重复口径

以下请求数字是各 message.content 解码后以一个换行连接的字符/UTF-8字节；不是 token、HTTP wire bytes 或整个会话累计量。完整 JSON 文件字节及每条消息分项见 [context_metrics.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_metrics.json)。不安装 tokenizer，也未初始化可能下载编码数据的 tokenizer。

| 载体 | 字符 | UTF-8 字节 | 解释 |
|---|---:|---:|---|
| 当前 WGS 文件 | 23,842 | 23,918 | 428 行 |
| frontmatter（含分隔符） | 8,476 | 8,494 | 133 行；程序机器契约 |
| 正文 | 15,366 | 15,424 | 295 行 |
| 旧 load_skill() 返回 | 16,777 | 16,873 | 剥 frontmatter，但另生成 runtime/scripts header |
| schema-2 exact snapshot | 23,842 | 23,918 | 保留全文；不能拿 legacy body 大小代替 |
| Consultant 0005 完整内容 | 104,970 | 109,022 | 8 条消息；system 22,820 字符 |
| Planner 0008 完整内容 | 190,261 | 201,099 | 9 条；含纠正历史 |
| Generate 0009 完整内容 | 126,478 | 131,708 | 6 条；catalog/evidence binding 标签 2,878 字符 |
| Generate 0016 完整内容 | 134,306 | 139,532 | 6 条；同两标签 2,878 字符 |
| Recovery 0017 完整内容 | 304,595 | 310,447 | 13 条；大量继承证据，不等于 binding 本身大小 |
| 当前四 helper catalog | 6,967 | 7,047 | 当前函数构造，小JSON统一序列化口径 |
| 同目录白名单草案 | 3,180 | 3,196 | 减少 3,851 字节（54.65%）；含必要调用/参数/限制与状态 |

helper 机器契约第12—53行 1,719 字符；正文 helper 说明第211—310行 6,594 字符（含科学检查范围，不可整段删掉）。参数 arity/repeatable 在 frontmatter、正文/例子、动态 invocation 三处表达；解释器路径在同一 invocation 有多种表示并对四资源重复。当前 catalog 叶值数 135→草案63，**这是模型只读信息数，不是必填字段数**。

真实 WGS 候选要抄4个 helper handles；草案只抄1个根 helper，程序从核对过的 frontmatter展开3个delegate并在审批前冻结。四FASTQ标签/路径8项、reference标签/路径2项、observation ID与evidence变量2项仍保留；没有把这些业务值冒充可删字段。当前 binding_env 默认已可省略，不能再把它计作本轮新增节省。

两个 Generate 历史请求均有2处 “Normally omit binding_env” 文本；`required_runtime_binding_ids` 在所测各角色请求各出现5次，主要来自两个 Skill全文，不能把每次当成独立强制约束。binding字段字符串次数与完整相关片段大小分别保存，零词频不代表角色没有其他等价来源字段。

### 完整样本和优化草案

[历史 helper 目录与原响应](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_historical_helper_sample.json)、[当前目录与内部 receipts](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_current_catalog.json)、[同一候选的具体草案](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/context_helper_concrete_draft.json)保留完整binding相关片段。草案沿用现有 shell payload，使用已验证Python前缀，保留相同参数。它不是已批准脚本；当前生产会拒绝只声明根handle，必须先实现delegate派生，不能直接运行草案。

跨步完整候选/来源映射/提交消费见 [cross_samples.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_samples.json) 与 [同候选精简草案](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_draft.json)。保留真实业务字符串和完整执行代码，producer仅省两个已声明type，consumer仅省两个固定`plan_binding:<name>` refs；各减少2个机械leaf值。当前validator会拒绝producer缺type，须schema/normalizer同步改后才能使用，未提交草案运行。

| 跨步binding片段，canonical compact JSON | 原UTF-8字节 | 草案字节 | 减少 |
|---|---:|---:|---:|
| prepare输入投影 | 3,066 | 1,347 | 1,719 |
| build输入投影 | 3,387 | 1,344 | 2,043 |
| prepare输出binding片段 | 192 | 154 | 38 |
| build输出binding片段 | 146 | 24 | 122 |

这些不包含全部请求/脚本，不能与其他统计直接相加。原两个真实候选已经省略binding_env，新增收益=0。证据摘录不含凭据；机器绝对资源路径保留用于复核，未读取被禁止的密钥文件。

关于模型为何仍填错 `binding_env`：**已证实** 当前提示和示例允许省略、显式字段接受严格校验；历史错误候选把值与来源混淆。**推测** 同名env、动态source表和兼容旧格式可能增加混淆。没有可观测模型因果证据，不能断言由某条旧示例诱发；应以相同fixture对比新schema/示例，再测试误填率。

## Q3：固定 helper 契约是否恢复了语义 binder

**已证实没有恢复科学语义 binder，但新增了有实际限制的代码形式检查。** 路径为 selected Skill snapshot/frontmatter → `resolve_script_invocation` → Generate提示 → `bind_action_skill_resources` → `validate_skill_script_invocations` →原审批→prelaunch同receipt复核→runner。固定入口和arity从显式声明取；没有按变量名推断数据库/文件角色、没有从任意代码补 binding，也未静默改写批准命令。

检查并非“完全不扫描代码”：声明有契约的 helper 后，整个真实 executable carrier 被按 simple-shell 子集解析；quoted argv/变量查表是确定语法操作，不是科学语义判断。`code+argv` 按统一 executable carrier 选择实际argv正文；其他code/argv一致性检查仍由既有接纳边界承担，不能从helper检查单独PASS推断整个候选PASS。

| 当前表达 | helper契约行为 | 判断 |
|---|---|---|
| 单reader直接argv、正确绝对Python | PASS | 既有action接口足够 |
| code+argv | 检查实际选中的argv载体 | 不扫描未执行的code来假通过 |
| 显式绑定 env 变量、引号、续行、optional set/exec | 支持 | 保留单引号字面量，不执行shell求值 |
| 两行直接helper | 支持 | 不能据此推断复合shell全支持 |
| wrapper、赋值、命令替换、heredoc、compound command | UNVERIFIABLE | 当前拒绝；只证明不在支持子集，不证明科学违规 |
| `echo starting` 后调用正确helper | UNVERIFIABLE | 隔离见证复现限制过宽，需在契约范围与诊断上改进 |
| 未声明helper refs的自由科学脚本 | 不进入新增helper gate | 继续现有检查，不引入通用语义解析 |
| batch/helper要求批准后evidence变量的直接argv | 当前不支持 | argv不作shell变量展开，不能把字符串占位符当真实目录 |

`build_skill_script_argv()` 当前没有生产调用者，仅测试使用；所谓自动前缀是给模型提示，非自动生成完整候选，更非批准后改命令。单reader可直接复用既有argv。batch可继续已有simple-shell承载一个明确的deferred变量；没有证据支持新建全局DSL/renderer/独立审批协议。

delegates来源明确且脚本内调用已核对：mixed helper导入 batch 的 `assess_prefix`，直接启动 FASTQ/FASTA reader；batch 独立入口也启动 FASTQ reader；子进程均用 `sys.executable`。程序可展开这一显式闭包、验证每份源码并冻结；不应要求模型了解并重复枚举内部实现。自由脚本继续原接口；无法判定的载体应给出准确unsupported/未验证范围和现有修正路径，不能标为“已证明违规”或“已检查通过”。

## Q4：运行环境如何到实际进程，Picard在哪里断

**已证实** `default_env` 是发现/检查的声明默认值，required列表是动作必须已确认的事实，optional列表只有被明确请求才解析。required/optional全空意味着此capability没有声明runtime事实义务，不是“所有工具已可用”。当前主链没有传 resolver 的 `requested_optional_ids`；`_structured_action_from_object_result` (`bio_agents.py:21838/22022`) 也不保留该未知字段，因此只填Skill optional列表不会生效。

真实链：Consultant成功检查WGS → accepted `runtime.environment.prefix=/public/home/liusiqi/anaconda3/envs/bio_wgs` → Plan选择prepare_reference但required/optional空 → Generate runtime rows空 → action/manifest bindings=[] → `build_exact_action_environment`继承ambient并只加action显式env → biomind Python执行approved.py → `shutil.which('picard')`未找到 →发布missing_commands后rc1，未建索引。动作resolver为空直接来自未声明/选择科学入口与调用方式，不是它在序列化中漏掉了本应属于manifest的行。

**另有已证实的上游投影缺口**：`node_context._compact_planner_runtime_projection:1561`未读取producer的`env/checked_env/declared_env`及`resolved_runtime_paths`，也不是按Skill保留各自状态。原Planner 0006、Generate0016、Recovery0018请求均只有Skill文本的bio_wgs，没有已核验绝对prefix/runtime_evidence/runtime_audit。不能因此把所有故障预设为上下文丢失，但这确实限制模型发现“环境已确认、入口待核验”的信息，应与下游capability/action断点一起修，不能仅补提示文字。

原 [approved.py:79](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/approved.py:79)、[runtime_env_manifest.json](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/runtime_env_manifest.json)、[executed.sh:6](/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/attempts/attempt_51bd254a3abad5bfdfa0/executed.sh:6)贯通这一点；完整相关片段、rc/stdout、manifest和源hash见 [runtime_history.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/runtime_history.json)。

与之相对，input_check成功重试同样bindings=[]，但只需框架Python/readers，正式Commit接受validation_observation并推进；accepted_binding_refs和accepted_artifact_refs都空。不能为了环境修复强迫只读helper提供科学runtime或假输出。

当前 Picard 是bio_wgs内wrapper，默认找同环境Java和JAR；有效`JAVA_HOME`会覆盖Java。既有帮助探测可复用为“当前入口/子命令可启动”，不能写成事故时环境就绪或字典生成成功。GATK绝对wrapper受Java版本影响的既有probe是另一入口行为，不应冒称Picard完全依赖ambient Java。

当前**已有** typed runtime evidence recovery：未知required事实在Generate阶段进入受控inspection；expired→UNKNOWN，冲突→AMBIGUOUS，完整negative才UNAVAILABLE；binding校验器不安装。原空required没有触发这条路径，裸PATH查找失败发生执行后，进入独立Recovery。`RuntimeEvidenceRecovery._scope:165`拒绝已被执行拥有的步骤，`create_request:262`拒空required，因此不能直接把已失败动作送进这条prelaunch服务就宣称闭环。应从失败包启动限定发现，先结束/释放旧执行所有权，按必要的Plan修订确认范围，再生成新候选/批准/attempt，保留已Commit的input_check；不能删除所有权保护或修改旧批准命令。复用旧报告时修正了“完全无环境恢复通路”的过宽说法，也明确这条后置恢复接线尚缺。精确错误包与调用点见运行环境明细及 [runtime_witness.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/runtime_witness.json)。

## Q5：跨步骤声明何时成为事实

**真实贯通样本** `session_1789563666`：标量 `genome_version=arabidopsis_thaliana_custom`，以及数据库artifact_ref，在 prepare_database候选中产生；执行、验证、Commit之后有 accepted binding identity；build_database与annotate_variants通过明确consume引用读取同一已提交绑定。标量对应 `plan-binding-ab0f72e804c427dac7da`，资源对应 `plan-binding-eb4155b7fe838749427a`。这是历史实际执行证据；当前无API隔离见证另列，不将历史版本等同现役。

计划中的produces声明只是承诺；接纳候选中的typed value是拟采用的值；冻结保证审批对象稳定；只有成功执行、效果/产物验证与Commit后才成为下游可用记录。数据库名称等合法新建标识可以执行前选择，不能要求先观察“最终名字”；但目录已建、数据库可用、测得统计值必须由执行/验证证明。当前`accept_action_binding_candidates`复制候选值，**不从stdout测量或更新标量**，因此实测指标应由typed observation/明确parser提供，不能预先猜一个值后仅靠rc0宣布已观测。artifact_ref先指已批准资源identity，存在/内容/效果是否完成仍由后续验证负责。

真实恢复补证 `session_1789537621` 中，第一次build虽rc0但`snpeff_database_ready=false`，未凭退出码证明完成；Recovery修脚本后仍消费相同accepted标量/资源，脚本用`$GENOME_VERSION`构造路径，第二次观察为true。它说明恢复可以原样传递已确认业务值；不是本轮重新执行，也不证明所有恢复路径已无陈旧状态。

`session_1789464978` 是拒绝对照：genome_version=task_local_genome_v1尚未成为accepted下游事实；0013有一致的code+argv但缺operation且binding_env错误；0014补operation并改为合法argv-only，来源映射仍错。不能把这条未完成链当成功跨步。代码选执行载体、语法证据和progress历史问题在当前的已有修复/见证中分别核验，不重复实施已完成修复。

typed string不因“像路径”自动变artifact；明确artifact_ref/public handle才查权威catalog；显式concrete角色拒绝未解析变量/占位；普通literal string允许原样保留类似`/tmp/$UNEXPANDED`的文本，程序不推断其文件角色；未知/越界handle不能被猜成资源。正常未来产物可在Plan声明，不能以接纳时不存在否定整个声明。只在当前动作使用的普通参数无须进入plan binding history；只读观察用typed observation+明确依赖即可，不强制产生文件。

候选纠正必须从新候选重建来源/有效值/check快照；当前public normalization已支持省略来源映射，argv-only语法/progress历史缺口已有修复。另有**本轮新复现的现役缺陷**：`plan_tracker._apply_upstream_step_rollback`（3197附近）使target及descendants步骤失效，但只调用失效target producer的binding。隔离生产路径构造a→b→c，回滚a后b.status=invalidated，`accepted_plan_binding(state,'b')`仍返回b-old；b同Plan tuple重跑改值触发`PLAN_BINDING_IMMUTABLE`。证明的是过期索引仍可读及正常重跑被阻；本轮没有证明完整生产图允许c实际执行旧值，其他步骤状态门槛仍可能阻止。应让accepted binding失效域与已计算的affected step集合一致，保留历史供审计，不能通过允许覆盖所有已提交值来修复。

详细新旧候选见证、历史读取兼容及精确调用边界见 [cross_witness_results.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_witness_results.json) 和跨步明细。无需为这些精确引用查找重新建立代码语义binder。

## Q6：保留、真正删除与合并

完整逐项关系在矩阵。下面按保护目的汇总，避免把所有hash/mtime都称为不可删“安全要求”。

| 处理 | 条件与行为案例 | 影响/兼容 |
|---|---|---|
| 保留资源scope/真实路径/内容校验 | 已选helper合法；未知handle/脚本移出Skill/内容变更拒绝并说明来源 | 保护执行对象；允许批准前重新解析位置，批准后不得静默跟随 |
| 保留有效批准对应真实执行 | 同候选恢复可继续；argv/env/helper变更需失效/修订 | 保留最终action/manifest/批准身份关系；不新增审批协议 |
| 保留输入类型、产物、Commit与旧文件保护 | 真输入类型不符/产物缺失/旧目标未授权替换仍拦；readonly无输出合法 | 文件内容前缀探测不等于全文件完整验证，也不把普通string派成文件 |
| 删除Skill receipt独立binding_digest（有前提） | 整体receipt已参与policy/批准，单独字段无独立生产读取者；去掉后相同候选等价、脚本变更仍被核心身份挡住 | 仅此字段；不要误删workspace binding_digest、resource_binding_digest或批准hash。新版本canonical投影忽略旧冗余字段；旧批准用其原验证版本，不能直接改历史对象 |
| 减少解释器mtime/ctime硬比较 | 同字节解释器仅touch目前会SKILL_BINDING_DRIFT；真实解释器/入口变化仍需识别 | 时间戳作为缓存失效提示；确认内容/必要环境身份后再判是否执行性变化。不是仅删字段不补同等校验；大型解释器hash成本需下一轮测量 |
| 派生helper delegate列表 | 明确root+合法声明闭包可执行；缺/循环/越界delegate仍具体失败 | 旧显式列表作为兼容输入核验；审批前展开完整receipt，不能批准后补脚本 |
| 逐步取消模型重抄consumed refs / source mapping | 唯一已批准consume ref可精确派生；不匹配显式值仍拒绝 | public新版本schema/示例/validator一起改；旧记录只读兼容，不为模糊引用自动找最近值 |
| 合并同候选重复resolve/序列化 | 相同snapshot/evidence revision复用；发生revision/freshness/path变化重算 | 保留prelaunch边界；不可用无限缓存绕过真实变更 |
| 删除重复入模原始身份与目录字段 | 模型仍看到可用性、入口、参数、变化/未确认结论；证据按ref读取 | 程序内部是否保留另行论证；不是把复杂记录隐藏就宣称误拦已修 |
| 删除normalization receipt重复的完整normalized_candidate | 当前可由raw+frozen+catalog精确重建；正常候选重建相同，改资源/operation仍应被拒 | prelaunch当前整体receipt equality会读到它，必须一起升级版本化比较；不能直接删旧记录字段。主样本prepare/build/annotate重复copy约4,997/9,231/5,435字节（默认json.dumps口径） |
| 缩减完整generate_binding_resolution副本 | Execute实际读旁边digest，再从当前权威重新解析；保留诊断所需生成时证据ref | 同时处理审批完整action及历史审计读取，保留一次value比较；不是删除digest或让值漂移 |
| accepted_at归并Commit事件 | 时间用于通用审计展示，不决定scope/执行；相同Commit接受多值可共享事件时间 | 低优先级，历史reader缺省兼容；不是声称无人读取或迁移全部历史库 |

**成本证据**：四helper目录构造见证 `_verified_snapshot=9`、`_resource_snapshot=4`，每次四资源bind或prelaunch为5/4；没有持久更新外层access的情况会重复构造缓存，现有snapshot缓存也重新读字节验证变化。Generate、接纳两次runtime resolve、prelaunch再resolve。此处报告调用数量与工作量，不编造毫秒或性能提升。

初次审计的helper/runtime/value绑定链没有发现对大型FASTQ/FASTA反复全文hash的证据：Skill脚本读取有2MiB上限，普通artifact `_observed_file_metadata` (`managed_artifacts.py:1713`)只读64KiB前缀并可能解压64KiB；`action_runtime.py:1050`替换旧文件获准后备份会全量复制并hash。

**最终并发版本新增一个直接相关分支**：`workspace_revalidation._read_verified_files:122` 在unchanged且显式允许idempotent revalidation时，依据已批准`exact_file_sha256_v1`对postcondition文件全文hash（每遍总量界限16GiB），由bio_agents执行后调用；成功复用路径在collect阶段读一遍、finalize验证再读一遍。它可能包括FASTA；不能继续笼统说“从不全文hash”，也不能说每个Generate/prelaunch都hash。此为源码工作量证据，未执行大型文件或测量耗时。旧文件备份和这条复用证明各自有具体用途，应在下一轮减少重复时按分支评估，不能统一删除全部内容证明。

## Q7：错误能否在正确位置被纠正

| 类型 | 当前证据与路由 | 缺口/保留 |
|---|---|---|
| helper解释器/参数形式错误 | SKILL_INVOCATION_INTERPRETER_REQUIRED / ARGUMENT_INVALID；接纳前进入Generate局部修正；既有图见证2次Generate、1批准、1执行、1Commit、0Recovery | 具体detail有用，但被包装成public_binding_normalization，validation_source错指plan_bindings；需保留真实helper validator/field/actual/expected |
| 来源映射错 | public normalization 的typed violation，修候选后重新冻结；不能当科学执行失败 | actual value与expected source object必须区分；省略可派生字段不应再要求用户批准一个等价对象 |
| 未知资源引用 | exact catalog/scope查找失败；缺失/越界字段明确返回 | 不从变量名、字符串外形或其他operation拿来源补齐 |
| 环境未确认 | REQUIRED_RUNTIME_BINDING_UNAVAILABLE + UNKNOWN/UNAVAILABLE/AMBIGUOUS，受控runtime evidence request | 列出已确认prefix与未确认tool/subcommand；empty required的断点必须在声明与选择处修 |
| 实际执行失败 | 原Picard为PROCESS_EXIT_NONZERO，attempt、rc、日志ref、stdout missing_commands可读；独立Recovery | 不能把当前PATH未命中提升为整机未安装；本轮不恢复原执行 |

helper exact violation当前有具体解释器detail，但公共包泛化为“Generated action failed deterministic candidate admission”，并带 `INTERNAL_PROTOCOL` 前缀与 GENERATED_ACTION owner不协调。只修改提示词不能修复错误来源标注。应将carrier、helper handle、argument/field、expected/actual和“不适用/未验证”范围沿现有局部修正包传递，保留限长详情/证据ref。

候选修正不是执行重试；原WGS第一次helper rc126后发生两次格式拒绝，最后一次执行成功，不能说连续三次忘解释器。新helper两个接纳修正graph场景没有在不合法候选时批准/执行。批准身份真实变化仍需原失效机制；派生默认字段省略不应制造额外用户确认。

## Q8：提示词、Skill与机器契约如何精简

**loader已核验**：SkillManager发现frontmatter并索引scripts/references；legacy `load_skill()`剥frontmatter后加header；schema-2 `SkillAccessService.ensure_snapshot/load_skill_documents/canonical_loaded_skill_documents`保留原完整SKILL.md并按已见material继承。reference文件只在明确resource读取时生成snapshot，不会递归拼接全文；当前WGS没有references目录。`project_skill_context`可按heading/section/range/resource提供精确材料，已存在当前tool-result去重与manifest-only路径，不能另建第二个loader。

因此拆正文后确实可能减小默认载荷，但仅把frontmatter搬成引用会破坏当前直接解析入口。先保留机器契约权威位置，再将长示例、历史故障经过及阶段方法细节分别移到按需reference/维护文档；必须用实际请求确认新正文没有被默认全部读回，并核验SKILLS_DIR/selected snapshot不仍指旧副本。正文移动使Skill revision变化，旧批准不能无声沿用。

| 内容 | 唯一职责建议 | 本轮依据 |
|---|---|---|
| 查资源/填写业务值/程序派生/缺事实处理 | 共用短协议 + 当前mode schema/examples；既有“通常省略binding_env”保留 | manager、step_effects、normalize三个层当前已具基础，不必每Skill复述 |
| 固定helper参数/解释器/delegates | 明确frontmatter机器契约；模型说明由同源投影 | 当前重复三处；整块invocation序列化带入无用identity |
| 实际路径/候选/可用状态 | 受控发现+核验的动态事实 | default_env文本不等于action进程环境 |
| 科学方法与验收 | Skill正文及阶段reference | FASTQ前缀不是全文件；unknown ploidy可延后确认到calling前；标记保留duplicates；保持原始reference只读与index完整性 |
| 失败经过与维护解释 | docs/fixes / audits | 不应每轮给模型重复历史事故；必要运行错误仍动态提供 |
| 内部身份/批准关联 | 程序版本化receipt/manifest及证据索引 | 逐项Q6决定是否真保留，不对所有metadata永久兜底 |

### 可评审的修改切片（全部未实施）

| 顺序/切片 | 具体文件/函数与删改内容 | 兼容、验收、风险；只改提示词是否够 |
|---|---|---|
| A0 回滚binding失效域 | `plan_tracker._apply_upstream_step_rollback`、`plan_bindings.invalidate_producer_bindings/accepted_plan_binding/accept_action_binding_candidates`，复用已算出的target+descendants影响集合 | 撤销affected producers的accepted索引，历史记录标stale保留；正常新值重跑可提交，仍有效的不可变值不得任意覆盖，外部不受影响分支保留。旧persisted state须在读取/迁移时按明确失效事实修复，不能按名称猜。三步rollback→重跑→下游读取生产图回归；提示词无效 |
| A 环境事实到动作 | WGS capability/runtime检查声明；`node_context._compact_planner_runtime_projection`按Skill保留已确认环境/入口/coverage；`runtime_inspection.py:RuntimeInspectionAdapter`；`bio_agents.py:_runtime_audit_for_skill/_runtime_binding_source/_generate_impl/_bind_pending_action/execute_logic_impl`；`action_runtime.resolve_action_runtime_bindings`；`RuntimeEvidenceRecovery._scope/create_request`及既有恢复事务接线 | 为该已选科学实现明确必需工具/Java事实和inspection producer，再在审批前以现有argv/env/code承载。执行后故障须结束旧ownership、限定发现、必要Plan修订和新动作批准，保留旧Commit；不能绕过not_dispatched保护。不要全局PATH注入或给所有动作加要求。ambient miss但声明环境工具存在能生成正确候选；真缺/冲突/陈旧保持区别。提示词不够 |
| A 的可选接口决定 | 优先现有明确capability选择+必需facts；现役不可达optional-ID接口可弃用并保留旧读取。确需optional选择时才扩 `_structured_action_from_object_result`、schema/examples、resolver所有调用及recovery assessment | 不能声称新增optional字段已经被现有action支持，也不能要求互斥实现同时存在。避免为单例再加模型机械字段；与环境专项只形成这一份改动 |
| B helper责任与诊断 | `skill_resource_binding.bind_action_skill_resources`按显式declaration展开delegates；`skill_script_invocation.validate_skill_script_invocations`保留确定调用检查/明确未验证范围；`bio_agents.py:44349`异常包装保留正确validator来源 | 旧显式refs验证后兼容；循环/越界/changed source仍拒；无契约自由脚本与leaf helper合法direct argv不受新增限制；batch的deferred变量仍用既有simple-shell。UNVERIFIABLE只改准确诊断，不改为PASS；新wrapper支持需另行验证显式carrier证据。错误候选0执行，修正后1审批/1执行/1Commit。不靠自由代码推断依赖；提示词不够 |
| C 入模与手填字段精简 | `bio_agents.py:42333`改helper provider白名单；`prompts/manager.py:get_generate_v2_prompt`与`step_effects.generate_output_examples`共享一次规则/当前资源；`plan_bindings.public_binding_requirements/normalize_public_action_bindings/public_binding_shape_violations/binding_input_progress_facts`派生唯一type和消费ref、合并raw映射重复assessment | 保留入口/arity/可用性/变化结论，按需read证据；相同真实候选对比字节/字段，错误显式来源仍局部反馈。legacy完整协议和历史v1/v2读法保留；内部类型/来源仍严格，不仅删prompt还留schema必填 |
| D 内部冗余 | `skill_resource_binding` receipt独立digest；`skill_script_invocation.resolve_script_invocation`生成身份，`skill_resource_binding.bind_action_skill_resources:105/validate_action_skill_resources:124`全等比较及execution policy hash消费身份；action接纳同候选重复resolve | 先版本化等价投影再删冗余digest；时间戳变更只触发核验，验收须证明真实入口/内容/明确执行环境变化仍失效；保留prelaunch防漂移。当前stat不覆盖Python包/stdlib，替代方案须明确身份范围，不能宣称已证明完整环境等价。安全收益/成本须用变更与无变更对照验证；提示词无效 |
| D 的跨步receipt归并 | `plan_bindings.normalize_public_action_bindings:1828`与`bio_agents.py:47479`整receipt比较，及48004 resolution digest读取 | 新receipt保留raw引用+必要binding输入+权威摘要，删除完整normalized copy和可重建resolution副本；旧v1/v2继续原身份逻辑，验证审批恢复/资源漂移/诊断可追溯；不能把间接消费者误当不存在 |
| E Skill拆分 | WGS SKILL正文 + 新按阶段references；复核`SkillAccessService.load_skill_documents/serve_context_request`、`SkillManager`索引、material carry | 保留机器frontmatter、方法边界和科学验收；故障回顾移维护文档。比较实际request默认载荷和按需读取可达；如果仅移动正文即可，不先改loader。旧快照/批准按现有版本机制处理 |

这些切片继续使用Planner单一正式Plan、既有action/argv/code、受控发现和恢复、正式批准及Commit。没有全局输入输出配对表、语义normalizer/binder、独立Consultant TaskSpec、renderer或新审批协议。

## 验证记录、未验证项与完成边界

本轮运行了现役生产函数的13项隔离helper见证、runtime evidence/resolution见证、上下文构造/计数及17项跨步纯路径见证（其中2项为缺陷复现）；具体命令、源码hash、结果与替身范围在各 evidence 和专项明细。跨步acceptance见证用合成rc/validation调用真实函数，未跑当前完整CommitStep/graph；真实提交贯通来自历史证据，不以返回值替代真实科学执行。负例返回预期拒绝表示**成功见证当前行为**，其中回滚后代binding残留、`echo+helper`、仅touch解释器等仍是待优化问题，不代表已修好。

复用：六文件最终hash匹配的69测试记录；WGS原approved/actual/rc/Commit；真实成功snpeff跨步链；旧只读Picard/GATK帮助probe。未重跑这些科学任务，也未新增帮助探测冒充E2E。

未验证：新精简协议真实模型误填率；新环境绑定后真正参考索引/完整WGS及工具版本科学兼容性；所有可选环境分支；长任务恢复/进程热重载；删除内部metadata后的新实现回归（尚未实现）；完整历史进程Git revision和完整PATH/JAVA_HOME；全系统性能或token收益。后续最小验证按切片从离线生产链开始，真实工具小输入/科学E2E需另轮授权，不能使用原session自动恢复。

工作区保护、审计材料语法/JSON/表格/链接与独立提交范围的最终验证写入 `evidence/workspace_protection.json`。完整提交hash和文件范围在最终回执中返回；任何本报告中的草案都没有发布到现役协议。
