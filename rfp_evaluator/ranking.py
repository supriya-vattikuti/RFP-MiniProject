"""Deterministic supplier ranking and tie-break explanations."""

from __future__ import annotations

from datetime import date
from typing import Any


def _date_key(value: str) -> date:
    return date.fromisoformat(value)


def rank_suppliers(suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        suppliers,
        key=lambda supplier: (
            -float(supplier["absolute_weighted_score"]),
            -float(supplier["ppi"]),
            _date_key(supplier["submission_date"]),
            -float(supplier["experience_rating"]),
            supplier["supplier_name"].casefold(),
        ),
    )
    for index, supplier in enumerate(ranked, start=1):
        supplier["rank"] = index
        if index == 1:
            supplier["tie_break_explanation"] = (
                "Ranked first by absolute weighted score; PPI, submission date, "
                "experience rating, and supplier name resolve ties in that order."
            )
            continue
        previous = ranked[index - 2]
        reasons = []
        if supplier["absolute_weighted_score"] != previous["absolute_weighted_score"]:
            reasons.append("higher absolute weighted score")
        elif supplier["ppi"] != previous["ppi"]:
            reasons.append("higher PPI")
        elif supplier["submission_date"] != previous["submission_date"]:
            reasons.append("earlier submission date")
        elif supplier["experience_rating"] != previous["experience_rating"]:
            reasons.append("higher experience rating")
        else:
            reasons.append("supplier name in ascending order")
        supplier["tie_break_explanation"] = (
            f"Ranked below {previous['supplier_name']} due to {reasons[0]}."
        )
    return ranked
