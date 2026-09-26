"""标本事件保藏中心调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RISK_INDEXES = {"HUMIDITY", "INJURY", "CONGESTION", "HAZMAT", "SECONDARY", "CUSTOM"}
RESOURCE_KINDS = {"preservation-box", "tow-truck", "ambulance", "warning-kit", "evidence-kit", "rapid-response-team"}
CENTER_KINDS = {"road-section", "receiving-vault", "herbarium-room", "storage", "patrol-station"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class RiskIndexRecord:
    risk_index: str
    duty_date: str
    index_value: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RiskIndexRecord":
        risk_index = required_text(raw.get("risk_index"), "risk_index", 16).upper()
        if risk_index not in RISK_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("risk_index 必须是 HUMIDITY、INJURY、CONGESTION、HAZMAT 或 SECONDARY")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            risk_index=risk_index,
            duty_date=date_text(raw.get("duty_date"), "duty_date"),
            index_value=decimal_value(raw.get("index_value"), "index_value", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class ResponseCenter:
    center_id: str
    name: str
    kind: str
    timezone: str
    capacity_units: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponseCenter":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in CENTER_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            center_id=identifier(raw.get("center_id"), "center_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_units=decimal_value(
                raw.get("capacity_units"), "capacity_units", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class RoadCorridor:
    corridor_id: str
    origin_center_id: str
    destination_center_id: str
    preservation_resource_kind: str
    hourly_capacity: Decimal
    delay_basis_points: int
    response_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoadCorridor":
        preservation_resource_kind = required_text(raw.get("preservation_resource_kind"), "preservation_resource_kind", 32)
        if preservation_resource_kind not in RESOURCE_KINDS:
            raise ValidationFailed("preservation_resource_kind 不是受支持的电源类型")
        loss = raw.get("delay_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("delay_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_center_id"), "origin_center_id")
        destination = identifier(raw.get("destination_center_id"), "destination_center_id")
        if origin == destination:
            raise ValidationFailed("转运路线起点和终点不能相同")
        return cls(
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            origin_center_id=origin,
            destination_center_id=destination,
            preservation_resource_kind=preservation_resource_kind,
            hourly_capacity=decimal_value(
                raw.get("hourly_capacity"), "hourly_capacity", minimum=Decimal("0.001")
            ),
            delay_basis_points=loss,
            response_minutes=positive_integer(raw.get("response_minutes"), "response_minutes"),
        )


@dataclass(frozen=True, slots=True)
class PreservationResourceLot:
    preservation_resource_lot_id: str
    center_id: str
    preservation_resource_kind: str
    grade: str
    quantity_units: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PreservationResourceLot":
        preservation_resource_kind = required_text(raw.get("preservation_resource_kind"), "preservation_resource_kind", 32)
        if preservation_resource_kind not in RESOURCE_KINDS:
            raise ValidationFailed("preservation_resource_kind 不是受支持的电源类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            preservation_resource_lot_id=identifier(raw.get("preservation_resource_lot_id"), "preservation_resource_lot_id"),
            center_id=identifier(raw.get("center_id"), "center_id"),
            preservation_resource_kind=preservation_resource_kind,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_units=decimal_value(
                raw.get("quantity_units"), "quantity_units", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    dispatch_id: str
    corridor_id: str
    specimen_event_id: str
    duty_date: str
    requested_units: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DispatchRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            dispatch_id=identifier(raw.get("dispatch_id"), "dispatch_id"),
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            specimen_event_id=identifier(raw.get("specimen_event_id"), "specimen_event_id"),
            duty_date=date_text(raw.get("duty_date"), "duty_date"),
            requested_units=decimal_value(
                raw.get("requested_units"), "requested_units", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ResponseScenario:
    scenario_id: str
    name: str
    risk_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponseScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_road_corridors = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            risk_index_drop_percent=decimal_value(
                raw.get("risk_index_drop_percent", 0),
                "risk_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_road_corridors,
            demand_changes=parsed_demand,
        )
