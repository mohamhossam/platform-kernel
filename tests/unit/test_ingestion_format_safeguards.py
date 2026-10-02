"""Deterministic fixtures for optional renderers and unsupported format regions."""

import base64
import io
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from pypdf import PdfWriter

from smb_kernel.documents.local_ocr import extract_local_ocr
from smb_kernel.documents.office_preview import render_slide
from smb_kernel.documents.text_extractor import SafeDocumentTextExtractor
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)
from tests.presentation_fixtures import PPTX_MIME, drawing_paragraph, presentation


def test_corrupt_png_crc_is_an_explicit_extraction_error() -> None:
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8"
        "AAwMCAO+a1ioAAAAASUVORK5CYII="
    )
    with pytest.raises(DocumentExtractionError, match="Raster image decoding failed"):
        SafeDocumentTextExtractor().extract_structured("image/png", content)


def test_missing_slide_runtime_is_explicit() -> None:
    with pytest.raises(UnsupportedDocumentError, match="not configured"):
        render_slide(
            presentation(drawing_paragraph("Rule")),
            PPTX_MIME,
            "office-slide/1",
            "",
            SafeDocumentTextExtractor(),
        )


@pytest.mark.parametrize("external", [True, False])
def test_slide_preview_rejects_external_and_embedded_content_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
    external: bool,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(presentation(drawing_paragraph("Rule")))) as source:
        with zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            if external:
                target.writestr(
                    "ppt/slides/_rels/slide1.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="ext" TargetMode="External" Target="https://example.invalid/"/>'
                    "</Relationships>",
                )
            else:
                target.writestr("ppt/embeddings/active.bin", b"untrusted object")
    runner = Mock()
    monkeypatch.setattr(subprocess, "Popen", runner)
    with pytest.raises(UnsupportedDocumentError):
        render_slide(
            output.getvalue(),
            PPTX_MIME,
            "office-slide/1",
            "must-not-run",
            SafeDocumentTextExtractor(),
        )
    runner.assert_not_called()


def test_slide_renderer_failure_and_bounded_raster(monkeypatch: pytest.MonkeyPatch) -> None:
    content = presentation(drawing_paragraph("Rule"))
    killed_group = Mock()
    monkeypatch.setattr(os, "killpg", killed_group, raising=False)
    process = MagicMock()
    process.__enter__.return_value = process
    process.wait.side_effect = [subprocess.TimeoutExpired("renderer", 45), 0]
    process.poll.return_value = None
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(DocumentExtractionError, match="timed out"):
        render_slide(
            content, PPTX_MIME, "office-slide/1", "local-renderer", SafeDocumentTextExtractor()
        )
    if os.name == "nt":
        process.kill.assert_called_once()
    else:
        killed_group.assert_called_once()

    def converted(args: list[str], **kwargs: object) -> MagicMock:
        assert kwargs["stdout"] == subprocess.DEVNULL and "--headless" in args
        destination = Path(args[-1]).with_suffix(".pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=500, height=300)
        writer.write(destination)
        completed = MagicMock()
        completed.__enter__.return_value = completed
        completed.wait.return_value = 0
        completed.poll.return_value = 0
        return completed

    monkeypatch.setattr(subprocess, "Popen", converted)
    raster = render_slide(
        content, PPTX_MIME, "office-slide/1", "local-renderer", SafeDocumentTextExtractor()
    )
    assert raster.startswith(b"\xff\xd8")


def test_unsupported_slide_graphic_has_its_own_warning_scope() -> None:
    extracted = SafeDocumentTextExtractor().extract_structured(
        PPTX_MIME,
        presentation(
            drawing_paragraph("Keep this rule")
            + '<p:graphicFrame><a:graphic><a:graphicData uri="chart"/></a:graphic></p:graphicFrame>'
        ),
    )
    warning = next(w for w in extracted.warnings if w.code == "unsupported_slide_graphic")
    affected = next(b for b in extracted.evidence_blocks if b.id == warning.block_id)
    assert affected.label == "Slide 1, graphic 1"
    assert affected.text != "Keep this rule"


def test_ocr_heading_is_not_copied_to_descendant_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def item(label: str, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            label=SimpleNamespace(value=label),
            text=text,
            prov=[SimpleNamespace(page_no=1, bbox=SimpleNamespace(l=1, t=2, r=3, b=4))],
        )

    document = SimpleNamespace(
        iterate_items=lambda: iter(
            [
                (item("section_header", "PRIVATE ORIGINAL HEADING"), 1),
                (item("text", "A separately selected rule."), 1),
            ]
        )
    )
    result = SimpleNamespace(status=SimpleNamespace(value="success"), document=document)
    modules = {
        "docling.datamodel.base_models": SimpleNamespace(
            InputFormat=SimpleNamespace(PDF="pdf"), DocumentStream=SimpleNamespace
        ),
        "docling.datamodel.pipeline_options": SimpleNamespace(
            PdfPipelineOptions=SimpleNamespace, TesseractCliOcrOptions=SimpleNamespace
        ),
        "docling.document_converter": SimpleNamespace(
            PdfFormatOption=SimpleNamespace,
            DocumentConverter=lambda **_kwargs: SimpleNamespace(
                convert=lambda *_args, **_kw: result
            ),
        ),
    }
    monkeypatch.setattr(
        "smb_kernel.documents.local_ocr.importlib.import_module",
        modules.__getitem__,
    )
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    extracted = extract_local_ocr(output.getvalue(), "application/pdf", str(tmp_path), 5, 1000)
    assert extracted.evidence_blocks[0].text == "PRIVATE ORIGINAL HEADING"
    for block in extracted.evidence_blocks:
        assert "PRIVATE ORIGINAL HEADING" not in block.label
        assert all("PRIVATE ORIGINAL HEADING" not in path for path in block.section_path)
