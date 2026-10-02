"""Word (DOCX) paragraphs, headings, numbering, nested tables and images."""

from __future__ import annotations

import posixpath
import zipfile
from xml.etree import ElementTree

from smb_kernel.documents.extraction_base import (
    DOCX_EXTRACTION_VERSION,
    MAX_WORD_GRID_COLUMNS,
    MAX_WORD_TABLE_DEPTH,
    A,
    EvidenceBuilder,
    ExtractionBase,
    R,
    W,
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


class WordExtraction(ExtractionBase):
    def _docx(self, content: bytes) -> ExtractedDocument:
        builder = EvidenceBuilder([], [], [])
        try:
            with self._office_archive(content, "DOCX") as archive:
                names = set(archive.namelist())
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise DocumentExtractionError("DOCX package is missing required content.")
                document_xml = archive.read("word/document.xml")
                styles = self._docx_styles(archive)
                numbering = self._docx_numbering(archive)
                relationships = self._relationships(archive, "word/_rels/document.xml.rels")
                root = self._parse_xml(document_xml, "DOCX document")
                body = root.find(f"{{{W}}}body")
                if body is None:
                    raise DocumentExtractionError("DOCX body is missing.")
                if root.findall(f".//{{{W}}}txbxContent") or any(
                    name.startswith(
                        ("word/header", "word/footer", "word/footnotes", "word/endnotes")
                    )
                    and name.endswith(".xml")
                    for name in names
                ):
                    builder.warnings.append(
                        DocumentExtractionWarning(
                            "word_additional_regions",
                            ExtractionWarningSeverity.WARNING,
                            "Word headers, footers, text boxes and notes are not fully extracted. "
                            "Compare these regions in the original or upload a PDF export.",
                        )
                    )
                section_levels: dict[int, str] = {}
                paragraph_number = 0
                decorative_context = True
                image_number = 0
                table_number = 0
                remaining_table_cells = self._max_visited_spreadsheet_cells
                list_counters: dict[tuple[str, int], int] = {}
                for child in body:
                    if child.tag == f"{{{W}}}p":
                        paragraph_number += 1
                        style_id = self._paragraph_style(child)
                        style_name = styles.get(style_id, style_id)
                        if style_id.startswith("TOC") or style_name.casefold().startswith(
                            "table of contents"
                        ):
                            continue
                        text = self._word_text(child)
                        heading_level = self._heading_level(style_id, style_name)
                        if text:
                            if heading_level is not None:
                                if not section_levels or heading_level <= min(section_levels):
                                    decorative_context = text.casefold() in {
                                        "document control",
                                        "introduction & business objective",
                                    }
                                section_levels = {
                                    level: value
                                    for level, value in section_levels.items()
                                    if level < heading_level
                                }
                                section_levels[heading_level] = (
                                    f"Heading at paragraph {paragraph_number}"
                                )
                                section_path = tuple(
                                    section_levels[level] for level in sorted(section_levels)
                                )
                                builder.add(
                                    EvidenceBlockKind.HEADING,
                                    f"Paragraph {paragraph_number}",
                                    section_path,
                                    text=text,
                                )
                            else:
                                section_path = self._section_path(section_levels)
                                kind = (
                                    EvidenceBlockKind.LIST_ITEM
                                    if child.find(f".//{{{W}}}numPr") is not None
                                    or style_id == "ListParagraph"
                                    else EvidenceBlockKind.PARAGRAPH
                                )
                                if kind is EvidenceBlockKind.LIST_ITEM:
                                    text = self._numbered_list_text(
                                        child, text, numbering, list_counters
                                    )
                                builder.add(
                                    kind, f"Paragraph {paragraph_number}", section_path, text=text
                                )
                    elif child.tag == f"{{{W}}}tbl":
                        table_number += 1
                        remaining_table_cells = self._docx_table(
                            child, f"Table {table_number}", builder, remaining_table_cells, 1
                        )
                    image_number = self._docx_images(
                        child,
                        image_number,
                        (f"Table {table_number}",)
                        if child.tag == f"{{{W}}}tbl"
                        else self._section_path(section_levels),
                        relationships,
                        names,
                        archive,
                        builder,
                        decorative_context=decorative_context and child.tag != f"{{{W}}}tbl",
                    )
                for target, external in relationships.values():
                    if external:
                        reference = builder.add(
                            EvidenceBlockKind.EXTERNAL_REFERENCE,
                            "External document reference",
                            self._section_path(section_levels),
                            text=target,
                        )
                        builder.warnings.append(
                            DocumentExtractionWarning(
                                "external_reference_not_followed",
                                ExtractionWarningSeverity.WARNING,
                                f"Referenced file {target!r} was not opened; upload it separately.",
                                reference.id,
                            )
                        )
        except zipfile.BadZipFile as exc:
            raise DocumentExtractionError("DOCX package is corrupt.") from exc
        except ElementTree.ParseError as exc:
            raise DocumentExtractionError("DOCX text XML is corrupt.") from exc
        if any(
            block.kind
            in {EvidenceBlockKind.HEADING, EvidenceBlockKind.PARAGRAPH, EvidenceBlockKind.LIST_ITEM}
            for block in builder.blocks
        ):
            builder.warnings.append(
                DocumentExtractionWarning(
                    "word_prose_structure",
                    ExtractionWarningSeverity.WARNING,
                    "Word headings, paragraphs and list items use neutral paragraph locations. "
                    "Original wording stays in its own reviewable passage, including after "
                    "correction or exclusion. Paragraph numbers count body paragraphs, including "
                    "blank and skipped contents entries; tables keep separate row locations.",
                )
            )
        text = self._non_blank(self._blocks_text(builder.blocks), "DOCX")
        return self._result(text, builder, extraction_version=DOCX_EXTRACTION_VERSION)

    @staticmethod
    def _paragraph_style(paragraph: ElementTree.Element) -> str:
        style = paragraph.find(f"./{{{W}}}pPr/{{{W}}}pStyle")
        return style.get(f"{{{W}}}val", "") if style is not None else ""

    @staticmethod
    def _heading_level(style_id: str, style_name: str) -> int | None:
        for value in (style_id, style_name.replace(" ", "")):
            if value.casefold().startswith("heading"):
                suffix = value[len("heading") :]
                if suffix.isdigit():
                    return int(suffix)
        return None

    @staticmethod
    def _word_text(node: ElementTree.Element) -> str:
        parts: list[str] = []
        for child in node.iter():
            if child.tag == f"{{{W}}}t" and child.text:
                parts.append(child.text)
            elif child.tag == f"{{{W}}}tab":
                parts.append("\t")
            elif child.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
                parts.append("\n")
        return "".join(parts).strip()

    def _docx_table(
        self,
        table: ElementTree.Element,
        location: str,
        builder: EvidenceBuilder,
        remaining_cells: int,
        depth: int,
    ) -> int:
        if depth > MAX_WORD_TABLE_DEPTH:
            raise UnsupportedDocumentError("DOCX table nesting exceeds the safe depth limit.")
        rows = table.findall(f"./{{{W}}}tr")
        if not rows:
            raise DocumentExtractionError(f"{location} contains no reviewable rows.")
        grid_width = len(table.findall(f"./{{{W}}}tblGrid/{{{W}}}gridCol"))
        if grid_width > MAX_WORD_GRID_COLUMNS:
            raise UnsupportedDocumentError("DOCX table grid exceeds the column limit.")
        uncertain_grid = not grid_width
        for row_number, row in enumerate(rows, start=1):
            before = self._word_grid_number(row.find(f"./{{{W}}}trPr/{{{W}}}gridBefore"), 0)
            after = self._word_grid_number(row.find(f"./{{{W}}}trPr/{{{W}}}gridAfter"), 0)
            column = before + 1
            values: list[str] = []
            if before:
                values.append(f"R{row_number}C1-C{before}: [omitted grid positions]")
            cells = row.findall(f"./{{{W}}}tc")
            if not cells:
                raise DocumentExtractionError(f"{location}, row {row_number} contains no cells.")
            nested: list[tuple[str, ElementTree.Element]] = []
            for cell in cells:
                span = self._word_grid_number(cell.find(f"./{{{W}}}tcPr/{{{W}}}gridSpan"), 1)
                if span < 1:
                    raise DocumentExtractionError("DOCX table gridSpan must be positive.")
                annotations = [f"[spans {span} columns]"] if span > 1 else []
                for name, direction in (("vMerge", "vertical"), ("hMerge", "horizontal")):
                    merge = cell.find(f"./{{{W}}}tcPr/{{{W}}}{name}")
                    if merge is not None:
                        state = merge.get(f"{{{W}}}val", "continue")
                        if state not in {"restart", "continue"}:
                            raise DocumentExtractionError("DOCX table merge state is invalid.")
                        annotations.append(
                            f"[{direction} merge "
                            f"{'start' if state == 'restart' else 'continuation'}]"
                        )
                text, nested_tables = self._word_cell_content(cell)
                coordinate = f"R{row_number}C{column}"
                values.append(f"{coordinate}: " + " ".join((text or "[empty cell]", *annotations)))
                nested.extend(
                    (f"{location} / {coordinate} / Nested table {number}", inner)
                    for number, inner in enumerate(nested_tables, 1)
                )
                column += span
                if column - 1 > MAX_WORD_GRID_COLUMNS:
                    raise UnsupportedDocumentError("DOCX table grid exceeds the column limit.")
            if after:
                values.append(
                    f"R{row_number}C{column}-C{column + after - 1}: [omitted grid positions]"
                )
            width = column - 1 + after
            if width > MAX_WORD_GRID_COLUMNS:
                raise UnsupportedDocumentError("DOCX table grid exceeds the column limit.")
            remaining_cells -= width
            if remaining_cells < 0:
                raise UnsupportedDocumentError("DOCX tables exceed the cell limit.")
            uncertain_grid |= width != grid_width
            builder.add(
                EvidenceBlockKind.TABLE_ROW,
                f"{location}, row {row_number}",
                (location,),
                text=" | ".join(values),
            )
            for nested_location, inner in nested:
                remaining_cells = self._docx_table(
                    inner, nested_location, builder, remaining_cells, depth + 1
                )
        builder.warnings.append(
            DocumentExtractionWarning(
                "word_table_structure",
                ExtractionWarningSeverity.WARNING,
                f"{location}: verify grid positions, merged cells and nested tables against the "
                "original. R/C labels and bracketed markers describe structure; headers and "
                "merged-cell wording are not copied into other rows."
                + (
                    " The declared grid is missing or differs from the rows."
                    if uncertain_grid
                    else ""
                ),
            )
        )
        return remaining_cells

    @staticmethod
    def _word_grid_number(node: ElementTree.Element | None, default: int) -> int:
        if node is None:
            return default
        try:
            number = int(node.get(f"{{{W}}}val", ""))
        except ValueError as exc:
            raise DocumentExtractionError("DOCX table grid value is invalid.") from exc
        if number < 0:
            raise DocumentExtractionError("DOCX table grid value must not be negative.")
        if number > MAX_WORD_GRID_COLUMNS:
            raise UnsupportedDocumentError("DOCX table grid exceeds the column limit.")
        return number

    def _word_cell_content(
        self, cell: ElementTree.Element
    ) -> tuple[str, tuple[ElementTree.Element, ...]]:
        parts: list[str] = []
        nested: list[ElementTree.Element] = []
        pending = list(reversed(list(cell)))
        while pending:
            item = pending.pop()
            if item.tag == f"{{{W}}}tbl":
                nested.append(item)
                parts.append(f"[nested table {len(nested)} follows separately]")
            elif item.tag == f"{{{W}}}p":
                parts.append(self._word_text(item))
            else:
                pending.extend(reversed(list(item)))
        return "\n".join(parts).strip(), tuple(nested)

    def _docx_images(
        self,
        node: ElementTree.Element,
        image_number: int,
        section_path: tuple[str, ...],
        relationships: dict[str, tuple[str, bool]],
        names: set[str],
        archive: zipfile.ZipFile,
        builder: EvidenceBuilder,
        *,
        decorative_context: bool,
    ) -> int:
        cell_labels: dict[int, str] = {}
        for cell_number, cell in enumerate(node.findall(f".//{{{W}}}tc"), start=1):
            for cell_blip in cell.findall(f".//{{{A}}}blip"):
                cell_labels[id(cell_blip)] = f"Table cell {cell_number}"
        for blip in node.findall(f".//{{{A}}}blip"):
            rel_id = blip.get(f"{{{R}}}embed")
            target = relationships.get(rel_id or "")
            if target is None or target[1]:
                continue
            package_path = self._word_target(target[0])
            image_number += 1
            if package_path not in names:
                missing = builder.add(
                    EvidenceBlockKind.EXTERNAL_REFERENCE,
                    f"Image {image_number}",
                    section_path,
                    text="Image content unavailable: missing package part.",
                )
                builder.warnings.append(
                    DocumentExtractionWarning(
                        "missing_image_part",
                        ExtractionWarningSeverity.BLOCKING,
                        "A body image could not be read from the DOCX package.",
                        missing.id,
                    )
                )
                continue
            mime_type = self._image_mime(package_path)
            if mime_type is None:
                missing = builder.add(
                    EvidenceBlockKind.EXTERNAL_REFERENCE,
                    f"Image {image_number}",
                    section_path,
                    text="Image content unavailable: unsupported image format.",
                )
                builder.warnings.append(
                    DocumentExtractionWarning(
                        "decorative_image_excluded"
                        if decorative_context
                        else "unsupported_body_image",
                        ExtractionWarningSeverity.INFO
                        if decorative_context
                        else ExtractionWarningSeverity.BLOCKING,
                        f"Body image {package_path} was excluded because its format "
                        "cannot be submitted safely.",
                        missing.id,
                    )
                )
                continue
            try:
                mime_type, asset_bytes, decorative = self._sanitize_image(
                    archive.read(package_path), mime_type
                )
            except DocumentExtractionError:
                missing = builder.add(
                    EvidenceBlockKind.EXTERNAL_REFERENCE,
                    f"Image {image_number}",
                    section_path,
                    text="Image content unavailable: unsafe or invalid image.",
                )
                builder.warnings.append(
                    DocumentExtractionWarning(
                        "invalid_body_image",
                        ExtractionWarningSeverity.BLOCKING,
                        f"Body image {package_path} could not be decoded safely.",
                        missing.id,
                    )
                )
                continue
            if decorative:
                continue
            asset_id = self._asset_id(f"{package_path}#{image_number}", asset_bytes)
            builder.add(
                EvidenceBlockKind.IMAGE,
                f"Image {image_number} â€” {cell_labels[id(blip)]}"
                if id(blip) in cell_labels
                else f"Image {image_number}",
                section_path,
                asset=(asset_id, mime_type, package_path, asset_bytes),
            )
        return image_number

    def _docx_styles(self, archive: zipfile.ZipFile) -> dict[str, str]:
        if "word/styles.xml" not in archive.namelist():
            return {}
        root = self._parse_xml(archive.read("word/styles.xml"), "DOCX styles")
        result: dict[str, str] = {}
        for style in root.findall(f".//{{{W}}}style"):
            style_id = style.get(f"{{{W}}}styleId", "")
            name = style.find(f"{{{W}}}name")
            if style_id and name is not None:
                result[style_id] = name.get(f"{{{W}}}val", style_id)
        return result

    def _docx_numbering(
        self,
        archive: zipfile.ZipFile,
    ) -> dict[tuple[str, int], tuple[str, str, int]]:
        if "word/numbering.xml" not in archive.namelist():
            return {}
        root = self._parse_xml(archive.read("word/numbering.xml"), "DOCX numbering")
        abstract_levels: dict[tuple[str, int], tuple[str, str, int]] = {}
        for abstract in root.findall(f".//{{{W}}}abstractNum"):
            abstract_id = abstract.get(f"{{{W}}}abstractNumId", "")
            for level in abstract.findall(f"./{{{W}}}lvl"):
                level_number = int(level.get(f"{{{W}}}ilvl", "0"))
                format_node = level.find(f"./{{{W}}}numFmt")
                text_node = level.find(f"./{{{W}}}lvlText")
                start_node = level.find(f"./{{{W}}}start")
                abstract_levels[(abstract_id, level_number)] = (
                    format_node.get(f"{{{W}}}val", "decimal")
                    if format_node is not None
                    else "decimal",
                    text_node.get(f"{{{W}}}val", "%1.") if text_node is not None else "%1.",
                    int(start_node.get(f"{{{W}}}val", "1")) if start_node is not None else 1,
                )
        result: dict[tuple[str, int], tuple[str, str, int]] = {}
        for numbering in root.findall(f".//{{{W}}}num"):
            number_id = numbering.get(f"{{{W}}}numId", "")
            abstract_reference = numbering.find(f"./{{{W}}}abstractNumId")
            if abstract_reference is None:
                continue
            abstract_id = abstract_reference.get(f"{{{W}}}val", "")
            for (candidate_id, level_index), definition in abstract_levels.items():
                if candidate_id == abstract_id:
                    result[(number_id, level_index)] = definition
        return result

    @staticmethod
    def _numbered_list_text(
        paragraph: ElementTree.Element,
        text: str,
        numbering: dict[tuple[str, int], tuple[str, str, int]],
        counters: dict[tuple[str, int], int],
    ) -> str:
        number_properties = paragraph.find(f"./{{{W}}}pPr/{{{W}}}numPr")
        if number_properties is None:
            return f"â€¢ {text}"
        number = number_properties.find(f"./{{{W}}}numId")
        level = number_properties.find(f"./{{{W}}}ilvl")
        number_id = number.get(f"{{{W}}}val", "") if number is not None else ""
        level_number = int(level.get(f"{{{W}}}val", "0")) if level is not None else 0
        definition = numbering.get((number_id, level_number))
        if definition is None:
            return f"â€¢ {text}"
        number_format, template, start = definition
        key = number_id, level_number
        counters[key] = counters.get(key, start - 1) + 1
        if number_format == "bullet":
            marker = template.replace("\uf0b7", "â€¢") or "â€¢"
        else:
            marker = template
            for current_level in range(level_number + 1):
                current = counters.get((number_id, current_level), start)
                marker = marker.replace(f"%{current_level + 1}", str(current))
        return f"{marker} {text}".strip()

    @staticmethod
    def _word_target(target: str) -> str:
        return posixpath.normpath(posixpath.join("word", target)).lstrip("/")
