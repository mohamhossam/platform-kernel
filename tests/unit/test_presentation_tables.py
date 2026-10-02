"""PPTX table extraction contracts, including malformed but parseable OOXML."""

import pytest

from smb_kernel.documents.extraction_base import (
    PPTX_EXTRACTION_VERSION,
)
from smb_kernel.documents.model import EvidenceBlockKind
from smb_kernel.documents.text_extractor import (
    SafeDocumentTextExtractor,
)
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)
from tests.presentation_fixtures import (
    PPTX_MIME,
    drawing_paragraph,
    drawing_table,
    presentation,
    reviewed_table_presentation,
    table_cell,
)


def test_tables_are_ordered_rows_without_duplicate_slide_text_or_inferred_headers() -> None:
    slide = (
        drawing_paragraph("Before")
        + drawing_table(
            (table_cell("Channel"), table_cell("Rule")),
            (table_cell("BCRM"), table_cell("التغطية مطلوبة")),
            (table_cell("CPP"), table_cell("")),
        )
        + drawing_paragraph("After")
        + drawing_table((table_cell("Different table"), table_cell("Separate context")))
    )
    result = SafeDocumentTextExtractor().extract_structured(
        PPTX_MIME,
        presentation(
            slide, drawing_table((table_cell("Second slide"), table_cell("Rule"))), notes="Notes"
        ),
    )
    assert result.extraction_version == PPTX_EXTRACTION_VERSION
    blocks = result.evidence_blocks
    assert [b.label for b in blocks] == [
        "Slide 1, paragraph 1",
        "Slide 1, table 1, row 1",
        "Slide 1, table 1, row 2",
        "Slide 1, table 1, row 3",
        "Slide 1, paragraph 2",
        "Slide 1, table 2, row 1",
        "Slide 1, speaker notes",
        "Slide 2, table 1, row 1",
    ]
    assert blocks[2].text == "R2C1: BCRM | R2C2: التغطية مطلوبة"
    assert blocks[3].text == "R3C1: CPP | R3C2: [empty cell]"
    assert blocks[1].kind is EvidenceBlockKind.TABLE_ROW
    assert len({blocks[i].section_path for i in (0, 1, 5, 6, 7)}) == 5
    assert result.text.count("التغطية مطلوبة") == 1
    assert len(result.warnings) == 3
    assert all(w.code == "slide_table_structure" for w in result.warnings)


def test_cell_runs_breaks_and_literal_delimiters_are_preserved() -> None:
    cell = (
        "<a:tc><a:txBody><a:p><a:r><a:t>XG</a:t></a:r><a:r><a:t>PON</a:t></a:r>"
        "<a:br/><a:r><a:t>شرط | literal</a:t></a:r></a:p>"
        "<a:p><a:r><a:t>Second paragraph</a:t></a:r></a:p></a:txBody></a:tc>"
    )
    result = SafeDocumentTextExtractor().extract_structured(
        PPTX_MIME, presentation(drawing_table((cell,), columns=1))
    )
    assert result.evidence_blocks[0].text == "R1C1: XGPON\nشرط | literal\nSecond paragraph"


def test_merged_cells_never_copy_anchor_text_into_continuation_rows() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        PPTX_MIME, reviewed_table_presentation()
    )
    assert "[spans 2 rows]" in (result.evidence_blocks[0].text or "")
    assert "[vertical merge continuation]" in (result.evidence_blocks[1].text or "")
    assert "Private operations" not in (result.evidence_blocks[1].text or "")
    assert all("Private" not in "/".join(b.section_path) for b in result.evidence_blocks)
    horizontal = SafeDocumentTextExtractor().extract_structured(
        PPTX_MIME,
        presentation(
            drawing_table((table_cell("Title", 'gridSpan="2"'), table_cell("", 'hMerge="1"')))
        ),
    )
    assert horizontal.evidence_blocks[0].text == (
        "R1C1: Title [spans 2 columns] | R1C2: [empty cell] [horizontal merge continuation]"
    )


@pytest.mark.parametrize(
    "attributes",
    ['gridSpan="no"', 'gridSpan="0"', 'gridSpan="3"', 'rowSpan="2"', 'vMerge="yes"', 'hMerge="-1"'],
)
def test_invalid_merge_metadata_is_an_explicit_extraction_failure(attributes: str) -> None:
    with pytest.raises(DocumentExtractionError, match="merged-cell"):
        SafeDocumentTextExtractor().extract_structured(
            PPTX_MIME,
            presentation(drawing_table((table_cell("value", attributes), table_cell("")))),
        )


def test_table_grid_and_aggregate_cell_limits_fail_explicitly() -> None:
    extractor = SafeDocumentTextExtractor()
    with pytest.raises(DocumentExtractionError, match="table grid"):
        extractor.extract_structured(
            PPTX_MIME, presentation(drawing_table((table_cell("only one"),)))
        )
    with pytest.raises(DocumentExtractionError, match="no usable grid"):
        extractor.extract_structured(PPTX_MIME, presentation(drawing_table(columns=0)))
    table = drawing_table((table_cell("one"), table_cell("two")))
    with pytest.raises(UnsupportedDocumentError, match="cell limit"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=3).extract_structured(
            PPTX_MIME, presentation(table, table)
        )
    with pytest.raises(UnsupportedDocumentError, match="characters"):
        SafeDocumentTextExtractor(max_extracted_characters=5).extract_structured(
            PPTX_MIME, presentation(table)
        )


def test_extraction_is_deterministic_and_format_versions_are_independent() -> None:
    extractor = SafeDocumentTextExtractor()
    content = reviewed_table_presentation()
    assert extractor.extract_structured(PPTX_MIME, content) == extractor.extract_structured(
        PPTX_MIME, content
    )
    assert (
        extractor.extract_structured("text/plain", b"Rule").extraction_version
        == "structured-text-sections-v1"
    )
