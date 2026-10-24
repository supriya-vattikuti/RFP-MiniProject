"""RFQ framework-generation and supplier-evaluation prompt construction."""

from __future__ import annotations

import json
from typing import Any


CRITERIA_GENERATION_PROMPT = """You are helping a procurement analyst derive an evaluation framework.
Use ONLY requirements and priorities evidenced in the uploaded RFQ.
Identify relevant criteria and assign relative weights based only on the RFQ.
Do not use predefined weights, standard procurement weights, or fixed categories.
Weights for active criteria must sum to exactly 100. Explain each weight using RFQ
traceability. Include evidence guidance and relevant page references. Do not invent
requirements. Return only a JSON object with this schema:
{{
  "rfq_summary": "concise summary",
  "criteria": [
    {{
      "criterion_id": "unique stable id",
      "name": "criterion",
      "description": "what it evaluates",
      "weight": 25,
      "max_score": 10,
      "active": true,
      "evaluation_guidance": ["evidence to consider"],
      "rfq_traceability": [
        {{"requirement": "RFQ requirement", "page_reference": "Page 1"}}
      ],
      "weight_rationale": "reason grounded in RFQ"
    }}
  ]
}}

RFQ requirements:
{rfq_requirements}

RFQ content with page labels:
{rfq_text}
"""

DEFAULT_PROMPT_TEMPLATE = """Evaluate the supplier proposal using the approved evaluation framework.
Use the RFQ requirements as the source of truth. Assess evidence only; do not invent
facts or recommend a winner. Score every approved criterion from 0 to its max_score.
Return only valid JSON with this structure:
{{
  "evaluations": [
    {{
      "criterion_id": "approved criterion id",
      "criterion": "approved criterion name",
      "score": 0,
      "evidence": ["Page 1: evidence quotation or fact"],
      "page_reference": "Page 1",
      "reasoning": "evidence-based assessment",
      "confidence": 0.0
    }}
  ],
  "risks": [],
  "missing_information": [],
  "overall_summary": "evidence-based summary, no winner recommendation"
}}

RFQ requirements:
{rfq_requirements}

Approved criteria and weightage:
{evaluation_criteria}

Supplier proposal with page labels:
{supplier_proposal}
"""


def build_evaluation_prompt(
    criteria: list[dict[str, Any]],
    proposal_text: str,
    template: str = DEFAULT_PROMPT_TEMPLATE,
    rfq_requirements: list[dict[str, Any]] | None = None,
) -> str:
    criteria_json = json.dumps(
        [
            {
                "criterion_id": item["criterion_id"],
                "name": item["name"],
                "description": item.get("description", ""),
                "weight": item["weight"],
                "maximum_score": item.get("maximum_score", item.get("max_score")),
                "evaluation_guidance": item.get("evaluation_guidance", []),
                "rfq_traceability": item.get("rfq_traceability", []),
            }
            for item in criteria
            if item.get("active", True)
        ],
        ensure_ascii=False,
    )
    requirements_json = json.dumps(rfq_requirements or [], ensure_ascii=False)
    return (
        template.replace("{evaluation_criteria}", criteria_json)
        .replace("{rfq_requirements}", requirements_json)
        .replace("{supplier_proposal}", proposal_text)
        .replace("{criteria_json}", criteria_json)
        .replace("{proposal_text}", proposal_text)
    )


def build_criteria_generation_prompt(
    rfq_text: str,
    rfq_requirements: list[dict[str, Any]],
    template: str = CRITERIA_GENERATION_PROMPT,
) -> str:
    return (
        template.replace(
            "{rfq_requirements}", json.dumps(rfq_requirements, ensure_ascii=False)
        )
        .replace("{rfq_text}", rfq_text)
    )
