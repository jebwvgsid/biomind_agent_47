# Q1 / Q4 / Q5 / Q6 环境到执行链审计（现状记录）

证据仓库根：`/public/home/liusiqi/03.毕设/Biomind_v1`。历史现场根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497`。下文代码位置均相对仓库根，历史位置相对现场根；主报告引用时使用完整路径。2026-09-17 本轮仅隔离审计。

## 已确认的主结论

1. **不是 Consult 完全没检查环境。** 历史 `plan_state.json#/task_state/runtime_evidence/records/0` 明确有 `consultant_runtime_inspection`：2026-09-17T07:03:59Z mapping 检查 succeeded，环境 bio_wgs，已验路径 `/public/home/liusiqi/anaconda3/envs/bio_wgs`。但唯一事实是 `runtime.environment.prefix`；不存在 Picard executable、CreateSequenceDictionary、MarkDuplicates 或 Java 兼容性事实。`records/1–2` 则记录 bio_gatk 检查 execution_error，prefix 为 UNKNOWN，不能写成 GATK 已验可用，也不能写成真实 calling 已执行失败。
2. **正式能力明确不索取 runtime。** 当前 `skills/genomics/wgs-short-read-mapping/SKILL.md:104–110` 的 prepare_reference 能力 required / optional runtime 列表都为 `[]`，同文件 125 行的 default_env 是发现/健康检查选择，不是动作执行承诺。`SkillManager.get_execution_capability_registry`（2059）优先采集显式能力；`resolve_action_runtime_bindings`（`agent_core/action_runtime.py:475`）只解析 required + 被明确请求的 optional；不会从 packages/default_env 或脚本内容猜绑定。空 manifest 是合同的直接结果，不是 resolver 漏把已有 Picard 绑定拷进去。
   补充：当前生产调用点未传入 `requested_optional_ids`（其仅在resolver形参/内部出现）。因此仅给Skill添加optional列表也不会让现有动作选用这些项；若未来采用可选实现，必须让现有正式选择载体传递被批准的optional选择，并纳入manifest/批准身份，不能偷偷取全表。
3. **历史动作确实空绑定，环境事实没有进入正式选用。** `attempts/attempt_51bd254a3abad5bfdfa0/runtime_env_manifest.json#/bindings=[]`；`approved.py` 对裸命令做 `shutil.which` 后缺 picard，rc=1。当前环境事实即使 ready，也不会自动扩大空能力：真实 resolver 隔离见证 PASS，向 registry 同时提供 verified prefix 和 Picard 路径仍产生空 manifest。
4. **历史 Planner / Generate / Recovery 知道文本 `default_env: bio_wgs`，但未收到已验证 prefix。** 实际 request 的 `inherited_skill_material` 含 bio_wgs；Planner `planner_initial-0006-62fd286e6ccb/request.json`、Generate `generate-0016-d4f379d86176/request.json`、第二次 Recovery `recovery_turn-0018-f4de4bd43eec/request.json` 都没有 `runtime_evidence` / `runtime_audit` 或已验绝对 prefix。不是 Skill 重选。`accepted_consultant_handoff` 无 runtime_requirements；代码允许语义 runtime_requirements，但不会把完整审计自动塞给 Planner。
5. **第二次 Recovery 是模型选择 blocked。** `context_trace/recovery_turn-0020-5745b56334d0/response.json#/tool_calls/0/args`，action=blocked，reason_code=`MISSING_REQUIRED_CAPABILITY_DICTIONARY_TOOL`，要求保留 input_check、等待已验证工具或授权安装。不能写成框架预算耗尽。当前 Recovery 工具是受限读取 failure/step/artifact/skill/evidence；没有任意 shell/环境发现安装工具。既有 prelaunch runtime-evidence recovery 也不能拯救本例空声明：`RuntimeEvidenceRecovery.create_request` 255–263 拒绝空 required，`_scope` 160–166 要求动作尚未派发。

## Q1：配置链

四项 `.env` 白名单文件声明值均与任务书一致：

| 项 | 文件声明 | 组件职责 |
|---|---|---|
| BIO_SOFTWARE_ROOT | `/public/home/liusiqi/03.毕设/agent_shim` | PackageManager.root_dir；权威旧 registry 为该根下 software_registry.json；shim_dir=root/bin_shims |
| BIO_SOURCE_INSTALL_DIR | `/public/home/liusiqi/03.毕设/source_tools` | PackageManager.source_root；源码安装提示；受控 runtime inspection 额外只读根 |
| ANACONDA_ROOT | `/public/home/liusiqi/anaconda3` | PackageManager 配置 conda/mamba 候选及 envs/name 发现根 |
| BIOMIND_SKILLS_DIR | `/public/home/liusiqi/03.毕设/Biomind_v1/skills` | config.SKILLS_DIR → SkillManager.skills_root.resolve() |

本审计 `login:false` 子进程中四项均未设置；这是审计进程，不代表 BioMind 现场进程。有限 `/proc` 扫描未发现 argv 前三项明确匹配 bio_agents.py / biomind 的活进程；进一步 `ps` 仅可见工具沙箱的 codex 与当前命令，故**无法据此检查主机 BioMind 活进程环境**。未取得原进程有效值；历史 PATH 无法回溯。不得把当前帮助启动反推事故当时环境。

加载顺序：`config/__init__.py:5` 先导入 settings；`config/settings.py:79–90` 依次加载代码根 `.env`、启动 cwd 的 `.env`，后者非空值覆盖前者及进程现存非空值（空值不会覆盖已有非空）。随后 `config/config.py:28–40` 只为缺失/空环境变量补项目 `.env` 默认；52–54 / 418–424 固化配置常量。最后 startup 再调用一次同类 loader（`config/startup.py:38–55,90`）。PathManager.WORK_DIR 取导入时 cwd；不同 cwd 的 `.env` 有影响，常量导入后改进程环境不会自动更新消费者。未导入配置执行这些加载；隔离见证禁止所有 `.env*` 打开并绕开包初始化。

PackageManager 初始化会 mkdir、load_from_yaml、repair_registered_shims（65–105），所以本轮不在真实目录实例化。BIO 两目录构造成 Path，未主动 resolve/expanduser；相对值受 cwd 影响。ANACONDA_ROOT manager 路径 expanduser，优先已配 mamba/conda，fallback PATH；`_get_conda_root` 可使用 manager info / manager路径 / 配置 / CONDA_PREFIX 推断。Skills root resolve。没有“配置这四项后所有 worker 自动激活 bio_wgs”的接线。

## Q4：精确链路与断点

| 跳 | 生产者 → 接收者 / 持久化 | 实际内容与边界 |
|---|---|---|
| 发现 | SkillManager.get_runtime_requirements(761) → BioAnalystAgent._consultant_runtime_preflight(19599) → _runtime_audit_for_skill(19315) | default_env=bio_wgs，包装健康检查，读取 Skill registry 与 PackageManager registry |
| 健康检查 | SkillManager._wrap_runtime_healthcheck(663) → PackageManager._direct_environment_healthcheck(442), verify_environment(667) | 原 bash -lc 被换成 conda run -n bio_wgs bash -c；PM 去掉 conda 前缀，子进程 PATH 前置 bio_wgs/bin、加 CONDA_PREFIX/DEFAULT_ENV；保留其他 ambient（包括可能的 JAVA_HOME）。真实方法隔离见证已通过 |
| 事实 | _runtime_audit_for_skill.finalize(19339) → _verified_runtime_paths(19235) / prefix存在补充(19397) → 持久化 runtime_evidence | helper健康检查无 RESOLVED_PICARD_BIN 输出，仅补 env prefix；ready 只是所声明粗检查通过，不等于所有所需子命令 |
| Consult handoff | _accepted_consultant_handoff_projection(6838) / schema2_phase_seed(3133) | 允许模型提交 runtime_requirements 语义；历史 handoff 未填。完整 runtime audit 不应当被误认成已进入实际请求 |
| Planner | bio_agents.py:38986 build_node_context(runtime.input_audit=runtime_audits) → agent_core/node_context.py:1561 _compact_planner_runtime_projection | 当前别名查 selected_environment/environment/environment_name/conda_env，未查审计实际使用 env/checked_env/declared_env；resolved_executable 别名未查 resolved_runtime_paths。取 raw + reversed(rows) 的 first，不按 Skill 展示；可降级/遮蔽多 Skill 状态。历史请求没有目标 prefix，当前源码这一投影是可定位断点 |
| Generate | bio_agents.py:42020 resolve_action_runtime_bindings → project_action_capability_context_for_provider / structured_action(44013) | required/optional 为空，生成的当前动作绑定为空；只含合法 artifact 等。继承 Skill 文本的 bio_wgs 不构成选择 |
| 批准/执行 | pending_action runtime manifest → Execute 47878 重新解析并比 manifest hash；48187 build_exact_action_environment(os.environ, action.env, controller_identity) → execution.executor.run_script_path(894) → run_command(504) | 实际只消费经过动作验证的 env。不会从 registry 再注入 PATH/Java。run_command 传 env 到 asyncio.create_subprocess_shell，cwd沿现有进程，入口 runner来自不可变脚本及解释器。原批准 Python 内部裸工具查找继承此执行环境 |
| Recovery | failure packet + 同一 Skill 文本 + 限定 evidence tools → submit_recovery_directive | 独立上下文设计保留；`node_context.py:2171` 明确不把完整机器 runtime snapshot灌入 Recovery。需提供最小语义环境诊断与正式检查入口，不能恢复旧上下文混注 |

## Q5/Q6：现有能力、缺口与推荐

现有 runtime_evidence 支持 AVAILABLE/UNAVAILABLE/UNKNOWN/AMBIGUOUS；registry冲突会失败关闭；prelaunch路径失效有独立 `RUNTIME_BINDING_PRELAUNCH_PATH_INVALID`。`RuntimeInspectionAdapter` 限定 Skill、环境指纹、方法摘要、根目录和输出schema；60秒、64 KiB上限；探测nonzero、sandbox缺失、timeout分开；只输出白名单事实。旧 PackageManager.verify_environment 180秒 capture_output，无硬输出上限，所有异常归 broken；粗健康检查未区分 Picard 子命令/Java。旧进程runtime cache检查env存在后可复用成功；持久化健康检查缓存默认1小时、同命令；新的task evidence还有scope/expiry约束。不能以 UNKNOWN/粗检查失败推断整机未安装。

本例 WGS healthcheck 带括号/管道，现有 `RuntimeInspectionAdapter.describe` 已在隔离见证中首先返回 `RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED`（真实 `_admit_script`，不是模拟回执）；之后还存在不产生 `RESOLVED_*`、无 inspection_outputs、execution capability 空required 的合同缺口。即使 bwrap可用，也不能直接用本声明证明缺失 Picard。其允许根只有声明prefix+source_tools，不包含默认shim根；不是任意盘全扫描工具。

推荐 A：按实际科学操作补现有 Skill capability runtime声明和inspection_outputs；发现选择先用户/Skill/已有注册/配置根的有界候选，验证具体 Picard及Java/subcommand后写现有 runtime_evidence。提供按Skill语义投影，消除Planner字段别名断点；Generate按已批准选择形成明确脚本入口或action.env（现有程序不会自动从bindings拼PATH）。按操作选 prefix执行或显式 executable/interpreter/env，而非全局混合多个bin。修正式范围必须随现有Planner修订与动作审批生成新动作，不在冻结动作中换环境。

恢复路径：裸命令查找失败 → 同failure packet识别“尚未绑定/未知” → 只读候选探测 → 验证并提交证据 → 若只改当前动作且既有能力/计划范围容许则重生成并重新动作审批；如果要加runtime能力合同、改工具实现或科学方法则现有正式Planner修订、重新批准 → 新attempt → 原CommitStep验收。保留 input_check 已提交结果。现有prelaunch runtime-evidence service可复用，但需独立补齐“动作已失败后、释放旧执行所有权再进入新动作”的合法接线；不能取消 `_not_dispatched` 或跳过批准。

现状见证：`evidence/runtime_witness.py` 调用现有 resolver、路径验收、Skill健康检查包装、PM环境转换、RuntimeInspectionAdapter.describe 与真实 runner；**11个场景 PASS**。包括空能力不消费候选、合法generic空绑定、多候选冲突、失效路径、带环境检查、受控PATH查无Picard、两个动作不同Java和父进程不被污染。还证明：仅提供 bash/head/samtools/picard、完全不提供 bwa/bwa-mem2 的受控PATH中，真实Skill→PM健康检查仍 rc=0，因为 `bwa | head` 无 pipefail，失败可被head吞掉。这是**当前粗检查未触发误报风险**，不是本次已经发生BWA缺失。第一次用系统Python 3.9导入失败；换已有 biomind Python3.12 后成功，未安装。PASS仅确认当前行为，未修复缺陷。

### Planner 证据投影的额外精确结论

历史 Planner 三份实际请求 `planner_initial-0006-62fd286e6ccb`、`planner_initial-0007-154b34f72bde`、`planner_initial-0008-d5e9808aca76` 的 `/messages/1/content` 均含 `semantic_history.runtime={"runtime_ready":true}`，没有按Skill环境清单。与此同时 durable 有bio_gatk的UNKNOWN。路径为 `_runtime_binding_source` 返回可供绑定的正向audit + evaluations；Planner只传入 `runtime_audits`；`project_runtime_audits`（`agent_core/runtime_evidence.py:379–400`）有意仅投影AVAILABLE绑定，丢掉UNKNOWN的展示；最后 `_compact_planner_runtime_projection`取单一ready bool。这种正向绑定兼容视图不适合充当Consult/Planner完整环境诊断。修复应新增**同一现有证据的按Skill语义投影**供Planner消费，保留正向绑定视图原授权职责；不是新增registry，也不是把UNKNOWN改成missing。

### Q5：当前本机软件探测结果

完整参数/启动环境白名单/时间/退出码/超时/输出上限见 `evidence/runtime_probe_results.json` 与 `runtime_gatk_followup_results.json`。所有科学工具只用帮助/版本或无输入usage；未传入真实FASTQ、FASTA、BAM、VCF。主探测每条20秒，补充每条10秒，stdout+stderr 64 KiB硬限。软件输出先出现但I/O收集在限时内未完成的情况如实保留 `outcome=timeout`，即使child rc=0也不标完整通过。

| 候选/启动方式 | 观察 | 可得结论 / 不可得结论 |
|---|---|---|
| PATH=/usr/bin:/bin，`command -v picard` | rc1 | 仅此受控PATH未命中；不是主机未安装 |
| `/public/home/liusiqi/anaconda3/envs/bio_wgs/bin/picard CreateSequenceDictionary --help`，同一精简PATH | 完整usage，rc1，completed | 同一候选显式入口可启动并识别字典子命令；Picard旧版help返回1不能当执行失败；未生成字典 |
| bio_wgs/bin在PATH首位，`picard MarkDuplicates --help` | 完整usage，rc1，completed | MarkDuplicates子命令可识别；未处理BAM，不证明去重兼容 |
| bio_wgs java | 输出OpenJDK11.0.1；首版探测收集timeout，独立文件捕获复测rc0/0.946秒完成 | 可启动；首版timeout为探测等待异常，不能解释成Java超时。包装器内部优先自身prefix Java，可被JAVA_HOME覆盖，需正式动作控制 |
| bio_wgs bwa index / mem | 显示相应usage，rc1；index completed，mem首版探测timeout，独立复测rc1/0.055秒完成 | 索引/比对命令被识别，未生成索引或比对；不能把mem首版探测timeout解释成BWA挂住 |
| bio_wgs samtools | 版本1.3.1；faidx/sort/index/flagstat/depth/quickcheck/dict各能显示usage或选项诊断；部分探测timeout | 工具过旧，`--help`非零本身不是工具不存在；需要实际参数与产物小样验收 |
| 同一samtools `coverage --help` | `[main] unrecognized command 'coverage'`；首版探测timeout，独立复测rc1/0.091秒完成且同错误 | 子命令确实不支持，是当前Skill后续QC风险；不能写为本次已发生错误或软件超时。`dict`只证有候选能力，不能静默替代Picard或解决MarkDuplicates |
| bio_gatk PATH，裸 `gatk HaplotypeCaller --help` | rc255，completed；`Launcher class not found` / `org/graalvm/polyglot/PolyglotException` | GATK wrapper shebang是 `/usr/bin/env python`；bio_gatk/bin/python链接到Graal Python，启动器失败发生在GATK Java业务运行前 |
| 同一gatk，用 `/usr/bin/python3 <gatk绝对wrapper> ...` 且仅bio_gatk/bin:/usr/bin:/bin | GATK4.6.2.0 --version及HaplotypeCaller、CombineGVCFs、GenotypeGVCFs、SelectVariants、VariantFiltration、ValidateVariants --help均rc0/completed | 明确Python入口+环境Java21可启动这些所需子命令。不能说所有GATK都不可用，也不能将此审计选择当作原动作已批准选择 |
| bio_gatk `bcftools stats --help` | usage，rc1/completed | stats子命令可识别；未读VCF统计 |
| 配置根conda `run --help` | 20秒timeout、SIGKILL、无有效输出 | 本机conda run/prefix启动未验证；不据此断言语法不支持或Conda未安装 |

Picard入口真实链接到 `bio_wgs/share/picard-2.20.4-0/picard`，脚本19–26行先选 `$ENV_PREFIX/bin/java`，非空JAVA_HOME可覆盖。GATK wrapper链接到 `bio_gatk/share/gatk4-4.6.2.0-1/gatk`，第一行依赖PATH中的Python，240行选择裸 `java`；bio_gatk/bin/java链接到lib/jvm/bin/java，当前版本输出21.0.8。**只绑定工具路径并不足以涵盖launcher解释器与Java；全局拼多个环境PATH也不安全。** 原GATK inspection_execution_error没有保存可定位的启动阶段stderr在本摘录中，不能把当前Python问题直接归入历史GATK检查。

安装元数据见 `evidence/runtime_install_metadata.json`：bio_wgs/conda-meta/history的2026-07-04事务包含Picard2.20.4、OpenJDK11.0.1、BWA0.7.19、samtools1.3.1；bio_gatk 2026-07-03注册GATK4.6.2.0、Picard3.4.0与Graal Java17，2026-07-10事务将Java换成21.0.8、samtools换成1.23.1。**这是事故前存在安装记录的支持证据；不是历史文件字节不可变证明，更不是历史PATH或分析可用证明。** 没有把Conda包的build timestamp误当安装时间。

首版probe的完成判定把process.wait与stdout/stderr管道EOF一起等待，出现child已退出而await超时的观测。`runtime_probe_wait_check.py` 在相同argv/PATH、独立TMPDIR改用真实Popen + 普通文件捕获 + poll + 5秒/64KiB限，java、bwa mem、coverage三条均迅速结束。因此将首版相关结果定性为 `PROBE_WAIT_OR_PIPE_COMPLETION_ANOMALY`；未复测项目仍保留“探测未完整收尾”，不将工具判坏。Conda单条确实无输出后被限时终止，仍不能推断是软件不存在。原始JSON保留原outcome和初始unrun字段，并补followup_correction：GATK原先暂缓的子命令现已用显式Python/Java环境完成help检查。

### Q6：复用现有正式修订路径的准确位置

`agent_core/workflow/recovery_agent.py:123` 的实际协议允许 retry_step / revise_plan / reconsult / revalidate / ask_user / blocked / run_diagnostic_probe / organize_result。`recovery_runtime.py:717–789` 将 retry_step映射candidate_repair_required，revise_plan映射plan_revision_required，ask_user映射user_input_required，blocked直接终止且不消费恢复预算；856–863写现有planner_revision_request，864–907为新Generate绑定已有RecoveryExecutionEnvelope、诊断准入与generation authority。当前mapping Skill的diagnostic_operations仅samtools_header和binary_prefix，没有环境发现操作。run_diagnostic_probe只接受已注册operation和artifact handles，不能把任意conda/which塞进去。

真实runtime修复episode只由 `bio_agents.py:42864–42893` 的 **Generate前 required binding缺失** 激发；必须 required=true、failure_scope=runtime_fact、schema2。原Picard查找是在已执行脚本内部发生，能力空required没有激发这个路由。保持已有状态语义和审批；补齐入口与operation覆盖才可形成可达“发现→验证→正式绑定→新动作”恢复链。

### A补丁范围建议与执行方式比较

| 逻辑补丁 | 复用/拟改位置 | 验收 / 风险与回退 |
|---|---|---|
| A1 能力与环境验证合同 | 两个现役WGS Skill的runtime inspection_outputs、按操作execution_capabilities；SkillManager runtime健康检查契约；不借脚本语义猜工具 | prepare_reference必须证明FAI/BWA索引/dictionary完整能力，mapping含实际去重实现，calling含Python/Java/子命令。保留input_check/generic空binding合法。修复粗BWA pipeline误报、coverage版本缺口；涉及能力hash变更，旧计划应按现有drift/审批处理，不能热补冻结计划 |
| A2 有界发现与语义事实传递 | PackageManager旧注册发现 + RuntimeInspectionAdapter + runtime_evidence；bio_agents._runtime_binding_source与Planner node_context投影 | 缺PATH但候选存在优先发现；多版本/失效记录不自动挑第一个；UNKNOWN不变missing；Planner按Skill显示已验/未验事实；不复制全量环境。可与A1合并验收，否则只加bindings会直接blocked |
| A3 精确动作启动方案 | 现有Generate action contract/project_action_runtime_for_provider；action.env与script adapter；runner原审批身份/文件路径验证 | 同一次验证选用的executable、Python、Java、必要env进入正式动作并重新批准；两动作环境隔离，ambient JAVA_HOME不能误覆盖已选Picard Java。应避免恢复legacy build_action_execution_environment自动注入链。风险是环境激活依赖遗漏，回退到既有fail-closed等待，不全局激活环境 |
| A4 失败后修复已有选择 | recovery_runtime现有retry/revise/reconsult接收、诊断operation准入、RuntimeEvidenceRecovery的prelaunch机制 | 补新候选/证据访问与执行失败后的合法新动作接线；同Plan允许的动作修正重审action，能力/实现/方法变化走正式Plan修订。保留completed步骤、禁止同拒绝授权重复询问。不能扩大run_diagnostic_probe为任意shell或删除execution ownership条件 |

完整prefix执行可用于需要激活脚本/动态库等环境整体契约的工具；但本机Conda `run --help`仍未完成，且仅把prefix/bin置前不能保证所有activation变量齐备，下一轮需在隔离样本中核实指定prefix启动。显式executable/interpreter/env适合本例已识别的固定入口：Picard wrapper自定位jar/Java但需要控制JAVA_HOME，GATK wrapper必须明确Python与Java所在环境。本轮证据支持**按能力选择这两种方式并统一到现有正式action批准对象**，优先用本机已验证的显式入口解决Picard与GATK launcher，随后为确需整体环境的操作验证prefix方案；不规定所有工具一律Conda，也不在主进程拼多环境PATH。

### 本轮验证状态与未验证边界

| 类别 | 本轮结果 |
|---|---|
| 当前行为隔离见证 | 11 PASS；缺陷仍存在，不计为生产修复 |
| 软件帮助/版本/usage | 27条最终保留记录，混合completed与timeout；按上表解释非零与探测失败；补充文件前两条曾为区分启动器重复运行一次 |
| 修复后回归 | 未实施生产补丁，未运行 |
| 真实软件小样产物验证 | 未运行；没有FASTA/BAM/VCF样本输入/产出 |
| 真实安装、模型/API、原任务恢复、完整WGS | 均未运行 |
| 主机活进程有效环境与历史PATH | 未取得；工具PID namespace无法代表主机 |

证据索引：`runtime_history_and_config.json` 保存白名单配置、三条历史环境事实、实际request定位/计数/短摘录、manifest、源码hash；`runtime_source_excerpts.txt` 保存此次读取的关键源码片段及对应完整文件hash/时间，防外部并行编辑使行号失去含义。见证使用禁止dotenv打开的audit hook；绕过执行包无关初始化、以config替身导入PM，仅`__new__`并调用纯方法，不创建/修复生产registry，不启动BioAnalystAgent。
