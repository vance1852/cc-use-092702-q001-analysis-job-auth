from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.errors import Conflict, Forbidden, InvalidState
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService
from taxonomy_lab.storage import connect


ROOT = Path(__file__).resolve().parents[1]
TASK_FAMILY = "insect-taxonomy"


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat2", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
            ("admin", "admin"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.evidence_protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.evidence_protocol)
        # 只有被授予该任务适用范围的统计人员才能领取分析任务。
        self.service.grant_qualification("admin", "stat", TASK_FAMILY)
        self.service.grant_qualification("admin", "stat2", TASK_FAMILY)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def _sealed_job(self) -> dict:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        return self.service.claim_job("stat", 30)

    def test_complete_workflow(self) -> None:
        imported = self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("stat", 30)
        self.assertEqual(job["task_family"], TASK_FAMILY)
        self.assertEqual(job["claimed_by"], "stat")
        analysis = self.service.complete_job("stat", job["job_id"])
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
        failed = self.service.fail_job("stat", job["job_id"], "临时计算失败", retry_seconds=5)
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("stat", 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("stat2", 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("stat", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("stat2", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "stat2")
        self.assertEqual(second["lease_event"], "taken_over")
        # 旧持有者的迟到完成必须被拒绝。
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", first["job_id"])
        # 新持有者可以正常完成。
        analysis = self.service.complete_job("stat2", second["job_id"])
        self.assertEqual(analysis["result"]["conclusion"], "pass")


class LeaseSecurityTests(unittest.TestCase):
    """针对停用外协账号占住租约、越权领取等问题的回归测试。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat2", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
            ("admin", "admin"),
            ("ext", "statistician"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", self.protocol)
        self.service.grant_qualification("admin", "stat", TASK_FAMILY)
        self.service.grant_qualification("admin", "stat2", TASK_FAMILY)
        self.service.grant_qualification("admin", "ext", TASK_FAMILY)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)

    def tearDown(self) -> None:
        self.connection.close()

    def test_claim_requires_known_active_statistician(self) -> None:
        from taxonomy_lab.errors import NotFound, ValidationFailed
        with self.assertRaises(NotFound):
            self.service.claim_job("ghost", 30)
        with self.assertRaises(Forbidden):
            self.service.claim_job("operator", 30)
        with self.assertRaises(ValidationFailed):
            self.service.claim_job("stat", 0)

    def test_unqualified_statistician_cannot_claim(self) -> None:
        self.service.create_user("stat3", "无资格统计", "statistician")
        # 轮询领取时看不到自己任务适用范围之外的任务。
        self.assertIsNone(self.service.claim_job("stat3", 30))
        job_id = self.connection.execute("SELECT job_id FROM analysis_jobs").fetchone()[0]
        # 指定领取时明确拒绝并留痕。
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat3", 30, job_id=job_id)
        timeline = self.service.job_timeline("auditor", job_id)
        self.assertEqual(timeline["events"][0]["event_type"], "rejected")
        self.assertEqual(timeline["events"][0]["reason_code"], "not_qualified_for_task_family")
        self.assertEqual(timeline["events"][0]["actor_id"], "stat3")

    def test_deactivated_account_cannot_hold_or_finish_lease(self) -> None:
        job = self.service.claim_job("ext", 30)
        self.service.set_user_active("admin", "ext", False, "外协合同到期，停用账号")
        # 停用后领取、续作、完成、失败全部被账号状态拦截。
        with self.assertRaises(Forbidden):
            self.service.claim_job("ext", 30)
        with self.assertRaises(Forbidden):
            self.service.renew_job("ext", job["job_id"], 30)
        with self.assertRaises(Forbidden):
            self.service.complete_job("ext", job["job_id"])
        with self.assertRaises(Forbidden):
            self.service.fail_job("ext", job["job_id"], "崩溃")
        # 租约到期后由合格人员接管；即使账号重新启用，旧持有者也无法再写结论。
        self.clock.advance(seconds=31)
        takeover = self.service.claim_job("stat", 30)
        self.assertEqual(takeover["lease_event"], "taken_over")
        self.service.set_user_active("admin", "ext", True, "外协账号重新启用")
        with self.assertRaises(InvalidState):
            self.service.complete_job("ext", job["job_id"])
        analysis = self.service.complete_job("stat", job["job_id"])
        self.assertEqual(analysis["result"]["conclusion"], "pass")

    def test_qualification_revocation_blocks_further_operations(self) -> None:
        job = self.service.claim_job("ext", 30)
        self.service.revoke_qualification("admin", "ext", TASK_FAMILY, "外协不再承担该调查族")
        with self.assertRaises(Forbidden):
            self.service.complete_job("ext", job["job_id"])
        with self.assertRaises(Forbidden):
            self.service.renew_job("ext", job["job_id"], 30)

    def test_claim_replay_returns_stable_state(self) -> None:
        first = self.service.claim_job("stat", 30, request_key="claim-1")
        second = self.service.claim_job("stat", 30, request_key="claim-1")
        self.assertEqual(first, second)
        self.assertEqual(second["sequence_no"], first["sequence_no"])
        # 同键不同内容视为冲突。
        with self.assertRaises(Conflict):
            self.service.claim_job("stat", 60, request_key="claim-1")

    def test_renew_extends_lease_and_blocks_takeover(self) -> None:
        job = self.service.claim_job("stat", 10)
        self.clock.advance(seconds=8)
        renewed = self.service.renew_job("stat", job["job_id"], 30)
        self.assertEqual(renewed["lease_event"], "renewed")
        self.clock.advance(seconds=9)
        # 续作后旧到期时间已失效，尚未到期不能接管。
        with self.assertRaises(Conflict):
            self.service.claim_job("stat2", 30, job_id=job["job_id"])

    def test_release_requires_reason_and_returns_job_to_queue(self) -> None:
        job = self.service.claim_job("stat", 30)
        from taxonomy_lab.errors import ValidationFailed
        with self.assertRaises(ValidationFailed):
            self.service.release_job("stat", job["job_id"], "  ")
        released = self.service.release_job("stat", job["job_id"], "发现样线数据需要补录")
        self.assertEqual(released["state"], "queued")
        next_claim = self.service.claim_job("stat2", 30, job_id=job["job_id"])
        self.assertEqual(next_claim["lease_event"], "acquired")
        timeline = self.service.job_timeline("auditor", job["job_id"])
        types = [(event["event_type"], event["reason_code"]) for event in timeline["events"]]
        self.assertIn(("acquired", "job_available"), types)
        self.assertIn(("released", "worker_released"), types)
        self.assertEqual(len({event["sequence_no"] for event in timeline["events"]}), len(timeline["events"]))

    def test_late_result_after_takeover_is_rejected_and_explained(self) -> None:
        job = self.service.claim_job("stat", 10)
        self.clock.advance(seconds=11)
        self.service.claim_job("stat2", 10)
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", job["job_id"])
        timeline = self.service.job_timeline("auditor", job["job_id"])
        by_type: dict[str, list[dict]] = {}
        for event in timeline["events"]:
            by_type.setdefault(event["event_type"], []).append(event)
        self.assertEqual(by_type["taken_over"][0]["actor_id"], "stat2")
        self.assertEqual(by_type["rejected"][0]["actor_id"], "stat")
        self.assertTrue(by_type["rejected"][0]["reason"])
        # 被拒绝的迟到结果没有产生分析记录，新持有者的结论才生效。
        analysis = self.service.complete_job("stat2", job["job_id"])
        stored = self.connection.execute(
            "SELECT created_by FROM analyses WHERE analysis_id=?", (analysis["analysis_id"],)
        ).fetchone()
        self.assertEqual(stored["created_by"], "stat2")

    def test_timeline_survives_service_restart(self) -> None:
        job = self.service.claim_job("stat", 10)
        self.clock.advance(seconds=11)
        self.service.claim_job("stat2", 20)
        with self.assertRaises(InvalidState):
            self.service.complete_job("stat", job["job_id"])
        before = self.service.job_timeline("auditor", job["job_id"])
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "restart.sqlite3"
            file_connection = connect(database)
            file_service = TaxonomyLabService(file_connection, self.clock)
            for user_id, role in (
                ("operator", "operator"),
                ("stat", "statistician"),
                ("stat2", "statistician"),
                ("approver", "approver"),
                ("auditor", "auditor"),
                ("admin", "admin"),
            ):
                file_service.create_user(user_id, user_id, role)
            file_service.register_device("operator", "scope-a", "A 型", "厂商")
            file_service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
            file_service.publish_evidence_protocol("stat", self.protocol)
            file_service.grant_qualification("admin", "stat", TASK_FAMILY)
            file_service.grant_qualification("admin", "stat2", TASK_FAMILY)
            file_service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
            file_service.start_batch("operator", "batch-a", 1)
            file_service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
            file_service.seal_batch("stat", "batch-a", 2)
            restored_job = file_service.claim_job("stat", 10)
            self.clock.advance(seconds=11)
            file_service.claim_job("stat2", 20)
            with self.assertRaises(InvalidState):
                file_service.complete_job("stat", restored_job["job_id"])
            file_connection.close()

            reopened = connect(database)
            try:
                restarted_service = TaxonomyLabService(reopened, self.clock)
                after = restarted_service.job_timeline("auditor", restored_job["job_id"])
            finally:
                reopened.close()
        self.assertEqual(
            [(e["event_type"], e["reason_code"], e["actor_id"]) for e in before["events"]],
            [(e["event_type"], e["reason_code"], e["actor_id"]) for e in after["events"]],
        )
        self.assertEqual([e["sequence_no"] for e in after["events"]], [1, 2, 3])

    def test_other_task_family_job_is_out_of_scope(self) -> None:
        protocol2 = dict(self.protocol)
        protocol2["evidence_protocol_id"] = "demo-bird-call-v1"
        protocol2["title"] = "秋季夜间声纹调查"
        protocol2["task_family"] = "bird-call-nocturnal"
        self.service.publish_evidence_protocol("stat", protocol2)
        self.service.create_batch("operator", "batch-b", "demo-bird-call-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-b", 1)
        self.service.seal_batch("stat", "batch-b", 2)
        bird_job = self.connection.execute(
            "SELECT job_id FROM analysis_jobs WHERE batch_id='batch-b'"
        ).fetchone()[0]
        # stat 只有昆虫分类族资格，轮询看不到声纹任务……
        claimed = self.service.claim_job("stat", 30)
        self.assertNotEqual(claimed["job_id"], bird_job)
        # ……指定领取声纹任务也会被拒绝。
        with self.assertRaises(Forbidden):
            self.service.claim_job("stat", 30, job_id=bird_job)
        # 授予声纹族资格后可以领取。
        self.service.grant_qualification("admin", "stat", "bird-call-nocturnal")
        bird_claim = self.service.claim_job("stat", 30, job_id=bird_job)
        self.assertEqual(bird_claim["job_id"], bird_job)


if __name__ == "__main__":
    unittest.main()
