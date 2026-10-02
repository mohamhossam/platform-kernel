"""Optional local slide rasterization, invoked only in the resource-bounded child."""

import io
import os
import signal
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from smb_kernel.documents.ports import DocumentExtractorPort
from smb_kernel.errors import (
    DocumentExtractionError,
    UnsupportedDocumentError,
)


def _run_converter(arguments: list[str]) -> int:
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    with subprocess.Popen(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
    ) as process:
        try:
            return process.wait(timeout=45)
        finally:
            if sys.platform != "win32":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The renderer and all descendants already exited.
            elif process.poll() is None:
                process.kill()
            # Windows descendants also belong to the bounded child's kill-on-close job.
            process.wait(timeout=5)


def render_slide(
    content: bytes, mime_type: str, location: str, executable: str, extractor: DocumentExtractorPort
) -> bytes:
    if mime_type != "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        raise UnsupportedDocumentError("Slide previews require a PPTX source.")
    if not executable:
        raise UnsupportedDocumentError(
            "Slide rendering is not configured. Ask your administrator to enable local slide "
            "previews, or upload a PDF export for page previews."
        )
    try:
        number = int(location.removeprefix("office-slide/"))
    except ValueError as exc:
        raise UnsupportedDocumentError("Invalid slide location.") from exc
    if location != f"office-slide/{number}" or number < 1:
        raise UnsupportedDocumentError("Invalid slide location.")
    # Validate all normal format bounds before invoking the optional renderer.
    extractor.extract_structured(mime_type, content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".bin", ".exe", ".js", ".vbs")) or any(
                    part in name.lower() for part in ("vbaproject", "activex/", "embeddings/")
                ):
                    raise UnsupportedDocumentError(
                        "Active or embedded objects prevent safe slide previews."
                    )
                if name.endswith(".rels"):
                    data = archive.read(name)
                    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                        raise UnsupportedDocumentError("Unsafe XML prevents slide previews.")
                    for relation in ElementTree.fromstring(data):
                        target = relation.get("Target", "")
                        if (
                            relation.get("TargetMode", "").lower() == "external"
                            or ":" in target
                            or target.startswith(("/", "\\"))
                        ):
                            raise UnsupportedDocumentError(
                                "External links prevent safe slide previews. "
                                "Use a self-contained PDF export."
                            )
        with tempfile.TemporaryDirectory(prefix="smb-slide-preview-") as directory:
            root = Path(directory)
            source = root / "source.pptx"
            source.write_bytes(content)
            profile = root / "profile"
            user = profile / "user"
            user.mkdir(parents=True)
            (user / "registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                "</item></oor:items>",
                encoding="utf-8",
            )
            try:
                return_code = _run_converter(
                    [
                        executable,
                        f"-env:UserInstallation={profile.as_uri()}",
                        "--headless",
                        "--nologo",
                        "--nodefault",
                        "--norestore",
                        "--convert-to",
                        "pdf:impress_pdf_Export",
                        "--outdir",
                        str(root),
                        str(source),
                    ],
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DocumentExtractionError("Local slide rendering failed or timed out.") from exc
            pdf = root / "source.pdf"
            if return_code != 0 or not pdf.is_file() or pdf.stat().st_size > 50 * 1024 * 1024:
                raise DocumentExtractionError(
                    "Local slide rendering produced no bounded PDF preview."
                )
            return extractor.extract_asset(
                "application/pdf", pdf.read_bytes(), f"pdf-page/{number}"
            )
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocumentExtractionError("Slide preview source is malformed.") from exc
