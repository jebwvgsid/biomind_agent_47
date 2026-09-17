# Q6/Q7：现有批准身份与现状见证

本附件针对本轮磁盘工作树，基线为 `4ea823511328ed4035ec2a99ddf09a3419bb288a` 加 `evidence/baseline.json` 中列出的用户既有修改；不是只检查 HEAD。

## 可以复用的权威边界

`/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/action_runtime.py:1119` 的 `build_action_approval_identity` 把脚本摘要、能力契约摘要、runtime/artifact/policy manifest 摘要和工作目录纳入动作身份；`:1141` 的 `validate_action_approval_identity` 拦截变化，其中 runtime manifest 改变输出 `RUNTIME_BINDING_SNAPSHOT_DRIFT`。环境修复后应重建 action/attempt，按这一机制判断代码批准是否仍有效，不能修改已批准动作的环境然后沿用旧身份。

`/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/plan_tracker.py:8611` 的 `build_approval_scope` 纳入正式计划、输入、所选 Skills、约束和批准投影。能力契约/步骤语义变化会改变 `semantic_plan_hash`，须走既有正式修订和重新批准；仅增加诊断历史不应无条件重做全计划批准。代码中仍有兼容字段 `renderer_invocation_hash`，本报告不据此要求恢复已退役 renderer 的控制权。

安装授权是另一层操作范围，不能从科学计划已批准推导；具体安装授权接线见 `notes/install_skills.md`。本轮的批准函数见证只证明已有动作和计划审批能力，不证明安装授权已贯通。

## 已执行验证

执行脚本：`/public/home/liusiqi/03.毕设/Biomind_v1/docs/audits/environment_install_recovery_20260917/evidence/approval_witness.py`。在 `/tmp` 使用已安装的 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，禁止 dotenv 读取、网络和任何子进程；文件写入仅审计目录或临时目录。真实生产函数仅接收合成值，不读取原任务来恢复状态。

`evidence/approval_results.json` 记录九个 PASS 现状场景：相同身份接受；脚本、能力、runtime、artifact、policy、cwd 六种变化拒绝；同路径可执行文件内容改变仍通过路径级预检；注册路径删除被拒绝。PASS 只表示观察得到验证。

复用 `tests/test_action_scoped_runtime_bindings.py` 和 `tests/test_plan_contract_approval_scope.py`：21 项中 19 PASS、1 FAIL、1 ERROR。完整记录为 `evidence/approval_existing_tests.txt`。失败均位于 `SnpeffCapabilityContractTests`，期待 `genomics.snpeff_annotation.check_contig_compatibility/build_database`，而当前 `/public/home/liusiqi/03.毕设/Biomind_v1/skills/genomics/snpeff-annotation/SKILL.md:90` 仅声明 `execute/inspect`。这证明该既有测试与当前能力名称不一致，不能归因为本轮修改，不能据此宣布审批机制全部失败或全套测试通过。本轮保留原测试和生产文件。

## 版本与健康事实仍需补齐

`action_runtime.py:615` 的 `validate_runtime_paths_prelaunch` 检查路径、权限、文件类别/非空；`:475` 的 resolver manifest 保存的是 runtime 值摘要。合成工具在同一路径被替换字节后，旧 resolution 仍通过此预检，重新 resolve 的 manifest hash 也不变。这只证明这两层单独不检验版本/文件内容，不声称其他证据层没有 fingerprint。未来应把健康检查所验证的工具版本、wrapper/Java 依赖身份与已有 `runtime_evidence.scope.environment_fingerprint` 关联，变化时使相应事实过期并重新批准受影响动作。不能以路径没变宣称安装升级不影响批准身份。

未执行：真实安装、模型、原会话恢复、真实工具科学运算、完整 WGS、修复后回归。独立测试缺口应在对应逻辑补丁中修订，以当前正式能力契约为准。

收尾版本补记：09:17Z `plan_tracker.py` 出现并行资源分配修改；仅涉及 `_rewrite_plan_resource_bindings_after_allocation/_prepare_atomic_plan_objects`，不是上述批准函数。测试实际导入源码身份和时间在 `approval_results.json`，不能将其称为所有后续工作树修改的整体验证。
