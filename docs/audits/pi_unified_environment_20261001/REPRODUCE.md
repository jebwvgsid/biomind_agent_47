# 审计夹具与证据说明

主报告：[README.md](README.md)。本目录仅审计产物，可单独提交。所有脚本从自身路径计算 checkout 根；需要本机已安装的 BioMind Python/Pi/Node，不安装依赖。

## 文件

| 文件 | 内容 |
|---|---|
| baseline.json | 起始 HEAD/branch/dirty 摘要和关键源码/dist/补丁散列；非完整仓库快照 |
| case_evidence.json | 两个指定 session 的调用定位、命令、结果散列、usage；没有结果数据、完整消息或认证头 |
| registry_evidence.json / current_lookup.json | 三个明确登记来源和既有 inventory 的相关事实、无探测查询结果 |
| artifact_metadata.json | 两个已完成输出目录的文件元数据；小文件摘要散列，无科学结果原文 |
| python_witness.json | 7 个新增当前行为见证，断言包含已知缺陷，不等同目标验收 PASS |
| existing_tests.json | 5 个现有 native 集成测试和 1 个 protected 持久环境消费测试结果 |
| node_witness.json | 当前 dist 的角色激活、原生 bash 接线/流更新/管道/timeout，及 TS 内存转译比较 |
| request_reconstruction.json | 当前 serializer 加历史前缀消息的离线重建统计，非原始请求体 |
| acceptance.tsv | 附件 32 项当前状态、证据范围和后续目标 |
| delivery_verification.json | 提交前完整性与交付边界检查 |

## 本轮实际验证方式

在 checkout 根执行新增 Python 见证：

```sh
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B docs/audits/pi_unified_environment_20261001/audit_fixture.py
```

它只创建 TemporaryDirectory，禁止 config/dotenv/旧 Agent/provider 模块导入；PackageManager 通过 `__new__` 使用现有登记函数，跳过扫描构造器。选择阶段 Popen 被替换为抛错计数器；正式执行仅运行临时脚本。安装 executor 被无网络受控替身替代，返回固定成功/失败；第二次写失败和 stale writer 是合成故障。fresh-process 查询另启动同 Python 读取同临时权威记录，不靠同进程缓存。注册产生的 shim 不执行。

本轮还执行了这些现有用例（其自身使用临时数据与 NoProvider guard）：

```text
integrations/pi_runtime/tests/test_native_workflow.py::NativeWorkflow
  test_T13_native_external_cwd_output_no_contract_no_bwrap
  test_T14_replay_and_new_connection_one_effect
  test_T16_cancel_cleans_descendant_and_replay
  test_T17_T19_migration_pending_not_replayed_and_requires_new_request
  test_T18_legacy_and_profile_mismatch
tests/test_registered_runtime_reuse.py
  test_formal_command_consumes_persisted_registration_without_model_env
```

protected 单例经 `test_external_access_integration` 先安装 NoProvider guard，再 `pytest.main(['-q','-p','no:cacheprovider', '<上述单例>'])`；未跑全仓测试。

Node 两个脚本使用安装的 `.runtime/node/bin/node`，预加载现有 `integrations/pi_runtime/testing/offline-guard.mjs`，在独立临时 agent/home 范围运行。guard 的 `BIOMIND_PI_TEST_ROOT`/`BIOMIND_PI_GUARD_TRACE` 指向 `/tmp/biomind-v2-node-guard`，其 socket/fetch 拒绝网络；reconstruct_request 显式传入内存 fetch 替身，未放行 socket。没有真实凭证读取，使用固定合成 credential marker。

```text
node --import <runtime>/testing/offline-guard.mjs <本目录>/native_witness.mjs
node --import <runtime>/testing/offline-guard.mjs <本目录>/reconstruct_request.mjs
```

应在独立 agent 环境、PATH 仅 `/usr/bin:/bin` 下复现 native PATH 缺失场景。native_witness 使用真实 Pi extension + FakeDomain + scripted provider + 真实临时 bash；因此能证明工具激活、消息接线和进程语义，不能证明自然模型会先查库。reconstruct_request 的当前系统规则/Skill 索引/偏好和合成 request/cwd 元数据不同于历史原始进程，限制已写在结果中。

`collect_evidence.py` 只读两个写死的 session 和明确登记/源码文件，不执行里面的任何命令；不建议当后续验收脚本反复运行，因为它会把本目录的起始 baseline 换成运行时的新基线。本次已冻结结果应作为审计证据保留。当前只读 lookup 使用 Guard、临时 Domain data-root、明确生产登记读路径和 Popen 禁止计数；没有写真实登记。

## 已知边界

没有重跑 TAD、ORA 或 reproduce.sh。case_evidence 中 ORA 重跑命令是历史会话证据，不是复现指令。没有真实安装、环境健康检查或家目录搜索。没有覆盖真实 module/login-shell 超时、完整安装取消/崩溃、跨节点部署以及后续优化尚未实现的目标。所有原始数据/会话/大日志均留在原处，不打包进 Git。
