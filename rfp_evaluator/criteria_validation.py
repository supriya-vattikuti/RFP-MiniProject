"""Structural validation for RFQ-generated evaluation frameworks."""

from __future__ import annotations

import json
import math
from typing import Any


class FrameworkValidationError(ValueError):
    """Raised when Cohere returns an unusable evaluation framework."""


def parse_and_validate_framework(response_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(response_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise FrameworkValidationError("Cohere returned malformed JSON for the framework.") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("criteria"), list):
        raise FrameworkValidationError("Framework JSON must contain a criteria array.")
    source = parsed["criteria"]
    if not source:
        raise FrameworkValidationError("Cohere returned no evaluation criteria.")

    criteria: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, item in enumerate(source, start=1):
        if not isinstance(item, dict):
            raise FrameworkValidationError(f"Criterion {index} is not an object.")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise FrameworkValidationError(f"Criterion {index} has no name.")
        if not isinstance(description, str) or not description.strip():
            raise FrameworkValidationError(f"{name}: a description is required.")
        criterion_id = str(item.get("criterion_id", index)).strip()
        if not criterion_id or criterion_id in seen_ids:
            raise FrameworkValidationError("Criterion IDs must be present and unique.")
        normalized_name = name.strip().casefold()
        if normalized_name in seen_names:
            raise FrameworkValidationError(f"Duplicate criterion name: {name.strip()}.")
        seen_ids.add(criterion_id)
        seen_names.add(normalized_name)

        weight = _number(item.get("weight"))
        maximum = _number(item.get("max_score", item.get("maximum_score")))
        if weight is None or weight <= 0 or weight > 100:
            raise FrameworkValidationError(f"{name.strip()}: weight must be greater than 0 and at most 100.")
        if maximum is None or maximum <= 0 or maximum > 100:
            raise FrameworkValidationError(f"{name.strip()}: max_score must be greater than 0 and at most 100.")
        guidance = item.get("evaluation_guidance", [])
        if isinstance(guidance, str):
            guidance = [guidance]
        if not isinstance(guidance, list) or not any(
            isinstance(entry, str) and entry.strip() for entry in guidance
        ):
            raise FrameworkValidationError(f"{name.strip()}: evaluation guidance is required.")
        traceability = item.get("rfq_traceability", [])
        if not isinstance(traceability, list):
            raise FrameworkValidationError(f"{name.strip()}: RFQ traceability must be a list.")
        active = item.get("active", True)
        if not isinstance(active, bool):
            raise FrameworkValidationError(f"{name.strip()}: active must be true or false.")
        rationale = item.get("weight_rationale", item.get("weight_reasoning", ""))
        criteria.append(
            {
                "criterion_id": criterion_id,
                "name": name.strip(),
                "description": description.strip(),
                "weight": weight,
                "maximum_score": maximum,
                "active": active,
                "evaluation_guidance": [
                    entry.strip() for entry in guidance if isinstance(entry, str) and entry.strip()
                ],
                "rfq_traceability": [
                    {
                        "requirement": str(trace.get("requirement", "")).strip(),
                        "page_reference": str(trace.get("page_reference", "")).strip(),
                    }
                    for trace in traceability
                    if isinstance(trace, dict)
                ],
                "weight_rationale": str(rationale).strip(),
            }
        )
    return {
        "rfq_summary": str(parsed.get("rfq_summary", "")).strip(),
        "criteria": criteria,
        "weight_total": round(sum(item["weight"] for item in criteria if item["active"]), 4),
    }


def validate_approved_framework(
    criteria: list[dict[str, Any]], *, require_traceability: bool = False
) -> None:
    if not criteria:
        raise FrameworkValidationError("Add at least one active criterion before approval.")
    names: set[str] = set()
    identifiers: set[str] = set()
    for item in criteria:
        if not item.get("active"):
            continue
        name = str(item.get("name", "")).strip()
        criterion_id = str(item.get("criterion_id", "")).strip()
        if not name:
            raise FrameworkValidationError("Every active criterion must have a name.")
        if name.casefold() in names:
            raise FrameworkValidationError(f"Duplicate criterion name: {name}.")
        if not criterion_id or criterion_id in identifiers:
            raise FrameworkValidationError("Active criterion IDs must be unique.")
        if not str(item.get("description", "")).strip():
            raise FrameworkValidationError(f"{name}: a description is required.")
        if not item.get("evaluation_guidance"):
            raise FrameworkValidationError(f"{name}: evaluation guidance is required.")
        traceability = item.get("rfq_traceability", [])
        if require_traceability and not any(
            isinstance(trace, dict)
            and str(trace.get("requirement", "")).strip()
            for trace in traceability
        ):
            raise FrameworkValidationError(
                f"{name}: add RFQ traceability before approving the framework."
            )
        weight = _number(item.get("weight"))
        maximum = _number(item.get("maximum_score", item.get("max_score")))
        if weight is None or weight <= 0:
            raise FrameworkValidationError(f"{name}: active weight must be greater than 0.")
        if maximum is None or maximum <= 0:
            raise FrameworkValidationError(f"{name}: maximum score must be greater than 0.")
        names.add(name.casefold())
        identifiers.add(criterion_id)
    total = sum(
        float(item["weight"]) for item in criteria if item.get("active")
    )
    if not math.isclose(total, 100.0, abs_tol=1e-6):
        raise FrameworkValidationError(
            f"Active criteria weight {total:g}%. Weights must total exactly 100% before approval."
        )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
