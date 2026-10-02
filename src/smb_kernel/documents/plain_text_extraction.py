"""UTF-8 text, Markdown and delimited (CSV/TSV) sources."""

from __future__ import annotations

import csv
import io

from smb_kernel.documents.extraction_base import (
    DELIMITED_EXTRACTION_VERSION,
    MAX_WORKBOOK_COLUMNS,
    MAX_WORKBOOK_ROWS,
    TEXT_EXTRACTION_VERSION,
    EvidenceBuilder,
    ExtractionBase,
)
from smb_kernel.documents.model import (
    DocumentExtractionWarning,
    EvidenceBlockKind,
    ExtractionWarningSeverity,
)
from smb_kernel.documents.ports import (
    ExtractedDocument,
)
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


class PlainTextExtraction(ExtractionBase):
    def _text(self, text: str) -> ExtractedDocument:
        stripped = self._non_blank(text, "TXT")
        builder = EvidenceBuilder([], [], [])
        sections: dict[int, str] = {}
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if line:
                if line.startswith("#") and " " in line:
                    heading, _title = line.split(" ", 1)
                    if set(heading) == {"#"} and len(heading) <= 6:
                        level = len(heading)
                        sections = {key: value for key, value in sections.items() if key < level}
                        sections[level] = f"Heading at line {number}"
                        builder.add(
                            EvidenceBlockKind.HEADING,
                            f"Line {number}",
                            self._section_path(sections),
                            text=line,
                        )
                        continue
                builder.add(
                    EvidenceBlockKind.PARAGRAPH,
                    f"Line {number}",
                    self._section_path(sections),
                    text=line,
                )
        if sections:
            builder.warnings.append(
                DocumentExtractionWarning(
                    "text_section_structure",
                    ExtractionWarningSeverity.WARNING,
                    "Heading wording is reviewed only in its own passage. Section paths use "
                    "neutral heading line positions so excluded or corrected headings are not "
                    "copied into other passages. Hash-prefixed headings are navigation hints; "
                    "this line-based extraction does not render full Markdown syntax.",
                )
            )
        return self._result(stripped, builder, extraction_version=TEXT_EXTRACTION_VERSION)

    def _delimited(self, content: bytes, delimiter: str) -> ExtractedDocument:
        if b"\x00" in content:
            raise UnsupportedDocumentError("Delimited files must not contain NUL bytes.")
        text = self._decode_utf8(content)
        builder = EvidenceBuilder([], [], [])
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        width: int | None = None
        cells = 0
        characters = 0
        try:
            for row_number, row in enumerate(reader, 1):
                if row_number > MAX_WORKBOOK_ROWS:
                    raise UnsupportedDocumentError("Delimited data exceeds the row limit.")
                cells += len(row)
                if cells > self._max_visited_spreadsheet_cells:
                    raise UnsupportedDocumentError("Delimited data exceeds the cell limit.")
                if len(row) > MAX_WORKBOOK_COLUMNS:
                    raise UnsupportedDocumentError("Delimited data exceeds the column limit.")
                if not row:  # Empty physical records have no cells or header semantics.
                    continue
                if width is None:
                    width = len(row)
                elif len(row) != width:
                    raise DocumentExtractionError(
                        f"Row {row_number} does not match the first record width. "
                        "Check the delimiter."
                    )
                if not any(value.strip() for value in row):
                    continue
                rendered = " | ".join(
                    f"R{row_number}C{i}: {value if value.strip() else '[empty cell]'}"
                    for i, value in enumerate(row, 1)
                )
                characters += len(rendered) + bool(builder.blocks)
                if characters > self._max_extracted_characters:
                    raise UnsupportedDocumentError(
                        f"Extracted content exceeds {self._max_extracted_characters} characters."
                    )
                builder.add(
                    EvidenceBlockKind.TABLE_ROW,
                    f"Row {row_number}",
                    ("Delimited table",),
                    text=rendered,
                )
        except csv.Error as exc:
            raise DocumentExtractionError(
                "Delimited data is malformed or contains an oversized field."
            ) from exc
        if not builder.blocks:
            raise DocumentExtractionError("Delimited data has no nonempty records.")
        builder.warnings.append(
            DocumentExtractionWarning(
                "delimited_row_structure",
                ExtractionWarningSeverity.WARNING,
                "Review every record, including the first: headers are not inferred or copied. "
                "R/C labels identify logical records and fields, not physical text lines. "
                "Quoted line breaks stay inside their field; [empty cell] is an extraction marker.",
            )
        )
        return self._result(
            self._blocks_text(builder.blocks),
            builder,
            extraction_version=DELIMITED_EXTRACTION_VERSION,
        )
