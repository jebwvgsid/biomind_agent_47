import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

from agent_core.plan_tracker import (
    mark_renderer_action_completed,
    next_renderer_manifest_action,
    normalize_renderer_manifest,
    register_renderer_manifest,
    renderer_manifest_complete,
    resolve_renderer_action_script,
    validate_renderer_manifest_provenance,
)


GATK_SCRIPT = Path(__file__).resolve().parents[1] / "skills/genomics/wgs-variant-calling/scripts/render_gatk_wgs_scripts.py"
spec = importlib.util.spec_from_file_location("gatk_renderer", GATK_SCRIPT)
gatk_renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gatk_renderer
spec.loader.exec_module(gatk_renderer)


class RendererManifestTests(unittest.TestCase):
    def _manifest(self, root: Path):
        for name in ("00.preflight.sh", "01.run.sh"):
            path = root / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        return {
            "schema_version": 3,
            "manifest_type": "renderer_artifact_contract",
            "outdir": str(root),
            "renderer": {"id": "test.renderer", "version": 3},
            "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
            "scripts": ["00.preflight.sh", "01.run.sh"],
            "actions": [
                {"id": "preflight", "script": "00.preflight.sh", "kind": "verification", "depends_on": []},
                {"id": "run", "script": "01.run.sh", "kind": "primary", "depends_on": ["preflight"]},
            ],
            "artifacts": [],
        }

    def test_manifest_drives_exact_script_and_resume_cursor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._manifest(root)
            state = {"steps": [{"step_id": "step-1", "status": "running"}]}
            normalized = register_renderer_manifest(
                state,
                "step-1",
                manifest,
                manifest_path=root / "manifest.json",
                task_id="task-a",
                producer_action_id="render-a",
            )
            execution = state["steps"][0]["renderer_execution"]
            first = next_renderer_manifest_action(normalized, execution)
            self.assertEqual(first["id"], "preflight")
            self.assertEqual(resolve_renderer_action_script(normalized, first).name, "00.preflight.sh")
            mark_renderer_action_completed(state, "step-1", "preflight", {"execution_id": "exec-1"})
            second = next_renderer_manifest_action(normalized, execution)
            self.assertEqual(second["id"], "run")
            mark_renderer_action_completed(state, "step-1", "run", {"execution_id": "exec-2"})
            self.assertTrue(renderer_manifest_complete(normalized, execution))

    def test_requested_alias_not_in_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = normalize_renderer_manifest(self._manifest(Path(tmpdir)))
            with self.assertRaisesRegex(ValueError, "not declared"):
                resolve_renderer_action_script(manifest, {"id": "01.build_db.sh"})

    def test_provenance_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = normalize_renderer_manifest(self._manifest(Path(tmpdir)))
            result = validate_renderer_manifest_provenance(
                manifest,
                task_id="different-task",
                step_id="step-1",
                producer_action_id="render-a",
            )
            self.assertFalse(result["valid"])
            self.assertIn("task_id", result["mismatches"])

    def test_legacy_scripts_are_adapted_in_order(self):
        manifest = normalize_renderer_manifest({
            "schema_version": 2,
            "scripts": ["00.preflight.sh", "step1.call.sh"],
            "outdir": "/tmp/rendered",
        })
        self.assertTrue(manifest["legacy_adapted"])
        self.assertEqual(manifest["source_schema_version"], 2)
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["actions"][0]["kind"], "verification")
        self.assertEqual(manifest["actions"][1]["depends_on"], [manifest["actions"][0]["id"]])

    def test_gatk_requested_conditional_action_becomes_required(self):
        args = SimpleNamespace(
            annotation_gff="genes.gff3",
            annotation_fasta="ref.fa",
            annovar_buildver="custom",
        )
        actions = gatk_renderer.action_manifest(
            args,
            {"step8.ANNOVAR_annotation.sh": ["true"]},
            [{
                "artifact_id": "gatk_annovar_output",
                "producer": "step8.ANNOVAR_annotation.sh",
            }],
        )

        self.assertTrue(actions[0]["enabled"])
        self.assertTrue(actions[0]["required"])

    def test_gatk_renderer_emits_same_v3_action_protocol(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bam = root / "sample.bam"
            bam.write_text("bam\n", encoding="utf-8")
            samples = root / "samples.tsv"
            samples.write_text(f"sample\t{bam}\n", encoding="utf-8")
            intervals = root / "intervals.tsv"
            intervals.write_text("interval_id\tgatk_interval\nchr1\tchr1\n", encoding="utf-8")
            ref = root / "ref.fa"
            ref.write_text(">chr1\nACGT\n", encoding="utf-8")
            outdir = root / "rendered"
            argv = [
                "render_gatk_wgs_scripts.py",
                "--sample-bam-list", str(samples),
                "--sample-ploidy", "2",
                "--task-label", "task-g", "--producer-label", "render-g",
                "--intervals-tsv", str(intervals),
                "--ref", str(ref),
                "--outdir", str(outdir),
                "--producer-step-id", "step-4",
            ]
            with patch.object(sys, "argv", argv), patch.dict(
                "os.environ", {"BIOMIND_TASK_ID": "task-g", "BIOMIND_ACTION_ID": "render-g"}
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(gatk_renderer.main(), 0)
            manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["renderer"]["id"], "genomics.gatk_wgs")
        self.assertTrue(manifest["actions"])
        self.assertIn("requirements", manifest)
        self.assertIn("tool_metadata", manifest)


if __name__ == "__main__":
    unittest.main()
