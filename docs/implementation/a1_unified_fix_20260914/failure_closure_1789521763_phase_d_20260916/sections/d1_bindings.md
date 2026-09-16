# D1 / C1：具体 binding 准入、冻结与真实回执

## 结论与分类

**当前仍缺少“此字段必须已经是具体标识”的可执行契约。** `produced_bindings.<name>.value` 声明为 `string` 时，现有完整准入只验证字符串类型；`${GENOME_VERSION}` 因而作为普通字符串进入候选、注入环境以及冻结的 workspace postcondition。脚本另行计算并覆盖同名 shell 变量，写到了不同路径。attester 按原冻结路径拒绝成功，行为正确。

分类：**旧修复有效但未覆盖的新输入变体 + 真实 Generate 偏离已发布使用说明**。没有证据证明旧 warning/revalidate 修复发生回归；也不能把 `chr1` 的科学含义、模型 Recovery 推测或旧会话结论作为本次首因。本节读的是当前工作树，disk HEAD `118b28e5f320e5d150a5db3eca163a2417218f08` 不等于历史进程加载字节；本轮加载与保护清单由根账本登记。

## 历史因果锚点（只读复用）

证据根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789521763`。

| 对象 | 直接事实 |
|---|---|
| `context_trace/generate-0015-bf36b77c0ddf/response.json` | 原模型声明 `produced_bindings.genome_version={type:string,value:"${GENOME_VERSION}"}`；另有合法公开 artifact handle。不是审计推断出来的值。 |
| `executions/execution_records.jsonl` 第 3 行 | `prepare_database_layout` / `action_d213abed559f264d` / `attempt_4c906c80435373ae51a0`；exit 0、进程成功、输出 reconciliation accepted；action contract 保留同一字符串，`generate_binding_resolution.environment.GENOME_VERSION` 也保留同一字符串，记录为 `candidate:genome_version`，不是已 Commit 的正式 binding。 |
| `attempts/attempt_4c906c80435373ae51a0/mutation_intent.json` | 冻结要求 `data/genomes/${GENOME_VERSION}.fa` 与 `data/${GENOME_VERSION}/genes.gtf`；after snapshot 记录的是 `data/genomes/chr1.fa`（16,000,014 bytes）与 `data/chr1/genes.gtf`（28,659,281 bytes）。after 可靠且未截断。 |
| 同一 mutation 回执 | `changed_during_attempt=true`，`attested=false`，`attestation_status=postcondition_unsatisfied`；`data` 目录要求通过，两个含字面占位符的文件要求缺失。 |
| 同 attempt stdout | `GENOME_VERSION=chr1`；只证明脚本本轮运行时使用的名字，不证明科学上选择合理，也不能反向改写已批准声明。 |

因此首个确定冲突是**声明/冻结路径与实际写入路径不一致**。0 退出码或 reconciliation accepted 不覆盖 workspace attestation，也不足以接受该步骤或该 binding。D2 继续追踪完整失败事实如何进入 Recovery；此处不重复其消息投影审计。

## 当前相关入口与字段语义

以下是该数据流所有相关入口；范围为当前 Generate→Execute→Commit 的 binding 及直接 runtime/workspace 消费者，不是全仓所有字符串/文件路径扫描。行号以本轮保护源码为准。

| 字段/入口 | 权威、实际语义与边界 | 当前位置 |
|---|---|---|
| 模型 native action（code / argv / 双载体） | 正式 parser 提取同一个 action；binding 与可执行文本分开。三类载体随后经过同一正常化和准入。 | `bio_agents.py:21752`, `:22129`, `:44262`, `:44274` |
| `produced_bindings.<name>.{type,value}` | `_candidate_map` 验名称、类型、shape；string 仅 `isinstance(str)`，没有 concrete/template/expression 角色区分；integer/number/bool/json/artifact_ref 有各自 shape。 | `agent_core/plan_bindings.py:730` |
| 适用 operation 的 `binding_types` | 检查类型与声明名字；artifact_ref 进一步查 resource identity/kind/revision；string 的内容仍原样保留。 | `agent_core/plan_bindings.py:1017`, `:1208`, `:1285`, `:1651` |
| `artifact_ref.value.handle`（嵌套具体引用） | 公开 `public_handle_v2` 只允许当前 operation、resource、kind 唯一精确匹配；不展开 `${HANDLE}`。完整旧对象协议保持严格检查，不猜配、不迁移。 | `agent_core/plan_bindings.py:1712` |
| `consumed_bindings.<name>.ref` | 必须恰为 `plan_binding:<name>`；只取当前 plan/revision 已接受 binding；不是字符串插值器。 | `agent_core/plan_bindings.py:875`, `:896` |
| `binding_env.<ENV>.{source,name}` | source 只能为已声明 produced/accepted binding；公开协议可省略，由冻结规则派生；不是填写实际 env 值的位置。 | `agent_core/plan_bindings.py:1119`, `:1712` |
| `resolve_action_binding_environment` | scalar 原值转为 env 字符串，json 做 JSON 序列化，artifact_ref 解析当前正式路径；记录 value hash。无 expandvars/eval、无 shell 赋值扫描。 | `agent_core/plan_bindings.py:896` |
| action `env` | 与已解析 binding env 同名不同值会拒绝；代码本体仍可重新赋值，单靠此映射不能保证任意脚本守约。 | `bio_agents.py:47926` |
| `formal_workspace_bindings` / artifact 路径 | 来自当前正式目录/绑定修订，不由模型输入作为权威；Generate 与 Execute 重算后比对。 | `bio_agents.py:47346`；`agent_core/workspace_attestation.py:655` |
| frozen postcondition `relative_path` | 只实现 `{binding:NAME}` 模板；替换值要求非空字符串且不含路径分隔符；单次替换后作路径 containment 检查。嵌入值 `${NAME}` 被保留为字面片段，不再解析。 | `agent_core/workspace_attestation.py:38`, `:666`, `:689`, `:716` |
| accepted binding / frozen intent | 正式接纳取声明值，不从 stdout 推断；schema2 通过真实 Commit 才提升。intent 在进程前固定 action digest/plan/step/path；回执核对其 scope/hash。 | `agent_core/plan_bindings.py:779`；`agent_core/workflow/commit_step.py:543`；`agent_core/workspace_attestation.py:716`, `:1322` |
| runtime 能力事实 | ready registry + capability manifest 精确解析已有运行时路径；预启动核验绝对路径/种类/权限。是 runtime evidence 的机制，不是任意输出语义值的 runtime-derived binding 导出器。 | `agent_core/runtime_evidence.py:138`, `:222`；`agent_core/action_runtime.py:473`, `:613` |
| workspace after facts | runtime attester 观察冻结目标，并按真实改变/可信访问证据区分 mutated/revalidated；不把进程打印的路径当权威。 | `agent_core/workspace_attestation.py:962`, `:1182` |

### 不应混成一种“占位符”

1. **具体 scalar 标识/路径片段**：本次 genome_version 实际用于文件布局，应在执行前明确。当前 `string` schema 未编码此语义，这是 C1 的缺口。
2. **显式引用**：artifact handle 和 `plan_binding:name` 已有精确解析。坏 handle 已能触发局部 Generate 纠正。
3. **框架支持的模板**：已冻结 evidence contract 中的 `{binding:name}` 是合法模板。`derived_artifact_layout` 的 `<data_root>` 等只是展示说明，不是可执行通用模板语言。
4. **脚本与普通文本**：`$LOCAL`、`${LOCAL:-fallback}`、字面花括号和带 `$` 的普通文本可以合法存在。不得全局扫描 code/argv/JSON 并一律拒绝。
5. **运行时观察出的语义值**：当前没有把 shell 局部变量或任意 stdout 自动提升为 scalar PlanBinding 的受支持入口。已有 runtime 路径 evidence 不代表该功能存在。若某值执行前确实未知，应由先行受支持观察产生可引用事实、再由新 Generate 明确声明；不能在原动作执行后偷偷改写声明、旧 approval 或冻结 postcondition。

当前 literal 字符串 `${LITERAL_TEXT}` 也能是合法文件名。仅从字符无法判定模型想表达变量还是字面文本。此歧义必须由**字段角色**解决；无角色声明时不能宣称所有类似字符串都是非法。opaque `json` 的嵌套 note/path-like key 目前没有具体路径契约，不能自行按 key 名猜类型。

## schema、示例、载体与旧修复边界

`skills/genomics/snpeff-annotation/SKILL.md:112` 将 genome_version 声明为 string；`:308` 要求选择稳定显式值，`:309` 要求脚本消费系统注入 env，`:366` 禁止靠 shell 局部变量作跨步载体，`:374` 区分真实 runtime 路径。说明已提示正确用法，但未替代可执行准入。

`public_binding_requirements`（`agent_core/plan_bindings.py:1691`）对 scalar 只发布 `model semantic value`；`binding_input_progress_facts`（`:1794`）把非空 string 算作输入字段就绪，`public_binding_shape_violations`（`:1829`）不检查具体字符串内容。C1 不能只改说明，也应同源更新准入及反馈/进度展示，避免“字段已好”与新 validator 相互矛盾。

`generate_output_examples`（`agent_core/step_effects.py:3258`）遇到未知 required scalar 会给完整 blocked example，不擅自编造值。应保留。`materialize_effect_action`（`:3582`）审批前选择实际解释器载体并保持 code/argv 同源，冲突拒绝；C1 应接在共用结构化 action 边界，不分别增加三套脚本解析器。无结构化 binding 的旧 script-only 形式不能靠扫描 shell 变量生成 binding。

前轮 missing-observation warning 修复只允许相应软缺项进入语义审查；`CompletionGate._observation_reviewable`（`agent_core/completion_gate.py:2515`）仍排除 invalid/stale/effects 等硬错误。`revalidation_fingerprint` / `revalidation_stop_reason`（`agent_core/workflow/recovery_runtime.py:60`, `:74`）只约束同一失败核验的重复消费。它们都不改 scalar 声明或冻结布局。对于已经冻错的 `${GENOME_VERSION}` 路径，实际文件在 `chr1` 下面，反复 revalidate 应继续失败；放宽该检查会错误地接纳不同对象。

原 F1–F6 限定结论原样保留：F1 公开协议；F2 审批前真实 code/argv/helper 同源；F3 原记录所对应进度机制（后续公开协议修复对政策的替代仍按旧文保留）；F4 输入异常处理；F5 实际审批/显示；F6 原 22 场真实框架图与完整六步推进。源 `docs/implementation/a1_unified_fix_20260914/README.md:9` 及原 acceptance matrix。F6 固定 provider 会预先给出 `new_fixture_v2` 且脚本直接消费 env，未覆盖“string 内留下 shell 表达式，再在脚本另选值”。其 Plan/approval/attester/Commit 真实、provider 与科学叶子替身的边界不变；不能因此撤销其机制结论，也不能升级成所有模型变体或科学 E2E 成功。

## C1 最小修改设计（阶段 D 未实施）

1. **明确字段角色**。优先从已冻结 workspace evidence 中 `{binding:name}` 的用法识别受影响的 concrete path-segment binding；若现有 string 还需同时支持任意字面文本，最小增加该 operation binding rule 的角色声明（例如 `binding_value_roles: {genome_version: concrete_identifier}`），同源发布在 schema/requirements/example。不是增加全局字符串类型系统；opaque json/code 保持原义。明确 concrete 字段拒绝未解析 shell/框架表达式的有限语法，以及确实需要字面 token 时的显式政策。其余标点、中文、空格等仍由该字段既有路径约束决定。
2. **共享准入**。在 `_validate_binding_value_contract` 及 workspace 模板消费的公共边界调用同一个按字段角色工作的校验器。反馈包含 `field_path=produced_bindings.<name>.value`、原值、所需角色、未解析表达式种类、`REPAIR_REQUIRED`，交给现有 Generate 局部纠正；在 approval/action_ready 前生效。保留 artifact/ref 精确查表、范围/修订校验。禁止 expandvars/eval、宿主 env 推断、shell 赋值扫描、猜 chr1。
3. **只填一次**。成功值仍只写 produced_bindings；系统派生 binding_env。示例显示脚本消费注入 env，并说明不要重新计算覆盖。任意脚本仍可能偏离；维持真实 runtime attestation 和 Commit 硬门，不能宣称静态检查已保证所有 shell 行为。
4. **冻结对象不变通**。Generate 修正值要产生新 candidate fingerprint/normalized receipt/action identity，重新进入真实审批；Execute 重算正常化回执、workspace authority 与 binding-resolution digest，之后才冻结新 intent。不得修改已批准动作、历史 binding 或旧 intent/receipt 以迁就 after snapshot。已有 `BINDING_NORMALIZATION_RECEIPT_DRIFT`（`bio_agents.py:47390`）、`PLAN_BINDING_RESOLUTION_STALE`（`:47921`）、receipt scope/hash 坚持拒绝复用。
5. **只增必要验收**。现状 witness 的 bad scalar→真实 approval 应改为阶段 E 的 bad scalar→同 Generate 局部反馈→具体值→真实审批→执行→attestation→Commit；保留旧 unknown-handle 修复完整图和合法值/载体/过期授权拒绝。不得用预填 approved/accepted、扩大预算或绕过 attestation 让测试通过。

## 本轮见证与尚未证明项

新代码仅在 `offline_witnesses/test_d1_bindings.py`，由根代理复制到 A1 新 `/tmp` allowlisted export，再用 `tests/cli_model_progress_support.py` guard 运行；禁止原工作树裸 import。依赖原 `tests/test_workspace_binding_attestation.py` 与 `tests/a1_unified_fix/test_f6_graph.py` 及其 exporter 自动收集的显式 Python fixture 依赖。完整加载字节/guard/输出/失败回执见根验证账本。

- 组件链经过真实 parser→public normalizer→operation admission→workspace admission→env resolver→intent→before/after attester。三种 scalar 名 × 两种变量语法 × code/argv/both，换成非 snpEff `prepare_bundle`、`objects/<label>.txt`、新 workspace/identity。写入的是新 tiny bytes，不执行组件脚本。该链证明准入缺口及 after 拒绝，不代表真实批准执行。
- 正例保留具体值、中文/空格、普通 `$`/花括号文本、显式字面 token 与合法 `{binding:name}` 模板；嵌套 artifact handle 不展开、consumed ref 精确解析、opaque JSON 不被误作运行时 resolver。
- scope 反例对真实签发的 receipt 改预期 action/attempt/binding revision/plan revision/path，验证旧回执不能作新对象证据；这是 attestation 层覆盖。任意“审批后改 binding”完整图的新增变体尚未单独跑，必须在总表标 PARTIAL，不能由此替代审批 guard 的实际执行证明。
- 两条新图仅替换 F6 的模型叶子：一条在第二步产生未解析 scalar，观察其直接到真实第二次审批 interrupt 并停止；只批准执行前一合法步骤，不运行坏候选。另一条新坏 handle→同 Generate 修复→原六步正式闭环，证明旧局部纠正机制可复用。阶段 D 未实施 scalar 修复，所以 V04 的目标闭环仍待阶段 E。

本轮测试的最终结果为 41 个唯一 case PASS：run03 的 39 个组件，加 run02 的两张真实图精确复用；初始失败、fixture 自身修正与加载字节由根验证回执保留。未发现新的受支持 runtime-derived scalar exporter；当前 literal/expression 角色界限需要 C1 接口小改，不能从既有 schema 虚构该功能。真实模型遵约率、科学上 identifier 的合理性、原进程完整加载修订均不由这些离线见证证明。

### run02 实际结果与新夹具修正

根隔离运行 `/tmp/bm_failure_closure_d_run02/results/pytest-results.json` 已记录：D1 共 41 个 case，34 PASS、7 FAIL。32 个组件通过；两张真实图均通过：坏 handle 局部纠正图 105.324 秒、7 Generate / 6 Reviewer / 0 Recovery / 6 次实际批准、执行和 Commit，最终 `task_verified`；未解析 scalar 图 21.100 秒、2 Generate / 1 Reviewer / 0 Recovery / 1 次实际批准执行和 Commit，随后停在真实第二次 approval interrupt，无该 binding 接纳。图回执分别为 `f6-d1-existing-repair*.json[.gz]` 与 `f6-d1-unresolved-preapproval*.json[.gz]`。这两图未卡死，不应把中间耗时误报为回归。

7 个失败全发生在新合法值组件的 `code == argv[4]` 严格断言：夹具给两字段添加末尾换行，但正式 parser 对 code 去外围空白、对 argv 保持原字节。实际是新夹具构造的字符串不同，不是 scalar 校验失败。只修改新 witness 的 body 构造，使输入从一开始就是同一无外围换行的程序；保留严格 equality 与准入、冻结、attestation 原断言。run02 原失败完整保留；该次运行未完成这 7 个合法值的最终 attestation。随后 run03 的新隔离重验已全部补证，不能用单纯类型准入代替这次实际核验。新的 body 构造影响组件 `_case`，不影响两图的 provider/runner，因此图结果可按精确依赖复用。

### 最终 D1 见证结果

`/tmp/bm_failure_closure_d_run03/results/pytest-results.json` 的 D1 `call` 回执为 **39/39 PASS**（组件实际合计 0.205 秒，不含 guard 启动）。其中包括修正输入后的 7 个合法值：仍保留 code/argv 严格一致性检查，并完成真实冻结、before/after snapshot 与 runtime attestation；没有放松生产检查或成功断言。

另按精确依赖复用 run02 已通过的两张图：**旧坏 handle→同 Generate 局部修复→6 次批准/执行/Commit**，以及**未解析 scalar→真实审批 interrupt**。本次仅调整组件 `_case` 的合成 body 外围换行，未修改图的 provider、runner、断言或其依赖生产字节；保护记录和导出/加载字节归根验证账本。未再次运行两张图，未重复计数。D1 最终为 **41 个唯一 case PASS**。

这些 PASS 分别表示**当前缺陷成功复现、现有保护有效、合法输入保留**；不表示 C1 生产修复已经实施。V04 的“未解析 scalar→拒收→局部修复→批准→执行→核验→Commit”目标仍为阶段 E 待实施；V17 新审批变体的完整图仍按覆盖表保留 PARTIAL。run02 的 7 个夹具失败及修正原因继续保留。
