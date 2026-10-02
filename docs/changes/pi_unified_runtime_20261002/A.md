# A：登记可靠性与安装目标验证

15 项 PM 事务测试、16 项 Domain 安装目标/失败/恢复测试通过。使用临时目录及真实合成子进程，无真实软件安装或科学计算。

PM 采用稳定 `.lock` 文件，锁内重读与合并，临时文件 fsync 后原子替换。省略字段保留，身份冲突拒绝，损坏库不当空库。新增能力保留独立 unknown 声明证据。shim 派生错误与权威提交分开报告。

安装仍需原确认；目标未创建时先安装，再按精确目标启动结构化验证。任意 shell rc=0 不能作为包验证。验证完成保存原 claim 恢复依据，登记失败仅重试登记，丢失回执及取消按实际提交事实对账。当前安装 API 仍创建 prefix 环境，runner-only 登记不代表支持 runner-only 安装。

测试命令：`/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B tests/test_registry_atomic_20261002.py`，以及 `.../python -B integrations/pi_runtime/tests/test_install_targets_20261002.py -v`。日志见同目录。

Git：`deltas/A-staging.json` 记录精确来源。Domain service 依赖既有未提交 native/profile 代码，本轮安装替换无法直接应用审计 HEAD；保留有效工作树并提交 before→after 补丁，不夹带该文件原有改动。基线与前像位于 `/tmp/biomind_impl_20261002_baseline`，新 SHA 单独 checkout 不足以复现。其他可独立应用的源码块直接入 commit，同时保留 delta。
