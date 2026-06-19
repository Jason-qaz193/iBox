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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import re
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
        default=0,
        help="Maximum scan cycles (0 = unlimited until done or --target-count reached)",
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
        "--scan-concurrency",
        type=int,
        default=4,
        help="Parallel workers for activity-detail and synthesis-center requests (default: 4)",
    )
    synthesis_auto.add_argument(
        "--loop-interval",
        type=float,
        default=2.0,
        help="Seconds to wait before re-scanning when activities are not ready yet (default: 2)",
    )
    synthesis_auto.add_argument(
        "--detail-interval",
        type=float,
        default=0.3,
        help="Seconds to wait between activity-detail requests when --scan-concurrency=1 (default: 0.3)",
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
    synthesis_auto.add_argument(
        "--captcha-mode",
        choices=["auto", "manual", "skip"],
        default="auto",
        help="Only used when synthesis-center returns needSlider=1",
    )
    synthesis_auto.add_argument(
        "--captcha-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for captcha when needSlider=1",
    )
    synthesis_auto.add_argument(
        "--captcha-id",
        default="0d4b08eac1cbdcad36bbf607c5bf3e1b",
        help="GeeTest captcha_id when needSlider=1",
    )
    synthesis_auto.add_argument(
        "--captcha-headed",
        action="store_true",
        help="Show browser window for Playwright captcha solve when needSlider=1",
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
    market_buy.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称")
    market_buy.add_argument("--price", dest="price", type=float, default=None, help="最高单价（元）")
    market_buy.add_argument("--quantity", dest="quantity", type=positive_int, default=1, help="购买数量 (default: 1)")
    market_buy.add_argument(
        "--payment-platform",
        dest="payment_platform",
        type=int,
        default=None,
        help="支付平台代码 (default: 30)",
    )
    add_payload_arg(market_buy, required=False)

    consign_create = subparsers.add_parser(
        "consign-create",
        help="Create consignment listing(s) from owned collections",
    )
    add_auth_args(consign_create)
    consign_create.add_argument(
        "--支付密码",
        dest="consignment_password",
        required=True,
        help="寄售/支付密码",
    )
    consign_create.add_argument(
        "--藏品名字",
        dest="collection_name",
        required=True,
        help="藏品名称（匹配「我的藏品」分组名）",
    )
    consign_create.add_argument(
        "--出售价格",
        dest="price",
        type=float,
        required=True,
        help="寄售单价（元）",
    )
    consign_create.add_argument(
        "--出售数量",
        dest="quantity",
        type=positive_int,
        required=True,
        help="寄售数量",
    )
    consign_create.add_argument("--group-id", dest="group_id", default="", help="藏品分组 ID（可代替 --藏品名字）")
    consign_create.add_argument(
        "--payment-platform",
        dest="payment_platform",
        type=int,
        default=None,
        help="支付方式代码，写入 paymentPlatformCodes（默认 30）",
    )
    add_payload_arg(consign_create, required=False)

    consign_cancel = subparsers.add_parser(
        "consign-cancel",
        help="Cancel consignment listing(s) for a collection by name",
    )
    add_auth_args(consign_cancel)
    consign_cancel.add_argument(
        "--支付密码",
        dest="consignment_password",
        required=True,
        help="寄售/支付密码",
    )
    consign_cancel.add_argument(
        "--藏品名字",
        dest="collection_name",
        required=True,
        help="藏品名称（匹配「我的藏品」分组名）",
    )
    consign_cancel.add_argument(
        "--下架价格",
        dest="price",
        type=float,
        required=True,
        help="仅下架指定单价（元）的寄售单",
    )
    consign_cancel.add_argument(
        "--下架数量",
        dest="quantity",
        type=positive_int,
        required=True,
        help="下架数量",
    )
    consign_cancel.add_argument("--group-id", dest="group_id", default="", help="藏品分组 ID（可代替 --藏品名字）")

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


_OWNED_COLLECTION_ID_KEYS = (
    "id",
    "digitalCollectionId",
    "collectionId",
    "digitalCollectionID",
    "collectionID",
)


def normalize_collection_name(name: str | None) -> str:
    """Match keys for collection names; ignore punctuation and spaces."""
    if name is None:
        return ""
    normalized = str(name).strip()
    normalized = re.sub(
        r"[^\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFFA-Za-z0-9\u2160-\u2169]+",
        "",
        normalized,
    )
    return normalized.lower()


def collection_group_display_name(group: dict) -> str:
    return str(
        first_present(
            group,
            ("name", "groupName", "collectionName", "title", "digitalCollectionGroupName"),
        )
        or ""
    ).strip()


def _match_collection_groups(groups_list: list, collection_name: str) -> list[dict]:
    normalized_target = normalize_collection_name(collection_name)
    if not normalized_target:
        return [
            g
            for g in groups_list
            if isinstance(g, dict) and collection_name in collection_group_display_name(g)
        ]

    matched: list[dict] = []
    for group in groups_list:
        if not isinstance(group, dict):
            continue
        normalized_name = normalize_collection_name(collection_group_display_name(group))
        if not normalized_name:
            continue
        if normalized_target in normalized_name or normalized_name in normalized_target:
            matched.append(group)

    exact = [
        g
        for g in matched
        if normalize_collection_name(collection_group_display_name(g)) == normalized_target
    ]
    return exact or matched


def fetch_owned_collection_groups(client) -> list[dict] | dict:
    """Fetch owned collection groups (paginated). Returns auth-failure dict on 401."""
    all_groups: list[dict] = []
    seen_keys: set[str] = set()
    page_size = 20
    query_variants = [{"groupType": 0}, {"groupType": 1}]

    for variant in query_variants:
        page_no = 1
        while True:
            result = client.get(
                "/personal-center-service/users/digital-collection-groups"
                f"?groupType={variant['groupType']}&pageNo={page_no}&pageSize={page_size}"
            )
            if is_auth_failure(result):
                return result
            if isinstance(result, dict) and result.get("rpcBridgeNotReady"):
                return result
            if not is_success(result):
                code = result.get("code")
                if str(code) == "401":
                    return result
                break

            groups_data = (result.get("data") or {})
            groups_list = groups_data if isinstance(groups_data, list) else (
                groups_data.get("list") or groups_data.get("records") or groups_data.get("data") or []
            )
            if isinstance(groups_list, list):
                for group in groups_list:
                    if not isinstance(group, dict):
                        continue
                    gid = first_present(
                        group,
                        ("id", "groupId", "digitalCollectionGroupId", "collectionGroupId"),
                    )
                    key = str(gid) if gid not in (None, "") else collection_group_display_name(group)
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        all_groups.append(group)

            has_more = False
            if isinstance(groups_data, dict):
                has_more = bool(groups_data.get("hasMore"))
            if not has_more and isinstance(groups_list, list) and len(groups_list) < page_size:
                break
            if not groups_list:
                break
            page_no += 1
            if page_no > 50:
                break
    return all_groups


def suggest_owned_collection_names(groups_list: list, collection_name: str, *, limit: int = 8) -> list[str]:
    target = normalize_collection_name(collection_name)
    if not target:
        return []
    ranked: list[tuple[int, str]] = []
    for group in groups_list:
        if not isinstance(group, dict):
            continue
        display = collection_group_display_name(group)
        if not display:
            continue
        norm = normalize_collection_name(display)
        if not norm:
            continue
        if norm == target:
            score = 0
        elif target in norm or norm in target:
            score = 1
        elif target[:2] in norm:
            score = 2
        else:
            continue
        ranked.append((score, display))
    ranked.sort(key=lambda item: (item[0], len(item[1])))
    seen: set[str] = set()
    suggestions: list[str] = []
    for _, display in ranked:
        if display in seen:
            continue
        seen.add(display)
        suggestions.append(display)
        if len(suggestions) >= limit:
            break
    return suggestions


def resolve_group_id_by_public_market(
    client,
    collection_name: str,
    *,
    search_pages: int = 10,
    segment_id: str = "-1",
) -> str | dict:
    normalized_target = normalize_collection_name(collection_name)
    if not normalized_target and not (collection_name or "").strip():
        raise SystemExit(f"Error: invalid collection name '{collection_name}'")

    seen = set()
    for page_no in range(1, max(1, int(search_pages)) + 1):
        path = (
            "/public-service/markets"
            f"?sortType=0&pageNo={page_no}&segmentId={segment_id}"
            "&sortField=2&pageSize=50&timeRange=0"
        )
        result = client.get(path)
        if is_auth_failure(result):
            return result
        if not is_success(result):
            continue
        for item in extract_list_payload(result):
            if not isinstance(item, dict):
                continue
            name = first_present(item, ("name", "groupName", "collectionName", "title"))
            if not name:
                continue
            if normalized_target:
                if normalized_target not in normalize_collection_name(str(name)):
                    continue
            elif collection_name not in str(name):
                continue
            gid = first_present(item, ("id", "groupId", "collectionGroupId", "digitalCollectionGroupId"))
            if gid in (None, "") or str(gid) in seen:
                continue
            seen.add(str(gid))
            print(f"[resolve] public market: {name!r} -> group_id={gid}", flush=True)
            return str(gid)
        data = result.get("data")
        if isinstance(data, dict) and data.get("hasMore") is False:
            break
    return ""


def resolve_group_id_for_consign(
    client,
    collection_name: str,
) -> tuple[str, str, dict | None]:
    owned = fetch_owned_collection_groups(client)
    if isinstance(owned, dict):
        return "", "", owned

    matched = _match_collection_groups(owned, collection_name)
    if matched:
        group_id = str(
            first_present(
                matched[0],
                ("id", "groupId", "digitalCollectionGroupId", "collectionGroupId"),
            )
        )
        display = collection_group_display_name(matched[0]) or collection_name
        print(
            f"[consign-create] matched owned group: {display!r} -> group_id={group_id}",
            flush=True,
        )
        return group_id, display, None

    public = resolve_group_id_by_public_market(client, collection_name)
    if isinstance(public, dict):
        return "", "", public
    if public:
        owned_items = list_unlocked_digital_collection_ids(client, public, limit=1)
        if owned_items:
            print(
                f"[consign-create] resolved via public market (owned items found): group_id={public}",
                flush=True,
            )
            return public, collection_name, None

    return "", "", None


def resolve_group_id_for_market(client, collection_name: str, config: dict | None = None) -> str | dict:
    """Prefer public market lookup; fall back to owned collections."""
    public = resolve_group_id_by_public_market(client, collection_name)
    if isinstance(public, dict):
        return public
    if public:
        print(f"[market-buy] public market: {collection_name!r} -> group_id={public}", flush=True)
        return public

    owned = fetch_owned_collection_groups(client)
    if isinstance(owned, dict):
        return owned
    matched = _match_collection_groups(owned, collection_name)
    if matched:
        group_id = str(
            first_present(
                matched[0],
                ("id", "groupId", "digitalCollectionGroupId", "collectionGroupId"),
            )
        )
        print(f"[market-buy] owned group: {collection_name!r} -> group_id={group_id}", flush=True)
        return group_id

    raise SystemExit(f"Error: collection '{collection_name}' not found in public market or your wallet")


def build_market_buy_payload(
    group_id: str,
    price_yuan: float,
    quantity: int,
    payment_platform: int,
    *,
    extra: dict | None = None,
) -> dict:
    price_fen = int(round(float(price_yuan) * 100))
    payload = {
        "digitalCollectionGroupId": int(group_id),
        "maxCount": max(1, int(quantity)),
        "maxSinglePrice": price_fen,
        "paymentPlatformCode": int(payment_platform),
        "level": -1,
    }
    if extra:
        payload.update(extra)
    return payload


def list_owned_collection_items(
    client,
    group_id: str,
    *,
    lock_status: int | None = None,
    consigned_only: bool = False,
) -> list[dict] | None:
    items_out: list[dict] = []
    page_no = 1
    page_size = 50
    while True:
        path = (
            f"/personal-center-service/users/digital-collection-groups/{group_id}"
            f"?pageSize={page_size}&pageNo={page_no}"
        )
        if lock_status is not None:
            path += f"&lockStatus={int(lock_status)}"
        result = client.get(path)
        if not is_success(result):
            return None
        items = extract_list_payload(result)
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            if consigned_only and int(item.get("consignmentStatus") or 0) != 1:
                continue
            items_out.append(item)
        if len(items) < page_size:
            break
        data = result.get("data")
        if isinstance(data, dict) and data.get("hasMore") is False:
            break
        page_no += 1
        if page_no > 100:
            break
    return items_out


def list_unlocked_digital_collection_ids(
    client,
    group_id: str,
    *,
    limit: int | None = None,
) -> list[str] | None:
    items = list_owned_collection_items(client, group_id, lock_status=0)
    if items is None:
        return None
    collection_ids: list[str] = []
    want = max(1, int(limit)) if limit is not None else None
    for item in items:
        cid = first_present(item, _OWNED_COLLECTION_ID_KEYS)
        if cid not in (None, ""):
            collection_ids.append(str(cid))
            if want is not None and len(collection_ids) >= want:
                return collection_ids[:want]
    if want is not None:
        return collection_ids[:want]
    return collection_ids


def list_consigned_orders_for_cancel(
    client,
    group_id: str,
    *,
    price_yuan: float | None = None,
    limit: int | None = None,
) -> tuple[list[dict], int] | None:
    """Owned items with consignmentStatus=1; orderId is the consign-orders/{id}/cancel id."""
    items = list_owned_collection_items(client, group_id, consigned_only=True)
    if items is None:
        return None
    target_fen = int(round(float(price_yuan) * 100)) if price_yuan is not None else None
    orders: list[dict] = []
    for item in items:
        consign_id = first_present(
            item,
            ("orderId", "consignOrderId", "consignmentOrderId", "consignmentOrderNo"),
        )
        if consign_id in (None, ""):
            continue
        if target_fen is not None:
            item_fen = to_price_fen(item.get("price"))
            if item_fen is None or item_fen != target_fen:
                continue
        orders.append(
            {
                "consign_order_id": str(consign_id),
                "digital_collection_id": first_present(item, _OWNED_COLLECTION_ID_KEYS),
                "name": item.get("name") or "",
                "price": item.get("price"),
            }
        )
    total_matched = len(orders)
    if limit is not None:
        orders = orders[: max(1, int(limit))]
    return orders, total_matched


def build_consign_password_fields(consignment_password: str) -> dict:
    pwd = str(consignment_password)
    return {
        "consignPassword": pwd,
        "consignmentPassword": pwd,
        "password": pwd,
        "consignmentPassWord": pwd,
    }


def build_consign_create_payload(
    digital_collection_id: str,
    price_yuan: float,
    consignment_password: str,
    *,
    payment_platform_codes: list[int] | None = None,
    extra: dict | None = None,
) -> dict:
    price_val: int | float = float(price_yuan)
    if price_val == int(price_val):
        price_val = int(price_val)
    pwd = str(consignment_password)
    payload = {
        "digitalCollectionId": int(digital_collection_id),
        "price": price_val,
        "paymentPlatformCodes": [int(x) for x in (payment_platform_codes or [30])],
        **build_consign_password_fields(pwd),
    }
    if extra:
        payload.update(extra)
    return payload


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


def _fetch_single_activity_detail(*, worker_client, config, activity_id: str, detail_interval: float) -> dict:
    detail_path = render_command_path(
        config,
        "synthesis-activity-detail",
        "/synthesis-service/synthetic/activity/detail?id={activity_id}",
        activity_id=activity_id,
    )
    detail_result = worker_client.get_synthesis_activity_detail(detail_path)
    if isinstance(detail_result, dict) and str(detail_result.get("code", "")) == "429" or (
        isinstance(detail_result, dict) and "429" in str(detail_result.get("_raw", ""))[:50]
    ):
        backoff = max(detail_interval * 3, 1.0)
        print(f"[detail] 429 on activity {activity_id}, backing off {backoff:.1f}s…", flush=True)
        time.sleep(backoff)
        detail_result = worker_client.get_synthesis_activity_detail(detail_path)
    return {"activity_id": activity_id, "detail": detail_result}


def fetch_synthesis_activity_details_parallel(
    *,
    client,
    config,
    activity_ids: list[str],
    detail_interval: float,
    concurrency: int,
) -> list[dict]:
    if not activity_ids:
        return []
    workers = max(concurrency, 1)
    if workers == 1:
        activity_details = []
        for idx, activity_id in enumerate(activity_ids):
            if idx > 0 and detail_interval > 0:
                time.sleep(detail_interval)
            activity_details.append(
                _fetch_single_activity_detail(
                    worker_client=client,
                    config=config,
                    activity_id=activity_id,
                    detail_interval=detail_interval,
                )
            )
        return activity_details

    activity_details: list[dict | None] = [None] * len(activity_ids)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for index, activity_id in enumerate(activity_ids):
            worker_client = build_parallel_submit_client(client)
            future = executor.submit(
                _fetch_single_activity_detail,
                worker_client=worker_client,
                config=config,
                activity_id=activity_id,
                detail_interval=detail_interval,
            )
            future_map[future] = index
        for future in as_completed(future_map):
            activity_details[future_map[future]] = future.result()
    return [item for item in activity_details if item is not None]


def resolve_synthetic_targets_from_details(
    activity_details: list[dict],
    pre_start_window: float,
) -> tuple[list[str], dict[str, str], datetime | None]:
    synthetic_ids: list[str] = []
    synthetic_id_to_activity_id: dict[str, str] = {}
    for item in activity_details:
        detail_result = item.get("detail")
        activity_id = str(item.get("activity_id", ""))
        if is_success(detail_result):
            new_ids = extract_synthetic_ids(detail_result)
            for sid in new_ids:
                synthetic_id_to_activity_id.setdefault(str(sid), activity_id)
            synthetic_ids.extend(new_ids)

    synthetic_ids = list(dict.fromkeys(synthetic_ids))
    wait_target: datetime | None = None
    if not synthetic_ids:
        pre_ids, pre_id_map, pre_start = extract_upcoming_synthetic_ids_with_start(activity_details)
        if pre_ids:
            now_dt = datetime.now()
            seconds_until = (pre_start - now_dt).total_seconds() if pre_start else 0
            if pre_start and 0 < seconds_until <= pre_start_window:
                print(
                    f"[wait] Pre-fetched {len(pre_ids)} synthetic ID(s): {pre_ids}; "
                    f"activity opens at {pre_start.strftime('%H:%M:%S')} "
                    f"({seconds_until:.0f}s away) — waiting…",
                    flush=True,
                )
                wait_target = pre_start
            synthetic_ids = pre_ids
            for sid, aid in pre_id_map.items():
                synthetic_id_to_activity_id.setdefault(sid, aid)
    else:
        pre_ids, pre_id_map, earliest_start = extract_upcoming_synthetic_ids_with_start(activity_details)
        active_set = set(synthetic_ids)
        upcoming_ids = [sid for sid in pre_ids if sid not in active_set]
        if upcoming_ids and earliest_start is not None and 0 < (earliest_start - datetime.now()).total_seconds() <= pre_start_window:
            wait_target = earliest_start
            synthetic_ids = upcoming_ids
            for sid, aid in pre_id_map.items():
                if sid in set(upcoming_ids):
                    synthetic_id_to_activity_id.setdefault(sid, aid)
        elif earliest_start is not None and 0 < (earliest_start - datetime.now()).total_seconds() <= pre_start_window:
            wait_target = earliest_start

    return synthetic_ids, synthetic_id_to_activity_id, wait_target


def fetch_synthesis_centers_parallel(
    *,
    client,
    config,
    synthetic_ids: list[str],
    concurrency: int,
    pre_center_cache: dict[str, dict] | None = None,
) -> dict[str, dict]:
    center_results = dict(pre_center_cache or {})
    pending = [str(synthetic_id) for synthetic_id in synthetic_ids if str(synthetic_id) not in center_results]
    if not pending:
        return center_results

    def fetch_one(synthetic_id: str) -> tuple[str, dict]:
        try:
            worker_client = build_parallel_submit_client(client)
            center_path = render_command_path(
                config,
                "synthesis-center",
                "/synthesis-service/synthetic/center/{synthetic_id}",
                synthetic_id=synthetic_id,
            )
            return synthetic_id, worker_client.get_synthesis_center(center_path)
        except Exception as exc:
            return synthetic_id, {"code": 1, "error": str(exc)}

    workers = max(concurrency, 1)
    if workers == 1:
        for synthetic_id in pending:
            sid, result = fetch_one(synthetic_id)
            center_results[sid] = result
        return center_results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for sid, result in executor.map(fetch_one, pending):
            center_results[sid] = result
    return center_results


def run_pre_start_wait_and_pre_center(
    *,
    client,
    config,
    synthetic_ids: list[str],
    wait_target: datetime,
    pre_center_offset: float,
    scan_concurrency: int,
) -> dict[str, dict]:
    seconds_until = (wait_target - datetime.now()).total_seconds()
    print(
        f"[wait] Activity opens at {wait_target.strftime('%H:%M:%S')} "
        f"({seconds_until:.0f}s away) — will pre-fetch synthesis-center "
        f"{pre_center_offset:.0f}s before start…",
        flush=True,
    )
    pre_call_time = wait_target - timedelta(seconds=max(pre_center_offset, 0))
    if pre_call_time > datetime.now():
        _wait_until_start(pre_call_time)

    pre_center_cache = fetch_synthesis_centers_parallel(
        client=client,
        config=config,
        synthetic_ids=synthetic_ids,
        concurrency=scan_concurrency,
    )
    for synthetic_id in synthetic_ids:
        center_result = pre_center_cache.get(str(synthetic_id), {})
        if is_success(center_result):
            center_data = (center_result.get("data") or {})
            surplus_num = to_int(first_present(center_data, ("surplusNum", "remainNum", "leftNum")))
            print(
                f"[pre-center] {synthetic_id} cached ✔"
                + (f"  surplusNum={surplus_num}" if surplus_num is not None else ""),
                flush=True,
            )
        else:
            print(
                f"[pre-center] {synthetic_id} failed ({center_result.get('code')}), will fetch live",
                flush=True,
            )

    _wait_until_start(wait_target)
    print("[wait] Activity start time reached, beginning synthesis attempts…", flush=True)
    return pre_center_cache


def synthesis_needs_slider(center_result: dict) -> bool:
    center_data = (center_result or {}).get("data") or {}
    need_slider = to_int(first_present(center_data, ("needSlider", "need_slider")))
    return bool(need_slider)


def build_synthesis_confirm_path(
    config: dict,
    uid: str,
    captcha_params: dict | None = None,
) -> str:
    params = captcha_params or {}
    return render_command_path(
        config,
        "synthesis-confirm",
        (
            "/synthesis-service/synthetic/center/confirm"
            "?uid={uid}&captcha_id={captcha_id}"
            "&lot_number={lot_number}&pass_token={pass_token}"
            "&gen_time={gen_time}&captcha_output={captcha_output}"
        ),
        uid=uid or "",
        captcha_id=params.get("captcha_id", ""),
        lot_number=params.get("lot_number", ""),
        pass_token=params.get("pass_token", ""),
        gen_time=params.get("gen_time", ""),
        captcha_output=params.get("captcha_output", ""),
    )


def build_synthesis_confirm_body(*, outer_activity_id: str, synthetic_id: str, synthetic_num: int) -> dict:
    return {
        "activityId": int(outer_activity_id) if outer_activity_id else None,
        "syntheticNum": synthetic_num,
        "syntheticId": int(synthetic_id),
    }


def resolve_outer_activity_id(
    synthetic_id: str,
    synthetic_id_to_activity_id: dict[str, str],
    candidate: dict,
) -> str:
    outer_activity_id = synthetic_id_to_activity_id.get(str(synthetic_id), "")
    if not outer_activity_id:
        candidate_activity_id = candidate.get("activity_id")
        if candidate_activity_id not in (None, ""):
            outer_activity_id = str(candidate_activity_id)
    return outer_activity_id


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

            def _get_captcha_params(synth_id: str) -> tuple[dict | None, str | None]:
                cap_mode = parsed.captcha_mode
                cap_timeout = parsed.captcha_timeout
                cap_id = parsed.captcha_id

                if cap_mode in ("auto", "playwright"):
                    try:
                        from src.geetest_solver import playwright_solve, check_dependencies
                        ok, msg = check_dependencies()
                        if not ok:
                            raise ImportError(msg)
                        print(f"[captcha] Auto-solving GeeTest V4 (synthetic_id={synth_id})…", flush=True)
                        result = playwright_solve(
                            captcha_id=cap_id,
                            timeout=cap_timeout,
                            headed=getattr(parsed, "captcha_headed", False),
                        )
                        if result and result.get("lot_number"):
                            print(f"[captcha] Auto-solved ✓  lot_number={result['lot_number'][:8]}…", flush=True)
                            return result, None
                        raise RuntimeError(f"Playwright returned empty result: {result}")
                    except Exception as exc:
                        print(f"[captcha] Auto-solve failed: {exc}", flush=True)
                        if cap_mode == "playwright":
                            return None, f"Playwright captcha solve failed: {exc}"
                        print("[captcha] Falling back to manual mode…", flush=True)

                if cap_mode == "skip":
                    return None, "Captcha required (needSlider=1) but --captcha-mode=skip"

                print(
                    f"[captcha] Please solve the GeeTest slider captcha in the iBox app "
                    f"(synthetic_id={synth_id})",
                    flush=True,
                )
                print(f"[captcha] Waiting up to {cap_timeout:.0f} s for captcha result…", flush=True)
                from src.frida_client import poll_captcha
                params = poll_captcha(
                    device_host=device_host,
                    timeout=cap_timeout,
                    clear_before=True,
                )
                if params:
                    print(f"[captcha] Captured from app ✓  lot_number={params['lot_number'][:8]}…", flush=True)
                    return params, None
                return None, f"Captcha not obtained within {cap_timeout:.0f} s"

            def synthesis_auto_operation():
                cycles = []
                successful_submits = []
                successful_state_by_synthetic_id: dict[str, tuple] = {}
                remaining_target_count = parsed.target_count
                last_activity_list_result = None
                last_activity_details: list[dict] = []
                last_synthetic_ids: list[str] = []
                cycle_no = 0

                while True:
                    cycle_no += 1
                    if parsed.max_rounds > 0 and cycle_no > parsed.max_rounds:
                        print(f"[cycle {cycle_no - 1}] Reached --max-rounds={parsed.max_rounds}, stopping.", flush=True)
                        break
                    if remaining_target_count is not None and remaining_target_count <= 0:
                        print(f"[cycle {cycle_no}] Target count reached, stopping.", flush=True)
                        break

                    print(f"[cycle {cycle_no}] Fetching latest synthesis activities…", flush=True)
                    activity_list_result = client.get_synthesis_activity_list(activity_list_path)
                    last_activity_list_result = activity_list_result
                    if not is_success(activity_list_result):
                        print(
                            f"[cycle {cycle_no}] activity-list failed ({activity_list_result.get('code')}), "
                            f"retry in {parsed.loop_interval:.1f}s…",
                            flush=True,
                        )
                        time.sleep(max(parsed.loop_interval, 0.1))
                        continue

                    activity_ids = extract_activity_ids(activity_list_result)
                    if not activity_ids:
                        print(
                            f"[cycle {cycle_no}] No activities found, retry in {parsed.loop_interval:.1f}s…",
                            flush=True,
                        )
                        time.sleep(max(parsed.loop_interval, 0.1))
                        continue

                    activity_details = fetch_synthesis_activity_details_parallel(
                        client=client,
                        config=config,
                        activity_ids=activity_ids,
                        detail_interval=parsed.detail_interval,
                        concurrency=parsed.scan_concurrency,
                    )
                    last_activity_details = activity_details

                    synthetic_ids, synthetic_id_to_activity_id, wait_target = resolve_synthetic_targets_from_details(
                        activity_details,
                        parsed.pre_start_window,
                    )
                    last_synthetic_ids = synthetic_ids

                    if not synthetic_ids:
                        print(
                            f"[cycle {cycle_no}] No synthetic ids discovered, retry in {parsed.loop_interval:.1f}s…",
                            flush=True,
                        )
                        time.sleep(max(parsed.loop_interval, 0.1))
                        continue

                    pre_center_cache: dict[str, dict] = {}
                    if wait_target is not None and wait_target > datetime.now():
                        pre_center_cache = run_pre_start_wait_and_pre_center(
                            client=client,
                            config=config,
                            synthetic_ids=synthetic_ids,
                            wait_target=wait_target,
                            pre_center_offset=parsed.pre_center_offset,
                            scan_concurrency=parsed.scan_concurrency,
                        )

                    center_results = fetch_synthesis_centers_parallel(
                        client=client,
                        config=config,
                        synthetic_ids=synthetic_ids,
                        concurrency=parsed.scan_concurrency,
                        pre_center_cache=pre_center_cache,
                    )

                    cycle_entries = []
                    cycle_progress = False
                    any_craftable = False
                    for synthetic_id in synthetic_ids:
                        if remaining_target_count is not None and remaining_target_count <= 0:
                            break

                        center_result = center_results.get(str(synthetic_id), {})
                        if str(synthetic_id) in pre_center_cache:
                            print(
                                f"[cycle {cycle_no}] using pre-fetched center for synthetic_id={synthetic_id}",
                                flush=True,
                            )
                        entry = {
                            "synthetic_id": synthetic_id,
                            "center": center_result,
                        }
                        if not is_success(center_result):
                            entry["code"] = center_result.get("code", 1)
                            cycle_entries.append(entry)
                            continue

                        candidates = extract_recipe_candidates(center_result)
                        if not candidates:
                            entry["code"] = 1
                            entry["error"] = "Could not derive synthesis materials from synthesis-center response."
                            cycle_entries.append(entry)
                            continue

                        candidate = choose_recipe_candidate(candidates, synthetic_id)
                        max_times = candidate.get("max_times", 0)
                        center_data = (center_result or {}).get("data") or {}
                        surplus_num = to_int(first_present(center_data, ("surplusNum", "remainNum", "leftNum")))
                        max_synthetic_num = to_int(first_present(center_data, ("maxSyntheticNum", "maxNum")))
                        server_cap = (
                            min(v for v in (surplus_num, max_synthetic_num) if v is not None)
                            if any(v is not None for v in (surplus_num, max_synthetic_num))
                            else None
                        )
                        if server_cap is not None and server_cap < max_times:
                            max_times = server_cap
                        material_state_signature = build_material_state_signature(candidate)

                        if max_times <= 0:
                            entry["plan"] = summarize_synthesis_plan(candidate, max_times)
                            entry["code"] = 0
                            entry["message"] = "Current materials are insufficient for this recipe."
                            cycle_entries.append(entry)
                            continue

                        any_craftable = True
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
                            cycle_entries.append(entry)
                            continue

                        payload = build_synthesis_submit_payload(candidate, synthetic_id, max_times)
                        entry["payload"] = payload
                        if parsed.dry_run:
                            entry["code"] = 0
                            entry["submitted"] = False
                            cycle_entries.append(entry)
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
                            cycle_progress = True
                            continue

                        print(
                            f"[cycle {cycle_no}] submitting synthetic_id={synthetic_id} "
                            f"syntheticNum={max_times}",
                            flush=True,
                        )
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

                        confirmed = False
                        if is_success(submit_result):
                            need_slider = synthesis_needs_slider(center_result)
                            outer_activity_id = resolve_outer_activity_id(
                                synthetic_id,
                                synthetic_id_to_activity_id,
                                candidate,
                            )
                            if need_slider:
                                captcha_params, captcha_err = _get_captcha_params(str(synthetic_id))
                                if captcha_err:
                                    entry["captcha_error"] = captcha_err
                                    entry["code"] = 1
                                else:
                                    confirm_path = build_synthesis_confirm_path(
                                        config,
                                        uid or "",
                                        captcha_params,
                                    )
                                    confirm_body = build_synthesis_confirm_body(
                                        outer_activity_id=outer_activity_id,
                                        synthetic_id=str(synthetic_id),
                                        synthetic_num=max_times,
                                    )
                                    print(
                                        f"[confirm] synthetic_id={synthetic_id} needSlider=1 "
                                        f"activityId={outer_activity_id!r} syntheticNum={max_times}",
                                        flush=True,
                                    )
                                    confirm_result = client.confirm_synthesis(confirm_path, confirm_body)
                                    entry["confirm"] = confirm_result
                                    confirmed = is_success(confirm_result)
                                    entry["code"] = 0 if confirmed else confirm_result.get("code", 1)
                            else:
                                confirm_path = build_synthesis_confirm_path(config, uid or "")
                                confirm_body = build_synthesis_confirm_body(
                                    outer_activity_id=outer_activity_id,
                                    synthetic_id=str(synthetic_id),
                                    synthetic_num=max_times,
                                )
                                print(
                                    f"[confirm] synthetic_id={synthetic_id} needSlider=0 "
                                    f"activityId={outer_activity_id!r} syntheticNum={max_times}",
                                    flush=True,
                                )
                                confirm_result = client.confirm_synthesis(confirm_path, confirm_body)
                                entry["confirm"] = confirm_result
                                confirmed = is_success(confirm_result)
                                entry["code"] = 0 if confirmed else confirm_result.get("code", 1)

                        cycle_entries.append(entry)

                        if confirmed:
                            cycle_progress = True
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
                            successful_state_by_synthetic_id[str(synthetic_id)] = material_state_signature
                            successful_submits.append(
                                {
                                    "cycle": cycle_no,
                                    "synthetic_id": synthetic_id,
                                    "times": max_times,
                                    "attempt_count": submit_outcome["attempt_count"],
                                    "submit_concurrency": submit_outcome["concurrency"],
                                    "submit": submit_result,
                                    **({"confirm": entry["confirm"]} if "confirm" in entry else {}),
                                }
                            )

                    cycles.append({"cycle": cycle_no, "entries": cycle_entries})

                    if parsed.dry_run:
                        break
                    if remaining_target_count is not None and remaining_target_count <= 0:
                        break
                    if not any_craftable:
                        print(f"[cycle {cycle_no}] No craftable recipes left, synthesis complete.", flush=True)
                        break
                    if cycle_progress:
                        continue
                    print(
                        f"[cycle {cycle_no}] Craftable items remain but submit did not succeed, "
                        f"retry in {parsed.loop_interval:.1f}s…",
                        flush=True,
                    )
                    time.sleep(max(parsed.loop_interval, 0.1))

                any_discovered_craftable = any(
                    (entry.get("plan") or {}).get("max_times", 0) > 0
                    for cycle_info in cycles
                    for entry in cycle_info["entries"]
                )
                return {
                    "code": 0 if parsed.dry_run or successful_submits or not any_discovered_craftable else 1,
                    "activity_list": last_activity_list_result,
                    "activity_details": last_activity_details,
                    "synthetic_ids": last_synthetic_ids,
                    "cycles": cycles,
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
            path = render_command_path(
                config,
                "market-buy",
                "/order-create-service/batch-purchase-consignment-orders?uid={uid}",
                uid=uid,
            )
            collection_name = (parsed.collection_name or "").strip()
            extra_payload = parse_payload_arg(parsed.payload) or {}
            if collection_name and parsed.price is not None:
                group_id = resolve_group_id_for_market(client, collection_name, config)
                if isinstance(group_id, dict):
                    operation = lambda: group_id
                else:
                    payment_platform = (
                        parsed.payment_platform
                        if parsed.payment_platform is not None
                        else int(get_command_default(config, "market-buy", "payment_platform_code", "30"))
                    )
                    payload = build_market_buy_payload(
                        group_id,
                        parsed.price,
                        parsed.quantity,
                        payment_platform,
                        extra=extra_payload,
                    )
                    print(
                        f"[market-buy] group_id={group_id} maxSinglePrice={payload['maxSinglePrice']}fen "
                        f"maxCount={payload['maxCount']}",
                        flush=True,
                    )
                    operation = lambda: client.post(path, payload)
            else:
                payload = extra_payload
                if not payload:
                    raise SystemExit(
                        "Error: market-buy requires --collection-name + --price, or --payload"
                    )
                operation = lambda: client.post(path, payload)
        elif cmd == "consign-create":
            consign_path = render_command_path(
                config,
                "consign-create",
                "/order-create-service/consignment-orders",
            )
            extra_payload = parse_payload_arg(parsed.payload) or {}
            payment_platform = (
                parsed.payment_platform
                if parsed.payment_platform is not None
                else int(
                    get_command_default(config, "consign-create", "payment_platform_code", "30")
                )
            )

            def consign_create_operation():
                group_id = (parsed.group_id or "").strip()
                collection_name = (parsed.collection_name or "").strip()
                display_name = collection_name or group_id
                if not group_id and not collection_name:
                    return {
                        "code": 1,
                        "error": "either --藏品名字 or --group-id is required",
                    }
                if not group_id and collection_name:
                    resolved_id, resolved_display, auth_fail = resolve_group_id_for_consign(
                        client, collection_name
                    )
                    if isinstance(auth_fail, dict) and auth_fail:
                        return auth_fail
                    group_id = resolved_id or ""
                    if resolved_display:
                        display_name = resolved_display
                if not group_id:
                    owned_groups = fetch_owned_collection_groups(client)
                    hint = ""
                    if isinstance(owned_groups, list):
                        suggestions = suggest_owned_collection_names(owned_groups, collection_name)
                        if suggestions:
                            hint = " similar owned collections: " + " | ".join(suggestions)
                        elif owned_groups:
                            sample = [
                                collection_group_display_name(g)
                                for g in owned_groups[:10]
                                if collection_group_display_name(g)
                            ]
                            if sample:
                                hint = (
                                    f" (fetched {len(owned_groups)} owned groups; "
                                    f"examples: {' | '.join(sample)})"
                                )
                        else:
                            hint = " (owned collection group list is empty)"
                        print(
                            f"[consign-create] match key={normalize_collection_name(collection_name)!r} "
                            f"(ignores punctuation/parentheses; keeps Ⅰ-Ⅹ)",
                            flush=True,
                        )
                    return {
                        "code": 1,
                        "error": f"no owned collection group matching {collection_name!r}{hint}",
                        "matchKey": normalize_collection_name(collection_name),
                    }

                qty = max(1, int(parsed.quantity))
                item_ids = list_unlocked_digital_collection_ids(client, group_id, limit=qty)
                if item_ids is None:
                    return {
                        "code": 1,
                        "error": f"failed to list unlocked collections for group {group_id}",
                    }
                if len(item_ids) < qty:
                    return {
                        "code": 1,
                        "error": (
                            f"need {qty} unlocked item(s) to consign, "
                            f"only {len(item_ids)} available in group {group_id}"
                        ),
                        "group_id": group_id,
                        "available": len(item_ids),
                    }

                print(
                    f"[consign-create] group_id={group_id} quantity={qty} "
                    f"price={parsed.price}yuan paymentPlatformCodes=[{payment_platform}]",
                    flush=True,
                )
                results: list[dict] = []
                success_count = 0
                fail_count = 0
                for index, digital_collection_id in enumerate(item_ids[:qty], start=1):
                    payload = build_consign_create_payload(
                        digital_collection_id,
                        parsed.price,
                        parsed.consignment_password,
                        payment_platform_codes=[payment_platform],
                        extra=extra_payload,
                    )
                    print(
                        f"[consign-create] posting {index}/{qty} "
                        f"digitalCollectionId={digital_collection_id}",
                        flush=True,
                    )
                    result = client.post(consign_path, payload)
                    ok = is_success(result)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                        print(
                            f"[consign-create] failed digitalCollectionId={digital_collection_id}: "
                            f"code={result.get('code')} message={result.get('message')}",
                            flush=True,
                        )
                    results.append(
                        {
                            "digitalCollectionId": digital_collection_id,
                            "ok": ok,
                            "result": result,
                        }
                    )

                summary = (
                    f"{display_name}藏品寄售已完成，寄售个数：{success_count}，失败个数：{fail_count}"
                )
                print(summary, flush=True)
                return {
                    "code": 0 if fail_count == 0 else 1,
                    "message": summary,
                    "summary": summary,
                    "collectionName": display_name,
                    "successCount": success_count,
                    "failCount": fail_count,
                    "group_id": group_id,
                    "results": results,
                }

            operation = consign_create_operation
        elif cmd == "consign-cancel":
            cancel_password_payload = build_consign_password_fields(parsed.consignment_password)

            def consign_cancel_operation():
                print("准备开始下架", flush=True)
                group_id = (parsed.group_id or "").strip()
                collection_name = (parsed.collection_name or "").strip()
                display_name = collection_name or group_id
                if not group_id and collection_name:
                    resolved_id, resolved_display, auth_fail = resolve_group_id_for_consign(
                        client, collection_name
                    )
                    if isinstance(auth_fail, dict) and auth_fail:
                        return auth_fail
                    group_id = resolved_id or ""
                    if resolved_display:
                        display_name = resolved_display
                if not group_id:
                    owned_groups = fetch_owned_collection_groups(client)
                    hint = ""
                    if isinstance(owned_groups, list):
                        suggestions = suggest_owned_collection_names(owned_groups, collection_name)
                        if suggestions:
                            hint = " similar owned collections: " + " | ".join(suggestions)
                    return {
                        "code": 1,
                        "error": f"no owned collection group matching {collection_name!r}{hint}",
                        "matchKey": normalize_collection_name(collection_name),
                    }

                cancel_qty = max(1, int(parsed.quantity))
                cancel_price = parsed.price
                listed = list_consigned_orders_for_cancel(
                    client,
                    group_id,
                    price_yuan=cancel_price,
                    limit=cancel_qty,
                )
                if listed is None:
                    return {
                        "code": 1,
                        "error": f"failed to list consigned items for group {group_id}",
                    }
                consigned, total_matched = listed
                if not consigned:
                    hint = f" (group_id={group_id})"
                    if cancel_price is not None:
                        hint += f", no listings at {cancel_price} yuan"
                    if total_matched > 0:
                        hint += f", matched {total_matched} but quantity filter left none"
                    return {
                        "code": 1,
                        "error": f"no active consignment listings for {display_name!r}{hint}",
                        "group_id": group_id,
                        "matched": total_matched,
                    }

                print(
                    f"[consign-cancel] group_id={group_id} to cancel={len(consigned)} "
                    f"(price={cancel_price}yuan, quantity={cancel_qty}"
                    + (f", matched={total_matched}" if total_matched != len(consigned) else "")
                    + ")",
                    flush=True,
                )
                results: list[dict] = []
                success_count = 0
                fail_count = 0
                for index, entry in enumerate(consigned, start=1):
                    consign_order_id = entry["consign_order_id"]
                    cancel_path = render_command_path(
                        config,
                        "consign-cancel",
                        "/order-service/consign-orders/{consign_order_id}/cancel",
                        consign_order_id=consign_order_id,
                    )
                    print(
                        f"[consign-cancel] cancel {index}/{len(consigned)} "
                        f"consignOrderId={consign_order_id}",
                        flush=True,
                    )
                    result = client.post(cancel_path, cancel_password_payload)
                    ok = is_success(result)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                        print(
                            f"[consign-cancel] failed consignOrderId={consign_order_id}: "
                            f"code={result.get('code')} message={result.get('message')}",
                            flush=True,
                        )
                    results.append({"consign_order_id": consign_order_id, "ok": ok, "result": result})

                summary = (
                    f"{display_name}藏品下架已完成，下架个数：{success_count}，失败个数：{fail_count}"
                )
                print(summary, flush=True)
                return {
                    "code": 0 if fail_count == 0 else 1,
                    "message": summary,
                    "summary": summary,
                    "collectionName": display_name,
                    "successCount": success_count,
                    "failCount": fail_count,
                    "group_id": group_id,
                    "results": results,
                }

            operation = consign_cancel_operation
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
