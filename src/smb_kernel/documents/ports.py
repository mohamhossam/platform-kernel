"""Structured evidence extraction and blob storage boundaries for document bytes."""

from dataclasses import dataclass
from typing import Protocol

from smb_kernel.documents.model import (
    DocumentAsset,
    DocumentEvidenceBlock,
    DocumentExtractionWarning,
    DocumentVersionId,
)


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    extraction_version: str
    evidence_blocks: tuple[DocumentEvidenceBlock, ...]
    warnings: tuple[DocumentExtractionWarning, ...]
    assets: tuple[DocumentAsset, ...]


class DocumentExtractorPort(Protocol):
    def extract(self, mime_type: str, content: bytes) -> str: ...

    def extract_structured(self, mime_type: str, content: bytes) -> ExtractedDocument: ...

    def extract_asset(self, mime_type: str, content: bytes, package_path: str) -> bytes: ...


class DocumentStoragePort(Protocol):
    def put(self, version_id: DocumentVersionId, content: bytes) -> None: ...
    def get(self, version_id: DocumentVersionId) -> bytes: ...
    def delete(self, version_id: DocumentVersionId) -> None: ...


class DocumentScannerPort(Protocol):
    def scan(self, content: bytes) -> bool:
        """True when the content is clean; raises when no verdict can be reached."""
        ...
