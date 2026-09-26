from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from taxonomy_lab.jsonio import load_evidence_items, load_evidence_protocol
from taxonomy_lab.numeric import group_indicator, summarize, wilson_interval


ROOT = Path(__file__).resolve().parents[1]


class NumericTests(unittest.TestCase):
    def test_summary_uses_sample_variance(self) -> None:
        summary = summarize([1, 2, 3, 4])
        self.assertEqual(summary.mean, Decimal("2.5"))
        self.assertEqual(summary.median, Decimal("2.5"))
        self.assertEqual(summary.sample_variance, Decimal(5) / Decimal(3))

    def test_single_value_has_no_variance(self) -> None:
        summary = summarize(["4.20"])
        self.assertEqual(summary.minimum, Decimal("4.20"))
        self.assertIsNone(summary.sample_variance)

    def test_wilson_interval_contains_observed_proportion(self) -> None:
        interval = wilson_interval(5, 6)
        self.assertLess(interval.lower, 5 / 6)
        self.assertGreater(interval.upper, 5 / 6)
        self.assertGreaterEqual(interval.lower, 0)
        self.assertLessEqual(interval.upper, 1)

    def test_group_indicator_uses_declared_strata(self) -> None:
        evidence_protocol = load_evidence_protocol(ROOT / "fixtures" / "demo_evidence_protocol.json")
        rows = load_evidence_items(ROOT / "fixtures" / "demo_evidence_items.jsonl", evidence_protocol)
        grouped = group_indicator(rows, "completion_seconds")
        self.assertEqual(grouped["morphology"].count, 3)
        self.assertEqual(grouped["cross-method"].count, 3)


if __name__ == "__main__":
    unittest.main()

