"""Deterministic weighted scoring and criterion benchmarks."""

from __future__ import annotations

from typing import Any


def calculate_absolute_score(evaluations: list[dict[str, Any]]) -> float:
    return round(
        sum(
            (float(item["score"]) / float(item["maximum_score"]))
            * float(item["weight"])
            for item in evaluations
        ),
        4,
    )


def calculate_benchmarks(
    suppliers: list[dict[str, Any]],
) -> dict[str, float]:
    benchmark: dict[str, float] = {}
    for supplier in suppliers:
        for item in supplier["evaluations"]:
            criterion = item["criterion"]
            benchmark[criterion] = max(
                benchmark.get(criterion, 0.0), float(item["score"])
            )
    return benchmark


def add_relative_metrics(
    suppliers: list[dict[str, Any]],
    benchmarks: dict[str, float],
) -> None:
    for supplier in suppliers:
        relative_values: list[tuple[float, float]] = []
        for item in supplier["evaluations"]:
            best = benchmarks[item["criterion"]]
            score = float(item["score"])
            relative = score / best * 100 if best > 0 else 0.0
            item["benchmark"] = best
            item["gap"] = round(score - best, 4)
            item["relative_percent"] = round(relative, 4)
            relative_values.append((relative, float(item["weight"])))
        total_weight = sum(weight for _, weight in relative_values)
        supplier["ppi"] = round(
            sum(value * weight for value, weight in relative_values) / total_weight
            if total_weight
            else 0.0,
            4,
        )
        supplier["absolute_weighted_score"] = calculate_absolute_score(
            supplier["evaluations"]
        )
