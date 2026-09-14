# F2 实际 helper 作用域的补充修复

最终组件验证 **31 PASS，实际退出码 0**。冻结生产文件 `agent_core/step_effects.py` 的 SHA-256 为 `2b6b56bc8e49a2addc32b3f2f776d45d0fccccf369546b04605b269849a99c3e`；验收加载基线仍单独记录为 `8bf302c37bb38259d4878524ddc6e2d0e2a27e60` 加当时 overlay。最终组合图使用 root 随后的统一候选，不能把此组件结果扩成全链通过。

限定复核发现原 scope 检查把 Python 字符串中的 marker 当作源码，也把 `python -c/-m/script.py ... -` 中的数据参数 `-` 当作 stdin 入口。新实现只在 marker 物化路径增加两项检查：Python `tokenize` 必须识别该独立行为真正的 COMMENT token；Bash 单引号 heredoc 必须属于同一 Python 命令的实际 stdin selector。无法判定的注入位置进入既有可纠正拒收，不展开通用 shell 或科学语义扫描，不影响没有 marker 的普通动作。

新测试用实际小进程复现旧字节替换的失败：四种错位在实际调用处无法获得 helper，`-m json.tool` 则把 stdin 当 JSON 读取。它们是明确未经过新准入的旧行为见证；新 materializer 在执行前准确拒绝。正例保留 native Python argv、原 Bash/Python/code/一致 code+argv，以及 `-P`、分离/拼接 `-W/-X`、短开关组合、`--`、stdin 后的 `-`、中文、空格、引号和带引号分号参数。

第一轮的新 `-m runpy` 见证误用了模块参数，原始失败保留。第二轮实测推翻了“`-x -` 会跳过 stdin 首行 helper import”的假设：该 Python 入口返回 0 并正常发布合成事实；因此没有按猜测加入限制，最终把首行 marker 的 `-x -` 作为真实正例。两轮失败均未抹去或改写为生产通过。

[验证账本与三轮原始回执](f2_scope/verification.json) 保存独立加载版本、逐用例结果、进程输出及 hash。本次未再发现 F1 三入口、AskUser END、F5 默认正文/输入 owner 的额外阻塞；新增驱动的安全复核范围和限制也记入账本。原 session、真实 provider、科学工具及未提供的 R/其他解释器能力没有被此结果验证。
