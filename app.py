"""Streamlit UI for RFQ-first, analyst-approved procurement evaluation."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Any

import streamlit as st

from rfp_evaluator import database
from rfp_evaluator.cohere_client import CohereEvaluator
from rfp_evaluator.criteria_validation import parse_and_validate_framework, validate_approved_framework
from rfp_evaluator.prompts import DEFAULT_PROMPT_TEMPLATE
from rfp_evaluator.services import (
    generate_rfq_framework,
    parse_and_store_rfq,
    evaluate_run,
    override_supplier_score,
)


st.set_page_config(page_title="AI Procurement Evaluation", page_icon="📊", layout="wide")
database.initialize_database()

if "prompt_editor" not in st.session_state:
    saved_prompt = database.latest_prompt_version()
    st.session_state["prompt_editor"] = (
        saved_prompt["prompt_text"] if saved_prompt else DEFAULT_PROMPT_TEMPLATE
    )
    st.session_state["prompt_version_id"] = (
        saved_prompt["prompt_version_id"] if saved_prompt else None
    )


def supplier_name_from_file(filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"^RFP_", "", stem, flags=re.IGNORECASE)
    normalized = re.sub(r"[_-]+", " ", stem).casefold()
    known_names = {
        "apex systems": "Apex Systems",
        "brightpath tech": "BrightPath Tech",
        "nexaworks": "NexaWorks",
        "orbit digital": "Orbit Digital",
    }
    return known_names.get(normalized, normalized.title())


def _traceability_to_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{item.get('page_reference', '')}: {item.get('requirement', '')}".strip(": ")
        for item in items
    )


def _traceability_from_text(value: Any) -> list[dict[str, str]]:
    results = []
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line:
            continue
        page, separator, requirement = line.partition(":")
        results.append(
            {
                "page_reference": page.strip() if separator else "",
                "requirement": requirement.strip() if separator else page,
            }
        )
    return results


def _to_editor_rows(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "criterion_id": str(item["criterion_id"]),
            "name": item["name"],
            "description": item["description"],
            "weight": float(item["weight"]),
            "maximum_score": float(item["maximum_score"]),
            "active": bool(item.get("active", True)),
            "evaluation_guidance": "\n".join(item.get("evaluation_guidance", [])),
            "rfq_traceability": _traceability_to_text(item.get("rfq_traceability", [])),
            "weight_rationale": item.get("weight_rationale", ""),
        }
        for item in criteria
    ]


def _from_editor_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = []
    for row in rows:
        guidance = row.get("evaluation_guidance", "")
        source.append(
            {
                "criterion_id": str(row.get("criterion_id", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "description": str(row.get("description", "")).strip(),
                "weight": row.get("weight"),
                "max_score": row.get("maximum_score"),
                "active": bool(row.get("active")),
                "evaluation_guidance": [
                    line.strip() for line in str(guidance).splitlines() if line.strip()
                ],
                "rfq_traceability": _traceability_from_text(
                    row.get("rfq_traceability", "")
                ),
                "weight_rationale": str(row.get("weight_rationale", "")).strip(),
            }
        )
    return parse_and_validate_framework(
        json.dumps({"criteria": source}, ensure_ascii=False)
    )["criteria"]


def _editor_records(edited: Any) -> list[dict[str, Any]]:
    if isinstance(edited, list):
        return edited
    if hasattr(edited, "to_dict"):
        return edited.to_dict("records")
    raise TypeError("Criteria editor returned an unsupported data format.")


def _reset_framework_state() -> None:
    for key in (
        "rfq_id",
        "rfq_hash",
        "rfq_info",
        "framework",
        "framework_approved",
        "framework_warnings",
        "latest_result",
    ):
        st.session_state.pop(key, None)


def _save_prompt() -> None:
    saved = database.save_prompt_version(st.session_state["prompt_editor"])
    st.session_state["prompt_version_id"] = saved["prompt_version_id"]
    st.success(f"Prompt saved as version {saved['version_number']}.")


def _reset_prompt() -> None:
    st.session_state["prompt_editor"] = DEFAULT_PROMPT_TEMPLATE
    st.session_state["prompt_version_id"] = None


def render_left_panel() -> None:
    st.subheader("Analyst Control Panel")
    st.markdown("### Cohere API Key")
    st.text_input(
        "Cohere API key",
        type="password",
        key="cohere_api_key",
        help="Used only in memory for this Streamlit session.",
    )
    st.warning("Never commit your Cohere API key to GitHub. The key is used only at runtime.")
    if st.button("Connect/Test Cohere", key="test_cohere"):
        try:
            CohereEvaluator(st.session_state.get("cohere_api_key", "")).test_connection()
            st.success("Cohere connection successful.")
        except Exception as exc:
            st.error(
                f"Cohere connection failed ({type(exc).__name__}). "
                "Check the key and network, then retry."
            )

    st.markdown("### Evaluation Criteria")
    framework = st.session_state.get("framework")
    if not framework:
        st.info("Upload and parse an RFQ to generate its evaluation framework.")
    else:
        rows = _to_editor_rows(framework["criteria"])
        editor_key = (
            f"criteria_editor_{framework['version_id']}_"
            f"{st.session_state.get('criteria_editor_epoch', 0)}"
        )
        edited = st.data_editor(
            rows,
            key=editor_key,
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            column_config={
                "criterion_id": st.column_config.TextColumn("ID", disabled=True),
                "weight": st.column_config.NumberColumn("Weight %", min_value=0.0, max_value=100.0),
                "maximum_score": st.column_config.NumberColumn("Max score", min_value=0.1),
                "active": st.column_config.CheckboxColumn("Active"),
                "evaluation_guidance": st.column_config.TextColumn("Guidance (one item per line)"),
                "rfq_traceability": st.column_config.TextColumn("RFQ traceability (one per line)"),
            },
        )
        try:
            edited_rows = _editor_records(edited)
            edited_criteria = _from_editor_rows(edited_rows)
            active = [item for item in edited_criteria if item["active"]]
            total_weight = sum(item["weight"] for item in active)
            if abs(total_weight - 100) < 1e-6:
                st.success(f"Total Weight: {total_weight:g}%")
            else:
                st.warning(
                    f"Total Weight: {total_weight:g}% — evaluation cannot continue until "
                    "active criteria total 100%."
                )
        except (ValueError, TypeError, KeyError) as exc:
            edited_criteria = []
            st.error(f"Criteria need correction: {exc}")

        action_cols = st.columns(2)
        with action_cols[0]:
            if st.button("Add Criterion", key="add_criterion"):
                next_rows = _editor_records(edited)
                criterion_number = len(next_rows) + 1
                next_rows.append(
                    {
                        "criterion_id": str(uuid.uuid4()),
                        "name": f"New criterion {criterion_number}",
                        "description": "Describe the RFQ-specific requirement this criterion evaluates.",
                        "weight": 1.0,
                        "maximum_score": 10.0,
                        "active": True,
                        "evaluation_guidance": "Specify supplier evidence to assess.",
                        "rfq_traceability": "",
                        "weight_rationale": "",
                    }
                )
                st.session_state["framework"]["criteria"] = _from_editor_rows(next_rows)
                st.session_state["framework_approved"] = False
                st.session_state["criteria_editor_epoch"] = (
                    st.session_state.get("criteria_editor_epoch", 0) + 1
                )
                st.session_state.pop("latest_result", None)
                st.rerun()
        with action_cols[1]:
            if st.button("Regenerate Criteria", key="regenerate_criteria"):
                try:
                    evaluator = CohereEvaluator(st.session_state.get("cohere_api_key", ""))
                    result = generate_rfq_framework(
                        st.session_state["rfq_id"], evaluator
                    )
                    st.session_state["framework"] = result["framework"]
                    st.session_state["framework_warnings"] = result["framework_warnings"]
                    st.session_state["framework_approved"] = False
                    st.session_state["criteria_editor_epoch"] = (
                        st.session_state.get("criteria_editor_epoch", 0) + 1
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        f"Could not regenerate criteria ({type(exc).__name__}). "
                        "Check the RFQ and Cohere connection."
                    )

        save_cols = st.columns(2)
        with save_cols[0]:
            if st.button("Save Criteria", key="save_criteria", disabled=not edited_criteria):
                try:
                    version = database.save_criteria_version(
                        st.session_state["rfq_id"],
                        edited_criteria,
                        framework.get("summary", ""),
                    )
                    st.session_state["framework"] = version
                    st.session_state["framework_approved"] = False
                    st.session_state["criteria_editor_epoch"] = (
                        st.session_state.get("criteria_editor_epoch", 0) + 1
                    )
                    st.session_state.pop("latest_result", None)
                    st.success(f"Saved criteria version {version['version_number']} as a draft.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save criteria: {exc}")
        with save_cols[1]:
            if st.button(
                "Approve Framework",
                type="primary",
                key="approve_framework",
                disabled=not edited_criteria,
            ):
                try:
                    rfq = database.get_rfq(st.session_state["rfq_id"])
                    validate_approved_framework(
                        edited_criteria,
                        require_traceability=bool(rfq and rfq["requirements"]),
                    )
                    version = database.save_criteria_version(
                        st.session_state["rfq_id"],
                        edited_criteria,
                        framework.get("summary", ""),
                    )
                    database.approve_criteria_version(version["version_id"])
                    st.session_state["framework"] = database.get_criteria_version(
                        version["version_id"]
                    )
                    st.session_state["framework_approved"] = True
                    st.session_state["criteria_editor_epoch"] = (
                        st.session_state.get("criteria_editor_epoch", 0) + 1
                    )
                    st.session_state.pop("latest_result", None)
                    st.success(f"Approved framework version {version['version_number']}.")
                    st.rerun()
                except (ValueError, TypeError, KeyError) as exc:
                    st.error(str(exc))
        if st.session_state.get("framework_warnings"):
            st.warning("\n".join(st.session_state["framework_warnings"]))
        st.caption(
            f"Criteria version {framework['version_number']} · {framework['status']}"
        )
        versions = database.list_criteria_versions(st.session_state["rfq_id"])
        st.caption(
            "Version history: "
            + ", ".join(f"v{item['version_number']} ({item['status']})" for item in versions)
        )

    st.markdown("### Evaluation Prompt")
    st.text_area(
        "Editable supplier-evaluation prompt",
        key="prompt_editor",
        height=260,
        help="Available placeholders: {rfq_requirements}, {evaluation_criteria}, {supplier_proposal}.",
    )
    prompt_cols = st.columns(2)
    with prompt_cols[0]:
        if st.button("Save Prompt", key="save_prompt"):
            _save_prompt()
    with prompt_cols[1]:
        st.button("Reset Prompt", key="reset_prompt", on_click=_reset_prompt)
    current_prompt = database.latest_prompt_version()
    st.caption(
        f"Prompt version {current_prompt['version_number'] if current_prompt else 'default'}"
    )
    prompt_versions = database.list_prompt_versions()
    if prompt_versions:
        st.caption(
            "Prompt history: "
            + ", ".join(f"v{item['version_number']}" for item in prompt_versions)
        )


def render_rfq_workflow() -> None:
    st.subheader("Step 1 — RFQ Upload and Parsing")
    saved_rfqs = database.list_rfqs()
    if saved_rfqs:
        rfq_options = {item["rfq_id"]: item for item in saved_rfqs}
        selected_rfq_id = st.selectbox(
            "Load a previously parsed RFQ",
            options=list(rfq_options),
            format_func=lambda rfq_id: (
                f"{rfq_options[rfq_id]['file_name']} · "
                f"{rfq_options[rfq_id]['uploaded_at'][:10]}"
            ),
            key="saved_rfq_choice",
        )
        if st.button("Load selected RFQ", key="load_saved_rfq"):
            _reset_framework_state()
            rfq = database.get_rfq(selected_rfq_id)
            if rfq:
                st.session_state["rfq_id"] = rfq["rfq_id"]
                st.session_state["rfq_hash"] = rfq["file_hash"]
                st.session_state["rfq_info"] = rfq
                latest = database.latest_criteria_version(rfq["rfq_id"])
                if latest:
                    st.session_state["framework"] = latest
                    st.session_state["framework_approved"] = latest["status"] == "approved"
                    st.session_state["framework_warnings"] = []
                    st.session_state["criteria_editor_epoch"] = (
                        st.session_state.get("criteria_editor_epoch", 0) + 1
                    )
                st.rerun()
    upload = st.file_uploader(
        "Upload RFQ PDF",
        type=["pdf"],
        accept_multiple_files=False,
        key="rfq_upload",
        help="Parse and persist the RFQ before uploading supplier proposals.",
    )
    if upload is not None:
        upload_bytes = upload.getvalue()
        import hashlib

        current_hash = hashlib.sha256(upload_bytes).hexdigest()
        if st.session_state.get("rfq_hash") not in (None, current_hash):
            _reset_framework_state()
        st.write(f"**Selected RFQ:** {upload.name}")
        if st.button("Parse and Save RFQ", key="parse_rfq"):
            try:
                rfq = parse_and_store_rfq(upload.name, upload_bytes)
                st.session_state["rfq_id"] = rfq["rfq_id"]
                st.session_state["rfq_hash"] = current_hash
                st.session_state["rfq_info"] = rfq
                latest = database.latest_criteria_version(rfq["rfq_id"])
                if latest:
                    st.session_state["framework"] = latest
                    approved = latest["status"] == "approved"
                    st.session_state["framework_approved"] = approved
                    st.session_state["framework_warnings"] = []
                    st.session_state["criteria_editor_epoch"] = (
                        st.session_state.get("criteria_editor_epoch", 0) + 1
                    )
                st.success("RFQ parsed and its page text and requirement candidates were saved.")
                st.rerun()
            except ValueError as exc:
                st.error(f"RFQ parsing failed: {exc}")
            except Exception as exc:
                st.error(
                    f"RFQ could not be parsed or saved ({type(exc).__name__}). "
                    "Verify the PDF and try again."
                )

    rfq_id = st.session_state.get("rfq_id")
    if not rfq_id:
        st.info(
            "Upload and process the RFQ first. Evaluation criteria and weightage will be "
            "generated dynamically from the RFQ."
        )
        return
    rfq = database.get_rfq(rfq_id)
    if rfq is None:
        st.error("The saved RFQ could not be found. Upload and parse it again.")
        return
    st.write(
        f"**RFQ:** {rfq['file_name']} · {len(rfq['pages'])} page(s) · "
        f"{len(rfq['requirements'])} extracted requirement candidate(s)"
    )
    if st.button("Generate / Regenerate Framework with Cohere", key="generate_framework"):
        try:
            evaluator = CohereEvaluator(st.session_state.get("cohere_api_key", ""))
            with st.spinner("Cohere is deriving RFQ-specific criteria and weights..."):
                result = generate_rfq_framework(rfq_id, evaluator)
            st.session_state["framework"] = result["framework"]
            st.session_state["framework_approved"] = False
            st.session_state["framework_warnings"] = result["framework_warnings"]
            st.session_state["criteria_editor_epoch"] = (
                st.session_state.get("criteria_editor_epoch", 0) + 1
            )
            st.session_state.pop("latest_result", None)
            st.success("Framework generated. Review, edit, and approve it in the left panel.")
            st.rerun()
        except Exception as exc:
            st.error(
                f"Framework generation failed ({type(exc).__name__}). "
                "Check the API key, RFQ text, and Cohere availability."
            )

    st.markdown("#### RFQ Summary")
    framework = st.session_state.get("framework")
    summary = framework.get("summary", "") if framework else rfq.get("summary", "")
    st.write(summary or "Generate a framework to receive the RFQ summary.")
    with st.expander("Extracted RFQ requirements", expanded=False):
        st.dataframe(rfq["requirements"], width="stretch", hide_index=True)
    with st.expander("Page-aware RFQ text", expanded=False):
        for page in rfq["pages"]:
            st.markdown(f"**Page {page['page_number']}**")
            st.text(page["page_text"])


def _framework_is_approved() -> bool:
    framework = st.session_state.get("framework")
    if not framework or not st.session_state.get("framework_approved"):
        return False
    try:
        rfq = database.get_rfq(st.session_state.get("rfq_id", ""))
        validate_approved_framework(
            framework["criteria"],
            require_traceability=bool(rfq and rfq["requirements"]),
        )
        return True
    except ValueError:
        return False


def render_supplier_workflow() -> None:
    st.subheader("Step 2 — Supplier Input and Evaluation")
    approved = _framework_is_approved()
    if not approved:
        st.info("Supplier evaluation is disabled until the RFQ framework is generated, validated, and approved.")
    uploads = st.file_uploader(
        "Upload supplier proposal PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        key="supplier_pdfs",
        disabled=not approved,
    )
    suppliers: list[dict[str, Any]] = []
    if approved:
        for index, upload in enumerate(uploads or []):
            with st.expander(upload.name, expanded=True):
                name = st.text_input(
                    "Supplier name",
                    value=supplier_name_from_file(upload.name),
                    key=f"supplier_name_{index}_{upload.name}",
                )
                submitted = st.date_input(
                    "Submission date",
                    value=date.today(),
                    key=f"submission_date_{index}_{upload.name}",
                )
                rating = st.number_input(
                    "Historical experience rating (0-5)",
                    min_value=0.0,
                    max_value=5.0,
                    value=3.0,
                    step=0.5,
                    key=f"experience_{index}_{upload.name}",
                )
                suppliers.append(
                    {
                        "supplier_name": name,
                        "submission_date": submitted.isoformat(),
                        "experience_rating": rating,
                        "pdf_bytes": upload.getvalue(),
                        "file_name": upload.name,
                    }
                )
    if st.button("Evaluate Suppliers", type="primary", disabled=not approved or not suppliers):
        try:
            framework = st.session_state["framework"]
            criteria = [item for item in framework["criteria"] if item["active"]]
            validate_approved_framework(criteria)
            prompt_version = database.get_prompt_version(
                st.session_state.get("prompt_version_id", "")
            )
            if not prompt_version or prompt_version["prompt_text"] != st.session_state["prompt_editor"]:
                prompt_version = database.save_prompt_version(st.session_state["prompt_editor"])
                st.session_state["prompt_version_id"] = prompt_version["prompt_version_id"]
            evaluator = CohereEvaluator(st.session_state.get("cohere_api_key", ""))
            rfq = database.get_rfq(st.session_state["rfq_id"])
            progress = st.empty()

            def update_progress(index: int, total: int, supplier_name: str, status: str) -> None:
                progress.write(f"Supplier {index}/{total}: **{supplier_name}** — {status}")

            with st.spinner("Evaluating suppliers against the approved RFQ framework..."):
                result = evaluate_run(
                    suppliers,
                    evaluator,
                    st.session_state["prompt_editor"],
                    criteria=criteria,
                    rfq_id=st.session_state["rfq_id"],
                    criteria_version_id=framework["version_id"],
                    prompt_version_id=prompt_version["prompt_version_id"],
                    rfq_requirements=rfq["requirements"],
                    model=evaluator.model,
                    progress_callback=update_progress,
                )
            st.session_state["latest_result"] = result
            st.success(f"Evaluation complete. Run ID: {result['run_id']}")
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f"Evaluation could not be completed: {exc}")
        except Exception as exc:
            st.error(
                f"Evaluation failed ({type(exc).__name__}). "
                "Check document validity and Cohere availability."
            )


def render_results(run: dict[str, Any]) -> None:
    st.subheader("Step 3 — Results")
    suppliers = run["suppliers"]
    st.markdown("### Supplier Leaderboard")
    st.dataframe(
        [
            {
                "Rank": item["rank"],
                "Supplier": item["supplier_name"],
                "Score": item["absolute_weighted_score"],
                "PPI": item["ppi"],
                "Status": "Evaluated",
            }
            for item in suppliers
        ],
        width="stretch",
        hide_index=True,
    )
    st.markdown("### Criterion Comparison")
    comparisons = []
    for criterion in run["criteria"]:
        row: dict[str, Any] = {"Criterion": criterion["name"]}
        row.update(
            {
                item["supplier_name"]: next(
                    score["score"]
                    for score in item["evaluations"]
                    if score["criterion_id"] == criterion["criterion_id"]
                )
                for item in suppliers
            }
        )
        row["Benchmark"] = run["benchmarks"][criterion["name"]]
        comparisons.append(row)
    st.dataframe(comparisons, width="stretch", hide_index=True)

    st.markdown("### Supplier Scorecards")
    for supplier in suppliers:
        with st.expander(f"#{supplier['rank']} — {supplier['supplier_name']}"):
            st.metric("Weighted score", supplier["absolute_weighted_score"])
            st.metric("PPI", supplier["ppi"])
            st.write(f"**Tie-break:** {supplier['tie_break_explanation']}")
            st.write(supplier.get("overall_summary", ""))
            st.dataframe(
                [
                    {
                        "Criterion": item["criterion"],
                        "Score": item["score"],
                        "AI score": item.get("ai_score", item["score"]),
                        "Weighted score": item["weighted_score"],
                        "Weight (%)": item["weight"],
                        "Benchmark": item["benchmark"],
                        "Gap": item["gap"],
                        "Relative (%)": item["relative_percent"],
                        "Confidence": item["confidence"],
                        "Page reference": item["page_reference"],
                        "Reasoning": item["reasoning"],
                        "Evidence": "\n".join(item["evidence"]),
                    }
                    for item in supplier["evaluations"]
                ],
                width="stretch",
                hide_index=True,
            )
            st.markdown("**Analyst score overrides**")
            for criterion in supplier["evaluations"]:
                with st.form(
                    f"override_{run['run_id']}_{supplier['supplier_name']}_{criterion['criterion_id']}"
                ):
                    st.caption(
                        f"{criterion['criterion']} · original AI score "
                        f"{criterion.get('ai_score', criterion['score']):g} / "
                        f"{criterion['maximum_score']:g}"
                    )
                    override_cols = st.columns([1, 2, 1])
                    with override_cols[0]:
                        analyst_score = st.number_input(
                            "Analyst score",
                            min_value=0.0,
                            max_value=float(criterion["maximum_score"]),
                            value=float(criterion["score"]),
                            key=f"override_score_{run['run_id']}_{supplier['supplier_name']}_{criterion['criterion_id']}",
                        )
                    with override_cols[1]:
                        reason = st.text_input(
                            "Override reason",
                            key=f"override_reason_{run['run_id']}_{supplier['supplier_name']}_{criterion['criterion_id']}",
                        )
                    with override_cols[2]:
                        analyst = st.text_input(
                            "Analyst",
                            value="Procurement Analyst",
                            key=f"override_analyst_{run['run_id']}_{supplier['supplier_name']}_{criterion['criterion_id']}",
                        )
                    submitted = st.form_submit_button("Save override")
                if submitted:
                    try:
                        updated = override_supplier_score(
                            run,
                            supplier["supplier_name"],
                            criterion["criterion_id"],
                            analyst_score,
                            reason,
                            analyst,
                        )
                        st.session_state["latest_result"] = updated
                        st.success(
                            f"Override saved. Original AI score {criterion.get('ai_score', criterion['score']):g}; "
                            f"analyst score {analyst_score:g}."
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                if criterion.get("override_reason"):
                    st.caption(
                        f"Override: {criterion.get('analyst_score')} by "
                        f"{criterion.get('override_analyst')} — {criterion['override_reason']}"
                    )
            if supplier["risks"]:
                st.warning("Risks: " + "\n".join(map(str, supplier["risks"])))
            if supplier["missing_information"]:
                st.info(
                    "Missing information: "
                    + "\n".join(map(str, supplier["missing_information"]))
                )
            if supplier["warnings"]:
                st.warning("\n".join(supplier["warnings"]))

    st.subheader("Run Details")
    st.json(
        {
            "run_id": run["run_id"],
            "rfq_id": run["rfq_id"],
            "criteria_version_id": run["criteria_version_id"],
            "prompt_version_id": run["prompt_version_id"],
            "cohere_model": run["model"],
            "benchmarks": run["benchmarks"],
        }
    )
    st.subheader("JSON Download")
    st.download_button(
        "Download evaluation JSON",
        data=json.dumps(run, ensure_ascii=False, indent=2),
        file_name=f"rfp-evaluation-{run['run_id']}.json",
        mime="application/json",
    )


st.title("AI Procurement Evaluation")
st.caption("RFQ-driven criteria, analyst-approved weightage, and deterministic supplier ranking.")
left, right = st.columns([0.32, 0.68], gap="large")
with left:
    render_left_panel()
with right:
    render_rfq_workflow()
    render_supplier_workflow()
    if "latest_result" in st.session_state:
        render_results(st.session_state["latest_result"])
    with st.expander("Previous evaluation runs"):
        st.dataframe(database.list_runs(), width="stretch", hide_index=True)
