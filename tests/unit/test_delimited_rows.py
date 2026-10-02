"""Delimited record fidelity, bounded traversal and explicit adapter failures."""

import csv
import io

import pytest

from smb_kernel.documents.model import EvidenceBlockKind
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


@pytest.mark.parametrize("delimiter,mime", [(",", "text/csv"), ("\t", "text/tab-separated-values")])
def test_records_preserve_first_row_quotes_line_breaks_empty_fields_and_inert_values(
    delimiter: str, mime: str
) -> None:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerows(
        [
            ["Private header", "Private label", ""],
            ["XGPON", 'التغطية, مطلوبة\r\n"quoted"\tvalue | literal', '=HYPERLINK("bad")'],
            ["", "", ""],
            ["Tail", "", "Last"],
        ]
    )
    extractor = SafeDocumentTextExtractor()
    content = stream.getvalue().encode("utf-8-sig")
    result = extractor.extract_structured(mime, content)
    assert result == extractor.extract_structured(mime, content)
    assert result.extraction_version == "structured-delimited-rows-v1"
    assert [b.label for b in result.evidence_blocks] == ["Row 1", "Row 2", "Row 4"]
    assert all(b.kind is EvidenceBlockKind.TABLE_ROW for b in result.evidence_blocks)
    assert result.evidence_blocks[0].text == (
        "R1C1: Private header | R1C2: Private label | R1C3: [empty cell]"
    )
    public = result.evidence_blocks[1]
    assert public.text == (
        'R2C1: XGPON | R2C2: التغطية, مطلوبة\r\n"quoted"\tvalue | literal | R2C3: =HYPERLINK("bad")'
    )
    assert "Private" not in (public.text or "") + public.label + "/".join(public.section_path)
    assert "logical records" in result.warnings[0].message


def test_single_record_is_reviewable_without_inventing_a_header() -> None:
    result = SafeDocumentTextExtractor().extract_structured("text/csv", b"BCRM,Coverage")
    assert result.evidence_blocks[0].text == "R1C1: BCRM | R1C2: Coverage"


def test_blank_records_preserve_positions_and_do_not_supply_business_content() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        "text/csv", b"\n,\nBCRM,Rule\n\nCPP,Rule"
    )
    assert [b.label for b in result.evidence_blocks] == ["Row 3", "Row 5"]


@pytest.mark.parametrize("content", [b"", b"\n,\n,", b"a,b\nc", b'a,b\n"unclosed,c', b"\xff"])
def test_empty_malformed_or_invalid_utf8_fails_explicitly(content: bytes) -> None:
    with pytest.raises(DocumentExtractionError):
        SafeDocumentTextExtractor().extract_structured("text/csv", content)


def test_nul_is_rejected() -> None:
    with pytest.raises(UnsupportedDocumentError, match="NUL"):
        SafeDocumentTextExtractor().extract_structured("text/csv", b"a,\x00")


@pytest.mark.parametrize("content", [b"a,b,c", b",,", b"a,b\nc,d"])
def test_cell_budget_includes_first_and_blank_records(content: bytes) -> None:
    with pytest.raises(UnsupportedDocumentError, match="cell limit"):
        SafeDocumentTextExtractor(max_visited_spreadsheet_cells=2).extract_structured(
            "text/csv", content
        )


def test_record_and_column_limits_cover_empty_and_wide_input() -> None:
    extractor = SafeDocumentTextExtractor()
    with pytest.raises(UnsupportedDocumentError, match="column limit"):
        extractor.extract_structured("text/csv", b"," * 1000)
    with pytest.raises(UnsupportedDocumentError, match="row limit"):
        extractor.extract_structured("text/csv", b"\n" * 100001)


def test_rendered_character_budget_includes_coordinate_overhead() -> None:
    with pytest.raises(UnsupportedDocumentError, match="characters"):
        SafeDocumentTextExtractor(max_extracted_characters=5).extract_structured("text/csv", b"a")


def test_oversized_field_is_an_explicit_adapter_error() -> None:
    with pytest.raises(DocumentExtractionError, match="oversized field"):
        SafeDocumentTextExtractor().extract_structured(
            "text/csv", b"a" * (csv.field_size_limit() + 1)
        )
