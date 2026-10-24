"""RFQ document extraction with page-aware requirement candidates."""

from __future__ import annotations

import re
from typing import Any

from .pdf_extraction import extract_pdf_pages


_REQUIREMENT_TYPES = {
    "security": ("security", "cyber", "encryption", "privacy", "data residency"),
    "compliance": ("compliance", "certification", "iso ", "regulation", "audit"),
    "commercial": ("price", "cost", "commercial", "payment", "budget", "pricing"),
    "implementation": ("implementation", "deployment", "timeline", "milestone", "delivery"),
    "support": ("support", "sla", "service level", "warranty", "maintenance"),
    "technical": ("technical", "integration", "api", "architecture", "platform"),
    "experience": ("experience", "reference", "case study", "years of"),
}


def parse_rfq_document(pdf_bytes: bytes) -> dict[str, Any]:
    pages = extract_pdf_pages(pdf_bytes)
    cleaned_pages = [
        {
            "page": int(page["page"]),
            "text": clean_text(str(page["text"])),
        }
        for page in pages
    ]
    requirements = extract_requirement_candidates(cleaned_pages)
    return {
        "pages": cleaned_pages,
        "requirements": requirements,
        "raw_text": "\n\n".join(
            f"[Page {page['page']}]\n{page['text']}" for page in cleaned_pages
        ),
    }


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_requirement_candidates(
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    current_section = ""
    for page in pages:
        for raw_line in str(page["text"]).splitlines():
            line = re.sub(r"^\s*(?:[-*•]|\d+(?:\.\d+)*[.)]?)\s*", "", raw_line).strip()
            if not line:
                continue
            if len(line) < 90 and (line.isupper() or line.endswith(":")):
                current_section = line.rstrip(":").strip()
            if len(line) < 24:
                continue
            folded = line.casefold()
            requirement_type = next(
                (
                    kind
                    for kind, terms in _REQUIREMENT_TYPES.items()
                    if any(term in folded for term in terms)
                ),
                "general",
            )
            requirements.append(
                {
                    "requirement_type": requirement_type,
                    "requirement_text": line,
                    "page_number": int(page["page"]),
                    "source_section": current_section,
                }
            )
    return requirements
