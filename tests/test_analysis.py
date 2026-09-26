from __future__ import annotations

import unittest
from pathlib import Path

from taxonomy_lab.analysis import analyze, bootstrap_mean_interval
from taxonomy_lab.jsonio import load_evidence_items, load_evidence_protocol


ROOT = Path(__file__).resolve().parents[1]


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence_protocol = load_evidence_protocol(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = load_evidence_items(ROOT / "fixtures" / "demo_evidence_items.jsonl", self.evidence_protocol)

    def test_same_snapshot_is_deterministic(self) -> None:
        first = analyze(self.evidence_protocol, self.rows)
        second = analyze(self.evidence_protocol, self.rows)
        self.assertEqual(first, second)
        self.assertEqual(first["conclusion"], "pass")
        self.assertEqual(first["included_count"], 6)

    def test_missing_evidence_group_is_insufficient(self) -> None:
        rows = tuple(row for row in self.rows if row.evidence_group_key == "morphology")
        result = analyze(self.evidence_protocol, rows)
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["insufficient"][0]["evidence_group"], "cross-method")

    def test_bootstrap_seed_controls_result(self) -> None:
        values = [row.indicators["completion_seconds"] for row in self.rows]
        self.assertEqual(
            bootstrap_mean_interval(values, seed=42, samples=200),
            bootstrap_mean_interval(values, seed=42, samples=200),
        )


if __name__ == "__main__":
    unittest.main()
