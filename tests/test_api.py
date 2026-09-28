from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService
from taxonomy_lab.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TaxonomyLabService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def post(self, path: str, payload: dict, actor: str | None = None, key: str | None = None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        if key:
            headers["Idempotency-Key"] = key
        return self.app.handle("POST", path, headers, json.dumps(payload).encode("utf-8"))

    def _sealed_batch(self) -> None:
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for user_id, role in (
            ("operator", "operator"), ("stat", "statistician"),
            ("approver", "approver"), ("auditor", "auditor"),
        ):
            self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        self.app.handle(
            "POST", "/capture_devices", {"X-Actor-Id": "operator"},
            json.dumps({"device_id": "scope-a", "model_name": "A", "vendor": "厂"}).encode(),
        )
        self.app.handle(
            "POST", "/builds", {"X-Actor-Id": "operator"},
            json.dumps({"build_id": "build-a", "device_id": "scope-a", "version": "1", "content_sha256": "b" * 64}).encode(),
        )
        self.post("/evidence_protocols", protocol, actor="stat")
        self.post("/batches", {
            "batch_id": "batch-a", "evidence_protocol_id": protocol["evidence_protocol_id"],
            "evidence_protocol_version": protocol["version"], "build_id": "build-a",
        }, actor="operator")
        self.post("/batches/batch-a/start", {"expected_revision": 1}, actor="operator")
        self.post(
            "/batches/batch-a/evidence_items", {"evidence_items": rows}, actor="operator", key="key-1"
        )
        self.post("/batches/batch-a/seal", {"expected_revision": 2}, actor="stat")
        self.post(f"/users/stat/task_families", {"task_family": protocol["task_family"]}, actor="approver")

    def test_lease_lifecycle_over_http_rejects_stale_holder(self) -> None:
        self._sealed_batch()
        claim = self.app.handle(
            "POST", "/jobs/claim", {"X-Actor-Id": "stat"}, b'{"lease_seconds": 10}'
        )
        self.assertEqual(claim.status, 200)
        job = claim.body["job"]
        self.assertEqual(job["lease_owner"], "stat")
        self.assertTrue(job["lease_fence_token"])
        # 未授权的操作员不能查看租约轨迹。
        denied = self.app.handle("GET", "/jobs/lease_events", {"X-Actor-Id": "operator"})
        self.assertEqual(denied.status, 403)
        # 无 X-Actor-Id 的旧式领取必须被拒绝。
        unauthenticated = self.app.handle("POST", "/jobs/claim", {}, b"{}")
        self.assertEqual(unauthenticated.status, 422)
        # 停用账号领取被拒绝，且轨迹中保留原因。
        self.patch_user_active("stat", False)
        inactive = self.app.handle("POST", "/jobs/claim", {"X-Actor-Id": "stat"}, b"{}")
        self.assertEqual(inactive.status, 403)
        self.patch_user_active("stat", True)
        # 同一幂等键重放返回稳定结果。
        replay = self.app.handle(
            "POST", "/jobs/claim", {"X-Actor-Id": "stat", "Idempotency-Key": "claim-1"},
            b'{"lease_seconds": 30}',
        )
        again = self.app.handle(
            "POST", "/jobs/claim", {"X-Actor-Id": "stat", "Idempotency-Key": "claim-1"},
            b'{"lease_seconds": 30}',
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(again.body, replay.body)

    def patch_user_active(self, user_id: str, active: bool) -> None:
        response = self.app.handle(
            "PATCH", f"/users/{user_id}", {"X-Actor-Id": "approver"},
            json.dumps({"active": active}).encode(),
        )
        self.assertEqual(response.status, 200)

    def test_lease_events_explain_occupancy_and_rejection(self) -> None:
        self._sealed_batch()
        self.app.handle("POST", "/jobs/claim", {"X-Actor-Id": "stat"}, b'{"lease_seconds": 10}')
        history = self.app.handle("GET", "/jobs/lease_events", {"X-Actor-Id": "auditor"})
        self.assertEqual(history.status, 200)
        types = [event["event_type"] for event in history.body["events"]]
        self.assertEqual(types, ["job.claimed"])
        self.assertTrue(history.body["events"][0]["reason"])

    def test_lease_history_survives_restart(self) -> None:
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "restart.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            service = TaxonomyLabService(connection, clock)
            service.create_user("operator", "操作员", "operator")
            service.create_user("stat", "统计", "statistician")
            service.create_user("stat-b", "统计乙", "statistician")
            service.create_user("approver", "审批", "approver")
            service.grant_task_family("approver", "stat", protocol["task_family"])
            service.grant_task_family("approver", "stat-b", protocol["task_family"])
            service.register_device("operator", "scope-a", "A", "厂")
            service.register_build("operator", "build-a", "scope-a", "1", "b" * 64)
            service.publish_evidence_protocol("stat", protocol)
            service.create_batch("operator", "batch-a", protocol["evidence_protocol_id"], protocol["version"], "build-a")
            service.start_batch("operator", "batch-a", 1)
            rows = [
                json.loads(line)
                for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            service.import_evidence_items("operator", "batch-a", "key-1", rows)
            service.seal_batch("stat", "batch-a", 2)
            first = service.claim_job("stat", 10, idempotency_key="claim-1")
            clock.advance(seconds=11)
            second = service.claim_job("stat-b", 10, idempotency_key="claim-2")
            service.complete_job("stat-b", second["job_id"], second["lease_fence_token"])
            expected = [
                (event["event_type"], event["actor_id"], event["previous_owner"])
                for event in service.lease_history("approver")
            ]
            connection.close()
            # 重新打开同一数据库：租约轨迹、接管关系和事件顺序必须完整还原。
            reopened = connect(database)
            service2 = TaxonomyLabService(reopened)
            restored = [
                (event["event_type"], event["actor_id"], event["previous_owner"])
                for event in service2.lease_history("approver")
            ]
            self.assertEqual(restored, expected)
            self.assertEqual(
                ["job.claimed", "job.taken_over", "job.completed"],
                [event[0] for event in restored],
            )
            self.assertEqual(restored[1][2], "stat")
            # 幂等领取记录同样持久化：同一账号用同一键重放仍返回首次租约。
            replayed = service2.claim_job("stat", 10, idempotency_key="claim-1")
            self.assertEqual(replayed["job_id"], first["job_id"])
            self.assertEqual(replayed["lease_fence_token"], first["lease_fence_token"])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
