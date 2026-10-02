"""Limits and safety helpers every format extractor shares: bounded XML parsing,
archive and image checks, and the size-capped result."""

from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree

from PIL import Image, ImageOps, UnidentifiedImageError

from smb_kernel.documents.model import (
    DocumentAsset,
    DocumentEvidenceBlock,
    DocumentExtractionWarning,
    EvidenceBlockKind,
)
from smb_kernel.documents.ports import (
    ExtractedDocument,
)
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)

SUPPORTED_MIME_TYPES = {
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}
MAX_OFFICE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_OFFICE_PARTS = 5_000
MAX_WORKBOOK_SHEETS = 50
MAX_WORKBOOK_NONEMPTY_CELLS = 100_000
MAX_WORKBOOK_VISITED_CELLS = 1_000_000
MAX_WORKBOOK_ROWS = 100_000
MAX_WORKBOOK_COLUMNS = 1_000
MAX_PDF_PAGES = 200
MAX_EXTRACTED_CHARACTERS = 1_000_000
MAX_XML_NODES = 250_000
MAX_IMAGE_PIXELS = 20_000_000
EXTRACTION_VERSION = "structured-evidence-v2"
PPTX_EXTRACTION_VERSION = "structured-pptx-tables-v2"
DOCX_EXTRACTION_VERSION = "structured-docx-sections-v3"
XLSX_EXTRACTION_VERSION = "structured-xlsx-sections-v3"
DELIMITED_EXTRACTION_VERSION = "structured-delimited-rows-v1"
TEXT_EXTRACTION_VERSION = "structured-text-sections-v1"
MAX_WORD_TABLE_DEPTH = 8
MAX_WORD_GRID_COLUMNS = 1000

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@dataclass
class EvidenceBuilder:
    blocks: list[DocumentEvidenceBlock]
    warnings: list[DocumentExtractionWarning]
    assets: list[DocumentAsset]

    def add(
        self,
        kind: EvidenceBlockKind,
        label: str,
        section_path: tuple[str, ...],
        *,
        text: str | None = None,
        asset: tuple[str, str, str, bytes] | None = None,
    ) -> DocumentEvidenceBlock:
        ordinal = len(self.blocks) + 1
        normalized = (text or "").strip()
        fingerprint_source = normalized.encode("utf-8")
        asset_id: str | None = None
        if asset is not None:
            asset_id, mime_type, package_path, content = asset
            fingerprint_source = content
        fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
        block_id = hashlib.sha256(
            f"{ordinal}|{kind.value}|{label}|{fingerprint}".encode()
        ).hexdigest()[:24]
        block = DocumentEvidenceBlock(
            block_id,
            kind,
            ordinal,
            section_path,
            label,
            fingerprint,
            normalized or None,
            asset_id,
        )
        self.blocks.append(block)
        if asset is not None:
            asset_id, mime_type, package_path, content = asset
            self.assets.append(
                DocumentAsset(
                    asset_id,
                    block_id,
                    mime_type,
                    package_path,
                    hashlib.sha256(content).hexdigest(),
                )
            )
        return block


class ExtractionBase:
    def __init__(
        self,
        *,
        max_pdf_pages: int = MAX_PDF_PAGES,
        max_extracted_characters: int = MAX_EXTRACTED_CHARACTERS,
        max_visited_spreadsheet_cells: int = MAX_WORKBOOK_VISITED_CELLS,
        max_decoded_image_pixels: int = MAX_IMAGE_PIXELS,
        max_xml_nodes: int = MAX_XML_NODES,
    ) -> None:
        self._max_pdf_pages = max_pdf_pages
        self._max_extracted_characters = max_extracted_characters
        self._max_visited_spreadsheet_cells = max_visited_spreadsheet_cells
        self._max_decoded_image_pixels = max_decoded_image_pixels
        self._max_xml_nodes = max_xml_nodes
        self._visited_xml_nodes = 0

    @staticmethod
    def _require_signature(content: bytes, signature: bytes, label: str) -> None:
        if not content.startswith(signature):
            raise UnsupportedDocumentError(
                f"The uploaded bytes do not match the declared {label} type."
            )

    @staticmethod
    def _decode_utf8(content: bytes) -> str:
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentExtractionError("TXT documents must use UTF-8 encoding.") from exc

    def _result(
        self,
        text: str,
        builder: EvidenceBuilder,
        *,
        extraction_version: str = EXTRACTION_VERSION,
    ) -> ExtractedDocument:
        if len(text) > self._max_extracted_characters:
            raise UnsupportedDocumentError(
                f"Extracted content exceeds {self._max_extracted_characters} characters."
            )
        return ExtractedDocument(
            text,
            extraction_version,
            tuple(builder.blocks),
            tuple(builder.warnings),
            tuple(builder.assets),
        )

    @staticmethod
    def _blocks_text(blocks: Iterable[DocumentEvidenceBlock]) -> str:
        return "\n".join(item.text for item in blocks if item.text)

    @staticmethod
    def _section_path(levels: dict[int, str]) -> tuple[str, ...]:
        return tuple(levels[level] for level in sorted(levels))

    @staticmethod
    def _asset_id(package_path: str, content: bytes) -> str:
        return hashlib.sha256(
            f"{package_path}|{hashlib.sha256(content).hexdigest()}".encode()
        ).hexdigest()[:24]

    def _sanitize_image(self, content: bytes, mime_type: str) -> tuple[str, bytes, bool]:
        try:
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > self._max_decoded_image_pixels:
                    raise UnsupportedDocumentError("Decoded image exceeds the safe pixel limit.")
                expected_format = "PNG" if mime_type == "image/png" else "JPEG"
                if source.format != expected_format:
                    raise DocumentExtractionError("Raster image format does not match its type.")
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                image = ImageOps.exif_transpose(source)
                decorative = image.width < 120 or image.height < 80
                image.thumbnail((1600, 1600))
                has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                output = io.BytesIO()
                if has_alpha:
                    image.convert("RGBA").save(output, format="PNG", optimize=True)
                    return "image/png", output.getvalue(), decorative
                image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
                return "image/jpeg", output.getvalue(), decorative
        except Image.DecompressionBombError as exc:
            raise UnsupportedDocumentError("Decoded image exceeds the safe pixel limit.") from exc
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise DocumentExtractionError("Raster image decoding failed.") from exc

    @staticmethod
    def _relationship_part(package_path: str) -> str:
        directory, filename = posixpath.split(package_path)
        return posixpath.join(directory, "_rels", f"{filename}.rels")

    def _relationships(self, archive: zipfile.ZipFile, path: str) -> dict[str, tuple[str, bool]]:
        if path not in archive.namelist():
            return {}
        root = self._parse_xml(archive.read(path), "Office relationships")
        return {
            item.get("Id", ""): (
                item.get("Target", ""),
                item.get("TargetMode", "").casefold() == "external",
            )
            for item in root.findall(f"{{{REL}}}Relationship")
            if item.get("Id") and item.get("Target")
        }

    @staticmethod
    def _image_mime(path: str) -> str | None:
        extension = PurePosixPath(path).suffix.casefold()
        return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(extension)

    @staticmethod
    def _office_archive(content: bytes, label: str) -> zipfile.ZipFile:
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
            entries = archive.infolist()
            if len(entries) > MAX_OFFICE_PARTS:
                archive.close()
                raise UnsupportedDocumentError(f"{label} contains too many package parts.")
            if any(
                info.filename.startswith(("/", "\\"))
                or ".." in info.filename.replace("\\", "/").split("/")
                for info in entries
            ):
                archive.close()
                raise UnsupportedDocumentError(f"{label} contains an unsafe archive path.")
            if sum(info.file_size for info in entries) > MAX_OFFICE_UNCOMPRESSED_BYTES:
                archive.close()
                raise UnsupportedDocumentError(f"{label} expanded content is too large.")
            names = {info.filename for info in entries}
            if any(name.casefold().endswith("vbaproject.bin") for name in names):
                archive.close()
                raise UnsupportedDocumentError(f"Macro-enabled {label} content is not supported.")
            return archive
        except zipfile.BadZipFile:
            raise

    def _parse_xml(self, content: bytes, label: str) -> ElementTree.Element:
        upper = content.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise UnsupportedDocumentError(f"{label} contains unsafe XML declarations.")
        try:
            root = ElementTree.fromstring(content)
            self._visited_xml_nodes += sum(1 for _ in root.iter())
            if self._visited_xml_nodes > self._max_xml_nodes:
                raise UnsupportedDocumentError(
                    f"The document contains more than {self._max_xml_nodes} XML nodes."
                )
            return root
        except ElementTree.ParseError as exc:
            raise DocumentExtractionError(f"{label} XML is malformed.") from exc

    @staticmethod
    def _non_blank(text: str, label: str) -> str:
        stripped = text.strip()
        if not stripped:
            raise DocumentExtractionError(f"{label} extraction produced no usable text.")
        return stripped
