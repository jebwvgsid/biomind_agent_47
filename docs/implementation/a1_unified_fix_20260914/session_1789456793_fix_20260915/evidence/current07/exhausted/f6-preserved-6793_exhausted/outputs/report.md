# BioMind managed run stop

Stop identity: managed-stop:7b7053477604f3460935ad0f06bfd365f260036c1eef528491d3ca2517d25bc3

自动检查已停止，当前问题尚未解决。

原任务：Run a six-operation synthetic snpEff framework mechanism: check contigs, prepare an isolated database layout, build via the explicitly authorized synthetic algorithm executable, annotate synthetic ANN records, parse summaries, and deliver all required physical outputs. These are synthetic protocol fixtures, never real scientific snpEff results. Genome version new_fixture_v2; readonly inputs: /tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/输入 新构造/两样本.vcf, /tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/输入 新构造/新参考.fa, /tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/输入 新构造/新注释.gtf; required outputs: annotated_vcf=/tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/Part2.vairant_annotation/新机制结果/annotated.vcf, ann_summary=/tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/Part2.vairant_annotation/新机制结果/ann_summary.tsv, report=/tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/Part2.vairant_annotation/新机制结果/report.md, reproduce_script=/tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/Part2.vairant_annotation/新机制结果/reproduce.sh, stage_status=/tmp/biomind-6793-fix-20260915/current07/exhausted/scratch/biomind-plan-local-graph-mpxwgw2b/中文 两样本/Part2.vairant_annotation/新机制结果/stage_status.tsv。

当前步骤：check_contig_compatibility；执行 attempt：。

没有可确认的命令执行记录。

确定性检查：尚无足够事实确认通过；步骤正式提交：未提交。

原始起因（断言待核实）：execute.env must be an object of string values.。

本次没有已记录的工具读取错误；策略仍未形成可接纳的解决决定。

停止原因：GENERATE_REPAIR_BUDGET_EXHAUSTED；连续无进展轮次=0，模型轮次=0，工具调用=0。

读取失败或预算结束不能证明科学依赖不可用、数据不兼容或科学目标已经完成。

未完成阶段：check_contig_compatibility=failed；prepare_database=pending；build_database=pending；annotate_variants=pending；summarize_ann=pending；finalize_outputs=pending。

尚无本次后续科学结果的完成证明；生成状态报告不等于科学产物通过验收。

本次尚未确认生成报告文件。

文件交付：not_attempted（截至本次说明生成时；冻结正文不回写，最终交付以持久 receipt 为准）；report.md=not_attempted ；stage_status.tsv=not_attempted ；reproduce.sh=not_attempted 

来源阶段：generate；原始错误：ACTION_SCHEMA_TYPE_ERROR；停止阶段：generate。

末次候选：{"attempt_id": "", "execution_started": false, "approved": false, "candidate_fingerprint": "17c11281135f9d286cf52d13115c71360a50ccbf647ac57d8e21a956f5933ec3", "candidate_event_id": "candidate-call:ea7d4197429415451754f13dc159e6d407c791ee900d8fd51c147c5b21d61613", "cause_code": "ACTION_OPERATION_SCOPE_MISMATCH", "correction_category": "candidate_contract"}

检查计数范围：{"local_tool_calls": 0, "known_causal_errors": 0, "history_coverage": "bounded_admitted_causal_chain"}

末次候选错误：ACTION_OPERATION_SCOPE_MISMATCH；The action operation is not authorized by the selected approved capability.

停止策略：corrective_generate_budget_exhausted；修正记录：{"version": "candidate-corrections-v2", "scope": {"task_id": "fix-f6-6793_exhausted", "task_revision": 1, "plan_id": "plan_b6ccc350d4f9e9be2b49", "plan_version": "plan_v1", "plan_revision": 1, "step_id": "check_contig_compatibility"}, "limit": 4, "source": "task_state.budgets.max_candidate_repairs_per_epoch", "unit": "additional_candidate_calls", "categories_share_total": true, "rejection_ordinal": 5, "corrections_authorized_before": 4, "corrections_observed_before_rejection": 4, "remaining_before_authorization": 0, "category": "candidate_contract", "progress_assessment": {"schema_version": 1, "producer": "biomind.generate_candidate_admission.v1", "progress_proven": true, "basis": "validated_representation_target", "comparison_fingerprint": "17c11281135f9d286cf52d13115c71360a50ccbf647ac57d8e21a956f5933ec3", "validation_signature": "91ede59be172393eecdf8ac0301913859e98534ac367e547a341a219f234fe24", "comparison_context_fingerprint": "8d92f69ae4d656caadecdc302493ec06619bbce2a7f31d6bf08a068a266c6436", "representation_targets": ["action_schema_complete", "action_type", "effect_helper_materialization", "executable_syntax", "execute.argv_shape", "execute.env_keys", "execute.env_object", "execute.env_values", "execute.executable_body", "execute.outputs_shape", "json_object"], "binding_facts": ["binding_env"], "binding_axis": "evaluated", "newly_satisfied_targets": ["executable_syntax"], "resolved_targets": ["$.code"], "pending_targets": ["operation"], "scientific_success": "not_assessed"}, "correction_authorized": false, "corrections_authorized": 4, "remaining": 0, "repair_ordinal": 5, "repair_limit": 4, "event_id": "candidate-rejection:3eef37893b4afb588e869c93", "correction_category": "candidate_contract"}

AI 辅助说明（不改变上述事实）：
已记录 origin：{"attempt_id": "", "concerns": [], "error_code": "ACTION_SCHEMA_TYPE_ERROR", "evidence_refs": [], "failure_id": "", "failure_type": "generated_action_contract_error", "process_fact_scope": "original causal execution; no scientific validity inferred", "returncode": null, "source_owner": "GENERATED_ACTION", "source_stage": "generate", "summary": "execute.env must be an object of string values."}
已记录 stop_reason：{"budget_receipt": {"categories_share_total": true, "category": "candidate_contract", "correction_authorized": false, "correction_category": "candidate_contract", "corrections_authorized": 4, "corrections_authorized_before": 4, "corrections_observed_before_rejection": 4, "event_id": "candidate-rejection:3eef37893b4afb588e869c93", "limit": 4, "progress_assessment": {"basis": "validated_representation_target", "binding_axis": "evaluated", "binding_facts": ["binding_env"], "comparison_context_fingerprint": "8d92f69ae4d656caadecdc302493ec06619bbce2a7f31d6bf08a068a266c6436", "comparison_fingerprint": "17c11281135f9d286cf52d13115c71360a50ccbf647ac57d8e21a956f5933ec3", "newly_satisfied_targets": ["executable_syntax"], "pending_targets": ["operation"], "producer": "biomind.generate_candidate_admission.v1", "progress_proven": true, "representation_targets": ["action_schema_complete", "action_type", "effect_helper_materialization", "executable_syntax", "execute.argv_shape", "execute.env_keys", "execute.env_object", "execute.env_values", "execute.executable_body", "execute.outputs_shape", "json_object"], "resolved_targets": ["$.code"], "schema_version": 1, "scientific_success": "not_assessed", "validation_signature": "91ede59be172393eecdf8ac0301913859e98534ac367e547a341a219f234fe24"}, "rejection_ordinal": 5, "remaining": 0, "remaining_before_authorization": 0, "repair_limit": 4, "repair_ordinal": 5, "scope": {"plan_id": "plan_b6ccc350d4f9e9be2b49", "plan_revision": 1, "plan_version": "plan_v1", "step_id": "check_contig_compatibility", "task_id": "fix-f6-6793_exhausted", "task_revision": 1}, "source": "task_state.budgets.max_candidate_repairs_per_epoch", "unit": "additional_candidate_calls", "version": "candidate-corrections-v2"}, "detail": "Generate encountered an internal protoco

任务状态：FAILED；自动执行已停止。

停止总结生成状态：COMPLETED（仅表示总结处理状态，不代表任务成功）
