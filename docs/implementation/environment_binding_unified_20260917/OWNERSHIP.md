# 共同基线与写入权

实际基线为 evidence/baseline.json 的 HEAD 加其中列出的受测源码，不是审计 HEAD 单独可运行。
精确前置源码和已跟踪补丁保存在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/`。恢复时先 checkout 记录的 HEAD，再按 manifest 校验 prerequisite_source 并覆盖对应相关源码；不得读取或复制凭据。initial_status.txt 保留开始时索引与工作树状态。

| 负责人 | 独占文件/区域 |
| --- | --- |
| root | bio_agents.py（安装节点单独交接）、node_context.py、runtime inspection/evidence 接线、SkillManager、最终 WGS Skill、整体验证 |
| r1_rollback | plan_tracker.py、plan_bindings.py、R1 新测试 |
| install_boundary | installer.py、state.py 安装字段、early_nodes.TypedInstallSubagent、安装测试；随后交接 bio_agents.install_subagent 区域 |
| helper_increment | skill_script_invocation.py、skill_resource_binding.py、helper 新测试及旧 delegate 用例 |

同一文件重叠区域只有一位写入者，交接以任务消息明确。所有提交由主任务管理。旧 dirty 保留，候选相对共同前置状态的增量单独导出，不能 git add 整个工作树。
