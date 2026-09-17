# SnpEff 现役契约夹具迁移

只调整 `tests/test_action_scoped_runtime_bindings.py` 中两项 SnpEff 专属测试；`tests/test_plan_contract_approval_scope.py` 保持原样。

现役 Skill 提供 `genomics.snpeff_annotation.inspect` 和 `.execute`，两者 Java、SnpEff JAR 均为显式 optional runtime，required 为空。旧 `check_contig_compatibility` / `build_database` 名称已退出契约，测试不再要求它们存在。未知与缺失 capability 仍分别拒绝；没有补造 required IDs，也没有把 optional 路径自动升级为正式执行证据。

新断言通过真实 resolver 验证 required 为空时空 runtime registry 可返回空 bindings；另以合法合成契约把 Java 从 optional 移至 required，保持旧批准 hash，验证 `RUNTIME_BINDING_SNAPSHOT_DRIFT`。原 Skill 契约在测试中未变。

隔离新进程运行原 action runtime 与 approval scope 两组，**21 passed**。见 `evidence/approval_fixture_results.json` 及逐项 pytest 回执。
