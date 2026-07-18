#!/usr/bin/env python3
"""One-shot setup helper for hybrid cloud deploy (方案 A)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import load_config, resolve_device_host, build_parser  # noqa: E402
from src.device_bridge import bridge_check, ensure_adb_ready  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify cloud bridge (RPC + adb)")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    args = parser.parse_args()

    config = load_config(args.config)
    run_parser = build_parser(config)
    parsed = run_parser.parse_args(["bridge-check"])
    device_host = resolve_device_host(parsed, config)
    ensure_adb_ready(parsed, config)

    adb_cfg = config.get("adb") or {}
    adb_host = str(adb_cfg.get("host") or "").strip() or None
    adb_port = int(adb_cfg.get("port") or 5555)

    report = bridge_check(device_host, adb_host, adb_port)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("ready"):
        print("\n[ok] Bridge ready for cloud deploy.", flush=True)
        return 0
    print("\n[fail] Fix RPC/adb connectivity before running qq_bot on cloud.", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
