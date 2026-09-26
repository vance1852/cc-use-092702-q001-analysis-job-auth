"""观察材料采集规范和观察记录记录的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    """一个需要单独覆盖的校准环境分层。"""

    key: str
    label: str
    required_reviews: int

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "EvidenceGroup":
        data = _require_mapping(raw, path)
        required_reviews = data.get("required_reviews")
        if isinstance(required_reviews, bool) or not isinstance(required_reviews, int):
            raise ValidationError(f"{path}.required_reviews 必须是整数")
        if required_reviews <= 0:
            raise ValidationError(f"{path}.required_reviews 必须大于零")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            required_reviews=required_reviews,
        )


@dataclass(frozen=True, slots=True)
class Indicator:
    """协议中声明的一个可观察记录指标。"""

    key: str
    label: str
    kind: str
    unit: str | None
    direction: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Indicator":
        data = _require_mapping(raw, path)
        kind = _required_text(data.get("kind"), f"{path}.kind")
        if kind not in {"binary", "continuous", "count"}:
            raise ValidationError(f"{path}.kind 不受支持")
        direction = _required_text(data.get("direction"), f"{path}.direction")
        if direction not in {"higher", "lower"}:
            raise ValidationError(f"{path}.direction 必须是 higher 或 lower")
        unit = _optional_text(data.get("unit"), f"{path}.unit")
        if kind == "binary" and unit is not None:
            raise ValidationError(f"{path}.unit 对二元指标必须为空")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            kind=kind,
            unit=unit,
            direction=direction,
        )


@dataclass(frozen=True, slots=True)
class EvidenceProtocol:
    """一次校准所依据的不可歧义协议版本。"""

    evidence_protocol_id: str
    version: int
    title: str
    task_family: str
    strata: tuple[EvidenceGroup, ...]
    indicators: tuple[Indicator, ...]
    evidence_group_weights: Mapping[str, Decimal]
    seed: int
    bootstrap_samples: int
    admission_rules: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceProtocol":
        data = _require_mapping(raw, "evidence_protocol")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationError("evidence_protocol.version 必须是正整数")
        strata = tuple(
            EvidenceGroup.from_dict(item, f"evidence_protocol.strata[{index}]")
            for index, item in enumerate(_require_sequence(data.get("strata"), "evidence_protocol.strata"))
        )
        indicators = tuple(
            Indicator.from_dict(item, f"evidence_protocol.indicators[{index}]")
            for index, item in enumerate(_require_sequence(data.get("indicators"), "evidence_protocol.indicators"))
        )
        if not strata:
            raise ValidationError("evidence_protocol.strata 不能为空")
        if not indicators:
            raise ValidationError("evidence_protocol.indicators 不能为空")
        if len({item.key for item in strata}) != len(strata):
            raise ValidationError("evidence_protocol.strata.key 不能重复")
        if len({item.key for item in indicators}) != len(indicators):
            raise ValidationError("evidence_protocol.indicators.key 不能重复")
        raw_weights = _require_mapping(data.get("evidence_group_weights"), "evidence_protocol.evidence_group_weights")
        if set(raw_weights) != {item.key for item in strata}:
            raise ValidationError("evidence_protocol.evidence_group_weights 必须覆盖全部且仅覆盖已声明分层")
        weights = {
            key: _decimal(value, f"evidence_protocol.evidence_group_weights.{key}")
            for key, value in raw_weights.items()
        }
        if any(value <= 0 for value in weights.values()):
            raise ValidationError("evidence_protocol.evidence_group_weights 必须全部大于零")
        if sum(weights.values(), Decimal(0)) != Decimal(1):
            raise ValidationError("evidence_protocol.evidence_group_weights 之和必须为 1")
        seed = data.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValidationError("evidence_protocol.seed 必须是整数")
        bootstrap_samples = data.get("bootstrap_samples")
        if (
            isinstance(bootstrap_samples, bool)
            or not isinstance(bootstrap_samples, int)
            or bootstrap_samples < 100
            or bootstrap_samples > 100000
        ):
            raise ValidationError("evidence_protocol.bootstrap_samples 必须在 100 到 100000 之间")
        rules = tuple(
            _require_mapping(item, f"evidence_protocol.admission_rules[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("admission_rules"), "evidence_protocol.admission_rules")
            )
        )
        if not rules:
            raise ValidationError("evidence_protocol.admission_rules 不能为空")
        for index, rule in enumerate(rules):
            indicator = _required_text(rule.get("indicator"), f"evidence_protocol.admission_rules[{index}].indicator")
            if indicator not in {item.key for item in indicators}:
                raise ValidationError(f"evidence_protocol.admission_rules[{index}].indicator 未声明")
            operator = _required_text(rule.get("operator"), f"evidence_protocol.admission_rules[{index}].operator")
            if operator not in {"gte", "lte"}:
                raise ValidationError(f"evidence_protocol.admission_rules[{index}].operator 不受支持")
            _decimal(rule.get("threshold"), f"evidence_protocol.admission_rules[{index}].threshold")
        return cls(
            evidence_protocol_id=_required_text(data.get("evidence_protocol_id"), "evidence_protocol.evidence_protocol_id"),
            version=version,
            title=_required_text(data.get("title"), "evidence_protocol.title"),
            task_family=_required_text(data.get("task_family"), "evidence_protocol.task_family"),
            strata=strata,
            indicators=indicators,
            evidence_group_weights=weights,
            seed=seed,
            bootstrap_samples=bootstrap_samples,
            admission_rules=rules,
        )

    @property
    def indicator_map(self) -> dict[str, Indicator]:
        return {indicator.key: indicator for indicator in self.indicators}

    @property
    def evidence_group_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.strata)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """一条已结构化的标本事件观察记录。"""

    source_batch: str
    source_row: str
    device_id: str
    evidence_protocol_id: str
    evidence_protocol_version: int
    evidence_group_key: str
    observed_at: str
    indicators: Mapping[str, Decimal]
    excluded_reason: str | None

    @classmethod
    def from_dict(cls, raw: object, evidence_protocol: EvidenceProtocol) -> "EvidenceItem":
        data = _require_mapping(raw, "evidence_item")
        evidence_protocol_id = _required_text(data.get("evidence_protocol_id"), "evidence_item.evidence_protocol_id")
        evidence_protocol_version = data.get("evidence_protocol_version")
        if evidence_protocol_id != evidence_protocol.evidence_protocol_id or evidence_protocol_version != evidence_protocol.version:
            raise ValidationError("观察记录引用的协议版本与当前协议不一致")
        evidence_group_key = _required_text(data.get("evidence_group_key"), "evidence_item.evidence_group_key")
        if evidence_group_key not in evidence_protocol.evidence_group_keys:
            raise ValidationError("evidence_item.evidence_group_key 未在协议中声明")
        indicator_data = _require_mapping(data.get("indicators"), "evidence_item.indicators")
        expected = evidence_protocol.indicator_map
        missing = sorted(set(expected) - set(indicator_data))
        extra = sorted(set(indicator_data) - set(expected))
        if missing or extra:
            raise ValidationError(f"观察记录指标不匹配：缺少 {missing}，多出 {extra}")
        parsed: dict[str, Decimal] = {}
        for key, value in indicator_data.items():
            indicator = expected[key]
            number = _decimal(value, f"evidence_item.indicators.{key}")
            if indicator.kind == "binary" and number not in {Decimal(0), Decimal(1)}:
                raise ValidationError(f"evidence_item.indicators.{key} 必须是 0 或 1")
            if indicator.kind == "count" and number != number.to_integral_value():
                raise ValidationError(f"evidence_item.indicators.{key} 必须是整数")
            parsed[key] = number
        return cls(
            source_batch=_required_text(data.get("source_batch"), "evidence_item.source_batch"),
            source_row=_required_text(data.get("source_row"), "evidence_item.source_row"),
            device_id=_required_text(data.get("device_id"), "evidence_item.device_id"),
            evidence_protocol_id=evidence_protocol_id,
            evidence_protocol_version=evidence_protocol.version,
            evidence_group_key=evidence_group_key,
            observed_at=_required_text(data.get("observed_at"), "evidence_item.observed_at"),
            indicators=parsed,
            excluded_reason=_optional_text(data.get("excluded_reason"), "evidence_item.excluded_reason"),
        )
