"""分类实验观察采信服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .analysis import ALGORITHM_VERSION, analyze
from .clock import SystemClock, isoformat
from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ServiceError, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "evidence_item.import",
        "exclusion.request", "exclusion.revoke",
    },
    "statistician": {"evidence_protocol.publish", "batch.seal", "exclusion.review", "analysis.run"},
    "approver": {"decision.write", "user.manage"},
    "auditor": {"report.read", "audit.read"},
}

# 可以查看任务租约轨迹的角色。
LEASE_HISTORY_ROLES = frozenset({"statistician", "approver", "auditor"})


class _LeaseDenial(RuntimeError):
    """内部信号：租约操作被拒绝，事务回滚后需要落一条拒绝事件。"""

    def __init__(self, reason: str, message: str, error_cls: type[ServiceError]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.error_cls = error_cls


class TaxonomyLabService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def set_user_active(self, actor_id: str, user_id: str, active: bool) -> dict[str, Any]:
        """停用或重新启用账号；停用后该账号不得再占用或操作任何任务租约。"""

        self._require(actor_id, "user.manage")
        if self.connection.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
            raise NotFound(f"用户不存在: {user_id}")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE users SET active=? WHERE user_id=?",
                (1 if active else 0, user_id),
            )
            self._audit(
                "user",
                user_id,
                "user.activated" if active else "user.deactivated",
                actor_id,
                {"active": bool(active)},
            )
        return {"user_id": user_id, "active": bool(active)}

    def grant_task_family(self, actor_id: str, user_id: str, task_family: str) -> dict[str, Any]:
        """授予账号处理某一任务适用范围（任务族）的资格。"""

        self._require(actor_id, "user.manage")
        if not task_family.strip():
            raise ValidationFailed("任务适用范围不能为空")
        if self.connection.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
            raise NotFound(f"用户不存在: {user_id}")
        family = task_family.strip()
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO user_task_families(user_id,task_family,granted_by,granted_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,task_family) DO UPDATE SET granted_by=excluded.granted_by,granted_at=excluded.granted_at",
                (user_id, family, actor_id, now),
            )
            self._audit("user", user_id, "user.task_family_granted", actor_id, {"task_family": family})
        return {"user_id": user_id, "task_family": family}

    def _task_families(self, user_id: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT task_family FROM user_task_families WHERE user_id=?", (user_id,)
        ).fetchall()
        return frozenset(row["task_family"] for row in rows)

    def _job_task_family(self, batch_id: str) -> str:
        row = self.connection.execute(
            "SELECT p.task_family FROM batches b "
            "JOIN evidence_protocol_catalog p "
            "ON p.evidence_protocol_id=b.evidence_protocol_id AND p.version=b.evidence_protocol_version "
            "WHERE b.batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFound("任务适用范围不存在")
        return row["task_family"]

    def _lease_event(
        self,
        job_id: int,
        event_type: str,
        actor_id: str,
        reason: str,
        previous_owner: str | None = None,
        fence_token: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO lease_events(job_id,event_type,actor_id,previous_owner,lease_fence_token,"
            "lease_expires_at,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, event_type, actor_id, previous_owner, fence_token, expires_at, reason, self._now()),
        )

    def register_device(
        self, actor_id: str, device_id: str, model_name: str, vendor: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capture_devices(device_id,model_name,vendor,created_at) VALUES(?,?,?,?)",
                    (device_id, model_name, vendor, self._now()),
                )
                self._audit("device", device_id, "device.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"实验采集设备已存在: {device_id}") from exc
        return {"device_id": device_id, "model_name": model_name, "vendor": vendor}

    def register_build(
        self, actor_id: str, build_id: str, device_id: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("构建摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO builds(build_id,device_id,version,content_sha256,created_at) VALUES(?,?,?,?,?)",
                    (build_id, device_id, version, content_sha256.lower(), self._now()),
                )
                self._audit("build", build_id, "build.registered", actor_id, {"device_id": device_id, "version": version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("构建编号、版本或摘要冲突") from exc
        return {"build_id": build_id, "device_id": device_id, "version": version}

    def publish_evidence_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence_protocol.publish")
        try:
            evidence_protocol = EvidenceProtocol.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_protocol_catalog(evidence_protocol_id,version,title,task_family,canonical_json,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        evidence_protocol.evidence_protocol_id,
                        evidence_protocol.version,
                        evidence_protocol.title,
                        evidence_protocol.task_family,
                        text,
                        digest,
                        self._now(),
                    ),
                )
                identity = f"{evidence_protocol.evidence_protocol_id}@{evidence_protocol.version}"
                self._audit("evidence_protocol", identity, "evidence_protocol.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"evidence_protocol_id": evidence_protocol.evidence_protocol_id, "version": evidence_protocol.version, "sha256": digest}

    def _evidence_protocol(self, evidence_protocol_id: str, version: int) -> tuple[EvidenceProtocol, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM evidence_protocol_catalog WHERE evidence_protocol_id=? AND version=?",
            (evidence_protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return EvidenceProtocol.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    def create_batch(
        self,
        actor_id: str,
        batch_id: str,
        evidence_protocol_id: str,
        evidence_protocol_version: int,
        build_id: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "batch.create")
        self._evidence_protocol(evidence_protocol_id, evidence_protocol_version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO batches(batch_id,evidence_protocol_id,evidence_protocol_version,build_id,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, evidence_protocol_id, evidence_protocol_version, build_id, "draft", actor_id, self._now()),
                )
                self._audit("batch", batch_id, "batch.created", actor_id, {"build_id": build_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号冲突或构建不存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def start_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.start")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET state='running',revision=revision+1,started_at=? "
                "WHERE batch_id=? AND state='draft' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是当前草稿版本")
            self._audit("batch", batch_id, "batch.started", actor_id, {"from_revision": expected_revision})
        return self.get_batch(batch_id)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def import_evidence_items(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        raw_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence_item.import")
        rows = tuple(raw_rows)
        if not rows:
            raise ValidationFailed("观察记录数组不能为空")
        request_digest = content_digest(rows)
        scope = f"evidence_items:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "running":
            raise InvalidState("只有运行中的批次可以导入观察记录")
        evidence_protocol, _ = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        parsed: list[EvidenceItem] = []
        for raw in rows:
            try:
                item = EvidenceItem.from_dict(raw, evidence_protocol)
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            if item.device_id != self.connection.execute(
                "SELECT device_id FROM builds WHERE build_id=?", (batch["build_id"],)
            ).fetchone()["device_id"]:
                raise ValidationFailed("观察记录设备与批次登记不一致")
            parsed.append(item)
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        try:
            with transaction(self.connection, immediate=True):
                for item, raw in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO evidence_items(batch_id,source_batch,source_row,device_id,evidence_group_key,observed_at," 
                        "indicators_json,content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            item.source_batch,
                            item.source_row,
                            item.device_id,
                            item.evidence_group_key,
                            item.observed_at,
                            canonical_json({key: format(value, "f") for key, value in item.indicators.items()}),
                            content_digest([raw]),
                            actor_id,
                            self._now(),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("batch", batch_id, "evidence_items.imported", actor_id, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源行重复或幂等键并发冲突") from exc
        return response

    def request_exclusion(self, actor_id: str, evidence_item_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.request")
        evidence_item = self.connection.execute(
            "SELECT evidence_item_id,batch_id FROM evidence_items WHERE evidence_item_id=?", (evidence_item_id,)
        ).fetchone()
        if evidence_item is None:
            raise NotFound("观察记录不存在")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO exclusion_requests(evidence_item_id,status,reason,requested_by,requested_at) "
                    "VALUES(?,?,?,?,?)",
                    (evidence_item_id, "pending", reason, actor_id, self._now()),
                )
                exclusion_id = cursor.lastrowid
                self._audit("evidence_item", str(evidence_item_id), "exclusion.requested", actor_id, {"reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该观察记录已有待处理或生效排除") from exc
        return {"exclusion_id": exclusion_id, "status": "pending"}

    def review_exclusion(
        self, actor_id: str, exclusion_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "exclusion.review")
        row = self.connection.execute(
            "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("排除申请不存在")
        if row["status"] != "pending":
            raise InvalidState("排除申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的排除申请")
        status = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE exclusion_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE exclusion_id=? AND status='pending'",
                (status, actor_id, self._now(), note, exclusion_id),
            )
            self._audit("exclusion", str(exclusion_id), f"exclusion.{status}", actor_id, {"note": note})
        return {"exclusion_id": exclusion_id, "status": status}

    def revoke_exclusion(self, actor_id: str, exclusion_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.revoke")
        row = self.connection.execute(
            "SELECT e.*,o.batch_id FROM exclusion_requests e "
            "JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id WHERE e.exclusion_id=?",
            (exclusion_id,),
        ).fetchone()
        if row is None:
            raise NotFound("排除记录不存在")
        if row["status"] != "approved":
            raise InvalidState("只有已批准的排除可以撤销")
        if row["requested_by"] != actor_id:
            raise Forbidden("只有原申请人可以撤销排除")
        batch = self.get_batch(row["batch_id"])
        if batch["state"] != "running":
            raise InvalidState("批次封存后不能改变排除状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exclusion_requests SET status='revoked',review_note=?,reviewed_at=? "
                "WHERE exclusion_id=? AND status='approved'",
                (reason, self._now(), exclusion_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("排除状态已变化")
            self._audit(
                "evidence_item",
                str(row["evidence_item_id"]),
                "exclusion.revoked",
                actor_id,
                {"exclusion_id": exclusion_id, "reason": reason},
            )
        return {"exclusion_id": exclusion_id, "status": "revoked"}

    def seal_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.seal")
        with transaction(self.connection, immediate=True):
            pending = self.connection.execute(
                "SELECT count(*) FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
                "WHERE o.batch_id=? AND e.status='pending'", (batch_id,)
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有待复核的排除申请")
            cursor = self.connection.execute(
                "UPDATE batches SET state='sealed',revision=revision+1,sealed_at=? "
                "WHERE batch_id=? AND state='running' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次状态或版本已变化")
            new_revision = expected_revision + 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO analysis_jobs(batch_id,batch_revision,state,available_at,created_at,updated_at) "
                "VALUES(?,?, 'queued', ?,?,?)",
                (batch_id, new_revision, now, now, now),
            )
            self._audit("batch", batch_id, "batch.sealed", actor_id, {"revision": new_revision})
        return self.get_batch(batch_id)

    def _lease_payload(self, job: sqlite3.Row, *, takeover_from: str | None = None) -> dict[str, Any]:
        return {
            "job_id": job["job_id"],
            "batch_id": job["batch_id"],
            "batch_revision": job["batch_revision"],
            "state": job["state"],
            "attempts": job["attempts"],
            "lease_owner": job["lease_owner"],
            "lease_expires_at": job["lease_expires_at"],
            "lease_fence_token": job["lease_fence_token"],
            "taken_over_from": takeover_from,
        }

    @staticmethod
    def _claim_request_digest(actor_id: str, lease_seconds: int) -> str:
        return content_digest([{"actor_id": actor_id, "lease_seconds": lease_seconds}])

    def _claim_idempotent(
        self, actor_id: str, lease_seconds: int, idempotency_key: str | None
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        request_digest = self._claim_request_digest(actor_id, lease_seconds)
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM job_claim_requests WHERE scope='claim' AND key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一领取键对应了不同的领取请求")
        return json.loads(row["response_json"])

    def _account_for_analysis(self, actor_id: str) -> sqlite3.Row:
        """账号状态与岗位权限校验；任务适用范围在定位到任务后再校验。"""

        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (actor_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {actor_id}")
        if not row["active"]:
            raise _LeaseDenial("account_inactive", "用户已停用，不能占用分析任务", Forbidden)
        if "analysis.run" not in ROLE_PERMISSIONS[row["role"]]:
            raise _LeaseDenial(
                "role_not_allowed",
                f"角色 {row['role']} 不能领取分析任务",
                Forbidden,
            )
        return row

    def _authorize_lease_actor(self, actor_id: str, batch_id: str) -> tuple[sqlite3.Row, str]:
        """校验账号状态、岗位权限与任务适用范围，返回账号行与任务族。"""

        user = self._account_for_analysis(actor_id)
        task_family = self._job_task_family(batch_id)
        if task_family not in self._task_families(actor_id):
            raise _LeaseDenial(
                "task_family_not_granted",
                f"账号未被授予任务适用范围 {task_family}",
                Forbidden,
            )
        return user, task_family

    def _record_denial(
        self, actor_id: str, denial: _LeaseDenial, job_id: int | None = None
    ) -> None:
        """在独立事务中保留拒绝原因；尚未定位到具体任务时 job_id 为空。"""

        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO lease_events(job_id,event_type,actor_id,previous_owner,lease_fence_token,"
                "lease_expires_at,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, "job.lease_rejected", actor_id, None, None, None, denial.reason, self._now()),
            )

    def claim_job(
        self,
        actor_id: str,
        lease_seconds: int = 60,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """领取一个可处理的分析任务。

        领取前校验账号启用状态、analysis.run 岗位权限以及任务适用范围授权；
        租约记录可追溯操作者和栅栏令牌。同一 idempotency_key 重放返回首次结果。
        """

        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        actor_id = actor_id.strip()
        if not actor_id:
            raise ValidationFailed("操作者编号不能为空")
        request_digest = self._claim_request_digest(actor_id, lease_seconds)
        cached = self._claim_idempotent(actor_id, lease_seconds, idempotency_key)
        if cached is not None:
            return cached
        # 提前校验账号状态与岗位权限：停用或越权账号不得进入领取事务。
        try:
            self._account_for_analysis(actor_id)
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        result: dict[str, Any] | None = None
        try:
            with transaction(self.connection, immediate=True):
                rows = self.connection.execute(
                    "SELECT j.*,p.task_family FROM analysis_jobs j "
                    "JOIN batches b ON b.batch_id=j.batch_id "
                    "JOIN evidence_protocol_catalog p "
                    "ON p.evidence_protocol_id=b.evidence_protocol_id "
                    "AND p.version=b.evidence_protocol_version "
                    "WHERE (j.state='queued' AND j.available_at<=?) "
                    "OR (j.state='leased' AND j.lease_expires_at<=?) "
                    "ORDER BY j.available_at,j.job_id LIMIT 50",
                    (now, now),
                ).fetchall()
                chosen: sqlite3.Row | None = None
                # 事务内再次校验账号状态与岗位权限，防止领取前一刻被停用或改岗。
                self._account_for_analysis(actor_id)
                granted_families = self._task_families(actor_id)
                for candidate in rows:
                    if candidate["task_family"] not in granted_families:
                        # 任务适用范围不匹配：记录拒绝原因后继续寻找可领取的任务。
                        self._lease_event(
                            candidate["job_id"], "job.lease_rejected", actor_id,
                            f"task_family_not_granted: 账号未被授予任务适用范围 {candidate['task_family']}",
                        )
                        continue
                    chosen = candidate
                    break
                if chosen is None:
                    return None
                previous_owner = chosen["lease_owner"]
                takeover = previous_owner is not None and chosen["state"] == "leased"
                fence_token = uuid.uuid4().hex
                self.connection.execute(
                    "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,"
                    "lease_expires_at=?,lease_fence_token=?,last_error=NULL,updated_at=? WHERE job_id=?",
                    (actor_id, expires, fence_token, now, chosen["job_id"]),
                )
                claimed = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (chosen["job_id"],)
                ).fetchone()
                if takeover:
                    self._lease_event(
                        chosen["job_id"], "job.taken_over", actor_id,
                        f"旧租约已于 {chosen['lease_expires_at']} 到期，合格人员接管任务族 {chosen['task_family']} 任务",
                        previous_owner=previous_owner, fence_token=fence_token, expires_at=expires,
                    )
                else:
                    self._lease_event(
                        chosen["job_id"], "job.claimed", actor_id,
                        f"领取任务族 {chosen['task_family']} 任务，租约 {lease_seconds} 秒",
                        fence_token=fence_token, expires_at=expires,
                    )
                result = self._lease_payload(claimed, takeover_from=previous_owner)
                if idempotency_key:
                    self.connection.execute(
                        "INSERT INTO job_claim_requests(scope,key,request_sha256,job_id,response_json,created_at) "
                        "VALUES('claim',?,?,?,?,?)",
                        (idempotency_key, request_digest, chosen["job_id"], canonical_json(result), now),
                    )
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        except sqlite3.IntegrityError as exc:
            raise Conflict("领取请求键并发冲突") from exc
        return result

    def renew_job(self, actor_id: str, job_id: int, lease_seconds: int = 60) -> dict[str, Any]:
        """续作当前持有的未到期租约，返回带新栅栏令牌的租约。"""

        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        try:
            self._account_for_analysis(actor_id)
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        try:
            with transaction(self.connection, immediate=True):
                job = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
                if job is None:
                    raise NotFound("分析任务不存在")
                self._authorize_lease_actor(actor_id, job["batch_id"])
                if job["state"] != "leased" or job["lease_owner"] != actor_id:
                    raise _LeaseDenial("not_lease_holder", "任务未由当前操作者持有，不能续作", InvalidState)
                if job["lease_expires_at"] <= now:
                    raise _LeaseDenial("lease_expired", "任务租约已经过期，只能由合格人员接管", InvalidState)
                fence_token = uuid.uuid4().hex
                self.connection.execute(
                    "UPDATE analysis_jobs SET lease_expires_at=?,lease_fence_token=?,updated_at=? "
                    "WHERE job_id=? AND state='leased' AND lease_owner=?",
                    (expires, fence_token, now, job_id, actor_id),
                )
                self._lease_event(
                    job_id, "job.renewed", actor_id, f"续作租约 {lease_seconds} 秒",
                    fence_token=fence_token, expires_at=expires,
                )
                renewed = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial, job_id)
            raise denial.error_cls(denial.message) from denial
        return self._lease_payload(renewed)

    def takeover_job(self, actor_id: str, job_id: int, lease_seconds: int = 60) -> dict[str, Any]:
        """显式接管一个租约已经到期的任务；接管者必须通过全部资格校验。"""

        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        try:
            self._account_for_analysis(actor_id)
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        previous_owner: str | None = None
        try:
            with transaction(self.connection, immediate=True):
                job = self.connection.execute(
                    "SELECT j.*,p.task_family FROM analysis_jobs j "
                    "JOIN batches b ON b.batch_id=j.batch_id "
                    "JOIN evidence_protocol_catalog p "
                    "ON p.evidence_protocol_id=b.evidence_protocol_id AND p.version=b.evidence_protocol_version "
                    "WHERE j.job_id=?",
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise NotFound("分析任务不存在")
                self._authorize_lease_actor(actor_id, job["batch_id"])
                if job["state"] != "leased":
                    raise _LeaseDenial("job_not_leased", "任务不在租约中，无需接管", InvalidState)
                if job["lease_owner"] == actor_id:
                    raise _LeaseDenial("already_holder", "操作者已持有该任务，请使用续作", InvalidState)
                if job["lease_expires_at"] > now:
                    raise _LeaseDenial("lease_active", "旧租约尚未到期，不能接管", InvalidState)
                previous_owner = job["lease_owner"]
                fence_token = uuid.uuid4().hex
                self.connection.execute(
                    "UPDATE analysis_jobs SET attempts=attempts+1,lease_owner=?,lease_expires_at=?,"
                    "lease_fence_token=?,updated_at=? WHERE job_id=?",
                    (actor_id, expires, fence_token, now, job_id),
                )
                self._lease_event(
                    job_id, "job.taken_over", actor_id,
                    f"旧租约已于 {job['lease_expires_at']} 到期，合格人员接管任务族 {job['task_family']} 任务",
                    previous_owner=previous_owner, fence_token=fence_token, expires_at=expires,
                )
                taken = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial, job_id)
            raise denial.error_cls(denial.message) from denial
        return self._lease_payload(taken, takeover_from=previous_owner)

    def lease_history(self, actor_id: str, job_id: int | None = None) -> list[dict[str, Any]]:
        """管理接口：查看每次占用、拒绝、释放和接管的原因，顺序按事件编号排列。"""

        user = self._user(actor_id)
        if user["role"] not in LEASE_HISTORY_ROLES:
            raise Forbidden("当前角色不能查看租约轨迹")
        if job_id is None:
            rows = self.connection.execute(
                "SELECT event_id,job_id,event_type,actor_id,previous_owner,lease_fence_token,"
                "lease_expires_at,reason,created_at FROM lease_events ORDER BY event_id"
            ).fetchall()
        else:
            job = self.connection.execute("SELECT job_id FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound("分析任务不存在")
            rows = self.connection.execute(
                "SELECT event_id,job_id,event_type,actor_id,previous_owner,lease_fence_token,"
                "lease_expires_at,reason,created_at FROM lease_events WHERE job_id=? ORDER BY event_id",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _analysis_evidence_items(self, batch_id: str, evidence_protocol: EvidenceProtocol) -> tuple[EvidenceItem, ...]:
        rows = self.connection.execute(
            "SELECT o.*,e.reason AS excluded_reason FROM evidence_items o "
            "LEFT JOIN exclusion_requests e ON e.evidence_item_id=o.evidence_item_id AND e.status='approved' "
            "WHERE o.batch_id=? ORDER BY o.evidence_item_id",
            (batch_id,),
        ).fetchall()
        items: list[EvidenceItem] = []
        for row in rows:
            indicators = json.loads(row["indicators_json"])
            items.append(EvidenceItem(
                source_batch=row["source_batch"],
                source_row=row["source_row"],
                device_id=row["device_id"],
                evidence_protocol_id=evidence_protocol.evidence_protocol_id,
                evidence_protocol_version=evidence_protocol.version,
                evidence_group_key=row["evidence_group_key"],
                observed_at=row["observed_at"],
                indicators={key: Decimal(str(value)) for key, value in indicators.items()},
                excluded_reason=row["excluded_reason"],
            ))
        return tuple(items)

    def _load_active_lease(
        self, actor_id: str, job_id: int, fence_token: str | None
    ) -> tuple[sqlite3.Row, str]:
        """完成/失败路径的统一校验：账号、权限、适用范围、持有者、栅栏令牌与到期时间。"""

        job = self.connection.execute(
            "SELECT j.*,p.task_family FROM analysis_jobs j "
            "JOIN batches b ON b.batch_id=j.batch_id "
            "JOIN evidence_protocol_catalog p "
            "ON p.evidence_protocol_id=b.evidence_protocol_id AND p.version=b.evidence_protocol_version "
            "WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise NotFound("分析任务不存在")
        self._authorize_lease_actor(actor_id, job["batch_id"])
        if job["state"] != "leased":
            raise _LeaseDenial("job_not_leased", "任务不在有效租约中，操作被拒绝", InvalidState)
        if job["lease_owner"] != actor_id:
            raise _LeaseDenial(
                "stale_lease_holder",
                "任务已被其他合格人员接管，旧持有者的迟到结果不得写入",
                InvalidState,
            )
        if fence_token is not None and job["lease_fence_token"] != fence_token:
            raise _LeaseDenial(
                "lease_fence_mismatch",
                "租约栅栏令牌已失效（可能已经续作或被接管），结果不得写入",
                InvalidState,
            )
        if job["lease_expires_at"] <= self._now():
            raise _LeaseDenial("lease_expired", "任务租约已经过期，结果不得写入", InvalidState)
        return job, job["task_family"]

    def complete_job(self, actor_id: str, job_id: int, fence_token: str | None = None) -> dict[str, Any]:
        """由当前租约持有者提交分析结论；迟到或栅栏失效的结果一律拒绝。"""

        try:
            self._account_for_analysis(actor_id)
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        analysis_id = 0
        input_digest = ""
        result: dict[str, Any] = {}
        denied: _LeaseDenial | None = None
        try:
            with transaction(self.connection, immediate=True):
                job, task_family = self._load_active_lease(actor_id, job_id, fence_token)
                batch = self.get_batch(job["batch_id"])
                evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
                evidence_items = self._analysis_evidence_items(batch["batch_id"], evidence_protocol)
                snapshot_rows = [
                    {
                        "source_batch": item.source_batch,
                        "source_row": item.source_row,
                        "evidence_group": item.evidence_group_key,
                        "indicators": {key: format(value, "f") for key, value in item.indicators.items()},
                        "excluded_reason": item.excluded_reason,
                    }
                    for item in evidence_items
                ]
                input_digest = content_digest(snapshot_rows)
                result = analyze(evidence_protocol, evidence_items)
                existing = self.connection.execute(
                    "SELECT analysis_id,result_json FROM analyses WHERE batch_id=? AND batch_revision=? AND input_sha256=?",
                    (batch["batch_id"], job["batch_revision"], input_digest),
                ).fetchone()
                if existing is None:
                    cursor = self.connection.execute(
                        "INSERT INTO analyses(batch_id,batch_revision,evidence_protocol_sha256,input_sha256,algorithm_version,seed,"
                        "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            batch["batch_id"], job["batch_revision"], evidence_protocol_digest, input_digest,
                            ALGORITHM_VERSION, evidence_protocol.seed, canonical_json(result), actor_id, self._now(),
                        ),
                    )
                    analysis_id = cursor.lastrowid
                else:
                    analysis_id = existing["analysis_id"]
                    result = json.loads(existing["result_json"])
                now = self._now()
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,"
                    "lease_fence_token=NULL,updated_at=? "
                    "WHERE job_id=? AND state='leased' AND lease_owner=? AND lease_fence_token=?",
                    (now, job_id, actor_id, job["lease_fence_token"]),
                )
                if cursor.rowcount != 1:
                    # 并发下租约已被接管或续作：回滚全部写入，旧结论不得覆盖新结论。
                    raise _LeaseDenial("lease_changed", "租约已在提交期间变化，结果不得写入", InvalidState)
                self.connection.execute(
                    "UPDATE batches SET state='analyzed' WHERE batch_id=? AND state IN ('sealed','analyzing')",
                    (batch["batch_id"],),
                )
                self._lease_event(
                    job_id, "job.completed", actor_id,
                    f"任务族 {task_family} 分析完成并释放租约，analysis_id={analysis_id}",
                    fence_token=job["lease_fence_token"],
                )
                self._audit(
                    "batch",
                    batch["batch_id"],
                    "analysis.completed",
                    actor_id,
                    {"analysis_id": analysis_id, "input_sha256": input_digest, "job_id": job_id},
                )
        except _LeaseDenial as denial:
            denied = denial
        if denied is not None:
            self._record_denial(actor_id, denied, job_id)
            raise denied.error_cls(denied.message) from denied
        return {"analysis_id": analysis_id, "input_sha256": input_digest, "result": result}

    def fail_job(
        self,
        actor_id: str,
        job_id: int,
        error: str,
        retry_seconds: int = 0,
        fence_token: str | None = None,
    ) -> dict[str, Any]:
        """由当前租约持有者报告失败并释放租约；任务延迟后重新可领取。"""

        if retry_seconds < 0:
            raise ValidationFailed("重试延迟不能为负")
        try:
            self._account_for_analysis(actor_id)
        except _LeaseDenial as denial:
            self._record_denial(actor_id, denial)
            raise denial.error_cls(denial.message) from denial
        reason_text = error[:1000]
        available = isoformat(self.clock.now() + timedelta(seconds=retry_seconds))
        denied: _LeaseDenial | None = None
        try:
            with transaction(self.connection, immediate=True):
                job, task_family = self._load_active_lease(actor_id, job_id, fence_token)
                now = self._now()
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "lease_fence_token=NULL,last_error=?,updated_at=? "
                    "WHERE job_id=? AND state='leased' AND lease_owner=? AND lease_fence_token=?",
                    (available, reason_text, now, job_id, actor_id, job["lease_fence_token"]),
                )
                if cursor.rowcount != 1:
                    raise _LeaseDenial("lease_changed", "租约已变化，失败报告被拒绝", InvalidState)
                self._lease_event(
                    job_id, "job.failed", actor_id,
                    f"任务族 {task_family} 分析失败并释放租约：{reason_text}（{retry_seconds} 秒后可再次领取）",
                    fence_token=job["lease_fence_token"],
                )
        except _LeaseDenial as denial:
            denied = denial
        if denied is not None:
            self._record_denial(actor_id, denied, job_id)
            raise denied.error_cls(denied.message) from denied
        return {"job_id": job_id, "state": "queued", "available_at": available}

    def decide(
        self, actor_id: str, batch_id: str, analysis_id: int, decision: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if decision not in {"needs_more_data", "approved", "rejected"}:
            raise ValidationFailed("未知观察材料采信决定")
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=? AND batch_id=?", (analysis_id, batch_id)
        ).fetchone()
        if analysis_row is None:
            raise NotFound("分析版本不存在")
        if analysis_row["created_by"] == actor_id:
            raise Forbidden("统计负责人不能批准自己的分析")
        batch = self.get_batch(batch_id)
        if batch["state"] != "analyzed" or batch["revision"] != analysis_row["batch_revision"]:
            raise InvalidState("分析不是批次当前可审批版本")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO decisions(batch_id,analysis_id,decision,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, analysis_id, decision, reason, actor_id, self._now()),
                )
                self.connection.execute("UPDATE batches SET state='decided' WHERE batch_id=?", (batch_id,))
                self._audit(
                    "batch",
                    batch_id,
                    "decision.recorded",
                    actor_id,
                    {"decision_id": cursor.lastrowid, "analysis_id": analysis_id, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该分析版本已经形成决定") from exc
        return {"batch_id": batch_id, "analysis_id": analysis_id, "decision": decision}

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
            raise Forbidden("当前角色不能读取完整报告")
        batch = self.get_batch(batch_id)
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        decision_row = None
        if analysis_row is not None:
            decision_row = self.connection.execute(
                "SELECT * FROM decisions WHERE analysis_id=?", (analysis_row["analysis_id"],)
            ).fetchone()
        exclusions = self.connection.execute(
            "SELECT e.exclusion_id,e.evidence_item_id,e.status,e.reason,e.requested_by,e.reviewed_by "
            "FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
            "WHERE o.batch_id=? ORDER BY e.exclusion_id", (batch_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? "
            "ORDER BY event_id", (batch_id,)
        ).fetchall()
        lease_rows = self.connection.execute(
            "SELECT l.event_id,l.event_type,l.actor_id,l.previous_owner,l.lease_fence_token,"
            "l.lease_expires_at,l.reason,l.created_at,j.batch_id FROM lease_events l "
            "JOIN analysis_jobs j ON j.job_id=l.job_id "
            "WHERE j.batch_id=? ORDER BY l.event_id", (batch_id,)
        ).fetchall()
        return {
            "batch": batch,
            "evidence_protocol": {
                "evidence_protocol_id": evidence_protocol.evidence_protocol_id,
                "version": evidence_protocol.version,
                "sha256": evidence_protocol_digest,
                "seed": evidence_protocol.seed,
                "bootstrap_samples": evidence_protocol.bootstrap_samples,
            },
            "analysis": None if analysis_row is None else {
                "analysis_id": analysis_row["analysis_id"],
                "input_sha256": analysis_row["input_sha256"],
                "algorithm_version": analysis_row["algorithm_version"],
                "created_by": analysis_row["created_by"],
                "result": json.loads(analysis_row["result_json"]),
            },
            "decision": None if decision_row is None else dict(decision_row),
            "exclusions": [dict(row) for row in exclusions],
            "lease_events": [dict(row) for row in lease_rows],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }
