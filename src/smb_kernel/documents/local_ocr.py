"""Optional local Docling/Tesseract pipeline; called only inside the bounded child."""

from __future__ import annotations

import hashlib
import importlib
import io
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader

from smb_kernel.documents.model import (
    DocumentEvidenceBlock,
    DocumentExtractionWarning,
    EvidenceBlockKind,
    ExtractionWarningSeverity,
)
from smb_kernel.documents.ports import ExtractedDocument
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


def extract_local_ocr(
    content: bytes, mime_type: str, artifacts_path: str, max_pages: int, max_characters: int
) -> ExtractedDocument:
    if not artifacts_path or not Path(artifacts_path).is_dir():
        raise DocumentExtractionError(
            "Local OCR models are not provisioned. Set LIBRARY_OCR_ARTIFACTS_PATH "
            "to a pre-downloaded Docling model directory."
        )
    try:
        base: Any = importlib.import_module("docling.datamodel.base_models")
        options: Any = importlib.import_module("docling.datamodel.pipeline_options")
        converters: Any = importlib.import_module("docling.document_converter")
    except ImportError as exc:
        raise DocumentExtractionError(
            "Local OCR runtime is unavailable. Install the document-ocr extra "
            "and Tesseract English/Arabic language data."
        ) from exc
    if mime_type.startswith("image/"):
        with Image.open(io.BytesIO(content)) as image:
            stream = io.BytesIO()
            image.convert("RGB").save(stream, format="PDF")
            content = stream.getvalue()
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        raise UnsupportedDocumentError("Encrypted PDFs cannot be processed.")
    if len(reader.pages) > max_pages:
        raise UnsupportedDocumentError("PDF exceeds the configured page limit.")
    pipeline = options.PdfPipelineOptions(
        artifacts_path=artifacts_path,
        enable_remote_services=False,
        do_ocr=True,
        do_table_structure=True,
        ocr_options=options.TesseractCliOcrOptions(lang=["eng", "ara"]),
    )
    converter = converters.DocumentConverter(
        format_options={base.InputFormat.PDF: converters.PdfFormatOption(pipeline_options=pipeline)}
    )
    try:
        result = converter.convert(
            base.DocumentStream(name="source.pdf", stream=io.BytesIO(content)),
            max_num_pages=max_pages,
        )
    except Exception as exc:
        # Provider/parser boundary: never return an empty success or expose source/parser output.
        raise DocumentExtractionError(
            "Local OCR failed. Check the installed models and English/Arabic Tesseract data."
        ) from exc
    if str(result.status.value) != "success":
        raise DocumentExtractionError(
            "Local OCR was incomplete. Partial conversion cannot be published."
        )
    blocks: list[DocumentEvidenceBlock] = []
    sections: tuple[str, ...] = ()
    characters = 0
    for item, _level in result.document.iterate_items():
        label = str(item.label.value)
        if label in {"page_header", "page_footer"}:
            continue
        text = str(getattr(item, "text", "") or "").strip()
        kind = EvidenceBlockKind.PARAGRAPH
        if label == "table":
            cells = item.data.table_cells
            rows: dict[int, list[str]] = {}
            for cell in cells:
                rows.setdefault(int(cell.start_row_offset_idx), []).append(str(cell.text))
            text = "\n".join(" | ".join(rows[key]) for key in sorted(rows))
            kind = EvidenceBlockKind.TABLE_ROW
        elif label in {"section_header", "title"}:
            sections = (f"Heading at OCR item {len(blocks) + 1}",)
            kind = EvidenceBlockKind.HEADING
        elif label == "list_item":
            kind = EvidenceBlockKind.LIST_ITEM
        if not text:
            continue
        locations = []
        for provenance in item.prov:
            bbox = provenance.bbox
            locations.append(
                f"Page {provenance.page_no}, "
                f"region ({bbox.l:g}, {bbox.t:g}, {bbox.r:g}, {bbox.b:g})"
            )
        if not locations:
            raise DocumentExtractionError("OCR text is missing page provenance.")
        characters += len(text)
        if characters > max_characters:
            raise UnsupportedDocumentError("OCR exceeds the extracted-character limit.")
        digest = hashlib.sha256(text.encode()).hexdigest()
        ordinal = len(blocks) + 1
        blocks.append(
            DocumentEvidenceBlock(
                f"ocr-{ordinal}-{digest[:16]}",
                kind,
                ordinal,
                sections,
                "; ".join(locations),
                digest,
                text,
            )
        )
    if not blocks:
        raise DocumentExtractionError(
            "OCR found no searchable text. Review or replace the original image."
        )
    warning = DocumentExtractionWarning(
        "ocr_review",
        ExtractionWarningSeverity.WARNING,
        "Verify reading order, Arabic wording, table cells and diagrams against the original. "
        "OCR labels do not establish diagram relationships.",
    )
    return ExtractedDocument(
        "\n\n".join(b.text or "" for b in blocks),
        "docling-tesseract-eng-ara-sections-v2",
        tuple(blocks),
        (warning,),
        (),
    )
