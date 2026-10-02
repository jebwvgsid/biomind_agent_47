"""Offline registry transactions: no provider/config imports or real environments."""
import copy
import importlib.abc
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NoConfiguration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "config" or fullname.startswith("config.") or fullname == "dotenv" or fullname.startswith("dotenv."):
            raise AssertionError(f"forbidden configuration import: {fullname}")
        return None


sys.meta_path.insert(0, NoConfiguration())
from package_manager import PackageManager, RegistryError


def concurrent_writer(path, barrier, index):
    manager = PackageManager.for_registry(path)
    # All processes open the same starting snapshot before anybody writes.
    barrier.wait(timeout=20)
    manager.register_environment("shared", packages=[f"package-{index}"], record={"extensions": {f"writer-{index}": index}})
    manager.register_environment(f"environment-{index}", prefix=f"/synthetic/env-{index}")


class AtomicRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="biomind-registry-a-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "software_registry.json"
        self.pm = PackageManager.for_registry(self.path)

    def read(self):
        return json.loads(self.path.read_text())

    def seed(self):
        self.pm.register_environment("rna", prefix="/synthetic/rna", env_runner="/synthetic/rna/bin/Rscript",
            status="verified", versions={"R": "4.5.3"}, evidence=[{"kind": "execution", "time": "old"}],
            record={"extension": {"keep": True}, "backend": "local", "scope": "analysis", "last_verified_at": "old"})

    def test_metadata_factory_no_initialization_or_probe(self):
        with patch("subprocess.run", side_effect=AssertionError("probe")), patch("shutil.which", side_effect=AssertionError("probe")):
            pm = PackageManager.for_registry(self.path)
            self.assertFalse(pm.registry_initialized)
            self.assertEqual(pm.registry, {"tools": {}, "environments": {}})
        self.assertFalse(self.path.parent.exists())

    def test_omitted_fields_and_unknown_fields_survive_idempotent_update(self):
        self.seed()
        initial = self.read()["environments"]["rna"]
        self.pm.register_environment("rna", capabilities=["ora"], evidence=[{"kind": "user_declaration"}])
        current = self.read()["environments"]["rna"]
        for field in ("prefix", "env_runner", "versions", "extension", "backend", "scope", "last_verified_at", "status"):
            self.assertEqual(current[field], initial[field])
        self.assertEqual(current["evidence"], [{"kind": "execution", "time": "old"}, {"kind": "user_declaration"}])
        self.assertEqual(current["capability_evidence"]["capability:ora"]["status"], "unknown")
        content = self.path.read_bytes()
        with patch("os.replace", side_effect=AssertionError("idempotent operation rewrote authority")):
            self.pm.register_environment("rna", capabilities=["ora"], evidence=[{"kind": "user_declaration"}])
        self.assertFalse(self.pm.last_registry_receipt["changed"])
        self.assertEqual(content, self.path.read_bytes())

    def test_two_stale_writers_merge_changes_not_snapshots(self):
        self.seed()
        first, second = PackageManager.for_registry(self.path), PackageManager.for_registry(self.path)
        first.register_environment("new", prefix="/synthetic/new")
        second.register_environment("rna", packages=["fgsea"])
        self.assertIn("new", self.read()["environments"])
        # The compatibility save path also merges independent nested changes.
        first, second = PackageManager.for_registry(self.path), PackageManager.for_registry(self.path)
        first.registry["environments"]["rna"]["extension"]["first"] = True
        second.registry["environments"]["rna"]["extension"]["second"] = True
        first._save_registry()
        second._save_registry()
        self.assertEqual(self.read()["environments"]["rna"]["extension"], {"keep": True, "first": True, "second": True})

    def test_real_process_writers_share_stable_lock(self):
        self.seed()
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)
        processes = [context.Process(target=concurrent_writer, args=(str(self.path), barrier, i)) for i in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        registry = self.read()
        self.assertEqual(set(registry["environments"]["shared"]["packages"]), {f"package-{i}" for i in range(4)})
        self.assertEqual(len(registry["environments"]["shared"]["extensions"]), 4)
        self.assertTrue(all(f"environment-{i}" in registry["environments"] for i in range(4)))

    def test_identity_conflicts_preserve_existing_record(self):
        self.seed()
        original = self.path.read_bytes()
        for field, value in (("prefix", "/wrong"), ("backend", "remote"), ("scope", "other"), ("path", "/wrong")):
            with self.subTest(field=field), self.assertRaisesRegex(RegistryError, "REGISTRY_IDENTITY_CONFLICT"):
                self.pm.register_environment("rna", record={field: value})
            self.assertEqual(original, self.path.read_bytes())

    def test_corrupt_schema_and_unreadable_never_become_empty(self):
        self.path.parent.mkdir(parents=True)
        for content, code in (("{broken", "REGISTRY_CORRUPT"), ('[]', 'REGISTRY_SCHEMA_INCOMPATIBLE'),
                              ('{"tools":[]}', 'REGISTRY_SCHEMA_INCOMPATIBLE'),
                              ('{"schema_version":99}', 'REGISTRY_SCHEMA_INCOMPATIBLE')):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(RegistryError, code):
                self.pm.register_environment("new")
            self.assertEqual(content, self.path.read_text())
        self.path.write_text('{"environments": {}, "tools": {}}')
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RegistryError, "REGISTRY_UNREADABLE"):
                self.pm.register_environment("new")

    def test_write_and_replace_faults_preserve_old_bytes_cleanup_temp(self):
        self.seed()
        original = self.path.read_bytes()
        faults = [patch("tempfile.NamedTemporaryFile", side_effect=OSError("write failed")),
                  patch("os.replace", side_effect=OSError("replace failed")),
                  patch("os.fsync", side_effect=OSError("flush failed"))]
        for fault in faults:
            with fault, self.assertRaises(OSError):
                self.pm.register_environment("rna", notes="new")
            self.assertEqual(original, self.path.read_bytes())
            self.assertFalse(list(self.path.parent.glob(".*.tmp")))

    def test_directory_flush_failure_reports_committed_fact(self):
        calls = 0
        original_fsync = os.fsync
        def failing_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory flush failed")
            return original_fsync(fd)
        with patch("os.fsync", side_effect=failing_directory):
            self.pm.register_environment("rna", prefix="/synthetic/rna")
        self.assertIn("rna", self.read()["environments"])
        self.assertTrue(self.pm.last_registry_receipt["committed"])
        self.assertIn("directory flush failed", self.pm.last_registry_receipt["durability_warning"])

    def test_bind_and_import_preserve_fields_use_authority(self):
        self.seed()
        stale = PackageManager.for_registry(self.path)
        self.pm.register_environment("other", prefix="/synthetic/other")
        stale.merge_registry_data({"tools": {"Rscript": {"env": "rna", "extension": "keep"}},
                                  "environments": {"rna": {"packages": ["fgsea"]}}, "producer_extension": {"x": 1}})
        stale.bind_tool_to_environment("Rscript", "rna")
        value = self.read()
        self.assertIn("other", value["environments"])
        self.assertEqual(value["tools"]["Rscript"]["extension"], "keep")
        self.assertEqual(value["producer_extension"], {"x": 1})
        self.assertEqual(value["environments"]["rna"]["prefix"], "/synthetic/rna")
        with self.assertRaisesRegex(RegistryError, "REGISTRY_IDENTITY_CONFLICT"):
            stale.bind_tool_to_environment("Rscript", "other")

    def test_legacy_partial_replacement_keeps_unspecified_fields(self):
        self.seed()
        self.pm.registry["environments"]["rna"] = {"env": "rna", "notes": "changed"}
        self.pm._save_registry()
        self.assertEqual(self.read()["environments"]["rna"]["prefix"], "/synthetic/rna")
        self.pm.register_environment("rna", remove_fields=("extension",))
        self.assertNotIn("extension", self.read()["environments"]["rna"])

    def test_ready_requires_launch_and_does_not_destroy_old_record(self):
        self.pm.register_environment("new", notes="user declaration")
        content = self.path.read_bytes()
        with self.assertRaisesRegex(RegistryError, "REGISTRY_LAUNCH_INCOMPLETE"):
            self.pm.register_environment("new", status="verified")
        self.assertEqual(content, self.path.read_bytes())

    def test_optional_shim_failure_commits_prefix_and_reports_derived_failure(self):
        with patch.object(self.pm, "_create_shim", side_effect=OSError("shim disk full")):
            self.pm.register_existing_tool("Rscript", "rna", prefix="/synthetic/rna")
        self.assertEqual(self.read()["environments"]["rna"]["prefix"], "/synthetic/rna")
        self.assertEqual(self.pm.last_registry_receipt["derived_errors"], ["shim disk full"])

    def test_required_shim_failure_preserves_previous_registry(self):
        self.seed()
        content = self.path.read_bytes()
        with patch.object(self.pm, "_create_shim", side_effect=OSError("shim failed")), self.assertRaises(OSError):
            self.pm.register_existing_tool("Rscript", "rna", setup_script="export NEEDED=yes")
        self.assertEqual(content, self.path.read_bytes())

    def test_changed_shim_never_overwrites_shared_old_file(self):
        self.pm.register_existing_tool("toy", "rna", prefix="/synthetic/rna", setup_script="export NEEDED=one")
        first = Path(self.read()["tools"]["toy"]["shim_path"])
        old_content = first.read_bytes()
        self.pm.register_existing_tool("toy", "rna", prefix="/synthetic/rna", setup_script="export NEEDED=two")
        second = Path(self.read()["tools"]["toy"]["shim_path"])
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), old_content)
        self.assertIn("export NEEDED=two", second.read_text())

    def test_register_new_environment_does_not_normalize_unrelated_rows(self):
        self.path.parent.mkdir(parents=True)
        minimal = {"opaque_extension": {"keep": [1, 2]}, "status": "verified"}
        initial = {"tools": {"oldtool": {"custom_field": True}}, "environments": {"old": minimal}, "custom_top": "keep"}
        self.path.write_text(json.dumps(initial))
        manager = PackageManager.for_registry(self.path)
        manager.register_environment("new", prefix="/synthetic/new")
        stored = self.read()
        self.assertEqual(stored["environments"]["old"], minimal)
        self.assertEqual(stored["tools"], initial["tools"])
        self.assertEqual(stored["custom_top"], "keep")

    def test_one_complete_record_requires_one_replace(self):
        original_replace = os.replace
        with patch("os.replace", wraps=original_replace) as replacement:
            self.seed()
        self.assertEqual(replacement.call_count, 1)
        fresh = PackageManager.for_registry(self.path)
        self.assertEqual(fresh.get_environment("rna")["prefix"], "/synthetic/rna")
        self.assertEqual(fresh.get_environment("rna")["status"], "verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
