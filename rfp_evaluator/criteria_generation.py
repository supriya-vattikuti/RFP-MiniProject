"""Cohere-driven generation of an RFQ-specific evaluation framework."""

from __future__ import annotations

from typing import Any, Protocol

from .criteria_validation import FrameworkValidationError, parse_and_validate_framework
from .prompts import CRITERIA_GENERATION_PROMPT, build_criteria_generation_prompt


class LLMClient(Protocol):
    def generate(self, prompt: str) -> str: ...


def generate_evaluation_framework(
    rfq_text: str,
    rfq_requirements: list[dict[str, Any]],
    cohere_client: LLMClient,
    prompt: str = CRITERIA_GENERATION_PROMPT,
) -> dict[str, Any]:
    request = build_criteria_generation_prompt(rfq_text, rfq_requirements, prompt)
    response = cohere_client.generate(request)
    try:
        framework = parse_and_validate_framework(response)
    except FrameworkValidationError as exc:
        correction = (
            request
            + "\nYour previous framework was invalid: "
            + str(exc)
            + ". Return a complete corrected JSON framework with unique criteria, "
            + "valid positive weights, descriptions, score maxima, and guidance."
        )
        framework = parse_and_validate_framework(cohere_client.generate(correction))
    if framework["weight_total"] != 100:
        correction = (
            request
            + "\nYour previous active weights summed to "
            + f"{framework['weight_total']:g}%, not 100%. Return a corrected complete JSON "
            + "framework. Preserve RFQ grounding and make active weights total exactly 100%."
        )
        framework = parse_and_validate_framework(cohere_client.generate(correction))
    return framework
