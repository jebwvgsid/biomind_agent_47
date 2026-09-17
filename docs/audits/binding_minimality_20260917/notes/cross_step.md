# 跨步骤值与资源引用：Q1/Q5，以及 Q2/Q6/Q7 的直接证据

本分报告核验当前工作区，根任务记录的 HEAD 为 `4ea823511328ed4035ec2a99ddf09a3419bb288a`。不把 HEAD、当前工作区和历史运行采用版本混为一谈；本次纯路径见证的源码 SHA-256 已写入 [cross_witness_results.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_witness_results.json)。历史 trace 没有提供可独立闭合的全部加载源码清单，故只能证明历史行为和当前函数各自的事实。

**已证实的主结论：** `binding_env` 是“进程变量名→值来源对象”的声明，不是 Conda 名称，也不是实际环境值字典；`resolve_action_binding_environment` 才将它解析成真正的 `environment`。`produced_bindings` 是当前候选拟产生的有类型业务值，`consumed_bindings` 是对当前 Plan 已接纳值的精确引用。三者与 `runtime.*` 软件入口绑定是不同通路。

**本轮新复现现役缺陷：** 上游 rollback 使后代步骤失效时，仅清理 rollback target 的值绑定，后代产生的值仍可通过 `accepted_plan_binding` 读取，而且后代重新产生不同值会被 `PLAN_BINDING_IMMUTABLE` 拒绝。见下文“失效缺口”；这是成功复现缺陷，不是修复通过。

## 样本与可重放证据

- [cross_samples.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_samples.json)：20 份原证据的绝对路径、字节数和 SHA-256；完整模型候选；实际请求 binding 投影/完整输出协议/完整反馈；提交、接受值和执行记录。
- [cross_extract.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_extract.py)：只读提取，不恢复 session。
- [cross_witness.py](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_witness.py)：调用当前生产纯函数；禁 dotenv 与网络；未载入 `config`；仅编译历史代码而不执行；所有修改仅在新内存对象和审计目录。
- [cross_draft.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_draft.json)：同一真实 prepare/build 输入输出的精简草案与精确量化。草案未实施，当前 producer validator 会拒绝草案缺少的 `type`。

历史原目录前缀为 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/`。定向检查其中 135 份 `session_*/plan_state.json` 的 `plan_binding_history`，发现 5 份有 accepted 值；未据旧报告假设 1789464978 已完成跨步。

### 真实声明失败对照：session_1789464978

`context_trace/generate-0013-24c6c8bf7900/response.json`：声明 `genome_version=task_local_genome_v1`，`snpeff_db.value.handle=plan-artifact-53779ae3f11eddc6b2e480c7`；缺 `operation`，同时带一致的 `code` 和 Python `argv`；`binding_env` 错写实际值。0014 补 `operation=prepare_database` 并合法改为 argv-only，但 `binding_env` 仍错。两次候选都不是科学执行事实。

`plan_state.json` 无 `plan_binding_history`，仅 inspect/contig 两次 Commit；`executions/execution_records.jsonl` 仅两行，prepare 未启动，不能称“数据库建库失败”。历史请求 0013/0014 内容总量分别 114,639/126,177 字符（UTF-8 118,499/130,231 字节）。这统计的是所有 message.content，不是完整 JSON wire 字节或 binding 专有字节。

当前 `execution/script_validation.py:14 check_executable_action` 对两份真实候选均返回 PASS，body SHA 一致，B 的来源为 argv。`candidate_progress_assessment` 对补 operation 的 B 返回 `progress_proven=true, repeat_proven=false`。`agent_core/recovery.py:4934 RecoveryPolicy.decide` 当前仅以已证实 repeat 或预算作为此分支停止依据；`progress_proven` 是诊断。本轮未调用完整 RecoveryPolicy/graph，此 route 的验证为源码与既有记录复用。历史修复 `5bc79f2cc42eccaeb17caad34cea9c23f989c116` 的 198 项回执不是本轮重跑数。

### 完整真实跨步主样本：session_1789563666

1. **计划声明**：prepare 的已批准 `generate_binding_input_contract` 宣告产生 `genome_version:string` 和 `snpeff_db:artifact_ref`，后者精确对应 `plan-artifact-320098ac0415075787a63b28` mutable workspace；build/annotate 宣告消费同名值。`binding_value_roles.genome_version=concrete_path_segment` 是显式接口限制，不从字符串推导科学角色。
2. **模型选择与输入**：真实 `generate-0013-250adceda996/request.json` 的 current_focus 提供 workspace、binding_input_requirements 和源映射；0013 response 选择字符串 `arabidopsis_thaliana_custom` 与上述 handle，省略 `binding_env`。前一候选 0012 漏 operation，0013 只修该字段，未执行两次 prepare。字符串是模型选定的本地数据库键，不应将它的措辞解释成程序已验证物种或公共库 ID。
3. **候选接纳/冻结**：执行 action `action_eab899e01603ccd6` 的 `binding_normalization_receipt.raw_public_candidate` 保存省略映射的原始候选。`normalized_candidate` 补 `binding_env` 两个 source 对象，并把 handle 展开为 `canonical_resource_id/resource_kind/binding_revision/plan_handle/binding_id`。`generate_binding_resolution.environment` 得到同一字符串及 `/.../session_1789563666/plan_artifacts/snpeff`。此时 records 的 ID 仍为 `candidate:genome_version` / `candidate:snpeff_db`，并非 accepted。
4. **执行与观测**：prepare attempt `attempt_4cef80a49be134ac1d73` exit 0，stdout 实际打印该键名和 config 路径。目录 postconditions 检查 `data/genomes/{binding:genome_version}.fa` 与 `data/{binding:genome_version}/genes.gtf`；这来自机器声明的有限模板展开，不扫描任意脚本推理角色。目录存在/非空不等于全科学正确性。
5. **验证/Commit**：`commit-step-a8a06af36d368a7003a217aa` 完成 prepare，accepted refs 为 `plan-binding-ab0f72e804c427dac7da`（标量）和 `plan-binding-eb4155b7fe838749427a`（资源），并推进 build。二者保存在 task_state 历史与 active name→ID 索引。
6. **下游输入与消费**：真实 `generate-0016-19e9e7fc7501/request.json` 包含 `accepted_plan_bindings` 的 name/value/type/producer_step；response 显式填两个 `plan_binding:` refs，省略 `binding_env`。build action `action_570db5f4c98c1f91` 的 resolution records 指向上述两个 accepted ID，路径不被“像路径”推断，而由同 revision 的 formal workspace row 取出。
7. **实际下游执行结果**：build attempt `attempt_8fd52285d9aa34ae018f` exit 0，stdout 报告 `PREDICTOR_SIZE=4049438`, `READY=true`，`snpeff_database_ready` observation 携带原样数据库名；Commit `commit-step-52a2fd9df7aee1d1aba032b4` 推进 annotate。annotate attempt `attempt_bdf57580aacdb0985aea` 仍消费同两个 ID，exit 0，并在历史记录中完成提交。整项任务的 summarize/final 尚未证明全部完成，本轮没有重新检验科学产物内容。
8. **批准与实际字节**：prepare/build/annotate 的 approved 与 executed 文件分别按其 script ref SHA-256 校验，6/6 一致；executed 是 wrapper，不能要求 wrapper SHA 等于批准正文 SHA。两者的 parent_script_ref 对应原批准体，未运行它们。

当前进程环境传递在 `bio_agents.py:47988–48184 execute_logic_impl`：再次精确解析，比较接纳时 resolution digest，拒绝 action.env 的同名不同值，再经 `build_exact_action_environment` 传给 runner。`actual_environment.injected_bindings={}` 是 runtime 软件注册投影，不能据此否定 Plan binding 环境传递。主样本 build 代码还把相同值写为局部常量，所以不能仅凭此样本证明它“只依赖注入环境”；下述恢复样本提供真实环境变量消费。

### 真实恢复补证：session_1789537621

`cross_samples.json.recovery_slice` 保留两个真实 build 的完整 approved command 和 binding 字段。两个动作均消费同一 accepted 字符串 `taskref_part2_remap_snp` / 同一 workspace；第一个命令 `GENOME_VERSION="$GENOME_VERSION"`，恢复命令直接使用注入的 `$GENOME_VERSION` 构造路径。两次 stdout 的 predictor 路径均含该键，证明真实下游取用了该值。

第一次 attempt `attempt_d5c176241694077b5355` exit 0，但 observation 的 `snpeff_database_ready.value=false`、`fatal_in_build_log=true`。第二次 `attempt_1c9e88cd2f4b45ed59bc` 通过 recovery envelope `recovery-b25ad3d10015303fda8361b6-01` / `PATCH_SCRIPT`，仍读取同两个 accepted IDs，观测转 true；未重产 prepare binding。这里的“第一次 exit 0”并不等于已通过科学验证。这是历史真实恢复证据，不是本轮重新执行，也不自动证明所有恢复路径都无旧快照。

## 字段生产—消费者矩阵（Q1）

表中源码前缀 `/public/home/liusiqi/03.毕设/Biomind_v1/`；持久化“action”包含 pending_action、归档 action_contract 与审批所绑定的候选。“模型可见”以当前 Generate 投影及历史 request 区分。

| 字段/样本 | 首产/决定者/确认时点 | 程序派生与持久化 | 真实读取者/入模/执行 | 合法空、失效与建议 |
|---|---|---|---|---|
| `produced_bindings.genome_version={type:string,value:arabidopsis_thaliana_custom}` | 名称、类型、角色由批准 Skill/capability；值由 Generate 选择；候选接纳不等于完成 | `_candidate_map:754` 校型；`accept_action_binding_candidates:803` 在 schema2 Commit 后复制候选；action→history | resolver 和 postcondition 模板、Commit；模型填写；派生为 GENOME_VERSION | 无该 operation sub-contract 时可空；scope/revision/rollback失效；保留真实业务值，把已声明 type 改为可派生 |
| `produced_bindings.snpeff_db.value.handle` | Planner 选择工作区；Generate 只能选当前 allowed handle | `normalize_public_action_bindings:1760` 精确查 operation+resource+kind，派生5个formal字段；action→history | workspace attester/resolver；模型只填handle；执行取canonical path | 无产值操作可空；未知/跨operation不接纳；revision变化拒绝；保留选择（仅当有多个合法选项） |
| `consumed_bindings.<name>.ref` | 批准operation已经固定全部消费名；模型重复写 `plan_binding:<name>` | 验证精确集合与ref，尚未自动补省略；action | `validate_declared_binding_operation:1255`、`resolve_action_binding_environment:920`；模型填；resolver执行查值 | 无消费可省略；有消费当前必填；建议公共协议可省略，由批准rule派生；显式错误仍拒绝 |
| `binding_env` | 机器契约声明 source=`produced_binding/accepted_binding`+name；不是值，也不是conda | public v2 normalizer按frozen rule派生；legacy v1不派生；action/normalization receipt | required-env validator→resolver→action env；模型上下文读到兼容说明，默认不填写 | v2合法省略、空对象或正确子集可补全；显式错误不得静默修；legacy完整表仍严格；从默认输出schema/例子中移除，仅兼容读取 |
| `generate_binding_resolution.environment` | resolver按已确认声明精确取值；candidate还只是预执行选择 | 值转string/JSON；artifact_ref走formal row；写action | 当轮碰撞检查和Execute重新解析；非Conda；最终进进程 | 普通只读无binding可`{}`；重复完整快照可缩为digest+必要错误引用（须升级receipt） |
| `generate_binding_resolution.records` / digest | 系统记录env_name/name/binding_id/type/value_hash | 当前Generate持久化完整resolution及其digest | Execute只读digest比较新resolution；runtime内部使用新records；通用审计读取完整action | 保留失效比较；旧完整resolution可由候选+权威状态重建，删除须替代诊断来源；不是直接删字段即可 |
| `formal_workspace_bindings` 的 path/revision/kind/operation_scope/contract_revision | Plan正式allocation+批准capability+evidence contract | `project_current_action_workspace_bindings:342`；current action、approval snapshot | Generate显示、normalizer、workspace admission、prelaunch重建authority比较；artifact path执行 | 无workspace可空；旧path/revision拒绝；用途与限制入模，内部身份不必全量默认展示 |
| workspace `binding_digest` | row规范JSON hash，`workspace_attestation.py:454` | 系统派生在workspace row与receipt.catalog_digests | normalizer receipt、prelaunch整receipt重算比较（非磁盘大文件hash） | 内部完整性有消费者，不能仅grep少就宣称死字段；可与versioned authority digest合并，但保留资源变化结论 |
| `binding_normalization_receipt` | 系统派生，模型不能授予自己有效receipt | 含raw/normalized/public三份候选及scope、contract/catalog/raw/normalized digest；action | prelaunch `bio_agents.py:47474` 重归一化后 `held != expected`；通用审计归档 | 无frozen旧动作可无；建议v3 receipt保留一个原始引用+必要binding输入+digest，删除完整normalized copy；v1/v2旧记录继续原逻辑 |

### PlanBinding 全字段（`agent_core/plan_bindings.py:716 PlanBinding`）

全部由 `accept_action_binding_candidates` 首次生成，在 `task_state.plan_binding_history` 持久化；`accepted_plan_bindings[name]=binding_id` 是当前索引。`accepted_plan_binding:897` 及投影是真正读取通道。业务值来自候选，以下身份不是模型填写。`project_accepted_plan_bindings:1245` 保留内部对象，但 `node_context.py:2151` 的 Generate 视图只给 name/value/type/producer_step/复用指令。

| 字段 | 生产/确认/持久化 | 消费、执行、入模 | 空/修改/建议 |
|---|---|---|---|
| `binding_id` | 系统hash(name/value_hash/type/plan tuple/step/action/attempt/revision)；接受时 | active索引、Commit accepted refs、resolution records；默认Generate不展示 | 正式接受不可空；新revision新ID；保留内部身份 |
| `name` | 批准rule固定、模型按名填 | active索引、精确引用、模板；Generate读写 | 无binding才无；名称不能随意别名猜测；保留 |
| `value` | 模型已选择候选，经执行+验证+Commit复制；并不从stdout自动测量 | 下游精确复用/真实env/显式模板；Generate可见 | 按类型允许的值；immutability保护同revision；保留 |
| `value_type` | 当前模型写但rule已固定、candidate_map验证 | resolver/type check、string vs artifact分支；Generate可见 | 不可随意缺失内部类型；公共填写可派生 |
| `producer_plan_id` | state plan_id | accepted_plan_binding scope检查；绑定ID组成 | scope不同返回不可用；保留 |
| `producer_plan_version` | state plan_version | 同上 | scope不同不可用；保留 |
| `producer_plan_revision` | state plan_revision | scope检查、不可变性判定、身份 | revision变化不可用；保留 |
| `producer_step_id` | execution_record.step_id优先，state fallback | rollback失效、model producer_step说明、归属审计 | 正式路径应非空；修复后对全部受影响step清理；保留 |
| `producer_action_id` | execution_record/action id | identity与归属审计（resolver不据其选值） | 正式Commit前已有action；可只内部保存，不能认定完全无消费者 |
| `producer_execution_attempt_id` | execution_record attempt/execution id | identity与归属审计、Commit证据串联 | 正式路径应有；保留内部，不默认入模 |
| `status` | 初次accepted；invalidate写stale | accepted_plan_binding拒非accepted、重产revision判定 | stale不再读；保留，补后代失效 |
| `revision` | 同名历史max+1 | binding_id与重产历史 | 初次1；显式失效后可2；保留（不是artifact binding_revision） |
| `value_hash` | 规范JSON值SHA-256 | from_mapping重新hash校验、immutability、resolution provenance | 不hash FASTQ/FASTA全文件；保留现有完整性直至合并receipt证明等价 |
| `accepted_at` | 系统UTC时钟 | 通用序列化/审计展示；没有赋予执行权限的专用读取 | 不参与scope选择或execution；可迁入一次Commit事件，不再每个binding复制；历史reader容忍缺省 |

`accepted_at` 不是“完全无人读取”：`to_dict/asdict`、history/projection/审计消费者会传递它。建议删除重复存储是基于已有 Commit 时间/事件与无授权语义，不是一次字符串搜索。`normalized_candidate` 也被 `held != expected` 间接读取，必须连生产者和该reader一起迁移。

## 声明、事实、验证与失效（Q5）

数据库键可以在执行前合法选择；它不需要等科学软件测量。当前 binder 接纳的是候选的原值，`accept_action_binding_candidates` 不从 stdout 取值、不自动证明这个字符串与某项观测相同。schema2 的实际权威边界是 `agent_core/workflow/commit_step.py:547 CommitStep.run` 在重新确认 scope/attempt/validation/completion gate 后接纳并 CAS 持久化；旧 schema（`bio_agents.py:50706`）才在 execution_finished 阶段接纳，不能用旧路径解释当前 schema2。

运行后才能得出的指标/计数应来自既有 typed observation/明确parser；不能把尚未运行时模型填的“实测值”宣传为已观测。当前这条 produced_bindings API 没有通用 stdout→scalar 更新入口。本轮不建议从自由脚本猜名称/参数/文件角色来补这条链。

资源引用在执行前可以指向获批的未来目录；normalizer验证的是身份，不要求 prepare 尚未运行就已有最终 predictor。实际完成由 workspace postconditions、输出验证和Commit判定。`artifact_ref` 解析精确查询同canonical identity/kind/revision，不根据 `'/tmp/foo'` 像路径便赋予文件角色；普通 string 含 `/tmp/.../$UNEXPANDED` 在默认 literal 角色原样保留。只有批准rule显式 `concrete_path_segment` 才拒 `$NAME/${NAME}`、斜线和点段。

只读 input检查通过 observation表达，两个真实 session 的早期只读步骤可无文件输出且空binding。无需跨步使用的普通脚本参数不需要 PlanBinding。绑定必要性的标准是有真实后续精确消费、明确授权范围或状态验收依赖，不能为满足输出列表制造无用途文件。

### 已复现的后代失效缺口

`agent_core/plan_tracker.py:3147 _apply_upstream_step_rollback` 计算 affected descendants，3190 调 `_invalidate_provenance` 失效 target+后代 artifacts，但3197只对 target调 `invalidate_producer_bindings`。`accepted_plan_binding:897` 只检查 binding.status和plan tuple，没有同步检查 producer step状态。

本轮在内存构造 a→b→c，a/b曾产生 accepted string，c失败回滚a。直接导入并调用真实 `_apply_upstream_step_rollback`，未替换其函数体或手动模拟失效。它使 b.status=`invalidated`、c.status=`waiting_upstream`、current_step_id=`a`；a binding不可用，但 `accepted_plan_binding(state,'b')` 仍读出`b-old`；b用同plan revision重新产生`b-new`又抛 `PLAN_BINDING_IMMUTABLE`。此精确缺口成立；本轮未证明它在所选历史 snpEff 样本出现（该样本 binding都来自同一个prepare，恰好避开后代producer情况）。cursor已回到a且c在等待，不能声称c绕过正常图闸门立即执行了旧值。异常可读入口是accepted_plan_binding/resolver，后续b重产被旧值拒绝的风险与此分开报告。

最小修改：在现有rollback原子状态候选内，对 `{target_id,*affected}` 每个producer统一失效bindings；不能只加模型“忘记旧值”的提示。保留 unaffected分支；全部更新随现有CAS保存。回归：a/b/c变更后a/b refs均移除、无关d保留；b重产得revision2；后续消费只能取新值；重复rollback幂等；审批身份变化仍经现有失效机制。不要为此建立第二套binder。

## 检查处置矩阵（Q6/Q7）

| 检查/位置 | 保护对象/依赖 | 正常与异常行为案例 | 结论 |
|---|---|---|---|
| `_candidate_map:754` 类型/形状 | 明确结构化值，防`artifact_ref`被path string冒充 | literal路径string原样；artifact_ref string拒 | 保留结构校验；公共type省略时只能按已批准确定类型补 |
| `validate_declared_binding_operation:1255` 消费/生产精确集合与ref | 当前operation的数据依赖，防跨operation错用来源 | build两refs同名通过；错ref/多未声明名拒 | 保留内部完整集合验证；删除公共强制重复抄consumed refs，可省略派生 |
| public normalizer显式source map校验1820 + public_shape1877 + full_required_env1166 | 原始输入具体反馈与归一化后完整性 | v2省略/正确子集合法；值误填得到actual/expected/detail | 合并raw边界两处的重复规则为一个反馈结果；归一化派生后v2不再把省略当缺失；legacy仍保留严格required-env检查 |
| `normalize_public_action_bindings:1809` exact handle lookup | 选择属于本任务/operation的资源 | 当前handle通过；旧handle、跨operation、歧义拒 | 保留硬条件，缺失/表达错误在Generate局部修正，不运行科学程序 |
| `resolve_action_binding_environment:937` accepted lookup | 只消费已提交的当前scope值 | prepare完成前不可读，Commit后可读；Plan revision变化不可读 | 保留，补全部后代失效；未知名不得当新科学执行失败 |
| artifact ref `_artifact_ref_path:604` | current workspace identity/revision/path | 同revision精确路径通过；revision变化拒 | 保留；不可用“同basename”或自写path替代 |
| prelaunch formal authority比较47437 + normalization receipt47474 | 批准候选与当前权威未漂移 | 原候选重建相同通过；allocation/operation变更拒 | 比较可统一authority摘要；不能删所有prelaunch，也不能静默换已批准path |
| resolution digest48004 与env碰撞48019 | 批准前冻结值与实际启动值相同 | `env.GENOME_VERSION`相同可合并；不同拒 | 保留单一有效值比较；重复完整resolution快照可删/外置，digest不能与workspace digest混淆 |
| Commit验证547 + immutability803 | 值不会在失败或重试中冒充已确认、不会静默改已接受值 | 非零/验证不通过不晋升；成功Commit后同值幂等，不同值拒；失效后新revision合法 | 保留；修rollback失效域，不通过去掉immutability掩盖陈旧值 |
| candidate progress和syntax check | 对最终候选准确记检查状态 | code+argv→合法argv-only两者syntax PASS；未知检查不得凭空PASS或无进展停机 | 当前历史问题已修；本轮无需重复改实现 |

来源映射真实错误包在 `cross_witness_results.json.results.wrong_source_map.actual_error`，有 `BINDING_ENV_CONTRACT_MISMATCH`、`field_path=binding_env`、actual、完整expected及“Delete optional binding_env...”修复说明。未知handle包有binding_name、field_path、handle，但当前函数自身不带allowed_handles；Generate反馈另带requirements作为可操作上下文。单独拿该异常字符串给模型不够，应保留其requirements引用。

`bio_agents.py:44356 _generate_impl` normalizer与 `44613` resolver失败走现有 `_generate_candidate_rejected_update` / generated_action repair，`execution_status=not_started`。`execute_logic_impl:48109` 在已批准prelaunch发现正式必需binding不可用时，明确归为 DEPENDENCY，要求重验上游；未声明错误引用归为GENERATED_ACTION重新Generate；hash破坏归内部错误。实际科学执行失败走既有Recovery，不因为一个可省略映射强行新增Recovery回合。

## 精简草案、量化与具体切片

`cross_draft.json` 对同一个真实prepare/build候选保留完整执行代码及业务值，输出草案只省略机器已确定的两个type（producer）或两个精确consumed ref（consumer）。operation继续保留；protocol marker/空produced map没有借机计算“新收益”；真实候选已经省略binding_env，故该项新收益严格为0。

| 相同样本、同一compact JSON口径 | 现状 | 草案 | 减少 |
|---|---:|---:|---:|
| prepare输入binding投影UTF-8 | 3,066 B | 1,347 B | 1,719 B |
| build输入binding投影UTF-8 | 3,387 B | 1,344 B | 2,043 B |
| prepare输出binding片段 | 192 B | 154 B | 38 B，2个机械leaf值 |
| build输出binding片段 | 146 B | 24 B | 122 B，2个机械leaf值 |

以上是仅binding投影，不是全部模型上下文减半承诺；无tokenizer统计。真实prepare/build请求message.content为128,090/146,452 UTF-8字节；输出协议在seed与独立message重复是另外一层量，不能与以上相加而不去重。

1. **优先修事实失效域**：`plan_tracker._apply_upstream_step_rollback` 与 `plan_bindings.invalidate_producer_bindings`，范围如上。必须改运行代码，提示词无效。
2. **删除模型机械输入要求**：`public_binding_requirements`、`normalize_public_action_bindings`、`public_binding_shape_violations`、`binding_input_progress_facts`，以及 `prompts/manager.py:3000`、当前Generate输出协议生成处同步更新。按版本新增公共省略兼容，缺类型只在rule唯一确定时补，consumed refs仅按已批准operation精确列表派生；显式错值/错ref照常反馈；legacy_full_v1严格保留；产出的内部formal action维持现有校验和批准机制。不做任意代码语义推断；只改提示词会被现有validator拒绝。
3. **真正删除重复payload/validator**：`normalize_public_action_bindings:1828` receipt中的完整`normalized_candidate`可由raw+frozen+catalog唯一重建；`bio_agents.py:47474`整receipt equality是它当前的间接消费者，必须一起改为versioned projection/digest比对，并保留旧v1/v2读取。源码搜索+追调用链未发现其他执行决策直接读取此快照。主样本prepare/build/annotate单独这份重复JSON约4,997/9,231/5,435 B（默认`json.dumps`格式，故不能混算compact表）。`generate_binding_resolution`完整副本的Execute消费者实际只读旁边digest，再独立重算；可保留生成时错误证据引用并删除重复full value副本。当前不能直接删字段，因为审批完整action和历史审计也包含它，需提升receipt/schema版本和测试旧审批恢复。
4. **合并env原始校验而保留必要硬条件**：当前public shape、normalizer对显式映射重复校验，full validator在normalizer后再核验必需映射。输出一次结构化assessment供local feedback，内部full action验证继续作为可信边界；对v2派生字段不再制造新的“模型缺失”义务。legacy按旧完整协议走原检查。正常“省略/正确显式”与异常“accepted/produced source颠倒”要共同见证，不能只藏字段不删重复拒绝路径。
5. **缩小默认模型投影**：只显示当前handle/path/用途/可用性、已接受value、具体调用环境变量以及postconditions；原始identity/revision/digest仍可由证据引用查看。`accepted_at`可归并Commit事件而非每个值副本；这个低优先级节省需要history reader的缺省兼容，不值得先迁移历史全库。不存在证据支持本轮直接删除所有hash/inode或一个总binding层。

校验成本：本路径 value_hash 对小的JSON候选值、receipt对候选JSON做hash；未见此处对大型FASTA/FASTQ整文件hash。normalizer在Generate接纳与prelaunch各运行，workspace row也重投影，resolution在Generate/prelaunch各算一次；本轮没有运行时间benchmark，不宣称性能收益。正式输入文件与script身份的成本由其他分报告核验。

## 验证边界与未验证

当前纯函数见证17项均满足各自断言，其中2项是缺陷复现（后代陈旧值可读/重产被拒），不能概称产品17项安全通过。17项中的acceptance使用合成returncode/validation调用真实函数，未模拟完整CommitStep；真实CommitStep贯通来自历史运行证据。编译历史候选是非执行语法见证，不是科学运行。真实helper、Picard环境由其他分报告负责。

首次误用系统Python3.9试图导入RecoveryPolicy时因现有`X | None`注解停止，未载入配置；后改用已存在`/public/home/liusiqi/anaconda3/envs/biomind/bin/python` (3.12)，缩小import边界到纯生产函数。提取器开发时两次缺导入/空字典KeyError已修正并重跑；这些属于审计脚本调试，不作产品缺陷。本轮无软件安装、API/模型调用、科学执行、生产文件编辑或原session写入。

未验证：完整当前graph在多producer rollback后的恢复（下轮最小隔离图a→b→c即可）；当前全部historical reader对拟议receipt v3的兼容；主样本历史进程完整源码版本；对所有自由脚本科学实义的证明（不应由binder承担）。以上不妨碍当前已证明的精确省略规则、提交边界、历史跨步消费及后代失效缺陷。

## 并发最终版本复核

最终保护检查发现其他工作并发修改了生产工作区。以下是只读定向复核及重新运行本分报告17项纯路径见证，不是本审计实施这些生产修改。原 [cross_witness_results.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_witness_results.json) 保留不变，SHA-256 为 `b728eff6aa5138c7738cd369332e6c30fe33281252b441e66adf6bcf9436363f`；新增结果为 [cross_witness_final_checkpoint.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/binding_minimality_20260917/evidence/cross_witness_final_checkpoint.json)。通过 `runpy` 执行未改动的原见证脚本，仅临时拦截该结果文件的 `Path.write_text`，改写至新文件；生产函数未替换。

| 最终检查文件 | SHA-256 | 对本分报告的影响 |
|---|---|---|
| `agent_core/plan_bindings.py` | `3d87cdba694d46416eca46995655c338c0ac13394e9fc3c667cc437db1cea20f` | 与初次见证相同；接纳803、读取899、解析920、失效1218均保留 |
| `agent_core/plan_tracker.py` | `724f098ea3b41ed6e78ff629a2f014a8e5121fbb19cd2ab631cc03da8d2628c2` | allocation promotion函数11489/11575改变；rollback3147及仅清target的3197未改 |
| `agent_core/semantic_completion.py` | `8dc3af63d3dc0f60a79c864be5fc32487fadbbb47e28770882cbee313a458cc7` | `evaluate_workspace_mutation_receipts:907`增强Controller rejected receipt诊断及plan_revision_required；未改PlanBinding值的复制/读取/失效 |
| `agent_core/step_effects.py` | `565d06037d3f68b7818f3fced964066109c3836be3e2420abf5bf6445ee3cfa2` | 本文件虽相对HEAD有Plan-owned workspace evidence检查，hash与初次纯见证相同；不新增脚本语义猜测 |
| `agent_core/workspace_attestation.py` | `cca7537f7641f3388970395484618b0b4e1922af35655f90f2788afebf2d3f67` | 新读取批准step mutation/evidence contract并验证其scope；`_artifact_ref_path`当前720，resolver包装774；引用精确解析结论不变；新增reuse内容证据见下 |
| `agent_core/workspace_revalidation.py` | `b020a6556583aa8c5b105ed93b6a3f17dca17652f826c1567422127f7e81ff3e` | 新文件，明确授权workspace reuse的Controller全文内容hash；不是本审计添加或执行 |
| `bio_agents.py` | `c4d81118032f0132a8269cb5344cc1650986d148a8e80bd911906bb304d35883` | workspace admission增加approved_step；49941接Controller revalidation；plan resolution现47994，实际提交仍由CommitStep547 |

上述7份源码在最终见证开始/结束的hash完全一致。17项断言仍满足，其中 `rollback_descendant_binding_defect` 与 `rollback_descendant_new_value_blocked` 仍是**现役缺陷复现**，不是修复通过：真实rollback后b步骤invalidated，旧binding仍可读，重产新值仍报IMMUTABLE；current_step仍指a、c仍waiting_upstream，未声称c绕过图闸门执行。未导入config，未读dotenv，未调用模型/API，未运行科学代码或新workspace reuse分支。

**最终版本成本结论需补充，不能沿用前述初始版本的宽泛说法。** 新 `workspace_revalidation.py:165 collect_controller_workspace_revalidation` 在process exit 0、显式allow_idempotent_revalidation、前后bounded snapshot可靠且不变的分支，要求获批 `exact_file_sha256_v1` 契约涵盖各file postcondition；`:122 _read_verified_files` 以1MiB块完整读取文件，每次总量上限16GiB。`:224 validate_controller_workspace_revalidation` 在receipt finalization重新调用同函数，再读一遍并比较。故成功reuse分支可能对FASTA等大文件做两遍全文hash；这不是每个Generate回合或所有动作的成本，不能把“无大文件全文hash”推广到最终版本。此处仅静态调用与工作量取证，没有执行新分支或测量时间；其科学等价性和新契约回归属于并发修改方的验证边界。

并发更新改变Plan/workspace mutation证据的确认条件，未改变本报告的三个核心结论：binding_env默认派生，schema2 Commit才成为accepted跨步值，以及多producer rollback失效域缺口。历史请求和真实session证据仍是原时点事实，不能用最终新增reuse规则倒推其当时采用行为。
