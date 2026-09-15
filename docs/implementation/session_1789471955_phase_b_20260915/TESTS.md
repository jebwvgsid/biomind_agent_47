# 离线验证命令与结果

所有命令从 `/public/home/liusiqi/03.毕设/Biomind_v1` 执行。bootstrap 在导入 BioMind 前拦截 network connect、`.env*`/`启动.sh` 读取，并禁用 dotenv 与自动 pytest 插件；不初始化真实 provider。隔离科学 fixture 不是原任务科学结果。

## 1. 定向验收和正式 Commit/审批边界

```bash
env -i PATH=/usr/bin:/bin \
 /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
 tests/phase_b_1789471955/offline_runner.py \
 tests/phase_b_1789471955 \
 tests/test_plan_contract_approval_scope.py \
 tests/test_typed_verifier_commit.py \
 --basetemp=/tmp/biomind_phase_b_release_20260915 \
 --junitxml=/tmp/phase_b_release.xml --tb=short
```

**93 passed，4 subtests passed，84.87s**。其中阶段 B 定向用例 72 项。

## 2. 最终累计账本落盘接缝

```bash
env -i PATH=/usr/bin:/bin \
 /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
 tests/phase_b_1789471955/offline_runner.py \
 tests/phase_b_1789471955/test_integration.py \
 --basetemp=/tmp/biomind_phase_b_durable_20260915 \
 --junitxml=/tmp/phase_b_durable.xml --tb=short
```

**12 passed，20.11s**。覆盖同 attempt 正反例以及 10 种压缩/取消模式，额外断言 final Recovery receipt 与真实 episode 归档均保存完整累计维护调用数。该项是上一组的定点后续验证，有重叠。

## 3. 已有 Generate / Plan / CLI / 审批保护回归

先创建隔离产物目录（测试写出器要求父目录存在）：

```bash
mkdir -p /tmp/biomind_phase_b_preservation_final
timeout 180s env -i PATH=/usr/bin:/bin \
 BIOMIND_GER_ARTIFACTS=/tmp/biomind_phase_b_preservation_final \
 BIOMIND_OFFLINE_RESULTS=/tmp/biomind_phase_b_preservation_final/results.json \
 /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
 tests/phase_b_1789471955/offline_runner.py \
 tests/a1_unified_fix/test_generate_class_checks.py \
 tests/a1_unified_fix/test_generate_class_reports.py \
 tests/planner_fix_acceptance/test_planner_recorded_workflow.py::test_corrected_recorded_plan_real_approval_reaches_generate_consumer \
 tests/planner_fix_acceptance/test_planner_cancellation_workflow.py \
 tests/test_approval_visibility.py tests/test_input_boundary.py \
 --junitxml=/tmp/phase_b_preservation_release.xml --tb=short
```

**80 passed，63.99s**。前一次同组验证漏建产物目录导致 9 个 FileNotFoundError；补齐目录后通过，没有为该错误修改生产行为。

## 4. 收集可提交证据

```bash
python tests/phase_b_1789471955/collect_evidence.py \
 /tmp/biomind_phase_b_release_20260915 \
 docs/implementation/session_1789471955_phase_b_20260915/evidence \
 /tmp/biomind_phase_b_durable_20260915
git diff --check
```

收集：10 种集成模式、13 个实际交互子进程、8 个审批入口、3 份历史事实投影；同模式以最后一次接线结果为准。JSON/GZIP 与 JUnit/TXT 保存于 `evidence/`，文件 hash 在 `artifact_hashes.json`。

## 5. 旧套件诊断的范围

离线 `--baseline` 仅通过 `git show de7037f7a522e5a7350c07b6349b80965b8f49ed:<production target>` 加载原来干净的目标代码；其他既有 dirty 模块保持原字节。原始目标核对和哈希见 protection.json，未 checkout/reset。

探索组 `tests/test_semantic_completion.py tests/test_semantic_completion_workflow.py tests/test_recovery_compaction_production.py tests/test_schema2_context_compaction.py tests/test_failure_packet_incident_lineage.py`：BASE 14 failed/67 passed，早期补丁 16 failed/65 passed。14 个共同失败主要是旧 report/CAS 断言及 12k fixture 容量已不足以容纳初始 System/Seed；新增两个失败是严格完整 schema 使旧小容量 fixture 转入要求 archive 的层次路径。没有关闭校验来消除这些失败。

`tests/test_inspection_evidence.py::test_I02_I03_I04_I10_real_handoff_reload_and_body` 的首失败在 BASE 与补丁各复现一次；它预设旧 inspection 对象数量。更广探索组被主动中断，不报告成功。各已完成诊断的失败列表和源日志 hash 保存在 `legacy_test_diagnostics.json`。

真实 provider、真实缓存命中率、真实供应商账单与科学 E2E：**NOT_RUN**。
