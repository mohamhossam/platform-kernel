"""Workbook merged regions are explicit, row-local and safely bounded."""

import pytest

from smb_kernel.documents.model import EvidenceBlockKind
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)
from tests.spreadsheet_fixtures import (
    XLSX_MIME,
    reviewed_spreadsheet_document,
    spreadsheet_document,
    with_merge_ranges,
)


def test_merged_anchor_wording_is_never_copied_to_continuations() -> None:
    extractor = SafeDocumentTextExtractor()
    content = reviewed_spreadsheet_document()
    result = extractor.extract_structured(XLSX_MIME, content)
    assert result == extractor.extract_structured(XLSX_MIME, content)
    assert result.extraction_version == "structured-xlsx-sections-v3"
    assert [b.label for b in result.evidence_blocks] == [
        "Worksheet 1",
        "Worksheet 1!1:1",
        "Worksheet 1!2:2",
        "Worksheet 1!3:3",
        "Hidden worksheet: Worksheet 2",
        "Worksheet 2!1:1",
    ]
    assert result.evidence_blocks[1].text == (
        "A1=Private header [merged A1:C1 anchor] | "
        "B1=[merged A1:C1 continuation; anchor A1] | "
        "C1=[merged A1:C1 continuation; anchor A1]"
    )
    public = result.evidence_blocks[3]
    assert public.kind is EvidenceBlockKind.WORKSHEET_RANGE
    assert (public.text or "").startswith("A3=[merged A2:A3 continuation; anchor A2] | B3=XGPON")
    assert "Private" not in (public.text or "") + public.label + "/".join(public.section_path)
    assert [w.code for w in result.warnings].count("worksheet_merged_cells") == 2
    assert any(w.code == "hidden_worksheet" for w in result.warnings)


def test_two_dimensional_merge_includes_empty_tail_rows_beyond_declared_dimension() -> None:
    content = with_merge_ranges(spreadsheet_document({"A1": "Anchor"}), ("A1:B3",), dimension="A1")
    result = SafeDocumentTextExtractor().extract_structured(XLSX_MIME, content)
    assert result.evidence_blocks[-1].label == "Worksheet 1!3:3"
    assert result.evidence_blocks[-1].text == (
        "A3=[merged A1:B3 continuation; anchor A1] | B3=[merged A1:B3 continuation; anchor A1]"
    )
    assert result.text.count("Anchor") == 1


def test_adjacent_and_empty_merged_anchors_are_distinct() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        XLSX_MIME, spreadsheet_document({"D2": "Tail"}, ("A1:B2", "C1:D1"))
    )
    assert result.evidence_blocks[1].text == (
        "A1=[empty cell] [merged A1:B2 anchor] | B1=[merged A1:B2 continuation; anchor A1] | "
        "C1=[empty cell] [merged C1:D1 anchor] | D1=[merged C1:D1 continuation; anchor C1]"
    )


def test_formulas_and_bilingual_multiline_text_remain_in_their_own_cells() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        XLSX_MIME,
        spreadsheet_document({"A1": "=1+1", "C1": "التغطية\nXGPON | literal"}, ("A1:B2",)),
    )
    assert "A1==1+1 (cached value unavailable) [merged A1:B2 anchor]" in result.text
    assert "C1=التغطية\nXGPON | literal" in result.text
    assert "=1+1" not in (result.evidence_blocks[-1].text or "")
    assert any(w.code == "formula_cache_unavailable" for w in result.warnings)


@pytest.mark.parametrize(
    "reference", ["", "bogus", "A:B", "1:2", "A0:B1", "B2:A1", "A1", "A1:A1", "Other!A1:B2"]
)
def test_invalid_merge_metadata_fails_explicitly(reference: str) -> None:
    content = with_merge_ranges(spreadsheet_document({"A1": "Rule"}), (reference,))
    with pytest.raises(DocumentExtractionError, match="merged range is invalid"):
        SafeDocumentTextExtractor().extract_structured(XLSX_MIME, content)


@pytest.mark.parametrize("references", [("A1:B2", "B2:C3"), ("A1:B2", "A1:B2")])
def test_overlapping_or_duplicate_merges_fail_without_silently_choosing_an_anchor(
    references: tuple[str, ...],
) -> None:
    with pytest.raises(DocumentExtractionError, match="overlap"):
        SafeDocumentTextExtractor().extract_structured(
            XLSX_MIME, with_merge_ranges(spreadsheet_document({"A1": "Rule"}), references)
        )


@pytest.mark.parametrize("reference", ["A1:ALM2", "A1:B100001"])
def test_merged_dimensions_cannot_bypass_worksheet_bounds(reference: str) -> None:
    with pytest.raises(UnsupportedDocumentError, match="safe extraction bounds"):
        SafeDocumentTextExtractor().extract_structured(
            XLSX_MIME, with_merge_ranges(spreadsheet_document({"A1": "Rule"}), (reference,))
        )


def test_merge_budget_is_checked_before_expanding_rows_and_across_sheets() -> None:
    with pytest.raises(UnsupportedDocumentError, match="visited cells"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=3).extract_structured(
            XLSX_MIME, with_merge_ranges(spreadsheet_document({"A1": "Rule"}), ("A1:B2",))
        )
    with pytest.raises(UnsupportedDocumentError, match="merged ranges exceed"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=6).extract_structured(
            XLSX_MIME, reviewed_spreadsheet_document()
        )


def test_merge_extent_counts_blank_positions_in_the_total_traversal_budget() -> None:
    with pytest.raises(UnsupportedDocumentError, match="visited cells"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=20).extract_structured(
            XLSX_MIME, with_merge_ranges(spreadsheet_document({"A1": "Rule"}), ("Z10:Z11",))
        )


def test_content_in_a_declared_continuation_is_an_explicit_failure() -> None:
    with pytest.raises(DocumentExtractionError, match="continuation cell contains a value"):
        SafeDocumentTextExtractor().extract_structured(
            XLSX_MIME,
            with_merge_ranges(
                spreadsheet_document({"A1": "Rule", "B1": "Conflicting"}), ("A1:B1",)
            ),
        )
