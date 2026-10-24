"""Validation and safe normalization of model-produced criterion scores."""

from __future__ import annotations

import json
import math
import re
from typing import Any


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_evaluation(
    response_text: str,
    criteria: list[dict[str, Any]],
    *,
    strict: bool = False,
    valid_page_numbers: set[int] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        parsed = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        if strict:
            raise ValueError("Cohere returned malformed JSON for supplier evaluation.")
        parsed = {}
        warnings.append("The model response was not valid JSON; all scores were set to 0.")

    entries = parsed.get("evaluations") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        if strict:
            raise ValueError("Supplier evaluation JSON must contain an evaluations array.")
        entries = []
        if parsed:
            warnings.append("The response did not contain an evaluations array.")

    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("criterion"), str):
            key = entry["criterion"].strip().casefold()
            if key in by_name and strict:
                raise ValueError(f"Cohere returned duplicate evaluation for {entry['criterion']}.")
            by_name[key] = entry

    normalized: list[dict[str, Any]] = []
    expected_names = {c["name"].strip().casefold() for c in criteria}
    unknown = sorted(key for key in by_name if key not in expected_names)
    if unknown and strict:
        raise ValueError(f"Cohere returned unknown criteria: {', '.join(unknown)}.")
    for criterion in criteria:
        name = criterion["name"]
        maximum = float(criterion.get("maximum_score", criterion.get("max_score")))
        entry = by_name.get(name.strip().casefold())
        if entry is None:
            if strict:
                raise ValueError(f"Cohere omitted required criterion: {name}.")
            warnings.append(f"Missing evaluation for {name}; score set to 0.")
            entry = {}

        returned_id = entry.get("criterion_id")
        if strict and returned_id is None:
            raise ValueError(f"Cohere omitted the criterion ID for {name}.")
        if strict and str(returned_id) != str(criterion["criterion_id"]):
            raise ValueError(f"Cohere returned an incorrect criterion ID for {name}.")
        score = _finite_number(entry.get("score"))
        if score is None:
            if strict:
                raise ValueError(f"Cohere returned an invalid score for {name}.")
            score = 0.0
            warnings.append(f"Invalid or missing score for {name}; score set to 0.")
        elif score < 0 or score > maximum:
            if strict:
                raise ValueError(f"Score for {name} must be between 0 and {maximum:g}.")
            score = min(max(score, 0.0), maximum)
            warnings.append(f"Score for {name} was out of range and was clamped.")

        evidence_value = entry.get("evidence")
        if isinstance(evidence_value, str):
            evidence = [evidence_value] if evidence_value.strip() else []
        elif isinstance(evidence_value, list):
            evidence = [str(item).strip() for item in evidence_value if str(item).strip()]
            if strict and not evidence:
                raise ValueError(f"Cohere returned empty evidence for {name}.")
        else:
            if strict:
                raise ValueError(f"Cohere omitted evidence for {name}.")
            evidence = []
            warning = (
                f"Missing evidence for {name}; evidence was omitted."
                if evidence_value is None
                else f"Malformed evidence for {name}; evidence was omitted."
            )
            warnings.append(warning)
        if strict and not evidence:
            raise ValueError(f"Cohere returned empty evidence for {name}.")

        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            if strict:
                raise ValueError(f"Cohere omitted reasoning for {name}.")
            warnings.append(f"Missing reasoning for {name}.")
        confidence = _finite_number(entry.get("confidence"))
        if strict and (confidence is None or not 0 <= confidence <= 1):
            raise ValueError(f"Cohere returned invalid confidence for {name}; expected 0 to 1.")
        page_reference = entry.get("page_reference", "")
        if strict and (
            not isinstance(page_reference, str) or not page_reference.strip()
        ):
            raise ValueError(f"Cohere omitted the page reference for {name}.")
        if strict and valid_page_numbers is not None:
            cited_pages = {
                int(number) for number in re.findall(r"\d+", str(page_reference))
            }
            if not cited_pages or not cited_pages.issubset(valid_page_numbers):
                raise ValueError(f"Cohere returned an invalid page reference for {name}.")

        normalized.append(
            {
                "criterion_id": criterion["criterion_id"],
                "criterion": name,
                "score": round(score, 4),
                "ai_score": round(score, 4),
                "maximum_score": maximum,
                "weight": float(criterion["weight"]),
                "evidence": evidence,
                "page_reference": str(page_reference).strip(),
                "reasoning": reasoning.strip() if isinstance(reasoning, str) else "",
                "confidence": confidence,
                "weighted_score": round((score / maximum) * float(criterion["weight"]), 4),
            }
        )

    if unknown and not strict:
        warnings.append(f"Ignored unknown criteria: {', '.join(unknown)}.")
    risks = parsed.get("risks", []) if isinstance(parsed, dict) else []
    missing = parsed.get("missing_information", []) if isinstance(parsed, dict) else []
    overall_summary = (
        parsed.get("overall_summary", "") if isinstance(parsed, dict) else ""
    )
    if strict and (
        not isinstance(overall_summary, str) or not overall_summary.strip()
    ):
        raise ValueError("Cohere omitted the overall evaluation summary.")
    for label, values in (("risks", risks), ("missing_information", missing)):
        if not isinstance(values, list):
            if strict:
                raise ValueError(f"Cohere returned malformed {label}.")
            if label == "risks":
                risks = []
            else:
                missing = []
    return {
        "evaluations": normalized,
        "warnings": warnings,
        "risks": risks,
        "missing_information": missing,
        "overall_summary": str(overall_summary).strip(),
    }
