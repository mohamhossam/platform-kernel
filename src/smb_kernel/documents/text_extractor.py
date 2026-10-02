"""Safe structured evidence extraction for PDF, DOCX, XLSX, and UTF-8 TXT."""

from __future__ import annotations

from pathlib import PurePosixPath

from smb_kernel.documents.extraction_base import SUPPORTED_MIME_TYPES
from smb_kernel.documents.pdf_image_extraction import (
    PdfAndImageExtraction,
)
from smb_kernel.documents.plain_text_extraction import PlainTextExtraction
from smb_kernel.documents.ports import (
    ExtractedDocument,
)
from smb_kernel.documents.presentation_extraction import (
    PresentationExtraction,
)
from smb_kernel.documents.spreadsheet_extraction import (
    SpreadsheetExtraction,
)
from smb_kernel.documents.word_extraction import WordExtraction
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


class SafeDocumentTextExtractor(
    PlainTextExtraction,
    PresentationExtraction,
    WordExtraction,
    SpreadsheetExtraction,
    PdfAndImageExtraction,
):
    def extract(self, mime_type: str, content: bytes) -> str:
        return self.extract_structured(mime_type, content).text

    def extract_structured(self, mime_type: str, content: bytes) -> ExtractedDocument:
        self._visited_xml_nodes = 0
        normalized = mime_type.strip().lower()
        if normalized in {"text/csv", "text/tab-separated-values"}:
            return self._delimited(
                content, "\t" if normalized == "text/tab-separated-values" else ","
            )
        if (
            normalized
            == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ):
            self._require_signature(content, b"PK", "PPTX")
            return self._pptx(content)
        if normalized == "application/pdf":
            self._require_signature(content, b"%PDF-", "PDF")
            return self._pdf(content)
        if normalized in {"text/plain", "text/markdown"}:
            if b"\x00" in content:
                raise UnsupportedDocumentError("TXT uploads must not contain NUL bytes.")
            return self._text(self._decode_utf8(content))
        if normalized in {"image/png", "image/jpeg"}:
            return self._image(content, normalized)
        if normalized == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            self._require_signature(content, b"PK", "DOCX")
            return self._docx(content)
        if normalized == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            self._require_signature(content, b"PK", "XLSX")
            return self._xlsx(content)
        raise UnsupportedDocumentError(
            "Only PDF, DOCX, XLSX, UTF-8 TXT/MD, PNG, and JPEG documents are supported."
        )

    def extract_asset(self, mime_type: str, content: bytes, package_path: str) -> bytes:
        self._visited_xml_nodes = 0
        normalized = mime_type.strip().lower()
        if normalized in {"image/png", "image/jpeg"}:
            if package_path != "image":
                raise DocumentExtractionError("Document image asset was not found.")
            return self._standalone_image(content, normalized)[1]
        if normalized == "application/pdf":
            try:
                number = int(package_path.removeprefix("pdf-page/"))
            except ValueError as exc:
                raise DocumentExtractionError("PDF page asset was not found.") from exc
            if package_path != f"pdf-page/{number}":
                raise DocumentExtractionError("PDF page asset was not found.")
            return self._render_pdf_page(content, number)[1]
        if normalized not in {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            raise UnsupportedDocumentError("This document type has no extractable media assets.")
        with self._office_archive(content, "Office document") as archive:
            clean_path = PurePosixPath(package_path).as_posix()
            if clean_path != package_path or clean_path not in archive.namelist():
                raise DocumentExtractionError("Document media asset was not found.")
            raw = archive.read(clean_path)
            asset_mime_type = self._image_mime(clean_path)
            if asset_mime_type is None:
                raise DocumentExtractionError("Document media asset type is unsupported.")
            _, sanitized, _ = self._sanitize_image(raw, asset_mime_type)
            return sanitized

    @staticmethod
    def expected_extension(mime_type: str) -> str | None:
        return SUPPORTED_MIME_TYPES.get(mime_type.strip().lower())
