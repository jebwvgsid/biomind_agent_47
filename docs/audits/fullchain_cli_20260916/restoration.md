# 基线、overlay 与还原

BASE：`8a84fea91d42f8f036d66badef36b13aec6203f8`。原分支：`refactor/consultant-controller-authority`。

本轮候选工作树：`/tmp/biomind_fullchain_cli_candidate_20260916`，分支 `fix/session-1789537621-fullchain-cli-20260916`。原工作树没有 checkout、reset、stash、提交或覆盖。

## 已验证的组合

测试对象是 BASE 加进入本轮时已有 overlay，再加本轮修改，**不是裸 BASE 或只应用新 commit 的对象**。`source_manifest.json` 固定 21 个生产文件及测试文件的最终字节；完整离线导出包含已声明的既有测试依赖。测试合成配置由现有 `tests/cli_model_progress_support.py`/offline bootstrap 注入，不加载原配置、凭据或启动脚本。

私有还原材料保存在 `/tmp/biomind_fullchain_cli_20260916`：

- `overlay/`：进入本轮时的相关既有 Python overlay，仅供本机还原，不加入本轮提交。
- `candidate_baseline_hashes.json`：候选初始文件指纹。
- `baseline.json`、`original_status.z`：原分支、索引及工作树见证。
- `verification_source_snapshot.tar.gz`：最终白名单源码快照；SHA-256 见 `source_manifest.json`。

## 复验方法

最直接的复验对象是白名单源码快照，或保留的候选工作树。使用已经安装的 biomind Python 和现有离线 bootstrap；各次 `invocation.json`/`test_results.json` 保存精确命令、合成环境、外部看门狗及结果。它们不会恢复原 session。

需要从 Git 手工还原时，在新隔离工作树中：

1. checkout BASE；恢复清单中的原有相关 overlay 和原有未跟踪测试依赖。
2. 应用 `commits.json` 中生产补丁，但排除 `agent_core/workflow/managed_stop.py` 与 `tests/test_recovery_tools.py`。
3. 对这两个共享文件应用本目录 `logical_overlay_delta.patch`。该 patch 的基准是原 overlay；它只包含本轮增量，避免把先存修改也提交进去。
4. 比较 `source_manifest.json` 的生产与测试 SHA-256；不一致则不是本次验收对象。随后使用白名单导出与离线 runner。

两个共享文件在新 commit 中仅包含本轮逻辑差异，因此不能把候选 `git diff` 的剩余 overlay 当作漏提交。配置来源回执证明有效选择及阈值；原部署采用哪个策略仍须在部署时确认，本轮未部署。

## 外部运行限制

外部模型/HTTP/socket、非白名单进程及原会话写入由离线 guard 拒绝。仅用已经安装的 Bash/Python 执行预先登记的合成微型脚本。Java/snpEff、真实缓存测试、原数据分析和原任务恢复均未运行。
