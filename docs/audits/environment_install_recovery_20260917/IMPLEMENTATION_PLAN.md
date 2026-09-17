# BioMind 环境与安装恢复实施计划

本计划由本轮源码、原始会话、全量 Skill 规则和现状见证推导；**仅供下一轮实施，本轮未改生产、未安装、未恢复 WGS**。依据见 [AUDIT_REPORT.md](AUDIT_REPORT.md)；证据/验收编号见 [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md)。仓库根为 `/public/home/liusiqi/03.毕设/Biomind_v1`；下面文件均指该根下现有文件，不要求新增同职责中心。

## 推荐顺序与合并边界

按 A→B→C→D→E 组织评审和逻辑提交。**D3 是重新运行 reference/mapping 的前置门槛**，不能因为列在 D 就等恢复后再修。A 完成可先验“发现与绑定”，B 未完成前不启用自动安装恢复，D3 未完成前不恢复受影响科学步骤。D1 是核验和收口当天既有修复，不重复造另一套 helper 入口。

| 组 | 建议逻辑补丁 | 前置条件 | 可合并与应分开 |
|---|---|---|---|
| A | A1 有界发现/健康证据与语义投影；A2 能力绑定和动作启动；A3 失败后的环境选择恢复 | 先记录实施时最新 HEAD/dirty 及本轮并发修改 | A1 的生产者与投影一起验；A2 的 Skill 契约与解析消费者一起验；A3 与 B3 可共享恢复证据接线，但不合并授权审批逻辑 |
| B | B1 安装职责/需求协议；B2 具体授权与取消；B3 目标验证/幂等登记/原步骤恢复；B4 人工交接 | A 的验证和正式绑定可用 | B4 可与 B3/C2 合并；B2 独立评审，避免用 UI/文案掩盖授权执行缺口 |
| C | C1 终态同步；C2 最新原因/已恢复取证/报告路径；C3 提前显示交付冲突 | 不依赖安装实现或科学成功 | C1/C2 可同提交但各自测试；C3 涉正式输出约束，建议独立 |
| D | D1 固定 helper 当前集成验收；D2 最小协议接线；D3 bundle 科学验收；D4 条件性依赖拆分 | D3 必须在科学恢复前完成 | D1 可随 A2 入口验收；D2 独立；默认暂不实施 D4，确有独立消费收益才与 D3 的正式计划修订合并 |
| E | E1 调用/阶段计量；E2 修复后重复上下文优化 | E1 可独立；E2 需 A/B/D 后对照数据 | 先修计量，再比较；不能一次捆进全面上下文重构 |

所有生产补丁保留单一 Planner 正式计划、Consult 事实/需求收集、既有批准身份、action/runtime binding、执行证据、CommitStep、旧文件所有权保护和独立 Recovery 上下文。不恢复 Consultant TaskSpec、旧 Controller 路由权、renderer 或任意脚本语义 normalizer，不新增平行 registry 或全局“工具→语言/环境”配对规则。

## A：接通已安装环境到正式动作

### A1 有界发现、子命令验证与最小事实投影

**问题与证据。** Q1/Q4/Q5；Consult 在声明 bio_wgs 中检查，但只形成 prefix 事实；粗 WGS healthcheck 的 BWA 管道可在 BWA 不存在时成功；Planner 投影未识别生产者 env/checked_env/resolved_runtime_paths；旧健康失败与 UNKNOWN/不存在混淆。

**复用与改动。** 在 `agent_core/skill_manager.py` 的 `get_runtime_requirements/_wrap_runtime_healthcheck`、`bio_agents.py` 的 `_runtime_audit_for_skill/_verified_runtime_paths/_consultant_runtime_preflight`、`agent_core/node_context.py::_compact_planner_runtime_projection`、`runtime_inspection.py/runtime_evidence.py` 和 `package_manager.py` 的有界候选/验证入口改接线。mapping/GATK 的 runtime 声明给出明确 inspection_outputs 及实际操作子命令，读现有两类 registry 作为候选，成功后只写既有权威 runtime_evidence；不能通过单数 install_command 存在就给出 ready。

**前后行为。** 先检查用户指定位置、Skill 声明、注册位置、配置根内候选；每个候选保留 provenance、入口及必要 interpreter/Java、方法与版本约束。区分未查、PATH未命中、环境未绑定、失效记录、多候选、不支持子命令、不兼容、探测失败和范围内无可用候选。找到满足约束者停止；多候选无法按约束裁决才询问；所有候选已排除或预算/权限使验证不完整分别返回 scoped missing/UNKNOWN。禁止全盘扫描和异常即未安装。

按 Skill/capability 输出少量已验事实和仍未知项，修正字段别名并避免以一个 latest row 掩盖其他 Skill；handoff 仍表达事实/需求，不复制整个环境或生成独立 TaskSpec。修 WGS 管道状态检查，用固定验证命令验证 bwa 子命令，而不是进一步分析任意脚本语义。

**缓存/兼容。** 复用 evidence scope/expiry、environment_fingerprint 和 method digest。目标 prefix、可执行/launcher 版本或摘要、healthcheck 方法、Skill contract、安装完成/失败残留变化使对应缓存过期；旧 registered/ready 仅作为候选，不能迁移成新子命令成功事实。计划未变时诊断事实本身不要求重新批准；正式选择变化交 A2/A3。模块重载需刷新 Skill metadata，不能仅正文 mtime 缓存更新。

**验收与回退。** M01/M02/M03：无 BWA 的 fixture 不再 ready；Picard ambient miss 但声明环境可启动时不发安装请求；UNKNOWN 保持未知；同一 scope 的 TTL/版本变化可重验；请求确实包含匹配 Skill 的语义事实。对配置 cwd/覆盖和缺失根做少量参数化边界测试。回退 A1 新发现生产者/投影，保留既有证据可读性；若新证据不完整就等待/阻塞，不回退成全局 PATH 猜测。

### A2 按能力声明并使用经过验证的动作启动方案

**问题。** Q4/Q5；prepare_reference required/optional 均空；合法精确 resolver 因而不消费 prefix/Picard。裸命令 runner继承 ambient 与实际验证环境不同。显式工具路径也不自动解决 wrapper 内 Python/Java/动态库。

**推荐方案。** 保留 `action_runtime.resolve_action_runtime_bindings` 的精确范围，在 mapping/GATK 每个实际科学 capability 内明确所需 executable/interpreter/环境选择和验证事实；只读 inspect/generic 保持空科学 runtime。必需依赖应进入 required；若用 optional，须补当前生产调用尚未传入的 requested_optional_ids 的明确选择和审批接线，不能只改列表。把“采用哪个启动方案”纳入现有 capability/action manifest 和批准身份。为多工具同环境动作推荐**显式 prefix + 经验证的动作环境构造**；为 wrapper/解释器有特殊要求的动作采用显式 executable/interpreter/必要 env。这两者按能力声明，不自动由 default_env 推断。

`agent_core/action_runtime.py` 管范围/manifest/环境值；`bio_agents.py` Generate/Execute 管同一选择的投影、冻结、复验；`execution/executor.py` 管真实启动。Skill capability 声明科学依赖，不让 runner 扫描脚本猜绑定。需要 PATH 时仅在该动作构造受控值；JAVA_HOME、PATH、必要动态库配置等采用经过同方式验证的组合，不把多个 Conda bin 堆在一起，不改变主进程或 shell rc。

**完整环境与显式注入的比较。** 完整 `conda run -p` 可处理需 activation 的环境，代价是启动开销、管理器可用性、输出/信号与 timeout 转发需额外验收；当前本机帮助探测未充分证明该启动链可用，不推荐未经核验全面替换。显式注入更符合现有 runner，能固定 wrapper/Java，但必须验证依赖闭包，不能只加 PICARD_BIN。官方 `-p` 仅证明机制存在。[Conda 官方 run 文档](https://docs.conda.io/projects/conda/en/stable/commands/run.html)

**批准/持久化。** 能力契约 revision 变化使正式 plan/capability hash 变化，走现有 Planner 修订；runtime 值变化使动作批准身份变化，重新动作批准。旧动作不原地注入。原地更换可执行版本须触发 A1 指纹过期，路径相同不代表批准仍适用；只记录必要依赖身份，不把所有环境变量灌进上下文。

**验收/风险/回退。** M01/M02/M03/M04/M11；同工具两版本按约束选择，两个 Java 动作在真实 runner 分别调用正确合成入口，父进程不变；验证与执行 argv/env 一致；需要的每个子命令有证据；空 runtime 合法动作仍可执行。风险是新启动方式隐藏依赖/信号差异，故先合成子进程再真实小样。回退能力 revision/启动接线，拒绝不兼容旧 pending action，保留已提交结果。

### A3 执行失败后的已有环境恢复

**断点。** Q6；`RuntimeEvidenceRecovery._scope/create_request` 只接受未派发且有 required 的动作；本例已失败且空 required。不能简单删除这些检查。Recovery 工具没有直接候选检查入口，文本知道 bio_wgs 不能提交健康事实。

**改动。** 复用 `agent_core/workflow/runtime_evidence_recovery.py`、`recovery_tools.py/recovery_agent.py`、runtime graph 与既有 plan_tracker 修订/动作重生路线：在失败所属 attempt 已结束、ownership 已释放、任务/步骤 scope 匹配时，用独立现有恢复上下文发起 A1 只读检查；把验收证据提交到当前任务权威位置，再提出正式重生成或计划修订。继续让原 `_not_dispatched` 保护旧动作；服务的是新恢复请求/新 action。

**行为/兼容。** 已有候选可修复选择，不先问安装；需要改工具实现、版本约束或科学计划则明确正式修订/重新批准；拒绝同案后等待新事实，不重复问。继续现有 waiting_user/手动处理/blocked 语义，不另建枚举。历史 input_check commit 保留，新 attempt 只处理未完成步骤。

**验收/风险/回退。** M01/M06/M07/M08；真实现有存储和恢复路线加只读探测替身，验证旧批准失效/保持条件、scope 陈旧拒绝、已提交步骤不重跑、检查失败不变成安装许可。风险为旧请求重复恢复；依靠已有事件身份与幂等收据。回退新增失败后入口，保留现有 prelaunch 服务和失败报告。

## B：安装职责、授权与安装后恢复

### B1 作用域化规则并提供合规安装需求入口

**问题。** Q2/Q3/Q7；88 项规则不是统一禁止，mapping 等少量 broad 规则与安装角色串入有冲突；script-output Generate 不允许 install_tool，managed/schema2 又跳旧 runtime_ready 安装预检，剩余脚本安装重定向不能替代合规需求表达。

**改动/行为。** 以 `evidence/skills_policy_matrix.md` 为范围清单，仅修改 C 类不清规则、S/E 类未限定安装建议及确需衔接者。mapping 明确“科学 action 不安装；先检查已有候选，缺少可用依赖可提出具体安装方案并委派”；保留 CALDER2/Mustache/BLAST 的特定目标和来源限制。模板同步作者规范；不要逐字重写全部88个 Skill。

在 `prompts/manager.py` 当前唯一输出协议和既有 action/outcome 适配中提供**非执行的安装需求/用户选择结果**，复用 typed `installation_requested` 和 install_subagent，保持 Planner 正式执行权；不把 install 塞入科学 shell，不恢复旧 broad JSON 或 Controller 路由。`bio_agents.py` 安装工单仅投影安装角色需要的 metadata、目标/版本约束与诊断证据，按真实截断结果移除角色错误继承，不能机械宣称全量 Skill 文本都进入安装器。

**兼容/验收/回退。** M05/M07：普通科学动作不能直装，需求能正确进入现有暂停/批准路径；历史 XML 仅在既有兼容边界读取，新生成使用单一正式协议；合法无 runtime 不受影响。旧规则的科学/来源约束继续生效。协议枚举会影响 parser/correction，因此单独验证三种业务结果互斥，回退新需求生成端而保留读取既有收据。

### B2 具体安装批准身份、复用和取消

**断点。** `approval_policy="existing_install_policy"` 是字符串，InstallerState/work order 没有主任务具体安装 scope；env_selection 仅选目标；`conda run` 前缀可跳 reviewer；quit 被当普通观察继续 Generate。

**改动。** `agent_core/state.py:259` 的 InstallerState、`agent_core/installer.py` 的 `inst_execute/router`，`bio_agents.py` 安装 handoff，`workflow/outcomes.py/early_nodes.py` 的相关 typed 收据，复用主链现有 approval_scope/identity/拒绝回执。请求在批准前包含工具/版本要求、目标绝对 prefix/目录、来源、拟执行命令、对已有环境的影响、验收方法和方案指纹。执行前核对它被当前仍有效授权覆盖；strict command 来源限制与用户授权分别校验。

**行为。** 已批准同方案不在各节点反复问；目标/版本/来源/影响改变按既有机制重新判断；科学计划批准不自动授权安装。取消/拒绝采用现有收据持久化，拒绝同方案不循环；用户明确选择手动或另给环境后转 A1/B3。低摩擦只允许已有明确只读探测能力，不对任意 conda run 包装命令免审，不用更复杂脚本语义扫描补洞。

**验收/兼容/风险/回退。** M05/M06/M07：无授权 runner=0；相同scope复用；改目标/版本失效；conda run 包管理不能绕审；quit终止不再生成。旧恢复缺 scope 时展示具体方案重新判断，不承认旧字符串等于授权。风险是漏掉某一执行分支，需参数化覆盖 bash/python/注册与取消，不安装真实软件。回退时停用安装执行入口，保留只读发现与人工交接；不能恢复无授权自动执行。

### B3 目标匹配、阶段收据、幂等登记和原步骤恢复

**断点。** 注册标签可内图 success；parent 有复检但与正式动作的验证语义不一致；TypedInstallSubagent 使用 any(ok)，旧成功可掩盖目标失败；源码有 `_mark_plan_current_done` 旧推进；两类 registry 更新不是原子恢复；安装次数计数不是幂等安装身份。

**改动。** `InstallerAgent` 产出可核验的安装执行/验证/登记分阶段收据，复用现有执行证据、immutable对象与 task store；`PackageManager.register_environment` 与 `SkillManager.record_environment` 保持各自既有职责，用关联收据让部分登记失败可只补登记。`TypedInstallSubagent.classify` 匹配 task、当前安装请求/attempt、Skill/capability、目标prefix，并要求全部必需子命令健康事实满足要求。内层文本 success、rc0、用户口头完成和 registry 条目都不能单独成为 available。

**行为。** 验证通过后失效相关缓存、由 A1 producer 写权威事实、由 A2 形成正式动作、A3 回原未完成步骤。移除 managed 安装成功直接 `_mark_plan_current_done` 的兼容推进；科学完成仍由原执行/验收/CommitStep负责。安装已成功而登记失败时不重复安装；健康失败时保留目标已变事实，诊断运行依赖，不用重装循环掩盖。重复恢复按同一请求身份返回已有阶段结果。

**验收/兼容/风险/回退。** M08/M09/M10：以替身安装器加真实 registry/证据/恢复路径覆盖全成功、rc0不能启动、登记失败、旧成功+目标失败、旧default环境健康但批准新target不可用、重复恢复、人工完成；保留 input_check，不出现安装替代科学 commit。阶段收据新增字段可向后读取，但旧条目只能候选，不能默认为验收通过。跨两个 registry 无法一次写时用可重试收据和当前事实校验，不能假事务。回退自动恢复、保持失败和已安装事实，不回滚共享环境或删除用户软件。

### B4 可操作的失败与人工交接

复用 B3 收据和 `workflow/managed_stop.py`、`runtime_recovery_reporting.py`、现有 AskUser/Recovery暂停载体。保存命令实际 argv/cwd/白名单环境、数值 rc、日志引用和关键 stderr、失败阶段、已知残留；区分权限、网络/源、求解、构建、兼容、验证、登记，未知单列。

手动方案必须来自已明确目标和版本约束，写出可确定命令、未验证前提、预期结果及返回 BioMind 的重新检测方式；不把失败原命令原样作为唯一建议。不默认 sudo、删除环境、全局升级或关闭证书验证。手动完成/另给已装prefix在同一待处理请求上更新事实并触发 A1/B3，不能直接恢复昂贵计算。

M07/M09/M10/M12 验收：失败信息可复制/定位；未验证命令不伪装已验证；人工完成仍重验；同案拒绝不再询问。可与 B3/C2 合并；持久化沿现有对象/收据，回退自动继续但保留人工说明和证据，不动现有软件。

## C：终态、最终原因与本次诊断

### C1 同步确切步骤的停止状态

Q10；`workflow/managed_stop.py::_save` 当前只在特定 candidate_scope_identity 下更新 step。按已有 task/step/attempt/ownership guard，让 post-execution 终止也同步对应未提交步骤；保持已提交步骤，后续步骤仍表示未执行/受依赖阻塞，不把 running 当进程状态，不将所有 pending 改失败。

复用 set_task_status、既有停止持久化和 CommitStep 收据。历史不可变 attempt 不批改；显示可对旧状态作有依据投影。M12 检查 global blocked 无不合理 current running、stale scope 不写别的步骤、input_check保留。风险是误结束另一 attempt，因此先 identity check。回退仅局部同步逻辑，不重启进程或复跑任务。

### C2 最终具体原因、取证历史和可达路径

改 `managed_stop` 当前事实投影、`agent_core/cli_lifecycle.py::_cause_text/terminal_text/delivery_text` 和现有停止报告字段消费。优先呈现已核实的 `missing_commands=[picard]`、空 bindings、失败阶段，并明确已有环境尚未验证；不把模型“缺能力”推断当独立事实。相同 evidence identity 后来读取成功的旧错误保留历史但撤出当前问题列表。

显示本次 report 绝对路径、已提交步骤、有效恢复尝试、下一步动作、交付 conflict 与内部归档 complete，正常变异统计未产生也能停止收尾。M12 复放原 fixture，校验新原因/已解决错误/真实路径、旧报告hash不变；复用现有报告路径不新建流水线。格式变化保持既有 CLI 结构化结果兼容；回退展示投影，不删历史或覆盖旧输出。

### C3 提前暴露目标文件冲突

在 Consult 收集输出/Planner 正式输出确定阶段，只读检查目标与 ownership。用户要求 exact path 时保持该约束并明确冲突；选择本次独立位置需反映到现有正式计划/输出批准中，不能悄悄换路径。复用 managed diagnostic 内部唯一目录；满足现有归档条件时显示真实路径，归档失败时明确失败回执。

M12 验收：旧 9 月 8 日文件保持原hash，9 月 17 日诊断有本次路径；内部归档与指定位置交付两种结果分别报告，未发生真实分析也能交付停止报告。风险为输出约束升级影响计划批准；只在正式修订后采用新目标。回退提前检查，保留最终 writer 的保护与fallback。

## D：helper、协议和科学验收

### D1 收口当前固定 helper 入口修复

复用已有 `agent_core/skill_script_invocation.py`、`skill_resource_binding.py`、mapping `generate.script_invocations` 及 Generate/Execute 的冻结/复验接线。本轮初始源码与旧69测试记录匹配，收尾时 bio_agents 已并发变化；实施时必须核最新字节，而不是重复添加另一套入口。

M13 用正常0644 helper、明确 interpreter/argv/delegates、错误参数/解释器漂移 fixture 复用既有测试，重跑受最新集成影响的一组；不chmod所有脚本，不分析任意科学代码。版本/资源身份变化按原审批重生；风险为同名解释器/venv resolve差异，依既有file identity回退单个契约，不全局PATH修补。

### D2 最小输出协议一致性与 native 接收证据

Q11；Generate 已有规则却产生两次格式错误，Recovery历史 system也已声明schema_version，当前有严格适配但原参数保存与准入无法由归档normalized tool_calls回推。

在 `prompts/manager.py`、`public_response.py/controller_audit.py` 的现有模板/解析边界，令正常/纠错上下文共享唯一协议、合法最小示例和精确失败反馈，保留现有单块兼容，不全面换协议。`recovery_agent.py/recovery_reception.py` 及已有 provider 接收适配保留原始 native arguments 字符串与complete元数据；只有证明完整、无重复键且仅缺唯一版本常量的既有适配准入才补1，其他缺字段/截断仍拒绝。不要新增语义normalizer。

分别验证历史两Generate错误、正确envelope、corrective blocked、B1新增非执行需求互斥；Recovery完整native/缺其他字段/重复键/截断，保证原始证据留存。无原始证明时继续现有显式correction，不凭猜测修参数。协议与收据向后可读；风险为错误接受截断参数，回退适配接线保持严格validator和已有纠错。

### D3 科学 bundle 完整性（恢复前必须完成）

Q9；原 approved.py 的 `if produced` 与冻结 `directory_exists` predicate不能证明完整成功。**修改范围是能力资源/科学验收契约和现有验证器，科学脚本仍由既有模型生成。** 不把原会话生成脚本当生产固定helper来硬编码修补。

在 mapping Skill 的 `prepare_reference` 能力/产物说明明确所选 BWA 版本的完整索引成员、FAI、dictionary、reference身份和必要一致性；正式 plan artifact/success criteria 带入这些要求；Generate按要求检查各命令结果；`completion_gate.py`/现有 artifact验证与CommitStep消费必要完整性收据。复制参考和新产物采用现有独立attempt/所有权边界。所有必需结果通过才产生/提交完整bundle；部分产物可留失败证据，不伪造成功、不污染已验证产品。

M14两种原脚本替身成为修复后negative controls；补缺失、空文件、错误contig、旧来源无验收的拒绝案例；完整一致、来源可追溯且允许复用的接受案例；真正允许恢复前还需经授权的微小真实FASTA与reads科学验证，覆盖FAI/BWA/dictionary、MarkDuplicates与所选caller入口/下游兼容。帮助成功不替代该层。版本/计划输出契约变更走正式revision+approval；旧bundle无来源证据不能自动迁移为通过。

风险是过度约束不适用于另一合法工具版本，因此必需成员由所选能力实现/版本确定，禁止全局工具文件配对。回退新版验收时关闭相应科学恢复，保留原已提交步骤和失败证据，不能回退成部分成功接受。

### D4 条件性科学依赖拆分

默认暂不实施。先保留bundle并完成D3；若有明确需求让独立消费者提前执行，再经单一Planner把BWA索引与GATK参考辅助文件划成少数有消费者依据的阶段/产物。收益是降低无关阻塞，成本是plan/approval/artifact identity、引用路径和复用来源的迁移。字典替代只能在本机参数/字段/下游验证后正式选择；不能据此跳过Picard MarkDuplicates，也不静默换calling方法。倍性等参数可前置确认但不作本次主因。

验收是新计划依赖正确、审批变更可见、相同参考来源一致，旧冻结计划仍不被原地改写。回退正式计划版本，不删除旧产物或伪造依赖完成。此补丁不应阻塞C/D2等独立修复。

## E：按修复后数据减少重复调用

### E1 计量先对账

在 `execution/profiling.py` 与既有 terminal_summary 调用计量接线，按唯一request/attempt/event identity把21个归档调用与metrics核对；候选拒绝、执行重试、提案修正、有效只读工具调用分别统计。标明主墙钟、人工等待、模型/命令嵌套区间；terminal_summary本次已在墙钟内，不能再加一次。分别给prompt/completion/cache字段，tokens不自动变费用。

复用原session离线统计fixture，无模型调用；既有字段语义保留或显式版本化，新增分类字段不能改写历史“correction=Supervisor RETRY”含义而不说明。通过标准为21 requests可对账、3 commands/4 confirmations准确、时间无双计。风险是重复trace事件，去重以已有稳定身份；回退新增聚合字段保留原始事件。

### E2 数据驱动的上下文与重复调用优化

在A/B/D修复后，对相同边界故障比较有效诊断、重复格式纠错、候选检查cache、token/墙钟。优先删重复的已解决证据投影/不相关Skill全文，继续现有独立Recovery seed、progressive resource读取与context epoch机制。只优化可量化重复内容，不能先减预算或必要取证、改科学线程数。

拟改范围限既有node_context/planner_context/provider上下文投影及profiler证据支持的具体生产者；未得到对照数据前不预定文本压缩比例或新上下文结构。验收保留诊断证据、审批身份和成功率边界，并报告同口径变化。回退单个投影改动即可，不影响已存事实/正式计划。

## 排除项与候选比较

| 候选 | 结论与依据 |
|---|---|
| 只补 Picard 路径 | 可成为A2某动作的一部分；不能解决Java/launcher、MarkDuplicates、GATK、事实投影和安装恢复，不作总方案 |
| 全局激活 bio_wgs / 修改 shell rc | 排除；绕过动作级选择，影响多环境隔离和批准可追溯性 |
| 所有 Skills 都允许直接安装 | 排除；88项政策作用域不同，科学步骤与安装职责必须分开 |
| 换 samtools dict | 仅候选能力；需本机参数/字段和下游验收+正式修订，不能替代MarkDuplicates |
| 所有 action 强制非空 bindings | 排除；现有合法generic/只读/helper动作必须保留 |
| 新增通用Controller或registry | 排除；现有runtime_evidence、approval、typed graph和CommitStep具备复用基础，真实问题是接线与职责 |
| 全面采用 conda run | 本轮不推荐；本机启动链未完整验证，按动作依赖与信号/输出要求比较；显式注入同样必须验证闭包 |

## 下一轮交付与通过门槛

实施开始重新记录完整HEAD/工作区和加载版本，复核本轮并行变动；逐逻辑补丁显式暂存相应文件，不混入用户dirty。每个补丁报告改动、验证、未运行范围和回退。测试以本轮14类矩阵参数化复用为主，不为每句政策建立逐字测试。

A/B的安装恢复完整链先用替身安装+真实证据/registry/审批恢复验收；D3真实软件小样与最终WGS分别列层级，依用户下一轮授权执行。任何真实安装都须具体范围授权；已有有效授权复用，不能将本轮审计授权沿用为安装授权。

最终只有在相应门槛通过后才恢复原未完成步骤；保留input_check提交，重新检测现有环境/产物和批准身份。生产修复、实际安装、真实小样、真实端到端的结果必须分别报告，不以现状见证PASS代替任何后续层级。
