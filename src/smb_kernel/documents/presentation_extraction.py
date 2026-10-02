"""PowerPoint (PPTX) slides, shapes and tables."""

from __future__ import annotations

import posixpath
import zipfile
from xml.etree import ElementTree

from smb_kernel.documents.extraction_base import (
    PPTX_EXTRACTION_VERSION,
    A,
    EvidenceBuilder,
    ExtractionBase,
    R,
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


class PresentationExtraction(ExtractionBase):
    def _pptx(self, content: bytes) -> ExtractedDocument:
        ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
        builder = EvidenceBuilder([], [], [])
        visited_cells = 0
        try:
            with self._office_archive(content, "PPTX") as archive:
                root = self._parse_xml(archive.read("ppt/presentation.xml"), "PPTX")
                relations = self._relationships(archive, "ppt/_rels/presentation.xml.rels")
                slides = root.findall(f".//{{{ns}}}sldId")
                if len(slides) > self._max_pdf_pages:
                    raise UnsupportedDocumentError("Presentation exceeds the slide limit.")
                for number, slide in enumerate(slides, 1):
                    relation = relations.get(slide.get(f"{{{R}}}id", ""))
                    if relation is None or relation[1]:
                        raise DocumentExtractionError(
                            "Presentation contains an invalid slide reference."
                        )
                    path = posixpath.normpath(posixpath.join("ppt", relation[0]))
                    node = self._parse_xml(archive.read(path), "PPTX slide")
                    # Neutral location labels must not copy excluded source text into context.
                    heading = (f"Slide {number}",)
                    tables = node.findall(f".//{{{A}}}tbl")
                    for region_number, graphic in enumerate(
                        node.findall(f".//{{{A}}}graphicData"), 1
                    ):
                        if graphic.find(f"{{{A}}}tbl") is None:
                            missing = builder.add(
                                EvidenceBlockKind.EXTERNAL_REFERENCE,
                                f"Slide {number}, graphic {region_number}",
                                heading,
                                text="Graphic content unavailable: "
                                "chart, diagram or embedded object.",
                            )
                            builder.warnings.append(
                                DocumentExtractionWarning(
                                    "unsupported_slide_graphic",
                                    ExtractionWarningSeverity.BLOCKING,
                                    f"Slide {number}, graphic {region_number}: "
                                    "visual relationships were not extracted.",
                                    missing.id,
                                )
                            )
                    table_paragraphs = {p for t in tables for p in t.iter(f"{{{A}}}p")}
                    table_number = paragraph_number = 0
                    for item in node.iter():
                        if item.tag == f"{{{A}}}tbl":
                            table_number += 1
                            visited_cells += len(item.findall(f"./{{{A}}}tr/{{{A}}}tc"))
                            if visited_cells > self._max_visited_spreadsheet_cells:
                                raise UnsupportedDocumentError(
                                    "Presentation tables exceed the cell limit."
                                )
                            self._pptx_table(item, number, table_number, builder)
                        elif item.tag == f"{{{A}}}p" and item not in table_paragraphs:
                            paragraph_number += 1
                            text = self._drawing_text(item)
                            if text:
                                builder.add(
                                    EvidenceBlockKind.PARAGRAPH,
                                    f"Slide {number}, paragraph {paragraph_number}",
                                    (*heading, "Slide text"),
                                    text=text,
                                )
                    rel_path = posixpath.join(
                        posixpath.dirname(path), "_rels", posixpath.basename(path) + ".rels"
                    )
                    for target, external in self._relationships(archive, rel_path).values():
                        if external:
                            builder.warnings.append(
                                DocumentExtractionWarning(
                                    "external_reference",
                                    ExtractionWarningSeverity.WARNING,
                                    f"Slide {number}: external reference was not fetched.",
                                )
                            )
                            continue
                        target_path = posixpath.normpath(
                            posixpath.join(posixpath.dirname(path), target)
                        )
                        if "notesSlides/notesSlide" in target_path and target_path.endswith(".xml"):
                            notes = self._parse_xml(archive.read(target_path), "PPTX speaker notes")
                            note_text = self._drawing_text(notes)
                            if note_text:
                                builder.add(
                                    EvidenceBlockKind.PARAGRAPH,
                                    f"Slide {number}, speaker notes",
                                    (*heading, "Speaker notes"),
                                    text=note_text,
                                )
                        elif self._image_mime(target_path):
                            mime, sanitized, _ = self._sanitize_image(
                                archive.read(target_path),
                                self._image_mime(target_path) or "image/png",
                            )
                            builder.add(
                                EvidenceBlockKind.IMAGE,
                                f"Slide {number}, image",
                                heading,
                                asset=(
                                    self._asset_id(target_path, sanitized),
                                    mime,
                                    target_path,
                                    sanitized,
                                ),
                            )
        except (zipfile.BadZipFile, KeyError) as exc:
            raise DocumentExtractionError("PPTX package is incomplete or malformed.") from exc
        if not builder.blocks:
            raise DocumentExtractionError("Presentation contains no reviewable content.")
        return self._result(
            "\n\n".join(b.text or "" for b in builder.blocks),
            builder,
            extraction_version=PPTX_EXTRACTION_VERSION,
        )

    @staticmethod
    def _drawing_text(node: ElementTree.Element) -> str:
        """Join formatting runs without inserting words; retain paragraph and explicit breaks."""
        paragraphs: list[str] = []
        for paragraph in node.iter(f"{{{A}}}p"):
            parts = [
                child.text or "" if child.tag == f"{{{A}}}t" else "\n"
                for child in paragraph.iter()
                if child.tag in {f"{{{A}}}t", f"{{{A}}}br"}
            ]
            paragraphs.append("".join(parts))
        return "\n".join(paragraphs).strip()

    def _pptx_table(
        self,
        table: ElementTree.Element,
        slide_number: int,
        table_number: int,
        builder: EvidenceBuilder,
    ) -> None:
        rows = table.findall(f"./{{{A}}}tr")
        width = len(table.findall(f"./{{{A}}}tblGrid/{{{A}}}gridCol"))
        if not rows or not width or table.findall(f".//{{{A}}}tbl"):
            raise DocumentExtractionError("Presentation table has no usable grid or rows.")
        location = f"Slide {slide_number}, table {table_number}"
        for row_number, row in enumerate(rows, 1):
            cells = row.findall(f"./{{{A}}}tc")
            if len(cells) != width:
                raise DocumentExtractionError(
                    f"{location}, row {row_number} does not match the table grid."
                )
            values: list[str] = []
            for column_number, cell in enumerate(cells, 1):
                annotations: list[str] = []
                for attribute, unit, maximum in (
                    ("gridSpan", "columns", width - column_number + 1),
                    ("rowSpan", "rows", len(rows) - row_number + 1),
                ):
                    try:
                        span = int(cell.get(attribute, "1"))
                    except ValueError as exc:
                        raise DocumentExtractionError(
                            f"{location} has an invalid merged-cell span."
                        ) from exc
                    if not 1 <= span <= maximum:
                        raise DocumentExtractionError(
                            f"{location} has a merged-cell span outside its grid."
                        )
                    if span > 1:
                        annotations.append(f"[spans {span} {unit}]")
                for attribute, direction in (("hMerge", "horizontal"), ("vMerge", "vertical")):
                    flag = cell.get(attribute, "false")
                    if flag not in {"true", "false", "0", "1"}:
                        raise DocumentExtractionError(
                            f"{location} has an invalid merged-cell flag."
                        )
                    if flag in {"true", "1"}:
                        annotations.append(f"[{direction} merge continuation]")
                # Never propagate an anchor's text into a continuation; that would defeat
                # row-level exclusions. Preserve even empty physical cell positions.
                body = cell.find(f"./{{{A}}}txBody")
                text = self._drawing_text(body) if body is not None else ""
                value = " ".join((text or "[empty cell]", *annotations))
                values.append(f"R{row_number}C{column_number}: {value}")
            builder.add(
                EvidenceBlockKind.TABLE_ROW,
                f"{location}, row {row_number}",
                (f"Slide {slide_number}", f"Table {table_number}"),
                text=" | ".join(values),
            )
        builder.warnings.append(
            DocumentExtractionWarning(
                "slide_table_structure",
                ExtractionWarningSeverity.WARNING,
                f"{location}: verify cell order and merged regions against the original. "
                "R/C labels are positions, not inferred headers; bracketed markers describe "
                "empty or merged cells, not source wording.",
            )
        )
