"""Platform resource limits applied to untrusted document child processes."""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from smb_kernel.documents import process_resources
from smb_kernel.documents.process_resources import (
    PosixChildProcessResourceLimiter,
    WindowsChildProcessResourceLimiter,
    child_process_resource_limiter,
)

MEMORY = 512 * 1024 * 1024
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")


@pytest.mark.parametrize(
    ("os_name", "expected"),
    [("nt", WindowsChildProcessResourceLimiter), ("posix", PosixChildProcessResourceLimiter)],
)
def test_the_running_platform_selects_its_limiter(
    monkeypatch: pytest.MonkeyPatch, os_name: str, expected: type[object]
) -> None:
    monkeypatch.setattr(process_resources, "os", types.SimpleNamespace(name=os_name))
    assert isinstance(child_process_resource_limiter(), expected)


def test_posix_limiter_caps_the_child_address_space(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, tuple[int, int]]] = []
    fake = types.ModuleType("resource")
    fake.RLIMIT_AS = 9  # type: ignore[attr-defined]
    fake.prlimit = lambda pid, limit, values: calls.append((pid, limit, values))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "resource", fake)

    limiter = PosixChildProcessResourceLimiter()
    assert limiter.apply(41, MEMORY) is None
    limiter.release(None)

    assert calls == [(41, 9, (MEMORY, MEMORY))]


def test_windows_limiter_refuses_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_resources, "sys", types.SimpleNamespace(platform="linux"))
    limiter = WindowsChildProcessResourceLimiter()

    with pytest.raises(OSError, match="require Windows"):
        limiter.apply(41, MEMORY)
    with pytest.raises(OSError, match="require Windows"):
        limiter.release(object())
    limiter.release(None)


@windows_only
def test_windows_job_caps_child_memory() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.readline(); data = bytearray(400 * 1024 * 1024); "
            "print('allocated')",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    limiter = WindowsChildProcessResourceLimiter()
    handle = limiter.apply(child.pid, 128 * 1024 * 1024)
    try:
        out, err = child.communicate("go\n", timeout=60)
    finally:
        limiter.release(handle)

    assert child.returncode != 0
    assert "allocated" not in out
    assert "MemoryError" in err


@windows_only
def test_releasing_the_windows_job_terminates_the_child() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    limiter = WindowsChildProcessResourceLimiter()
    try:
        handle = limiter.apply(child.pid, MEMORY)
        assert handle is not None
        limiter.release(handle)
        assert child.wait(timeout=20) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@windows_only
def test_windows_limiter_reports_a_missing_process() -> None:
    with pytest.raises(OSError, match="OpenProcess failed"):
        WindowsChildProcessResourceLimiter().apply(0, MEMORY)


@windows_only
def test_windows_limiter_reports_a_process_it_cannot_assign() -> None:
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()

    with pytest.raises(OSError, match="AssignProcessToJobObject failed"):
        WindowsChildProcessResourceLimiter().apply(exited.pid, MEMORY)
