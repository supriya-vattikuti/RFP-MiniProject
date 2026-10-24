# AI Procurement Evaluation

A modular Streamlit application that parses an RFQ, asks Cohere to derive an RFQ-specific evaluation framework, lets a procurement analyst edit and approve versioned criteria and weights, and evaluates supplier proposals against that approved framework. Python validates responses and deterministically calculates weighted scores, benchmarks, PPI, and rankings.

## Requirements and setup

- Python 3.10+
- Cohere API key for live RFQ framework generation and supplier evaluation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

```powershell
streamlit run app.py
```

1. Enter a Cohere key in the masked runtime-only field and use **Connect/Test Cohere**.
2. Upload an RFQ PDF and choose **Parse and Save RFQ**. The parser stores page text and page-referenced requirement candidates in SQLite.
3. Generate the framework with Cohere. The model derives criteria and weights from the uploaded RFQ; the application does not seed procurement criteria or percentages.
4. Review/edit criteria, descriptions, weights, score maxima, guidance, traceability, and active state in the left panel. Active weights must total 100%.
5. Approve the framework. Supplier upload remains disabled until the saved framework passes validation and is approved.
6. Upload proposal PDFs, provide supplier metadata, and evaluate. The result includes evidence, citations, confidence, risks, missing information, deterministic scorecards, rankings, and JSON export.

The left panel contains the Cohere control, editable criteria, and versioned supplier-evaluation prompt. The right panel contains the RFQ-first workflow, supplier evaluation, and results. Previously parsed RFQs, framework versions, prompt versions, evaluation runs, source proposal text, and analyst score overrides are persisted in `rfp_evaluator.sqlite3`.

The Cohere API key remains in Streamlit session memory; it is not stored in SQLite, written to logs, or included in prompts. `.env.example` contains placeholders only, and `.env` and secret files are ignored by Git.

## Framework and scoring behavior

- Framework criteria and initial weights come from Cohere based on the RFQ text and extracted requirement candidates. A non-100% weight response receives one correction request and otherwise remains an unapproved draft for analyst correction.
- The analyst can save multiple criteria versions. Approval creates an immutable version reference used by an evaluation run.
- Prompt updates are also versioned. Each evaluation records RFQ ID, criteria version, prompt version, Cohere model, and run ID.
- The supplier prompt contains the RFQ requirements and approved criteria. Cohere returns criterion-level assessment and evidence only; it does not choose a winner.
- Absolute weighted score: `sum((score / maximum score) * weight)`. Weights are percentage points and the result is on a 0–100 scale.
- Criterion benchmark: highest valid supplier score. Gap: supplier score minus benchmark. Relative performance: score divided by benchmark times 100; a zero benchmark yields 0%.
- PPI is the weighted average of criterion relative-performance percentages. Ranking is absolute weighted score descending, PPI descending, earlier submission date, higher experience rating, and supplier name ascending.
- Analyst overrides preserve the original AI score, analyst score, reason, analyst, and timestamp. Override scores recalculate metrics and ranking deterministically.

## Tests

```powershell
python -m pytest
```

Tests use mock Cohere responses and require no real API key.

## Module layout

- `rfp_evaluator/rfq_parser.py` — RFQ PDF parsing and page-aware requirement candidates
- `rfp_evaluator/pdf_extraction.py` — page-aware PDF extraction shared with supplier proposals
- `rfp_evaluator/criteria_generation.py` and `criteria_validation.py` — dynamic Cohere generation and framework validation
- `rfp_evaluator/cohere_client.py` and `prompts.py` — Cohere adapter and dynamic prompt templates
- `rfp_evaluator/database.py` — SQLite persistence, framework/prompt versions, runs, and audit events
- `rfp_evaluator/services.py` — RFQ workflow, supplier evaluation, and overrides
- `rfp_evaluator/validation.py` — strict supplier response validation
- `rfp_evaluator/scoring.py`, `benchmarking.py`, and `ranking.py` — deterministic calculations
- `app.py` — two-panel Streamlit application
