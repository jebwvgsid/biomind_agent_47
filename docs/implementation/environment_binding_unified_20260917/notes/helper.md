# R3 / D1 helper 增量

本切片复用已保存的未提交 helper 前置。生产修改仅在 `agent_core/skill_resource_binding.py` 和 `agent_core/skill_script_invocation.py`；Generate 的目录投影与错误传递由主任务在 `bio_agents.py` 统一接线。

现有 `skill_resource_refs` 的一个 root handle 即可选择 helper。binder 仅沿已选 snapshot 的显式 delegates 作确定性深度展开，保留旧显式列表顺序并去重；新增依赖追加到列表。每次绑定内部按 handle 复用 receipt，并按 snapshot 复用已核对的 SKILL 文本；没有跨候选缓存。缺失/越界/循环/过大闭包在批准前拒绝。未声明 import 不参与依赖发现。历史显式完整引用继续通过，错误显式引用仍拒绝。

完整 receipt、binding_digest、内容 hash、解释器 launch path 与 stat 身份仍进入批准，并在 Execute 前重读。venv symlink 启动入口保持不变。原始 action/source/argv 不改写，不预建 helper 的独占 leaf，不改变 `binding_env` 的已有可省略行为。

`project_skill_script_catalog` 是模型投影白名单：handle、可用状态、canonical/env 入口、单一 shell prefix、业务 arguments、carrier、明确依赖句柄、约束及 snapshot/resource 证据。内部原始 invocation、重复 argv prefix 和 interpreter stat 不进入默认投影。父层须用这个函数替换原来的 raw invocation deepcopy，并同步调用说明；实际请求的同口径大小、最终 Skill revision 的按需读取由累计集成验收确认，不能由本局部测试推断 token 或性能收益。

helper 异常的 `diagnostic` 保留真实 validator、helper、实际 carrier/字段、actual/expected 和证据。UNVERIFIABLE 说明未覆盖的形式及受支持选择；父层透传进既有 Generate 局部 candidate correction。没有新 Recovery 或通用 shell 解析。

`build_skill_script_argv` 现在被生产 `bind_action_skill_resources → validate_skill_script_invocations` 的 direct argv 分支调用，用来构造 expected argv 并核对原载体。现有 public schema 仍要求模型给出 argv；本轮没有宣称实现新的“仅参数输入自动生成动作”接口。带延迟 evidence 变量的 helper 仍使用受控 simple shell。

新进程离线局部回归：76 passed（新 root-selection 18 项、既有 invocation 40 项、resource binding 18 项），测试期间源码不变，四个改动文件与工作树 hash 一致。明细见 `evidence/helper_results.json` 与 `evidence/helper_pytest_results.json`。测试覆盖 diamond 闭包、legacy 顺序/去重、循环/缺失/越界、child/Skill/解释器 stat 变更、错误证据、白名单、direct argv 的生产 builder 调用、延迟变量和完整 helper/runtime 批准身份。

这不是累计生产 graph / 模型 / 科学 E2E 结果。后续主任务需要用最终候选复验 Generate 局部纠正 → 批准 → Execute，确保 root 闭包与实际 runtime 一同进入批准身份，并以最终 WGS Skill 验收科学 Commit 与真实请求。
