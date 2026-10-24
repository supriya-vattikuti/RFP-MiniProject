"""Page-aware text extraction from supplier proposal PDFs."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader


def extract_pdf_pages(pdf_bytes: bytes) -> list[dict[str, str | int]]:
    if not pdf_bytes:
        raise ValueError("The uploaded PDF is empty.")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
        pages = [
            {"page": index, "text": page.extract_text() or ""}
            for index, page in enumerate(reader.pages, start=1)
        ]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read the uploaded PDF: {exc}") from exc
    if not any(str(page["text"]).strip() for page in pages):
        raise ValueError("No extractable text was found in the uploaded PDF.")
    return pages


def format_page_evidence(pages: list[dict[str, str | int]]) -> str:
    return "\n\n".join(
        f"[Page {page['page']}]\n{page['text']}" for page in pages
    )
