"""System monitoring helpers used by VOCO tool wrappers."""

from __future__ import annotations

import csv
import ctypes
import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
from io import StringIO
from typing import Any

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DEFAULT_OUTPUT_LIMIT = 4000


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _truncate_text(text: str, limit: int = _DEFAULT_OUTPUT_LIMIT) -> str:
    normalized_limit = max(200, int(limit))
    if len(text) <= normalized_limit:
        return text
    return text[:normalized_limit]


def _run_command(command: list[str], timeout_seconds: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(1, int(timeout_seconds)),
        creationflags=_CREATE_NO_WINDOW,
    )


def _run_powershell(command: str, timeout_seconds: int = 20) -> subprocess.CompletedProcess[str]:
    normalized_command = str(command or "").strip()
    if not normalized_command:
        raise ValueError("PowerShell command is required.")
    return _run_command(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            normalized_command,
        ],
        timeout_seconds=timeout_seconds,
    )


def _extract_json_payload(text: str) -> str:
    payload = str(text or "").strip()
    if not payload:
        return ""
    if payload.startswith("{") or payload.startswith("["):
        return payload
    object_index = payload.find("{")
    array_index = payload.find("[")
    indices = [index for index in (object_index, array_index) if index >= 0]
    if not indices:
        return ""
    return payload[min(indices) :]


def _run_powershell_json(command: str, timeout_seconds: int = 20) -> Any:
    proc = _run_powershell(command=command, timeout_seconds=timeout_seconds)
    stdout = str(proc.stdout or "")
    stderr = str(proc.stderr or "")
    if proc.returncode != 0:
        detail = _truncate_text((stderr or stdout).strip(), 1000)
        raise RuntimeError(f"PowerShell exited with code {proc.returncode}: {detail}")

    payload = _extract_json_payload(stdout)
    if not payload:
        return []

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse PowerShell JSON output: {exc}") from exc


def _memory_snapshot() -> dict[str, int] | None:
    memory_status = _MEMORYSTATUSEX()
    memory_status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return None
    if not kernel32.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
        return None

    total_bytes = int(memory_status.ullTotalPhys)
    free_bytes = int(memory_status.ullAvailPhys)
    used_bytes = max(total_bytes - free_bytes, 0)
    return {
        "memory_load_percent": int(memory_status.dwMemoryLoad),
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
    }


def _uptime_seconds() -> int | None:
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return None
    try:
        return int(kernel32.kernel32.GetTickCount64() / 1000)
    except Exception:
        return None


def _process_count() -> int | None:
    proc = _run_command(["tasklist", "/FO", "CSV", "/NH"], timeout_seconds=15)
    if proc.returncode != 0:
        return None
    rows = [
        row
        for row in csv.reader(StringIO(proc.stdout or ""))
        if row and row[0].strip() and not row[0].strip().upper().startswith("INFO:")
    ]
    return len(rows)


def _parse_memory_kb(raw_value: str) -> int:
    digits = re.sub(r"[^\d]", "", str(raw_value or ""))
    return int(digits) if digits else 0


def _coerce_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _local_addresses() -> list[str]:
    discovered: set[str] = set()
    try:
        for family, *_rest, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = str(sockaddr[0]).strip()
            if address:
                discovered.add(address)
    except socket.gaierror:
        return []
    return sorted(discovered)


def get_health_snapshot() -> dict:
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\"
    disk_snapshot: dict[str, int | str | None] = {"path": system_drive}
    try:
        total_bytes, used_bytes, free_bytes = shutil.disk_usage(system_drive)
        disk_snapshot.update(
            {
                "total_bytes": int(total_bytes),
                "used_bytes": int(used_bytes),
                "free_bytes": int(free_bytes),
            }
        )
    except Exception:
        disk_snapshot.update({"total_bytes": None, "used_bytes": None, "free_bytes": None})

    return {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "cpu_count_logical": os.cpu_count() or 0,
        "uptime_seconds": _uptime_seconds(),
        "memory": _memory_snapshot(),
        "disk": disk_snapshot,
        "process_count": _process_count(),
    }


def list_running_processes(limit: int = 100) -> list[dict]:
    normalized_limit = max(1, min(int(limit), 1000))
    proc = _run_command(["tasklist", "/FO", "CSV", "/NH"], timeout_seconds=20)
    if proc.returncode != 0:
        detail = _truncate_text((proc.stderr or proc.stdout or "").strip(), 1000)
        raise RuntimeError(f"tasklist exited with code {proc.returncode}: {detail}")

    processes: list[dict] = []
    for row in csv.reader(StringIO(proc.stdout or "")):
        if len(row) < 5:
            continue
        image_name = str(row[0]).strip()
        if not image_name or image_name.upper().startswith("INFO:"):
            continue
        session_number = str(row[3]).strip()
        try:
            parsed_session_number: int | str = int(session_number)
        except ValueError:
            parsed_session_number = session_number

        processes.append(
            {
                "name": image_name,
                "pid": int(str(row[1]).strip()),
                "session_name": str(row[2]).strip(),
                "session_number": parsed_session_number,
                "memory_kb": _parse_memory_kb(row[4]),
            }
        )

    return processes[:normalized_limit]


def kill_process(pid: int | None = None, process_name: str | None = None, force: bool = True) -> dict:
    target_pid: int | None = None
    if pid is not None and str(pid).strip():
        target_pid = int(pid)
        if target_pid <= 0:
            raise ValueError("pid must be greater than zero.")

    target_name = str(process_name or "").strip()
    if target_pid is None and not target_name:
        raise ValueError("Either pid or process_name must be provided.")

    command = ["taskkill"]
    if target_pid is not None:
        command.extend(["/PID", str(target_pid)])
    else:
        command.extend(["/IM", target_name])
    if force:
        command.append("/F")

    proc = _run_command(command, timeout_seconds=20)
    output = _truncate_text((proc.stdout or proc.stderr or "").strip())
    if proc.returncode != 0:
        raise RuntimeError(f"taskkill exited with code {proc.returncode}: {output}")

    return {
        "pid": target_pid,
        "process_name": target_name or None,
        "force": bool(force),
        "returncode": int(proc.returncode),
        "output": output,
    }


def get_network_status() -> dict:
    command = (
        "$adapters = Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Select-Object Name,InterfaceDescription,Status,MacAddress,LinkSpeed;"
        "$ipConfigs = Get-NetIPConfiguration -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{"
        "InterfaceAlias = $_.InterfaceAlias; "
        "IPv4 = @($_.IPv4Address | ForEach-Object { $_.IPv4Address }); "
        "IPv6 = @($_.IPv6Address | ForEach-Object { $_.IPv6Address }); "
        "Gateway = @($_.IPv4DefaultGateway | ForEach-Object { $_.NextHop }); "
        "DnsServers = @($_.DNSServer.ServerAddresses); "
        "NetStatus = $_.NetAdapter.Status "
        "} "
        "};"
        "$profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
        "Select-Object InterfaceAlias,Name,NetworkCategory,IPv4Connectivity,IPv6Connectivity;"
        "[pscustomobject]@{adapters=$adapters; ip_configurations=$ipConfigs; profiles=$profiles} | "
        "ConvertTo-Json -Depth 6 -Compress"
    )

    powershell_error = ""
    try:
        raw = _run_powershell_json(command=command, timeout_seconds=20)
        if not isinstance(raw, dict):
            raise RuntimeError("Unexpected network payload format.")
        return {
            "hostname": socket.gethostname(),
            "local_addresses": _local_addresses(),
            "adapters": _coerce_list(raw.get("adapters")),
            "ip_configurations": _coerce_list(raw.get("ip_configurations")),
            "profiles": _coerce_list(raw.get("profiles")),
            "source": "powershell",
        }
    except Exception as exc:
        powershell_error = str(exc)

    fallback = _run_command(["ipconfig"], timeout_seconds=15)
    if fallback.returncode != 0:
        detail = _truncate_text((fallback.stderr or fallback.stdout or "").strip(), 1000)
        raise RuntimeError(f"Network status lookup failed. PowerShell: {powershell_error}; ipconfig: {detail}")
    return {
        "hostname": socket.gethostname(),
        "local_addresses": _local_addresses(),
        "source": "ipconfig",
        "powershell_error": powershell_error,
        "ipconfig_excerpt": _truncate_text(str(fallback.stdout or "").strip()),
    }


def list_usb_devices() -> list[dict]:
    command = (
        "$devices = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { "
        "$_.Class -eq 'USB' -or $_.InstanceId -like 'USB*' "
        "} | Select-Object FriendlyName,InstanceId,Status,Class,Manufacturer;"
        "$devices | ConvertTo-Json -Depth 5 -Compress"
    )

    try:
        payload = _run_powershell_json(command=command, timeout_seconds=20)
        devices = _coerce_list(payload)
        normalized_devices: list[dict] = []
        for item in devices:
            normalized_devices.append(
                {
                    "name": str(item.get("FriendlyName", "") or ""),
                    "device_id": str(item.get("InstanceId", "") or ""),
                    "status": str(item.get("Status", "") or ""),
                    "class": str(item.get("Class", "") or ""),
                    "manufacturer": str(item.get("Manufacturer", "") or ""),
                }
            )
        return normalized_devices
    except Exception:
        pass

    fallback = _run_command(
        [
            "wmic",
            "path",
            "Win32_PnPEntity",
            "where",
            "PNPClass='USB'",
            "get",
            "Name,DeviceID,Status,Manufacturer",
            "/format:csv",
        ],
        timeout_seconds=20,
    )
    if fallback.returncode != 0:
        detail = _truncate_text((fallback.stderr or fallback.stdout or "").strip(), 1000)
        raise RuntimeError(f"USB lookup failed: {detail}")

    rows = list(csv.DictReader(StringIO(fallback.stdout or "")))
    devices: list[dict] = []
    for row in rows:
        name = str(row.get("Name", "") or "").strip()
        device_id = str(row.get("DeviceID", "") or "").strip()
        if not name and not device_id:
            continue
        devices.append(
            {
                "name": name,
                "device_id": device_id,
                "status": str(row.get("Status", "") or "").strip(),
                "class": "USB",
                "manufacturer": str(row.get("Manufacturer", "") or "").strip(),
            }
        )
    return devices


def execute_powershell(command: str, timeout_seconds: int = 20, output_limit: int = _DEFAULT_OUTPUT_LIMIT) -> dict:
    normalized_command = str(command or "").strip()
    if not normalized_command:
        raise ValueError("PowerShell command is required.")

    proc = _run_powershell(command=normalized_command, timeout_seconds=timeout_seconds)
    stdout = str(proc.stdout or "").strip()
    stderr = str(proc.stderr or "").strip()
    output = stdout or stderr
    if proc.returncode != 0:
        detail = _truncate_text(output, output_limit)
        raise RuntimeError(f"PowerShell command failed (exit {proc.returncode}): {detail}")

    return {
        "command": normalized_command,
        "returncode": int(proc.returncode),
        "stdout": _truncate_text(stdout, output_limit),
        "stderr": _truncate_text(stderr, output_limit),
        "output": _truncate_text(output, output_limit),
    }


__all__ = [
    "execute_powershell",
    "get_health_snapshot",
    "get_network_status",
    "kill_process",
    "list_running_processes",
    "list_usb_devices",
]
