from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.errors import Conflict, Forbidden, InvalidState
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat-b", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
            ("contractor", "statistician"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.task_family = self.evidence_protocol["task_family"]
        self.service.grant_task_family("approver", "stat", self.task_family)
        self.service.grant_task_family("approver", "stat-b", self.task_family)
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.evidence_protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def test_complete_workflow(self) -> None:
        imported = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", 30)
        analysis = self.service.complete_job("stat", job["job_id"], job["lease_fence_token"])
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["batch"]["state"], "decided")
        self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")

    def test_idempotent_replay_and_conflict(self) -> None:
        first = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        second = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in self.rows]
        changed[0] = dict(changed[0])
        changed[0]["indicators"] = dict(changed[0]["indicators"])
        changed[0]["indicators"]["completion_seconds"] = "99"
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 6)

    def test_import_rolls_back_when_one_source_row_duplicates(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows[:1])
        with self.assertRaises(Conflict):
            self.service.import_evidence_items("operator", "batch-a", "key-2", self.rows[:2])
        count = self.connection.execute("SELECT count(*) FROM evidence_items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.seal_batch("operator", "batch-a", 2)
        with self.assertRaises(Forbidden):
            self.service.report("operator", "batch-a")

    def test_exclusion_review_and_revoke_leave_history(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "现场记录失效")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "观察材料充分")
        self.assertEqual(reviewed["status"], "approved")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='evidence_item' AND entity_id=? ORDER BY event_id",
            (str(evidence_item_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["exclusion.requested", "exclusion.revoked"])

    def test_failed_job_returns_to_queue_after_delay(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", 10)
        failed = self.service.fail_job(
            "stat", job["job_id"], "临时计算失败", retry_seconds=5, fence_token=job["lease_fence_token"]
        )
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("stat-b", 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("stat-b", 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("stat", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat-b", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "stat-b")
        self.assertNotEqual(second["lease_fence_token"], first["lease_fence_token"])
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", first["job_id"], first["lease_fence_token"])

    def test_deactivated_account_cannot_claim_or_keep_lease(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        self.service.set_user_active("approver", "stat-b", False)
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat-b", 10)
        job = self.service.claim_job("stat", 10)
        self.service.set_user_active("approver", "stat", False)
        with self.assertRaises(Forbidden):
            self.service.complete_job("stat", job["job_id"], job["lease_fence_token"])
        self.clock.advance(seconds=11)
        self.service.set_user_active("approver", "stat-b", True)
        takeover = self.service.claim_job("stat-b", 10)
        self.assertEqual(takeover["job_id"], job["job_id"])
        # 停用的旧持有者迟到完成，不得覆盖新持有者。
        with self.assertRaises(Forbidden):
            self.service.complete_job("stat", job["job_id"], job["lease_fence_token"])
        reject_events = self.connection.execute(
            "SELECT reason FROM lease_events WHERE event_type='job.lease_rejected' ORDER BY event_id"
        ).fetchall()
        self.assertTrue(any("account_inactive" in row[0] for row in reject_events))

    def test_account_without_task_family_grant_cannot_claim(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        # 未授予任务适用范围：队列中没有可领取的任务，并记录拒绝原因。
        self.assertIsNone(self.service.claim_job("contractor", 10))
        rejection = self.connection.execute(
            "SELECT reason FROM lease_events WHERE event_type='job.lease_rejected' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("task_family_not_granted", rejection[0])
        # 有资格的统计人员仍然能领到同一个任务。
        job = self.service.claim_job("stat", 10)
        self.assertIsNotNone(job)

    def test_claim_replay_returns_stable_state(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("stat", 10, idempotency_key="claim-1")
        self.service.fail_job(
            "stat", first["job_id"], "重新计算", retry_seconds=0, fence_token=first["lease_fence_token"]
        )
        replayed = self.service.claim_job("stat", 10, idempotency_key="claim-1")
        self.assertEqual(replayed, first)
        with self.assertRaises(Conflict):
            self.service.claim_job("stat", 20, idempotency_key="claim-1")

    def test_explicit_takeover_and_stale_fence_are_rejected(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", 10)
        with self.assertRaises(InvalidState):
            self.service.takeover_job("stat-b", job["job_id"], 10)
        self.clock.advance(seconds=11)
        taken = self.service.takeover_job("stat-b", job["job_id"], 10)
        self.assertEqual(taken["taken_over_from"], "stat")
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", job["job_id"], job["lease_fence_token"])
        events = [
            row[0]
            for row in self.connection.execute(
                "SELECT event_type FROM lease_events WHERE job_id=? ORDER BY event_id", (job["job_id"],)
            ).fetchall()
        ]
        self.assertEqual(
            events,
            ["job.claimed", "job.lease_rejected", "job.taken_over", "job.lease_rejected"],
        )
        reasons = [
            row[0]
            for row in self.connection.execute(
                "SELECT reason FROM lease_events WHERE job_id=? ORDER BY event_id", (job["job_id"],)
            ).fetchall()
        ]
        self.assertIn("lease_active", reasons[1])
        self.assertIn("stale_lease_holder", reasons[3])


if __name__ == "__main__":
    unittest.main()
