from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_FAMILY = "insect-taxonomy"


def _payload(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for user_id, role in (
            ("operator", "operator"), ("stat", "statistician"), ("stat2", "statistician"),
            ("auditor", "auditor"), ("admin", "admin"), ("ext", "statistician"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_device("operator", "scope-a", "A", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        for user_id in ("stat", "stat2", "ext"):
            self.service.grant_qualification("admin", user_id, TASK_FAMILY)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "k1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)

    def tearDown(self) -> None:
        self.connection.close()

    def _headers(self, actor: str | None = None, idempotency_key: str | None = None) -> dict:
        headers = {}
        if actor is not None:
            headers["X-Actor-Id"] = actor
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = _payload({"user_id": "u1", "display_name": "操作员", "role": "operator"})
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def test_claim_requires_actor_header(self) -> None:
        response = self.app.handle("POST", "/jobs/claim", body=_payload({}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_job_lease_paths_enforce_account_role_scope_and_owner(self) -> None:
        # 操作员无 analysis.run，禁止领取。
        denied = self.app.handle("POST", "/jobs/claim", self._headers("operator"), _payload({}))
        self.assertEqual(denied.status, 403)

        # 合格统计人员领取成功。
        claimed = self.app.handle(
            "POST", "/jobs/claim", self._headers("ext", "claim-1"), _payload({"lease_seconds": 10})
        )
        self.assertEqual(claimed.status, 200)
        job_id = claimed.body["job"]["job_id"]
        self.assertEqual(claimed.body["job"]["lease_owner"], "ext")

        # 重放同一请求键返回稳定状态。
        replay = self.app.handle(
            "POST", "/jobs/claim", self._headers("ext", "claim-1"), _payload({"lease_seconds": 10})
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body["job"]["sequence_no"], claimed.body["job"]["sequence_no"])

        # 他人不能完成、失败、续作未持有的租约。
        complete = self.app.handle(
            "POST", f"/jobs/{job_id}/complete", self._headers("stat"), b"{}"
        )
        self.assertEqual(complete.status, 409)
        fail = self.app.handle(
            "POST", f"/jobs/{job_id}/fail", self._headers("stat"), _payload({"error": "x"})
        )
        self.assertEqual(fail.status, 409)

        # 停用外协账号后，其续作被拒。
        self.app.handle(
            "POST", "/users/deactivate", self._headers("admin"),
            _payload({"user_id": "ext", "reason": "外协合同到期"}),
        )
        renew = self.app.handle(
            "POST", f"/jobs/{job_id}/renew", self._headers("ext"), _payload({"lease_seconds": 30})
        )
        self.assertEqual(renew.status, 403)

        # 租约到期后由合格人员接管。
        self.clock.advance(seconds=11)
        takeover = self.app.handle(
            "POST", f"/jobs/claim", self._headers("stat"), _payload({"job_id": job_id, "lease_seconds": 20})
        )
        self.assertEqual(takeover.status, 200)
        self.assertEqual(takeover.body["job"]["lease_event"], "taken_over")

        # 旧持有者的迟到完成仍被拒绝（账号已停用，返回 403）。
        late = self.app.handle("POST", f"/jobs/{job_id}/complete", self._headers("ext"), b"{}")
        self.assertEqual(late.status, 403)

        # 新持有者完成。
        done = self.app.handle("POST", f"/jobs/{job_id}/complete", self._headers("stat"), b"{}")
        self.assertEqual(done.status, 200)

        # 管理时间线说明了占用、拒绝、接管的原因，且需要审计权限。
        no_audit = self.app.handle("GET", f"/jobs/{job_id}/timeline", self._headers("stat"))
        self.assertEqual(no_audit.status, 403)
        timeline = self.app.handle("GET", f"/jobs/{job_id}/timeline", self._headers("auditor"))
        self.assertEqual(timeline.status, 200)
        events = timeline.body["events"]
        self.assertTrue(any(e["event_type"] == "acquired" and e["actor_id"] == "ext" for e in events))
        self.assertTrue(any(e["event_type"] == "rejected" and e["reason"] for e in events))
        self.assertTrue(any(e["event_type"] == "taken_over" and e["actor_id"] == "stat" for e in events))
        self.assertTrue(any(e["event_type"] == "succeeded" and e["actor_id"] == "stat" for e in events))
        self.assertEqual([e["sequence_no"] for e in events], list(range(1, len(events) + 1)))

    def test_unqualified_claim_is_rejected(self) -> None:
        self.service.create_user("stat3", "无资格", "statistician")
        response = self.app.handle("POST", "/jobs/claim", self._headers("stat3"), _payload({}))
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.body["job"])


if __name__ == "__main__":
    unittest.main()
