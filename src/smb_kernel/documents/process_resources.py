"""Platform resource limits for untrusted document child processes."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any, Protocol, cast


class ChildProcessResourceLimiter(Protocol):
    def apply(self, process_id: int, memory_bytes: int) -> object | None: ...

    def release(self, handle: object | None) -> None: ...


class PosixChildProcessResourceLimiter:
    def apply(self, process_id: int, memory_bytes: int) -> object | None:
        import resource

        resource_module: Any = resource
        prlimit = cast(
            Callable[[int, int, tuple[int, int]], object],
            resource_module.prlimit,
        )
        address_space_limit = cast(int, resource_module.RLIMIT_AS)
        prlimit(process_id, address_space_limit, (memory_bytes, memory_bytes))
        return None

    def release(self, handle: object | None) -> None:
        del handle


class WindowsChildProcessResourceLimiter:
    def apply(self, process_id: int, memory_bytes: int) -> object:
        if sys.platform != "win32":
            raise OSError("Windows process resource limits require Windows.")
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimits()
        # Closing the owning job also terminates renderer descendants after a timeout.
        limits.BasicLimitInformation.LimitFlags = 0x00000100 | 0x00002000
        limits.ProcessMemoryLimit = memory_bytes
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        process = kernel.OpenProcess(0x0100 | 0x0001, False, process_id)
        if not process:
            kernel.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel.AssignProcessToJobObject(job, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except BaseException:
            kernel.CloseHandle(job)
            raise
        finally:
            kernel.CloseHandle(process)
        return job

    def release(self, handle: object | None) -> None:
        if handle is not None:
            if sys.platform != "win32":
                raise OSError("Windows process resource limits require Windows.")
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.CloseHandle(handle)


def child_process_resource_limiter() -> ChildProcessResourceLimiter:
    if os.name == "nt":
        return WindowsChildProcessResourceLimiter()
    return PosixChildProcessResourceLimiter()
