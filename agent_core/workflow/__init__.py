"""Lazy access to retained workflow evidence types and profiling helpers.

Executable legacy graph adapters have been physically retired.  These exports
preserve saved outcome formats without loading a model or a second agent loop.
"""
from importlib import import_module

_EXPORTS = {'OUTCOME_SCHEMA_VERSION': ('agent_core.workflow.outcomes', 'OUTCOME_SCHEMA_VERSION'),
 'WORKFLOW_ROUTING_SCHEMA_LEGACY': ('agent_core.workflow.outcomes', 'WORKFLOW_ROUTING_SCHEMA_LEGACY'),
 'WORKFLOW_ROUTING_SCHEMA_TYPED': ('agent_core.workflow.outcomes', 'WORKFLOW_ROUTING_SCHEMA_TYPED'),
 'CommitStepOutcome': ('agent_core.workflow.outcomes', 'CommitStepOutcome'),
 'TriageOutcome': ('agent_core.workflow.outcomes', 'TriageOutcome'),
 'ConsultantOutcome': ('agent_core.workflow.outcomes', 'ConsultantOutcome'),
 'PlannerOutcome': ('agent_core.workflow.outcomes', 'PlannerOutcome'),
 'PlanReviewOutcome': ('agent_core.workflow.outcomes', 'PlanReviewOutcome'),
 'AskUserOutcome': ('agent_core.workflow.outcomes', 'AskUserOutcome'),
 'InstallSubagentOutcome': ('agent_core.workflow.outcomes', 'InstallSubagentOutcome'),
 'ResumeState': ('agent_core.workflow.outcomes', 'ResumeState'),
 'ExecuteOutcome': ('agent_core.workflow.outcomes', 'ExecuteOutcome'),
 'FinalAuditOutcome': ('agent_core.workflow.outcomes', 'FinalAuditOutcome'),
 'GenerateOutcome': ('agent_core.workflow.outcomes', 'GenerateOutcome'),
 'OutcomeContractError': ('agent_core.workflow.outcomes', 'OutcomeContractError'),
 'RecoveryOutcome': ('agent_core.workflow.outcomes', 'RecoveryOutcome'),
 'StrictOutcome': ('agent_core.workflow.outcomes', 'StrictOutcome'),
 'VerificationOutcome': ('agent_core.workflow.outcomes', 'VerificationOutcome'),
 'workflow_routing_schema_version': ('agent_core.workflow.outcomes', 'workflow_routing_schema_version'),
 'bind_workflow_routing_schema_for_task': ('agent_core.workflow.outcomes',
                                           'bind_workflow_routing_schema_for_task'),
 'route_blind_state_projection': ('agent_core.workflow.outcomes', 'route_blind_state_projection'),
 'profile_typed_node': ('agent_core.workflow.typed_node', 'profile_typed_node')}

__all__ = ['OUTCOME_SCHEMA_VERSION',
 'WORKFLOW_ROUTING_SCHEMA_LEGACY',
 'WORKFLOW_ROUTING_SCHEMA_TYPED',
 'CommitStepOutcome',
 'TriageOutcome',
 'ConsultantOutcome',
 'PlannerOutcome',
 'PlanReviewOutcome',
 'AskUserOutcome',
 'InstallSubagentOutcome',
 'ResumeState',
 'ExecuteOutcome',
 'FinalAuditOutcome',
 'GenerateOutcome',
 'OutcomeContractError',
 'RecoveryOutcome',
 'StrictOutcome',
 'VerificationOutcome',
 'profile_typed_node',
 'workflow_routing_schema_version',
 'bind_workflow_routing_schema_for_task',
 'route_blind_state_projection']

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol = _EXPORTS[name]
    value = getattr(import_module(module_name), symbol)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
