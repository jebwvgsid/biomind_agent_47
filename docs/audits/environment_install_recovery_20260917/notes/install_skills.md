# Q2/Q3/Q7/Q8：现役 Skill、安装授权与恢复链审计

范围：当前工作树与 session_1789628497 原始请求。仅审计，未改生产、未安装、未调用模型、未恢复任务。证据目录为 `/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/`。本文路径相对仓库的引用均以 `/public/home/liusiqi/03.毕设/Biomind_v1/` 为根；可点击定位见矩阵。外部同时修改生产文件已由主审计记录；本子审计结束保存相关源码 SHA256，行号以保存版本为准。

## 可直接采用的结论

1. **Q2：旧工具注册和 InstallerAgent 均未退役；但“保留入口”不等于安装闭环完好。** `bio_agents.py:2889` 构造 InstallerAgent；`:53214` 将 `install_subagent` 留给 schema-2 early adapter，`agent_core/workflow/runtime_graph.py:436` 注册正式 typed 安装节点。PackageManager 注册表、Skill runtime registry 都有读写方；`PackageManager.install_package()` 则只有定义、没有本仓库生产调用者，不能把该方法当成现役主安装入口。
2. **Q3：实际 loader 完整发现 88 个现役 Skill，无第二根覆盖或归档副本。** 矩阵在 [skills_policy_matrix.md](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/skills_policy_matrix.md)，逐文件版本、SHA256、规则局部上下文、引用文件/脚本与历史请求位置在 [skills_inventory.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/skills_inventory.json)。本次事故不是“所有 Skill 禁止安装”：多项明确要求委派；wgs mapping 存在 broad 禁安装语句与 prefer-declared 元数据、安装工单混用的作用域问题。未找到全体 Skill 统一禁止“询问安装”的有效规则。
3. **Q7：当前安装入口有逐段代码审批、strict 安装命令限制、复用/后验 healthcheck；缺少可复用的结构化会话安装授权与完整方案身份。** `approval_policy="existing_install_policy"` 是字符串，传给安装工单后没有变成可检验的工具/版本/来源/目标/影响范围授权。父任务的 approval_scope/approval_status 未作为 InstallerState 字段传入。环境选择不等于具体命令批准。
4. **Q8：失败输出有命令/stdout/stderr 的有限片段，缺少结构化退出码/失败阶段/变更残留/人工完成事件。** 主链失败时会原样追加 Skill `install_command`，不能据此宣称给出了现场核验过的手动方案。人工已装完成目前只能经一般恢复/新请求重新验证，尚无证据证明具有幂等的安装事件→验证→注册→新 action→原步骤恢复闭环。
5. **当前未触发但应先修的独立风险**：无 strict contract 时 `conda run -n ... python -m pip install ...` 满足低摩擦前缀，从而跳过 installer code review；注册标签本身可让内图报告成功；取消返回文字可再次路由 Generate；typed 安装分类器用任一旧成功回执压过目标失败。这四点已作隔离现状见证 PASS，绝非修复 PASS。

## Loader 与实际继承（Q3）

生产加载：`bio_agents.py:2894–2896` 从 `config.config.SKILLS_DIR` 建 SkillManager；具体根配置的读取/覆盖顺序归 Q1。直接用现役 `SkillManager` 安全导入实际根，得到 `skill_count=88`、`catalog_complete=true`、`catalog_revision=skill-catalog-9d30c7eba6d17008`。未导入 config 模块；没有触发 PackageManager 构造器或软件探测。默认系统 python 因版本低不支持该源码 `Mapping | str` 注解而失败；使用本机 biomind Python 后通过，此为审计解释器选择而非事故。

- `agent_core/skill_manager.py:73–188` 只递归配置根的 `SKILL.md`；路径 resolve 后必须仍在根内，隐藏目录跳过；排序后加载 YAML frontmatter，可把 metadata 下 runtime 等提升。不是按关键词命中判定现役。
- `:263–298` canonical ID 为相对目录；兼容 leaf/legacy_ids。冲突 alias 标为 ambiguous，不简单“最后覆盖”；`name` 文本本身不是跨根优先级。当前 `_ambiguous_skill_keys` 和所有路径保存在 inventory。
- `:2877–2917` 正文按 canonical ID 加载，用 mtime_ns + size 作文档缓存键，重读更新内容 hash；目录 metadata 初始化后不在每次 load 时重新解析（只更内容 hash），因此运行中 metadata 变更需要明确重新发现/建 manager，而非假设正文刷新等于 runtime contract 全部刷新。
- `:2920–2958` 头部投影 runtime，唯独跳过单数 `install_command`，仍会包含 `install_commands`/`install_fallbacks` 等其他 metadata；scripts/references 仅列绝对路径，不自动读内容。正文不是继承整个父目录模板。`skills/SKILL_TEMPLATE.md` 名称不是 SKILL.md，不被 loader 自动选中；它是作者规范，不是每轮模型公共规则。
- `:2960` 才组装被选 Skill 正文。Consult/Generate/Recovery 还受 progressive context 的实际请求选择限制；只有原始 request 的正文/短引文匹配才能证明事故模型收到规则。88 项矩阵有每项历史正文触达列，更多位置在 JSON。实际曾读正文的为 reseq-variant-workflow、wgs-bcftools-snp-indel（Consult候选各2请求）以及 wgs-gatk-snp-indel、wgs-short-read-mapping（各17请求）；曾读候选不等于最终选中。
- `.skill_runtime_registry.json` 是环境历史事实缓存，非正文索引也非自动安装授权。`:382` 读，`:391` 写；异常加载默认为空，不能将其等价于“未安装”。正文完整性和 runtime readiness 分开。

### 当前规则与角色作用域矩阵

| 规则来源与位置 | 短原文 | 角色/阶段/约束对象 | 生效/继承边界与本次实际触达 |
|---|---|---|---|
| mapping SKILL.md:138–149 | `never guess an environment path, silently substitute tools, or install one.` | 科学能力边界；前句要求 approved runtime binding，后句 whole bundle 缺能力时 block | 当前文本较笼统，不写“本科学 action”；历史 Consult 0004/0005、Generate 0016、Recovery 0020 确实收到；不是推测 |
| mapping SKILL.md:131–132 | `install_policy: prefer-declared` | 安装方案优先级，不是授权 | 元数据可用于 installer 工单；当前单数命令不会原样进入 loader 正文 header；本次未调用 installer |
| tad-calling SKILL.md:89–105 | `Do not emit direct package-manager installation commands as an execution step.` / `delegate installation` | 科学执行不得安装，允许发起委派；安装后复验 | 随该 Skill 正文载入；本事故未选 |
| scRNA env/bootstrap:31–40 | `delegate installation` / `Do not inline ... as the main analysis step` | 环境准备 Skill 仍委派专职 Installer | 不因名字 env/bootstrap 就假定本身执行安装；本事故未选 |
| gene-plot:54–64、scripts/plot_methylation.py:35/43 | `delegate installation instead of emitting direct ...` | 科学调用/脚本报缺包，建议委派并重新检测 | 脚本不执行安装；本事故未触达 |
| CALDER2 SKILL.md:33–40,60–71 | `declared-only` / `Do not install CALDER2 into hicexplore` | 限定工具/目标/安装来源；不是全面禁安装 | 子图 strict contract 有代码限制；安装者应保留这些目标/兼容限制 |
| compartment-calling:35–36,182 | `declared-only` / `Do not install CALDER2 into this runtime` | 指定 PC1 环境不得混入 CALDER2 | 与上例一样不能替换成“全部允许安装” |
| loop-calling:37–42,50–52 | `declared-only` / `Do not install the unrelated PyPI package named mustache` | 限定正确包与源码/env 文件 | 另有 git clone 命令；现有 strict 只筛识别为 install-like 的行，不能当成全面下载/任意 shell 授权验证 |
| local-blast:12–13 | `declared-only` | 固定 BLAST 环境创建方案 | 保留 strict 限制；不等价用户批准 |
| snpeff:153–155 | `use the declared runtime installer when permitted` | 条件许可；失败应准确 blocked | 这类表述比 broad 禁安装更明确；本事故未载入 |
| 六个 crop `references/install-and-data.md` | `Use this file when the user asks for ... installation` | 安装/数据/复现 SOP 有条件读取 | 会给 pip/conda/git 示例；并未自动进入所有阶段；本事故未读取 |
| entrez-fetch references usage-guide(.zh).md:16 | `pip install biopython` | 文档示例，角色授权未明示 | 可选资源；顶层 Skill 无安装政策；不可将示例当科学 action 直接授权 |
| crop scripts 缺包错误 | `Install with install.packages(...)` | 脚本 stop 后建议；不是实际安装副作用 | sommer、rrBLUP、BGLR 等需在角色策略中转译为委派/人工建议 |
| skills/SKILL_TEMPLATE.md:74–88 | `prefer-declared` / `declared-only` / `normal missing package repair remains allowed` | Skill 作者规范/metadata设计 | 不自动注入模型；当前代码 `_selected_skill_install_command_context` 部分实现此语义，不能当所有 Skill 每轮已收到 |
| prompts/templates.py:34 | `NO DIRECT INSTALL IN EXECUTE` | 旧/稳定 Generate 模板 | 当前 schema2 Generate 使用 manager 独立分支；本次 request 未见该原句，不能把此模板当事故实际公共规则 |
| templates.py:465–469 | install_tool only delegation / describe install step as delegated | 旧 Planner 模板 | 本轮不能凭磁盘存在恢复旧模板控制权；当前 schema2请求以实际内容为准 |
| manager.py:2817–2851 | three alternatives: executable envelope / typed Skill decision / blocked | 当前 script-output Generate 唯一输出协议 | 当前本事故运行模式；没有 install_tool 业务动作入口；旧文档 XML 委派例子与现协议不相容 |
| manager.py:3014 | `ask_user, search, install_tool ...` | 非 script-output 分支 | 此分支的允许不能拿来证明历史 script-output Generate 直接可委派 |
| templates.py:554–558、installer.py:378–409 | strict contract overrides generic heuristics | 安装子图命令约束 | 只在工单存在带 allowed_install_command 的 strict block 时激活；不是所有 Skill 默认禁修改 |
| templates.py:593–606,648–655 | `VERIFY FIRST` / `If (and ONLY if) ... exit code 0` | Installer 验证/注册 | 是 prompt 要求；注册标签实际处理未检查前次 verify receipt，见证证明内图不够 |
| templates.py:608–612 | shim路径出现即 `your job is DONE` | Installer 旧递归shim保护 | 路径存在只说明注册线索，不能证明可用；且只说 success 文本时可能触发内图“无代码/注册”纠错，规则内部有张力 |

历史 mapping snapshot hash 为 `5274a542c8b9f6e2bf6f940d52d0495f131b4b090d04c8d7cacaf978008b8119`，当前为 `ca46ef15d79e260f01a8967fa8c97ec6abb50adb0e14bb1175a2c76d345e7673`；关键禁安装句保持一致但全文版本不同。GATK 当前/历史均为 `7444ab129be62dedebabccc602f2d2ae88d39ae33f7426fa4641e6aa8d2013ad`。30条 inherited material 版本记录见 skills_context_witness.json。

上述表与 88 项逐项矩阵一起回答“每条相关规则”的适用角色/条件。涉及无安装政策的项仍受当前 task highlighted_constraints 与 action/approval 边界，不能推导默认可安装。`workspace_mutations: []` 是产物效果声明，不应按词命中解释成所有角色无环境安装权。

### 历史实际文本

原始根 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace/`。

- `consultant-0004-eaa66becf1f9/request.json#/messages/5/content` 第 104 行与 consultant-0005 同位置：mapping 禁猜路径/静默替换/安装原句。
- `generate-0016-d4f379d86176/request.json#/messages/5/content` 第 106 行：同一句；正文前部仍有 declared bio_wgs 信息。该调用是 prepare_reference 生成路径。
- `recovery_turn-0020-5745b56334d0/request.json#/messages/3/content` 第 106 行：同一句。`/messages/18/content` 第 8 行模型将 Skill 解读为缺失字典不可替换/安装；`/messages/18/tool_calls/0/args/instruction` 又明确“等待用户提供已验证的兼容字典工具或授权安装后再继续”。因此不能说本事故模型认为永远不许询问授权。
- 精确请求及每条短引文在 `skills_historical_rule_quotes.json`。本事故不存在 install_subagent request，因此**没有证据证明此会话安装角色实际误继承了禁安装规则**。当前工单源码确会将 `load_skills_for_prompt(...)` 截前 4000 字嵌入 installer（bio_agents:52657–52664）。`skills_context_witness.py/.json` 直接调用现役 loader 与现有 `_clip_prompt_text`，证明 mapping 单独为当前目标时，禁安装句位于 2078 字处，会进入 4000 字工单；gatk 正文没有该句。不能据此声称事故调用过安装器。

## 当前注册、安装和恢复调用关系（Q2/Q7）

```text
Skill YAML / .skill_runtime_registry.json
        ↕ SkillManager sync/import/record_environment
BIO_SOFTWARE_ROOT/software_registry.json ↔ PackageManager registry
        ↓ find_matching_environment / verify_environment（不是正式 action bind）
Execute preflight runtime_install_transition
  ├ typed TaskSpec forbid / failure_policy runtime_missing→block → 停止
  └ retire pending action → installation_request {tool, evidence, policy string, fingerprint}
       → TypedExecute ExecuteOutcome.installation_requested
       → schema2 route_execute_outcome → TypedInstallSubagent
       → existing install_subagent source
          → current-step skill narrowing → registered candidate reuse/verify
          → env_selection（可缓存目标，不是安装授权）
          → clipped selected Skill + declared/strict install command context
          → InstallerAgent own MemorySaver, task_installer_tool thread
             → inst_generate（独立 prompt/模型，非本轮执行）
             → inst_execute（code review / low-friction; registered tags）
             → textual final_result
          → _verify_skill_runtime(use_cache=False)
          → SkillManager.record_environment + PackageManager.register_environment
          → _mark_plan_current_done（遗留直接推进调用；不能当科学步骤完成）
       → TypedInstallSubagent.classify(runtime_status / current step status)
          available→Generate / failed→Recovery / terminal→END
       → 仍需 runtime inspection producer/正式 binding → 新 action → 原批准核对
```

关键定位：`bio_agents.py:46904–47020` typed install transition，`agent_core/workflow/execution.py:1314–1323,1533–1540` 收据+清空旧 action，`workflow/runtime_graph.py:99,339–349` typed route；`bio_agents.py:52584–53109` 父安装节点；`installer.py:413–756` 子图；`workflow/early_nodes.py:1162–1204` typed 收据分类。

正式入口与兼容遗留必须分清：

- schema2 current script Generate 无直接 `<install_tool>`。即便旧 `_parse_structured_action` 保留该类型，不能绕过 script parser/typed outcome。当前安装边是 Execute 发出 installation_request。但 `bio_agents.py:48313–48322,48743–48752` 在 managed_mode 直接令 runtime_ready=True，跳过 `_ensure_skill_runtime_ready` 的旧缺依赖移交。尚保留 `_redirect_install_command` 在 :48262/:48678 识别 shell 包管理语句后移交；这条违规动作兜底不是合规的主动安装需求入口。现 script protocol 又拒 install_tool，因此本事故模式缺从“已验证缺依赖”到“提出具体安装方案”的完整正式入口。
- `PackageManager.install_package` (`package_manager.py:830`) 非现役调用入口；其 rc0→shim→installed 没经过正式健康检查，属于留存未调用方法，不能将其弱逻辑当本事故事实。
- 活跃的是 `register_environment` (`:521–576`)、`register_existing_tool` (`:863–954`)、`bind_tool_to_environment` (`:708`)。后者防递归 shim，规范 path/env，并实际写 software registry 与创建 compatibility shim；它们不产生 `runtime.<id>` 正式 action binding。
- PackageManager 构造器会 mkdir、load tools_setup.yaml、repair_registered_shims，**不是纯只读初始化**。本轮不实例化到真实配置根；见证用 `__new__` 和临时 registry。
- Skill runtime registry 与 software registry 并非只写不读：`bio_agents._import_skill_runtime_registries`/sync (`:18948–18952`)，`SkillManager.sync_with_package_manager` (`:2461`)；正式 runtime resolver 仍只消费 current capability 所允许并经 runtime inspection 登记的 binding。因此缺的是“注册事实→当前 action 绑定”适配，不能新增平行注册中心。

## 授权、拒绝、手动处理：实际能力与断点（Q7/Q8）

| 环节 | 当前实际行为 | 尚缺的可验收行为 |
|---|---|---|
| 安装可否提出 | `semantic_install_policy` 只从 typed constraint forbid/runtime_missing block 判定，未禁止默认 install_allowed=true | install_allowed 是可提方案，不是已授予下载/更改环境的批准；不要把二者合并 |
| 方案具体性 | 工单含 tool、target_env、selected Skill packages/capabilities、可选 install_command/healthcheck；部分无版本/来源 pin | 用户批准前冻结命令、版本要求、来源、prefix、共享环境影响及验证命令；无充分现场证据的手动命令必须标记待核前提 |
| 目标选择 | `env_selection` 只问工具和 default_env，指定 env/缓存命中可跳过；cache key task/tool | 工具 env 选择不构成命令批准；变更版本、来源、目标/影响范围后需按现有审批身份重判 |
| 会话授权 | AgentState 有 approval_scope；InstallerState 只有 tool/task/selected_skills/messages/final_result；initial_payload 同样无授权字段 | 复用既有授权结构并传入最小有效作用域；来源/有效期/拒绝范围及更改后失效应可追溯，不新增第二套审批 |
| 执行批准 | 非低摩擦路径走 `HITLMiddleware.review_code(code,language)`，用户可 reject/edit/quit；无 provenance/scope 参数 | 同一有效批准无需每段再问；严格命令来源限制不得冒充用户授权；低摩擦必须改为可证明只读受控探测工具，不匹配任意 conda run 前缀 |
| 拒绝 | reject 产生短观察，下一轮 Installer Generate；重复观测/最多10轮限制 | 持久拒绝方案身份并禁止同方案反复请求；允许用户明确给出另一环境/新目标后重判 |
| 取消 | quit 返回 `Installation terminated.`，router 不识别为 failure/cancel，继续 Generate | 使用现有取消收据一路传播，终止下游安装，原任务等待/停止语义准确 |
| rc0 后检验 | parent `_verify_skill_runtime(use_cache=False)` 会否决不能启动；可覆盖 installer 文本失败 | 验证应与正式 runtime binding 相同启动方式、目标和所需子命令；每项必须成功；父校验与 registry 写失败不可混成同类错误 |
| 注册错误 | 内图 try/except 注册失败写文本；parent 安装后写 Skill registry/PM 不是事务，错误可能抛 harness error | 保存安装成功但登记失败事实，幂等补登记，禁止重跑已成功安装；写两处时记录独立收据并可重试 |
| 重复恢复 | `_install_attempt_counts`、scope counts 避免>2循环；pending interrupt 不加计数 | 计数是内存级，非安装幂等键；thread_id 不含 target_env，不能保证换目标后上下文/授权隔离 |
| 原任务继续 | typed available→Generate；原始 source 调 `_mark_plan_current_done`→advance_plan_after_verified_step，函数直接设 passed 并推进；未见安装语义检查 | 环境修好只能重新形成当前科学步骤动作，不能把安装成功直接标记科学步骤完成；source 不应以旧 advance 路线驱动schema2 |
| 人工安装 | 工单若用户称已有则只验证/注册；一般 AskUser/Recovery 可继续 | 没有专属“manual_done→recheck”既有事实收据证据，需在现有暂停/恢复载体上承接人工完成事件，保留已提交 input_check |

`semantic_install_policy` 定义在 `agent_core/task_spec.py:4176–4204`，覆盖 `forbidden_behavior.runtime_install` 和 dependency-specific missing block；不会从自然语言关键字恢复政策。保留这条架构，不通过重读全文用户请求偷偷改变授权。

### 失败证据与可操作手动交接

`installer.py:613–621` 观察中保存 `Command[:500]`、stdout[:500]、stderr[:500]，以 ✅/❌ 表示 rc，**没有数值退出码**，且 wrapper 注入的 PATH/cwd 不在该命令片段内。没有专门失败阶段与已知残留字段。`_installer_failure_reason` 还会截 500 字。已有 model request snapshot 能复盘模型可见输入/输出，但不是完整安装进程收据。

`bio_agents.py:53080–53089` 在安装失败后原样加 `Suggested install command: {install_command}`，没有核验命令是否正是刚失败的那条、无版本/软件源/目标影响、没有固定验收与回到 BioMind 的路径。应将网络、权限、solver、build、runtime verification、registry write 分阶段保留，未知原因继续 UNKNOWN。现有现场无法据此给出“已验证可安装”的 Picard/GATK 命令；本轮既没有下载，也没有验证包求解。可确定的只有让现有候选按 A 方案先验证，真正缺依赖后再冻结安装目标与版本要求。

## 隔离现状见证与边界

脚本 [install_witness.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/install_witness.py)，结果 [install_witness.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/install_witness.json)。使用现有 InstallerAgent/PackageManager 类体 AST（不改其方法），GraphCapture 只获取生产 `_build_graph` 的嵌套 execute/router，runner/reviewer 为替身；PackageManager.register_environment 与 `_save_registry` 实际写隔离临时文件。TypedInstallSubagent.classify 为原 AST 函数，输出构造器为简单记录替身。不调用模型、不运行安装命令、不导入 config、不连接真实 session。

| 场景 | 现状结果 | 证明与未证明 |
|---|---|---|
| 只有 register_environment tag，无任何 verify 执行 | PASS_CURRENT_BEHAVIOR：registry status registered；inner router end_success | 证明内图不是 healthcheck 强门禁；父层复验未被该见证执行 |
| conda run -n fake python -m pip install fakepkg，无 strict contract | PASS_CURRENT_BEHAVIOR：review_calls=0，拦截runner收到命令文本 | 证明任意 conda run 前缀可跳人工review；所有安装均替身，不是授权真实执行 |
| code review 返回 quit | PASS_CURRENT_BEHAVIOR：0执行，router generate | 证明取消文本未终止内图；没有真实用户交互 |
| runtime_status 旧skill ok=true、目标skill ok=false | PASS_CURRENT_BEHAVIOR：capability_available | 证明分类器 any(ok) 与作用域遗漏；未证明本次历史会话触发 |
| 实际 loader/缓存索引枚举 | PASS_CURRENT_BEHAVIOR：88、complete、config_imported=false | 证明当前目录与字段，未为所有 Skill 启动科学软件 |

未运行：真实模型、真实授权暂停恢复全链、真实安装、真实人工安装事件、真实 WGS。修复后回归和真实软件小样应分别列档，不能以以上 PASS 充当实施验收。

## B 类可独立实施补丁（推荐顺序）

**B1 作用域化安装政策与请求协议。** 在 `skills/genomics/wgs-short-read-mapping/SKILL.md` 将禁安装限定为科学 action，说明先发现/验证已有环境，真缺依赖时允许提交现有审批/Installer 方案；保留 whole bundle、不得静默换方法和 required capability 规则。按 88 行矩阵只修改有冲突/模糊安装建议的正文/参考/脚本报错（C/S/E 类），不得全量放开直接安装。`prompts/manager.py` script-output 分支增加一个明确的非执行安装需求/用户选择语义入口，复用 existing typed installation_requested 路径，不能将安装藏进 shell；调整 Installer handover 只携带安装角色所需 runtime metadata、版本/目标限制及诊断证据，不整段继承科学禁止规则。strict/declared-only 工具来源约束仍保留。涉及输出协议与持久化兼容；旧 XML 委派只在现有兼容解析边界适配，前向生成使用唯一正式协议。

验收：mapping 能发起具体方案但不能科学步骤直接 pip/conda；CALDER2 禁装进 hicexplore、Mustache 正确源限制仍生效；对合法无 runtime 动作无影响。风险：扩大模型输出枚举影响 correction；回退以功能开关停用新需求产生端，保留读旧收据能力，不改旧Plan语义。

**B2 把批准身份接入安装子图，修低摩擦/取消。** 复用主链现有 approval_scope/approval identity 机制，在 InstallerState 和 typed handoff 传最小 scope、安装方案指纹、真实 target prefix、命令/来源/版本/影响/验证方式；`inst_execute` 前校验当前方案被已有批准覆盖。只读验证使用明确探测能力/固定 argv 契约，去掉 conda run 任意前缀免审；不通过任意脚本语义重写弥补。`quit`/reject 使用现有取消/审批拒绝收据并保留方案身份，用户选手动/另一环境时经同一暂停恢复载体继续。改变 target/version/影响范围重验授权。

验收：未授权0安装，已覆盖授权不反复问，换目标失效；reject不重复同方案，quit不回Generate；conda run内包管理无法跳审批；独立安装context保留。风险：旧内存恢复缺scope；兼容时明确需要重新确认具体方案，不静默承认旧字符串批准。

**B3 安装证据/登记/正式绑定/当前步骤恢复收口。** 优先复用现有执行证据和 action/runtime binding，Installer输出分阶段收据（命令、cwd、白名单env、rc、日志路径/短stderr、安装变更、verification、registration）。登记标签不可独立宣称可用。`TypedInstallSubagent.classify` 必须匹配目标skill/capability/target prefix/当前安装attempt，所有必需验证通过才 available，旧成功不盖新失败。`_record_installed_skill_environment` 两个既有registry写回有收据并可幂等补偿；之后调用 A 补丁的 runtime inspection→现有binding producer，失效相关缓存，撤销旧动作并新建当前科学步骤attempt。去掉安装成功直接 `_mark_plan_current_done` 的 schema2旧推进调用，当前科学步骤仍经执行/验证/CommitStep。保留原已提交步骤。

验收：rc0+验证失败不可用；登记失败可只补登记不重装；旧/其它环境成功不盖目标失败；人工完成必复验；恢复从原未完成科学步骤，已commit不重跑；不同Java/环境动作隔离。涉及收据schema和缓存失效，旧历史仅作为候选线索，不能提升授权。B3与A的producer衔接可同一逻辑补丁，依赖A的动作环境验证先到位。

**B4 手动交接与失败阶段。** 在既有停止报告/ask_user/恢复路径消费B3收据，展示确切失败命令、阶段、关键stderr和已知环境变化。仅在工具版本/目标prefix/来源/环境兼容要求齐全时形成可复制人工命令；未核验的软件源/solver标明前提，不原样重复已失败命令作为唯一建议。人工完成和提供新环境都触发同一readonly复验入口，再走B3。不得加sudo/全局升级/删除环境/关证书来掩盖原因。

验收：权限/网络/solver/build/运行时/登记错误分开；unknown不写缺软件；拒绝/手动等待和终态blocked准确；本次报告路径可达。可与C的显示/报告同交付，但不依赖真实变异统计。

## 源码及现有测试边界补记

`docs/refactor/installer_vs_main_recovery.md`（2026-07-31）确有独立安装子图/短日志/注册成功的历史迁移说明；当前源码仍符合核心描述，但该文旧 Controller 路由段不再是schema2权威，本文以现代码 typed 边为准。`tests/test_installer_agent.py` 多项为源码字符串断言；`tests/test_bio_agents_memory.py:513` 覆盖父安装成功后直接healthcheck逻辑的源码存在性，`tests/test_skill_manager.py:728` 覆盖PM环境回写Skill registry。未运行依赖真实 config 导入的整套测试；本轮用上述5组有区分能力的隔离路径替身/loader见证。

`_mark_plan_current_done → advance_plan_after_verified_step → _persist_plan_tracker → save_plan_state` 是需修的旧推进调用链；advance 在 `plan_tracker.py:12665` 直接把步骤设 passed，持久化有CAS/Task/Plan权限检查而未见安装可用收据转科学完成的门禁。Typed early adapter在 source 返回之后分类，不能撤回已经发生的写。当前未执行完整安装+真实持久化回归，故仅登记为静态高优先级风险，修复后必须用小型冻结Plan验证安装不跳过科学步骤，保留CommitStep。

**新增定点断点：用户改用另一已存在/新装环境后，父层可能验证了旧默认环境。** `SkillManager.get_runtime_requirements:766` 使用 `env = default_env or get_runtime_env(...)`，而父安装结果处理 `bio_agents.py:53040` 调 `_verify_skill_runtime(sid,use_cache=False)` 不传 target_env；该函数 :19116 仍取 requirements.env。即使 registry 记录改成另一环境，声明default_env仍覆盖。`skills_context_witness.json` 增加纯内存fixture：recorded env=audit_user_other_env，现役get_runtime_requirements仍返回bio_wgs；未写真实registry。B3须以本次批准的target prefix/工具链验证并回写对应事实，不用另一环境通过的healthcheck认证当前安装成功。该问题未在原会话触发。
