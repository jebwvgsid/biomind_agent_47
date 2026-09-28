from __future__ import annotations

import copy
import hashlib
import json
import unittest

from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder,
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.stage_contracts import FailurePacket, StageContractError
from tests.test_recovery_projection import _durable, _execute_failure


def _content_ref(prefix: str, value: object) -> str:
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _failure(incident_id: str, ordinal: int) -> dict:
    source = _execute_failure()
    fingerprint = f"failure-runtime-{ordinal}"
    source["payload"]["incident_id"] = incident_id
    source["payload"]["failure_fingerprint"] = fingerprint
    source["payload"]["execution_record"][
        "failure_fingerprint"
    ] = fingerprint
    return source


def _directive(directive_id: str, summary: str) -> dict:
    return {
        "schema_version": 1,
        "directive_id": directive_id,
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": summary,
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate only the failed action.",
        "evidence_refs": ["execution-1"],
    }


class _Store:
    def __init__(self) -> None:
        self.value = _durable()
        self.save_count = 0

    def load(self, _data_path: str, _task_id: str) -> dict:
        return copy.deepcopy(self.value)

    def save(self, _data_path: str, _task_id: str, candidate: dict) -> None:
        self.save_count += 1
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(
            candidate.get("state_generation") or 0
        ) + 1


class FailurePacketIncidentLineageTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_schema1_packet_keeps_its_original_wire_shape(self) -> None:
        current = build_causal_failure_record(
            "execute_outcome",
            _execute_failure(),
            _durable(),
            task_id="task-recovery",
        )
        legacy_packet = copy.deepcopy(current["failure_packet"])
        for field in (
            "incident_id", "packet_version", "supersedes_packet_ref",
        ):
            legacy_packet.pop(field, None)
        parsed = FailurePacket.from_mapping(legacy_packet)
        self.assertEqual(parsed.to_dict(), legacy_packet)
        self.assertEqual(parsed.incident_id, "")
        self.assertEqual(parsed.packet_version, 0)

        legacy_record = copy.deepcopy(current)
        for field in (
            "incident_id", "packet_version", "supersedes_packet_ref",
        ):
            legacy_record.pop(field, None)
        legacy_record["failure_packet"] = legacy_packet
        legacy_ref = _content_ref("failure-packet", legacy_packet)
        legacy_record["packet_ref"] = legacy_ref
        durable = _durable()
        durable["task_state"]["causal_failure_packets"] = {
            legacy_ref: legacy_record
        }
        durable["task_state"]["active_failure_packet_ref"] = legacy_ref
        projection = project_recovery_seed(
            {
                "task_id": "task-recovery",
                "active_failure_packet_ref": legacy_ref,
                "failure_packet": legacy_packet,
            },
            durable,
        )
        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual(projection.failure_packet.incident_id, "")

    def test_binder_versions_one_incident_and_links_exact_parent(self) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        first = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-shared", 1),
        )
        first_ref = first["active_failure_packet_ref"]
        self.assertEqual(first["failure_packet"]["packet_version"], 1)
        self.assertNotIn(
            "supersedes_packet_ref", first["failure_packet"]
        )

        second = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-shared", 2),
        )
        second_ref = second["active_failure_packet_ref"]
        self.assertNotEqual(first_ref, second_ref)
        self.assertEqual(second["failure_packet"]["packet_version"], 2)
        self.assertEqual(
            second["failure_packet"]["supersedes_packet_ref"], first_ref
        )
        task_state = store.value["task_state"]
        self.assertEqual(
            task_state["causal_failure_incident_heads"]["incident-shared"],
            second_ref,
        )
        self.assertEqual(len(task_state["causal_failure_packets"]), 2)

    def test_exact_head_replay_is_idempotent(self) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        source = _failure("incident-idempotent", 1)
        first = binder.bind(
            {"task_id": "task-recovery"}, "execute_outcome", source
        )
        second = binder.bind(
            {"task_id": "task-recovery"}, "execute_outcome", source
        )
        self.assertEqual(first, second)
        self.assertEqual(store.save_count, 1)

    def test_new_inspection_evidence_versions_an_unchanged_source(self) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        source = _failure("incident-new-inspection", 1)
        first = binder.bind(
            {
                "task_id": "task-recovery",
                "step_worker_outcome": {"archive_ref": "archive:first"},
            },
            "execute_outcome",
            source,
        )
        second = binder.bind(
            {
                "task_id": "task-recovery",
                "step_worker_outcome": {"archive_ref": "archive:second"},
            },
            "execute_outcome",
            source,
        )
        self.assertEqual(second["failure_packet"]["packet_version"], 2)
        self.assertEqual(
            second["failure_packet"]["supersedes_packet_ref"],
            first["active_failure_packet_ref"],
        )
        self.assertIn(
            "archive:second", second["failure_packet"]["inspection_refs"]
        )


    def test_new_active_incident_supersedes_prior_active_packet_out_of_band(
        self,
    ) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        first = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-active-a", 1),
        )
        first_ref = first["active_failure_packet_ref"]
        immutable_first = copy.deepcopy(
            store.value["task_state"]["causal_failure_packets"][first_ref]
        )
        second = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-active-b", 2),
        )
        second_ref = second["active_failure_packet_ref"]
        task_state = store.value["task_state"]
        self.assertEqual(
            task_state["causal_failure_packets"][first_ref], immutable_first
        )
        superseded = [
            item
            for item in task_state["causal_failure_packet_lifecycle"]
            if item["packet_ref"] == first_ref
            and item["transition"] == "superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(
            superseded[0]["reason"], "active_incident_replaced"
        )
        self.assertEqual(superseded[0]["related_ref"], second_ref)
        self.assertEqual(
            task_state["active_failure_packet_ref"], second_ref
        )

    def test_consumed_older_version_cannot_override_incident_head(self) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        first = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-stale", 1),
        )
        first_ref = first["active_failure_packet_ref"]
        binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-stale", 2),
        )
        store.value["task_state"]["active_failure_packet_ref"] = ""
        store.value["task_state"]["consumed_failure_packet_refs"] = {
            first_ref: {"decision_id": "old-decision"}
        }
        with self.assertRaisesRegex(
            StageContractError, "RECOVERY_CAUSAL_PACKET_STALE"
        ):
            project_recovery_seed(
                {
                    "task_id": "task-recovery",
                    "active_failure_packet_ref": first_ref,
                    "failure_packet": first["failure_packet"],
                },
                store.value,
            )

    def test_missing_lineage_parent_fails_closed(self) -> None:
        store = _Store()
        binder = CausalFailurePacketBinder(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        first = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-parent", 1),
        )
        second = binder.bind(
            {"task_id": "task-recovery"},
            "execute_outcome",
            _failure("incident-parent", 2),
        )
        del store.value["task_state"]["causal_failure_packets"][
            first["active_failure_packet_ref"]
        ]
        with self.assertRaisesRegex(
            StageContractError, "RECOVERY_CAUSAL_PACKET_PARENT_MISSING"
        ):
            project_recovery_seed(
                {
                    "task_id": "task-recovery",
                    **second,
                },
                store.value,
            )



if __name__ == "__main__":
    unittest.main()
