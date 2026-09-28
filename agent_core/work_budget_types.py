"""Durable work-budget exception identity; no model/tool loop."""


class ToolLoopLimit(RuntimeError):
    def __init__(self, reason, budget=None):
        self.reason, self.budget = reason, budget or {}
        super().__init__('TOOL_BUDGET_EXHAUSTED' if reason.endswith('_limit') else reason.upper())
