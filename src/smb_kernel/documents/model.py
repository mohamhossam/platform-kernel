"""What extraction produces: evidence blocks, assets and warnings.

These are the extractor's output model, not any application's document
lifecycle. Applications re-export them so their own entities keep one class
per concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DocumentError(Exception):
    """Base error for source-document behavior."""


class InvalidDocumentError(DocumentError):
    """Document metadata or lifecycle state is invalid."""


@dataclass(frozen=True)
class DocumentVersionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise InvalidDocumentError("Document version ID must not be blank.")


class EvidenceBlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    IMAGE = "image"
    WORKSHEET_RANGE = "worksheet_range"
    EXTERNAL_REFERENCE = "external_reference"


class ExtractionWarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True)
class DocumentEvidenceBlock:
    id: str
    kind: EvidenceBlockKind
    ordinal: int
    section_path: tuple[str, ...]
    label: str
    content_fingerprint: str
    text: str | None = None
    asset_id: str | None = None

    def __post_init__(self) -> None:
        identifier = self.id.strip()
        label = self.label.strip()
        fingerprint = self.content_fingerprint.strip().lower()
        text = self.text.strip() if self.text is not None else None
        asset_id = self.asset_id.strip() if self.asset_id is not None else None
        sections = tuple(item.strip() for item in self.section_path if item.strip())
        if not identifier or self.ordinal < 1 or not label:
            raise InvalidDocumentError("Document evidence block metadata is incomplete.")
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise InvalidDocumentError("Evidence content fingerprint must be a SHA-256 digest.")
        if self.kind is EvidenceBlockKind.IMAGE:
            if not asset_id:
                raise InvalidDocumentError("Image evidence requires an asset ID.")
        elif not text:
            raise InvalidDocumentError("Text evidence requires nonblank content.")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "content_fingerprint", fingerprint)
        object.__setattr__(self, "section_path", sections)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "asset_id", asset_id)


@dataclass(frozen=True)
class DocumentAsset:
    id: str
    block_id: str
    mime_type: str
    package_path: str
    checksum_sha256: str

    def __post_init__(self) -> None:
        values = tuple(
            value.strip() for value in (self.id, self.block_id, self.mime_type, self.package_path)
        )
        checksum = self.checksum_sha256.strip().lower()
        if any(not value for value in values):
            raise InvalidDocumentError("Document asset metadata is incomplete.")
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise InvalidDocumentError("Document asset checksum must be a SHA-256 digest.")
        object.__setattr__(self, "id", values[0])
        object.__setattr__(self, "block_id", values[1])
        object.__setattr__(self, "mime_type", values[2].lower())
        object.__setattr__(self, "package_path", values[3])
        object.__setattr__(self, "checksum_sha256", checksum)


@dataclass(frozen=True)
class DocumentExtractionWarning:
    code: str
    severity: ExtractionWarningSeverity
    message: str
    block_id: str | None = None

    def __post_init__(self) -> None:
        code = self.code.strip()
        message = self.message.strip()
        block_id = self.block_id.strip() if self.block_id is not None else None
        if not code or not message:
            raise InvalidDocumentError("Document extraction warning is incomplete.")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "block_id", block_id)
