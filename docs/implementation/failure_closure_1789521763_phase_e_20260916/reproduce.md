# 隔离离线复现

本轮测试命令逐批记录在 `final_results.json` 的 `runs[].exit`；唯一 nodeid 在同文件和 V01—V20 矩阵中。原始 pytest 回执、请求、合成调用账本、压缩 archive 和失败历史打包在 `offline_evidence.tar.gz`，成员来源和 SHA-256 见 `evidence_index.json`。

前提：当前候选 commit 加 `loaded_sources.json` 中标识的先存安全 overlay/fixture。不要读取真实配置、凭据或启动脚本，也不要直接在原仓库导入生产模块。已知解释器为 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，不安装依赖。

在仓库目录，用**新的** `/tmp` 目录运行现有 exporter（不要覆盖已有见证）：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/a1_unified_fix/export_and_run.py /tmp/phase_e_new_run \
  --test tests/phase_e_1789521763/test_c1.py \
  --test tests/phase_e_1789521763/test_c2.py \
  --test tests/phase_e_1789521763/test_c3.py \
  --test tests/phase_e_1789521763/test_c4.py \
  --test tests/phase_e_1789521763/test_chains.py \
  --test tests/phase_e_1789521763/test_integration.py \
  --test tests/phase_e_1789521763/test_boundaries.py \
  --test tests/phase_b_1789471955/test_integration.py \
  --test tests/phase_b_1789471955/test_control.py \
  --test tests/phase_b_1789471955/offline_runner.py \
  --test tests/test_runtime_evidence_recovery.py \
  --test tests/test_observation_warning_recovery_stop.py \
  --test tests/test_recovery_reception_stop.py \
  --test tests/test_common_finalization_workflow.py
```

exporter 不导入原仓库生产配置。随后复用已有 guarded launcher，按 `final_results.json` 的批次/nodeid 运行，避免将慢的多图测试塞进一个 watchdog 窗口：

```python
import sys
from pathlib import Path
sys.path.insert(0, "tests")
from cli_model_progress_support import launch
rc = launch(
    Path("/tmp/phase_e_new_run/export"),
    Path("/tmp/phase_e_new_run/unit_results"),
    ["--noconftest", "-q", "-p", "no:cacheprovider",
     "tests/phase_e_1789521763/test_c1.py",
     "tests/phase_e_1789521763/test_c2.py",
     "tests/phase_e_1789521763/test_c3.py",
     "tests/phase_e_1789521763/test_c4.py",
     "-k", "not v04 and not real_graph_rc0"],
    installed_cli=True, watchdog_seconds=360)
assert rc == 0
```

图测试分批：C1 v04 + 链2 + parser；V20 两种失败；链3/链4；V17 两种 draft 变化；V19 Generate 迟到结果。`boundaries_final/regressions` 的旧 CLI 嵌套子进程测试被 `-k not actual_interactive_loop_process_exit` 排除，原因是既有 guard 拒绝它启动嵌套 Python；当前 13 种实际交互循环输入由 E 的 `test_v19_actual_interactive_control_zero_model` 验证，没有放开 guard。

最终三个 export 的生产映射相同；测试文件不同时只采用与最终工作树该测试文件哈希一致的结果。`loaded_sources.json` 给出最终测试哈希。源码保留在工作树，归档只保存测试证据；未复制用户科学输入或含凭据脚本。
