"""Word table boundaries prevent copied headers/merges bypassing row exclusions."""

import io

import pytest
from PIL import Image

from smb_kernel.documents.model import EvidenceBlockKind
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)
from tests.word_table_fixtures import (
    DOCX_MIME,
    reviewed_word_table_document,
    word_cell,
    word_document,
    word_paragraph,
    word_row,
    word_table,
)


def test_headers_merges_and_nested_tables_never_copy_wording_between_rows() -> None:
    extractor = SafeDocumentTextExtractor()
    content = reviewed_word_table_document()
    result = extractor.extract_structured(DOCX_MIME, content)
    assert result == extractor.extract_structured(DOCX_MIME, content)
    assert result.extraction_version == "structured-docx-sections-v3"
    assert [b.label for b in result.evidence_blocks] == [
        "Paragraph 1",
        "Table 1, row 1",
        "Table 1, row 2",
        "Table 1, row 3",
        "Table 1 / R3C3 / Nested table 1, row 1",
        "Table 2, row 1",
    ]
    public = result.evidence_blocks[3]
    assert public.kind is EvidenceBlockKind.TABLE_ROW
    assert "Private" not in (public.text or "") + public.label + "/".join(public.section_path)
    assert "R3C1: [empty cell] [vertical merge continuation]" in (public.text or "")
    assert "Public note\n[nested table 1 follows separately]" in (public.text or "")
    assert len({result.evidence_blocks[i].section_path for i in (3, 4, 5)}) == 3
    assert result.text.count("Private nested rule") == 1
    assert len(result.warnings) == 4


def test_grid_spans_and_omissions_preserve_positions_without_inventing_cells() -> None:
    table = word_table(
        word_row(
            word_cell("Merged", '<w:gridSpan w:val="2"/>'),
            word_cell("Tail"),
            properties='<w:gridBefore w:val="1"/><w:gridAfter w:val="1"/>',
        ),
        columns=5,
    )
    result = SafeDocumentTextExtractor().extract_structured(DOCX_MIME, word_document(table))
    assert result.evidence_blocks[0].text == (
        "R1C1-C1: [omitted grid positions] | R1C2: Merged [spans 2 columns] | "
        "R1C4: Tail | R1C5-C5: [omitted grid positions]"
    )
    assert all("grid is missing" not in w.message for w in result.warnings)


def test_grid_can_be_extended_by_span_but_uncertainty_is_visible() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME,
        word_document(
            word_table(word_row(word_cell("Wide", '<w:gridSpan w:val="3"/>')), columns=1)
        ),
    )
    assert result.evidence_blocks[0].text == "R1C1: Wide [spans 3 columns]"
    assert "differs from the rows" in result.warnings[0].message


def test_word_runs_paragraphs_and_explicit_breaks_preserve_cell_text() -> None:
    cell = (
        "<w:tc><w:p><w:r><w:t>XG</w:t></w:r><w:r><w:t>PON</w:t><w:br/>"
        "<w:t>شرط | literal</w:t><w:tab/><w:t>tab</w:t></w:r></w:p>"
        + word_paragraph("Next paragraph")
        + "</w:tc>"
    )
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME, word_document(word_table(word_row(cell), columns=1))
    )
    assert result.evidence_blocks[0].text == "R1C1: XGPON\nشرط | literal\ttab\nNext paragraph"


@pytest.mark.parametrize(
    "property_xml",
    [
        '<w:gridSpan w:val="oops"/>',
        '<w:gridSpan w:val="0"/>',
        '<w:gridSpan w:val="-1"/>',
        "<w:gridSpan/>",
        '<w:vMerge w:val="false"/>',
        '<w:hMerge w:val="unknown"/>',
    ],
)
def test_invalid_cell_metadata_fails_explicitly(property_xml: str) -> None:
    with pytest.raises(DocumentExtractionError, match="DOCX table"):
        SafeDocumentTextExtractor().extract_structured(
            DOCX_MIME, word_document(word_table(word_row(word_cell("Text", property_xml))))
        )


@pytest.mark.parametrize(
    "property_xml", ['<w:gridBefore w:val="-1"/>', '<w:gridAfter w:val="bad"/>']
)
def test_invalid_row_omissions_fail_explicitly(property_xml: str) -> None:
    with pytest.raises(DocumentExtractionError, match="DOCX table grid"):
        SafeDocumentTextExtractor().extract_structured(
            DOCX_MIME,
            word_document(word_table(word_row(word_cell("Text"), properties=property_xml))),
        )


def test_empty_table_and_row_fail_explicitly() -> None:
    for table in (word_table(), word_table(word_row())):
        with pytest.raises(DocumentExtractionError, match="contains no"):
            SafeDocumentTextExtractor().extract_structured(DOCX_MIME, word_document(table))


def test_cumulative_grid_and_nested_traversal_are_bounded() -> None:
    table = word_table(word_row(word_cell("Wide", '<w:gridSpan w:val="3"/>')), columns=3)
    with pytest.raises(UnsupportedDocumentError, match="cell limit"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=5).extract_structured(
            DOCX_MIME, word_document(table + table)
        )
    with pytest.raises(UnsupportedDocumentError, match="column limit"):
        SafeDocumentTextExtractor().extract_structured(
            DOCX_MIME,
            word_document(
                word_table(word_row(word_cell("Oversized", '<w:gridSpan w:val="1001"/>')))
            ),
        )
    for _ in range(8):
        table = word_table(word_row(word_cell("Outer", extra=table)), columns=1)
    with pytest.raises(UnsupportedDocumentError, match="depth limit"):
        SafeDocumentTextExtractor().extract_structured(DOCX_MIME, word_document(table))


def test_table_image_labels_do_not_copy_cell_or_heading_text() -> None:
    picture = io.BytesIO()
    Image.new("RGB", (240, 120), color=(20, 40, 80)).save(picture, format="PNG")
    cell = word_cell(
        "Private cell",
        extra='<w:p><w:r><w:drawing><a:blip r:embed="image"/></w:drawing></w:r></w:p>',
    )
    parts = {
        "word/media/image.png": picture.getvalue(),
        "word/_rels/document.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="image" Type="image" Target="media/image.png"/></Relationships>'
        ),
    }
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME,
        word_document(
            word_paragraph("Private heading", heading=True) + word_table(word_row(cell), columns=1),
            parts,
        ),
    )
    image = next(b for b in result.evidence_blocks if b.kind is EvidenceBlockKind.IMAGE)
    assert "Private" not in image.label + "/".join(image.section_path)
    assert image.section_path == ("Table 1",)
    assert "Table cell 1" in image.label
