"""SQLite persistence for RFQs, versioned frameworks, prompts, and runs."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def database_path() -> str:
    return os.environ.get("RFP_DATABASE_PATH", "rfp_evaluator.sqlite3")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path or database_path()))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: str | Path | None = None) -> None:
    with connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rfqs (
                rfq_id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_hash TEXT NOT NULL UNIQUE,
                uploaded_at TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS rfq_pages (
                rfq_id TEXT NOT NULL REFERENCES rfqs(rfq_id),
                page_number INTEGER NOT NULL,
                page_text TEXT NOT NULL,
                PRIMARY KEY (rfq_id, page_number)
            );
            CREATE TABLE IF NOT EXISTS rfq_requirements (
                requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfq_id TEXT NOT NULL REFERENCES rfqs(rfq_id),
                requirement_type TEXT NOT NULL,
                requirement_text TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                source_section TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS criteria_versions (
                version_id TEXT PRIMARY KEY,
                rfq_id TEXT NOT NULL REFERENCES rfqs(rfq_id),
                version_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                criteria_json TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                UNIQUE (rfq_id, version_number)
            );
            CREATE TABLE IF NOT EXISTS prompt_versions (
                prompt_version_id TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                version_number INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                criteria_json TEXT NOT NULL,
                prompt_template TEXT NOT NULL,
                rfq_id TEXT,
                criteria_version_id TEXT,
                prompt_version_id TEXT,
                model TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS supplier_results (
                run_id TEXT NOT NULL REFERENCES evaluation_runs(run_id),
                supplier_name TEXT NOT NULL,
                submission_date TEXT NOT NULL,
                experience_rating REAL NOT NULL,
                result_json TEXT NOT NULL,
                document_name TEXT NOT NULL DEFAULT '',
                document_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, supplier_name)
            );
            CREATE TABLE IF NOT EXISTS score_overrides (
                override_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES evaluation_runs(run_id),
                supplier_name TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                ai_score REAL NOT NULL,
                analyst_score REAL NOT NULL,
                override_reason TEXT NOT NULL,
                analyst TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfq_id TEXT,
                run_id TEXT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        _add_column(connection, "evaluation_runs", "rfq_id", "TEXT")
        _add_column(connection, "evaluation_runs", "criteria_version_id", "TEXT")
        _add_column(connection, "evaluation_runs", "prompt_version_id", "TEXT")
        _add_column(connection, "evaluation_runs", "model", "TEXT NOT NULL DEFAULT ''")
        _add_column(connection, "supplier_results", "document_name", "TEXT NOT NULL DEFAULT ''")
        _add_column(connection, "supplier_results", "document_text", "TEXT NOT NULL DEFAULT ''")
        connection.execute("DROP TABLE IF EXISTS criteria")


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    declaration: str,
) -> None:
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if name not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def save_rfq(
    rfq_id: str,
    file_name: str,
    file_hash: str,
    pages: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    path: str | Path | None = None,
) -> str:
    raw_text = "\n\n".join(
        f"[Page {page['page']}]\n{page['text']}" for page in pages
    )
    with connect(path) as connection:
        existing = connection.execute(
            "SELECT rfq_id FROM rfqs WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if existing:
            return str(existing["rfq_id"])
        connection.execute(
            """
            INSERT INTO rfqs (rfq_id, file_name, file_hash, uploaded_at, status, raw_text)
            VALUES (?, ?, ?, ?, 'parsed', ?)
            """,
            (rfq_id, file_name, file_hash, _now(), raw_text),
        )
        connection.executemany(
            "INSERT INTO rfq_pages (rfq_id, page_number, page_text) VALUES (?, ?, ?)",
            [(rfq_id, int(page["page"]), str(page["text"])) for page in pages],
        )
        connection.executemany(
            """
            INSERT INTO rfq_requirements
                (rfq_id, requirement_type, requirement_text, page_number, source_section)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    rfq_id,
                    requirement["requirement_type"],
                    requirement["requirement_text"],
                    int(requirement["page_number"]),
                    requirement.get("source_section", ""),
                )
                for requirement in requirements
            ],
        )
        _audit(connection, rfq_id, None, "rfq_parsed", {"file_name": file_name})
    return rfq_id


def get_rfq(rfq_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as connection:
        rfq = connection.execute("SELECT * FROM rfqs WHERE rfq_id = ?", (rfq_id,)).fetchone()
        if rfq is None:
            return None
        pages = connection.execute(
            "SELECT page_number, page_text FROM rfq_pages WHERE rfq_id = ? ORDER BY page_number",
            (rfq_id,),
        ).fetchall()
        requirements = connection.execute(
            """
            SELECT requirement_type, requirement_text, page_number, source_section
            FROM rfq_requirements WHERE rfq_id = ? ORDER BY requirement_id
            """,
            (rfq_id,),
        ).fetchall()
    return {
        **dict(rfq),
        "pages": [dict(row) for row in pages],
        "requirements": [dict(row) for row in requirements],
    }


def list_rfqs(path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT rfq_id, file_name, uploaded_at, status FROM rfqs ORDER BY uploaded_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def save_criteria_version(
    rfq_id: str,
    criteria: list[dict[str, Any]],
    summary: str,
    status: str = "draft",
    path: str | Path | None = None,
) -> dict[str, Any]:
    import uuid

    with connect(path) as connection:
        version = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM criteria_versions WHERE rfq_id = ?",
            (rfq_id,),
        ).fetchone()[0]
        version_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO criteria_versions
                (version_id, rfq_id, version_number, status, criteria_json, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                rfq_id,
                version,
                status,
                json.dumps(criteria, ensure_ascii=False),
                summary,
                _now(),
            ),
        )
        connection.execute(
            "UPDATE rfqs SET summary = ?, status = ? WHERE rfq_id = ?",
            (summary, "framework_generated", rfq_id),
        )
        _audit(
            connection,
            rfq_id,
            None,
            "criteria_version_created",
            {"version_id": version_id, "version_number": version},
        )
    return get_criteria_version(version_id, path)  # type: ignore[return-value]


def get_criteria_version(
    version_id: str, path: str | Path | None = None
) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM criteria_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["criteria"] = json.loads(result.pop("criteria_json"))
    return result


def list_criteria_versions(
    rfq_id: str, path: str | Path | None = None
) -> list[dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT version_id, version_number, status, created_at, approved_at
            FROM criteria_versions WHERE rfq_id = ? ORDER BY version_number DESC
            """,
            (rfq_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_criteria_version(
    rfq_id: str, path: str | Path | None = None
) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT version_id FROM criteria_versions
            WHERE rfq_id = ? ORDER BY version_number DESC LIMIT 1
            """,
            (rfq_id,),
        ).fetchone()
    return get_criteria_version(row["version_id"], path) if row else None


def latest_approved_criteria_version(
    rfq_id: str, path: str | Path | None = None
) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT version_id FROM criteria_versions
            WHERE rfq_id = ? AND status = 'approved'
            ORDER BY version_number DESC LIMIT 1
            """,
            (rfq_id,),
        ).fetchone()
    return get_criteria_version(row["version_id"], path) if row else None


def approve_criteria_version(
    version_id: str, path: str | Path | None = None
) -> None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT rfq_id FROM criteria_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise ValueError("The framework version no longer exists.")
        connection.execute(
            "UPDATE criteria_versions SET status = 'approved', approved_at = ? WHERE version_id = ?",
            (_now(), version_id),
        )
        _audit(
            connection,
            row["rfq_id"],
            None,
            "criteria_version_approved",
            {"version_id": version_id},
        )


def save_prompt_version(prompt_text: str, path: str | Path | None = None) -> dict[str, Any]:
    import uuid

    with connect(path) as connection:
        version = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM prompt_versions"
        ).fetchone()[0]
        prompt_version_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO prompt_versions
                (prompt_version_id, prompt_text, created_at, version_number)
            VALUES (?, ?, ?, ?)
            """,
            (prompt_version_id, prompt_text, _now(), version),
        )
    return get_prompt_version(prompt_version_id, path)  # type: ignore[return-value]


def get_prompt_version(
    prompt_version_id: str, path: str | Path | None = None
) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM prompt_versions WHERE prompt_version_id = ?",
            (prompt_version_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_prompt_version(path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM prompt_versions ORDER BY version_number DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def list_prompt_versions(path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT prompt_version_id, version_number, created_at
            FROM prompt_versions ORDER BY version_number DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def create_run(
    run_id: str,
    criteria: list[dict[str, Any]],
    prompt_template: str,
    path: str | Path | None = None,
    *,
    rfq_id: str | None = None,
    criteria_version_id: str | None = None,
    prompt_version_id: str | None = None,
    model: str = "",
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO evaluation_runs
                (run_id, created_at, criteria_json, prompt_template, rfq_id,
                 criteria_version_id, prompt_version_id, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _now(),
                json.dumps(criteria, ensure_ascii=False),
                prompt_template,
                rfq_id,
                criteria_version_id,
                prompt_version_id,
                model,
            ),
        )
        _audit(
            connection,
            rfq_id,
            run_id,
            "evaluation_run_created",
            {
                "criteria_version_id": criteria_version_id,
                "prompt_version_id": prompt_version_id,
                "model": model,
            },
        )


def save_supplier_result(
    run_id: str,
    supplier_name: str,
    submission_date: str,
    experience_rating: float,
    result: dict[str, Any],
    path: str | Path | None = None,
    *,
    document_name: str = "",
    document_text: str = "",
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO supplier_results
                (run_id, supplier_name, submission_date, experience_rating,
                 result_json, document_name, document_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                supplier_name,
                submission_date,
                experience_rating,
                json.dumps(result, ensure_ascii=False),
                document_name,
                document_text,
            ),
        )


def save_score_override(
    run_id: str,
    supplier_name: str,
    criterion_id: str,
    ai_score: float,
    analyst_score: float,
    reason: str,
    analyst: str,
    path: str | Path | None = None,
) -> None:
    if not reason.strip():
        raise ValueError("An override reason is required.")
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO score_overrides
                (run_id, supplier_name, criterion_id, ai_score, analyst_score,
                 override_reason, analyst, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                supplier_name,
                criterion_id,
                ai_score,
                analyst_score,
                reason.strip(),
                analyst.strip(),
                _now(),
            ),
        )
        run = connection.execute(
            "SELECT rfq_id FROM evaluation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        _audit(
            connection,
            run["rfq_id"] if run else None,
            run_id,
            "supplier_score_overridden",
            {
                "supplier_name": supplier_name,
                "criterion_id": criterion_id,
                "ai_score": ai_score,
                "analyst_score": analyst_score,
                "reason": reason.strip(),
                "analyst": analyst.strip(),
            },
        )


def update_run_results(
    run_id: str,
    suppliers: list[dict[str, Any]],
    path: str | Path | None = None,
) -> None:
    with connect(path) as connection:
        for supplier in suppliers:
            connection.execute(
                """
                UPDATE supplier_results SET result_json = ?
                WHERE run_id = ? AND supplier_name = ?
                """,
                (
                    json.dumps(supplier, ensure_ascii=False),
                    run_id,
                    supplier["supplier_name"],
                ),
            )


def get_run(run_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as connection:
        run = connection.execute(
            "SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        suppliers = connection.execute(
            """
            SELECT supplier_name, submission_date, experience_rating, result_json,
                   document_name, document_text
            FROM supplier_results WHERE run_id = ? ORDER BY supplier_name
            """,
            (run_id,),
        ).fetchall()
    return {
        **dict(run),
        "criteria": json.loads(run["criteria_json"]),
        "suppliers": [
            {
                "supplier_name": row["supplier_name"],
                "submission_date": row["submission_date"],
                "experience_rating": row["experience_rating"],
                "document_name": row["document_name"],
                "document_text": row["document_text"],
                "result": json.loads(row["result_json"]),
            }
            for row in suppliers
        ],
    }


def list_score_overrides(
    run_id: str, path: str | Path | None = None
) -> list[dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT supplier_name, criterion_id, ai_score, analyst_score,
                   override_reason, analyst, created_at
            FROM score_overrides WHERE run_id = ? ORDER BY override_id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_runs(path: str | Path | None = None) -> list[dict[str, str]]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT run_id, created_at FROM evaluation_runs ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def _audit(
    connection: sqlite3.Connection,
    rfq_id: str | None,
    run_id: str | None,
    event_type: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events (rfq_id, run_id, event_type, details_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (rfq_id, run_id, event_type, json.dumps(details, ensure_ascii=False), _now()),
    )
