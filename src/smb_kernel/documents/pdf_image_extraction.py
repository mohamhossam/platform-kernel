"""PDF pages and standalone images."""

from __future__ import annotations

import io

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from smb_kernel.documents.extraction_base import (
    EvidenceBuilder,
    ExtractionBase,
)
from smb_kernel.documents.model import (
    EvidenceBlockKind,
)
from smb_kernel.documents.ports import (
    ExtractedDocument,
)
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


class PdfAndImageExtraction(ExtractionBase):
    def _standalone_image(self, content: bytes, mime_type: str) -> tuple[str, bytes, bool]:
        try:
            with Image.open(io.BytesIO(content)) as source:
                expected = "PNG" if mime_type == "image/png" else "JPEG"
                if source.format != expected:
                    raise UnsupportedDocumentError(
                        "Image content does not match its declared type."
                    )
            return self._sanitize_image(content, mime_type)
        except Image.DecompressionBombError as exc:
            raise UnsupportedDocumentError("Decoded image exceeds the safe pixel limit.") from exc
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise DocumentExtractionError("Raster image decoding failed.") from exc

    def _image(self, content: bytes, mime_type: str) -> ExtractedDocument:
        builder = EvidenceBuilder([], [], [])
        asset_mime, safe_bytes, _ = self._standalone_image(content, mime_type)
        # An explicitly uploaded small image is still intentional source evidence.
        builder.add(
            EvidenceBlockKind.IMAGE,
            "Uploaded image",
            ("Image",),
            asset=(self._asset_id("image", safe_bytes), asset_mime, "image", safe_bytes),
        )
        return self._result("", builder)

    def _render_pdf_page(self, content: bytes, number: int) -> tuple[str, bytes]:
        try:
            with pdfium.PdfDocument(content) as document:
                if len(document) > self._max_pdf_pages:
                    raise UnsupportedDocumentError(
                        f"PDF contains more than {self._max_pdf_pages} pages."
                    )
                if number < 1 or number > len(document):
                    raise DocumentExtractionError("PDF page asset was not found.")
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise DocumentExtractionError("PDF page dimensions are invalid.")
                    scale = min(2.0, 1600 / max(width, height))
                    if width * height * scale * scale > self._max_decoded_image_pixels:
                        raise UnsupportedDocumentError("PDF render exceeds the safe pixel limit.")
                    bitmap = page.render(scale=scale, may_draw_forms=False)
                    try:
                        output = io.BytesIO()
                        bitmap.to_pil().convert("RGB").save(output, format="JPEG", quality=85)
                        return "image/jpeg", output.getvalue()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        except (pdfium.PdfiumError, ValueError, OSError) as exc:
            raise DocumentExtractionError("PDF page rendering failed.") from exc

    def _pdf(self, content: bytes) -> ExtractedDocument:
        builder = EvidenceBuilder([], [], [])
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise UnsupportedDocumentError("Encrypted PDF documents are not supported.")
            if len(reader.pages) > self._max_pdf_pages:
                raise UnsupportedDocumentError(
                    f"PDF contains more than {self._max_pdf_pages} pages."
                )
            if not reader.pages:
                raise DocumentExtractionError("PDF has no usable pages.")
            for number, page in enumerate(reader.pages, start=1):
                label = f"Page {number}"
                text = (page.extract_text() or "").strip()
                if text:
                    builder.add(EvidenceBlockKind.PARAGRAPH, label, (label,), text=text)
                if not text and page.get("/Contents") is None:
                    continue
                mime_type, safe_bytes = self._render_pdf_page(content, number)
                package_path = f"pdf-page/{number}"
                builder.add(
                    EvidenceBlockKind.IMAGE,
                    f"{label} visual",
                    (label,),
                    asset=(
                        self._asset_id(package_path, safe_bytes),
                        mime_type,
                        package_path,
                        safe_bytes,
                    ),
                )
        except (PdfReadError, ValueError, OSError) as exc:
            raise DocumentExtractionError("PDF text extraction failed.") from exc
        if not builder.blocks:
            raise DocumentExtractionError("PDF contains no usable text or visual content.")
        return self._result(self._blocks_text(builder.blocks), builder)
