"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import TaxonomyLabService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    evidence_protocol = load_json(fixtures / "demo_evidence_protocol.json")
    evidence_item_rows = [
        json.loads(line)
        for line in (fixtures / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="device-reviews-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = TaxonomyLabService(connection)
            service.create_user("operator-1", "测试操作员", "operator")
            service.create_user("stat-1", "统计负责人", "statistician")
            service.create_user("approver-1", "观察材料采信审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.create_user("admin-1", "保护站管理员", "admin")
            service.register_device("operator-1", "scope-a", "A 型标本事件实验采集设备", "示例设备供应商")
            service.register_build("operator-1", "build-a1", "scope-a", "1.0.0", "a" * 64)
            service.publish_evidence_protocol("stat-1", evidence_protocol)
            # 只有被授予该任务适用范围的合格统计人员才能领取分析任务。
            service.grant_qualification("admin-1", "stat-1", evidence_protocol["task_family"])
            service.create_batch("operator-1", "batch-demo", evidence_protocol["evidence_protocol_id"], evidence_protocol["version"], "build-a1")
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_evidence_items(
                "operator-1", "batch-demo", "demo-import-1", evidence_item_rows
            )
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("stat-1", lease_seconds=60, request_key="demo-claim-1")
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("stat-1", job["job_id"])
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            timeline = service.job_timeline("auditor-1", job["job_id"])
            report = service.report("auditor-1", "batch-demo")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "evidence_protocol": f"{evidence_protocol['evidence_protocol_id']}@{evidence_protocol['version']}",
        "evidence_item_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "event_count": len(report["events"]),
        "lease_event_count": len(timeline["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行校准数据基础工具的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
