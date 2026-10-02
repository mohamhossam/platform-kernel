"""Word prose corrections and exclusions cannot leak through source metadata."""

import io

import pytest
from PIL import Image

from smb_kernel.documents.model import (
    EvidenceBlockKind,
    ExtractionWarningSeverity,
)
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from tests.word_table_fixtures import (
    DOCX_MIME,
    reviewed_word_prose_document,
    word_cell,
    word_document,
    word_paragraph,
    word_row,
    word_table,
)


def test_prose_labels_and_paths_are_positional_while_source_text_is_unchanged() -> None:
    extractor = SafeDocumentTextExtractor()
    result = extractor.extract_structured(DOCX_MIME, reviewed_word_prose_document())
    assert result == extractor.extract_structured(DOCX_MIME, reviewed_word_prose_document())
    assert result.extraction_version == "structured-docx-sections-v3"
    assert [b.label for b in result.evidence_blocks] == [f"Paragraph {n}" for n in range(1, 6)]
    assert [b.kind for b in result.evidence_blocks] == [
        EvidenceBlockKind.HEADING,
        EvidenceBlockKind.PARAGRAPH,
        EvidenceBlockKind.LIST_ITEM,
        EvidenceBlockKind.HEADING,
        EvidenceBlockKind.PARAGRAPH,
    ]
    assert result.evidence_blocks[1].text == "Private paragraph XGPON التغطية مطلوبة."
    assert all("Private" not in b.label + "/".join(b.section_path) for b in result.evidence_blocks)
    assert result.evidence_blocks[2].section_path == ("Heading at paragraph 1",)
    assert result.evidence_blocks[4].section_path == ("Heading at paragraph 4",)
    assert any(w.code == "word_prose_structure" for w in result.warnings)


def test_blank_and_skipped_contents_paragraphs_keep_source_positions() -> None:
    toc = word_paragraph("Private contents").replace(
        "<w:p>", '<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>', 1
    )
    body = "<w:p/>" + toc + word_paragraph("Same", heading=True).replace("Heading1", "Heading3")
    body += word_paragraph("First")
    body += word_paragraph("Same", heading=True).replace("Heading1", "Heading3")
    body += word_paragraph("Second")
    body += word_paragraph("Deep", heading=True).replace("Heading1", "Heading5")
    body += word_paragraph("Nested")
    body += word_paragraph("Root", heading=True) + word_paragraph("Root text")
    blocks = (
        SafeDocumentTextExtractor()
        .extract_structured(DOCX_MIME, word_document(body))
        .evidence_blocks
    )
    assert [b.label for b in blocks] == [f"Paragraph {n}" for n in range(3, 11)]
    assert blocks[1].section_path == ("Heading at paragraph 3",)
    assert blocks[3].section_path == ("Heading at paragraph 5",)
    assert blocks[5].section_path == ("Heading at paragraph 5", "Heading at paragraph 7")
    assert blocks[7].section_path == ("Heading at paragraph 9",)
    assert all("Private contents" not in (b.text or "") for b in blocks)


def test_long_custom_style_heading_cannot_inflate_prose_labels() -> None:
    body = word_paragraph("عنوان " * 200, heading=True).replace("Heading1", "CustomTitle")
    styles = (
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:styleId="CustomTitle"><w:name w:val="Heading 2"/></w:style></w:styles>'
    )
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME,
        word_document(body + word_paragraph("Coverage"), {"word/styles.xml": styles.encode()}),
    )
    assert result.evidence_blocks[0].kind == EvidenceBlockKind.HEADING
    assert result.evidence_blocks[1].section_path == ("Heading at paragraph 1",)
    assert all(len(b.label) < 30 for b in result.evidence_blocks)


def test_images_and_external_references_have_neutral_section_paths() -> None:
    stream = io.BytesIO()
    Image.new("RGB", (240, 120), (50, 80, 130)).save(stream, format="PNG")
    image = '<w:p><w:r><w:drawing><a:blip r:embed="image"/></w:drawing></w:r></w:p>'
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="image" Target="media/picture.png"/>'
        '<Relationship Id="link" Target="https://example.invalid/reference" TargetMode="External"/>'
        "</Relationships>"
    )
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME,
        word_document(
            word_paragraph("Private heading", heading=True) + image,
            {
                "word/media/picture.png": stream.getvalue(),
                "word/_rels/document.xml.rels": relationships.encode(),
            },
        ),
    )
    assert {b.kind for b in result.evidence_blocks} == {
        EvidenceBlockKind.HEADING,
        EvidenceBlockKind.IMAGE,
        EvidenceBlockKind.EXTERNAL_REFERENCE,
    }
    assert all("Private" not in b.label + "/".join(b.section_path) for b in result.evidence_blocks)
    assert all(b.section_path == ("Heading at paragraph 1",) for b in result.evidence_blocks)


@pytest.mark.parametrize(
    ("headings", "table", "expected"),
    [
        ([], False, ExtractionWarningSeverity.INFO),
        (["Document control"], False, ExtractionWarningSeverity.INFO),
        (["Introduction & business objective"], False, ExtractionWarningSeverity.INFO),
        (["Functional requirements"], False, ExtractionWarningSeverity.BLOCKING),
        (["Document control", "Functional requirements"], False, ExtractionWarningSeverity.INFO),
        (["Document control"], True, ExtractionWarningSeverity.BLOCKING),
    ],
)
def test_neutral_paths_preserve_existing_unsupported_image_readiness(
    headings: list[str], table: bool, expected: ExtractionWarningSeverity
) -> None:
    body = "".join(
        word_paragraph(title, heading=True).replace("Heading1", f"Heading{n}")
        for n, title in enumerate(headings, 1)
    )
    image = '<w:p><w:r><w:drawing><a:blip r:embed="image"/></w:drawing></w:r></w:p>'
    body += word_table(word_row(word_cell("Rule", extra=image)), columns=1) if table else image
    body += word_paragraph("Coverage")
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="image" Target="media/picture.emf"/></Relationships>'
    )
    result = SafeDocumentTextExtractor().extract_structured(
        DOCX_MIME,
        word_document(
            body,
            {
                "word/media/picture.emf": b"unsupported",
                "word/_rels/document.xml.rels": relationships.encode(),
            },
        ),
    )
    image_warning = next(
        w
        for w in result.warnings
        if w.code in {"decorative_image_excluded", "unsupported_body_image"}
    )
    assert image_warning.severity == expected
