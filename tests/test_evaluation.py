import json
from pathlib import Path

import pytest

from rfp_evaluator import database
from rfp_evaluator.criteria_generation import generate_evaluation_framework
from rfp_evaluator.criteria_validation import (
    FrameworkValidationError,
    validate_approved_framework,
)
from rfp_evaluator.pdf_extraction import extract_pdf_pages
from rfp_evaluator.ranking import rank_suppliers
from rfp_evaluator.scoring import (
    add_relative_metrics,
    calculate_absolute_score,
    calculate_benchmarks,
)
from rfp_evaluator.services import (
    evaluate_run,
    generate_rfq_framework,
    override_supplier_score,
    parse_and_store_rfq,
)
from rfp_evaluator.validation import normalize_evaluation


class MockCohere:
    def __init__(
        self,
        evaluation_response: str | None = None,
        generation_responses: list[str] | None = None,
    ) -> None:
        self.evaluation_response = evaluation_response or "{}"
        self.generation_responses = list(generation_responses or [])
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.generation_responses:
            raise AssertionError("No mock framework-generation response provided.")
        return self.generation_responses.pop(0)

    def evaluate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.evaluation_response


@pytest.fixture
def database_file(tmp_path):
    path = tmp_path / "test.sqlite3"
    database.initialize_database(path)
    return path


def _criteria():
    return [
        {
            "criterion_id": "rfq-fit",
            "name": "RFQ Fit",
            "description": "Assess alignment with the RFQ requirements.",
            "weight": 100,
            "maximum_score": 10,
            "active": True,
            "evaluation_guidance": ["Compare response evidence with the RFQ."],
            "rfq_traceability": [
                {"requirement": "Meet the stated requirement.", "page_reference": "Page 1"}
            ],
            "weight_rationale": "The RFQ makes this requirement central.",
        }
    ]


def _framework_json(criteria=None, weight=100, summary="RFQ summary"):
    criterion = (criteria or _criteria())[0].copy()
    criterion["weight"] = weight
    return json.dumps(
        {
            "rfq_summary": summary,
            "criteria": [
                {
                    **criterion,
                    "max_score": criterion["maximum_score"],
                }
            ],
        }
    )


def _evaluation_json(score=8):
    return json.dumps(
        {
            "evaluations": [
                {
                    "criterion_id": "rfq-fit",
                    "criterion": "RFQ Fit",
                    "score": score,
                    "evidence": ["Page 1: proposal addresses the requirement."],
                    "page_reference": "Page 1",
                    "reasoning": "Evidence supports a strong response.",
                    "confidence": 0.91,
                }
            ],
            "risks": [],
            "missing_information": [],
            "overall_summary": "The proposal addresses the evaluated RFQ requirement.",
        }
    )


def _save_rfq_and_approved_version(path):
    database.save_rfq(
        "rfq-test",
        "rfq.pdf",
        "hash-test",
        [{"page": 1, "text": "The RFQ requires delivery within the stated timeline."}],
        [
            {
                "requirement_type": "implementation",
                "requirement_text": "The RFQ requires delivery within the stated timeline.",
                "page_number": 1,
                "source_section": "Delivery",
            }
        ],
        path,
    )
    version = database.save_criteria_version(
        "rfq-test", _criteria(), "Test requirements", path=path
    )
    database.approve_criteria_version(version["version_id"], path)
    prompt = database.save_prompt_version("Prompt version for tests.", path)
    return version, prompt


def test_database_does_not_seed_fixed_evaluation_criteria(database_file):
    with database.connect(database_file) as connection:
        tables = {
            row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "criteria" not in tables
    assert database.list_rfqs(database_file) == []


def test_pdf_extraction_preserves_page_numbers_and_rejects_empty_input():
    pdf = (Path(__file__).resolve().parents[1] / "RFP_apex_systems.pdf").read_bytes()
    pages = extract_pdf_pages(pdf)
    assert pages
    assert pages[0]["page"] == 1
    with pytest.raises(ValueError, match="empty"):
        extract_pdf_pages(b"")
    with pytest.raises(ValueError, match="Could not read"):
        extract_pdf_pages(b"not a pdf")


def test_dynamic_framework_generation_and_weight_correction():
    invalid = json.loads(_framework_json(weight=70))
    second = json.loads(_framework_json(weight=100, summary="Corrected framework"))
    mock = MockCohere(generation_responses=[json.dumps(invalid), json.dumps(second)])
    framework = generate_evaluation_framework(
        "Page 1: the RFQ requires secure hosted deployment.",
        [{"requirement_text": "secure hosted deployment", "page_number": 1}],
        mock,
    )
    assert framework["criteria"][0]["weight"] == 100
    assert framework["rfq_summary"] == "Corrected framework"
    assert len(mock.prompts) == 2
    assert "not 100%" in mock.prompts[1]
    assert "predefined weights" in mock.prompts[0]


def test_framework_validator_rejects_duplicate_and_bad_weights():
    duplicate = _criteria() * 2
    duplicate[1]["criterion_id"] = "rfq-fit-2"
    with pytest.raises(FrameworkValidationError, match="Duplicate criterion"):
        validate_approved_framework(duplicate)
    criteria = _criteria()
    criteria[0]["weight"] = -2
    with pytest.raises(FrameworkValidationError, match="weight"):
        validate_approved_framework(criteria)
    criteria[0]["weight"] = 90
    with pytest.raises(FrameworkValidationError, match="90"):
        validate_approved_framework(criteria)


def test_framework_versions_are_persisted_and_approved(database_file):
    database.save_rfq("rfq-versions", "rfq.pdf", "hash-v", [{"page": 1, "text": "Requirement"}], [], database_file)
    first = database.save_criteria_version("rfq-versions", _criteria(), "Summary", path=database_file)
    database.approve_criteria_version(first["version_id"], database_file)
    revised = _criteria()
    revised[0]["weight"] = 90
    revised.append(
        {
            **_criteria()[0],
            "criterion_id": "additional",
            "name": "Additional RFQ Requirement",
            "weight": 10,
        }
    )
    second = database.save_criteria_version("rfq-versions", revised, "Revised", path=database_file)
    assert first["version_id"] != second["version_id"]
    assert [item["version_number"] for item in database.list_criteria_versions("rfq-versions", database_file)] == [2, 1]
    assert database.get_criteria_version(first["version_id"], database_file)["status"] == "approved"


def test_normalization_clamps_for_review_but_strict_rejects_bad_scores():
    response = json.dumps(
        {
            "evaluations": [
                {
                    "criterion_id": "rfq-fit",
                    "criterion": "RFQ Fit",
                    "score": 12,
                    "evidence": ["Page 2"],
                    "page_reference": "Page 2",
                    "reasoning": "Strong",
                    "confidence": 0.8,
                }
            ],
            "risks": [],
            "missing_information": [],
            "overall_summary": "Summary",
        }
    )
    normalized = normalize_evaluation(response, _criteria())
    assert normalized["evaluations"][0]["score"] == 10
    with pytest.raises(ValueError, match="between 0 and 10"):
        normalize_evaluation(response, _criteria(), strict=True)


def test_strict_validation_rejects_malformed_or_missing_criterion():
    with pytest.raises(ValueError, match="malformed JSON"):
        normalize_evaluation("not json", _criteria(), strict=True)
    incomplete = json.dumps(
        {
            "evaluations": [],
            "risks": [],
            "missing_information": [],
            "overall_summary": "Summary",
        }
    )
    with pytest.raises(ValueError, match="omitted required criterion"):
        normalize_evaluation(incomplete, _criteria(), strict=True)
    with pytest.raises(ValueError, match="invalid page reference"):
        normalize_evaluation(
            _evaluation_json(),
            _criteria(),
            strict=True,
            valid_page_numbers={2},
        )


def test_benchmark_weighted_score_zero_benchmark_and_ranking():
    assert calculate_absolute_score(
        [
            {"score": 4, "maximum_score": 5, "weight": 30},
            {"score": 2, "maximum_score": 5, "weight": 70},
        ]
    ) == 52
    suppliers = [
        {
            "supplier_name": name,
            "submission_date": "2025-02-01",
            "experience_rating": 4,
            "evaluations": [
                {"criterion": "RFQ Fit", "score": 0 if name == "Alpha" else 8, "maximum_score": 10, "weight": 100}
            ],
        }
        for name in ("Bravo", "Alpha")
    ]
    benchmarks = calculate_benchmarks(suppliers)
    add_relative_metrics(suppliers, benchmarks)
    ranked = rank_suppliers(suppliers)
    assert benchmarks == {"RFQ Fit": 8}
    assert ranked[0]["supplier_name"] == "Bravo"
    assert ranked[0]["evaluations"][0]["relative_percent"] == 100
    assert ranked[1]["evaluations"][0]["relative_percent"] == 0


def test_rfq_processing_generates_and_persists_dynamic_framework(database_file):
    pdf = (Path(__file__).resolve().parents[1] / "RFP_apex_systems.pdf").read_bytes()
    mock = MockCohere(generation_responses=[_framework_json()])
    rfq = parse_and_store_rfq("rfq.pdf", pdf, str(database_file))
    generated = generate_rfq_framework(rfq["rfq_id"], mock, database_file=str(database_file))
    persisted = database.get_rfq(rfq["rfq_id"], database_file)
    assert persisted is not None
    assert persisted["pages"]
    assert persisted["raw_text"]
    assert generated["framework"]["criteria"][0]["name"] == "RFQ Fit"
    assert generated["framework"]["status"] == "draft"
    assert mock.prompts and "RFQ content with page labels" in mock.prompts[0]


def test_mock_cohere_evaluates_and_persists_all_four_supplier_pdfs(database_file):
    version, prompt = _save_rfq_and_approved_version(database_file)
    root = Path(__file__).resolve().parents[1]
    proposals = [
        ("Apex Systems", "RFP_apex_systems.pdf"),
        ("BrightPath Tech", "RFP_brightpath_tech.pdf"),
        ("NexaWorks", "RFP_nexaworks.pdf"),
        ("Orbit Digital", "RFP_orbit_digital.pdf"),
    ]
    mock = MockCohere(evaluation_response=_evaluation_json())
    run = evaluate_run(
        [
            {
                "supplier_name": name,
                "submission_date": f"2025-01-0{index + 1}",
                "experience_rating": 3,
                "pdf_bytes": (root / filename).read_bytes(),
                "file_name": filename,
            }
            for index, (name, filename) in enumerate(proposals)
        ],
        mock,
        "Evaluate {evaluation_criteria} with {rfq_requirements}: {supplier_proposal}",
        str(database_file),
        criteria=_criteria(),
        rfq_id="rfq-test",
        criteria_version_id=version["version_id"],
        prompt_version_id=prompt["prompt_version_id"],
        rfq_requirements=[{"requirement_text": "Meet the stated requirement.", "page_number": 1}],
        model="mock-model",
    )
    persisted = database.get_run(run["run_id"], database_file)
    assert len(run["suppliers"]) == 4
    assert len(mock.prompts) == 4
    assert all("Page 1" in content for content in mock.prompts)
    assert all('"criterion_id": "rfq-fit"' in content for content in mock.prompts)
    assert persisted is not None
    assert persisted["rfq_id"] == "rfq-test"
    assert persisted["criteria_version_id"] == version["version_id"]
    assert persisted["prompt_version_id"] == prompt["prompt_version_id"]
    assert len(persisted["suppliers"]) == 4
    assert persisted["suppliers"][0]["document_text"]


def test_runtime_api_key_is_not_written_to_database(database_file):
    secret = "mock-never-persist-this-key"
    database.save_prompt_version("safe prompt", database_file)
    assert secret.encode() not in Path(database_file).read_bytes()


def test_analyst_override_preserves_ai_score_and_recalculates_run(database_file):
    version, prompt = _save_rfq_and_approved_version(database_file)
    pdf = (Path(__file__).resolve().parents[1] / "RFP_apex_systems.pdf").read_bytes()
    run = evaluate_run(
        [
            {
                "supplier_name": "Apex Systems",
                "submission_date": "2025-01-01",
                "experience_rating": 4,
                "pdf_bytes": pdf,
                "file_name": "apex.pdf",
            }
        ],
        MockCohere(evaluation_response=_evaluation_json(score=8)),
        database_file=str(database_file),
        criteria=_criteria(),
        rfq_id="rfq-test",
        criteria_version_id=version["version_id"],
        prompt_version_id=prompt["prompt_version_id"],
        rfq_requirements=[{"requirement_text": "Meet the stated requirement.", "page_number": 1}],
        model="mock-model",
    )
    updated = override_supplier_score(
        run,
        "Apex Systems",
        "rfq-fit",
        4,
        "Evidence did not satisfy a mandatory RFQ condition.",
        "Analyst One",
        str(database_file),
    )
    result = updated["suppliers"][0]["evaluations"][0]
    persisted = database.get_run(run["run_id"], database_file)
    assert result["ai_score"] == 8
    assert result["score"] == 4
    assert result["analyst_score"] == 4
    assert persisted["suppliers"][0]["result"]["evaluations"][0]["ai_score"] == 8
    overrides = database.list_score_overrides(run["run_id"], database_file)
    assert overrides[0]["ai_score"] == 8
    assert overrides[0]["analyst_score"] == 4
    assert overrides[0]["analyst"] == "Analyst One"
