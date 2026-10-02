"""Actor and extraction-model invariants, and the fake identity mechanism."""

from __future__ import annotations

import pytest

from smb_kernel.documents.model import (
    DocumentAsset,
    DocumentError,
    DocumentEvidenceBlock,
    DocumentExtractionWarning,
    DocumentVersionId,
    EvidenceBlockKind,
    ExtractionWarningSeverity,
    InvalidDocumentError,
)
from smb_kernel.errors import AuthenticationRequiredError, ModelTransportError
from smb_kernel.identity.actor import ActorId, ActorProfile, InvalidIdentityError
from smb_kernel.identity.fake import FakeIdentityProvider
from smb_kernel.identity.ports import IdentityCredential

DIGEST = "a" * 64


def test_actor_profiles_normalise_content() -> None:
    actor = ActorProfile(ActorId(" amina "), " Amina ", " ", frozenset({" admin ", " "}))
    assert actor.id.value == "amina"
    assert actor.display_name == "Amina"
    assert actor.email is None
    assert actor.roles == frozenset({"admin"})
    assert actor.snapshot().display_name == "Amina"


@pytest.mark.parametrize("make", [lambda: ActorId(" "), lambda: ActorProfile(ActorId("a"), " ")])
def test_blank_identity_is_invalid(make) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidIdentityError):
        make()


def test_the_fake_provider_needs_personas_and_resolves_them() -> None:
    with pytest.raises(ValueError):
        FakeIdentityProvider(())
    personas = (ActorProfile(ActorId("one"), "One"), ActorProfile(ActorId("two"), "Two"))
    provider = FakeIdentityProvider(personas)
    assert provider.authenticate(IdentityCredential(None, None)).id.value == "one"
    assert provider.authenticate(IdentityCredential(None, "two")).id.value == "two"
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential(None, "three"))


def test_evidence_blocks_validate_and_normalise() -> None:
    block = DocumentEvidenceBlock(
        " b1 ", EvidenceBlockKind.PARAGRAPH, 1, (" Intro ", " "), " Para ", DIGEST.upper(), " hi "
    )
    assert (block.id, block.section_path, block.label, block.text) == (
        "b1",
        ("Intro",),
        "Para",
        "hi",
    )
    assert block.content_fingerprint == DIGEST
    with pytest.raises(InvalidDocumentError):
        DocumentEvidenceBlock("b", EvidenceBlockKind.IMAGE, 1, (), "Image", DIGEST)
    with pytest.raises(InvalidDocumentError):
        DocumentEvidenceBlock("b", EvidenceBlockKind.PARAGRAPH, 1, (), "P", "short", "x")


def test_assets_warnings_and_version_ids_validate() -> None:
    asset = DocumentAsset("a", "b", " IMAGE/PNG ", "word/media/1.png", DIGEST)
    assert asset.mime_type == "image/png"
    warning = DocumentExtractionWarning(" code ", ExtractionWarningSeverity.INFO, " note ")
    assert (warning.code, warning.message) == ("code", "note")
    for make in (
        lambda: DocumentAsset("a", "b", "image/png", "p", "bad"),
        lambda: DocumentExtractionWarning(" ", ExtractionWarningSeverity.INFO, "m"),
        lambda: DocumentVersionId(" "),
    ):
        with pytest.raises(InvalidDocumentError):
            make()
    assert issubclass(InvalidDocumentError, DocumentError)


def test_model_transport_errors_classify_safely() -> None:
    assert ModelTransportError("timeout").kind == "timeout"
    assert "unavailable" in str(ModelTransportError("unknown-kind"))
