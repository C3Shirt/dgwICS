"""Local CODESYS service control through the Windows Service Control Manager.

Service Running is distinct from a downloaded/running PLC application.
No elevation, service installation, startup-type change or remote SCM access.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time

from .contracts import write_json

SERVICES = {"runtime": "CODESYS Control Win V3 - x64", "gateway": "CODESYS Gateway V3"}
STATES = {1: "Stopped", 2: "StartPending", 3: "StopPending", 4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused"}


class ServiceStatus(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "service_type", "state", "controls", "win32_exit_code", "service_exit_code",
        "checkpoint", "wait_hint", "pid", "flags")]


def service_control(kind: str = "runtime", *, start: bool = False, timeout: float = 15) -> dict:
    result = {"service": SERVICES.get(kind), "requested_start": start, "started_by_call": False,
              "service_running": False, "plc_application_running_verified": False}
    manager = service = None
    try:
        if os.name != "nt" or kind not in SERVICES or type(start) is not bool or not 0 < timeout <= 60:
            raise ValueError("Expected Windows, a known CODESYS service, boolean start and timeout 0..60")
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenSCManagerW.restype = wintypes.HANDLE
        api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        api.OpenServiceW.restype = wintypes.HANDLE
        api.QueryServiceStatusEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        api.QueryServiceStatusEx.restype = wintypes.BOOL
        api.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        api.StartServiceW.restype = wintypes.BOOL
        api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        api.CloseServiceHandle.restype = wintypes.BOOL
        manager = api.OpenSCManagerW(None, None, 0x0001)  # local SC_MANAGER_CONNECT
        if not manager:
            raise ctypes.WinError(ctypes.get_last_error())
        service = api.OpenServiceW(manager, SERVICES[kind], 0x0004)  # SERVICE_QUERY_STATUS
        if not service:
            raise ctypes.WinError(ctypes.get_last_error())

        def query():
            value, size = ServiceStatus(), wintypes.DWORD()
            if not api.QueryServiceStatusEx(service, 0, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            result.update(state=STATES.get(value.state, str(value.state)), process_id=value.pid,
                          service_running=value.state == 4, win32_exit_code=value.win32_exit_code)
            return value.state

        state = query()
        result["state_before"] = result["state"]
        if start and state != 4:
            api.CloseServiceHandle(service)
            service = api.OpenServiceW(manager, SERVICES[kind], 0x0004 | 0x0010)  # QUERY_STATUS | START
            if not service:
                raise ctypes.WinError(ctypes.get_last_error())
            if not api.StartServiceW(service, 0, None):
                error = ctypes.get_last_error()
                if error != 1056:  # ERROR_SERVICE_ALREADY_RUNNING, including a concurrent start
                    raise ctypes.WinError(error)
            else:
                result["started_by_call"] = True
            deadline = time.monotonic() + timeout
            while query() != 4:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Service did not reach Running before the deadline")
                time.sleep(0.1)
        result["status"] = "ok"
    except (OSError, ValueError, TimeoutError) as exc:
        result.update(status="error", error=str(exc), windows_error=getattr(exc, "winerror", None))
        if result.get("windows_error") == 5:
            result["required_action"] = "Windows service START permission is required; this adapter does not elevate itself"
    finally:
        if service:
            api.CloseServiceHandle(service)
        if manager:
            api.CloseServiceHandle(manager)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start"))
    parser.add_argument("--kind", choices=SERVICES, default="runtime")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out and args.out.exists():
        parser.error("output exists; choose a fresh report path")
    result = service_control(args.kind, start=args.action == "start")
    if args.out:
        write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
