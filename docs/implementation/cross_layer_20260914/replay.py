"""Run only explicit offline groups from an exact candidate tree; never a session."""
import argparse
from pathlib import Path
import sys
REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(REPO/"tests"))
import export_cross_layer_implementation as exporter
from cli_model_progress_support import launch
GRAPH="tests/test_cross_layer_implementation_graph.py::test_production_combination_fix"
GROUPS={
 "core":["tests/test_cross_layer_domain.py","tests/test_cross_layer_correction.py","tests/test_cross_layer_publication.py","tests/test_cross_layer_delivery.py"],
 "AC":[GRAPH+"[A]",GRAPH+"[C]"],
 "B-success":[GRAPH+"[B_success]",GRAPH+"[B_last]"],
 "B-stop":[GRAPH+"[B_exhausted]",GRAPH+"[B_archive]"],
 "D":[GRAPH+"[D_gatk]",GRAPH+"[D_snpeff]"],
 "last-veto":[GRAPH+"[A_last]",GRAPH+"[D_gatk_veto]"],
 "names":["tests/test_plan_results_name.py"],
 "writer":["tests/test_runtime_recovery_reporting.py"],
 "preflight-recovery":["tests/test_preflight_types.py","tests/test_recovery_public_rejection.py"],
 "cli":["tests/test_cli_v2_display.py"],
 "approval-input":["tests/test_approval_owner_lifecycle.py","tests/test_input_boundary.py"],
 "planner":["tests/test_planner_boundary.py"],
 "protocol":["tests/test_cli_public_response.py::"+name for name in (
 "test_generate_unwrap_does_not_relax_existing_script_rejection",
 "test_generate_valid_json_cannot_override_negative_provider_integrity",
 "test_generate_duplicate_business_transport_is_rejected")],
 "transport":["tests/test_provider_attempt_reliability.py::ProviderAttemptReliabilityTests::"+name for name in (
 "test_a7_active_stream_still_hits_total_deadline","test_a8_timeout_cancellation_is_awaited_and_drained",
 "test_a11_partial_response_is_diagnostic_never_executable","test_a12_provider_error_taxonomy_remains_distinct",
 "test_a13_completed_malformed_response_is_not_transport_retry","test_p3_two_read_errors_exhaust_budget_without_action_or_execution")],
 "gates":["tests/"+x for x in (
 "test_semantic_completion.py::test_c04_true_predicate_does_not_override_bound_parser_concern",
 "test_semantic_completion.py::test_c07_cross_report_reviewer_decision_fails_closed",
 "test_semantic_completion.py::test_workspace_receipt_requires_runtime_producer_hash_and_scope",
 "test_semantic_completion_workflow.py::test_commit_independently_recomputes_and_honors_deterministic_veto",
 "test_semantic_completion_workflow.py::test_commit_rejects_verified_outcome_without_completion_report",
 "test_semantic_completion_workflow.py::test_c09_wrong_accept_cannot_hide_missing_runtime_mutation_receipt")],
}
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--tree",required=True)
 parser.add_argument("--root",type=Path,required=True,help="New directory under /tmp")
 parser.add_argument("--overlay",action="store_true",help="Explicit current protected overlay; hashes recorded separately")
 parser.add_argument("--group",choices=GROUPS,required=True)
 args=parser.parse_args();root=args.root.resolve()
 assert root.is_relative_to(Path("/tmp")) and root!=Path("/tmp") and not root.exists()
 # Only a tracked test from the same tree is added to the export allowlist.
 if args.group=="transport":exporter.base.TEST_FILES+=("tests/test_provider_attempt_reliability.py",)
 overlays=["prompts/manager.py","agent_core/node_context.py","agent_core/planner_context.py","agent_core/provider_message_safety.py","agent_core/workflow/managed_stop.py"] if args.overlay else []
 exporter.export(REPO,root/"export",args.tree,overlays)
 return launch(root/"export",root/"results",["-q",*GROUPS[args.group]],fatal_diagnostics=True,installed_cli=True)
if __name__=="__main__":raise SystemExit(main())
