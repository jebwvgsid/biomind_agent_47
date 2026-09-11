# 模型公开说明与真实工具活动流：实施与离线验收

执行依据：用户明确指定的《BioMind CLI：模型公开说明与真实工具活动流——实施规格 V1》。本轮是实施；没有覆盖或重写先前审计及缺陷 witness。

## 实施状态

CLI-1、CLI-2、CLI-3 均完成并分别提交。最终 exact 工作树和独立候选树各 **148 passed**；146 个白名单导出文件复核无漂移。没有以工作树通过替代独立提交验收。

| 切片 | 完整 commit | 父提交 |
|---|---|---|
| CLI-1 | `fe73e9b0ef1e427e6a6597d21cb0403cf3f84b3b` | `8769e3a4f42938d56853f3dfddf1294ff1397651` |
| CLI-2 | `1818f3e1c8cfe30d29b570aede25663734d7c152` | `fe73e9b0ef1e427e6a6597d21cb0403cf3f84b3b` |
| CLI-3 / final code HEAD | `54b6025bb96e65efc70d8017bb63dbc8ad73bd2d` | `1818f3e1c8cfe30d29b570aede25663734d7c152` |

现在使用已有模型回合返回的公开说明，在后续真实工具活动前显示。Consultant / Planner 的顶层 `public_message`、Generate 的原生 JSON 公开字段和单脚本 envelope、Recovery 的当前回合 `display_text` 均接入生产接收函数。没有旁白模型、进度工具回合或展示 transcript 回灌。

这是完整响应返回后的逐回合公开说明。长请求未完整返回时，系统只能报告等待或实际已接收响应活动；没有单请求内逐 token 公开流，也没有用 reasoning 代替公开文字。

## 基线、相交修改与提交隔离

- 仓库：`/public/home/liusiqi/03.毕设/Biomind_v1`。
- 分支：`refactor/consultant-controller-authority`。
- 本轮基线 / 审计提交：`8769e3a4f42938d56853f3dfddf1294ff1397651`，父提交 `e5d814bd77b44f52aa16fc1fff92982a9aa9d729`。
- 修改前重新取得 HEAD、分支、437 项状态（36 M / 33 D / 368 untracked），暂存区为空。目标安全源码保存修改前快照；没有读取原 session、业务数据库、启动脚本或凭据。
- `prompts/manager.py` 原有 HASH_OUTPUT_POLICY 及相关插入属于既有修改。本轮分别从当时 HEAD 应用本轮公开字段增量，生成独立待提交 blob；同时断言相同增量应用到修改前 exact 文件能逐字重建当前文件。只暂存该独立 blob，保留原 HASH_OUTPUT_POLICY 工作树差异。
- 其余代码和测试只按显式本轮路径暂存。没有 add -A、stash、清理、覆盖旧测试、切分支、amend 或 push。没有改 PlanTracker 的事件白名单、控制合同或 Recovery 的预算与上下文。
- exact 与候选提交白名单导出存在 8 个既有差异：`agent_core/node_context.py`、`planner_context.py`、`provider_message_safety.py`、`retriever.py`、`workflow/managed_stop.py`、`prompts/manager.py`、`tests/test_plan_local_preaction_convergence_v2.py`、`tests/test_recovery_agent_loop.py`。它们没有被夹带提交。

完整提交、父提交、精确路径、源码签名和状态核对见 `git_scope.json`；最终工作区 HEAD 为包含本报告的审计产物提交，完整 hash 在执行回执中返回。

修改前实际快照的 13 个目标安全文件签名为 `3b6227525484dd9ebe2c040ad503baa83e64d7a18ac2f4722dcde8e18ab24f43`，同范围修改后 exact 签名为 `ab3a6aa663c0626c5159a42facb3ee23546caf4f17b0a0d4f6bcba4a3fd166f7`。这是明确定义的目标文件范围，并非整仓库的历史快照；最终完整白名单导出的签名和逐文件 hash 另在验收证据中。

## 三个逻辑切片

**CLI-1 — 当前说明、显示顺序和真实活动。** `emit_public` 只收显式公开的完整字符串；DisplayTicket 记录本地身份、来源、scope 和 ready/enqueued/write/flush。取消、审批、错误、模型段落、工具起止进入同一有序序列。移除内容前缀去重，保留同一 item 去重；不同 provider / execution / tool / stream 的身份分开，迟到 provider 活动不能改写当前状态，旧执行终态仍保留。

初始 plain 使用独立追加 writer；TTY 由实际 Terminal/Application owner 写出；off 消费显示事件而不写正文。人机输入只使用 `interactive_terminal()`，plain 显示 owner 不接管原输入和信号策略。关键段落不等待 0.1 秒批次；首个随后工具前等待最多 50 ms 的真实写出确认。无文字不等待，普通显示失败或超时不触发模型重试；取消仍传播。

Recovery 按本轮 source_message_id 选记录，优先本轮 `display_text`；空值不借上一轮，不公开 raw JSON 或 typed reasoning block。原始 assistant / tool 历史仍保留。必须成功的 durable checkpoint 仍在显示副本之前，原错误传播未被显示 try/except 吞掉。

共享 token callback 不转发协议或 reasoning。provider observer 根据实际 payload 判断“等待”或“已接收”，失败展示真实 `failure_type`。Skill、inspect、glob、KB 和 Executor 从实际调用/进程边界产生事实，空结果只表示返回；`rc=0` 仍待正式核验。CLI-3 收尾回归还修复了 unavailable Skill 显示处错误的局部变量引用，并补齐实际 typed 服务不可用入口；原 SkillAccessError 和无派发行为保持。

**CLI-2 — Consultant / Planner。** 新的 `project_public_json` 只在完整严格 JSON 对象上剥离顶层 `public_message`，不递归删除同名业务字段、不丢未知业务键、不重建业务对象。字符串转义及业务成员原文保留；显示成员周围空白不构成新的批准 authority。Plan 仍按原 canonical 规则产生 identity/hash；不能把外层 raw response 的 hash 与正式 Plan hash 混同。

缺省、空、null、数字、列表等显示值不造成业务纠错。坏整段 JSON 不提取旁白，原 parser / validator 继续拒绝坏业务。原 response 不改写，接收历史只保存原 assistant 一次。Consultant 的正常 V2、discovery escalation 和 Planner 实际 system prompt 已启用规则；typed Skill 提前返回之前也已分离。ANSWER_USER 与同条公开说明完全相同才避免机械重复，不按前缀删段落。

**CLI-3 — Generate。** script 协议识别唯一外层：

```json
{"response_type":"biomind_generate_display_v1","public_message":"候选已形成，接下来先检查，再提交原有审批。","payload":"<shell>\nprintf 'fixture\\n'\n</shell>"}
```

标准 JSON 解码后 payload 不 strip、dedent、换行统一、二次 unescape 或替换正文。先复用原严格 script parser 确认 envelope 只承载脚本，再把相同 payload 交原候选/语法/审批/执行路径。不能将 Skill JSON、blocked、solution 或普通结论塞进 script payload 借用早返回或标签 fallback。唯一字符串 payload 已知但脚本非法时，公开说明可显示，后随真实拒绝；错误候选 identity 保留内层正文，不因可选说明变化改变。缺 payload、类型错误、冲突正文、重复业务载荷不猜选。

原生 action / Skill / blocked JSON 使用同一可选显示字段分离；旧合法单脚本块继续接受。早期 candidate / Recovery 缓存分类也解析业务副本，保存的仍是原 response。Generate 原来没有独立的 finish_reason=length 拒绝门，本轮补上已知截断/过滤/中断的最小检查，走原 representation 修正链。当前 provider attempt 的完成事实和两处缓存的允许字段保留 `finish_reason` / `completion_integrity`，防止已知截断在缓存重建后变成 UNKNOWN。没有修改 claim / CAS / 业务预算、Recovery Seed、history 或授权。

Supervisor 原有模块 print 接入同一 display_print，保持评估结果、来源和 off 行为；Direct response、Supervisor 和既有 final summary 不套用 C/P/G 协议，不新增模型调用。

## 来源与时序证据

逐节点字段、函数、入口、启用状态见 `source_matrix.tsv`。逐 T01–T24 的层级、测试函数与边界见 `acceptance_matrix.tsv`。`verification.json` 保存最终实际测试命令、结果及导出签名；`ordering_and_invariants.json` 保存合成时序和业务账本；`transcripts.txt` 为从该证据抽出的公开 transcript。

`timing_examples.tsv` 单独列出 8 组五时点示例，含当前候选树的 Recovery 正常/超时、C/P 的 plain/TTY，以及 Generate 到真实 run_script_path 边界；完整证据有 44 组 fixture/mode 记录。

时间源是 `time.monotonic()`，单位秒。ready 指当前完整公开字符串被选择并进入 emit_public 的时点；enqueued 是显示模型入队，write/flush 是实际 writer 调用返回，dispatch 是原工具调用边界。正常路径证明 write/flush ≤ dispatch；受控阻塞路径证明 dispatch 可先于 write/flush，随后 transcript 仍保序。没有把 enqueue 当 flush，也没有把 StringIO / prompt_toolkit Output 的 write 当用户屏幕像素可见。

终端测试中有受控事件和 test-only event-loop callback，用于受限环境的跨线程 Future 唤醒与退出；没有伪写票据、伪造时间或生产固定阅读 sleep。50 ms 是确认上限设计，不是实际性能承诺；调度可能消耗确认窗口，显示开关也并非零时延。

## 业务同一性与测试边界

- Recovery 固定 fixture：3 次已有模型调用、2 次实际工具派发；plain/off/timeout/broken 的原请求、directive、格式纠错及工具计数相同。另有实际 RecoveryLocalToolDispatcher / 原生产 sink / tiny Executor 验证。
- C/P 固定 fixture：C2/P2/G1，实际 resource inspect 与 Skill headings 各一次；TTY/plain/off 的图节点、Plan semantic hash 和调用账本相同。在 action_ready 停下，不宣称此 fixture 执行了脚本。
- Generate 完整 fixture：C2/P2/G2，一次原候选修正、一次 fake 审批、一次实际无害进程、一次正式 CommitStep。新 envelope 的 TTY/plain/off 与旧脚本 off 对照，Plan semantic hash、批准正文/UTF-8/hash、实际 run_script_path 文件及原 stdout parser receipt 相同。另测 native Skill 提前返回、wrapped Skill 拒绝、冲突载荷和已知 length。
- 合法 shell 包装内的真实 Bash 语法错误另经原 `check_script_body` / `bash -n` 拒绝；第二次 Generate 修正后才审批、执行和提交。新 envelope/plain 与旧脚本/off 的反馈记录和业务账本相同。实际 `main → run_terminal → _runtime_main` 也证明非 TTY 的 plain/off 交互入口在构造 Agent 前返回。
- 换 prompt 是协议能力变更，不能要求真实模型文字逐字等于旧模型。这里的 UI 开关对照固定相同业务 fixture；旁白模型调用数为 0。
- 字节单元覆盖空白、末尾换行、CRLF、Unicode、引号、反斜杠、heredoc、多行 Python/R 与非法嵌套包装；实际执行只使用一个明确允许的无害 Bash fixture，不宣称真实科学工作或所有语言进程已运行。
- 第二次 Generate 调用的原 assistant 与 validator_feedback 逐条保留，不加入渲染段落。具体反馈字段以已有协议为准；若原反馈没有 command_started 字段，不虚构该字段。另在第二次模型请求入口断言尚无审批和执行。

所有测试在 `/tmp` 白名单源码副本和新 scratch store 中运行。launcher 使用合成环境、fake config/provider、文件/SQLite/网络/subprocess guard；原仓库业务状态对测试进程不可访问。原宿主 `/proc` 指标读取被拒绝并沿既有 unknown 路径降级。依赖来自现有 Python 环境及已存在的离线 prompt_toolkit 缓存，没有安装、联网或真实 API 调用。

## 验证命令

在仓库目录运行；下列两个 `/tmp` 路径必须是新的隔离目录。离线依赖位置和各次准确命令也保存在 `verification.json` / 测试 launcher 中。

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/export_cli_model_progress_fixture.py /tmp/biomind-cli-reproduce-source --tree 54b6025bb96e65efc70d8017bb63dbc8ad73bd2d
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/cli_model_progress_support.py --export /tmp/biomind-cli-reproduce-source --results /tmp/biomind-cli-reproduce-results -q tests/test_cli_model_display.py tests/test_cli_model_recovery_tools.py tests/test_cli_model_progress.py tests/test_cli_model_progress_integration.py tests/test_cli_public_response.py tests/test_cli_cp_public_integration.py tests/test_cli_generate_integrity.py tests/test_cli_generate_public_integration.py tests/test_cli_final_response_regression.py
```

准确执行参数和每次候选 tree 在 `verification.json`。CLI-1 exact/tree 各 34 passed；CLI-2 先验证其余 60，再修本轮独立夹具并验证 2 项，总覆盖 62，不包装成同次 62。最终 CLI-3 exact **148 passed / 92.14 秒**，独立 tree `7029aa3f6cd4e96003142d6495382c54cc9f6c80` **148 passed / 93.21 秒**。早期 guard 拒绝、显示时序竞态、旧 fixture 依赖、临时断言错位和 T23 格式依赖保留在 `verification_history.md`；不累加中间 PASS，也不把旧审计 29 个缺陷 witness 算作本轮通过。

## 未运行与限制

真实模型/API、原 session、原业务数据库、科学任务、真实终端屏幕像素观察均 NOT RUN。真实 provider 说明质量、token 增量、格式失败率和实际首段时延 UNKNOWN。Installer / StepWorker 没有新增说明能力承诺。

常见凭据字段遮罩和 ANSI/OSC/C0/C1 清理只处理允许的显示副本，不能识别任意未知秘密。未读取真实秘密建立匹配表，也未改写原 stdout/stderr、日志、parser receipt 或批准文件。

旧缓存缺完整性元数据仍按原 UNKNOWN 兼容；Recovery 既有 64,000 字符缓存截取与原 digest 检查没有被本轮重写。已知不完整元数据保留不等于解决所有旧缓存完整性问题。更详细的审查限制见 `review_notes.md`。

语法纠错 fixture 还确认一个既有反馈投影限制：`exact_error` 保留实际 Bash 错误 code/message/stage，`contract_ref.execution_started=false` 也保留；但 `exact_violations` 中的 syntax_check / failure_stage 被现有 artifact 投影删去，field_path 变为 script 并出现不相符的 artifact 提示。本轮新旧协议反馈完全一致，没有为 UI 修改该业务上下文投影；不能宣称所有原始语法诊断字段都进入下一次模型请求。
