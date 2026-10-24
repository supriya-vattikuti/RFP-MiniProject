"""Application orchestration for a complete RFP evaluation run."""

from __future__ import annotations

import uuid
import hashlib
from datetime import date
from typing import Any

from . import database
from .benchmarking import add_relative_metrics, calculate_benchmarks
from .criteria_generation import LLMClient, generate_evaluation_framework
from .criteria_validation import validate_approved_framework
from .pdf_extraction import extract_pdf_pages, format_page_evidence
from .prompts import (
    CRITERIA_GENERATION_PROMPT,
    DEFAULT_PROMPT_TEMPLATE,
    build_evaluation_prompt,
)
from .rfq_parser import parse_rfq_document
from .ranking import rank_suppliers
from .scoring import add_relative_metrics, calculate_benchmarks
from .validation import normalize_evaluation


def validate_active_criteria(
    criteria: list[dict[str, Any]], *, require_traceability: bool = False
) -> None:
    validate_approved_framework(criteria, require_traceability=require_traceability)


def parse_and_store_rfq(
    file_name: str,
    pdf_bytes: bytes,
    database_file: str | None = None,
) -> dict[str, Any]:
    parsed = parse_rfq_document(pdf_bytes)
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    rfq_id = database.save_rfq(
        str(uuid.uuid4()),
        file_name,
        file_hash,
        parsed["pages"],
        parsed["requirements"],
        database_file,
    )
    rfq = database.get_rfq(rfq_id, database_file)
    return rfq  # type: ignore[return-value]


def generate_rfq_framework(
    rfq_id: str,
    evaluator: LLMClient,
    criteria_prompt: str = CRITERIA_GENERATION_PROMPT,
    database_file: str | None = None,
) -> dict[str, Any]:
    rfq = database.get_rfq(rfq_id, database_file)
    if rfq is None:
        raise ValueError("The selected RFQ could not be found.")
    framework = generate_evaluation_framework(
        rfq["raw_text"],
        rfq["requirements"],
        evaluator,
        criteria_prompt,
    )
    version = database.save_criteria_version(
        rfq_id,
        framework["criteria"],
        framework["rfq_summary"],
        path=database_file,
    )
    return {
        "rfq": rfq,
        "framework": version,
        "framework_warnings": (
            [f"Active weights currently total {framework['weight_total']:g}%; edit to 100% and approve."]
            if framework["weight_total"] != 100
            else []
        ),
    }


def process_rfq_upload(
    file_name: str,
    pdf_bytes: bytes,
    evaluator: LLMClient,
    criteria_prompt: str = CRITERIA_GENERATION_PROMPT,
    database_file: str | None = None,
) -> dict[str, Any]:
    rfq = parse_and_store_rfq(file_name, pdf_bytes, database_file)
    return generate_rfq_framework(
        rfq["rfq_id"], evaluator, criteria_prompt, database_file
    )


def evaluate_run(
    suppliers: list[dict[str, Any]],
    evaluator: Any,
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
    database_file: str | None = None,
    *,
    criteria: list[dict[str, Any]],
    rfq_id: str,
    criteria_version_id: str,
    prompt_version_id: str,
    rfq_requirements: list[dict[str, Any]],
    model: str = "",
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    if not suppliers:
        raise ValueError("Upload at least one supplier PDF.")
    validate_active_criteria(criteria, require_traceability=bool(rfq_requirements))
    supplier_names = [supplier["supplier_name"].strip().casefold() for supplier in suppliers]
    if len(supplier_names) != len(set(supplier_names)):
        raise ValueError("Supplier names must be unique within an evaluation run.")
    for supplier in suppliers:
        if not supplier["supplier_name"].strip():
            raise ValueError("Every supplier must have a name.")
        date.fromisoformat(supplier["submission_date"])
        experience = float(supplier["experience_rating"])
        if not 0 <= experience <= 5:
            raise ValueError("Experience ratings must be between 0 and 5.")

    run_id = str(uuid.uuid4())
    database.create_run(
        run_id,
        criteria,
        prompt_template,
        database_file,
        rfq_id=rfq_id,
        criteria_version_id=criteria_version_id,
        prompt_version_id=prompt_version_id,
        model=model,
    )
    evaluated: list[dict[str, Any]] = []
    document_texts: dict[str, str] = {}
    for index, supplier in enumerate(suppliers, start=1):
        if progress_callback:
            progress_callback(index, len(suppliers), supplier["supplier_name"], "Processing")
        pages = extract_pdf_pages(supplier["pdf_bytes"])
        proposal_text = format_page_evidence(pages)
        document_texts[supplier["supplier_name"].strip()] = proposal_text
        prompt = build_evaluation_prompt(
            criteria, proposal_text, prompt_template, rfq_requirements
        )
        raw_response = evaluator.evaluate(prompt)
        normalized = normalize_evaluation(
            raw_response,
            criteria,
            strict=True,
            valid_page_numbers={int(page["page"]) for page in pages},
        )
        evaluated.append(
            {
                "supplier_name": supplier["supplier_name"].strip(),
                "submission_date": supplier["submission_date"],
                "experience_rating": float(supplier["experience_rating"]),
                "evaluations": normalized["evaluations"],
                "warnings": normalized["warnings"],
                "risks": normalized["risks"],
                "missing_information": normalized["missing_information"],
                "overall_summary": normalized["overall_summary"],
            }
        )
        if progress_callback:
            progress_callback(index, len(suppliers), supplier["supplier_name"], "Evaluated")

    benchmarks = calculate_benchmarks(evaluated)
    add_relative_metrics(evaluated, benchmarks)
    ranked = rank_suppliers(evaluated)
    for supplier in ranked:
        database.save_supplier_result(
            run_id,
            supplier["supplier_name"],
            supplier["submission_date"],
            supplier["experience_rating"],
            supplier,
            database_file,
            document_name=supplier.get("file_name", ""),
            document_text=document_texts[supplier["supplier_name"]],
        )
    return {
        "run_id": run_id,
        "rfq_id": rfq_id,
        "criteria_version_id": criteria_version_id,
        "prompt_version_id": prompt_version_id,
        "model": model,
        "criteria": criteria,
        "benchmarks": benchmarks,
        "suppliers": ranked,
    }


def override_supplier_score(
    run: dict[str, Any],
    supplier_name: str,
    criterion_id: str,
    analyst_score: float,
    reason: str,
    analyst: str,
    database_file: str | None = None,
) -> dict[str, Any]:
    supplier = next(
        (item for item in run["suppliers"] if item["supplier_name"] == supplier_name),
        None,
    )
    if supplier is None:
        raise ValueError("Supplier not found in the evaluation run.")
    evaluation = next(
        (item for item in supplier["evaluations"] if item["criterion_id"] == criterion_id),
        None,
    )
    if evaluation is None:
        raise ValueError("Criterion not found in the supplier scorecard.")
    score = float(analyst_score)
    if not 0 <= score <= float(evaluation["maximum_score"]):
        raise ValueError(
            f"Analyst score must be between 0 and {evaluation['maximum_score']:g}."
        )
    if not reason.strip():
        raise ValueError("An override reason is required.")
    original_ai_score = float(evaluation.get("ai_score", evaluation["score"]))
    database.save_score_override(
        run["run_id"],
        supplier_name,
        criterion_id,
        original_ai_score,
        score,
        reason,
        analyst,
        database_file,
    )
    evaluation["ai_score"] = original_ai_score
    evaluation["score"] = score
    evaluation["analyst_score"] = score
    evaluation["override_reason"] = reason.strip()
    evaluation["override_analyst"] = analyst.strip()
    evaluation["weighted_score"] = round(
        score / float(evaluation["maximum_score"]) * float(evaluation["weight"]), 4
    )
    overrides = database.list_score_overrides(run["run_id"], database_file)
    for item in run["suppliers"]:
        item["score_overrides"] = [
            override
            for override in overrides
            if override["supplier_name"] == item["supplier_name"]
        ]
    run["benchmarks"] = calculate_benchmarks(run["suppliers"])
    add_relative_metrics(run["suppliers"], run["benchmarks"])
    run["suppliers"] = rank_suppliers(run["suppliers"])
    database.update_run_results(run["run_id"], run["suppliers"], database_file)
    return run
