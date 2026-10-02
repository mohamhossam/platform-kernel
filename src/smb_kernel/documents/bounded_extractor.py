"""Bounded, deadline-enforced subprocess boundary for document parsing."""

from __future__ import annotations

import multiprocessing
import os
import threading
from dataclasses import dataclass
from queue import Empty
from typing import Any, Literal, Protocol, cast

from smb_kernel.documents.ports import (
    DocumentExtractorPort,
    ExtractedDocument,
)
from smb_kernel.documents.process_resources import (
    ChildProcessResourceLimiter,
)
from smb_kernel.errors import (
    DocumentExtractionBusyError,
    DocumentExtractionError,
    DocumentExtractionTimeoutError,
    UnsupportedDocumentError,
)


@dataclass(frozen=True)
class ExtractionLimits:
    pdf_pages: int
    characters: int
    xml_nodes: int
    image_pixels: int
    spreadsheet_cells: int
    ocr_artifacts_path: str | None = None
    office_preview_executable: str = ""


class _StartSignal(Protocol):
    def wait(self) -> object: ...


class _ResultChannel(Protocol):
    def put(self, item: tuple[str, object]) -> None: ...


def _extract_child(
    start_event: _StartSignal,
    result_queue: _ResultChannel,
    operation: Literal["structured", "asset"],
    mime_type: str,
    content: bytes,
    package_path: str | None,
    limits: ExtractionLimits,
) -> None:
    start_event.wait()
    # Spreadsheet/image dependencies import NumPy. Keep its native runtimes from
    # reserving one worker arena per host CPU inside this intentionally small,
    # untrusted-document process. These values affect only the spawned child.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    from smb_kernel.documents.text_extractor import (
        SafeDocumentTextExtractor,
    )

    output = result_queue
    extractor = SafeDocumentTextExtractor(
        max_pdf_pages=limits.pdf_pages,
        max_extracted_characters=limits.characters,
        max_xml_nodes=limits.xml_nodes,
        max_decoded_image_pixels=limits.image_pixels,
        max_visited_spreadsheet_cells=limits.spreadsheet_cells,
    )
    try:
        if operation == "asset" and (package_path or "").startswith("office-slide/"):
            from smb_kernel.documents.office_preview import render_slide

            result: object = render_slide(
                content,
                mime_type,
                package_path or "",
                limits.office_preview_executable,
                extractor,
            )
            output.put(("ok", result))
            return
        result = (
            extractor.extract_structured(mime_type, content)
            if operation == "structured"
            else extractor.extract_asset(mime_type, content, package_path or "")
        )
        if (
            operation == "structured"
            and limits.ocr_artifacts_path
            and mime_type in {"application/pdf", "image/png", "image/jpeg"}
        ):
            from smb_kernel.documents.local_ocr import extract_local_ocr

            result = extract_local_ocr(
                content, mime_type, limits.ocr_artifacts_path, limits.pdf_pages, limits.characters
            )
        output.put(("ok", result))
    except UnsupportedDocumentError as exc:
        output.put(("unsupported", str(exc)))
    except DocumentExtractionError as exc:
        output.put(("extraction", str(exc)))
    except Exception:
        output.put(("extraction", "Document extraction failed."))


class BoundedSubprocessDocumentExtractor(DocumentExtractorPort):
    def __init__(
        self,
        *,
        concurrency: int,
        waiting_requests: int,
        deadline_seconds: float,
        memory_bytes: int,
        limits: ExtractionLimits,
        resource_limiter: ChildProcessResourceLimiter,
        process_context: multiprocessing.context.BaseContext,
    ) -> None:
        self._capacity = threading.BoundedSemaphore(concurrency + waiting_requests)
        self._executors = threading.BoundedSemaphore(concurrency)
        self._deadline = deadline_seconds
        self._memory_bytes = memory_bytes
        self._limits = limits
        self._limiter = resource_limiter
        # multiprocessing's platform-specific contexts expose Process/Queue/Event,
        # while typeshed's common BaseContext omits some of those factories.
        self._context = cast(Any, process_context)

    def extract(self, mime_type: str, content: bytes) -> str:
        return self.extract_structured(mime_type, content).text

    def extract_structured(self, mime_type: str, content: bytes) -> ExtractedDocument:
        result = self._run("structured", mime_type, content, None)
        if not isinstance(result, ExtractedDocument):
            raise DocumentExtractionError("Document child returned an invalid result.")
        return result

    def extract_asset(self, mime_type: str, content: bytes, package_path: str) -> bytes:
        result = self._run("asset", mime_type, content, package_path)
        if not isinstance(result, bytes):
            raise DocumentExtractionError("Document child returned invalid asset bytes.")
        return result

    def _run(
        self,
        operation: Literal["structured", "asset"],
        mime_type: str,
        content: bytes,
        package_path: str | None,
    ) -> object:
        if not self._capacity.acquire(blocking=False):
            raise DocumentExtractionBusyError(
                "Document extraction is busy. Retry after current work completes."
            )
        try:
            with self._executors:
                result_queue = self._context.Queue(maxsize=1)
                start_event = self._context.Event()
                process = self._context.Process(
                    target=_extract_child,
                    args=(
                        start_event,
                        result_queue,
                        operation,
                        mime_type,
                        content,
                        package_path,
                        self._limits,
                    ),
                    daemon=True,
                )
                process.start()
                handle: object | None = None
                try:
                    try:
                        process_id = process.pid
                        if process_id is None:
                            raise RuntimeError("Document extraction child has no process ID.")
                        handle = self._limiter.apply(process_id, self._memory_bytes)
                    except Exception as exc:
                        raise DocumentExtractionError(
                            "Document extraction process could not be resource constrained."
                        ) from exc
                    start_event.set()
                    try:
                        # Consume the pipe while the child is alive. Waiting for
                        # process exit first can deadlock when a large extracted
                        # document fills the multiprocessing queue pipe.
                        status, payload = result_queue.get(timeout=self._deadline)
                    except Empty as exc:
                        if process.is_alive():
                            process.terminate()
                            process.join()
                            raise DocumentExtractionTimeoutError(
                                f"Document extraction exceeded its "
                                f"{self._deadline:g}-second deadline."
                            ) from exc
                        raise DocumentExtractionError(
                            "Document extraction child exited without a usable result."
                        ) from exc
                    process.join(timeout=1.0)
                    if process.is_alive():
                        process.terminate()
                        process.join()
                        raise DocumentExtractionError(
                            "Document extraction child did not exit after returning a result."
                        )
                    if status == "unsupported":
                        raise UnsupportedDocumentError(str(payload))
                    if status != "ok":
                        raise DocumentExtractionError(str(payload))
                    return payload
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join()
                    self._limiter.release(handle)
                    result_queue.close()
                    result_queue.join_thread()
        finally:
            self._capacity.release()
