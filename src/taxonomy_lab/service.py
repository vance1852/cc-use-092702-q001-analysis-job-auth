"""分类实验观察采信服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .analysis import ALGORITHM_VERSION, analyze
from .clock import SystemClock, isoformat
from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .errors import (
    Conflict, Forbidden, InvalidState, NotFound, ServiceError, ValidationFailed,
)
from .jsonio import canonical_json, content_digest
from .storage import transaction


ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "evidence_item.import",
        "exclusion.request", "exclusion.revoke",
    },
    "statistician": {"evidence_protocol.publish", "batch.seal", "exclusion.review", "analysis.run"},
    "approver": {"decision.write"},
    "auditor": {"report.read", "audit.read"},
    "admin": {"user.manage", "qualification.manage", "report.read", "audit.read"},
}

# 租约事件的原因代码：管理接口据此说明每次占用、拒绝、释放和接管。
REASON_ACQUIRED = "job_available"
REASON_TAKEN_OVER = "lease_expired_takeover"
REASON_RENEWED = "lease_renewed"
REASON_SUCCEEDED = "analysis_completed"
REASON_FAILED = "worker_reported_failure"
REASON_RELEASED = "worker_released"
REASON_NOT_QUALIFIED = "not_qualified_for_task_family"
REASON_HELD_BY_OTHER = "lease_held_by_other"
REASON_NOT_HELD = "lease_not_held_by_actor"
REASON_EXPIRED = "lease_expired"
REASON_NOT_AVAILABLE = "job_not_available_yet"
REASON_NOT_LEASED = "job_not_in_leasable_state"
REASON_RACE_LOST = "lease_state_changed_concurrently"


class _LeaseRace(RuntimeError):
    """校验通过后、写入时租约状态被并发改变。"""

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


class TaxonomyLabService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        from .storage import initialize
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

    # ------------------------------------------------------------------ 用户

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

    def set_user_active(
        self, actor_id: str, user_id: str, active: bool, reason: str = ""
    ) -> dict[str, Any]:
        """启用或停用账号（含外协账号）；停用后不能再领取、续作或提交任何任务。"""

        self._require(actor_id, "user.manage")
        target = self.connection.execute(
            "SELECT user_id, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if target is None:
            raise NotFound(f"用户不存在: {user_id}")
        if actor_id == user_id and not active:
            raise ValidationFailed("不能停用当前操作者自己的账号")
        if not reason.strip():
            raise ValidationFailed("必须填写账号状态变更原因")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE users SET active=? WHERE user_id=?", (1 if active else 0, user_id)
            )
            self._audit(
                "user", user_id, "user.activated" if active else "user.deactivated", actor_id,
                {"reason": reason.strip()},
            )
        return {"user_id": user_id, "active": active}

    # ------------------------------------------------------------ 任务适用范围

    def _task_family_known(self, task_family: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM evidence_protocol_catalog WHERE task_family=? LIMIT 1", (task_family,)
        ).fetchone() is not None

    def grant_qualification(self, actor_id: str, user_id: str, task_family: str) -> dict[str, Any]:
        """授予某用户处理某任务族（如夜间声纹、样线观察、红外相机）的资格。"""

        self._require(actor_id, "qualification.manage")
        if not task_family.strip():
            raise ValidationFailed("任务适用范围不能为空")
        task_family = task_family.strip()
        if not self._task_family_known(task_family):
            raise NotFound(f"任务适用范围尚未在任何协议中登记: {task_family}")
        target = self.connection.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
        if target is None:
            raise NotFound(f"用户不存在: {user_id}")
        if "analysis.run" not in ROLE_PERMISSIONS[target["role"]]:
            raise ValidationFailed(f"角色 {target['role']} 不承担生态分析职责，不能授予任务资格")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO user_qualifications(user_id,task_family,granted_by,granted_at,revoked_at) "
                "VALUES(?,?,?,?,NULL) ON CONFLICT(user_id,task_family) DO UPDATE SET "
                "granted_by=excluded.granted_by, granted_at=excluded.granted_at, revoked_at=NULL",
                (user_id, task_family, actor_id, now),
            )
            self._audit(
                "qualification", f"{user_id}:{task_family}", "qualification.granted", actor_id,
                {"user_id": user_id, "task_family": task_family},
            )
        return {"user_id": user_id, "task_family": task_family, "revoked": False}

    def revoke_qualification(
        self, actor_id: str, user_id: str, task_family: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "qualification.manage")
        if not reason.strip():
            raise ValidationFailed("必须填写撤销资格的原因")
        row = self.connection.execute(
            "SELECT revoked_at FROM user_qualifications WHERE user_id=? AND task_family=?",
            (user_id, task_family),
        ).fetchone()
        if row is None:
            raise NotFound("未找到该任务资格授予记录")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE user_qualifications SET revoked_at=? "
                "WHERE user_id=? AND task_family=? AND revoked_at IS NULL",
                (self._now(), user_id, task_family),
            )
            if cursor.rowcount != 1:
                raise InvalidState("该资格已经被撤销")
            self._audit(
                "qualification", f"{user_id}:{task_family}", "qualification.revoked", actor_id,
                {"reason": reason.strip()},
            )
        return {"user_id": user_id, "task_family": task_family, "revoked": True}

    def _is_qualified(self, user_id: str, task_family: str | None) -> bool:
        # task_family 为空表示旧版本遗留任务：只校验账号与岗位，不再限制任务族。
        if task_family is None:
            return True
        return self.connection.execute(
            "SELECT 1 FROM user_qualifications WHERE user_id=? AND task_family=? AND revoked_at IS NULL",
            (user_id, task_family),
        ).fetchone() is not None

    # ------------------------------------------------------------- 目录与协议

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
            task_family = self.connection.execute(
                "SELECT p.task_family FROM batches b "
                "JOIN evidence_protocol_catalog p "
                "ON p.evidence_protocol_id=b.evidence_protocol_id AND p.version=b.evidence_protocol_version "
                "WHERE b.batch_id=?",
                (batch_id,),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO analysis_jobs(batch_id,batch_revision,task_family,state,available_at,created_at,updated_at) "
                "VALUES(?,?,?, 'queued', ?,?,?)",
                (batch_id, new_revision, task_family, now, now, now),
            )
            self._audit("batch", batch_id, "batch.sealed", actor_id, {"revision": new_revision})
        return self.get_batch(batch_id)

    # ------------------------------------------------------------- 任务租约

    def _get_job(self, job_id: int) -> sqlite3.Row:
        job = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise NotFound("分析任务不存在")
        return job

    def _last_sequence(self, job_id: int) -> int:
        return self.connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) AS seq FROM lease_events WHERE job_id=?", (job_id,)
        ).fetchone()["seq"]

    def _record_lease_event(
        self,
        job: sqlite3.Row | Mapping[str, Any],
        event_type: str,
        reason_code: str,
        reason: str,
        actor_id: str,
        lease_expires_at: str | None,
        request_key: str | None = None,
        previous_owner: str | None = None,
    ) -> int:
        """在租约写入事务内部追加一条严格有序的租约事件。"""

        sequence_no = self._last_sequence(job["job_id"]) + 1
        self.connection.execute(
            "INSERT INTO lease_events(job_id,batch_id,sequence_no,event_type,reason_code,reason,actor_id,"
            "lease_owner,lease_expires_at,request_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                job["job_id"], job["batch_id"], sequence_no, event_type, reason_code, reason, actor_id,
                actor_id, lease_expires_at, request_key, self._now(),
            ),
        )
        if previous_owner is not None:
            # 接管在通用审计流中也留痕，标明被接替的旧持有者。
            self.connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "job", str(job["job_id"]), "lease.taken_over", actor_id,
                    canonical_json({"previous_owner": previous_owner, "reason_code": reason_code}),
                    self._now(),
                ),
            )
        return sequence_no

    def _record_rejection(
        self, job: sqlite3.Row, actor_id: str, reason_code: str, reason: str
    ) -> None:
        """落一条拒绝事件。拒绝的业务操作本身不会写入，因此拒绝原因必须独立持久化。"""

        with transaction(self.connection, immediate=True):
            sequence_no = self._last_sequence(job["job_id"]) + 1
            self.connection.execute(
                "INSERT INTO lease_events(job_id,batch_id,sequence_no,event_type,reason_code,reason,actor_id,"
                "lease_owner,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job["job_id"], job["batch_id"], sequence_no, "rejected",
                    reason_code, reason, actor_id, job["lease_owner"], job["lease_expires_at"], self._now(),
                ),
            )

    def _reject(
        self,
        job: sqlite3.Row,
        actor_id: str,
        reason_code: str,
        reason: str,
        error_type: type[ServiceError] = InvalidState,
    ) -> None:
        self._record_rejection(job, actor_id, reason_code, reason)
        raise error_type(reason)

    def _assert_holder(self, actor_id: str, job: sqlite3.Row) -> None:
        """续作/完成/失败的统一前置校验：任务适用范围、持有者、租约有效期。"""

        if not self._is_qualified(actor_id, job["task_family"]):
            self._reject(
                job, actor_id, REASON_NOT_QUALIFIED,
                f"操作者没有任务适用范围 {job['task_family']} 的分析资格", Forbidden,
            )
        if job["state"] != "leased" or job["lease_owner"] != actor_id:
            self._reject(job, actor_id, REASON_NOT_HELD, "任务未由当前操作者持有")
        if job["lease_expires_at"] is not None and job["lease_expires_at"] <= self._now():
            self._reject(job, actor_id, REASON_EXPIRED, "任务租约已经过期，迟到结果不能被接受")

    def _claim_response(
        self, job_id: int, event_type: str, sequence_no: int, reason_code: str
    ) -> dict[str, Any]:
        claimed = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(claimed) | {"lease_event": event_type, "sequence_no": sequence_no, "reason_code": reason_code}

    def _store_claim_replay(
        self, request_key: str, request_digest: str, job_id: int, response: Mapping[str, Any]
    ) -> None:
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO job_requests(scope,request_key,request_sha256,job_id,response_json,created_at) "
                    "VALUES('job_claim',?,?,?,?,?)",
                    (request_key, request_digest, job_id, canonical_json(response), self._now()),
                )
            except sqlite3.IntegrityError:
                # 并发的同键请求已经落库；以已存储响应为准，由重放逻辑读取。
                pass

    def claim_job(
        self,
        actor_id: str,
        lease_seconds: int = 60,
        request_key: str | None = None,
        job_id: int | None = None,
    ) -> dict[str, Any] | None:
        """领取任务。

        - 账号必须存在且启用、角色必须具备 analysis.run；
        - 只能领取本人具备任务族资格（任务适用范围）的任务；
        - 租约绑定到可追溯的用户编号；过期任务只能被合格人员接管；
        - 相同 request_key 的重放返回首次领取时的稳定响应。
        """

        self._require(actor_id, "analysis.run")
        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        request_digest = content_digest([{
            "actor_id": actor_id, "lease_seconds": lease_seconds, "job_id": job_id,
        }])
        if request_key is not None:
            stored = self.connection.execute(
                "SELECT request_sha256,response_json FROM job_requests "
                "WHERE scope='job_claim' AND request_key=?",
                (request_key,),
            ).fetchone()
            if stored is not None:
                if stored["request_sha256"] != request_digest:
                    raise Conflict("同一领取请求键对应了不同请求内容")
                return json.loads(stored["response_json"])

        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))

        # 阶段一：针对指定任务做权威前置校验；拒绝原因独立落库。
        target: sqlite3.Row | None = None
        takeover = False
        previous_owner: str | None = None
        if job_id is not None:
            target = self._get_job(job_id)
            if not self._is_qualified(actor_id, target["task_family"]):
                self._reject(
                    target, actor_id, REASON_NOT_QUALIFIED,
                    f"操作者没有任务适用范围 {target['task_family']} 的分析资格", Forbidden,
                )
            if target["state"] == "leased" and target["lease_owner"] == actor_id and target["lease_expires_at"] > now:
                # 本人仍持有未到期租约：重复领取返回当前稳定状态。
                response = self._claim_response(
                    job_id, "acquired", self._last_sequence(job_id), REASON_ACQUIRED
                )
                if request_key is not None:
                    self._store_claim_replay(request_key, request_digest, job_id, response)
                return response
            if target["state"] not in {"queued", "leased"}:
                self._reject(
                    target, actor_id, REASON_NOT_LEASED,
                    f"任务状态为 {target['state']}，不能领取",
                )
            if target["state"] == "queued" and target["available_at"] > now:
                self._reject(target, actor_id, REASON_NOT_AVAILABLE, "任务尚未到可领取时间")
            if target["state"] == "leased" and target["lease_expires_at"] > now:
                self._reject(
                    target, actor_id, REASON_HELD_BY_OTHER,
                    f"租约由 {target['lease_owner']} 持有且尚未到期", Conflict,
                )
            takeover = target["state"] == "leased"
            previous_owner = target["lease_owner"] if takeover else None

        # 阶段二：比较并交换式写入，杜绝并发双领；校验后状态被抢走时补记拒绝。
        try:
            with transaction(self.connection, immediate=True):
                if target is None:
                    row = self.connection.execute(
                        "SELECT j.job_id, j.state FROM analysis_jobs j WHERE "
                        "((j.state='queued' AND j.available_at<=?) OR (j.state='leased' AND j.lease_expires_at<=?)) "
                        "AND (j.task_family IS NULL OR EXISTS ("
                        "SELECT 1 FROM user_qualifications q "
                        "WHERE q.user_id=? AND q.task_family=j.task_family AND q.revoked_at IS NULL)) "
                        "ORDER BY j.available_at,j.job_id LIMIT 1",
                        (now, now, actor_id),
                    ).fetchone()
                    if row is None:
                        return None
                    target = self.connection.execute(
                        "SELECT * FROM analysis_jobs WHERE job_id=?", (row["job_id"],)
                    ).fetchone()
                    takeover = target["state"] == "leased"
                    previous_owner = target["lease_owner"] if takeover else None

                if takeover:
                    cursor = self.connection.execute(
                        "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,claimed_by=?,"
                        "lease_expires_at=?,last_lease_expires_at=?,updated_at=? "
                        "WHERE job_id=? AND state='leased' AND lease_expires_at<=?",
                        (actor_id, actor_id, expires, expires, now, target["job_id"], now),
                    )
                else:
                    cursor = self.connection.execute(
                        "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,claimed_by=?,"
                        "lease_expires_at=?,last_lease_expires_at=?,updated_at=? "
                        "WHERE job_id=? AND state='queued' AND available_at<=?",
                        (actor_id, actor_id, expires, expires, now, target["job_id"], now),
                    )
                if cursor.rowcount != 1:
                    raise _LeaseRace(REASON_RACE_LOST, "任务在领取瞬间被其他合格人员取走或状态已变化")
                claimed = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (target["job_id"],)
                ).fetchone()
                event_type = "taken_over" if takeover else "acquired"
                reason_code = REASON_TAKEN_OVER if takeover else REASON_ACQUIRED
                reason = (
                    f"租约在 {previous_owner} 持有期间到期，由合格人员接管"
                    if takeover else "任务已进入可领取队列"
                )
                sequence_no = self._record_lease_event(
                    claimed, event_type, reason_code, reason, actor_id, expires,
                    request_key=request_key, previous_owner=previous_owner,
                )
                response = self._claim_response(claimed["job_id"], event_type, sequence_no, reason_code)
                if request_key is not None:
                    self.connection.execute(
                        "INSERT INTO job_requests(scope,request_key,request_sha256,job_id,response_json,created_at) "
                        "VALUES('job_claim',?,?,?,?,?)",
                        (request_key, request_digest, claimed["job_id"], canonical_json(response), now),
                    )
        except _LeaseRace as race:
            held = self._get_job(target["job_id"] if target is not None else job_id)
            self._record_rejection(held, actor_id, race.reason_code, race.reason)
            raise Conflict(race.reason) from race
        return response

    def renew_job(self, actor_id: str, job_id: int, lease_seconds: int = 60) -> dict[str, Any]:
        """持有者续作租约；同样校验账号状态、岗位权限和任务适用范围。"""

        self._require(actor_id, "analysis.run")
        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        job = self._get_job(job_id)
        self._assert_holder(actor_id, job)
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET lease_expires_at=?,last_lease_expires_at=?,updated_at=? "
                    "WHERE job_id=? AND state='leased' AND lease_owner=? AND lease_expires_at>?",
                    (expires, expires, now, job_id, actor_id, now),
                )
                if cursor.rowcount != 1:
                    raise _LeaseRace(REASON_RACE_LOST, "续作时任务租约已不属于当前操作者")
                claimed = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                sequence_no = self._record_lease_event(
                    claimed, "renewed", REASON_RENEWED, "操作者在租约到期前申请续作", actor_id, expires
                )
        except _LeaseRace as race:
            self._persist_race_rejection(actor_id, job_id, race)
            raise InvalidState(race.reason) from race
        return self._claim_response(job_id, "renewed", sequence_no, REASON_RENEWED)

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

    def _persist_race_rejection(
        self, actor_id: str, job_id: int, race: _LeaseRace
    ) -> None:
        try:
            held = self._get_job(job_id)
        except NotFound:
            return
        self._record_rejection(held, actor_id, race.reason_code, race.reason)

    def complete_job(self, actor_id: str, job_id: int) -> dict[str, Any]:
        """提交分析结论。只有当前持有者在租约有效期内可以提交，迟到结果一律拒绝。"""

        self._require(actor_id, "analysis.run")
        job = self._get_job(job_id)
        self._assert_holder(actor_id, job)
        batch = self.get_batch(job["batch_id"])
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(
            batch["evidence_protocol_id"], batch["evidence_protocol_version"]
        )
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
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                # 事务内再次确认租约归属，防止校验与写入之间发生接管。
                current = self.connection.execute(
                    "SELECT state,lease_owner,lease_expires_at FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if (
                    current["state"] != "leased"
                    or current["lease_owner"] != actor_id
                    or current["lease_expires_at"] <= now
                ):
                    raise _LeaseRace(REASON_NOT_HELD, "提交时任务租约已不属于当前操作者，迟到结果不能覆盖新结论")
                existing = self.connection.execute(
                    "SELECT analysis_id,result_json FROM analyses "
                    "WHERE batch_id=? AND batch_revision=? AND input_sha256=?",
                    (batch["batch_id"], job["batch_revision"], input_digest),
                ).fetchone()
                if existing is None:
                    cursor = self.connection.execute(
                        "INSERT INTO analyses(batch_id,batch_revision,evidence_protocol_sha256,input_sha256,"
                        "algorithm_version,seed,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            batch["batch_id"], job["batch_revision"], evidence_protocol_digest, input_digest,
                            ALGORITHM_VERSION, evidence_protocol.seed, canonical_json(result), actor_id, now,
                        ),
                    )
                    analysis_id = cursor.lastrowid
                else:
                    analysis_id = existing["analysis_id"]
                    result = json.loads(existing["result_json"])
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,"
                    "last_lease_expires_at=?,updated_at=? WHERE job_id=? AND state='leased' AND lease_owner=?",
                    (job["lease_expires_at"], now, job_id, actor_id),
                )
                if cursor.rowcount != 1:
                    raise _LeaseRace(REASON_NOT_HELD, "提交时任务租约已不属于当前操作者，迟到结果不能覆盖新结论")
                succeeded = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                self.connection.execute(
                    "UPDATE batches SET state='analyzed' WHERE batch_id=? AND state IN ('sealed','analyzing')",
                    (batch["batch_id"],),
                )
                self._record_lease_event(
                    succeeded, "succeeded", REASON_SUCCEEDED, "分析完成并形成采信结论", actor_id, None
                )
                self._audit(
                    "batch",
                    batch["batch_id"],
                    "analysis.completed",
                    actor_id,
                    {"analysis_id": analysis_id, "input_sha256": input_digest, "job_id": job_id},
                )
        except _LeaseRace as race:
            self._persist_race_rejection(actor_id, job_id, race)
            raise InvalidState(race.reason) from race
        return {"analysis_id": analysis_id, "input_sha256": input_digest, "result": result}

    def fail_job(self, actor_id: str, job_id: int, error: str, retry_seconds: int = 0) -> dict[str, Any]:
        """持有者报告失败并退回队列；同样要过账号、岗位、范围和租约校验。"""

        self._require(actor_id, "analysis.run")
        if retry_seconds < 0:
            raise ValidationFailed("重试间隔不能为负")
        job = self._get_job(job_id)
        self._assert_holder(actor_id, job)
        message = error.strip() or "未提供失败原因"
        available = isoformat(self.clock.now() + timedelta(seconds=retry_seconds))
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "last_lease_expires_at=?,last_error=?,updated_at=? "
                    "WHERE job_id=? AND state='leased' AND lease_owner=? AND lease_expires_at>?",
                    (available, job["lease_expires_at"], message[:1000], now, job_id, actor_id, now),
                )
                if cursor.rowcount != 1:
                    raise _LeaseRace(REASON_NOT_HELD, "上报失败时任务租约已不属于当前操作者")
                failed = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                retry_text = f"，{retry_seconds} 秒后重新可领" if retry_seconds else ""
                self._record_lease_event(
                    failed, "failed", REASON_FAILED,
                    f"操作者报告失败：{message[:1000]}{retry_text}", actor_id, None,
                )
        except _LeaseRace as race:
            self._persist_race_rejection(actor_id, job_id, race)
            raise InvalidState(race.reason) from race
        return {"job_id": job_id, "state": "queued", "available_at": available}

    def release_job(self, actor_id: str, job_id: int, reason: str) -> dict[str, Any]:
        """持有者在完成前主动释放租约，必须说明释放原因。"""

        self._require(actor_id, "analysis.run")
        if not reason.strip():
            raise ValidationFailed("释放租约必须填写原因")
        job = self._get_job(job_id)
        if job["state"] != "leased" or job["lease_owner"] != actor_id:
            self._reject(job, actor_id, REASON_NOT_HELD, "只有当前持有者可以释放租约")
        if not self._is_qualified(actor_id, job["task_family"]):
            self._reject(
                job, actor_id, REASON_NOT_QUALIFIED,
                f"操作者没有任务适用范围 {job['task_family']} 的分析资格", Forbidden,
            )
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "last_lease_expires_at=?,updated_at=? WHERE job_id=? AND state='leased' AND lease_owner=?",
                    (now, job["lease_expires_at"], now, job_id, actor_id),
                )
                if cursor.rowcount != 1:
                    raise _LeaseRace(REASON_RACE_LOST, "释放时任务租约已不属于当前操作者")
                released = self.connection.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                self._record_lease_event(
                    released, "released", REASON_RELEASED,
                    f"持有者主动释放：{reason.strip()[:1000]}", actor_id, None,
                )
        except _LeaseRace as race:
            self._persist_race_rejection(actor_id, job_id, race)
            raise InvalidState(race.reason) from race
        return self._claim_response(job_id, "released", self._last_sequence(job_id), REASON_RELEASED)

    # ------------------------------------------------------------- 管理与查询

    def job_timeline(self, actor_id: str, job_id: int) -> dict[str, Any]:
        """按严格顺序返回某次任务的占用、拒绝、释放、接管、完成和失败记录。"""

        self._require(actor_id, "audit.read")
        job = self._get_job(job_id)
        events = self.connection.execute(
            "SELECT sequence_no,event_type,reason_code,reason,actor_id,lease_owner,lease_expires_at,"
            "request_key,created_at FROM lease_events WHERE job_id=? ORDER BY sequence_no",
            (job_id,),
        ).fetchall()
        return {"job": dict(job), "events": [dict(row) for row in events]}

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
        if user["role"] not in {"statistician", "approver", "auditor", "admin"}:
            raise Forbidden("当前角色不能读取完整报告")
        batch = self.get_batch(batch_id)
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(
            batch["evidence_protocol_id"], batch["evidence_protocol_version"]
        )
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
        lease_events = self.connection.execute(
            "SELECT l.job_id,l.sequence_no,l.event_type,l.reason_code,l.reason,l.actor_id,"
            "l.lease_owner,l.lease_expires_at,l.created_at "
            "FROM lease_events l JOIN analysis_jobs j ON j.job_id=l.job_id "
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
            "lease_events": [dict(row) for row in lease_events],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }
