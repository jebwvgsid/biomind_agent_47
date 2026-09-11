# 验证过程与修正

本文件区分实现问题、fixture 问题和显示契约变更，不把早期失败删除或包装为通过。原失败记录仍位于本轮 `/tmp` 验收目录；提交只包含最终成功的脱敏 PTY 材料与此过程说明。

- UX1：独立候选 12 项通过；选定前轮回归与新显示测试共 115 项通过。
- UX2：复核发现审批详情循环的小数剩余时间可被 `safe_input` 取整为 0（原接口中 0 表示无限等待）；已修为明确截止检查与向上取整，保留原等待状态处理。另发现 Recovery 已完成 checkpoint 可包含观察预算超限投影，已在显示副本标为 partial，不计成功。新测试覆盖两者。
- 实际 PTY 揭露纯 Planner telemetry 会改变显示 active scope，使随后真实返回被当作旧调用而无法聚合。已修为诊断/详情不迁移当前 scope，保持显式 fence 处理，再用同一生产 `_planner_progress` 重放验证。没有把测试替换成理想化 emit 序列。
- 初版 PTY harness 省略原 `_aprompt_user` 的 `set_context` 前置，造成基线把草稿与 `n` 连在一起当反馈，新版则收到纯 `n`。已补齐两版共有的真实 UI 前置，保留同样按键；最终 approval.json（原 payload、hash、输入和解析结果）逐字相同。早期仅“都拒绝”不被当成响应完全等价。
- 初版无害 executor fixture 的脚本未放入 guard 允许的 scratch 子树，执行被拒；已移动合成脚本到 scratch，没有放宽 guard，也没有运行原科学任务。
- UX2 独立候选 150 项通过；完成 UX3 后独立候选 159 项通过。UX3 首轮测试曾对带末尾空格的 synthetic summary 要求逐字相等，但原 `RecoveryDirective` 已有 strip 行为；仅去掉 fixture 的尾空格，保留长英文、多句和零新增重试的条件，未修改业务 normalizer。
- `TERM=dumb` 和 BrokenPipe 的补充测试经生产 `run_terminal` 选择真实 plain owner，四种组合均通过；该验证没有假称进行了 dumb 终端的 PTY 绘制。
- 复核前轮相关测试时，旧 suite 出现 8 项失败：五项旧逐事件/工具名显示预期与默认 compact 冲突，一项旧 VALID 标签断言，另两项 graph fixture 原本自动返回 `y`，绕过真实输入流程而未退出新版审批详情。对应测试在本轮开始时均为 clean。逐事件用例改为明确 verbose；标签断言改为局部 scope；自动审批 fixture 在保留同一 `y` 前退出详情并释放显示优先级。原调用数、状态、原始字节、预算和公开 flush 早于派发的断言保留，没有延长原超时或修改生产代码。

- plain 阶段变化补丁的独立候选为 31 passed；只追加已通过显示身份及优先级仲裁的阶段变化，不改变 TTY。
- 最终同一候选树 `7164c04774838da92ee138b888b406e26019d0ef`、安全导出签名 `b1a9540392ac69d72b7c1207557701fd0db8a25da525c6bec20953011f1627a9`：新 V2/协议回归 **166 passed in 39.77s**，前轮七组回归在诊断模式 **49 passed in 121.24s**，两批都确认源码无漂移。
- 必须保留的异常：首次 215 项捕获模式批次完成 205 项后以 SIGSEGV（-11）退出；独立的 49 项捕获模式批次完成 40 项后也以 -11 退出，且前一次正在运行的用例本次已经通过。未取得 fatal 栈，原因仍为 UNKNOWN。最终 49 项仅在 Python 加 `-X faulthandler`，pytest 加 `-s` 关闭捕获；源码、guard、原断言和合成环境没有改变。70 秒 watchdog 的线程采样不是 fatal 根因证据。诊断模式通过不能表述为捕获模式崩溃已修复；未据此改动原有 dirty fixture、业务清理或原 session。

最终命令、结果与源码签名记录在 `verification.json`；其中不完整批次的 `abnormal_exit` 不标为 PASS。PTY 各次实际命令、源清单、逐字对比与哈希见 `replay_index.md`、`replay_manifest.json` 和 `pty_comparison.json`。
