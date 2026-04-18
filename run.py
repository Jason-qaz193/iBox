#!/usr/bin/env python3
"""
Run iBox login and market operations. Uses config/config.yaml.

Modes:
  (default)          RPC via WiFi or USB, using the app's own crypto
  --rpc              Same as default, kept for explicitness
  --python           Pure-Python fallback (experimental; only login/legacy purchase)
  --usb              RPC via USB + adb forward
  --host <ip>        RPC via phone IP on the same WiFi
"""

import argparse
from datetime import datetime, timedelta
import json
import os
import sys
import threading
import time
import uuid
from typing import TextIO

import yaml

from src.session_store import (
    build_session_payload,
    delete_account_session,
    default_session_path,
    load_account_session,
    save_account_session,
)


def load_config(path: str = None):
    path = path or os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_command_config(config: dict | None, command: str) -> dict:
    return ((config or {}).get("commands") or {}).get(command, {})


def get_command_default(config: dict | None, command: str, key: str, fallback: str) -> str:
    value = get_command_config(config, command).get("defaults", {}).get(key, fallback)
    return str(value)


def render_command_path(config: dict | None, command: str, fallback: str, **values) -> str:
    template = get_command_config(config, command).get("path", fallback)
    try:
        return template.format(**values)
    except KeyError as exc:
        missing = exc.args[0]
        raise SystemExit(f"Error: config.yaml command path for {command} is missing placeholder value: {missing}") from exc


def build_parser(config: dict | None = None) -> argparse.ArgumentParser:
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

    def add_auth_args(p: argparse.ArgumentParser):
        p.add_argument("mobile")
        p.add_argument("code", help="SMS code, or '-' to use the saved session for this mobile")
        p.add_argument("--cid", dest="cid")
        p.add_argument("--invitation", default="")
        p.add_argument("--uid", help="Override uid; defaults to the uid returned by login")

    def add_payload_arg(p: argparse.ArgumentParser, required: bool = True):
        p.add_argument(
            "--payload",
            required=required,
            help="JSON string or @/absolute/path/to/payload.json",
        )

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
    purchase_parser.add_argument("code", help="SMS code, or '-' to use the saved session for this mobile")
    purchase_parser.add_argument("legacy_arg3", nargs="?")
    purchase_parser.add_argument("legacy_arg4", nargs="?")
    purchase_parser.add_argument("legacy_arg5", nargs="?")
    purchase_parser.add_argument("--cid", dest="cid")
    purchase_parser.add_argument("--product-id", dest="product_id")
    purchase_parser.add_argument("--invitation", default="")

    market_info = subparsers.add_parser("market-info", help="Get market purchase info for a collection group")
    add_auth_args(market_info)
    market_info.add_argument("group_id")
    market_info.add_argument(
        "--config-type",
        default=get_command_default(config, "market-info", "config_type", "0"),
    )

    market_list = subparsers.add_parser("market-list", help="List consignment orders for a collection group")
    add_auth_args(market_list)
    market_list.add_argument("group_id")
    market_list.add_argument("--page-no", default=get_command_default(config, "market-list", "page_no", "1"))
    market_list.add_argument("--page-size", default=get_command_default(config, "market-list", "page_size", "20"))
    market_list.add_argument("--sort-type", default=get_command_default(config, "market-list", "sort_type", "1"))
    market_list.add_argument("--sort-field", default=get_command_default(config, "market-list", "sort_field", "1"))

    purchase_orders = subparsers.add_parser("purchase-orders", help="List purchase orders for a collection group")
    add_auth_args(purchase_orders)
    purchase_orders.add_argument("group_id")
    purchase_orders.add_argument("--page-no", default=get_command_default(config, "purchase-orders", "page_no", "1"))
    purchase_orders.add_argument("--page-size", default=get_command_default(config, "purchase-orders", "page_size", "20"))

    synthesis_activity_list = subparsers.add_parser("synthesis-activity-list", help="List synthesis activities")
    add_auth_args(synthesis_activity_list)
    synthesis_activity_list.add_argument(
        "--page-no",
        default=get_command_default(config, "synthesis-activity-list", "page_no", "1"),
    )
    synthesis_activity_list.add_argument(
        "--page-size",
        default=get_command_default(config, "synthesis-activity-list", "page_size", "20"),
    )

    synthesis_activity_detail = subparsers.add_parser("synthesis-activity-detail", help="Get synthesis activity detail")
    add_auth_args(synthesis_activity_detail)
    synthesis_activity_detail.add_argument("activity_id")

    synthesis_center = subparsers.add_parser("synthesis-center", help="Get synthesis center detail")
    add_auth_args(synthesis_center)
    synthesis_center.add_argument("synthetic_id")

    synthesis_work_status = subparsers.add_parser("synthesis-work-status", help="Get synthesis assistant work status")
    add_auth_args(synthesis_work_status)
    synthesis_work_status.add_argument("synthetic_id")

    synthesis_submit = subparsers.add_parser("synthesis-submit", help="Submit a synthesis request")
    add_auth_args(synthesis_submit)
    add_payload_arg(synthesis_submit)

    synthesis_auto = subparsers.add_parser(
        "synthesis-auto",
        help="Scan all synthesis recipes and auto-submit every currently craftable one",
    )
    add_auth_args(synthesis_auto)
    synthesis_auto.add_argument(
        "--max-rounds",
        type=int,
        default=20,
        help="Maximum scan rounds; each round re-checks all recipes after successful synthesis",
    )
    synthesis_auto.add_argument(
        "--target-count",
        "--expected-count",
        dest="target_count",
        type=positive_int,
        help="Expected total syntheticNum to submit in this run; defaults to all currently craftable items",
    )
    synthesis_auto.add_argument(
        "--submit-window",
        type=int,
        default=60,
        help="Keep retrying failed synthesis submits within this many seconds",
    )
    synthesis_auto.add_argument(
        "--retry-interval",
        type=float,
        default=0.3,
        help="Seconds to wait between retry attempts inside the submit window",
    )
    synthesis_auto.add_argument(
        "--submit-concurrency",
        type=int,
        default=1,
        help="How many parallel submit workers to run for the same synthesis item",
    )
    synthesis_auto.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the discovered synthesis plans without submitting",
    )
    synthesis_auto.add_argument(
        "--captcha-mode",
        choices=["auto", "manual", "skip"],
        default="auto",
        help=(
            "How to handle the GeeTest V4 captcha that submit triggers: "
            "auto=try Playwright first then fall back to manual, "
            "manual=wait for user to solve in the app (LSPosed hook captures result), "
            "skip=do NOT call confirm (legacy broken behaviour, synthesis stays pending)"
        ),
    )
    synthesis_auto.add_argument(
        "--captcha-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for captcha result (manual mode) or Playwright solve timeout",
    )
    synthesis_auto.add_argument(
        "--captcha-id",
        default="0d4b08eac1cbdcad36bbf607c5bf3e1b",
        help="GeeTest captcha_id for iBox (default matches production app)",
    )
    synthesis_auto.add_argument(
        "--captcha-headed",
        action="store_true",
        help="Show the browser window when using Playwright captcha auto-solve",
    )
    synthesis_auto.add_argument(
        "--detail-interval",
        type=float,
        default=0.3,
        help="Seconds to wait between each activity-detail request to avoid 429 (default: 0.3)",
    )
    synthesis_auto.add_argument(
        "--pre-center-offset",
        type=float,
        default=3.0,
        help="Seconds before activity start to pre-call synthesis-center and cache the payload (default: 3)",
    )
    synthesis_auto.add_argument(
        "--pre-start-window",
        type=float,
        default=60.0,
        help="Start polling this many seconds before the activity opens (default: 60)",
    )

    synthesis_confirm = subparsers.add_parser("synthesis-confirm", help="Confirm synthesis with captcha params")
    add_auth_args(synthesis_confirm)
    synthesis_confirm.add_argument("--confirm-uid", required=True, help="uid used in the confirm query string")
    synthesis_confirm.add_argument("--captcha-id", required=True)
    synthesis_confirm.add_argument("--lot-number", required=True)
    synthesis_confirm.add_argument("--pass-token", required=True)
    synthesis_confirm.add_argument("--gen-time", required=True)
    synthesis_confirm.add_argument("--captcha-output", required=True)
    add_payload_arg(synthesis_confirm, required=False)

    market_buy = subparsers.add_parser("market-buy", help="Create batch purchase-consignment order")
    add_auth_args(market_buy)
    add_payload_arg(market_buy)

    consign_create = subparsers.add_parser("consign-create", help="Create a consignment order")
    add_auth_args(consign_create)
    add_payload_arg(consign_create)

    consign_cancel = subparsers.add_parser("consign-cancel", help="Cancel a consignment order")
    add_auth_args(consign_cancel)
    consign_cancel.add_argument("consign_order_id")

    purchase_detail = subparsers.add_parser("purchase-detail", help="Get purchase-consignment order detail")
    add_auth_args(purchase_detail)
    purchase_detail.add_argument("order_uuid")

    wanted_detail = subparsers.add_parser("wanted-detail", help="Get public wanted/purchase order detail")
    add_auth_args(wanted_detail)
    wanted_detail.add_argument("purchase_order_id")

    wanted_deal = subparsers.add_parser(
        "wanted-deal",
        help=(
            "Deal a wanted/purchase order relation. "
            "Pass purchase_order_id+relation_id directly, or use --collection-name to look them up automatically."
        ),
    )
    add_auth_args(wanted_deal)
    wanted_deal.add_argument("purchase_order_id", nargs="?", default=None, help="Purchase order ID (optional when --collection-name is used)")
    wanted_deal.add_argument("relation_id", nargs="?", default=None, help="Relation ID (optional when --collection-name is used)")
    wanted_deal.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称 — auto-lookup purchase_order_id and relation_id by name")
    wanted_deal.add_argument("--group-id", dest="group_id", default="", help="藏品分组ID — query purchase orders directly, bypassing name lookup")
    wanted_deal.add_argument("--quantity", dest="quantity", type=int, default=1, help="出售数量 (default: 1)")
    wanted_deal.add_argument("--min-price", dest="min_price", type=float, default=0.0, help="最低出售价 — only match purchase orders at or above this price")
    wanted_deal.add_argument("--consignment-password", dest="consignment_password", default="", help="寄售密码 — included in the deal request body")
    wanted_deal.add_argument("--collection-id", dest="collection_id", default="", help="要卖出的具体藏品ID — auto-selects an unlocked item when omitted")
    wanted_deal.add_argument("--payment-platform", dest="payment_platform", type=int, default=30, help="支付钱包平台代码 (default: 30)")
    wanted_deal.add_argument("--po-page-size", dest="po_page_size", type=int, default=20, help="Page size when listing purchase orders for name lookup (default: 20)")
    wanted_deal.add_argument("--market-search-pages", dest="market_search_pages", type=int, default=10, help="How many public market pages to scan by collection name (default: 10)")
    wanted_deal.add_argument("--market-segment-id", dest="market_segment_id", default="-1", help="Public market segmentId to search (default: -1)")
    wanted_deal.add_argument("--dry-run", action="store_true", help="Print matched orders without executing the deal (only in name-lookup mode)")
    add_payload_arg(wanted_deal, required=False)

    wanted_buy = subparsers.add_parser(
        "wanted-buy",
        help="Place a buy/wanted order (求购单). Use --group-id+--price, or --collection-name+--price to look up group-id automatically.",
    )
    add_auth_args(wanted_buy)
    wanted_buy.add_argument("--group-id", dest="group_id", default="", help="藏品分组ID (skip if using --collection-name)")
    wanted_buy.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称 — auto-lookup group_id by name")
    wanted_buy.add_argument("--price", dest="price", type=float, required=True, help="出价（元）")
    wanted_buy.add_argument("--quantity", dest="quantity", type=int, default=1, help="求购数量 (default: 1)")
    wanted_buy.add_argument("--payment-platform", dest="payment_platform", type=int, default=25, help="支付平台代码 (default: 25)")
    wanted_buy.add_argument("--consignment-password", dest="consignment_password", default="", help="寄售密码")
    wanted_buy.add_argument("--dry-run", action="store_true", help="Print resolved group_id without placing order")
    add_payload_arg(wanted_buy, required=False)

    api_parser = subparsers.add_parser("api", help="Call an arbitrary authenticated iBox API path in RPC mode")
    add_auth_args(api_parser)
    api_parser.add_argument("method", choices=["GET", "POST"])
    api_parser.add_argument("path", help="Absolute API path starting with /")
    add_payload_arg(api_parser, required=False)

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


def parse_payload_arg(raw: str | None) -> dict | None:
    if not raw:
        return None
    if raw.startswith("@"):
        path = raw[1:]
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(raw)


def print_result(result: dict):
    print(json.dumps(result, ensure_ascii=False, indent=2))


class TeeStream:
    def __init__(self, *streams: TextIO):
        self._streams = streams

    def write(self, data: str):
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()

    def isatty(self):
        return any(getattr(stream, "isatty", lambda: False)() for stream in self._streams)


def setup_log_file(project_root: str) -> str:
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(logs_dir, f"run-{timestamp}.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(sys.__stdout__, log_file)
    sys.stderr = TeeStream(sys.__stderr__, log_file)
    return log_path


def iter_nested_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nested_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_nested_dicts(item)


def first_present(mapping: dict | None, keys: tuple[str, ...]):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_list_payload(result: dict | None) -> list:
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "records", "items", "data", "rows", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = extract_list_payload({"data": value})
                if nested:
                    return nested
    return []


def to_int(value) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def to_price_fen(value) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip()
    if not text:
        return None
    try:
        if "." in text:
            return int(round(float(text) * 100))
        return int(text)
    except (TypeError, ValueError):
        return None


def format_price_yuan(price_fen: int | None) -> str:
    if price_fen is None:
        return "unknown"
    return f"{price_fen / 100:.2f}"


def build_webview_api_headers(
    token: str | None,
    *,
    origin: str = "https://detail-page.ibox.art",
    app_version: str = "2.3.2",
) -> dict:
    origin = origin.rstrip("/")
    cookies = [
        f"version={app_version}",
        "deviceId=",
        "stage=",
    ]
    if token:
        cookies.insert(0, f"token={token}")
    headers = {
        "Content-Type": None,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; M2006J10C Build/SP1A.210812.016; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/89.0.4389.72 "
            "MQQBrowser/6.2 TBS/046295 Mobile Safari/537.36 ibox_app ;kyc/h5face;kyc/2.0 "
            f"iBoxWebView iboxVersion={app_version};"
        ),
        "msg-id": uuid.uuid4().hex,
        "platform-type": "1",
        "app-version": app_version,
        "device-id": "",
        "allowouttest": "1",
        "Origin": origin,
        "Referer": f"{origin}/",
        "X-Requested-With": "com.box.art",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": "; ".join(cookies),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def normalize_material_item(item: dict) -> dict | None:
    material_id = first_present(
        item,
        (
            "materialId",
            "sourceMaterialId",
            "collectionId",
            "digitalCollectionId",
            "targetId",
            "id",
        ),
    )
    required_count = first_present(
        item,
        (
            "needNum",
            "needCount",
            "consumeNum",
            "consumeCount",
            "num",
            "count",
            "quantity",
        ),
    )
    owned_count = first_present(
        item,
        (
            "ownNum",
            "ownedNum",
            "ownedCount",
            "currentNum",
            "currentCount",
            "holdNum",
            "holdCount",
            "inventoryNum",
            "inventoryCount",
            "numOwned",
            "countOwned",
            "surplusNum",
            "remainNum",
            "leftNum",
            "usableNum",
            "availableNum",
            "availableCount",
        ),
    )
    required_count = to_int(required_count)
    owned_count = to_int(owned_count)
    if material_id in (None, "") or not required_count or required_count <= 0:
        return None
    if owned_count is None:
        owned_count = 0
    return {
        "material_id": str(material_id),
        "required_count": required_count,
        "owned_count": owned_count,
        "raw": item,
    }


def extract_burn_album_recipe(center_result: dict) -> dict | None:
    data = (center_result or {}).get("data") or {}
    burn_albums = data.get("burnAlbums")
    if not isinstance(burn_albums, list) or not burn_albums:
        return None

    normalized_items = []
    material_groups = []
    for burn_group in burn_albums:
        if not isinstance(burn_group, dict):
            continue
        required_count = to_int(first_present(burn_group, ("quantity", "count", "needNum", "consumeNum"))) or 0
        albums = burn_group.get("albums")
        if not isinstance(albums, list) or required_count <= 0:
            continue
        group_items = []
        for album in albums:
            if not isinstance(album, dict):
                continue
            material_id = first_present(
                album,
                (
                    "materialId",
                    "digitalCollectionId",
                    "collectionId",
                    "id",
                ),
            )
            owned_count = to_int(
                first_present(
                    album,
                    (
                        "usableNum",
                        "ownedNum",
                        "holdNum",
                        "inventoryNum",
                        "availableNum",
                    ),
                )
            )
            if material_id in (None, ""):
                continue
            group_items.append(
                {
                    "material_id": str(material_id),
                    "required_count": required_count,
                    "owned_count": owned_count or 0,
                    "raw": {
                        "burn_group": burn_group,
                        "album": album,
                    },
                }
            )

        if not group_items:
            continue

        # burnAlbums behaves like "pick one material from each group", not "consume every album".
        selected_item = max(
            group_items,
            key=lambda item: (
                item["owned_count"] // item["required_count"],
                item["owned_count"],
            ),
        )
        normalized_items.append(selected_item)
        material_groups.append(
            {
                "required_count": required_count,
                "options": group_items,
                "selected_material_id": selected_item["material_id"],
            }
        )

    if not normalized_items:
        return None

    max_times = min(item["owned_count"] // item["required_count"] for item in normalized_items)
    return {
        "synthetic_id": str(data.get("id")) if data.get("id") not in (None, "") else None,
        "activity_id": first_present(data, ("activityId", "activityID")),
        "synthetic_count": 1,
        "materials": normalized_items,
        "material_groups": material_groups,
        "max_times": max_times,
        "raw_recipe": {"burnAlbums": burn_albums},
    }


def extract_recipe_candidates(center_result: dict) -> list[dict]:
    data = (center_result or {}).get("data") or {}
    candidates = []
    burn_album_candidate = extract_burn_album_recipe(center_result)
    if burn_album_candidate:
        candidates.append(burn_album_candidate)
    list_keys = (
        "materials",
        "materialList",
        "consumeMaterials",
        "consumeMaterialList",
        "sourceMaterials",
        "sourceMaterialList",
        "needMaterials",
        "needMaterialList",
        "elements",
        "componentList",
        "children",
    )
    for node in iter_nested_dicts(data):
        materials = None
        for key in list_keys:
            value = node.get(key)
            if isinstance(value, list) and value:
                materials = value
                break
        if not materials:
            continue
        normalized_items = []
        for item in materials:
            if isinstance(item, dict):
                normalized = normalize_material_item(item)
                if normalized:
                    normalized_items.append(normalized)
        if not normalized_items:
            continue
        synthetic_id = first_present(node, ("syntheticId", "syntheticID", "id"))
        activity_id = first_present(node, ("activityId", "activityID", "activeId"))
        synthetic_count = first_present(
            node,
            ("syntheticNum", "count", "num", "quantity", "targetCount"),
        )
        synthetic_count = to_int(synthetic_count) or 1
        max_times = min(item["owned_count"] // item["required_count"] for item in normalized_items)
        candidates.append(
            {
                "synthetic_id": str(synthetic_id) if synthetic_id not in (None, "") else None,
                "activity_id": str(activity_id) if activity_id not in (None, "") else None,
                "synthetic_count": synthetic_count,
                "materials": normalized_items,
                "max_times": max_times,
                "raw_recipe": node,
            }
        )
    deduped = []
    seen = set()
    for candidate in candidates:
        key = (
            candidate["synthetic_id"],
            candidate["activity_id"],
            tuple(
                (
                    item["material_id"],
                    item["required_count"],
                    item["owned_count"],
                )
                for item in candidate["materials"]
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def choose_recipe_candidate(candidates: list[dict], synthetic_id: str) -> dict:
    for candidate in candidates:
        if candidate.get("synthetic_id") == str(synthetic_id):
            return candidate
    if len(candidates) == 1:
        return candidates[0]
    craftable = [candidate for candidate in candidates if candidate.get("max_times", 0) > 0]
    if len(craftable) == 1:
        return craftable[0]
    raise SystemExit(
        "Error: could not uniquely identify the synthesis recipe from synthesis-center response. "
        "Use synthesis-center first to inspect the response shape."
    )


def build_synthesis_submit_payload(candidate: dict, synthetic_id: str, times: int) -> dict:
    materials_payload = []
    for item in candidate["materials"]:
        consumed = item["required_count"] * times
        materials_payload.append(
            {
                "materialId": item["material_id"],
                "count": consumed,
                "num": consumed,
                "quantity": consumed,
            }
        )

    payload = {
        "syntheticId": str(synthetic_id),
        "id": str(synthetic_id),
        "syntheticNum": times,
        "count": times,
        "num": times,
        "quantity": times,
        "materials": materials_payload,
        "materialList": materials_payload,
    }
    if candidate.get("activity_id"):
        payload["activityId"] = candidate["activity_id"]
    return payload


def summarize_synthesis_plan(candidate: dict, times: int) -> dict:
    return {
        "synthetic_id": candidate.get("synthetic_id"),
        "activity_id": candidate.get("activity_id"),
        "requested_times": times,
        "max_times": candidate.get("max_times", 0),
        "synthetic_count_per_time": candidate.get("synthetic_count", 1),
        "materials": [
            {
                "material_id": item["material_id"],
                "owned_count": item["owned_count"],
                "required_count_per_time": item["required_count"],
                "consumed_count": item["required_count"] * times,
            }
            for item in candidate["materials"]
        ],
    }


def build_material_state_signature(candidate: dict) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                item["material_id"],
                item["required_count"],
                item["owned_count"],
            )
            for item in candidate.get("materials", [])
        )
    )


def extract_activity_ids(activity_list_result: dict) -> list[str]:
    ids = []
    data = (activity_list_result or {}).get("data") or {}
    for node in iter_nested_dicts(data):
        activity_id = first_present(node, ("activityId", "activityID", "id"))
        synthetic_count = first_present(node, ("syntheticNum", "syntheticCount", "syntheticsCount"))
        title = first_present(node, ("title", "name", "activityName"))
        if activity_id in (None, ""):
            continue
        if synthetic_count is not None or title is not None:
            ids.append(str(activity_id))
    deduped = []
    seen = set()
    for activity_id in ids:
        if activity_id in seen:
            continue
        seen.add(activity_id)
        deduped.append(activity_id)
    return deduped


def parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def is_channel_active(channel: dict, now: datetime | None = None) -> bool:
    if not isinstance(channel, dict):
        return False
    now = now or datetime.now()
    start_time = parse_datetime_value(first_present(channel, ("startTime", "start_at", "beginTime")))
    end_time = parse_datetime_value(first_present(channel, ("endTime", "end_at", "finishTime")))
    if start_time and now < start_time:
        return False
    if end_time and now > end_time:
        return False
    return True


def extract_synthetic_ids(value) -> list[str]:
    ids = []
    now = datetime.now()
    for node in iter_nested_dicts(value):
        if "syntheticActivityId" in node:
            synthetic_activity_id = first_present(node, ("syntheticActivityId", "synthetic_activity_id"))
            if synthetic_activity_id not in (None, "") and is_channel_active(node, now):
                ids.append(str(synthetic_activity_id))
            continue
        synthetic_id = first_present(node, ("syntheticId", "syntheticID"))
        if synthetic_id not in (None, ""):
            ids.append(str(synthetic_id))
            continue
        synthetic = node.get("synthetic")
        if isinstance(synthetic, dict):
            nested_id = first_present(synthetic, ("id", "syntheticId", "syntheticID"))
            if nested_id not in (None, ""):
                ids.append(str(nested_id))
    deduped = []
    seen = set()
    for synthetic_id in ids:
        if synthetic_id in seen:
            continue
        seen.add(synthetic_id)
        deduped.append(synthetic_id)
    return deduped


def find_earliest_upcoming_start_time(activity_details: list[dict]) -> datetime | None:
    """Return the earliest future startTime found across all activity detail response nodes."""
    now = datetime.now()
    earliest: datetime | None = None
    for item in activity_details:
        detail = item.get("detail") if isinstance(item, dict) else item
        for node in iter_nested_dicts(detail or {}):
            start_time = parse_datetime_value(
                first_present(node, ("startTime", "start_at", "beginTime"))
            )
            if start_time and start_time > now:
                if earliest is None or start_time < earliest:
                    earliest = start_time
    return earliest


def extract_upcoming_synthetic_ids_with_start(
    activity_details: list[dict],
) -> tuple[list[str], dict[str, str], datetime | None]:
    """Extract syntheticActivityId values from channels that have not yet started
    (but have not ended either), bypassing the is_channel_active start-time filter.

    Returns (ids, synthetic_id_to_activity_id, earliest_start_time).
    Used to pre-populate IDs during the pre-start wait so no re-fetch is needed.
    """
    now = datetime.now()
    ids: list[str] = []
    id_to_activity: dict[str, str] = {}
    earliest_start: datetime | None = None
    for item in activity_details:
        if not isinstance(item, dict):
            continue
        activity_id = str(item.get("activity_id", ""))
        detail = item.get("detail")
        if not is_success(detail):
            continue
        for node in iter_nested_dicts(detail):
            if "syntheticActivityId" not in node:
                continue
            sid = first_present(node, ("syntheticActivityId", "synthetic_activity_id"))
            if sid in (None, ""):
                continue
            end_time = parse_datetime_value(first_present(node, ("endTime", "end_at", "finishTime")))
            if end_time and now > end_time:
                continue  # already ended, skip
            start_time = parse_datetime_value(first_present(node, ("startTime", "start_at", "beginTime")))
            ids.append(str(sid))
            if activity_id:
                id_to_activity.setdefault(str(sid), activity_id)
            if start_time and start_time > now:
                if earliest_start is None or start_time < earliest_start:
                    earliest_start = start_time
    deduped: list[str] = []
    seen: set[str] = set()
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            deduped.append(sid)
    return deduped, id_to_activity, earliest_start


def _wait_until_start(target: datetime):
    """Sleep with periodic countdown prints until target datetime is reached.

    Accuracy tiers:
      > 60s  : sleep 30s between checks
      10–60s : sleep 5s between checks, print every tick
      1–10s  : sleep 0.5s between checks, print every tick
      < 1s   : tight 20ms spin loop, no print (minimises OS scheduling jitter)
    """
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        if remaining < 1.0:
            # Tight spin: 20 ms granularity → ≤20 ms overshoot
            time.sleep(0.02)
        elif remaining <= 10:
            print(f"[wait] {remaining:.1f}s remaining…", flush=True)
            time.sleep(0.5)
        elif remaining <= 60:
            print(f"[wait] {remaining:.0f}s remaining…", flush=True)
            time.sleep(5.0)
        else:
            print(f"[wait] {remaining:.0f}s remaining…", flush=True)
            time.sleep(min(remaining - 5, 30.0))


def build_parallel_submit_client(client):
    cloned_client = client.__class__(
        base_url=client.base_url,
        device_host=client.device_host,
        headers=dict(client._http.headers),
    )
    if getattr(client, "token", None):
        cloned_client.set_token(client.token)
    return cloned_client


def submit_synthesis_with_retry(*, client, submit_path: str, payload: dict, submit_window: int, retry_interval: float, concurrency: int) -> dict:
    attempts = []
    started_at = time.monotonic()
    deadline = started_at + max(submit_window, 0)
    success_event = threading.Event()
    stop_event = threading.Event()
    attempts_lock = threading.Lock()
    result_lock = threading.Lock()
    result = {"submit": None}
    attempt_counter = {"value": 0}

    def worker():
        worker_client = build_parallel_submit_client(client)
        while not success_event.is_set() and not stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            with attempts_lock:
                attempt_counter["value"] += 1
                attempt_no = attempt_counter["value"]
            submit_result = worker_client.submit_synthesis(submit_path, payload)
            with attempts_lock:
                attempts.append(
                    {
                        "attempt": attempt_no,
                        "submit": submit_result,
                    }
                )
            if is_success(submit_result):
                with result_lock:
                    if result["submit"] is None:
                        result["submit"] = submit_result
                success_event.set()
                break
            if not should_retry_synthesis_submit(submit_result):
                stop_event.set()
                with result_lock:
                    if result["submit"] is None:
                        result["submit"] = submit_result
                break
            if retry_interval > 0:
                sleep_for = min(retry_interval, max(deadline - time.monotonic(), 0))
                if sleep_for > 0:
                    time.sleep(sleep_for)

    threads = []
    for index in range(max(concurrency, 1)):
        thread = threading.Thread(target=worker, name=f"synthesis-submit-{index + 1}", daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        remaining = max(deadline - time.monotonic(), 0)
        thread.join(timeout=remaining + 1)

    attempts.sort(key=lambda item: item["attempt"])
    successful_result = result["submit"]
    if successful_result is not None:
        return {
            "result": successful_result,
            "attempts": attempts,
            "attempt_count": len(attempts),
            "retried": len(attempts) > 1,
            "window_seconds": max(submit_window, 0),
            "concurrency": max(concurrency, 1),
        }
    return {
        "result": attempts[-1]["submit"] if attempts else {},
        "attempts": attempts,
        "attempt_count": len(attempts),
        "retried": len(attempts) > 1,
        "window_seconds": max(submit_window, 0),
        "concurrency": max(concurrency, 1),
    }


def is_success(result: dict) -> bool:
    return isinstance(result, dict) and result.get("code") == 0


def should_retry_synthesis_submit(result: dict | None) -> bool:
    if result is None:
        return True
    if not isinstance(result, dict):
        return True

    code = result.get("code")
    if code == 0:
        return False

    if code is not None:
        return str(code) in {"429", "500", "502", "503", "504"}

    raw = str(result.get("_raw", ""))
    lowered = raw.lower()
    if "too many requests" in lowered or "429" in lowered:
        return True
    if lowered.strip().startswith("<!doctype html") or lowered.strip().startswith("<html"):
        return False
    return True


def is_auth_failure(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False

    code = result.get("code")
    if str(code) in {"401", "403", "1001", "1002", "2001", "2002", "2003"}:
        return True

    message_parts = [
        result.get("message"),
        result.get("msg"),
        result.get("error"),
    ]
    message = " ".join(str(part) for part in message_parts if part not in (None, "")).lower()
    auth_keywords = (
        "token",
        "login",
        "auth",
        "authorization",
        "expired",
        "invalid",
        "unauthorized",
        "forbidden",
        "未登录",
        "登录失效",
        "重新登录",
        "token失效",
        "token过期",
        "鉴权",
        "认证",
        "过期",
        "失效",
    )
    return any(keyword in message for keyword in auth_keywords)


def uid_from_jwt(token: str) -> str | None:
    """Decode the JWT payload (no signature verification) and extract userId."""
    try:
        import base64 as _b64
        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(_b64.urlsafe_b64decode(padded))
        for key in ("userId", "uid"):
            val = payload.get(key)
            if val not in (None, "") and str(val).lstrip("-").isdigit():
                return str(val)
    except Exception:
        pass
    return None


def extract_uid(login_result: dict) -> str | None:
    data = (login_result or {}).get("data") or {}
    for key in ("uid", "userId", "id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    # Fallback: decode from the JWT token if present
    token = data.get("token")
    if token:
        return uid_from_jwt(str(token))
    return None


def extract_token(login_result: dict) -> str | None:
    data = (login_result or {}).get("data") or {}
    token = data.get("token")
    if token not in (None, ""):
        return str(token)
    return None


def normalize_code(code: str | None) -> str | None:
    if code in (None, "-", ""):
        return None
    return code


def save_login_session(session_path: str, mobile: str, login_result: dict, use_rpc: bool, device_host: str):
    token = extract_token(login_result)
    if not token:
        return
    uid = extract_uid(login_result)
    session_data = build_session_payload(
        mobile=mobile,
        token=token,
        uid=uid,
        extra={
            "mode": "rpc" if use_rpc else "python",
            "device_host": device_host if use_rpc else "",
        },
    )
    save_account_session(session_path, mobile, session_data)


def restore_saved_session(client, session_path: str, mobile: str) -> dict | None:
    session_data = load_account_session(session_path, mobile)
    if not session_data:
        return None
    token = session_data.get("token")
    if not token:
        return None
    client.set_token(str(token))
    return session_data


def login_and_save_session(
    *,
    client,
    session_path: str,
    mobile: str,
    code: str,
    c_id: str,
    invitation: str,
    use_rpc: bool,
    device_host: str,
):
    login_result = client.login(mobile, code, c_id, invitation)
    if is_success(login_result):
        save_login_session(session_path, mobile, login_result, use_rpc, device_host)
    return login_result


def ensure_authenticated_client(
    *,
    client,
    session_path: str,
    mobile: str,
    code: str | None,
    c_id: str,
    invitation: str,
    use_rpc: bool,
    device_host: str,
):
    session_data = restore_saved_session(client, session_path, mobile)
    if session_data:
        return {
            "code": 0,
            "message": "using saved session",
            "data": {
                "token": session_data.get("token", ""),
                "uid": session_data.get("uid", ""),
                "mobile": session_data.get("mobile", mobile),
            },
        }, session_data, True

    if code:
        login_result = login_and_save_session(
            client=client,
            session_path=session_path,
            mobile=mobile,
            code=code,
            c_id=c_id,
            invitation=invitation,
            use_rpc=use_rpc,
            device_host=device_host,
        )
        return login_result, None, False

    raise SystemExit(
        f"Error: no saved session found for mobile {mobile}. "
        "Pass the SMS code once to log in and create its session."
    )


def call_with_session_retry(
    *,
    operation,
    client,
    session_path: str,
    mobile: str,
    code: str | None,
    c_id: str,
    invitation: str,
    use_rpc: bool,
    device_host: str,
    used_saved_session: bool,
):
    result = operation()
    if not (used_saved_session and is_auth_failure(result)):
        return result, False, None

    delete_account_session(session_path, mobile)

    if not code:
        raise SystemExit(
            f"Error: saved session for mobile {mobile} has expired. "
            "It was removed from config/session.json. Pass an SMS code to log in again."
        )

    login_result = login_and_save_session(
        client=client,
        session_path=session_path,
        mobile=mobile,
        code=code,
        c_id=c_id,
        invitation=invitation,
        use_rpc=use_rpc,
        device_host=device_host,
    )
    if not is_success(login_result):
        return result, True, login_result

    return operation(), True, login_result


def require_rpc(cmd: str, use_rpc: bool):
    if not use_rpc:
        raise SystemExit(f"Error: {cmd} currently requires RPC mode")


def main():
    config = load_config()
    project_root = os.path.dirname(__file__)
    log_path = setup_log_file(project_root)
    print(f"[log] saving output to {log_path}")
    session_path = default_session_path(project_root)
    parser = build_parser(config)
    parsed = parser.parse_args()
    use_rpc = resolve_mode(parsed)
    device_host = resolve_device_host(parsed, config)
    base_url = config["base_url"]
    login_path = config["login"]["path"]
    sms_path = config.get("sms", {}).get("path", "/personal-center-service/login/sendSms")
    headers = config.get("headers") or {}
    app_version = str(headers.get("app-version") or headers.get("App-Version") or "2.3.2")
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

    rpc_only_commands = {
        "market-info",
        "market-list",
        "purchase-orders",
        "synthesis-activity-list",
        "synthesis-activity-detail",
        "synthesis-center",
        "synthesis-work-status",
        "synthesis-submit",
        "synthesis-auto",
        "synthesis-confirm",
        "market-buy",
        "consign-create",
        "consign-cancel",
        "purchase-detail",
        "wanted-detail",
        "wanted-deal",
        "wanted-buy",
        "api",
    }
    if cmd in rpc_only_commands:
        require_rpc(cmd, use_rpc)
        from src.frida_client import IBoxRPCClient

        c_id = parsed.cid or config_c_id
        if not c_id:
            raise SystemExit("Error: cId is required. Pass --cid or set login.c_id in config.yaml")

        client = IBoxRPCClient(base_url=base_url, device_host=device_host, headers=headers)
        normalized_code = normalize_code(parsed.code)
        login_result, saved_session, used_saved_session = ensure_authenticated_client(
            client=client,
            session_path=session_path,
            mobile=parsed.mobile,
            code=normalized_code,
            c_id=c_id,
            invitation=parsed.invitation or "",
            use_rpc=use_rpc,
            device_host=device_host,
        )
        if not is_success(login_result):
            print_result({"login": login_result})
            sys.exit(1)

        uid = parsed.uid or extract_uid(login_result) or ((saved_session or {}).get("uid") if saved_session else None)
        # Last resort: decode uid from the JWT token stored in session
        if not uid:
            _jwt_token = ((saved_session or {}).get("token") or extract_token(login_result))
            if _jwt_token:
                uid = uid_from_jwt(str(_jwt_token))
        operation = None

        if cmd == "market-info":
            path = render_command_path(
                config,
                "market-info",
                (
                    "/public-market-service/digital-collection-groups/{group_id}"
                    "/purchase-consignment-info?configType={config_type}"
                ),
                group_id=parsed.group_id,
                config_type=parsed.config_type,
            )
            operation = lambda: client.get(path)
        elif cmd == "market-list":
            if not uid:
                raise SystemExit("Error: uid is required for market-list. Pass --uid or ensure login response contains uid")
            path = render_command_path(
                config,
                "market-list",
                (
                    "/public-market-service/digital-collection-groups/{group_id}"
                    "/consignment-orders?pageNo={page_no}&pageSize={page_size}"
                    "&sortType={sort_type}&sortField={sort_field}&uid={uid}"
                ),
                group_id=parsed.group_id,
                page_no=parsed.page_no,
                page_size=parsed.page_size,
                sort_type=parsed.sort_type,
                sort_field=parsed.sort_field,
                uid=uid,
            )
            operation = lambda: client.get(path)
        elif cmd == "purchase-orders":
            if not uid:
                raise SystemExit("Error: uid is required for purchase-orders. Pass --uid or ensure login response contains uid")
            path = render_command_path(
                config,
                "purchase-orders",
                (
                    "/public-market-service/digital-collection-groups/{group_id}"
                    "/purchase-orders?pageNo={page_no}&pageSize={page_size}&uid={uid}"
                ),
                group_id=parsed.group_id,
                page_no=parsed.page_no,
                page_size=parsed.page_size,
                uid=uid,
            )
            operation = lambda: client.get(
                path,
                headers=build_webview_api_headers(
                    getattr(client, "token", None) or ((saved_session or {}).get("token") if saved_session else None),
                    origin="https://detail-page.ibox.art",
                    app_version=app_version,
                ),
            )
        elif cmd == "synthesis-activity-list":
            path = render_command_path(
                config,
                "synthesis-activity-list",
                "/synthesis-service/synthetic/activity/list?pageNo={page_no}&pageSize={page_size}",
                page_no=parsed.page_no,
                page_size=parsed.page_size,
            )
            operation = lambda: client.get_synthesis_activity_list(path)
        elif cmd == "synthesis-activity-detail":
            path = render_command_path(
                config,
                "synthesis-activity-detail",
                "/synthesis-service/synthetic/activity/detail?id={activity_id}",
                activity_id=parsed.activity_id,
            )
            operation = lambda: client.get_synthesis_activity_detail(path)
        elif cmd == "synthesis-center":
            path = render_command_path(
                config,
                "synthesis-center",
                "/synthesis-service/synthetic/center/{synthetic_id}",
                synthetic_id=parsed.synthetic_id,
            )
            operation = lambda: client.get_synthesis_center(path)
        elif cmd == "synthesis-work-status":
            path = render_command_path(
                config,
                "synthesis-work-status",
                "/synthesis-service/assistants/work-status/synthetics/{synthetic_id}",
                synthetic_id=parsed.synthetic_id,
            )
            operation = lambda: client.get_synthesis_work_status(path)
        elif cmd == "synthesis-submit":
            payload = parse_payload_arg(parsed.payload)
            path = render_command_path(
                config,
                "synthesis-submit",
                "/synthesis-service/synthetic/center/submit",
            )
            operation = lambda: client.submit_synthesis(path, payload)
        elif cmd == "synthesis-auto":
            activity_list_path = render_command_path(
                config,
                "synthesis-activity-list",
                "/synthesis-service/synthetic/activity/list?pageNo={page_no}&pageSize={page_size}",
                page_no=get_command_default(config, "synthesis-activity-list", "page_no", "1"),
                page_size=get_command_default(config, "synthesis-activity-list", "page_size", "100"),
            )
            submit_path = render_command_path(config, "synthesis-submit", "/synthesis-service/synthetic/center/submit")
            confirm_path_template = render_command_path(
                config,
                "synthesis-confirm",
                (
                    "/synthesis-service/synthetic/center/confirm"
                    "?uid={uid}&captcha_id={captcha_id}"
                    "&lot_number={lot_number}&pass_token={pass_token}"
                    "&gen_time={gen_time}&captcha_output={captcha_output}"
                ),
                uid="{uid}",
                captcha_id="{captcha_id}",
                lot_number="{lot_number}",
                pass_token="{pass_token}",
                gen_time="{gen_time}",
                captcha_output="{captcha_output}",
            )

            def _get_captcha_params(synth_id: str) -> tuple[dict | None, str | None]:
                """
                Obtain GeeTest V4 captcha params for the given synthetic_id.

                Returns (params_dict, error_message).  params_dict keys:
                    lot_number, pass_token, gen_time, captcha_output, captcha_id
                """
                cap_mode    = parsed.captcha_mode
                cap_timeout = parsed.captcha_timeout
                cap_id      = parsed.captcha_id

                # ── auto / playwright ────────────────────────────────────────
                if cap_mode in ("auto", "playwright"):
                    try:
                        from src.geetest_solver import playwright_solve, check_dependencies
                        ok, msg = check_dependencies()
                        if not ok:
                            raise ImportError(msg)
                        print(f"[captcha] Auto-solving GeeTest V4 (synthetic_id={synth_id})…")
                        result = playwright_solve(
                            captcha_id=cap_id,
                            timeout=cap_timeout,
                            headed=getattr(parsed, "captcha_headed", False),
                        )
                        if result and result.get("lot_number"):
                            print(f"[captcha] Auto-solved ✓  lot_number={result['lot_number'][:8]}…")
                            return result, None
                        raise RuntimeError(f"Playwright returned empty result: {result}")
                    except Exception as exc:
                        print(f"[captcha] Auto-solve failed: {exc}")
                        if cap_mode == "playwright":
                            return None, f"Playwright captcha solve failed: {exc}"
                        print("[captcha] Falling back to manual mode…")

                # ── manual ───────────────────────────────────────────────────
                print(
                    f"[captcha] Please solve the GeeTest slider captcha in the iBox app "
                    f"(synthetic_id={synth_id})"
                )
                print(f"[captcha] Waiting up to {cap_timeout:.0f} s for captcha result…")
                from src.frida_client import poll_captcha
                params = poll_captcha(
                    device_host=device_host,
                    timeout=cap_timeout,
                    clear_before=True,
                )
                if params:
                    print(f"[captcha] Captured from app ✓  lot_number={params['lot_number'][:8]}…")
                    return params, None
                return None, f"Captcha not obtained within {cap_timeout:.0f} s"

            def synthesis_auto_operation():
                activity_list_result = client.get_synthesis_activity_list(activity_list_path)
                if not is_success(activity_list_result):
                    return {
                        "code": activity_list_result.get("code", 1),
                        "activity_list": activity_list_result,
                    }

                activity_ids = extract_activity_ids(activity_list_result)
                if not activity_ids:
                    return {
                        "code": 1,
                        "activity_list": activity_list_result,
                        "error": "Could not discover any synthesis activities from synthesis-activity-list response.",
                    }

                activity_details = []
                synthetic_ids = []
                synthetic_id_to_activity_id: dict[str, str] = {}
                for idx, activity_id in enumerate(activity_ids):
                    if idx > 0 and parsed.detail_interval > 0:
                        time.sleep(parsed.detail_interval)
                    detail_path = render_command_path(
                        config,
                        "synthesis-activity-detail",
                        "/synthesis-service/synthetic/activity/detail?id={activity_id}",
                        activity_id=activity_id,
                    )
                    detail_result = client.get_synthesis_activity_detail(detail_path)
                    # 429: back off and retry once
                    if isinstance(detail_result, dict) and str(detail_result.get("code", "")) == "429" or (
                        isinstance(detail_result, dict) and "429" in str(detail_result.get("_raw", ""))[:50]
                    ):
                        backoff = max(parsed.detail_interval * 3, 1.0)
                        print(f"[detail] 429 on activity {activity_id}, backing off {backoff:.1f}s…", flush=True)
                        time.sleep(backoff)
                        detail_result = client.get_synthesis_activity_detail(detail_path)
                    activity_details.append({"activity_id": activity_id, "detail": detail_result})
                    if is_success(detail_result):
                        new_ids = extract_synthetic_ids(detail_result)
                        for sid in new_ids:
                            synthetic_id_to_activity_id.setdefault(str(sid), str(activity_id))
                        synthetic_ids.extend(new_ids)

                synthetic_ids = list(dict.fromkeys(synthetic_ids))
                if not synthetic_ids:
                    # Pre-fetch: extract upcoming channel IDs from already-fetched details
                    # (bypasses the is_channel_active start-time filter in extract_synthetic_ids)
                    pre_ids, pre_id_map, pre_start = extract_upcoming_synthetic_ids_with_start(activity_details)
                    if pre_ids:
                        now_dt = datetime.now()
                        seconds_until = (pre_start - now_dt).total_seconds() if pre_start else 0
                        if pre_start and 0 < seconds_until <= parsed.pre_start_window:
                            print(
                                f"[wait] Pre-fetched {len(pre_ids)} synthetic ID(s): {pre_ids}; "
                                f"activity opens at {pre_start.strftime('%H:%M:%S')} "
                                f"({seconds_until:.0f}s away) — waiting (no re-fetch needed)…"
                            )
                            wait_target = pre_start
                        else:
                            wait_target = None
                        synthetic_ids = pre_ids
                        for sid, aid in pre_id_map.items():
                            synthetic_id_to_activity_id.setdefault(sid, aid)
                    else:
                        wait_target = None
                else:
                    # synthetic_ids already known — but also check for upcoming channels
                    # that will start soon (e.g. a 抖合 window about to open).  If any
                    # not-yet-active channels fall within pre_start_window, wait for those
                    # and synthesize them instead of the currently-active ones.
                    pre_ids, pre_id_map, _es = extract_upcoming_synthetic_ids_with_start(activity_details)
                    active_set = set(synthetic_ids)
                    upcoming_ids = [sid for sid in pre_ids if sid not in active_set]
                    if upcoming_ids and _es is not None and 0 < (_es - datetime.now()).total_seconds() <= parsed.pre_start_window:
                        wait_target = _es
                        synthetic_ids = upcoming_ids
                        for sid, aid in pre_id_map.items():
                            if sid in set(upcoming_ids):
                                synthetic_id_to_activity_id.setdefault(sid, aid)
                    elif _es is not None and 0 < (_es - datetime.now()).total_seconds() <= parsed.pre_start_window:
                        wait_target = _es
                    else:
                        wait_target = None

                if not synthetic_ids:
                    return {
                        "code": 1,
                        "activity_list": activity_list_result,
                        "activity_details": activity_details,
                        "error": (
                            "Could not discover any synthetic ids from synthesis activity details. "
                            "Inspect result.activity_details and adjust parser aliases if needed."
                        ),
                    }

                # Pre-call synthesis-center during the wait so round 1 can skip it
                pre_center_cache: dict[str, dict] = {}
                if wait_target is not None:
                    seconds_until = (wait_target - datetime.now()).total_seconds()
                    print(
                        f"[wait] Activity opens at {wait_target.strftime('%H:%M:%S')} "
                        f"({seconds_until:.0f}s away) — will pre-fetch synthesis-center "
                        f"{parsed.pre_center_offset:.0f}s before start…"
                    )
                    # Sleep until pre_center_offset seconds before start
                    pre_call_time = wait_target - timedelta(seconds=max(parsed.pre_center_offset, 0))
                    if pre_call_time > datetime.now():
                        _wait_until_start(pre_call_time)
                    # Pre-call synthesis-center for each id
                    for _sid in synthetic_ids:
                        _center_path = render_command_path(
                            config,
                            "synthesis-center",
                            "/synthesis-service/synthetic/center/{synthetic_id}",
                            synthetic_id=_sid,
                        )
                        _center_result = client.get_synthesis_center(_center_path)
                        if is_success(_center_result):
                            pre_center_cache[str(_sid)] = _center_result
                            _cdata = (_center_result.get("data") or {})
                            _surplus = to_int(first_present(_cdata, ("surplusNum", "remainNum", "leftNum")))
                            print(
                                f"[pre-center] {_sid} cached ✔"
                                + (f"  surplusNum={_surplus}" if _surplus is not None else ""),
                                flush=True,
                            )
                        else:
                            print(f"[pre-center] {_sid} failed ({_center_result.get('code')}), will fetch live", flush=True)
                    # Tight spin for remaining time
                    _wait_until_start(wait_target)
                    print("[wait] Activity start time reached, beginning synthesis attempts…")

                rounds = []
                successful_submits = []
                successful_state_by_synthetic_id = {}
                remaining_target_count = parsed.target_count
                for round_no in range(1, max(parsed.max_rounds, 1) + 1):
                    if remaining_target_count is not None and remaining_target_count <= 0:
                        break
                    round_entries = []
                    round_progress = False
                    for synthetic_id in synthetic_ids:
                        if remaining_target_count is not None and remaining_target_count <= 0:
                            break
                        center_path = render_command_path(
                            config,
                            "synthesis-center",
                            "/synthesis-service/synthetic/center/{synthetic_id}",
                            synthetic_id=synthetic_id,
                        )
                        center_result = pre_center_cache.pop(str(synthetic_id), None)
                        if center_result is not None:
                            print(f"[round {round_no}] using pre-fetched center for synthetic_id={synthetic_id}", flush=True)
                        else:
                            center_result = client.get_synthesis_center(center_path)
                        entry = {
                            "synthetic_id": synthetic_id,
                            "center": center_result,
                        }
                        if not is_success(center_result):
                            entry["code"] = center_result.get("code", 1)
                            round_entries.append(entry)
                            continue

                        candidates = extract_recipe_candidates(center_result)
                        if not candidates:
                            entry["code"] = 1
                            entry["error"] = "Could not derive synthesis materials from synthesis-center response."
                            round_entries.append(entry)
                            continue

                        candidate = choose_recipe_candidate(candidates, synthetic_id)
                        max_times = candidate.get("max_times", 0)
                        # Respect the server-side per-user synthesis cap
                        center_data = (center_result or {}).get("data") or {}
                        surplus_num = to_int(first_present(center_data, ("surplusNum", "remainNum", "leftNum")))
                        max_synthetic_num = to_int(first_present(center_data, ("maxSyntheticNum", "maxNum")))
                        server_cap = min(v for v in (surplus_num, max_synthetic_num) if v is not None) if any(v is not None for v in (surplus_num, max_synthetic_num)) else None
                        if server_cap is not None and server_cap < max_times:
                            max_times = server_cap
                        material_state_signature = build_material_state_signature(candidate)

                        if max_times <= 0:
                            entry["plan"] = summarize_synthesis_plan(candidate, max_times)
                            entry["code"] = 0
                            entry["message"] = "Current materials are insufficient for this recipe."
                            round_entries.append(entry)
                            continue

                        available_times = max_times
                        if remaining_target_count is not None:
                            max_times = min(max_times, remaining_target_count)
                        plan = summarize_synthesis_plan(candidate, max_times)
                        plan["available_times_after_caps"] = available_times
                        if remaining_target_count is not None:
                            plan["target_count_remaining_before"] = remaining_target_count
                        entry["plan"] = plan

                        previous_success_state = successful_state_by_synthetic_id.get(str(synthetic_id))
                        if previous_success_state == material_state_signature:
                            entry["code"] = 0
                            entry["submitted"] = False
                            entry["message"] = (
                                "Skipping repeated submit because the material snapshot is unchanged "
                                "after a previous successful submission."
                            )
                            round_entries.append(entry)
                            continue

                        payload = build_synthesis_submit_payload(candidate, synthetic_id, max_times)
                        entry["payload"] = payload
                        if parsed.dry_run:
                            entry["code"] = 0
                            entry["submitted"] = False
                            round_entries.append(entry)
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
                            continue

                        submit_outcome = submit_synthesis_with_retry(
                            client=client,
                            submit_path=submit_path,
                            payload=payload,
                            submit_window=parsed.submit_window,
                            retry_interval=parsed.retry_interval,
                            concurrency=parsed.submit_concurrency,
                        )
                        submit_result = submit_outcome["result"]
                        entry["submit"] = submit_result
                        entry["submit_attempts"] = submit_outcome["attempts"]
                        entry["attempt_count"] = submit_outcome["attempt_count"]
                        entry["submit_concurrency"] = submit_outcome["concurrency"]
                        entry["code"] = submit_result.get("code", 0 if is_success(submit_result) else 1)

                        # ── GeeTest captcha → confirm step ────────────────────
                        # submit code=0 means "accepted, pending captcha".
                        # We must call /confirm with captcha params to actually
                        # complete the synthesis.  In --captcha-mode=skip we
                        # skip this step (legacy broken behaviour).
                        confirmed = False
                        if is_success(submit_result) and parsed.captcha_mode != "skip":
                            captcha_params, captcha_err = _get_captcha_params(str(synthetic_id))
                            if captcha_err:
                                entry["captcha_error"] = captcha_err
                                entry["code"] = 1
                            else:
                                _outer_activity_id = synthetic_id_to_activity_id.get(str(synthetic_id), "")
                                print(f"[confirm] synthetic_id={synthetic_id}  outer_activity_id={_outer_activity_id!r}  synthetic_num={max_times}")
                                _confirm_path = confirm_path_template.format(
                                    uid=uid or "",
                                    captcha_id=captcha_params.get("captcha_id") or parsed.captcha_id,
                                    lot_number=captcha_params["lot_number"],
                                    pass_token=captcha_params["pass_token"],
                                    gen_time=captcha_params["gen_time"],
                                    captcha_output=captcha_params["captcha_output"],
                                )
                                _confirm_body = {
                                    "activityId": int(_outer_activity_id) if _outer_activity_id else None,
                                    "syntheticNum": max_times,
                                    "syntheticId": int(synthetic_id),
                                }
                                print(f"[confirm] body={_confirm_body}")
                                confirm_result = client.confirm_synthesis(_confirm_path, _confirm_body)
                                entry["confirm"] = confirm_result
                                if is_success(confirm_result):
                                    confirmed = True
                                    entry["code"] = 0
                                else:
                                    entry["code"] = (
                                        confirm_result.get("code", 1)
                                        if isinstance(confirm_result, dict)
                                        else 1
                                    )

                        round_entries.append(entry)
                        actually_succeeded = (
                            (parsed.captcha_mode == "skip" and is_success(submit_result))
                            or confirmed
                        )
                        if actually_succeeded:
                            round_progress = True
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
                            successful_state_by_synthetic_id[str(synthetic_id)] = material_state_signature
                            successful_submits.append(
                                {
                                    "round": round_no,
                                    "synthetic_id": synthetic_id,
                                    "times": max_times,
                                    "attempt_count": submit_outcome["attempt_count"],
                                    "submit_concurrency": submit_outcome["concurrency"],
                                    "submit": submit_result,
                                    **({"confirm": entry["confirm"]} if "confirm" in entry else {}),
                                }
                            )

                    rounds.append({"round": round_no, "entries": round_entries})
                    if parsed.dry_run or not round_progress:
                        break

                any_discovered_craftable = any(
                    (entry.get("plan") or {}).get("max_times", 0) > 0
                    for round_info in rounds
                    for entry in round_info["entries"]
                )
                return {
                    "code": 0 if parsed.dry_run or successful_submits or not any_discovered_craftable else 1,
                    "activity_list": activity_list_result,
                    "activity_details": activity_details,
                    "synthetic_ids": synthetic_ids,
                    "rounds": rounds,
                    "target_count": parsed.target_count,
                    "remaining_target_count": remaining_target_count,
                    "submitted_count": len(successful_submits),
                    "submitted": successful_submits,
                }

            operation = synthesis_auto_operation
        elif cmd == "synthesis-confirm":
            payload = parse_payload_arg(parsed.payload)
            path = render_command_path(
                config,
                "synthesis-confirm",
                (
                    "/synthesis-service/synthetic/center/confirm"
                    "?uid={uid}&captcha_id={captcha_id}&lot_number={lot_number}"
                    "&pass_token={pass_token}&gen_time={gen_time}&captcha_output={captcha_output}"
                ),
                uid=parsed.confirm_uid,
                captcha_id=parsed.captcha_id,
                lot_number=parsed.lot_number,
                pass_token=parsed.pass_token,
                gen_time=parsed.gen_time,
                captcha_output=parsed.captcha_output,
            )
            operation = lambda: client.confirm_synthesis(path, payload)
        elif cmd == "market-buy":
            if not uid:
                raise SystemExit("Error: uid is required for market-buy. Pass --uid or ensure login response contains uid")
            payload = parse_payload_arg(parsed.payload)
            path = render_command_path(
                config,
                "market-buy",
                "/order-create-service/batch-purchase-consignment-orders?uid={uid}",
                uid=uid,
            )
            operation = lambda: client.post(path, payload)
        elif cmd == "consign-create":
            payload = parse_payload_arg(parsed.payload)
            path = render_command_path(
                config,
                "consign-create",
                "/order-create-service/consignment-orders",
            )
            operation = lambda: client.post(path, payload)
        elif cmd == "consign-cancel":
            path = render_command_path(
                config,
                "consign-cancel",
                "/order-service/consign-orders/{consign_order_id}/cancel",
                consign_order_id=parsed.consign_order_id,
            )
            operation = lambda: client.post(path)
        elif cmd == "purchase-detail":
            path = render_command_path(
                config,
                "purchase-detail",
                "/order-service/purchase-consignment-orders/{order_uuid}",
                order_uuid=parsed.order_uuid,
            )
            operation = lambda: client.get(path)
        elif cmd == "wanted-detail":
            path = render_command_path(
                config,
                "wanted-detail",
                "/public-service/digital-collection-groups/detail/purchase-consignment-orders/{purchase_order_id}",
                purchase_order_id=parsed.purchase_order_id,
            )
            operation = lambda: client.get(path)
        elif cmd == "wanted-deal":
            _uid = uid or ""
            collection_name = (parsed.collection_name or "").strip()
            group_id_override = (parsed.group_id or "").strip()
            if collection_name or group_id_override:
                target_qty = parsed.quantity
                target_min_price_yuan = parsed.min_price
                target_min_price_fen = int(round(target_min_price_yuan * 100))
                consignment_password = parsed.consignment_password or ""
                collection_id_override = (parsed.collection_id or "").strip()
                payment_platform = parsed.payment_platform
                po_page_size = parsed.po_page_size
                market_search_pages = max(1, int(parsed.market_search_pages or 1))
                market_segment_id = str(parsed.market_segment_id or "-1")
                dry_run = parsed.dry_run

                def wanted_deal_by_name_operation():
                    auth_token = getattr(client, "token", None) or ((saved_session or {}).get("token") if saved_session else None)
                    webview_headers = build_webview_api_headers(
                        auth_token,
                        origin="https://detail-page.ibox.art",
                        app_version=app_version,
                    )
                    warmed_groups = set()
                    auth_failure_result = None

                    def group_from_market_item(item: dict, source: str) -> dict | None:
                        name = first_present(item, ("name", "groupName", "collectionName", "title"))
                        if not name or (collection_name and collection_name not in str(name)):
                            return None
                        gid = first_present(item, ("id", "groupId", "collectionGroupId", "digitalCollectionGroupId"))
                        if gid in (None, ""):
                            return None
                        return {"group_id": str(gid), "name": str(name), "source": source}

                    def find_market_groups() -> list[dict]:
                        if group_id_override:
                            return [{"group_id": group_id_override, "name": collection_name or f"group-{group_id_override}", "source": "cli"}]
                        seen = set()
                        groups = []
                        for page_no in range(1, market_search_pages + 1):
                            path = (
                                "/public-service/markets"
                                f"?sortType=0&pageNo={page_no}&segmentId={market_segment_id}"
                                "&sortField=2&pageSize=50&timeRange=0"
                            )
                            result = client.get(path)
                            if not is_success(result):
                                print(f"[wanted-deal] market page {page_no} failed: code={result.get('code')}", flush=True)
                                continue
                            page_items = extract_list_payload(result)
                            for item in page_items:
                                if not isinstance(item, dict):
                                    continue
                                group = group_from_market_item(item, path)
                                if group and group["group_id"] not in seen:
                                    seen.add(group["group_id"])
                                    groups.append(group)
                            if groups:
                                break
                            if isinstance(result.get("data"), dict) and result["data"].get("hasMore") is False:
                                break
                        return groups

                    def warm_market_context(group_id: str) -> None:
                        nonlocal auth_failure_result
                        if group_id in warmed_groups:
                            return
                        warmed_groups.add(group_id)
                        path = (
                            f"/public-market-service/digital-collection-groups/{group_id}"
                            "/purchase-consignment-info?configType=0"
                        )
                        result = client.get(path)
                        if is_auth_failure(result):
                            auth_failure_result = result
                            return
                        if not is_success(result):
                            print(f"[wanted-deal] market context warmup failed for group {group_id}: code={result.get('code')}", flush=True)

                    def list_purchase_orders(group_id: str) -> list[dict]:
                        nonlocal auth_failure_result
                        warm_market_context(group_id)
                        if auth_failure_result:
                            return []
                        path = (
                            f"/public-market-service/digital-collection-groups/{group_id}"
                            f"/purchase-orders?pageNo=1&pageSize={po_page_size}&uid={_uid}"
                        )
                        result = client.get(path, headers=webview_headers)
                        if is_auth_failure(result):
                            auth_failure_result = result
                            return []
                        if not is_success(result):
                            print(f"[wanted-deal] purchase-orders failed for group {group_id}: code={result.get('code')}", flush=True)
                            return []
                        return extract_list_payload(result)

                    def candidate_from_order(group: dict, order: dict) -> dict | None:
                        po_id = first_present(order, ("id", "purchaseOrderId", "purchaseConsignmentOrderId", "purchaseOrderNo", "advanceOrderId"))
                        relation_id = first_present(order, ("orderRelationId", "relationId", "relation_id"))
                        if po_id in (None, "") or relation_id in (None, ""):
                            return None
                        price_fen = to_price_fen(first_present(order, ("price", "unitPrice", "salePrice")))
                        if target_min_price_fen > 0 and price_fen is not None and price_fen < target_min_price_fen:
                            return None
                        return {
                            "group_id": group["group_id"],
                            "group_name": group["name"],
                            "purchase_order_id": str(po_id),
                            "relation_id": str(relation_id),
                            "price_fen": price_fen,
                            "price_yuan": None if price_fen is None else price_fen / 100,
                            "payment_platform": first_present(order, ("paymentPlatformCode", "paymentPlatform")) or payment_platform,
                        }

                    def pick_owned_collection_id(group_id: str) -> str | None:
                        if collection_id_override:
                            return collection_id_override
                        path = (
                            f"/personal-center-service/users/digital-collection-groups/{group_id}"
                            "?pageSize=20&pageNo=1&lockStatus=0"
                        )
                        result = client.get(path)
                        if not is_success(result):
                            print(f"[wanted-deal] owned collections failed for group {group_id}: code={result.get('code')}", flush=True)
                            return None
                        for item in extract_list_payload(result):
                            if not isinstance(item, dict):
                                continue
                            cid = first_present(item, ("id", "digitalCollectionId", "collectionId", "digitalCollectionID", "collectionID"))
                            if cid not in (None, ""):
                                return str(cid)
                        return None

                    matched_groups = find_market_groups()
                    if not matched_groups:
                        return {"code": 1, "error": f"No public market group found matching name: {collection_name!r}"}

                    print(f"[wanted-deal] matched group(s): " + ", ".join(f"{g['name']}({g['group_id']})" for g in matched_groups), flush=True)

                    candidates = []
                    for group in matched_groups:
                        orders = list_purchase_orders(group["group_id"])
                        if auth_failure_result:
                            return auth_failure_result
                        print(f"[wanted-deal] {len(orders)} purchase order(s) in group {group['group_id']}", flush=True)
                        for order in orders:
                            if not isinstance(order, dict):
                                continue
                            candidate = candidate_from_order(group, order)
                            if candidate:
                                candidates.append(candidate)

                    if not candidates:
                        return {
                            "code": 1,
                            "error": f"No buy orders found for name={collection_name!r} with price >= {target_min_price_yuan}.",
                            "matched_groups": matched_groups,
                        }

                    candidates.sort(key=lambda c: (c["price_fen"] or 0), reverse=True)
                    print(f"[wanted-deal] {len(candidates)} candidate(s):", flush=True)
                    for candidate in candidates[: min(len(candidates), 5)]:
                        print(
                            f"  purchase_order_id={candidate['purchase_order_id']} "
                            f"relation_id={candidate['relation_id']} "
                            f"price={format_price_yuan(candidate['price_fen'])}",
                            flush=True,
                        )

                    if dry_run:
                        return {"code": 0, "dry_run": True, "matched_groups": matched_groups, "candidates": candidates}

                    if not consignment_password:
                        return {"code": 1, "error": "wanted-deal requires --consignment-password"}

                    owned_collection_cache = {}
                    deal_results = []
                    last_code = 1
                    for candidate in candidates[:target_qty]:
                        group_id = candidate["group_id"]
                        if group_id not in owned_collection_cache:
                            owned_collection_cache[group_id] = pick_owned_collection_id(group_id)
                        collection_id = owned_collection_cache[group_id]
                        if not collection_id:
                            return {"code": 1, "error": "Could not select an unlocked owned collection. Pass --collection-id explicitly."}

                        deal_path = render_command_path(
                            config,
                            "wanted-deal",
                            "/order-create-service/advance-orders/{purchase_order_id}/relation/{relation_id}/deal?uid={uid}",
                            purchase_order_id=candidate["purchase_order_id"],
                            relation_id=candidate["relation_id"],
                            uid=_uid,
                        )
                        deal_payload = {
                            "paymentPlatformCode": int(candidate["payment_platform"]),
                            "digitalCollectionId": int(collection_id),
                            "consignmentPassword": consignment_password,
                            "password": consignment_password,
                            "consignPassword": consignment_password,
                            "consignmentPassWord": consignment_password,
                        }
                        print(
                            f"[wanted-deal] dealing purchase_order_id={candidate['purchase_order_id']} "
                            f"relation_id={candidate['relation_id']} collection_id={collection_id}",
                            flush=True,
                        )
                        deal_result = client.post(deal_path, deal_payload)
                        last_code = deal_result.get("code", 0 if is_success(deal_result) else 1)
                        deal_results.append({
                            "purchase_order_id": candidate["purchase_order_id"],
                            "relation_id": candidate["relation_id"],
                            "collection_id": collection_id,
                            "payment_platform": candidate["payment_platform"],
                            "group_name": candidate["group_name"],
                            "price_fen": candidate["price_fen"],
                            "price_yuan": candidate["price_yuan"],
                            "deal": deal_result,
                        })

                    return {"code": last_code, "deal_results": deal_results, "matched_groups": matched_groups}

                operation = wanted_deal_by_name_operation
            else:
                # ── direct ID mode ────────────────────────────────────────────
                if not parsed.purchase_order_id or not parsed.relation_id:
                    raise SystemExit(
                        "Error: wanted-deal requires either --collection-name or both positional "
                        "arguments purchase_order_id and relation_id"
                    )
                extra_payload = parse_payload_arg(parsed.payload)
                deal_payload = {}
                if parsed.consignment_password:
                    deal_payload["consignmentPassword"] = parsed.consignment_password
                    deal_payload["password"] = parsed.consignment_password
                    deal_payload["consignPassword"] = parsed.consignment_password
                    deal_payload["consignmentPassWord"] = parsed.consignment_password
                if extra_payload:
                    deal_payload.update(extra_payload)
                path = render_command_path(
                    config,
                    "wanted-deal",
                    "/order-create-service/advance-orders/{purchase_order_id}/relation/{relation_id}/deal?uid={uid}",
                    purchase_order_id=parsed.purchase_order_id,
                    relation_id=parsed.relation_id,
                    uid=_uid,
                )
                operation = lambda: client.post(path, deal_payload)
        elif cmd == "wanted-buy":
            collection_name = (parsed.collection_name or "").strip()
            group_id = (parsed.group_id or "").strip()
            price_yuan = parsed.price          # user passes yuan, e.g. 4.5
            price_fen = int(round(price_yuan * 100))  # API uses 分 (cents)
            quantity = parsed.quantity
            payment_platform = parsed.payment_platform
            consignment_password = parsed.consignment_password or ""
            dry_run = parsed.dry_run
            extra_payload = parse_payload_arg(parsed.payload) or {}

            # Resolve group_id from name if not provided
            if not group_id and collection_name:
                groups_result = client.get(
                    "/personal-center-service/users/digital-collection-groups"
                    "?groupType=0&isMetaVerse=0&pageNo=1&pageSize=100"
                )
                print(f"[wanted-buy] groups result: {json.dumps(groups_result, ensure_ascii=False)[:400]}", flush=True)
                if not is_success(groups_result):
                    raise SystemExit(f"Error: failed to fetch collection groups: {groups_result.get('code')}")
                groups_data = (groups_result.get("data") or {})
                groups_list = groups_data if isinstance(groups_data, list) else (
                    groups_data.get("list") or groups_data.get("records") or groups_data.get("data") or []
                )
                matched = [g for g in groups_list if isinstance(g, dict) and collection_name in (g.get("name") or "")]
                if not matched:
                    raise SystemExit(f"Error: no collection group matching {collection_name!r}")
                group_id = str(first_present(matched[0], ("id", "groupId")))
                print(f"[wanted-buy] resolved group_id={group_id} name={matched[0].get('name')!r}")
            elif not group_id:
                raise SystemExit("Error: wanted-buy requires --group-id or --collection-name")

            if dry_run:
                def wanted_buy_op():
                    return {"code": 0, "dry_run": True, "group_id": group_id, "price_fen": price_fen, "quantity": quantity}
                operation = wanted_buy_op
            else:
                buy_payload = {
                    "groupId": int(group_id),
                    "price": price_fen,
                    "quantity": quantity,
                    "paymentPlatformCode": payment_platform,
                }
                if consignment_password:
                    buy_payload["consignmentPassword"] = consignment_password
                    buy_payload["password"] = consignment_password
                buy_payload.update(extra_payload)
                buy_path = render_command_path(
                    config,
                    "wanted-buy",
                    "/order-create-service/advance-orders",
                )
                operation = lambda: client.post(buy_path, buy_payload)
        elif cmd == "api":
            payload = parse_payload_arg(parsed.payload)
            operation = (
                (lambda: client.get(parsed.path))
                if parsed.method == "GET"
                else (lambda: client.post(parsed.path, payload))
            )

        result, retried, retry_login_result = call_with_session_retry(
            operation=operation,
            client=client,
            session_path=session_path,
            mobile=parsed.mobile,
            code=normalized_code,
            c_id=c_id,
            invitation=parsed.invitation or "",
            use_rpc=use_rpc,
            device_host=device_host,
            used_saved_session=used_saved_session,
        )
        if retry_login_result is not None:
            login_result = retry_login_result

        print_result({"login": login_result, "result": result})
        sys.exit(0 if is_success(result) else 1)

    mobile = parsed.mobile
    verification_code = normalize_code(parsed.code)
    if cmd == "login" and not verification_code:
        raise SystemExit("Error: login requires an SMS code; '-' is only supported for commands that reuse a saved session")
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
            if is_success(result):
                save_login_session(session_path, mobile, result, use_rpc, device_host)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)
        else:
            cart_cfg = config.get("cart") or {}
            order_cfg = config.get("order") or {}
            result, _, used_saved_session = ensure_authenticated_client(
                client=client,
                session_path=session_path,
                mobile=mobile,
                code=verification_code,
                c_id=c_id,
                invitation=invitation_code,
                use_rpc=use_rpc,
                device_host=device_host,
            )
            print("login:", json.dumps(result, ensure_ascii=False)[:200])
            if isinstance(result, dict) and result.get("code") == 0:
                if cart_cfg.get("path") and product_id:
                    r, retried, retry_login_result = call_with_session_retry(
                        operation=lambda: client.add_cart(cart_cfg["path"], {"productId": product_id, "quantity": 1}),
                        client=client,
                        session_path=session_path,
                        mobile=mobile,
                        code=verification_code,
                        c_id=c_id,
                        invitation=invitation_code,
                        use_rpc=use_rpc,
                        device_host=device_host,
                        used_saved_session=used_saved_session,
                    )
                    if retry_login_result is not None:
                        result = retry_login_result
                    print("cart:", json.dumps(r, ensure_ascii=False)[:200])
                if order_cfg.get("path"):
                    r, retried, retry_login_result = call_with_session_retry(
                        operation=lambda: client.create_order(order_cfg["path"]),
                        client=client,
                        session_path=session_path,
                        mobile=mobile,
                        code=verification_code,
                        c_id=c_id,
                        invitation=invitation_code,
                        use_rpc=use_rpc,
                        device_host=device_host,
                        used_saved_session=used_saved_session,
                    )
                    if retry_login_result is not None:
                        result = retry_login_result
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
            if is_success(result):
                save_login_session(session_path, mobile, result, use_rpc, device_host)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if (isinstance(result, dict) and result.get("code") == 0) else 1)

        cart_cfg = config.get("cart") or {}
        order_cfg = config.get("order") or {}
        from src.api_client import IBoxClient
        client = IBoxClient(base_url=base_url, headers=headers)
        login_result, _, used_saved_session = ensure_authenticated_client(
            client=client,
            session_path=session_path,
            mobile=mobile,
            code=verification_code,
            c_id=c_id,
            invitation=invitation_code,
            use_rpc=use_rpc,
            device_host=device_host,
        )
        results = {"login": login_result}
        if isinstance(login_result, dict) and login_result.get("code") == 0:
            if cart_cfg.get("path") and product_id:
                results["cart"], retried, retry_login_result = call_with_session_retry(
                    operation=lambda: client.add_cart(cart_cfg["path"], payload={"productId": product_id, "quantity": 1}),
                    client=client,
                    session_path=session_path,
                    mobile=mobile,
                    code=verification_code,
                    c_id=c_id,
                    invitation=invitation_code,
                    use_rpc=use_rpc,
                    device_host=device_host,
                    used_saved_session=used_saved_session,
                )
                if retry_login_result is not None:
                    results["login"] = retry_login_result
            if order_cfg.get("path"):
                results["order"], retried, retry_login_result = call_with_session_retry(
                    operation=lambda: client.create_order(order_cfg["path"]),
                    client=client,
                    session_path=session_path,
                    mobile=mobile,
                    code=verification_code,
                    c_id=c_id,
                    invitation=invitation_code,
                    use_rpc=use_rpc,
                    device_host=device_host,
                    used_saved_session=used_saved_session,
                )
                if retry_login_result is not None:
                    results["login"] = retry_login_result
        for name, res in results.items():
            print(name, ":", json.dumps(res, ensure_ascii=False)[:300] if res else "None")
        sys.exit(0 if (isinstance(results.get("login"), dict) and results["login"].get("code") == 0) else 1)


if __name__ == "__main__":
    main()
