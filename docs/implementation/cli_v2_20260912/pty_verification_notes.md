# 隔离真实 PTY 验证记录

生产基线：e75b95ecce117e9128c8b9079e554f6332c0f3b1；白名单 Git tree 6c7a2003340b824c6f03f8b261c734a570c685ae。本轮 replay test 以显式 baseline_test_overlay 加入基线；未将当前 dirty 生产代码混入基线。当前 exact_worktree 的测试结果与随后 candidate tree 结果分别记录。

最终锁定事件源 tests/replay_cli_v2_pty.py SHA-256：3706a4babdff488f0e68eb7b9cdcaf68478b71eb9fc893a3668af731b217f856。v6/v7 的前一版 SHA 为 86aeb36717c2436bbde7491509e6c8da7197b04ea19b73f0ca99b3336b98c657。最终 v8 全尺寸前后记录都使用最终同一 SHA。

使用 kernel PTY + TIOCSWINSZ + 真实生产 Terminal.app.run_async；业务线程运行固定合成输入，主线程运行真实 prompt_toolkit Application。F2、PageDown、Esc、中文双行输入及 Ctrl+C 均由 master FD 写入。没有以 DummyOutput 代替终端。各目录保留 raw 字节、带时间 asciicast、真实渲染票据、业务调用数、最后屏幕文本/SVG/PNG。截图从真实 VT 字节解码，使用本机 Noto CJK 字体重绘字符格；忽略颜色样式，因此不能声称与宿主终端像素一致。未知 VT 序列单列记录；已完成基线场景该列表为空。

生产函数范围：实际 BioAnalystAgent._planner_progress / _planner_observed_call、公开 JSON 投影及 emit_public/flush_public、call_with_activity 包裹实际新建文件读取和 glob；failure 另调用实际 executor.run_script_path、SupervisorNode.run 与 OutputRouter 正式结果出口；approval 使用实际 HITLMiddleware.review_code、_parse_code_response、BioAnalystAgent._prompt_user，fake interrupt 只接通当前生产审批显示和真实终端输入。没调用 agent 构造器、业务入口、原 session loader 或模型网络接口。fixed public JSON 是合成模型响应，并非恢复历史 session 的原始模型消息。

failure 场景的“正式产物证据不完整”是明确合成事实投影，不是本次从原 session 中重建的真实路由。真实生产 Supervisor 的局部 VALID 和正式失败显示出口可比较，但未以该合成事件顺序证明原 session 进入 Planner 的原因。该路由由单独静态审计和合成 typed-route 测试负责。

中间验证历史（不计入最终成功数）：

- screen 独立会话方式没有成功创建终端，改用标准库 kernel PTY；未更改生产代码。
- 可选 pyte 依赖网络 DNS 不可用；未安装包。改用记录未知控制序列的局部 VT 解码器，同时保留原始录制供独立终端回放。
- 初版 recorder 在 snapshot_ready 重复捕获，导致最终截图被 pytest 收尾信息覆盖；加入一次性 capture 标记。相应 early before-normal 不作最终截图证据。
- 旧 ConfirmationManager 场景虽通过，不能覆盖当前 HITL→OutputRouter 审批路径；v5 起改用当前实际生产路径。
- v3 导出含 harness 字符串转义 SyntaxError，只静态解析失败，没有运行该 source。
- v4/v5 failure 的 tiny script 放在结果根目录，违反已有 guard 仅允许 TMPDIR/scratch 下无害脚本的约束，执行返回 rc=1/OFFLINE_EXTERNAL_DENIED。曾误判默认 interpreter，核对源码后纠正：.sh 默认就是 bash。v6 将新脚本移入 scratch；未放宽 guard，实际 stdout、stderr、rc 和脚本 hash 已保存。
- v2/v5 approval 录制器的初始详情关闭和批准详情按键存在竞态，产生输入等待超时。v6 先确认初始 modal 已关闭，再启动 review；捕获详情后不重复 F2。生产审批及输入逻辑未为测试改变。
- v6 after 80×24 三场景运行成功；真实 observer 诊断使原 call scope 变成历史 scope，导致读/搜索没有聚合。已作为具体生产问题交显示 owner 修复，旧 after-v6 保留为修复前证据，不冒称工具聚合已通过。

每个子进程只获得记录在 invocation.json 中的合成环境。网络、原根目录、原数据库、敏感配置、未列入允许集合的 subprocess 均被原 guard 拒绝。tiny script 仅 printf；fixture stores 全在隔离结果目录。

- v7 精确比较审批发现：harness 直接调用同步 `_prompt_user`，未镜像原实际 `_aprompt_user` 的 `owner.set_context` 前置。旧基线把该输入视为 interactive_prompt，误把预留中文草稿与 n 拼接，虽然结果也是 reject，却走反馈拒绝分支。v8 恢复原 UI 的 set_context 前置，未改变任何人工按键；前后 approval.json 全字节相同，确认输入是单独 n。旧 v6/v7 不能作为完整审批响应相同的证据。
- v8 保留最终语义颜色实际输出属性（原始 cast/raw 有终端 SGR），截图仍为明示忽略颜色的字符格重绘。最终模型公开段落、批准对象及其解析结果、tiny script 字节及 raw receipt、调用次数，均由 compare_pty.py 作直接等价断言。

最终对照指标（完整尺寸的同事件源）：正常 48→3 逻辑行；失败与拟重规划事实投影 66→15；审批拒绝与取消 42→10。缩减比例按主历史中的逻辑换行计算，不混入状态重绘、详情正文或 shell/pytest 收尾。工具组分别是读取3项、搜索1次、空结果1次；failure 另有进程已退出且正式核验中的执行组。2/3/2 个公开段落全部保留，ID不重复。

最后结果：v8 before/after 3尺寸×3场景共18次真实PTY运行全部退出0，9对 compare_pty.py 全部断言通过；另一次80×24实际verbose录制退出0，其normal48行与compact3行调用次数相同。所有最终源文件 hash 检查保持不变；所有最终VT未知序列列表为空。

`terminal.cast` 保留 recorder 原始 output 事件；`terminal.replay.cast` 根据同目录 metrics.json 中实际记录的 resize 时刻加入 r 事件，并加入实际按键动作的 m 标记，原 output 事件逐字保留，哈希记录在 replay_derivation.json。此格式依据 [asciicast v2 官方说明](https://docs.asciinema.org/manual/asciicast/v2/#r-resize)；缩放场景应优先用 terminal.replay.cast 回放。transcript.txt 是实际历史票据顺序的纯文本，diagnostics.txt 是实际详情副本，并非另造理想事件串。

最终可提交素材已更新为独立提交树：after全部9组及verbose来自commit7481854c80f162d1f0ea9135a81dbf16cb99ea0f，tree2bb92f2ed935c0c9da5ba57f9b711400d81c47a0，signature d63ff8c9c639fe269a14bc98545657ec971989be273fcc22e4e45d66bef5b9a2。唯一测试overlay是此前尚未进入生产commit的tests/replay_cli_v2_pty.py，hash3706a4...；没有覆盖任何提交树生产文件。独立after9组均退出0，和before完整9对等价断言通过；不再以exact工作树代替最终提交的可复现验证。先前exact录制仍保留/tmp，不提交其after素材。

最终可提交素材已更新为独立提交树：after全部9组及verbose来自commit11d5e9ca6843bb7d833db7c7b3b778c05c55f1a5，treee3e0091e9e80976868a179cf2aeefde6a5883124，signature b13d3430fe260cd302e886922a13797e11a1901701d6af0fe5e109b21759ae15。唯一测试overlay是此前尚未进入生产commit的tests/replay_cli_v2_pty.py，hash3706a4...；没有覆盖任何提交树生产文件。独立after9组均退出0，和before完整9对等价断言通过；不再以exact工作树代替最终提交的可复现验证。先前exact录制仍保留/tmp，不提交其after素材。

最新plain专项：3例实际sink目标通过；固定两阶段场景compact/verbose各3行（规划、公开说明、审批），off0行。12次重复规划阶段和provider活动不会重复刷屏；真实已接受阶段变化出现在工具组之后。这个定量记录只针对最新PlainTerminal，不把TTY的48→3等指标当作plain前后测量。TTY Terminal类源码hash前后一致，但整个cli_terminal.py文件hash已变化，已明确分别记录。harness保持3706a4...不变。
