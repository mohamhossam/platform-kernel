"""Text heading wording never becomes unreviewable descendant metadata."""

import pytest

from smb_kernel.documents.model import EvidenceBlockKind
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown"])
def test_heading_wording_occurs_only_in_its_own_block(mime: str) -> None:
    source = "\ufeff\r\n# Private policy\r\n\r\nالتغطية مطلوبة XGPON\r\n"
    result = SafeDocumentTextExtractor().extract_structured(mime, source.encode())
    heading, paragraph = result.evidence_blocks
    assert result.extraction_version == "structured-text-sections-v1"
    assert heading.kind == EvidenceBlockKind.HEADING
    assert heading.text == "# Private policy" and heading.label == "Line 2"
    assert paragraph.text == "التغطية مطلوبة XGPON" and paragraph.label == "Line 4"
    assert heading.section_path == paragraph.section_path == ("Heading at line 2",)
    assert "Private" not in str(paragraph)
    assert result.warnings[0].code == "text_section_structure"


def test_skipped_levels_siblings_and_repeated_titles_keep_distinct_parents() -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        "text/markdown",
        b"### Same\nA\n### Same\nB\n##### Deep\nC\n# Root\nD",
    )
    paths = [block.section_path for block in result.evidence_blocks]
    assert paths[0] == paths[1] == ("Heading at line 1",)
    assert paths[2] == paths[3] == ("Heading at line 3",)
    assert paths[4] == paths[5] == ("Heading at line 3", "Heading at line 5")
    assert paths[6] == paths[7] == ("Heading at line 7",)


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown"])
def test_long_heading_does_not_inflate_descendant_labels(mime: str) -> None:
    result = SafeDocumentTextExtractor().extract_structured(
        mime, ("# " + "عنوان " * 300 + "\nCoverage").encode()
    )
    assert result.evidence_blocks[1].section_path == ("Heading at line 1",)


def test_literal_markup_and_links_remain_inert_reviewable_text() -> None:
    content = b"<script>alert(1)</script>\n![image](https://example.invalid/a)\n####### literal"
    result = SafeDocumentTextExtractor().extract_structured("text/markdown", content)
    assert result.text == content.decode()
    assert all(b.kind == EvidenceBlockKind.PARAGRAPH for b in result.evidence_blocks)
    assert all(b.section_path == () for b in result.evidence_blocks)
    assert not result.assets and not result.warnings


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown"])
@pytest.mark.parametrize("content", [b"", b" \r\n", b"\xff"])
def test_unusable_text_is_an_explicit_failure(mime: str, content: bytes) -> None:
    with pytest.raises(DocumentExtractionError):
        SafeDocumentTextExtractor().extract_structured(mime, content)


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown"])
def test_nul_and_character_limit_fail_explicitly(mime: str) -> None:
    with pytest.raises(UnsupportedDocumentError):
        SafeDocumentTextExtractor().extract_structured(mime, b"# Heading\n\x00")
    with pytest.raises(UnsupportedDocumentError, match="characters"):
        SafeDocumentTextExtractor(max_extracted_characters=5).extract_structured(
            mime, b"# Heading\nContent"
        )
