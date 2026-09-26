"""按预注册协议执行确定性观察一致性分析。"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Iterable, Mapping

from .contracts import Indicator, EvidenceItem, EvidenceProtocol
from .numeric import summarize, wilson_interval


ALGORITHM_VERSION = "device-reviews-analysis/1"


def _quantile(values: list[Decimal], probability: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("分位数输入不能为空")
    position = probability * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] * (Decimal(1) - fraction) + ordered[upper] * fraction


def bootstrap_mean_interval(
    values: Iterable[Decimal], *, seed: int, samples: int
) -> tuple[Decimal, Decimal]:
    data = tuple(values)
    if not data:
        raise ValueError("bootstrap 至少需要一个样本")
    generator = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(samples):
        total = sum((data[generator.randrange(len(data))] for _ in data), Decimal(0))
        means.append(total / Decimal(len(data)))
    return _quantile(means, Decimal("0.025")), _quantile(means, Decimal("0.975"))


def _indicator_result(indicator: Indicator, values: list[Decimal], seed: int, samples: int) -> dict[str, object]:
    summary = summarize(values)
    result: dict[str, object] = summary.as_dict()
    if indicator.kind == "binary":
        successes = sum(int(value) for value in values)
        interval = wilson_interval(successes, len(values))
        result.update({
            "successes": successes,
            "proportion": successes / len(values),
            "wilson_lower": interval.lower,
            "wilson_upper": interval.upper,
        })
    else:
        lower, upper = bootstrap_mean_interval(values, seed=seed, samples=samples)
        result.update({
            "bootstrap_lower": format(lower, "f"),
            "bootstrap_upper": format(upper, "f"),
        })
    return result


def analyze(evidence_protocol: EvidenceProtocol, evidence_items: Iterable[EvidenceItem]) -> dict[str, object]:
    all_evidence_items = tuple(evidence_items)
    included = tuple(item for item in all_evidence_items if item.excluded_reason is None)
    strata: dict[str, dict[str, object]] = {}
    insufficient: list[dict[str, object]] = []
    for evidence_group_index, evidence_group in enumerate(evidence_protocol.strata):
        rows = [item for item in included if item.evidence_group_key == evidence_group.key]
        coverage = {"actual": len(rows), "required": evidence_group.required_reviews, "complete": len(rows) >= evidence_group.required_reviews}
        if not coverage["complete"]:
            insufficient.append({"evidence_group": evidence_group.key, **coverage})
        indicators: dict[str, object] = {}
        for indicator_index, indicator in enumerate(evidence_protocol.indicators):
            values = [item.indicators[indicator.key] for item in rows]
            if values:
                indicators[indicator.key] = _indicator_result(
                    indicator,
                    values,
                    evidence_protocol.seed + evidence_group_index * 1009 + indicator_index,
                    evidence_protocol.bootstrap_samples,
                )
        strata[evidence_group.key] = {"coverage": coverage, "indicators": indicators}

    aggregate: dict[str, object] = {}
    for indicator in evidence_protocol.indicators:
        available = [
            (evidence_group.key, strata[evidence_group.key]["indicators"].get(indicator.key))
            for evidence_group in evidence_protocol.strata
        ]
        if any(value is None for _, value in available):
            aggregate[indicator.key] = {"available": False, "reason": "至少一个预注册分层无有效样本"}
            continue
        if indicator.kind == "binary":
            weighted = sum(
                evidence_protocol.evidence_group_weights[key] * Decimal(str(value["proportion"]))
                for key, value in available
            )
            lower = sum(
                evidence_protocol.evidence_group_weights[key] * Decimal(str(value["wilson_lower"]))
                for key, value in available
            )
            aggregate[indicator.key] = {
                "available": True,
                "weighted_mean": format(weighted, "f"),
                "wilson_lower": format(lower, "f"),
            }
        else:
            weighted = sum(
                evidence_protocol.evidence_group_weights[key] * Decimal(str(value["mean"]))
                for key, value in available
            )
            aggregate[indicator.key] = {"available": True, "weighted_mean": format(weighted, "f")}

    rule_results: list[dict[str, object]] = []
    for raw_rule in evidence_protocol.admission_rules:
        rule = dict(raw_rule)
        indicator_key = str(rule["indicator"])
        statistic = str(rule.get("statistic", "weighted_mean"))
        indicator_result = aggregate.get(indicator_key, {})
        raw_value = indicator_result.get(statistic) if isinstance(indicator_result, Mapping) else None
        threshold = Decimal(str(rule["threshold"]))
        passed = False
        if raw_value is not None:
            value = Decimal(str(raw_value))
            passed = value >= threshold if rule["operator"] == "gte" else value <= threshold
        rule_results.append({
            "indicator": indicator_key,
            "statistic": statistic,
            "operator": rule["operator"],
            "threshold": format(threshold, "f"),
            "actual": None if raw_value is None else str(raw_value),
            "passed": passed,
        })
    conclusion = "insufficient" if insufficient else ("pass" if all(item["passed"] for item in rule_results) else "fail")
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "seed": evidence_protocol.seed,
        "bootstrap_samples": evidence_protocol.bootstrap_samples,
        "included_count": len(included),
        "excluded_count": len(all_evidence_items) - len(included),
        "strata": strata,
        "aggregate": aggregate,
        "rules": rule_results,
        "insufficient": insufficient,
        "conclusion": conclusion,
    }
