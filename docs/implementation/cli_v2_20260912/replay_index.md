# 真实 PTY 前后回放

最终 18 次真实 PTY 运行（3 场景 × 3 尺寸 × 前后）均退出 0，9 对产物的直接等价断言全部通过。另一次 80×24 verbose 运行通过，保留正常场景的 48 行诊断，调用数与 compact 相同。没有运行 TERM=dumb 的真实 PTY；该模式不能从这些回放外推。

| 合成场景 | 主历史逻辑行：前→后 | 真实调用事实 | 80×24 截图与回放 |
| --- | --- | --- | --- |
| 正常读取/搜索 | 48→3（减少 93.75%） | 2 次固定响应投影，5 次真实文件工具，其中空结果1次；0执行 | [前屏幕](replay/before-normal-80/final.screen.png) · [后屏幕](replay/after-normal-80/final.screen.png) · [后回放](replay/after-normal-80/terminal.replay.cast) |
| 局部 VALID 后正式失败及拟重规划说明 | 66→15（减少77.27%） | 3次固定响应投影、1次实际Supervisor fake reviewer，5工具、1次真实无害printf进程 | [前屏幕](replay/before-failure-80/final.screen.png) · [后屏幕](replay/after-failure-80/final.screen.png) · [后回放](replay/after-failure-80/terminal.replay.cast) |
| 当前 HITL 审批拒绝与取消 | 42→10（减少76.19%） | 原审批对象、hash、纯n输入及解析结果逐字相同；审批1、拒绝1、取消请求1、执行0 | [前屏幕](replay/before-approval-80/final.screen.png) · [后屏幕](replay/after-approval-80/final.screen.png) · [审批详情](replay/after-approval-80/approval-details.screen.png) · [后回放](replay/after-approval-80/terminal.replay.cast) |

120×36、42×18→64×20 resize 的相同三场景指标一致，详见 [完整矩阵](pty_comparison.tsv) 与 [逐对断言结果](pty_comparison.json)。所有记录采用最终同一个 replay 脚本 SHA-256：`3706a4babdff488f0e68eb7b9cdcaf68478b71eb9fc893a3668af731b217f856`。

生产基线为 `e75b95ecce117e9128c8b9079e554f6332c0f3b1` 的白名单 Git 导出。最终 after 录制使用 `11d5e9ca6843bb7d833db7c7b3b778c05c55f1a5` 的独立白名单 Git tree 导出，仅明确 overlay 本轮新 replay 测试，生产源码均来自该提交。此前 exact_worktree 的对照另逐字复核其 `cli_display.py`、`cli_terminal.py`、`cli_activity.py`、`cli_review.py`、`bio_agents.py`、`supervisor.py` 均与 UX2 commit `4b6dc2d44c2b7d2d516bb80f9700de9da3832722` 相同：[源码身份对照](replay_source_identity.json)。全部导出来源列表在 replay 下的 before/after-source-manifest.json；不提交整个源码导出。

`terminal.ansi` 是 kernel PTY 的原始字节，包含隔离 bootstrap 的合成配置及 pytest 收尾来源证据。`terminal.cast` 是实际 Application 时段的原输出事件；`terminal.replay.cast` 另加入 metrics 中记录的真实缩放事件与动作标记，便于回放。修改来源与原始/派生哈希均在 replay_derivation.json。没有替换输出事件或伪造显示顺序。

截图由该 PTY 的实际 VT 字节解码成字符格再以本机字体呈现；不是 DummyOutput 截图、独立 UI 演示或宿主终端像素截屏。PNG/SVG省略颜色；原 ANSI/cast保留实际语义颜色。所有最终未知VT列表均为空。最终窄屏自然滚出首个段落，但完整历史、录制和详情均保留它，不把最后一屏可见行数当作完整历史。

本次回放实际使用生产 Terminal/Application、进度 observer、公开响应投影、工具活动包装、无害 executor、Supervisor 和当前 HITL 输入/解析路径。模型输入是固定合成响应；正式失败是明确合成事实投影。没有把这段事件序列当作原 session 路由证据，详见独立重规划审计。

复现（先用本轮白名单 exporter 创建隔离 source；不进入业务启动入口）：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/export_cli_v2_fixture.py /tmp/biomind-v2-replay-source
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/replay_cli_v2_pty.py --export /tmp/biomind-v2-replay-source --results /tmp/biomind-v2-replay-normal --scenario normal --cols 80 --rows 24
```

基线导出加 `--tree e75b95ecce117e9128c8b9079e554f6332c0f3b1 --baseline-overlay`。其他场景用 `--scenario failure` 或 `--scenario approval`；宽屏用 `--cols 120 --rows 36`，缩放用 `--cols 42 --rows 18 --resize 64x20`。每次 results 需为新的隔离目录；完整实际命令及只含合成值的环境在各 invocation.json 中。

[验证过程和已纠正的失败](pty_verification_notes.md)保留了早期 recorder 竞态、guard拒绝及审批前置遗漏的真实记录；这些失败迭代仅引用 `/tmp`，没有混入最终成功素材。所有复制文件的字节范围及SHA-256见[素材清单](replay_manifest.json)。未复制scratch、执行中间目录或整份源码导出。

不运行BioMind也可直接核对已提交证据：`python docs/implementation/cli_v2_20260912/verify_replay_artifacts.py`。该脚本只读取本目录的显式素材，核验412个文件hash、9对审批/回执/公开段/调用数等价及compact/verbose调用次数。

最终 plain 专项补丁已纳入上述最新提交及全部后置回放。`Terminal` 类正文与上一提交相同，`cli_terminal.py` 整文件因 PlainTerminal 修订而不同：[精确比较](plain_followup_source_identity.json)。最新 plain 专项3例均通过，当前固定两阶段场景compact/verbose各3行、off0行，重复阶段及provider活动不新增行；该专项是实际显示sink验证，不冒充真实业务模型调用：[结果](plain_stage_validation.json)。
