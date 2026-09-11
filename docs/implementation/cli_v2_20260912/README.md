# BioMind CLI V2 实施与定点审计

本轮在 `refactor/consultant-controller-authority` 的 `e75b95ecce117e9128c8b9079e554f6332c0f3b1` 上增量实施，保留前轮 CLI-1/2/3。没有回退到旧审计提交。业务路由、能力合同、CompletionGate 的问题只做静态审计，没有在 CLI 改动中修补。

审计主结论见 [replan_causal_audit.md](replan_causal_audit.md)：实际 `revise_plan` 已接纳并产生 `plan_revision_required`，图按该 typed outcome 进入 Planner；但随后真实 Planner 请求未携带这次修订指令。正式产物未通过 gate 的直接条件是 provenance 仍为 `expected`，`value_present=false` 是后续报告投影。现有合同中的 observation-only 能力、probe purpose 与正式文件输出存在矛盾，需要另开业务修复。静态证据只见一次执行，后续有取消 receipt，取消发起方未知。

## 本轮显示行为

- `BIOMIND_CLI_VERBOSITY=compact` 为默认；`verbose` 按需显示已分类的安全遥测，与已有 `BIOMIND_UI=plain/off`、TTY/fallback 渲染选择独立。
- Planner 字符计数、候选阈值、payload 间隔和常规 parse/normalize/compile 计时不进入 compact 主区；原 logger、callback、watchdog、响应反馈继续运行。真实失败、停止、重试、截断和取消仍显示。
- 实际工具的返回回执按 task、node、调用及工具身份聚合。空结果单列；失败、部分结果、未知或未派发不计成功。新的模型段落、审批、取消、错误与路由转向结束分组。遥测不会改变当前 provider scope，旧工具退出仍保留回执。
- stdout/stderr 实时显示副本继续按完整行处理跨 chunk 的机器 marker；结束出口仅保留结果摘要与安全详情，不再次整段回显机器 JSON。原 stdout、parser 输入、receipt 与审批正文不变。
- rc=0 只表示进程退出；Supervisor 的 VALID 标为局部执行检查，正式核验和提交另行判定。Recovery 的接纳提示来自实际 admission 事件，不从模型文字推断执行成功。
- TTY 主区保留模型段落、紧凑工具活动及必要事件，底部是一条状态与输入区。工具样式较淡，实际错误/审批/取消有语义颜色；plain/fallback 不添加样式控制字节。
- plain 只在通过 scope/fence/审批优先级仲裁后的真实阶段变化时追加一行；重复阶段和 provider 活动不刷历史，阶段边界先结束此前的工具组。
- 计划/代码审批使用原完整对象。F2、Ctrl+O 和单行 `/details` 仅浏览；Esc 返回，不提交批准，不进入模型反馈。多行内容中的命令文本保留原语义。无法可靠概括作用范围时自动展开完整代码；plain 使用完整安全正文回退。完整审批正文独立保留，不被遥测详情队列挤出。
- `/logs` 提供诊断文件位置，不把未分类原始日志尾部自动复制进界面；原日志采集和文件记录未删减。`verbose` 不因此开放原始请求、私有 reasoning 或认证信息。

公开说明切片只调整现有调用中的语言/详略提示，并优先使用本轮已验证 native Recovery directive 的现成 summary；较长的本轮公开说明可在详情查看。原 assistant/reception/context 原文保留，不新增旁白调用或文案 validator。真实模型可能因 prompt 变化产生不同文字或决策，本轮未调用真实模型，固定响应等价测试不能证明未来真实模型行为不变。

相对基线，`PUBLIC_MESSAGE_POLICY` 字符数为 803→1139（+336），`RECOVERY_SYSTEM_PROMPT` 为 5921→6510（+589）。这些是对应常量的字符数，不能直接等同 token。预计包含这些提示的现有请求输入 token 增加；输出长度可能受引导而改变，实际 token、延迟和语言稳定性未测，也未承诺零成本。

## 验收材料与复现

- [acceptance_matrix.tsv](acceptance_matrix.tsv)：U1–U14 的范围、证据和限制。
- [verification.json](verification.json)：提交树、命令、结果及本轮安全隔离记录。
- [verification/manifest.json](verification/manifest.json)：合成业务图的调用、原始执行/审批证据，以及 Recovery 实际接纳、预算和条件边的三模式对照。
- [source_matrix.tsv](source_matrix.tsv)：生产出口与显示用途。
- [replay_index.md](replay_index.md)：真实生产 renderer 的 PTY 原始字节、asciicast、终端快照及前后指标。
- [reference_mapping.md](reference_mapping.md)：官方 Codex/Claude 参考到 BioMind 的映射及版本限制。
- [review_notes.md](review_notes.md)：定点代码复核与修正边界。

所有运行均通过 `tests/export_cli_v2_fixture.py` 的安全源码白名单导出到新 `/tmp` 目录，再由 `tests/cli_v2_support.py` 的既有严格 guard 启动。只提供合成配置、fake provider、隔离状态与已准许的无害小进程；禁止原 session、原数据库、真实网络模型/API 和真实科学流程。PTY harness 使用内核 PTY 与生产 `Terminal/Application`，不是静态 mock。PNG/SVG/TXT 是从实际 PTY 字节重建的终端快照；原始 `.ansi` 与 `.cast` 是逐字输出依据。

```bash
python tests/export_cli_v2_fixture.py /tmp/biomind-v2-recheck --tree <commit-or-tree>
python tests/cli_v2_support.py --export /tmp/biomind-v2-recheck --results /tmp/biomind-v2-recheck-results -q tests/test_cli_v2_display.py tests/test_cli_v2_integration.py tests/test_cli_v2_output.py tests/test_cli_v2_business.py tests/test_cli_v2_recovery_route.py tests/test_cli_v2_public_style.py tests/test_cli_v2_terminal_fallback.py tests/test_cli_v2_plain_stage.py tests/test_cli_public_response.py tests/test_cli_generate_integrity.py tests/test_cli_model_display.py tests/test_cli_model_recovery_tools.py tests/test_cli_model_progress.py tests/test_cli_model_progress_integration.py tests/test_cli_cp_public_integration.py tests/test_cli_generate_public_integration.py tests/test_cli_final_response_regression.py
python tests/replay_cli_v2_pty.py --export /tmp/biomind-v2-recheck --results /tmp/biomind-v2-pty-normal --scenario normal --cols 80 --rows 24
```

使用全新目录；命令具体版本与可复现文件范围见 `verification.json`。上述合并命令的捕获模式在本机存在下述未解决限制。最终通过的验收分为前十个 V2/协议文件 166 项，以及后七个前轮文件 49 项；后者使用 `-s`，并由同一严格 guard 的子进程加 `-X faulthandler` 留下诊断记录。精确命令已单独保存。早期切片没有后续切片新测试文件，复现时只运行该提交已经包含的测试。

## 限制

本次没有收到规格引用的原 S1 终端附件或 S4 参考截图；文件名定位未匹配，不能认证原 session 的像素、终端尺寸或逐字屏幕时序。session 审计依据静态原始 receipt；PTY 回放使用明确标记的合成输入与事实投影，两者不混作原 WGS 结果。原运行 manifest 记录的磁盘 HEAD 与已加载代码身份也没有被强行等同。

两次 pytest 输出捕获模式的回归进程分别在 205、40 项通过后发生 SIGSEGV，根因未确证；相同源码的 166 项独立验收及 49 项诊断模式回归通过，生产 renderer 的 9 对 PTY 对照也通过。这些通过不能证明该原生崩溃已修复，详见 [verification_history.md](verification_history.md)；本轮没有为绕开崩溃去修改业务清理或降低原测试断言。

工具返回值有局部语义限制：既有 KB retriever 的空字符串同时可能代表无匹配或错误，本轮显示为 unknown，不计成功，没有改它的返回契约。安全显示仍屏蔽控制序列、凭据模式及超长单行；原始业务证据不因此改变。未知代码作用范围的 plain 审批回退会较长，这是保持完整审批对象可知所需的例外。

全部提交仅含本轮可隔离的代码、测试或脱敏产物；未 push。原有 dirty/untracked/staged 的保护核对结果与完整提交 hash 见 `verification.json`。
