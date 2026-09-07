"""METHOD-04: exercise the real Skill/YAML loader, without any runtime probe."""
from pathlib import Path

from agent_core.skill_manager import SkillManager


def test_contig_guidance_keeps_metadata_and_method_authority():
    root = Path(__file__).resolve().parents[1]
    manager = SkillManager(str(root / "skills"))
    meta = manager.get_skill_meta("genomics/snpeff-annotation")
    capabilities = meta["generate"]["execution_capabilities"]
    contig = next(item for item in capabilities if item["capability_id"].endswith("check_contig_compatibility"))
    guidance = contig["execution_guidance"]
    metadata_rule = next(item for item in guidance if "##contig" in item)
    assert metadata_rule.endswith("assembly is not part of the contig ID.")
    text = "\n".join(guidance)
    assert "typed boolean claim" in text
    assert "independent structured parser belongs to the verification layer" in text
    assert "specific approved requirement, execution record, and independent parser receipt" in text
    assert "unless the approved contract explicitly requires it" in text
    assert contig["required_runtime_binding_ids"] == []
    for operation in ("build_database", "annotate_variants"):
        contract = next(item for item in capabilities if item["capability_id"].endswith(operation))
        assert set(contract["required_runtime_binding_ids"]) == {"runtime.java.bin", "runtime.snpeff.jar"}
