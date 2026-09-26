from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from taxonomy_lab.jsonio import JsonDataError, canonical_json, content_digest, load_evidence_items, load_evidence_protocol


ROOT = Path(__file__).resolve().parents[1]


class JsonIoTests(unittest.TestCase):
    def test_fixture_round_trip(self) -> None:
        evidence_protocol = load_evidence_protocol(ROOT / "fixtures" / "demo_evidence_protocol.json")
        rows = load_evidence_items(ROOT / "fixtures" / "demo_evidence_items.jsonl", evidence_protocol)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0].indicators["completion_seconds"], Decimal("42.8"))

    def test_canonical_json_orders_keys_and_preserves_decimal(self) -> None:
        self.assertEqual(canonical_json({"b": Decimal("2.50"), "a": 1}), '{"a":1,"b":"2.50"}')

    def test_digest_depends_on_order(self) -> None:
        self.assertNotEqual(content_digest([{"a": 1}, {"a": 2}]), content_digest([{"a": 2}, {"a": 1}]))

    def test_duplicate_source_identity_is_rejected(self) -> None:
        evidence_protocol = load_evidence_protocol(ROOT / "fixtures" / "demo_evidence_protocol.json")
        first = (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.jsonl"
            path.write_text(first + "\n" + first + "\n", encoding="utf-8")
            with self.assertRaisesRegex(JsonDataError, "来源身份重复"):
                load_evidence_items(path, evidence_protocol)


if __name__ == "__main__":
    unittest.main()

