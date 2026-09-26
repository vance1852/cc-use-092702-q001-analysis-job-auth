from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from taxonomy_lab.contracts import EvidenceItem, EvidenceProtocol, ValidationError
from taxonomy_lab.jsonio import load_json


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.evidence_protocol = EvidenceProtocol.from_dict(self.raw_evidence_protocol)

    def test_evidence_protocol_builds_indexes(self) -> None:
        self.assertEqual(self.evidence_protocol.version, 1)
        self.assertEqual(self.evidence_protocol.evidence_group_keys, {"morphology", "cross-method"})
        self.assertEqual(set(self.evidence_protocol.indicator_map), {"completed", "completion_seconds", "interventions"})

    def test_evidence_protocol_rejects_duplicate_indicator(self) -> None:
        raw = deepcopy(self.raw_evidence_protocol)
        raw["indicators"].append(deepcopy(raw["indicators"][0]))
        with self.assertRaisesRegex(ValidationError, "不能重复"):
            EvidenceProtocol.from_dict(raw)

    def test_evidence_item_rejects_unknown_evidence_group(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "device_id": "r1",
            "evidence_protocol_id": self.evidence_protocol.evidence_protocol_id,
            "evidence_protocol_version": self.evidence_protocol.version,
            "evidence_group_key": "unknown",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "indicators": {"completed": 1, "completion_seconds": 4, "interventions": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "未在协议中声明"):
            EvidenceItem.from_dict(raw, self.evidence_protocol)

    def test_binary_indicator_is_strict(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "device_id": "r1",
            "evidence_protocol_id": self.evidence_protocol.evidence_protocol_id,
            "evidence_protocol_version": self.evidence_protocol.version,
            "evidence_group_key": "morphology",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "indicators": {"completed": 2, "completion_seconds": 4, "interventions": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "必须是 0 或 1"):
            EvidenceItem.from_dict(raw, self.evidence_protocol)


if __name__ == "__main__":
    unittest.main()

