#!/usr/bin/env python3
"""
Watch iBox HTTP captures in real-time.

Usage:
  python watch_capture.py                     # USB mode (adb forward)
  python watch_capture.py --host 192.168.x.x  # WiFi mode
  python watch_capture.py --interval 0.5      # poll every 0.5s (default 1s)

Prints each new capture as it arrives. Press Ctrl-C to stop.
"""

import argparse
import time
import sys
import json

from src.frida_client import get_connection, get_capture, RPC_HOST


def fmt_capture(c: dict) -> str:
    sep = "=" * 70
    lines = [
        "",
        sep,
        f"[capture] {c.get('method')} {c.get('url')}",
        "",
        "--- Request Headers ---",
    ]
    for k, v in (c.get("reqHeaders") or {}).items():
        lines.append(f"  {k}: {v}")

    enc = str(c.get("encBody") or "")
    lines += [
        "",
        "--- Encrypted Request Body ---",
        f"  {enc[:800]}{'…' if len(enc) > 800 else ''}",
        "",
        f"--- Response {c.get('respCode')} Headers ---",
    ]
    for k, v in (c.get("respHeaders") or {}).items():
        lines.append(f"  {k}: {v}")

    body = str(c.get("respBody") or "")
    # Try pretty-printing JSON
    try:
        body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
    except Exception:
        pass

    lines += [
        "",
        "--- Response Body ---",
        body[:1500] + ("…" if len(body) > 1500 else ""),
        sep,
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Watch iBox HTTP captures")
    parser.add_argument("--host", default=None, help="Phone IP for WiFi mode (default: USB/adb-forward)")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds (default 1.0)")
    args = parser.parse_args()

    host = args.host or RPC_HOST
    get_connection(host)  # connect + ping

    print(f"[watch] Listening for sail-api captures (interval={args.interval}s). Press Ctrl-C to stop.\n")

    last_seq = None

    while True:
        try:
            c = get_capture(host)
            if c and c.get("seq") != last_seq:
                last_seq = c.get("seq")
                print(fmt_capture(c))
                sys.stdout.flush()
        except KeyboardInterrupt:
            print("\n[watch] Stopped.")
            break
        except Exception as e:
            print(f"[watch] Error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
