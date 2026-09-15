# session_1789456793 连续故障修复

本分册接续 A1 / Plan V2，限定 Generate 纠错生命周期、Recovery 可读日志、精确步骤归属与 STOP 收尾。原事故只用于既有诊断定位，未恢复或执行。

- [实施及分层回执](implementation_report.md)
- [验证账本](verification.json)：区分修复前 witness、组件、连续图、真实外部能力。
- [连续事件索引](trace_index.json)：指向实际 provider messages、审批载体、内外 rc、attestation、Commit、步骤状态和正式报告。
- [实际导出 manifest](final_manifest.json)及 [版本等同性](version_ledger.json)：工作树 + 保护 overlay；逐进程 loaded_manifest 另列，导出不是执行证明。
- [保护账本](protection.json)、[净提交范围](commit_files.tsv)。

只用固定模型叶子和新 Python 合成小进程；不是 Java/JAR/snpEff，不是科学结果。parser、审批、执行器、attester、Reviewer、gate、Commit、持久化、日志 reader、STOP writer 走正式代码。没有预填步骤 ready/approved/succeeded；合成运行时能力观察与科学小进程替身明确登记。所有失败尝试保留。

复现使用现有 `tests/a1_unified_fix/export_and_run.py` 安全导出，加入本轮三个测试文件、V2 相关测试和账本所列目标；随后使用 `run_final.py --export <新隔离导出> --results <不存在的新目录> --watchdog-seconds 360 -q -p tests.a1_unified_fix.session_6793_manifest <目标>`。命令以受控非登录 shell 和已知解释器执行，环境仅显式 PATH/LC_ALL/PYTHONDONTWRITEBYTECODE；现有 guard 禁止间接正式资源访问。长图分组运行；测试 watchdog 不改变任何生产预算。PTY 使用既有 `test_f5_pty.py --export ... --results ... --mode tty|plain --scenario ...` 父注入器，普通 pytest 跳过该仅子进程入口不等于 PTY 验收。

**状态：本单限定机制 COMPLETE，生产净补丁已提交。** [第13节完整回执](implementation_report.md)与机器账本分开列witness、组件、图机制及真实外部能力。大JSON/终端收集副本的无损压缩位置见 [compressed_artifacts.json](compressed_artifacts.json)，原隔离结果保持原样。
