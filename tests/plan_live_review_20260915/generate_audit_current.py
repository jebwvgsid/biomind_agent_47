@dataclass
class GenerateInvocationAudit:
    """Controller-owned metrics for one Generate invocation boundary."""

    prompt_assembly_started: bool = False
    prompt_assembly_succeeded: bool = False
    provider_call_attempted: bool = False
    llm_invoked: bool = False
    generate_call_id: str = field(default_factory=lambda: "generate-" + uuid.uuid4().hex)
    scope: Dict[str, Any] = field(default_factory=dict)
    source_ref: str = ""

    def freeze_scope(self, plan_state: Mapping[str, Any], step_id: str) -> None:
        scope = candidate_repair_scope(plan_state, task_id=str(plan_state.get("task_id") or ""), step_id=step_id)
        scope["attempt_id"] = ""  # Generate has no execution attempt of its own.
        if self.scope and self.scope != scope:
            raise ValueError("GENERATE_INVOCATION_SCOPE_CHANGED")
        self.scope = scope
        self.source_ref = str(plan_state.get("plan_snapshot_ref") or plan_state.get("approved_plan_hash") or "")

    def mark_provider_call_attempted(self) -> None:
        if not self.prompt_assembly_succeeded:
            raise RuntimeError("GENERATE_PROVIDER_CALL_BEFORE_PROMPT_SAFETY")
        self.provider_call_attempted = True
        self.llm_invoked = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generate_call_id": self.generate_call_id,
            "scope": deepcopy(self.scope),
            "source_ref": self.source_ref,
            "prompt_assembly_started": bool(self.prompt_assembly_started),
            "prompt_assembly_succeeded": bool(self.prompt_assembly_succeeded),
            "provider_call_attempted": bool(self.provider_call_attempted),
            "llm_invoked": bool(self.llm_invoked),
        }
