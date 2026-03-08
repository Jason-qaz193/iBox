#!/usr/bin/env python3
"""
Run login only or full purchase flow. Uses config/config.yaml.

Modes:
  (default)          RPC via WiFi or USB, using the app's own crypto
  --rpc              Same as default, kept for explicitness
  --python           Pure-Python fallback (experimental; HTTP format may change)
  --usb              RPC via USB + adb forward
  --host <ip>        RPC via phone IP on the same WiFi

Examples:
  python run.py sms <mobile>
  python run.py login <mobile> <code> [cId] [invitation]
  python run.py purchase <mobile> <code> <cId> <productId> [invitation]
  python run.py purchase <mobile> <code> --product-id <productId>

Notes:
  - cId defaults to login.c_id in config.yaml when omitted.
  - For purchase, use --product-id when cId comes from config; a single positional
    value after <code> is ambiguous and will be rejected.
"""

import argparse
import json
import os
import sys

import yaml


def load_config(path: str = None):
    path = path or os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iBox CLI")
    parser.add_argument("--rpc", action="store_true", help="Use RPC bridge mode")
    parser.add_argument(
        "--python",
        action="store_true",
        help="Use pure-Python crypto fallback (experimental)",
    )
    parser.add_argument("--usb", action="store_true", help="Use USB + adb forward for RPC")
    parser.add_argument("--host", help="Phone IP for RPC WiFi mode")

    subparsers = parser.add_subparsers(dest="command", required=True)

    sms_parser = subparsers.add_parser("sms", help="Send SMS code")
    sms_parser.add_argument("mobile")

    capture_parser = subparsers.add_parser("capture", help="Print the last captured HTTP exchange")
    capture_parser.set_defaults()

    login_parser = subparsers.add_parser("login", help="Login with SMS code")
    login_parser.add_argument("mobile")
    login_parser.add_argument("code")
    login_parser.add_argument("legacy_cid", nargs="?")
    login_parser.add_argument("legacy_invitation", nargs="?")
    login_parser.add_argument("--cid", dest="cid")
    login_parser.add_argument("--invitation", default="")

    purchase_parser = subparsers.add_parser("purchase", help="Login, then add to cart and create order")
    purchase_parser.add_argument("mobile")
    purchase_parser.add_argument("code")
    purchase_parser.add_argument("legacy_arg3", nargs="?")
    purchase_parser.add_argument("legacy_arg4", nargs="?")
    purchase_parser.add_argument("legacy_arg5", nargs="?")
    purchase_parser.add_argument("--cid", dest="cid")
    purchase_parser.add_argument("--product-id", dest="product_id")
    purchase_parser.add_argument("--invitation", default="")

    return parser


def resolve_mode(parsed: argparse.Namespace) -> bool:
    if parsed.rpc and parsed.python:
        raise SystemExit("Error: --rpc and --python cannot be used together")
    if parsed.python:
        return False
    return True


def resolve_device_host(parsed: argparse.Namespace, config: dict) -> str:
    if parsed.host:
        return parsed.host
    if parsed.usb:
        return "127.0.0.1"
    return config.get("device_host", "127.0.0.1")


def resolve_login_args(parsed: argparse.Namespace, config_c_id: str) -> tuple[str, str]:
    c_id = parsed.cid or parsed.legacy_cid or config_c_id
    invitation_code = parsed.invitation or parsed.legacy_invitation or ""
    if not c_id:
        raise SystemExit("Error: cId is required. Pass --cid, use the positional cId, or set login.c_id in config.yaml")
    return c_id, invitation_code


def resolve_purchase_args(parsed: argparse.Namespace, config_c_id: str) -> tuple[str, str | None, str]:
    if parsed.cid or parsed.product_id or parsed.invitation:
        c_id = parsed.cid or config_c_id
        product_id = parsed.product_id
        invitation_code = parsed.invitation or ""
    else:
        legacy_values = [v for v in (parsed.legacy_arg3, parsed.legacy_arg4, parsed.legacy_arg5) if v is not None]
        if len(legacy_values) == 1:
            raise SystemExit(
                "Error: purchase with a single positional value after <code> is ambiguous. "
                "Use --product-id <id> when cId comes from config, or pass both <cId> <productId>."
            )
        c_id = legacy_values[0] if legacy_values else config_c_id
        product_id = legacy_values[1] if len(legacy_values) > 1 else None
        invitation_code = legacy_values[2] if len(legacy_values) > 2 else ""

    if not c_id:
        raise SystemExit("Error: cId is required. Pass --cid or set login.c_id in config.yaml")
    return c_id, product_id, invitation_code


def main():
    parser = build_parser()
    parsed = parser.parse_args()
    config = load_config()
    use_rpc = resolve_mode(parsed)
    device_host = resolve_device_host(parsed, config)
    base_url = config["base_url"]
    login_path = config["login"]["path"]
    sms_path = config.get("sms", {}).get("path", "/personal-center-service/login/sendSms")
    headers = config.get("headers") or {}
    config_c_id = config.get("login", {}).get("c_id", "")
    cmd = parsed.command

    # ── capture command ───────────────────────────────────────────────────────
    if cmd == "capture":
        if not use_rpc:
            raise SystemExit("Error: capture requires RPC mode")
        from src.frida_client import get_connection, setup_adb_forward
        if device_host == "127.0.0.1":
            try:
                setup_adb_forward()
            except Exception as e:
                print(f"[rpc] adb forward failed: {e}")
        conn = get_connection(device_host)
        result = conn.call({"type": "capture"})
        capture = result.get("capture")
        if not capture:
            print("[capture] No request captured yet — open iBox and trigger any API call first.")
            sys.exit(1)
        print(f"\n{'='*60}")
        print(f"[capture] {capture.get('method')} {capture.get('url')}")
        print(f"\n--- Request Headers ---")
        for k, v in (capture.get("reqHeaders") or {}).items():
            print(f"  {k}: {v}")
        print(f"\n--- Encrypted Request Body (first 200 chars) ---")
        print(f"  {str(capture.get('encBody', ''))[:200]}")
        print(f"\n--- Response {capture.get('respCode')} Headers ---")
        for k, v in (capture.get("respHeaders") or {}).items():
            print(f"  {k}: {v}")
        print(f"\n--- Response Body (first 300 chars) ---")
        print(f"  {str(capture.get('respBody', ''))[:300]}")
        if capture.get("respDecrypted"):
            print(f"\n--- Decrypted Response (first 300 chars) ---")
            print(f"  {str(capture.get('respDecrypted', ''))[:300]}")
        print(f"{'='*60}\n")
        sys.exit(0)

    # ── sms command ───────────────────────────────────────────────────────────
    if cmd == "sms":
        mobile = parsed.mobile
        if use_rpc:
            from src.frida_client import IBoxRPCClient
            client = IBoxRPCClient(base_url=base_url, device_host=device_host, headers=headers)
            result = client.send_sms(mobile, path=sms_path)
        else:
            from src.api_client import IBoxClient
            client = IBoxClient(base_url=base_url, headers=headers)
            result = client.send_sms_code(mobile, path=sms_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)

    mobile = parsed.mobile
    verification_code = parsed.code
    if cmd == "login":
        c_id, invitation_code = resolve_login_args(parsed, config_c_id)
        product_id = None
    else:
        c_id, product_id, invitation_code = resolve_purchase_args(parsed, config_c_id)

    if use_rpc:
        # ── RPC mode: app handles all encryption via rpc_bridge.js ───────────
        from src.frida_client import IBoxRPCClient
        client = IBoxRPCClient(base_url=base_url, device_host=device_host, headers=headers)
        if cmd == "login":
            result = client.login(mobile, verification_code, c_id, invitation_code)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)
        else:
            cart_cfg = config.get("cart") or {}
            order_cfg = config.get("order") or {}
            result = client.login(mobile, verification_code, c_id, invitation_code)
            print("login:", json.dumps(result, ensure_ascii=False)[:200])
            if isinstance(result, dict) and result.get("code") == 0:
                if cart_cfg.get("path") and product_id:
                    r = client.add_cart(cart_cfg["path"], {"productId": product_id, "quantity": 1})
                    print("cart:", json.dumps(r, ensure_ascii=False)[:200])
                if order_cfg.get("path"):
                    r = client.create_order(order_cfg["path"])
                    print("order:", json.dumps(r, ensure_ascii=False)[:200])
            sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)
    else:
        # ── Pure-Python mode ──────────────────────────────────────────────────
        if cmd == "login":
            from src.login_flow import login
            client, result = login(
                base_url=base_url,
                mobile=mobile,
                verification_code=verification_code,
                c_id=c_id,
                invitation_code=invitation_code,
                login_path=login_path,
                headers=headers,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)

        cart_cfg = config.get("cart") or {}
        order_cfg = config.get("order") or {}
        from src.purchase_flow import run_purchase_flow
        client, results = run_purchase_flow(
            base_url=base_url,
            login_path=login_path,
            cart_path=cart_cfg.get("path", ""),
            order_path=order_cfg.get("path", ""),
            mobile=mobile,
            verification_code=verification_code,
            c_id=c_id,
            invitation_code=invitation_code,
            product_id=product_id,
            headers=headers,
        )
        for name, res in results.items():
            print(name, ":", json.dumps(res, ensure_ascii=False)[:300] if res else "None")
        sys.exit(0 if (isinstance(results.get("login"), dict) and results["login"].get("code") == 0) else 1)


if __name__ == "__main__":
    main()
