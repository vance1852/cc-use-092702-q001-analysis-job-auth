"""库区事件、读数、告警、工单和资源的领域模型。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

@dataclass(frozen=True)
class ZoneRecord:
    zone_record_id: str; collection_zone: str; biosafety_type: str; length_m: float; criticality: int; status: str = "normal"
    def validate(self) -> None:
        if not self.zone_record_id.strip() or not self.collection_zone.strip(): raise ValueError("zone_record id and collection_zone are required")
        if self.biosafety_type not in {"water", "quarantine", "gas"}: raise ValueError("unsupported biosafety type")
        if self.length_m <= 0 or not 1 <= self.criticality <= 5: raise ValueError("zone_record dimensions are invalid")

@dataclass(frozen=True)
class MonitoringRecord:
    monitoring_record_id: str; zone_record_id: str; sensor_source_id: str; speed_kmh: float; traffic_flow_vph: float; impact_index: float; observed_at: str
    def validate(self) -> None:
        if not self.monitoring_record_id.strip() or not self.zone_record_id.strip() or not self.sensor_source_id.strip(): raise ValueError("monitoring_record identifiers are required")
        if min(self.speed_kmh, self.traffic_flow_vph, self.impact_index) < 0: raise ValueError("monitoring_record values cannot be negative")
        parse_time(self.observed_at)

def as_dict(value: Any) -> dict[str, Any]:
    return {name: getattr(value, name) for name in value.__dataclass_fields__} if hasattr(value, "__dataclass_fields__") else dict(value)
