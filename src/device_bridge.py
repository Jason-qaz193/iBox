"""
Device bridge helpers for hybrid cloud deployment (方案 A).

Cloud VPS runs run.py / qq_bot.py; the phone stays at home and is reached via
Tailscale / frp / VPN.  This module wires up:

  - RPC  : TCP 27042  →  config device_host
  - adb  : TCP 5555   →  config adb.host:adb.port
"""

from __future__ import annotations

import socket
import subprocess
from typing import Any

_adb_serial: str | None = None


def set_adb_serial(serial: str | None) -> None:
    global _adb_serial
    _adb_serial = (serial or "").strip() or None


def get_adb_serial() -> str | None:
    return _adb_serial


def build_adb_cmd(*args: str) -> list[str]:
    cmd = ["adb"]
    if _adb_serial:
        cmd += ["-s", _adb_serial]
    cmd += list(args)
    return cmd


def run_adb(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        build_adb_cmd(*args),
        capture_output=True,
        timeout=timeout,
    )


def resolve_adb_config(parsed: Any, config: dict) -> str | None:
    if getattr(parsed, "adb_serial", None):
        return str(parsed.adb_serial).strip()
    adb_cfg = config.get("adb") or {}
    if isinstance(adb_cfg, dict):
        serial = adb_cfg.get("serial")
        if serial:
            return str(serial).strip()
    return None


def resolve_adb_endpoint(parsed: Any, config: dict) -> tuple[str | None, int]:
    if getattr(parsed, "adb_host", None):
        host = str(parsed.adb_host).strip()
        port = int(getattr(parsed, "adb_port", None) or 5555)
        return host or None, port
    adb_cfg = config.get("adb") or {}
    if isinstance(adb_cfg, dict):
        host = str(adb_cfg.get("host") or "").strip()
        port = int(adb_cfg.get("port") or 5555)
        if host:
            return host, port
    return None, 5555


def adb_connect(host: str, port: int = 5555) -> str:
    target = f"{host}:{port}"
    proc = run_adb("connect", target, timeout=20.0)
    out = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
    text = out or err
    if proc.returncode != 0 and "connected" not in text.lower():
        raise RuntimeError(f"adb connect {target} failed: {text or proc.returncode}")
    return target


def ensure_adb_ready(parsed: Any, config: dict) -> str | None:
    serial = resolve_adb_config(parsed, config)
    if serial:
        set_adb_serial(serial)
        return serial

    host, port = resolve_adb_endpoint(parsed, config)
    if not host:
        set_adb_serial(None)
        return None

    serial = adb_connect(host, port)
    set_adb_serial(serial)
    return serial


def check_tcp(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "ok"
    except OSError as exc:
        return False, str(exc)


def check_rpc(device_host: str, port: int = 27042) -> dict:
    ok, detail = check_tcp(device_host, port)
    result = {"ok": ok, "host": device_host, "port": port, "detail": detail}
    if not ok:
        return result
    try:
        from .frida_client import get_connection

        conn = get_connection(device_host)
        ping = conn.call({"type": "ping"}, timeout=8.0)
        result["ping"] = ping
        result["ok"] = bool(ping)
        if not ping:
            result["detail"] = "TCP open but RPC ping failed"
    except Exception as exc:
        result["ok"] = False
        result["detail"] = str(exc)
    return result


def check_adb_device() -> dict:
    serial = get_adb_serial()
    proc = run_adb("get-state", timeout=10.0)
    out = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
    text = out or err
    ok = proc.returncode == 0 and "device" in text.lower()
    result: dict = {"ok": ok, "serial": serial, "state": text}
    if not ok:
        result["detail"] = text or f"exit {proc.returncode}"
        return result

    proc = run_adb("shell", "echo", "ibox-bridge-ok", timeout=10.0)
    shell_out = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
    result["shell"] = shell_out
    result["ok"] = proc.returncode == 0 and "ibox-bridge-ok" in shell_out
    if not result["ok"]:
        result["detail"] = "adb device present but shell command failed"
    return result


def bridge_check(device_host: str, adb_host: str | None = None, adb_port: int = 5555) -> dict:
    report: dict = {"device_host": device_host, "rpc": check_rpc(device_host)}
    if adb_host:
        try:
            serial = adb_connect(adb_host, adb_port)
            set_adb_serial(serial)
        except Exception as exc:
            report["adb"] = {"ok": False, "host": adb_host, "port": adb_port, "detail": str(exc)}
        else:
            report["adb"] = {"host": adb_host, "port": adb_port, **check_adb_device()}
    elif get_adb_serial():
        report["adb"] = check_adb_device()
    else:
        report["adb"] = {"ok": None, "detail": "adb not configured (optional for non sale-rush)"}
    report["ready"] = bool(report["rpc"].get("ok")) and (
        report["adb"].get("ok") is not False
    )
    return report
