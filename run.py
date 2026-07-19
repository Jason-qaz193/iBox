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
    parser.add_argument("--adb-host", help="Wireless adb host (cloud/Tailscale/frp)")
    parser.add_argument("--adb-port", type=int, default=5555, help="Wireless adb port (default 5555)")
    parser.add_argument("--adb-serial", help="adb device serial (skip adb connect)")

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

    bridge_check_parser = subparsers.add_parser(
        "bridge-check",
        help="Check RPC (27042) and wireless adb connectivity for cloud/hybrid deploy",
    )
    bridge_check_parser.set_defaults()

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
        help="Scan synthesis recipes and auto-submit craftable ones (optionally filter by activity)",
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
        help="Seconds between scans while craftable activities exist (default: 2)",
    )
    synthesis_auto.add_argument(
        "--idle-interval",
        type=float,
        default=30.0,
        help="Seconds to wait when nothing is craftable and no activity is opening soon (default: 30)",
    )
    synthesis_auto.add_argument(
        "--far-interval",
        type=float,
        default=90.0,
        help="Max seconds between scans when the next activity is still far away (default: 90)",
    )
    synthesis_auto.add_argument(
        "--once",
        action="store_true",
        help="Exit after target count or no craftable recipes (default: keep running until stopped)",
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
        "--synthetic-id",
        dest="synthetic_id_filter",
        default="",
        help="Only auto-submit this syntheticActivityId (e.g. 13993)",
    )
    synthesis_auto.add_argument(
        "--activity-name",
        dest="activity_name_filter",
        default="",
        help="Only auto-submit recipes whose activity/name matches this keyword",
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

    market_buy = subparsers.add_parser(
        "market-buy",
        help="Scan market and buy consignment listings at or below max unit price",
    )
    add_auth_args(market_buy)
    market_buy.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称")
    market_buy.add_argument("--price", dest="price", type=float, default=None, help="最高单价（元）")
    market_buy.add_argument("--quantity", dest="quantity", type=positive_int, default=1, help="购买数量 (default: 1)")
    market_buy.add_argument(
        "--支付密码",
        dest="consignment_password",
        default="",
        help="支付密码",
    )
    market_buy.add_argument(
        "--payment-platform",
        dest="payment_platform",
        type=int,
        default=None,
        help="支付平台代码 (default: 30)",
    )
    market_buy.add_argument(
        "--list-pages",
        dest="list_pages",
        type=positive_int,
        default=10,
        help="每轮扫描市场挂单的最大页数 (default: 10)",
    )
    market_buy.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=None,
        help="无符合条件挂单时的重试间隔秒数 (default: 2)",
    )
    add_payload_arg(market_buy, required=False)

    market_purchase = subparsers.add_parser(
        "market-purchase",
        help="Buy consignment listings at an exact price (optionally from a specific seller)",
    )
    add_auth_args(market_purchase)
    market_purchase.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称")
    market_purchase.add_argument("--price", dest="price", type=float, required=True, help="购买单价（元，精确匹配）")
    market_purchase.add_argument(
        "--quantity",
        dest="quantity",
        type=positive_int,
        default=1,
        help="购买数量 (default: 1)",
    )
    market_purchase.add_argument(
        "--支付密码",
        dest="consignment_password",
        default="",
        help="支付密码",
    )
    market_purchase.add_argument(
        "--seller-uid",
        dest="seller_uid",
        default="",
        help="卖家 userId/uid（点对点购买时指定 B 用户）",
    )
    market_purchase.add_argument(
        "--consign-order-id",
        dest="consign_order_id",
        default="",
        help="指定寄售单 ID（orderId|藏品ID；多个用 、 或逗号分隔，批量直购）",
    )
    market_purchase.add_argument(
        "--digital-collection-id",
        dest="digital_collection_id",
        default="",
        help="指定藏品 digitalCollectionId（直购必填，可与寄售单 ID 一起传）",
    )
    market_purchase.add_argument(
        "--list-pages",
        dest="list_pages",
        type=positive_int,
        default=10,
        help="扫描市场挂单的最大页数 (default: 10)",
    )
    market_purchase.add_argument(
        "--payment-platform",
        dest="payment_platform",
        type=int,
        default=None,
        help="支付平台代码 (default: 30)",
    )
    add_payload_arg(market_purchase, required=False)

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
            "Sell to market buy orders (求购成交). "
            "Scan purchase orders at or above --min-price until --quantity deals complete."
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
    wanted_deal.add_argument(
        "--po-max-pages",
        dest="po_max_pages",
        type=int,
        default=5,
        help="Max pages to scan per group when listing purchase orders (default: 5)",
    )
    wanted_deal.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=None,
        help="轮询间隔（秒）：无符合条件求购单或本轮未成交时等待后再扫描（默认 10）",
    )
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
    wanted_buy.add_argument("--payment-platform", dest="payment_platform", type=int, default=30, help="支付平台代码 (default: 30，汇付钱包)")
    wanted_buy.add_argument(
        "--支付密码",
        dest="consignment_password",
        default="",
        help="寄售/支付密码",
    )
    wanted_buy.add_argument("--consignment-password", dest="consignment_password", default="", help="寄售密码（同 --支付密码）")
    wanted_buy.add_argument("--dry-run", action="store_true", help="Print resolved group_id without placing order")
    add_payload_arg(wanted_buy, required=False)

    sale_rush = subparsers.add_parser(
        "sale-rush",
        help="First-sale / priority purchase (首发抢购): POST /order-create-service/sales/{sale_id}/orders",
    )
    add_auth_args(sale_rush)
    sale_rush.add_argument("--sale-id", dest="sale_id", default="", help="首发活动 ID（如 369）")
    sale_rush.add_argument("--group-id", dest="group_id", default="", help="藏品分组 ID")
    sale_rush.add_argument("--collection-name", dest="collection_name", default="", help="藏品名称（自动查 group 与 sale-info；省略时用 --auto）")
    sale_rush.add_argument(
        "--auto",
        action="store_true",
        help="自动匹配首页首发抢购活动（仅扫描 home/new-products 与 sale-infos 第 1 页，抢购所有 saleStatus=1）",
    )
    sale_rush.add_argument(
        "--quantity",
        dest="quantity",
        type=sale_rush_quantity,
        default=0,
        help="购买数量；0 表示使用该活动允许的最大购买数 userOnceMaxBuyNum（默认 0）",
    )
    sale_rush.add_argument(
        "--payment-platform",
        dest="payment_platform",
        type=int,
        default=None,
        help="支付平台代码 (default: 30)",
    )
    sale_rush.add_argument(
        "--支付密码",
        dest="consignment_password",
        default="",
        help="支付密码（钱包扣款）",
    )
    sale_rush.add_argument(
        "--consignment-password",
        dest="consignment_password",
        default="",
        help="支付密码（同 --支付密码）",
    )
    sale_rush.add_argument(
        "--no-wait",
        dest="wait_for_start",
        action="store_false",
        help="不等待 onSaleTime，立即尝试下单",
    )
    sale_rush.set_defaults(wait_for_start=True)
    sale_rush.add_argument(
        "--retry-window",
        type=float,
        default=30.0,
        help="开售后创建订单的重试窗口（秒，default: 30）",
    )
    sale_rush.add_argument(
        "--retry-interval",
        type=float,
        default=0.2,
        help="创建订单失败后的重试间隔（秒，default: 0.2）",
    )
    sale_rush.add_argument(
        "--captcha-mode",
        choices=["app", "auto", "playwright", "manual", "skip"],
        default="app",
        help="验证码：首发抢购仅使用 App 内 WebView 原生验证码(RPC 捕获)；"
        "auto/playwright 等同 app，不会弹出浏览器",
    )
    sale_rush.add_argument(
        "--captcha-timeout",
        type=float,
        default=120.0,
        help="等待验证码的最长时间（秒）",
    )
    sale_rush.add_argument(
        "--captcha-id",
        default="0d4b08eac1cbdcad36bbf607c5bf3e1b",
        help="GeeTest captcha_id",
    )
    sale_rush.add_argument(
        "--captcha-headed",
        dest="captcha_headed",
        action="store_true",
        help="Playwright 解验证码时显示浏览器窗口（语序点选成功率更高）",
    )
    sale_rush.add_argument(
        "--captcha-headless",
        dest="captcha_headed",
        action="store_false",
        help="Playwright 使用无头模式（语序点选可能失败）",
    )
    sale_rush.set_defaults(captcha_headed=True)
    sale_rush.add_argument("--dry-run", action="store_true", help="仅解析活动信息，不下单")
    add_payload_arg(sale_rush, required=False)

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


def sale_rush_quantity(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def resolve_sale_rush_buy_quantity(requested: int, max_buy: int | None) -> int:
    """0 requested means use activity max (userOnceMaxBuyNum)."""
    if requested > 0:
        if max_buy is not None and max_buy > 0:
            return min(requested, int(max_buy))
        return requested
    if max_buy is not None and max_buy > 0:
        return int(max_buy)
    return 1


_OWNED_COLLECTION_ID_KEYS = (
    "id",
    "digitalCollectionId",
    "collectionId",
    "digitalCollectionID",
    "collectionID",
)


_COLLECTION_NAME_KEEP_CHARS = (
    r"\u4E00-\u9FFF"  # CJK Unified Ideographs
    r"\u3400-\u4DBF"  # CJK Extension A
    r"\uF900-\uFAFF"  # CJK Compatibility Ideographs
    r"A-Za-z0-9"
    r"\u2160-\u217F"  # Ⅰ Ⅱ Ⅲ … roman numerals
    r"\uFF10-\uFF19"  # fullwidth digits
)
_FULLWIDTH_DIGIT_MAP = str.maketrans("０１２３４５６７８９", "0123456789")
_UNICODE_ROMAN_CHARS = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹⅺⅻ"
_ASCII_ROMAN_NUMERALS = (
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "xi",
    "xii",
)
_UNICODE_ROMAN_TO_ASCII = dict(zip(_UNICODE_ROMAN_CHARS, _ASCII_ROMAN_NUMERALS * 2))
_TRAILING_ROMAN_SUFFIX_RE = re.compile(r"(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)$")


def trailing_roman_suffix(normalized_name: str) -> str | None:
    match = _TRAILING_ROMAN_SUFFIX_RE.search(normalized_name or "")
    return match.group(1) if match else None


def report_task_progress(done: int, total: int) -> None:
    """Emit a machine-readable progress line for QQ bot / log consumers."""
    print(f"[ibox-progress] {int(done)}/{int(total)}", flush=True)


def normalize_collection_name(name: str | None) -> str:
    """Strip punctuation/symbols; keep Chinese, English, digits and roman numerals (ⅠⅡⅢ)."""
    if name is None:
        return ""
    normalized = str(name).strip()
    normalized = re.sub(rf"[^{_COLLECTION_NAME_KEEP_CHARS}]+", "", normalized)
    normalized = normalized.translate(_FULLWIDTH_DIGIT_MAP)
    normalized = normalized.lower()
    for uchar, ascii_roman in _UNICODE_ROMAN_TO_ASCII.items():
        normalized = normalized.replace(uchar, ascii_roman)
    return normalized


def collection_name_matches(query: str, candidate: str) -> bool:
    normalized_query = normalize_collection_name(query)
    normalized_candidate = normalize_collection_name(candidate)
    if normalized_query and normalized_candidate:
        query_roman = trailing_roman_suffix(normalized_query)
        candidate_roman = trailing_roman_suffix(normalized_candidate)
        if query_roman and query_roman != candidate_roman:
            return False
        if candidate_roman and not query_roman and normalized_query != normalized_candidate:
            return False
        return (
            normalized_query in normalized_candidate
            or normalized_candidate in normalized_query
        )
    raw_query = str(query or "").strip()
    raw_candidate = str(candidate or "").strip()
    if not raw_query:
        return False
    return raw_query in raw_candidate


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
            if isinstance(g, dict) and collection_name_matches(collection_name, collection_group_display_name(g))
        ]

    matched: list[dict] = []
    for group in groups_list:
        if not isinstance(group, dict):
            continue
        display_name = collection_group_display_name(group)
        if collection_name_matches(collection_name, display_name):
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
                if not collection_name_matches(collection_name, str(name)):
                    continue
            elif not collection_name_matches(collection_name, str(name)):
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
        owned_items, list_err = list_unlocked_digital_collection_ids(client, public, limit=1)
        if owned_items:
            print(
                f"[consign-create] resolved via public market (owned items found): group_id={public}",
                flush=True,
            )
            return public, collection_name, None
        if isinstance(list_err, dict):
            code = list_err.get("code")
            msg = list_err.get("message") or list_err.get("error")
            detail = f" (API code={code} message={msg})" if code not in (None, "") or msg else ""
            return "", "", {
                "code": 1,
                "error": (
                    f"found {collection_name!r} on market (group_id={public}) "
                    f"but failed to list your unlocked items{detail}"
                ),
                "group_id": public,
            }
        return "", "", {
            "code": 1,
            "error": (
                f"found {collection_name!r} on market (group_id={public}) "
                f"but you do not own any unlocked items to consign"
            ),
            "group_id": public,
        }

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


def market_listing_price_fen(item: dict) -> int | None:
    return to_price_fen(
        first_present(item, ("price", "salePrice", "consignmentPrice", "singlePrice", "maxSinglePrice"))
    )


def market_listing_seller_uid(item: dict) -> str:
    return str(
        first_present(
            item,
            ("userId", "sellerUserId", "sellerId", "ownerUserId", "uid", "sellerUid", "publishUserId"),
        )
        or ""
    ).strip()


def extract_consign_order_id_from_result(result: dict | None) -> str | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        value = first_present(
            data,
            ("consignOrderId", "consignmentOrderId", "consignmentOrderNo", "orderId", "id"),
        )
        if value not in (None, ""):
            return str(value)
    value = first_present(
        result,
        ("consignOrderId", "consignmentOrderId", "consignmentOrderNo", "orderId", "id"),
    )
    if value not in (None, ""):
        return str(value)
    return None


def owned_item_digital_collection_id(item: dict) -> str:
    value = first_present(
        item,
        ("digitalCollectionId", "collectionId", "digitalCollectionID", "collectionID"),
    )
    if value not in (None, ""):
        return str(value).strip()
    digital_collection = item.get("digitalCollection")
    if isinstance(digital_collection, dict):
        nested = digital_collection.get("id")
        if nested not in (None, ""):
            return str(nested).strip()
    return ""


def extract_consign_item_ids(item: dict) -> dict:
    digital_collection_id = owned_item_digital_collection_id(item)
    order_uuid = str(first_present(item, ("orderId", "orderUuid")) or "").strip()
    numeric_market_id = ""
    for key in ("consignmentOrderId", "consignOrderId", "marketOrderId"):
        value = item.get(key)
        if value not in (None, "") and str(value).lstrip("-").isdigit():
            numeric_market_id = str(value).strip()
            break
    raw_id = item.get("id")
    if (
        numeric_market_id == ""
        and raw_id not in (None, "")
        and str(raw_id).lstrip("-").isdigit()
        and str(raw_id) != digital_collection_id
    ):
        numeric_market_id = str(raw_id).strip()
    cancel_id = str(
        first_present(item, ("orderId", "consignOrderId", "consignmentOrderId", "consignmentOrderNo"))
        or ""
    ).strip()
    display_id = numeric_market_id or order_uuid or cancel_id
    purchase_id = numeric_market_id or order_uuid or cancel_id
    return {
        "digital_collection_id": digital_collection_id,
        "order_uuid": order_uuid,
        "market_listing_id": numeric_market_id,
        "cancel_id": cancel_id,
        "consign_order_id": cancel_id or order_uuid or numeric_market_id,
        "display_id": display_id,
        "purchase_id": purchase_id,
    }


def warm_market_group_context(
    client,
    group_id: str,
    *,
    headers: dict | None = None,
) -> dict | None:
    path = (
        f"/public-market-service/digital-collection-groups/{group_id}"
        "/purchase-consignment-info?configType=0"
    )
    result = client.get(path, headers=headers)
    if not is_success(result):
        code = result.get("code") if isinstance(result, dict) else result
        message = result.get("message") if isinstance(result, dict) else ""
        print(
            f"[market-list] warmup failed group_id={group_id} code={code} message={message}",
            flush=True,
        )
        return result if isinstance(result, dict) else {"code": code, "message": message}
    return None


def resolve_seller_consign_entry_after_create(
    client,
    group_id: str,
    digital_collection_id: str,
    price_yuan: float,
    create_order_id: str = "",
) -> dict | None:
    listed = list_consigned_orders_for_cancel(
        client,
        group_id,
        price_yuan=price_yuan,
        limit=30,
    )
    if not listed:
        return None
    orders, _ = listed
    needle = str(create_order_id or "").strip()
    for order in orders:
        if str(order.get("digital_collection_id") or "") == str(digital_collection_id):
            return order
        if needle and needle in {
            str(order.get("order_uuid") or ""),
            str(order.get("consign_order_id") or ""),
            str(order.get("market_listing_id") or ""),
            str(order.get("display_id") or ""),
            str(order.get("purchase_id") or ""),
        }:
            return order
    return None


def resolve_consign_order_id_after_create(
    client,
    group_id: str,
    digital_collection_id: str,
    price_yuan: float,
) -> str | None:
    entry = resolve_seller_consign_entry_after_create(
        client,
        group_id,
        digital_collection_id,
        price_yuan,
    )
    if not entry:
        return None
    return str(entry.get("display_id") or entry.get("consign_order_id") or "") or None


def market_listing_identifiers(item: dict) -> set[str]:
    ids: set[str] = set()
    for key in (
        "id",
        "orderId",
        "orderUuid",
        "consignOrderId",
        "consignmentOrderId",
        "consignmentOrderNo",
    ):
        value = item.get(key)
        if value not in (None, ""):
            ids.add(str(value).strip())
    return ids


def listing_matches_consign_order_id(item: dict, consign_order_id: str) -> bool:
    needle = str(consign_order_id or "").strip()
    if not needle:
        return True
    return needle in market_listing_identifiers(item)


def market_listing_purchase_id(item: dict) -> str:
    numeric_id = item.get("id")
    if numeric_id not in (None, "") and str(numeric_id).lstrip("-").isdigit():
        return str(numeric_id).strip()
    return str(
        first_present(
            item,
            ("orderUuid", "orderId", "consignOrderId", "consignmentOrderId", "consignmentOrderNo", "id"),
        )
        or ""
    ).strip()


def resolve_market_listing_id_after_create(
    client,
    config: dict,
    group_id: str,
    list_uid: str,
    *,
    create_order_id: str = "",
    digital_collection_id: str = "",
    price_yuan: float | None = None,
    retries: int = 8,
    delay_sec: float = 2.0,
) -> str | None:
    target_fen = to_price_fen(price_yuan) if price_yuan is not None else None
    for attempt in range(max(1, retries)):
        if attempt > 0 and delay_sec > 0:
            time.sleep(delay_sec)
        listings, _ = fetch_group_market_listings(
            client,
            config,
            group_id,
            list_uid,
            max_pages=5,
        )
        scanned = len(listings or [])
        for item in listings or []:
            if create_order_id and listing_matches_consign_order_id(item, create_order_id):
                purchase_id = market_listing_purchase_id(item)
                if purchase_id:
                    print(
                        f"[consign-create] resolved market listing id={purchase_id} "
                        f"via createOrderId={create_order_id} (attempt {attempt + 1}, scanned={scanned})",
                        flush=True,
                    )
                    return purchase_id
            item_dc = market_listing_digital_collection_id(item)
            if digital_collection_id and item_dc == str(digital_collection_id):
                if target_fen is not None:
                    item_fen = market_listing_price_fen(item)
                    if item_fen != target_fen:
                        continue
                purchase_id = market_listing_purchase_id(item)
                if purchase_id:
                    print(
                        f"[consign-create] resolved market listing id={purchase_id} "
                        f"via digitalCollectionId={digital_collection_id} (attempt {attempt + 1}, scanned={scanned})",
                        flush=True,
                    )
                    return purchase_id
        print(
            f"[consign-create] market listing not visible yet "
            f"(attempt {attempt + 1}/{retries}, scanned={scanned}, createOrderId={create_order_id or '-'})",
            flush=True,
        )
    return None


def market_listing_order_id(item: dict) -> str:
    return market_listing_purchase_id(item)


def market_listing_digital_collection_id(item: dict) -> str:
    value = first_present(
        item,
        ("digitalCollectionId", "collectionId", "digitalCollectionID", "collectionID"),
    )
    if value not in (None, ""):
        return str(value).strip()
    digital_collection = item.get("digitalCollection")
    if isinstance(digital_collection, dict):
        nested = digital_collection.get("id")
        if nested not in (None, ""):
            return str(nested).strip()
    return ""


def fetch_group_market_listings(
    client,
    config: dict,
    group_id: str,
    buyer_uid: str,
    *,
    page_size: int = 50,
    max_pages: int = 10,
    use_webview_headers: bool = True,
    app_version: str = "2.3.2",
) -> tuple[list[dict], dict | None]:
    listings: list[dict] = []
    headers = None
    if use_webview_headers:
        headers = build_webview_api_headers(
            getattr(client, "token", None),
            app_version=app_version,
        )
        warm_err = warm_market_group_context(client, group_id, headers=headers)
        if warm_err and is_auth_failure(warm_err):
            return None, warm_err
    for page_no in range(1, max(1, int(max_pages)) + 1):
        path = render_command_path(
            config,
            "market-list",
            (
                "/public-market-service/digital-collection-groups/{group_id}"
                "/consignment-orders?pageNo={page_no}&pageSize={page_size}"
                "&sortType={sort_type}&sortField={sort_field}&uid={uid}"
            ),
            group_id=group_id,
            page_no=str(page_no),
            page_size=str(page_size),
            sort_type=get_command_default(config, "market-list", "sort_type", "1"),
            sort_field=get_command_default(config, "market-list", "sort_field", "1"),
            uid=buyer_uid,
        )
        result, api_error = client_get_with_rate_limit_retry(
            client,
            path,
            label=f"market-list group_id={group_id} page={page_no}",
            headers=headers,
        )
        if result is None:
            if api_error and is_auth_failure(api_error):
                return None, api_error
            return listings if listings else None, api_error
        if page_no == 1 and isinstance(result, dict) and is_auth_failure(result):
            return None, result
        items = extract_list_payload(result)
        if isinstance(items, list):
            listings.extend(item for item in items if isinstance(item, dict))
        elif isinstance(result, dict) and page_no == 1:
            print(
                f"[market-list] empty/unparsed list group_id={group_id} "
                f"code={result.get('code')} message={result.get('message')}",
                flush=True,
            )
        if not isinstance(items, list) or not _list_page_has_more(
            result,
            page_no=page_no,
            page_size=page_size,
            items_count=len(items),
        ):
            break
        if page_no < max_pages and LIST_OWNED_PAGE_INTERVAL_SEC > 0:
            time.sleep(LIST_OWNED_PAGE_INTERVAL_SEC)
    if not listings:
        print(
            f"[market-list] no listings returned group_id={group_id} uid={buyer_uid}",
            flush=True,
        )
    return listings, None


def market_listing_within_price_limit(item: dict, max_price_fen: int) -> tuple[bool, int | None]:
    """Return (ok, price_fen) when listing unit price is at or below the instruction limit."""
    item_fen = market_listing_price_fen(item)
    if item_fen is None:
        return False, None
    return item_fen <= max_price_fen, item_fen


def select_market_listings(
    listings: list[dict],
    *,
    price_fen: int,
    quantity: int,
    seller_uid: str = "",
    consign_order_id: str = "",
    exact_price: bool = True,
    exclude_ids: set[str] | frozenset[str] | None = None,
    sort_cheapest: bool = False,
) -> list[dict]:
    selected: list[dict] = []
    want = max(1, int(quantity))
    seller_uid = str(seller_uid or "").strip()
    consign_order_id = str(consign_order_id or "").strip()
    skip_ids = exclude_ids or set()
    for item in listings:
        purchase_id = market_listing_purchase_id(item)
        if not purchase_id or purchase_id in skip_ids:
            continue
        if consign_order_id and not listing_matches_consign_order_id(item, consign_order_id):
            continue
        ok_price, item_fen = market_listing_within_price_limit(item, price_fen)
        if item_fen is None:
            continue
        if exact_price and item_fen != price_fen:
            continue
        if not exact_price and not ok_price:
            continue
        if seller_uid and market_listing_seller_uid(item) != seller_uid:
            continue
        selected.append(
            {
                "consign_order_id": purchase_id,
                "digital_collection_id": market_listing_digital_collection_id(item),
                "seller_uid": market_listing_seller_uid(item),
                "price_fen": item_fen,
                "raw": item,
            }
        )
        if len(selected) >= want:
            break
    if sort_cheapest:
        selected.sort(key=lambda entry: entry["price_fen"])
    return selected


def split_consign_purchase_ref(consign_order_id: str, digital_collection_id: str = "") -> tuple[str, str]:
    order_id = str(consign_order_id or "").strip()
    dc_id = str(digital_collection_id or "").strip()
    if "|" in order_id:
        left, right = order_id.split("|", 1)
        order_id = left.strip()
        if not dc_id:
            dc_id = right.strip()
    return order_id, dc_id


def parse_consign_purchase_ref_list(
    consign_order_id: str,
    digital_collection_id: str = "",
) -> list[tuple[str, str]]:
    """Parse one or more orderId|digitalCollectionId pairs (、, ; or newline separated)."""
    raw = str(consign_order_id or "").strip()
    shared_dc = str(digital_collection_id or "").strip()
    if not raw and not shared_dc:
        return []
    chunks = [part.strip() for part in re.split(r"[、,;\n]+", raw) if part.strip()] if raw else []
    refs: list[tuple[str, str]] = []
    for index, chunk in enumerate(chunks):
        dc_hint = shared_dc if len(chunks) == 1 else ""
        order_id, dc_id = split_consign_purchase_ref(chunk, dc_hint)
        if order_id:
            refs.append((order_id, dc_id))
    if not refs and raw:
        order_id, dc_id = split_consign_purchase_ref(raw, shared_dc)
        if order_id:
            refs.append((order_id, dc_id or shared_dc))
    return refs


def build_market_purchase_payload(
    consign_order_id: str,
    payment_platform: int,
    consignment_password: str,
    *,
    digital_collection_id: str = "",
    extra: dict | None = None,
) -> dict:
    payload: dict = {
        "consignmentOrderId": str(consign_order_id),
        "orderId": str(consign_order_id),
        "paymentPlatformCode": int(payment_platform),
        **build_consign_password_fields(consignment_password),
    }
    if digital_collection_id:
        payload["digitalCollectionId"] = int(digital_collection_id)
    if extra:
        payload.update(extra)
    return payload


def extract_purchase_order_id(create_result: dict | None) -> str:
    if not isinstance(create_result, dict):
        return ""
    data = create_result.get("data")
    if isinstance(data, dict):
        order_id = first_present(data, ("orderId", "orderUUId", "orderUuid", "id"))
        if order_id not in (None, ""):
            return str(order_id)
    nested = first_present(create_result, ("orderId", "orderUUId", "orderUuid", "id"))
    return "" if nested in (None, "") else str(nested)


def fetch_purchase_cashier(
    client,
    purchase_order_id: str,
    *,
    payment_initiator_type: int = 0,
) -> dict:
    path = (
        f"/payment-service/cashiers/gain"
        f"?orderUUId={purchase_order_id}&paymentInitiatorType={int(payment_initiator_type)}"
    )
    return client.get(path)


def complete_purchase_payment(
    client,
    *,
    create_result: dict,
    consignment_password: str,
    ibox_token: str,
    app_version: str,
    max_price_yuan: float | None = None,
    config: dict | None = None,
    payment_initiator_type: int = 0,
    label: str = "market-purchase",
) -> dict:
    from src.hfpay_wallet import (
        parse_wallet_uuid,
        pay_via_wallet_cashier,
        resolve_encrypted_wallet_password,
    )

    purchase_order_id = extract_purchase_order_id(create_result)
    if not purchase_order_id:
        return {
            "paid": False,
            "error": "create succeeded but response missing purchase orderId",
            "create": create_result,
        }

    print(
        f"[{label}] order locked (unpaid) purchaseOrderId={purchase_order_id}",
        flush=True,
    )
    out: dict = {
        "purchase_order_id": purchase_order_id,
        "paid": False,
        "create": create_result,
    }

    cashier = fetch_purchase_cashier(
        client,
        purchase_order_id,
        payment_initiator_type=payment_initiator_type,
    )
    out["cashier"] = cashier
    if not is_success(cashier):
        out["error"] = (
            f"cashier/gain failed: code={cashier.get('code')} message={cashier.get('message')}"
        )
        print(f"[{label}] {out['error']}", flush=True)
        return out

    link = first_present(cashier.get("data") or {}, ("link", "cashierLink", "url"))
    if not link:
        out["error"] = "cashier/gain succeeded but link missing"
        print(f"[{label}] {out['error']}", flush=True)
        return out

    out["cashierLink"] = link
    print(
        f"[{label}] paying via Huifu wallet (same as App cashier)…",
        flush=True,
    )

    wallet_uuid = parse_wallet_uuid(link)
    enc_pwd, pwd_hint = resolve_encrypted_wallet_password(
        config,
        plain_password=consignment_password,
        wallet_uuid=wallet_uuid,
    )
    print(f"[{label}] wallet-pay: {pwd_hint}", flush=True)
    if not enc_pwd:
        out["error"] = pwd_hint
        return out
    if not ibox_token:
        out["error"] = "missing ibox token for wallet session"
        return out

    wallet_result = pay_via_wallet_cashier(
        cashier_link=link,
        ibox_token=str(ibox_token),
        encrypted_password=enc_pwd,
        app_version=app_version,
        max_trans_amt_yuan=max_price_yuan,
    )
    out["wallet_pay"] = wallet_result
    trans_amt = wallet_result.get("trans_amt")
    if trans_amt not in (None, ""):
        print(f"[{label}] cashier trans_amt={trans_amt} yuan", flush=True)

    if wallet_result.get("ok"):
        out["paid"] = True
        print(f"[{label}] wallet payment success", flush=True)
        return out

    out["error"] = wallet_result.get("error") or "wallet payment failed"
    print(f"[{label}] wallet payment failed: {out['error']}", flush=True)
    out["hint"] = (
        "Order is locked but unpaid. Open iBox → 订单中心 → 待支付, "
        f"or pay manually: {link}"
    )
    return out


CONSIGN_POST_INTERVAL_SEC = 0.5
LIST_OWNED_PAGE_INTERVAL_SEC = 0.35
MARKET_BUY_DEFAULT_POLL_INTERVAL_SEC = 2.0


def run_market_buy_sweep(
    *,
    client,
    config: dict,
    uid: str,
    group_id: str,
    collection_name: str,
    target_price_yuan: float,
    target_qty: int,
    consignment_password: str,
    payment_platform: int,
    purchase_path: str,
    extra_payload: dict,
    ibox_token: str,
    app_version: str = "2.3.2",
    list_pages: int = 10,
    poll_interval: float = MARKET_BUY_DEFAULT_POLL_INTERVAL_SEC,
) -> dict:
    """
    Scan public market listings and buy one-by-one until target_qty is reached.
    Only locks and pays when listing unit price <= target_price_yuan (instruction limit).
    Keeps polling when no eligible listings are available.
    """
    target_price_fen = int(round(float(target_price_yuan) * 100))
    remaining = max(1, int(target_qty))
    success_count = 0
    fail_count = 0
    results: list[dict] = []
    tried_ids: set[str] = set()
    poll_interval = max(0.5, float(poll_interval))
    round_no = 0

    print(
        f"[market-buy] sweep group_id={group_id} max_price={target_price_yuan}yuan "
        f"target_qty={target_qty} list_pages={list_pages} poll_interval={poll_interval:g}s",
        flush=True,
    )
    report_task_progress(0, target_qty)

    while remaining > 0:
        round_no += 1
        listings, list_error = fetch_group_market_listings(
            client,
            config,
            group_id,
            uid,
            max_pages=max(1, int(list_pages)),
            app_version=app_version,
        )
        if listings is None:
            listings = []
        if list_error and is_auth_failure(list_error):
            message = str(list_error.get("message") or list_error.get("error") or "登录已失效")
            return {
                "code": 1,
                "error": (
                    f"market-list auth failed: {message}. "
                    "Saved session may have expired — pass SMS code to log in again."
                ),
                "auth_failure": True,
                "group_id": group_id,
            }
        if not listings and list_error:
            detail = ""
            if isinstance(list_error, dict):
                detail = f" code={list_error.get('code')} message={list_error.get('message')}"
            print(
                f"[market-buy] round {round_no}: market list failed{detail}; "
                f"retry in {poll_interval:g}s (remaining={remaining})",
                flush=True,
            )
            time.sleep(poll_interval)
            continue

        candidates = select_market_listings(
            listings,
            price_fen=target_price_fen,
            quantity=remaining,
            exact_price=False,
            exclude_ids=tried_ids,
            sort_cheapest=True,
        )
        if not candidates:
            print(
                f"[market-buy] round {round_no}: no listing <= {target_price_yuan}yuan "
                f"(scanned={len(listings)}, remaining={remaining}); retry in {poll_interval:g}s",
                flush=True,
            )
            time.sleep(poll_interval)
            continue

        entry = candidates[0]
        order_id = entry["consign_order_id"]
        item_fen = int(entry["price_fen"])
        item_yuan = item_fen / 100.0
        tried_ids.add(order_id)

        if item_fen > target_price_fen:
            print(
                f"[market-buy] skip consignOrderId={order_id}: "
                f"price {item_yuan:g}yuan > limit {target_price_yuan:g}yuan",
                flush=True,
            )
            fail_count += 1
            continue

        index = success_count + fail_count + 1
        payload = build_market_purchase_payload(
            order_id,
            payment_platform,
            consignment_password,
            digital_collection_id=entry.get("digital_collection_id") or "",
            extra=extra_payload,
        )
        print(
            f"[market-buy] locking {index} consignOrderId={order_id} "
            f"price={item_yuan:g}yuan (limit {target_price_yuan:g}yuan, remaining={remaining})",
            flush=True,
        )
        create_result = client_post_with_rate_limit_retry(
            client,
            purchase_path,
            payload,
            label=f"market-buy {success_count + 1}/{target_qty}",
        )
        if not is_success(create_result):
            fail_count += 1
            print(
                f"[market-buy] lock failed consignOrderId={order_id}: "
                f"code={create_result.get('code')} message={create_result.get('message')}",
                flush=True,
            )
            results.append(
                {
                    "consign_order_id": order_id,
                    "price_yuan": item_yuan,
                    "ok": False,
                    "paid": False,
                    "result": create_result,
                }
            )
            if CONSIGN_POST_INTERVAL_SEC > 0:
                time.sleep(CONSIGN_POST_INTERVAL_SEC)
            continue

        payment = complete_purchase_payment(
            client,
            create_result=create_result,
            consignment_password=consignment_password,
            ibox_token=ibox_token,
            app_version=app_version,
            max_price_yuan=target_price_yuan,
            config=config,
            label=f"market-buy {success_count + 1}/{target_qty}",
        )
        paid = bool(payment.get("paid"))
        wallet_pay = payment.get("wallet_pay") or {}
        if wallet_pay.get("aborted_before_pay"):
            paid = False
            print(
                f"[market-buy] payment aborted: {wallet_pay.get('error') or payment.get('error')}",
                flush=True,
            )

        purchase_entry = {
            "consign_order_id": order_id,
            "seller_uid": entry.get("seller_uid"),
            "digital_collection_id": entry.get("digital_collection_id"),
            "price_yuan": item_yuan,
            "ok": paid,
            "paid": paid,
            "purchase_order_id": payment.get("purchase_order_id"),
            "cashierLink": payment.get("cashierLink"),
            "create": create_result,
            "payment": payment,
            "result": create_result if paid else payment,
        }
        results.append(purchase_entry)
        if paid:
            success_count += 1
            remaining -= 1
            report_task_progress(success_count, target_qty)
            print(
                f"[market-buy] paid ok; progress {success_count}/{target_qty}",
                flush=True,
            )
        else:
            fail_count += 1
            print(
                f"[market-buy] lock ok but pay failed consignOrderId={order_id}: "
                f"{payment.get('error') or 'wallet payment failed'}",
                flush=True,
            )

        if remaining > 0 and CONSIGN_POST_INTERVAL_SEC > 0:
            time.sleep(CONSIGN_POST_INTERVAL_SEC)

    summary = (
        f"{collection_name}捡漏已完成，购买个数：{success_count}，失败个数：{fail_count}"
    )
    print(summary, flush=True)
    return {
        "code": 0 if success_count >= target_qty else 1,
        "message": summary,
        "summary": summary,
        "collectionName": collection_name,
        "successCount": success_count,
        "failCount": fail_count,
        "targetQty": target_qty,
        "maxPriceYuan": target_price_yuan,
        "group_id": group_id,
        "rounds": round_no,
        "results": results,
    }


def _list_page_has_more(result: dict, *, page_no: int, page_size: int, items_count: int) -> bool:
    if items_count < page_size:
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return items_count >= page_size
    if data.get("hasMore") is False:
        return False
    total = to_int(
        first_present(data, ("total", "totalCount", "totalNum", "count", "recordCount"))
    )
    if total is not None and page_no * page_size >= total:
        return False
    page_count = to_int(first_present(data, ("totalPages", "pageCount", "pages")))
    if page_count is not None and page_no >= page_count:
        return False
    return True


def list_owned_collection_items(
    client,
    group_id: str,
    *,
    lock_status: int | None = None,
    consigned_only: bool = False,
    item_limit: int | None = None,
) -> tuple[list[dict] | None, dict | None]:
    items_out: list[dict] = []
    page_no = 1
    page_size = 50
    max_pages = 100
    if item_limit is not None:
        max_pages = min(max_pages, max(1, (int(item_limit) + page_size - 1) // page_size))
    while True:
        path = (
            f"/personal-center-service/users/digital-collection-groups/{group_id}"
            f"?pageSize={page_size}&pageNo={page_no}"
        )
        if lock_status is not None:
            path += f"&lockStatus={int(lock_status)}"
        result, api_error = client_get_with_rate_limit_retry(
            client,
            path,
            label=f"list-owned-items group_id={group_id} page={page_no}",
        )
        if result is None:
            if isinstance(api_error, dict):
                print(
                    f"[list-owned-items] API failed group_id={group_id} lockStatus={lock_status} "
                    f"code={api_error.get('code')} message={api_error.get('message')}",
                    flush=True,
                )
            return None, api_error if isinstance(api_error, dict) else {"error": str(api_error)}
        items = extract_list_payload(result)
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            if consigned_only and int(item.get("consignmentStatus") or 0) != 1:
                continue
            items_out.append(item)
            if item_limit is not None and len(items_out) >= item_limit:
                return items_out[:item_limit], None
        if not _list_page_has_more(result, page_no=page_no, page_size=page_size, items_count=len(items)):
            break
        page_no += 1
        if page_no > max_pages:
            break
        if LIST_OWNED_PAGE_INTERVAL_SEC > 0:
            time.sleep(LIST_OWNED_PAGE_INTERVAL_SEC)
    return items_out, None


def list_unlocked_digital_collection_ids(
    client,
    group_id: str,
    *,
    limit: int | None = None,
) -> tuple[list[str] | None, dict | None]:
    want = max(1, int(limit)) if limit is not None else None
    items, api_error = list_owned_collection_items(
        client,
        group_id,
        lock_status=0,
        item_limit=want,
    )
    if items is None:
        return None, api_error
    collection_ids: list[str] = []
    for item in items:
        cid = first_present(item, _OWNED_COLLECTION_ID_KEYS)
        if cid not in (None, ""):
            collection_ids.append(str(cid))
            if want is not None and len(collection_ids) >= want:
                return collection_ids[:want], None
    if want is not None:
        return collection_ids[:want], None
    return collection_ids, None


WANTED_DEAL_POST_INTERVAL_SEC = 1.0
WANTED_DEAL_DEFAULT_POLL_INTERVAL_SEC = 10.0
WANTED_DEAL_MAX_CANDIDATES_PER_ROUND = 25
WANTED_DEAL_STALE_ORDER_CODES = frozenset({"3600000", "3600002"})


def wanted_deal_relation_key(purchase_order_id: str, relation_id: str) -> str:
    return f"{purchase_order_id}:{relation_id}"


def is_wanted_deal_stale_order(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("code")) in WANTED_DEAL_STALE_ORDER_CODES


def dedupe_wanted_deal_candidates(candidates: list[dict]) -> list[dict]:
    """One attempt per purchase order per round — avoids stale relation spam."""
    best_by_po: dict[str, dict] = {}
    for candidate in candidates:
        po_id = str(candidate["purchase_order_id"])
        prev = best_by_po.get(po_id)
        if prev is None or (candidate.get("price_fen") or 0) > (prev.get("price_fen") or 0):
            best_by_po[po_id] = candidate
    out = list(best_by_po.values())
    out.sort(key=lambda c: (c["price_fen"] or 0), reverse=True)
    return out


def ensure_wanted_deal_collection_pool(
    client,
    group_id: str,
    *,
    used_ids: set[str],
    min_available: int,
    pools: dict[str, list[str]],
    force_refresh: bool = False,
) -> tuple[bool, dict | None]:
    """Return (pool_ready, error). error is set on auth failure or list API failure."""
    if not force_refresh:
        available = [cid for cid in pools.get(group_id, []) if cid not in used_ids]
        pools[group_id] = available
        if available:
            return True, None
    fetch_limit = max(min_available, 20) + len(used_ids) + 5
    item_ids, list_error = list_unlocked_digital_collection_ids(
        client,
        group_id,
        limit=fetch_limit,
    )
    if item_ids is None:
        detail = ""
        if isinstance(list_error, dict):
            detail = f" (API code={list_error.get('code')} message={list_error.get('message')})"
        print(
            f"[wanted-deal] failed to list unlocked collections for group {group_id}{detail}",
            flush=True,
        )
        if isinstance(list_error, dict) and is_auth_failure(list_error):
            return False, list_error
        return False, None
    pools[group_id] = [cid for cid in item_ids if cid not in used_ids]
    if pools[group_id]:
        print(
            f"[wanted-deal] loaded {len(pools[group_id])} unlocked collection(s) "
            f"for group {group_id}",
            flush=True,
        )
    return bool(pools[group_id]), None


def peek_wanted_deal_collection_id(
    group_id: str,
    *,
    override: str,
    used_ids: set[str],
    pools: dict[str, list[str]],
) -> str | None:
    override = str(override or "").strip()
    if override and override not in used_ids:
        return override
    for cid in pools.get(group_id, []):
        if cid not in used_ids:
            return cid
    return None


def mark_wanted_deal_collection_consumed(
    group_id: str,
    collection_id: str,
    *,
    used_ids: set[str],
    pools: dict[str, list[str]],
) -> None:
    used_ids.add(str(collection_id))
    pools[group_id] = [cid for cid in pools.get(group_id, []) if cid != str(collection_id)]


def resolve_wanted_deal_collection_id(
    client,
    group_id: str,
    *,
    override: str,
    used_ids: set[str],
    pools: dict[str, list[str]],
    min_available: int,
) -> tuple[str | None, dict | None]:
    """Pick an unlocked collection id, refreshing the pool from API when needed."""
    collection_id = peek_wanted_deal_collection_id(
        group_id,
        override=override,
        used_ids=used_ids,
        pools=pools,
    )
    if collection_id:
        return collection_id, None
    pool_ready, pool_error = ensure_wanted_deal_collection_pool(
        client,
        group_id,
        used_ids=used_ids,
        min_available=min_available,
        pools=pools,
        force_refresh=True,
    )
    if pool_error and is_auth_failure(pool_error):
        return None, pool_error
    if pool_ready:
        collection_id = peek_wanted_deal_collection_id(
            group_id,
            override=override,
            used_ids=used_ids,
            pools=pools,
        )
        if collection_id:
            return collection_id, None
    return None, None


def wanted_deal_candidate_from_order(
    group: dict,
    order: dict,
    *,
    min_price_fen: int,
    payment_platform: int,
) -> dict | None:
    po_id = first_present(
        order,
        ("id", "purchaseOrderId", "purchaseConsignmentOrderId", "purchaseOrderNo", "advanceOrderId"),
    )
    relation_id = first_present(order, ("orderRelationId", "relationId", "relation_id"))
    if po_id in (None, "") or relation_id in (None, ""):
        return None
    price_fen = to_price_fen(first_present(order, ("price", "unitPrice", "salePrice")))
    if min_price_fen > 0 and price_fen is not None and price_fen < min_price_fen:
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


def list_group_wanted_purchase_orders(
    client,
    group_id: str,
    uid: str,
    *,
    page_size: int,
    max_pages: int,
    webview_headers: dict | None = None,
    warmup_path: str | None = None,
) -> tuple[list[dict], dict | None]:
    if warmup_path:
        warm_result, warm_err = client_get_with_rate_limit_retry(
            client,
            warmup_path,
            label=f"wanted-deal warmup group={group_id}",
        )
        if warm_result is None and warm_err and is_auth_failure(warm_err):
            return [], warm_err
    orders: list[dict] = []
    page_size = max(1, int(page_size))
    for page_no in range(1, max(1, int(max_pages)) + 1):
        path = (
            f"/public-market-service/digital-collection-groups/{group_id}"
            f"/purchase-orders?pageNo={page_no}&pageSize={page_size}&uid={uid}"
        )
        result, api_error = client_get_with_rate_limit_retry(
            client,
            path,
            headers=webview_headers,
            label=f"wanted-deal purchase-orders group={group_id} page={page_no}",
        )
        if result is None:
            if api_error and is_auth_failure(api_error):
                return orders, api_error
            print(
                f"[wanted-deal] purchase-orders failed for group {group_id} page={page_no}: "
                f"code={(api_error or {}).get('code')} message={(api_error or {}).get('message')}",
                flush=True,
            )
            break
        if is_auth_failure(result):
            return orders, result
        page_items = extract_list_payload(result)
        if not isinstance(page_items, list):
            break
        orders.extend(item for item in page_items if isinstance(item, dict))
        data = result.get("data")
        if isinstance(data, dict) and data.get("hasMore") is False:
            break
        if len(page_items) < page_size:
            break
    return orders, None


def pick_next_wanted_deal_collection_id(
    client,
    group_id: str,
    *,
    override: str = "",
    used_ids: set[str] | None = None,
) -> str | None:
    used = used_ids or set()
    override = str(override or "").strip()
    if override and override not in used:
        return override
    item_ids, list_error = list_unlocked_digital_collection_ids(client, group_id, limit=100)
    if item_ids is None:
        detail = ""
        if isinstance(list_error, dict):
            detail = f" (API code={list_error.get('code')} message={list_error.get('message')})"
        print(
            f"[wanted-deal] failed to list unlocked collections for group {group_id}{detail}",
            flush=True,
        )
        return None
    for cid in item_ids:
        if cid not in used:
            return cid
    return None


def build_wanted_deal_payload(
    *,
    payment_platform: int,
    collection_id: str,
    consignment_password: str,
    extra: dict | None = None,
) -> dict:
    pwd = str(consignment_password)
    payload = {
        "paymentPlatformCode": int(payment_platform),
        "digitalCollectionId": int(collection_id),
        "consignmentPassword": pwd,
        "password": pwd,
        "consignPassword": pwd,
        "consignmentPassWord": pwd,
    }
    if extra:
        payload.update(extra)
    return payload


def build_wanted_buy_payload(
    group_id: str,
    price_yuan: float,
    quantity: int,
    payment_platform: int,
    *,
    extra: dict | None = None,
) -> dict:
    """Create wanted-buy order body (matches H5 wantToBuy page: buyCount, not quantity)."""
    price_val: int | float = float(price_yuan)
    if price_val == int(price_val):
        price_val = int(price_val)
    payload = {
        "groupId": int(group_id),
        "price": price_val,
        "buyCount": max(1, int(quantity)),
        "paymentPlatformCode": int(payment_platform),
    }
    if extra:
        payload.update(extra)
    return payload


def warmup_wanted_buy(client, group_id: str) -> None:
    """Mirror App pre-submit GETs on the wantToBuy page."""
    client_get_with_rate_limit_retry(
        client,
        f"/public-service/platform-rate?groupId={group_id}",
        label=f"wanted-buy warmup platform-rate group={group_id}",
    )
    client_get_with_rate_limit_retry(
        client,
        "/payment-service/payment-platforms?placeOrderMethod=2",
        label="wanted-buy warmup payment-platforms",
    )


def warmup_sale_rush(client) -> None:
    client_get_with_rate_limit_retry(
        client,
        "/payment-service/payment-platforms?placeOrderMethod=0",
        label="sale-rush warmup payment-platforms",
    )


def _unwrap_get_result(result_pair: tuple[dict | None, dict | None]) -> dict | None:
    result, api_error = result_pair
    if result is not None:
        return result
    return api_error


def fetch_group_sale_info(client, group_id: str) -> dict | None:
    return _unwrap_get_result(
        client_get_with_rate_limit_retry(
            client,
            f"/public-service/digital-collection-groups/{group_id}/sale-info",
            label=f"sale-rush sale-info group={group_id}",
        )
    )


def fetch_sale_info_by_id(client, sale_id: str) -> dict | None:
    return _unwrap_get_result(
        client_get_with_rate_limit_retry(
            client,
            f"/public-service/sale-infos/{sale_id}",
            label=f"sale-rush sale-info id={sale_id}",
        )
    )


def list_sale_infos_page(client, *, page_no: int = 1, page_size: int = 20) -> dict | None:
    return _unwrap_get_result(
        client_get_with_rate_limit_retry(
            client,
            f"/public-service/sale-infos?sortField=0&sortType=1&pageNo={page_no}&pageSize={page_size}",
            label=f"sale-rush list sale-infos page={page_no}",
        )
    )


def match_sale_infos_by_name(items: list, collection_name: str) -> list[dict]:
    normalized_target = normalize_collection_name(collection_name)
    matched: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        display = sale_display_name_from_info(item)
        if display and collection_name_matches(collection_name, display):
            matched.append(item)
    if normalized_target:
        exact = [
            item
            for item in matched
            if normalize_collection_name(sale_display_name_from_info(item)) == normalized_target
        ]
        return exact or matched
    return matched


def resolve_sale_target_by_collection_name(
    client,
    collection_name: str,
    *,
    max_pages: int = 10,
) -> dict:
    """Match a first-sale activity by scanning /public-service/sale-infos pages."""
    seen_names: list[str] = []
    for page_no in range(1, max(1, int(max_pages)) + 1):
        result = list_sale_infos_page(client, page_no=page_no)
        if result is None:
            return {"code": 1, "error": "failed to list sale-infos"}
        if not is_success(result):
            return result

        data = (result or {}).get("data")
        items: list = []
        has_more = False
        if isinstance(data, dict):
            raw_list = data.get("list") or data.get("records") or []
            items = raw_list if isinstance(raw_list, list) else []
            has_more = bool(data.get("hasMore"))
        else:
            extracted = extract_list_payload(result)
            items = extracted if isinstance(extracted, list) else []

        for item in items:
            if not isinstance(item, dict):
                continue
            name = sale_display_name_from_info(item)
            if name and name not in seen_names:
                seen_names.append(name)

        matched = match_sale_infos_by_name(items, collection_name)
        if matched:
            if len(matched) > 1:
                names = [sale_display_name_from_info(item) for item in matched[:8]]
                return {
                    "code": 1,
                    "error": (
                        f"multiple sale activities match {collection_name!r}: "
                        + "、".join(names)
                    ),
                    "similar": names,
                }
            item = matched[0]
            sale_id = str(first_present(item, ("id", "saleId", "saleInfoId")) or "")
            group_id = str(first_present(item, ("groupId", "digitalCollectionGroupId")) or "")
            display = sale_display_name_from_info(item, collection_name)
            print(
                f"[sale-rush] sale-infos: {collection_name!r} -> "
                f"sale_id={sale_id} group_id={group_id} name={display!r}",
                flush=True,
            )
            return {
                "code": 0,
                "sale_id": sale_id,
                "group_id": group_id,
                "collection_name": display,
            }

        if not has_more and len(items) < 20:
            break

    suggestions = suggest_owned_collection_names(
        [{"name": name} for name in seen_names],
        collection_name,
        limit=8,
    )
    message = f"sale activity for '{collection_name}' not found in sale-infos list"
    if suggestions:
        message += "; similar sale names: " + "、".join(suggestions)
    return {"code": 1, "error": message, "similar": suggestions}


def extract_sale_infos_list(result: dict | None) -> list[dict]:
    if not is_success(result):
        return []
    data = (result or {}).get("data")
    items: list = []
    if isinstance(data, dict):
        raw_list = data.get("list") or data.get("records") or []
        items = raw_list if isinstance(raw_list, list) else []
    else:
        extracted = extract_list_payload(result)
        items = extracted if isinstance(extracted, list) else []
    return [item for item in items if isinstance(item, dict)]


def fetch_home_new_products(client) -> dict | None:
    return _unwrap_get_result(
        client_get_with_rate_limit_retry(
            client,
            "/public-service/home/new-products",
            label="sale-rush home/new-products",
        )
    )


def is_home_first_publish_sale_item(item: dict) -> bool:
    link = str(item.get("link") or "")
    if "first-publish" in link:
        return True
    sale_id = first_present(item, ("id", "saleId", "saleInfoId"))
    group_id = first_present(item, ("groupId", "digitalCollectionGroupId"))
    if sale_id not in (None, "") and group_id not in (None, ""):
        return True
    group = item.get("digitalCollectionGroup")
    if isinstance(group, dict) and group.get("id") not in (None, ""):
        return bool(sale_id not in (None, ""))
    return False


def parse_home_sale_list_item(item: dict) -> dict | None:
    if not is_home_first_publish_sale_item(item):
        return None
    sale_id = str(first_present(item, ("id", "saleId", "saleInfoId")) or "")
    group_id = str(first_present(item, ("groupId", "digitalCollectionGroupId")) or "")
    if not group_id:
        group = item.get("digitalCollectionGroup")
        if isinstance(group, dict):
            group_id = str(group.get("id") or "")
    if not sale_id:
        return None
    on_sale_time = parse_datetime_value(first_present(item, ("onSaleTime", "startTime")))
    price = first_present(item, ("price", "salePrice"))
    price_yuan = float(price) if price is not None else None
    return {
        "sale_id": sale_id,
        "group_id": group_id,
        "collection_name": sale_display_name_from_info(item),
        "on_sale_time": on_sale_time,
        "price_yuan": price_yuan,
        "max_buy": to_int(first_present(item, ("userOnceMaxBuyNum", "maxBuyNum", "maxNum"))),
        "sale_status": to_int(first_present(item, ("saleStatus", "status"))),
        "link": str(item.get("link") or ""),
    }


def scan_home_sale_candidates(client) -> list[dict]:
    """Scan homepage first-publish feeds only (no market / multi-page crawl)."""
    seen: set[str] = set()
    candidates: list[dict] = []

    def add_items(items: list[dict], source: str) -> None:
        for item in items:
            parsed = parse_home_sale_list_item(item)
            if not parsed:
                continue
            sale_id = str(parsed.get("sale_id") or "")
            if not sale_id or sale_id in seen:
                continue
            seen.add(sale_id)
            parsed["source"] = source
            candidates.append(parsed)

    new_products = fetch_home_new_products(client)
    if isinstance(new_products, dict) and is_success(new_products):
        add_items(extract_sale_infos_list(new_products), "home/new-products")

    page1 = list_sale_infos_page(client, page_no=1, page_size=20)
    if isinstance(page1, dict) and is_success(page1):
        add_items(extract_sale_infos_list(page1), "sale-infos/page1")

    return candidates


def pick_auto_sale_rush_candidates(candidates: list[dict]) -> list[dict]:
    """Return all on-sale (status=1) activities; if none, nearest upcoming (status=0)."""
    if not candidates:
        return []
    now = datetime.now()
    on_sale = [c for c in candidates if int(c.get("sale_status") or -1) == 1]
    if on_sale:
        return sorted(
            on_sale,
            key=lambda c: (
                c.get("on_sale_time") or datetime.min,
                int(c.get("sale_id") or 0),
            ),
            reverse=True,
        )
    upcoming = [
        c
        for c in candidates
        if int(c.get("sale_status") or -1) == 0
        and isinstance(c.get("on_sale_time"), datetime)
        and c["on_sale_time"] > now
    ]
    if upcoming:
        return [min(upcoming, key=lambda c: c["on_sale_time"])]
    return []


def enrich_sale_rush_picked_target(client, picked: dict) -> dict:
    enriched = dict(picked)
    info_result = fetch_sale_info_by_id(client, str(picked["sale_id"]))
    if isinstance(info_result, dict) and is_success(info_result):
        sale_data = extract_sale_info_record(info_result)
        if sale_data:
            enriched["collection_name"] = sale_display_name_from_info(
                sale_data, enriched.get("collection_name") or ""
            )
            enriched["on_sale_time"] = parse_datetime_value(
                first_present(sale_data, ("onSaleTime", "startTime"))
            ) or enriched.get("on_sale_time")
            price = first_present(sale_data, ("price", "salePrice"))
            enriched["price_yuan"] = (
                float(price) if price is not None else enriched.get("price_yuan")
            )
            enriched["max_buy"] = to_int(
                first_present(sale_data, ("userOnceMaxBuyNum", "maxBuyNum", "maxNum"))
            )
            enriched["sale_status"] = to_int(
                first_present(sale_data, ("saleStatus", "status"))
            ) or enriched.get("sale_status")
            sale_link = first_present(
                sale_data,
                ("link", "h5Link", "detailLink", "jumpUrl", "url", "pageUrl"),
            )
            if sale_link:
                enriched["link"] = str(sale_link)
            enriched["sale_info"] = info_result
    return enriched


def resolve_auto_sale_rush_targets(client) -> dict:
    candidates = scan_home_sale_candidates(client)
    if not candidates:
        return {
            "code": 1,
            "error": "homepage has no first-publish sale activities (new-products / sale-infos page 1)",
        }
    picked_list = pick_auto_sale_rush_candidates(candidates)
    if not picked_list:
        preview = [
            f"{c.get('collection_name')} status={c.get('sale_status')} sale_id={c.get('sale_id')}"
            for c in candidates[:6]
        ]
        return {
            "code": 1,
            "error": "no eligible homepage sale (need status=抢购中/即将发售)",
            "candidates": preview,
        }

    targets: list[dict] = []
    for picked in picked_list:
        enriched = enrich_sale_rush_picked_target(client, picked)
        status = to_int(enriched.get("sale_status"))
        if status is not None and status != 1:
            print(
                f"[sale-rush] skip sale_id={enriched.get('sale_id')} "
                f"name={enriched.get('collection_name')!r} status={status} (not on-sale)",
                flush=True,
            )
            continue
        print(
            f"[sale-rush] auto-picked sale_id={enriched['sale_id']} "
            f"name={enriched.get('collection_name')!r} "
            f"status={enriched.get('sale_status')} "
            f"source={enriched.get('source')} "
            f"opens={enriched['on_sale_time'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(enriched.get('on_sale_time'), datetime) else '-'}",
            flush=True,
        )
        targets.append(enriched)

    if not targets:
        return {
            "code": 1,
            "error": "no on-sale activities after sale-info refresh (need saleStatus=1)",
        }

    if len(targets) > 1:
        print(
            f"[sale-rush] auto mode: rushing {len(targets)} on-sale activities",
            flush=True,
        )
    return {"code": 0, "targets": targets, "multi": len(targets) > 1}


def is_captcha_related_failure(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    code = result.get("code")
    if code in (406, "406"):
        return True
    message = str(result.get("message") or result.get("error") or "").lower()
    needles = (
        "验证码",
        "captcha",
        "geetest",
        "滑块",
        "pass_token",
        "lot_number",
        "极验",
        "风控",
    )
    return any(needle in message for needle in needles)


def extract_sale_info_record(result: dict | None) -> dict | None:
    if not is_success(result):
        return None
    data = (result or {}).get("data")
    return data if isinstance(data, dict) else None


def sale_display_name_from_info(sale_data: dict, fallback: str = "") -> str:
    group = sale_data.get("digitalCollectionGroup")
    if isinstance(group, dict):
        name = first_present(group, ("name", "groupName", "collectionName"))
        if name:
            return str(name)
    return fallback


def build_sale_order_payload(
    num: int,
    payment_platform: int,
    *,
    extra: dict | None = None,
) -> dict:
    payload = {
        "num": max(1, int(num)),
        "paymentPlatformCode": int(payment_platform),
    }
    if extra:
        payload.update(extra)
    return payload


def build_sale_order_path(config: dict, sale_id: str, captcha_params: dict) -> str:
    params = captcha_params or {}
    return render_command_path(
        config,
        "sale-rush",
        (
            "/order-create-service/sales/{sale_id}/orders"
            "?captcha_id={captcha_id}&lot_number={lot_number}"
            "&pass_token={pass_token}&gen_time={gen_time}&captcha_output={captcha_output}"
        ),
        sale_id=str(sale_id),
        captcha_id=params.get("captcha_id", ""),
        lot_number=params.get("lot_number", ""),
        pass_token=params.get("pass_token", ""),
        gen_time=params.get("gen_time", ""),
        captcha_output=params.get("captcha_output", ""),
    )


def resolve_sale_rush_target(
    client,
    *,
    sale_id: str,
    group_id: str,
    collection_name: str,
    config: dict,
    auto: bool = False,
) -> dict:
    if auto and not (sale_id or group_id or collection_name):
        auto_resolved = resolve_auto_sale_rush_targets(client)
        if auto_resolved.get("code") not in (None, 0):
            return auto_resolved
        targets = auto_resolved.get("targets") or []
        if not targets:
            return {"code": 1, "error": "no auto sale targets"}
        if len(targets) == 1:
            return {"code": 0, **targets[0]}
        return {
            "code": 0,
            "targets": targets,
            "multi": True,
        }

    info_result: dict | None = None
    resolved_group_id = (group_id or "").strip()
    resolved_sale_id = (sale_id or "").strip()

    if resolved_sale_id:
        info_result = fetch_sale_info_by_id(client, resolved_sale_id)
    else:
        if not resolved_group_id:
            if not collection_name:
                return {
                    "code": 1,
                    "error": "sale-rush requires --sale-id, --group-id, or --collection-name",
                }
            sale_lookup = resolve_sale_target_by_collection_name(client, collection_name)
            if sale_lookup.get("code") not in (None, 0):
                return sale_lookup
            resolved_sale_id = str(sale_lookup.get("sale_id") or "")
            resolved_group_id = str(sale_lookup.get("group_id") or "")
            if not resolved_sale_id and not resolved_group_id:
                return {
                    "code": 1,
                    "error": f"could not resolve sale activity for {collection_name!r}",
                }
        if resolved_sale_id:
            info_result = fetch_sale_info_by_id(client, resolved_sale_id)
        else:
            info_result = fetch_group_sale_info(client, resolved_group_id)

    if info_result is None:
        return {"code": 1, "error": "failed to fetch sale-info"}
    if not is_success(info_result):
        return info_result

    sale_data = extract_sale_info_record(info_result)
    if not sale_data:
        return {"code": 1, "error": "sale-info response missing data", "raw": info_result}

    if not resolved_sale_id:
        resolved_sale_id = str(first_present(sale_data, ("id", "saleId", "saleInfoId")) or "")
    if not resolved_group_id:
        resolved_group_id = str(
            first_present(sale_data, ("groupId", "digitalCollectionGroupId")) or ""
        )

    on_sale_time = parse_datetime_value(first_present(sale_data, ("onSaleTime", "startTime")))
    max_buy = to_int(first_present(sale_data, ("userOnceMaxBuyNum", "maxBuyNum", "maxNum")))
    price = first_present(sale_data, ("price", "salePrice"))
    price_yuan = float(price) if price is not None else None

    return {
        "code": 0,
        "sale_id": resolved_sale_id,
        "group_id": resolved_group_id,
        "collection_name": sale_display_name_from_info(sale_data, collection_name),
        "on_sale_time": on_sale_time,
        "price_yuan": price_yuan,
        "max_buy": max_buy,
        "sale_status": to_int(first_present(sale_data, ("saleStatus", "status"))),
        "priority_num": to_int(first_present(sale_data, ("priorityNum", "priority_num"))),
        "sale_info": info_result,
    }


def list_consigned_orders_for_cancel(
    client,
    group_id: str,
    *,
    price_yuan: float | None = None,
    limit: int | None = None,
) -> tuple[list[dict], int] | None:
    """Owned items with consignmentStatus=1; orderId is the consign-orders/{id}/cancel id."""
    target_fen = int(round(float(price_yuan) * 100)) if price_yuan is not None else None
    orders: list[dict] = []
    page_no = 1
    page_size = 50
    max_pages = 100
    if limit is not None:
        max_pages = min(max_pages, max(5, int(limit) * 3))
    while True:
        path = (
            f"/personal-center-service/users/digital-collection-groups/{group_id}"
            f"?pageSize={page_size}&pageNo={page_no}"
        )
        result, api_error = client_get_with_rate_limit_retry(
            client,
            path,
            label=f"list-consigned-items group_id={group_id} page={page_no}",
        )
        if result is None:
            if api_error:
                print(
                    f"[list-consigned-items] API failed group_id={group_id} "
                    f"code={api_error.get('code')} message={api_error.get('message')}",
                    flush=True,
                )
            return None
        items = extract_list_payload(result)
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            if int(item.get("consignmentStatus") or 0) != 1:
                continue
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
                    **extract_consign_item_ids(item),
                    "name": item.get("name") or "",
                    "price": item.get("price"),
                }
            )
        total_matched = len(orders)
        if limit is not None and total_matched >= max(1, int(limit)):
            orders = orders[: max(1, int(limit))]
            return orders, total_matched
        if not _list_page_has_more(result, page_no=page_no, page_size=page_size, items_count=len(items)):
            break
        page_no += 1
        if page_no > max_pages:
            break
        if LIST_OWNED_PAGE_INTERVAL_SEC > 0:
            time.sleep(LIST_OWNED_PAGE_INTERVAL_SEC)
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
            # Do NOT use surplusNum/remainNum/leftNum here — those are often
            # activity leftover quota, not wallet ownership (causes false craftable).
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
        status = to_int(first_present(node, ("syntheticStatus", "status")))
        if status == 2:
            continue
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


def _record_synthesis_name(names: set[str], value) -> None:
    if value in (None, ""):
        return
    text = str(value).strip()
    if text:
        names.add(text)


def build_synthetic_id_metadata(activity_details: list[dict]) -> dict[str, dict]:
    """Map syntheticActivityId -> activity_id, display names, and schedule."""
    metadata: dict[str, dict] = {}
    for item in activity_details:
        if not isinstance(item, dict):
            continue
        activity_id = str(item.get("activity_id", "") or "")
        detail = item.get("detail")
        if not is_success(detail):
            continue
        detail_data = (detail or {}).get("data") or {}
        outer_names: set[str] = set()
        _record_synthesis_name(outer_names, first_present(detail_data, ("name", "title", "activityName")))
        _record_synthesis_name(outer_names, first_present(detail, ("name", "title", "activityName")))
        for node in iter_nested_dicts(detail):
            sid = first_present(node, ("syntheticActivityId", "synthetic_activity_id"))
            if sid in (None, ""):
                continue
            sid = str(sid)
            entry = metadata.setdefault(
                sid,
                {
                    "activity_id": activity_id,
                    "names": set(),
                    "start_time": None,
                    "end_time": None,
                },
            )
            if activity_id:
                entry["activity_id"] = activity_id
            entry["names"].update(outer_names)
            _record_synthesis_name(
                entry["names"],
                first_present(node, ("name", "title", "activityName", "description")),
            )
            for group in node.get("syntheticGroupNameList") or []:
                if isinstance(group, dict):
                    _record_synthesis_name(
                        entry["names"],
                        first_present(group, ("syntheticCustomDcName", "name", "title")),
                    )
            for album in node.get("targetAlbums") or []:
                if isinstance(album, dict):
                    _record_synthesis_name(
                        entry["names"], first_present(album, ("name", "groupName", "title"))
                    )
            start_time = parse_datetime_value(first_present(node, ("startTime", "start_at", "beginTime")))
            end_time = parse_datetime_value(first_present(node, ("endTime", "end_at", "finishTime")))
            if start_time and (entry["start_time"] is None or start_time < entry["start_time"]):
                entry["start_time"] = start_time
            if end_time and (entry["end_time"] is None or end_time > entry["end_time"]):
                entry["end_time"] = end_time
    return metadata


def synthesis_activity_name_matches(query: str, names: set[str]) -> bool:
    for name in names:
        if collection_name_matches(query, name):
            return True
    return False


def apply_synthesis_activity_filter(
    synthetic_ids: list[str],
    upcoming_ids: set[str],
    metadata: dict[str, dict],
    *,
    synthetic_id_filter: str | None,
    activity_name_filter: str | None,
) -> tuple[list[str], set[str], list[str]]:
    if synthetic_id_filter:
        sid = str(synthetic_id_filter).strip()
        if not sid:
            return synthetic_ids, upcoming_ids, []
        universe = list(dict.fromkeys([*synthetic_ids, *upcoming_ids, *metadata.keys()]))
        if sid not in universe:
            return [], set(), []
        filtered_ids = [sid]
        filtered_upcoming = {sid} if sid in upcoming_ids else set()
        return filtered_ids, filtered_upcoming, filtered_ids

    if activity_name_filter:
        query = str(activity_name_filter).strip()
        if not query:
            return synthetic_ids, upcoming_ids, list(synthetic_ids)
        matched = [
            sid
            for sid, entry in metadata.items()
            if synthesis_activity_name_matches(query, entry.get("names") or set())
        ]
        if not matched:
            return [], set(), []
        matched_set = set(matched)
        filtered_from_scan = [sid for sid in synthetic_ids if sid in matched_set]
        filtered_ids = filtered_from_scan or matched
        filtered_upcoming = upcoming_ids & matched_set
        if not filtered_upcoming and upcoming_ids:
            filtered_upcoming = matched_set & set(upcoming_ids)
        return filtered_ids, filtered_upcoming, matched

    return synthetic_ids, upcoming_ids, list(synthetic_ids)


def resolve_wait_target_for_synthetic_ids(
    metadata: dict[str, dict],
    synthetic_ids: list[str],
    *,
    pre_start_window: float,
) -> datetime | None:
    now = datetime.now()
    earliest: datetime | None = None
    for sid in synthetic_ids:
        start_time = (metadata.get(str(sid)) or {}).get("start_time")
        if start_time and start_time > now:
            if earliest is None or start_time < earliest:
                earliest = start_time
    if earliest is None:
        return None
    seconds_until = (earliest - now).total_seconds()
    if 0 < seconds_until <= pre_start_window:
        return earliest
    return None


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
    remaining_target_count: int | None = None,
    upcoming_ids: set[str] | None = None,
    metadata: dict[str, dict] | None = None,
) -> dict:
    """Wait near open, match materials for this wave, then either arm for submit or skip.

    Returns dict:
      skip: bool — True when materials not enough (do not attempt this wave)
      wave_ids: list[str]
      craftable_ids: list[str]
      centers: dict[str, dict] — pre-open centers (informational)
      next_wait_target: datetime | None — suggested next activity when skipped
    """
    upcoming_ids = set(upcoming_ids or ())
    metadata = metadata or {}
    wave_ids = select_synthetic_ids_for_wait_wave(
        synthetic_ids,
        wait_target=wait_target,
        upcoming_ids=upcoming_ids,
        metadata=metadata,
    )
    seconds_until = (wait_target - datetime.now()).total_seconds()
    print(
        f"[wait] Activity opens at {wait_target.strftime('%H:%M:%S')} "
        f"({seconds_until:.0f}s away) — pre-match materials for wave {wave_ids} "
        f"{pre_center_offset:.0f}s before start…",
        flush=True,
    )
    pre_call_time = wait_target - timedelta(seconds=max(pre_center_offset, 0))
    if pre_call_time > datetime.now():
        _wait_until_start(pre_call_time)

    pre_centers = fetch_synthesis_centers_parallel(
        client=client,
        config=config,
        synthetic_ids=wave_ids,
        concurrency=scan_concurrency,
    )
    for synthetic_id in wave_ids:
        center_result = pre_centers.get(str(synthetic_id), {})
        if is_success(center_result):
            center_data = (center_result.get("data") or {})
            surplus_num = to_int(first_present(center_data, ("surplusNum", "remainNum", "leftNum")))
            print(
                f"[pre-match] {synthetic_id} center ✔"
                + (f"  surplusNum={surplus_num}" if surplus_num is not None else ""),
                flush=True,
            )
        else:
            print(
                f"[pre-match] {synthetic_id} center failed ({center_result.get('code')})",
                flush=True,
            )

    craftable_jobs, _ = plan_craftable_jobs_from_centers(
        synthetic_ids=wave_ids,
        center_results=pre_centers,
        remaining_target_count=remaining_target_count,
        log_prefix="[pre-match]",
    )
    craftable_ids = [str(job["synthetic_id"]) for job in craftable_jobs]
    if not craftable_ids:
        next_wait = next_synthesis_start_after(
            metadata,
            after=wait_target,
            synthetic_ids=synthetic_ids,
        )
        print(
            f"[pre-match] wave {wave_ids} materials NOT enough — skip this open"
            + (
                f", next activity at {next_wait.strftime('%H:%M:%S')}"
                if next_wait
                else ", continue monitoring"
            ),
            flush=True,
        )
        # Do not busy-wait until open; advance clock past this wave.
        remain = (wait_target - datetime.now()).total_seconds()
        if remain > 0:
            time.sleep(min(remain + 0.5, 3.0))
        return {
            "skip": True,
            "wave_ids": wave_ids,
            "craftable_ids": [],
            "centers": pre_centers,
            "next_wait_target": next_wait,
        }

    print(
        f"[pre-match] wave ready to craft: {craftable_ids} — waiting for open…",
        flush=True,
    )
    _wait_until_start(wait_target)
    print(
        "[wait] Activity start time reached, submitting pre-matched recipes…",
        flush=True,
    )
    return {
        "skip": False,
        "wave_ids": wave_ids,
        "craftable_ids": craftable_ids,
        "centers": pre_centers,
        "next_wait_target": None,
    }


def synthesis_needs_slider(center_result: dict) -> bool:
    center_data = (center_result or {}).get("data") or {}
    need_slider = to_int(first_present(center_data, ("needSlider", "need_slider")))
    return bool(need_slider)


def resolve_geetest_captcha_params(
    *,
    device_host: str,
    captcha_mode: str,
    captcha_timeout: float,
    captcha_id: str,
    captcha_headed: bool,
    context: str,
    prefer_app: bool = False,
    sale_rush: bool = False,
    playwright_allowed: bool = True,
    app_group_id: str = "",
    app_sale_id: str = "",
    app_sale_link: str = "",
    app_collection_name: str = "",
) -> tuple[dict | None, str | None]:
    cap_mode = (captcha_mode or "auto").strip().lower()
    cap_timeout = captcha_timeout
    cap_id = captcha_id

    def capture_from_app(*, reuse_cached: bool = True) -> tuple[dict | None, str | None]:
        from src.frida_client import peek_rpc_captcha, poll_captcha

        if sale_rush:
            reuse_cached = False
            print(f"[captcha] App 原生验证码 ({context})…", flush=True)
            try:
                from src.app_captcha_solver import solve_captcha_on_device

                result = solve_captcha_on_device(
                    device_host,
                    timeout=cap_timeout,
                    wake_app=True,
                    group_id=app_group_id,
                    sale_id=app_sale_id,
                    sale_link=app_sale_link,
                    collection_name=app_collection_name,
                )
                if result and result.get("lot_number"):
                    print(
                        f"[captcha] App token ✓  lot_number={result['lot_number'][:8]}…",
                        flush=True,
                    )
                    return result, None
            except Exception as exc:
                print(f"[captcha] App captcha failed: {exc}", flush=True)
            return None, f"App captcha not obtained within {cap_timeout:.0f} s"

        if reuse_cached:
            cached = peek_rpc_captcha(device_host=device_host)
            if cached and cached.get("lot_number"):
                print(
                    f"[captcha] Reusing App-captured token ({context}) "
                    f"lot_number={cached['lot_number'][:8]}…",
                    flush=True,
                )
                return cached, None

        print(
            f"[captcha] 请在 iBox App 打开对应页面并点击购买/确认，完成验证码 ({context})",
            flush=True,
        )
        print(f"[captcha] Waiting up to {cap_timeout:.0f} s for App captcha…", flush=True)
        params = poll_captcha(
            device_host=device_host,
            timeout=cap_timeout,
            clear_before=not reuse_cached,
        )
        if params:
            print(
                f"[captcha] Captured from app ✓  lot_number={params['lot_number'][:8]}…",
                flush=True,
            )
            return params, None
        return None, f"Captcha not obtained within {cap_timeout:.0f} s"

    def solve_with_http_sale_rush() -> tuple[dict | None, str | None]:
        try:
            from src.geetest_solver import sale_rush_solve

            print(f"[captcha] HTTP/Playwright auto-solve ({context})…", flush=True)
            result = sale_rush_solve(
                captcha_id=cap_id,
                timeout=cap_timeout,
                headed=captcha_headed,
                max_http_attempts=6,
                max_playwright_retries=2 if playwright_allowed else 0,
            )
            if result and result.get("lot_number"):
                print(
                    f"[captcha] Sale-rush solved ✓  lot_number={result['lot_number'][:8]}…",
                    flush=True,
                )
                return result, None
            return None, "Sale-rush captcha solve returned empty result"
        except Exception as exc:
            print(f"[captcha] Sale-rush auto-solve failed: {exc}", flush=True)
            return None, str(exc)

    def solve_with_playwright(*, headed: bool) -> tuple[dict | None, str | None]:
        try:
            from src.geetest_solver import playwright_solve, check_dependencies

            ok, msg = check_dependencies()
            if not ok:
                raise ImportError(msg)
            print(
                f"[captcha] Playwright GeeTest ({context}, headed={headed})…",
                flush=True,
            )
            result = playwright_solve(
                captcha_id=cap_id,
                timeout=cap_timeout,
                headed=headed,
                max_slider_attempts=10,
                max_retries=2,
                sale_rush=sale_rush,
            )
            if result and result.get("lot_number"):
                print(
                    f"[captcha] Auto-solved ✓  lot_number={result['lot_number'][:8]}…",
                    flush=True,
                )
                return result, None
            raise RuntimeError(f"Playwright returned empty result: {result}")
        except Exception as exc:
            print(f"[captcha] Playwright failed: {exc}", flush=True)
            return None, str(exc)

    def solve_on_device(*, wake_app: bool = True) -> tuple[dict | None, str | None]:
        try:
            from src.app_captcha_solver import solve_captcha_on_device

            print(f"[captcha] App WebView auto-solve ({context})…", flush=True)
            result = solve_captcha_on_device(
                device_host,
                timeout=min(cap_timeout, 120.0),
                wake_app=wake_app,
            )
            if result and result.get("lot_number"):
                print(
                    f"[captcha] App token ✓  lot_number={result['lot_number'][:8]}…",
                    flush=True,
                )
                return result, None
            return None, "App captcha not obtained"
        except Exception as exc:
            print(f"[captcha] App auto-solve failed: {exc}", flush=True)
            return None, str(exc)

    if cap_mode == "skip":
        return None, f"Captcha required for {context} but --captcha-mode=skip"

    if sale_rush:
        if cap_mode in {"auto", "playwright"}:
            print(
                "[captcha] sale-rush 仅使用 App 原生验证码（已忽略 HTTP/Playwright 浏览器模式）",
                flush=True,
            )
        return capture_from_app(reuse_cached=False)

    if cap_mode in {"app", "manual"}:
        return capture_from_app(reuse_cached=True)

    if cap_mode == "playwright":
        if not playwright_allowed:
            return capture_from_app(reuse_cached=False)
        result, err = solve_with_playwright(headed=captcha_headed)
        if result:
            return result, None
        return None, err or "Playwright captcha solve failed"

    # auto
    if prefer_app:
        result, err = capture_from_app(reuse_cached=True)
        if result:
            return result, None
        # Headed browser is less likely to get click-captcha than headless.
        if playwright_allowed:
            result, pw_err = solve_with_playwright(headed=True)
            if result:
                return result, None
        print("[captcha] Playwright unavailable/failed, waiting for App captcha…", flush=True)
        return capture_from_app(reuse_cached=False)

    if not playwright_allowed:
        return capture_from_app(reuse_cached=False)

    result, err = solve_with_playwright(headed=captcha_headed)
    if result:
        return result, None
    if cap_mode == "auto":
        alt_headed = not captcha_headed
        print(
            f"[captcha] Retrying Playwright with headed={alt_headed}…",
            flush=True,
        )
        result, err = solve_with_playwright(headed=alt_headed)
        if result:
            return result, None
    if cap_mode == "auto":
        print("[captcha] Falling back to App captcha capture…", flush=True)
        return capture_from_app(reuse_cached=False)
    return None, err or "Captcha solve failed"


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


SYNTHESIS_DEFAULT_IDLE_INTERVAL_SEC = 30.0
SYNTHESIS_DEFAULT_FAR_INTERVAL_SEC = 90.0


def compute_synthesis_poll_interval(
    *,
    wait_target: datetime | None,
    has_craftable: bool,
    pre_start_window: float,
    active_interval: float,
    idle_interval: float,
    far_interval: float,
) -> float:
    now = datetime.now()
    if wait_target and wait_target > now:
        seconds_until = (wait_target - now).total_seconds()
        if seconds_until <= pre_start_window:
            return max(0.3, min(active_interval, 1.0))
        return min(far_interval, max(idle_interval, seconds_until - pre_start_window))
    if has_craftable:
        return max(0.3, active_interval)
    return max(idle_interval, active_interval)


def plan_synthesis_job(
    center_result: dict,
    synthetic_id: str,
    remaining_target_count: int | None,
) -> dict | None:
    if not is_success(center_result):
        return None
    candidates = extract_recipe_candidates(center_result)
    if not candidates:
        return None
    candidate = choose_recipe_candidate(candidates, str(synthetic_id))
    max_times = int(candidate.get("max_times") or 0)
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
    if max_times <= 0:
        return None
    available_times = max_times
    if remaining_target_count is not None:
        max_times = min(max_times, remaining_target_count)
    return {
        "synthetic_id": str(synthetic_id),
        "candidate": candidate,
        "max_times": max_times,
        "available_times_after_caps": available_times,
        "material_state_signature": build_material_state_signature(candidate),
        "center_result": center_result,
    }


def format_recipe_materials_brief(candidate: dict | None) -> str:
    if not candidate:
        return "-"
    parts = []
    for item in candidate.get("materials") or []:
        parts.append(
            f"{item.get('material_id')}:own={item.get('owned_count')}/need={item.get('required_count')}"
        )
    return ", ".join(parts) if parts else "-"


def plan_craftable_jobs_from_centers(
    *,
    synthetic_ids: list[str],
    center_results: dict[str, dict],
    remaining_target_count: int | None,
    log_prefix: str = "",
) -> tuple[list[dict], list[dict]]:
    """Return (craftable_jobs, cycle_entries). Logs non-craftable recipes when prefix set."""
    craftable_jobs: list[dict] = []
    cycle_entries: list[dict] = []
    prefix = f"{log_prefix} " if log_prefix else ""
    for synthetic_id in synthetic_ids:
        center_result = center_results.get(str(synthetic_id), {})
        entry: dict = {"synthetic_id": synthetic_id, "center": center_result}
        job = plan_synthesis_job(center_result, synthetic_id, remaining_target_count)
        if job is None:
            if is_success(center_result):
                entry["code"] = 0
                entry["message"] = "Current materials are insufficient for this recipe."
                mats = "-"
                try:
                    candidates = extract_recipe_candidates(center_result)
                    if candidates:
                        cand = choose_recipe_candidate(candidates, str(synthetic_id))
                        mats = format_recipe_materials_brief(cand)
                except SystemExit:
                    mats = "ambiguous"
                print(
                    f"{prefix}synthetic_id={synthetic_id} materials NOT enough [{mats}]",
                    flush=True,
                )
            else:
                entry["code"] = center_result.get("code", 1)
                print(
                    f"{prefix}synthetic_id={synthetic_id} center failed "
                    f"code={center_result.get('code')} "
                    f"message={center_result.get('message') or center_result.get('error')}",
                    flush=True,
                )
            cycle_entries.append(entry)
            continue
        job["center_result"] = center_result
        craftable_jobs.append(job)
        entry["plan"] = summarize_synthesis_plan(job["candidate"], job["max_times"])
        entry["code"] = 0
        cycle_entries.append(entry)
        print(
            f"{prefix}synthetic_id={synthetic_id} materials OK "
            f"max_times={job['max_times']} "
            f"[{format_recipe_materials_brief(job['candidate'])}]",
            flush=True,
        )
    return craftable_jobs, cycle_entries


def select_synthetic_ids_for_wait_wave(
    synthetic_ids: list[str],
    *,
    wait_target: datetime,
    upcoming_ids: set[str],
    metadata: dict[str, dict] | None = None,
) -> list[str]:
    """Pick recipe ids that belong to the upcoming open wave at wait_target."""
    metadata = metadata or {}
    wave: list[str] = []
    for sid in synthetic_ids:
        sid = str(sid)
        start_time = (metadata.get(sid) or {}).get("start_time")
        if start_time and abs((start_time - wait_target).total_seconds()) <= 2.0:
            wave.append(sid)
            continue
        if sid in upcoming_ids:
            wave.append(sid)
    return list(dict.fromkeys(wave)) or list(dict.fromkeys(str(s) for s in synthetic_ids))


def next_synthesis_start_after(
    metadata: dict[str, dict],
    *,
    after: datetime,
    synthetic_ids: list[str] | None = None,
) -> datetime | None:
    allowed = {str(s) for s in synthetic_ids} if synthetic_ids is not None else None
    earliest: datetime | None = None
    for sid, entry in (metadata or {}).items():
        if allowed is not None and str(sid) not in allowed:
            continue
        start_time = entry.get("start_time") if isinstance(entry, dict) else None
        if start_time and start_time > after:
            if earliest is None or start_time < earliest:
                earliest = start_time
    return earliest


def prioritize_synthesis_jobs(
    jobs: list[dict],
    *,
    upcoming_ids: set[str],
) -> list[dict]:
    def sort_key(job: dict) -> tuple:
        sid = str(job["synthetic_id"])
        priority = 0 if sid in upcoming_ids else 1
        return (priority, -(int(job.get("max_times") or 0)))

    return sorted(jobs, key=sort_key)


SYNTHESIS_BATCH_REPORT_REASONS = frozenset({
    "no_materials",
    "activity_ended",
    "target_reached",
})
SYNTHESIS_STOP_LOOP_REASONS = frozenset({"target_reached"})


def _synthesis_reward_name_from_row(row: dict) -> str:
    name = first_present(row, ("groupName", "name", "collectionName", "title"))
    return str(name).strip() if name not in (None, "") else ""


def summarize_synthesis_batch_rewards(batch_submits: list[dict]) -> list[str]:
    labels: list[str] = []
    for item in batch_submits:
        confirm = item.get("confirm") if isinstance(item, dict) else None
        if not isinstance(confirm, dict):
            continue
        data = confirm.get("data")
        rows = extract_list_payload(confirm) if isinstance(confirm, dict) else []
        if not rows and isinstance(data, dict):
            rows = data.get("list") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _synthesis_reward_name_from_row(row)
            token_ids = row.get("tokenIds") or row.get("tokenId") or []
            if isinstance(token_ids, list) and token_ids:
                labels.append(f"{name}#{token_ids[0]}" if name else f"#{token_ids[0]}")
            elif name:
                labels.append(str(name))
    return labels


def summarize_synthesis_reward_totals(all_submits: list[dict]) -> list[dict]:
    """Aggregate confirmed synthesis outputs by collection name."""
    counts: dict[str, int] = {}
    for item in all_submits:
        times = max(1, int(item.get("times") or 1))
        confirm = item.get("confirm") if isinstance(item, dict) else None
        if not isinstance(confirm, dict):
            continue
        rows = extract_list_payload(confirm)
        if not rows and isinstance(confirm.get("data"), dict):
            rows = confirm["data"].get("list") or []
        if not isinstance(rows, list) or not rows:
            name = "未知藏品"
            counts[name] = counts.get(name, 0) + times
            continue
        per_submit = max(1, len(rows))
        share = max(1, times // per_submit)
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _synthesis_reward_name_from_row(row) or "未知藏品"
            counts[name] = counts.get(name, 0) + share
    return [{"name": name, "count": count} for name, count in sorted(counts.items())]


def report_synthesis_batch_complete(
    *,
    batch_no: int,
    reason: str,
    batch_submits: list[dict],
    target_count: int | None,
    total_submitted: int,
    all_submits: list[dict] | None = None,
    terminal: bool = False,
) -> None:
    batch_times = sum(int(item.get("times") or 1) for item in batch_submits)
    rewards = summarize_synthesis_batch_rewards(batch_submits)
    reward_totals = summarize_synthesis_reward_totals(all_submits or batch_submits)
    payload = {
        "batch": batch_no,
        "reason": reason,
        "batch_submitted_count": len(batch_submits),
        "batch_times": batch_times,
        "target_count": target_count,
        "total_submitted": total_submitted,
        "rewards": rewards,
        "reward_totals": reward_totals,
        "terminal": terminal,
    }
    print(f"[ibox-synthesis-batch] {json.dumps(payload, ensure_ascii=False)}", flush=True)


def is_success(result: dict) -> bool:
    return isinstance(result, dict) and result.get("code") == 0


def is_rate_limited(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    message = str(result.get("message") or result.get("msg") or "")
    if any(token in message for token in ("NullPointerException", "Exception", "Error")):
        return False
    if "过于频繁" in message:
        return True
    return str(result.get("code")) == "10002" and not message.strip()


def client_get_with_rate_limit_retry(
    client,
    path: str,
    *,
    label: str = "GET",
    max_attempts: int = 5,
    base_delay: float = 2.0,
    headers: dict | None = None,
) -> tuple[dict | None, dict | None]:
    delay = base_delay
    last_result: dict | None = None
    for attempt in range(1, max_attempts + 1):
        result = client.get(path, headers=headers)
        if is_success(result):
            return result, None
        last_result = result if isinstance(result, dict) else {"error": str(result)}
        if is_rate_limited(last_result) and attempt < max_attempts:
            print(
                f"[rate-limit] {label} code={last_result.get('code')} "
                f"message={last_result.get('message')} retry in {delay:.1f}s "
                f"({attempt}/{max_attempts})",
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        break
    return None, last_result


def client_post_with_rate_limit_retry(
    client,
    path: str,
    payload: dict,
    *,
    label: str = "POST",
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> dict:
    delay = base_delay
    last_result: dict = {"code": 1, "error": "empty response"}
    for attempt in range(1, max_attempts + 1):
        result = client.post(path, payload)
        if is_success(result):
            return result
        last_result = result if isinstance(result, dict) else {"error": str(result)}
        if is_rate_limited(last_result) and attempt < max_attempts:
            print(
                f"[rate-limit] {label} code={last_result.get('code')} "
                f"message={last_result.get('message')} retry in {delay:.1f}s "
                f"({attempt}/{max_attempts})",
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        break
    return last_result


def should_retry_synthesis_submit(result: dict | None) -> bool:
    if result is None:
        return True
    if not isinstance(result, dict):
        return True

    code = result.get("code")
    if code == 0:
        return False

    # Business "not open yet" / clock skew at the exact open second — keep retrying
    # inside the submit window instead of giving up on the first attempt.
    message = " ".join(
        str(part)
        for part in (result.get("message"), result.get("msg"), result.get("error"))
        if part not in (None, "")
    )
    if "活动未开始" in message or "未开始" in message:
        return True
    if str(code) in {"500006", "500007"}:
        return True

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

    if result.get("auth_failure"):
        return True

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
    user_info = data.get("userInfo") or {}
    if isinstance(user_info, dict):
        value = user_info.get("userId")
        if value not in (None, ""):
            return str(value)
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
    from src.device_bridge import ensure_adb_ready, bridge_check as run_bridge_check

    adb_serial = ensure_adb_ready(parsed, config)
    if adb_serial:
        print(f"[bridge] adb ready serial={adb_serial}", flush=True)
    base_url = config["base_url"]
    login_path = config["login"]["path"]
    sms_path = config.get("sms", {}).get("path", "/personal-center-service/login/sendSms")
    headers = config.get("headers") or {}
    app_version = str(headers.get("app-version") or headers.get("App-Version") or "2.3.2")
    config_c_id = config.get("login", {}).get("c_id", "")
    cmd = parsed.command

    # ── capture command ───────────────────────────────────────────────────────
    if cmd == "bridge-check":
        adb_cfg = config.get("adb") or {}
        adb_host = getattr(parsed, "adb_host", None) or (
            str(adb_cfg.get("host") or "").strip() if isinstance(adb_cfg, dict) else ""
        ) or None
        adb_port = int(getattr(parsed, "adb_port", None) or (
            adb_cfg.get("port") if isinstance(adb_cfg, dict) else None
        ) or 5555)
        report = run_bridge_check(device_host, adb_host, adb_port)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report.get("ready") else 1)

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
        "market-purchase",
        "consign-create",
        "consign-cancel",
        "purchase-detail",
        "wanted-detail",
        "wanted-deal",
        "wanted-buy",
        "sale-rush",
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
            operation = lambda: client.get(
                path,
                headers=build_webview_api_headers(getattr(client, "token", None)),
            )
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
                return resolve_geetest_captcha_params(
                    device_host=device_host,
                    captcha_mode=parsed.captcha_mode,
                    captcha_timeout=parsed.captcha_timeout,
                    captcha_id=parsed.captcha_id,
                    captcha_headed=getattr(parsed, "captcha_headed", False),
                    context=f"synthetic_id={synth_id}",
                )

            def synthesis_auto_operation():
                continuous = not parsed.once
                idle_interval = max(float(parsed.idle_interval), 1.0)
                far_interval = max(float(parsed.far_interval), idle_interval)
                cycles: list[dict] = []
                successful_submits: list[dict] = []
                current_batch_submits: list[dict] = []
                successful_state_by_synthetic_id: dict[str, tuple] = {}
                remaining_target_count = parsed.target_count
                last_activity_list_result = None
                last_activity_details: list[dict] = []
                last_synthetic_ids: list[str] = []
                cycle_no = 0
                batch_no = 0
                last_had_craftable = False

                def finish_batch(reason: str) -> bool:
                    nonlocal batch_no, remaining_target_count, current_batch_submits
                    nonlocal successful_state_by_synthetic_id
                    if not current_batch_submits and reason not in SYNTHESIS_BATCH_REPORT_REASONS:
                        return continuous
                    batch_no += 1
                    stop_loop = reason in SYNTHESIS_STOP_LOOP_REASONS
                    if not continuous and reason in SYNTHESIS_BATCH_REPORT_REASONS:
                        stop_loop = True
                    report_synthesis_batch_complete(
                        batch_no=batch_no,
                        reason=reason,
                        batch_submits=list(current_batch_submits),
                        target_count=parsed.target_count,
                        total_submitted=len(successful_submits),
                        all_submits=list(successful_submits),
                        terminal=stop_loop,
                    )
                    current_batch_submits = []
                    successful_state_by_synthetic_id.clear()
                    if parsed.target_count is not None:
                        remaining_target_count = parsed.target_count
                    if stop_loop:
                        print(
                            f"[synthesis-auto] batch complete ({reason}); stopping.",
                            flush=True,
                        )
                        return False
                    print(
                        f"[synthesis-auto] batch complete ({reason}); "
                        "continuing to monitor for the next synthesis activity…",
                        flush=True,
                    )
                    return True

                while True:
                    cycle_no += 1
                    if parsed.max_rounds > 0 and cycle_no > parsed.max_rounds:
                        print(
                            f"[cycle {cycle_no - 1}] Reached --max-rounds={parsed.max_rounds}, stopping.",
                            flush=True,
                        )
                        break

                    print(f"[cycle {cycle_no}] Fetching latest synthesis activities…", flush=True)
                    activity_list_result = client.get_synthesis_activity_list(activity_list_path)
                    last_activity_list_result = activity_list_result
                    if not is_success(activity_list_result):
                        if is_auth_failure(activity_list_result):
                            print(
                                f"[cycle {cycle_no}] activity-list auth failed "
                                f"({activity_list_result.get('code')}), stopping.",
                                flush=True,
                            )
                            return activity_list_result
                        retry_in = compute_synthesis_poll_interval(
                            wait_target=None,
                            has_craftable=False,
                            pre_start_window=parsed.pre_start_window,
                            active_interval=parsed.loop_interval,
                            idle_interval=idle_interval,
                            far_interval=far_interval,
                        )
                        print(
                            f"[cycle {cycle_no}] activity-list failed ({activity_list_result.get('code')}), "
                            f"retry in {retry_in:.1f}s…",
                            flush=True,
                        )
                        time.sleep(retry_in)
                        continue

                    activity_ids = extract_activity_ids(activity_list_result)
                    if not activity_ids:
                        if successful_submits or batch_no > 0 or current_batch_submits:
                            if not finish_batch("activity_ended"):
                                break
                        if not continuous:
                            break
                        retry_in = compute_synthesis_poll_interval(
                            wait_target=None,
                            has_craftable=False,
                            pre_start_window=parsed.pre_start_window,
                            active_interval=parsed.loop_interval,
                            idle_interval=idle_interval,
                            far_interval=far_interval,
                        )
                        print(
                            f"[cycle {cycle_no}] No synthesis activities, waiting for next; "
                            f"retry in {retry_in:.1f}s…",
                            flush=True,
                        )
                        time.sleep(retry_in)
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
                    upcoming_ids: set[str] = set()
                    pre_ids, _, earliest_start = extract_upcoming_synthetic_ids_with_start(activity_details)
                    if pre_ids and earliest_start is not None:
                        seconds_until = (earliest_start - datetime.now()).total_seconds()
                        if 0 < seconds_until <= parsed.pre_start_window:
                            active_set: set[str] = set()
                            for item in activity_details:
                                detail = item.get("detail")
                                if is_success(detail):
                                    active_set.update(extract_synthetic_ids(detail))
                            filtered = {sid for sid in pre_ids if sid not in active_set}
                            upcoming_ids = filtered or set(pre_ids)

                    synthesis_metadata = build_synthetic_id_metadata(activity_details)
                    sid_filter = (getattr(parsed, "synthetic_id_filter", "") or "").strip() or None
                    name_filter = (getattr(parsed, "activity_name_filter", "") or "").strip() or None
                    matched_filter_ids: list[str] = []
                    if sid_filter or name_filter:
                        synthetic_ids, upcoming_ids, matched_filter_ids = apply_synthesis_activity_filter(
                            synthetic_ids,
                            upcoming_ids,
                            synthesis_metadata,
                            synthetic_id_filter=sid_filter,
                            activity_name_filter=name_filter,
                        )
                        # Name/id filter matched the whole activity family. Keep ALL
                        # matched recipe ids for center checks — do not shrink to only
                        # the not-yet-open "upcoming" subset (that caused 17:05 to
                        # check only one id while materials were on another recipe).
                        if matched_filter_ids:
                            synthetic_ids = list(dict.fromkeys(matched_filter_ids))
                            last_synthetic_ids = synthetic_ids
                            upcoming_ids = {
                                sid
                                for sid in matched_filter_ids
                                if (synthesis_metadata.get(str(sid)) or {}).get("start_time")
                                and (synthesis_metadata.get(str(sid)) or {})["start_time"]
                                > datetime.now()
                            }
                        filter_label = sid_filter or name_filter
                        print(
                            f"[synthesis-auto] activity filter {filter_label!r} -> "
                            f"synthetic id(s) {synthetic_ids}"
                            + (f" upcoming={sorted(upcoming_ids)}" if upcoming_ids else ""),
                            flush=True,
                        )
                        filtered_wait = resolve_wait_target_for_synthetic_ids(
                            synthesis_metadata,
                            synthetic_ids,
                            pre_start_window=parsed.pre_start_window,
                        )
                        if filtered_wait is not None:
                            wait_target = filtered_wait
                        elif matched_filter_ids:
                            future_starts = [
                                entry.get("start_time")
                                for sid in matched_filter_ids
                                for entry in [synthesis_metadata.get(str(sid)) or {}]
                                if entry.get("start_time") and entry["start_time"] > datetime.now()
                            ]
                            if future_starts:
                                wait_target = min(future_starts)
                        # If some matched recipes are already open, do not sleep until the
                        # next future start — try the active ones in this cycle first.
                        active_matched = [sid for sid in synthetic_ids if sid not in upcoming_ids]
                        if active_matched and wait_target is not None:
                            print(
                                f"[synthesis-auto] {len(active_matched)} matched id(s) already open "
                                f"{active_matched}; skip wait until {wait_target.strftime('%H:%M:%S')}",
                                flush=True,
                            )
                            wait_target = None

                    if sid_filter or name_filter:
                        if not synthetic_ids and not matched_filter_ids:
                            retry_in = compute_synthesis_poll_interval(
                                wait_target=wait_target,
                                has_craftable=False,
                                pre_start_window=parsed.pre_start_window,
                                active_interval=parsed.loop_interval,
                                idle_interval=idle_interval,
                                far_interval=far_interval,
                            )
                            filter_label = sid_filter or name_filter
                            print(
                                f"[cycle {cycle_no}] No synthetic id matches filter {filter_label!r}, "
                                f"retry in {retry_in:.1f}s…",
                                flush=True,
                            )
                            time.sleep(retry_in)
                            continue
                        if not synthetic_ids and matched_filter_ids:
                            synthetic_ids = matched_filter_ids
                            last_synthetic_ids = synthetic_ids

                    if not synthetic_ids:
                        retry_in = compute_synthesis_poll_interval(
                            wait_target=wait_target,
                            has_craftable=False,
                            pre_start_window=parsed.pre_start_window,
                            active_interval=parsed.loop_interval,
                            idle_interval=idle_interval,
                            far_interval=far_interval,
                        )
                        print(
                            f"[cycle {cycle_no}] No synthetic ids discovered, retry in {retry_in:.1f}s…",
                            flush=True,
                        )
                        time.sleep(retry_in)
                        continue

                    pre_center_cache: dict[str, dict] = {}
                    waited_for_open = False
                    pre_matched_ids: list[str] | None = None
                    if wait_target is not None and wait_target > datetime.now():
                        pre_match = run_pre_start_wait_and_pre_center(
                            client=client,
                            config=config,
                            synthetic_ids=synthetic_ids,
                            wait_target=wait_target,
                            pre_center_offset=parsed.pre_center_offset,
                            scan_concurrency=parsed.scan_concurrency,
                            remaining_target_count=remaining_target_count,
                            upcoming_ids=upcoming_ids,
                            metadata=synthesis_metadata,
                        )
                        if pre_match.get("skip"):
                            next_wait = pre_match.get("next_wait_target")
                            retry_in = compute_synthesis_poll_interval(
                                wait_target=next_wait if isinstance(next_wait, datetime) else None,
                                has_craftable=False,
                                pre_start_window=parsed.pre_start_window,
                                active_interval=parsed.loop_interval,
                                idle_interval=idle_interval,
                                far_interval=far_interval,
                            )
                            print(
                                f"[cycle {cycle_no}] skip this wave (no materials); "
                                f"retry in {retry_in:.1f}s…",
                                flush=True,
                            )
                            if not continuous:
                                break
                            time.sleep(retry_in)
                            cycles.append(
                                {
                                    "cycle": cycle_no,
                                    "entries": [],
                                    "skipped_wave": pre_match.get("wave_ids"),
                                    "reason": "pre_match_no_materials",
                                }
                            )
                            continue
                        waited_for_open = True
                        pre_matched_ids = list(pre_match.get("craftable_ids") or [])
                        # Prefer live centers after open for submit accuracy.
                        pre_center_cache = {}
                        if pre_matched_ids:
                            synthetic_ids = list(dict.fromkeys(pre_matched_ids))
                            last_synthetic_ids = synthetic_ids

                    center_results = fetch_synthesis_centers_parallel(
                        client=client,
                        config=config,
                        synthetic_ids=synthetic_ids,
                        concurrency=parsed.scan_concurrency,
                        pre_center_cache=pre_center_cache,
                    )
                    if waited_for_open:
                        print(
                            f"[cycle {cycle_no}] live synthesis-center refresh after open "
                            f"for {len(synthetic_ids)} pre-matched id(s)",
                            flush=True,
                        )

                    craftable_jobs, cycle_entries = plan_craftable_jobs_from_centers(
                        synthetic_ids=synthetic_ids,
                        center_results=center_results,
                        remaining_target_count=remaining_target_count,
                        log_prefix=f"[cycle {cycle_no}]",
                    )

                    craftable_jobs = prioritize_synthesis_jobs(
                        craftable_jobs,
                        upcoming_ids=upcoming_ids,
                    )
                    any_craftable = bool(craftable_jobs)

                    if not any_craftable:
                        if last_had_craftable or current_batch_submits:
                            if not finish_batch("no_materials"):
                                break
                        last_had_craftable = False
                        next_wait = next_synthesis_start_after(
                            synthesis_metadata,
                            after=datetime.now(),
                            synthetic_ids=last_synthetic_ids or synthetic_ids,
                        )
                        retry_in = compute_synthesis_poll_interval(
                            wait_target=next_wait,
                            has_craftable=False,
                            pre_start_window=parsed.pre_start_window,
                            active_interval=parsed.loop_interval,
                            idle_interval=idle_interval,
                            far_interval=far_interval,
                        )
                        if next_wait:
                            print(
                                f"[cycle {cycle_no}] No craftable recipes — "
                                f"wait next activity at {next_wait.strftime('%H:%M:%S')} "
                                f"(retry in {retry_in:.1f}s)…",
                                flush=True,
                            )
                        else:
                            print(
                                f"[cycle {cycle_no}] No craftable recipes in wallet, "
                                f"retry in {retry_in:.1f}s…",
                                flush=True,
                            )
                        if not continuous:
                            break
                        time.sleep(retry_in)
                        cycles.append({"cycle": cycle_no, "entries": cycle_entries})
                        continue

                    last_had_craftable = True
                    if upcoming_ids:
                        print(
                            f"[cycle {cycle_no}] prioritizing {len(upcoming_ids)} upcoming synthetic id(s)",
                            flush=True,
                        )

                    cycle_progress = False
                    stop_after_batch = False
                    for job in craftable_jobs:
                        if remaining_target_count is not None and remaining_target_count <= 0:
                            if not finish_batch("target_reached"):
                                stop_after_batch = True
                            break

                        synthetic_id = job["synthetic_id"]
                        candidate = job["candidate"]
                        max_times = job["max_times"]
                        material_state_signature = job["material_state_signature"]
                        center_result = job["center_result"]

                        previous_success_state = successful_state_by_synthetic_id.get(str(synthetic_id))
                        if previous_success_state == material_state_signature:
                            continue

                        payload = build_synthesis_submit_payload(candidate, synthetic_id, max_times)
                        if parsed.dry_run:
                            cycle_progress = True
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
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
                        confirmed = False
                        confirm_result = None
                        if not is_success(submit_result):
                            print(
                                f"[cycle {cycle_no}] submit failed synthetic_id={synthetic_id} "
                                f"code={submit_result.get('code') if isinstance(submit_result, dict) else submit_result} "
                                f"message="
                                f"{(submit_result or {}).get('message') if isinstance(submit_result, dict) else ''}"
                                f" attempts={submit_outcome.get('attempt_count')}",
                                flush=True,
                            )
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
                                    print(f"[cycle {cycle_no}] captcha error: {captcha_err}", flush=True)
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
                                    confirmed = is_success(confirm_result)
                                    if not confirmed:
                                        print(
                                            f"[cycle {cycle_no}] confirm failed synthetic_id={synthetic_id} "
                                            f"code={confirm_result.get('code') if isinstance(confirm_result, dict) else confirm_result} "
                                            f"message="
                                            f"{(confirm_result or {}).get('message') if isinstance(confirm_result, dict) else ''}",
                                            flush=True,
                                        )
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
                                confirmed = is_success(confirm_result)
                                if not confirmed:
                                    print(
                                        f"[cycle {cycle_no}] confirm failed synthetic_id={synthetic_id} "
                                        f"code={confirm_result.get('code') if isinstance(confirm_result, dict) else confirm_result} "
                                        f"message="
                                        f"{(confirm_result or {}).get('message') if isinstance(confirm_result, dict) else ''}",
                                        flush=True,
                                    )

                        if confirmed:
                            cycle_progress = True
                            if remaining_target_count is not None:
                                remaining_target_count -= max_times
                            successful_state_by_synthetic_id[str(synthetic_id)] = material_state_signature
                            submit_record = {
                                "cycle": cycle_no,
                                "synthetic_id": synthetic_id,
                                "times": max_times,
                                "attempt_count": submit_outcome["attempt_count"],
                                "submit_concurrency": submit_outcome["concurrency"],
                                "submit": submit_result,
                                "confirm": confirm_result,
                            }
                            successful_submits.append(submit_record)
                            current_batch_submits.append(submit_record)
                            if parsed.target_count is not None:
                                submitted_qty = sum(
                                    item.get("times", 1) for item in current_batch_submits
                                )
                                report_task_progress(submitted_qty, parsed.target_count)
                            if remaining_target_count is not None and remaining_target_count <= 0:
                                if not finish_batch("target_reached"):
                                    stop_after_batch = True
                                break

                    cycles.append({"cycle": cycle_no, "entries": cycle_entries})

                    if parsed.dry_run:
                        break
                    if stop_after_batch:
                        break
                    if not continuous:
                        if remaining_target_count is not None and remaining_target_count <= 0:
                            break
                        if not any_craftable:
                            break

                    retry_in = compute_synthesis_poll_interval(
                        wait_target=wait_target,
                        has_craftable=any_craftable,
                        pre_start_window=parsed.pre_start_window,
                        active_interval=parsed.loop_interval,
                        idle_interval=idle_interval,
                        far_interval=far_interval,
                    )
                    if cycle_progress:
                        if retry_in > 0:
                            time.sleep(min(retry_in, parsed.loop_interval))
                        continue
                    print(
                        f"[cycle {cycle_no}] Craftable items remain but submit did not succeed, "
                        f"retry in {retry_in:.1f}s…",
                        flush=True,
                    )
                    time.sleep(retry_in)

                any_discovered_craftable = any(
                    (entry.get("plan") or {}).get("max_times", 0) > 0
                    for cycle_info in cycles
                    for entry in cycle_info["entries"]
                )
                return {
                    "code": 0 if parsed.dry_run or successful_submits or not any_discovered_craftable else 1,
                    "continuous": continuous,
                    "batch_count": batch_no,
                    "activity_list": last_activity_list_result,
                    "activity_details": last_activity_details,
                    "synthetic_ids": last_synthetic_ids,
                    "synthetic_id_filter": (getattr(parsed, "synthetic_id_filter", "") or "").strip() or None,
                    "activity_name_filter": (getattr(parsed, "activity_name_filter", "") or "").strip() or None,
                    "cycles": cycles,
                    "target_count": parsed.target_count,
                    "remaining_target_count": remaining_target_count,
                    "submitted_count": len(successful_submits),
                    "submitted": successful_submits,
                    "reward_totals": summarize_synthesis_reward_totals(successful_submits),
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
            collection_name = (parsed.collection_name or "").strip()
            extra_payload = parse_payload_arg(parsed.payload) or {}
            if collection_name and parsed.price is not None:
                if not (parsed.consignment_password or "").strip():
                    raise SystemExit("Error: market-buy requires --支付密码")
                buy_qty = max(1, int(parsed.quantity))
                list_pages = max(1, int(parsed.list_pages or 10))
                poll_interval = parsed.poll_interval
                if poll_interval is None:
                    poll_interval = float(
                        get_command_default(
                            config,
                            "market-buy",
                            "poll_interval",
                            str(MARKET_BUY_DEFAULT_POLL_INTERVAL_SEC),
                        )
                    )
                poll_interval = max(0.5, float(poll_interval))
                payment_platform = (
                    parsed.payment_platform
                    if parsed.payment_platform is not None
                    else int(get_command_default(config, "market-buy", "payment_platform_code", "30"))
                )
                purchase_path = render_command_path(
                    config,
                    "market-buy",
                    "/order-create-service/purchase-consignment-orders?uid={uid}",
                    uid=uid,
                )

                def market_buy_operation():
                    group_id = resolve_group_id_for_market(client, collection_name, config)
                    if isinstance(group_id, dict):
                        return group_id
                    ibox_token = getattr(client, "token", None) or (
                        (saved_session or {}).get("token") if saved_session else None
                    )
                    return run_market_buy_sweep(
                        client=client,
                        config=config,
                        uid=uid,
                        group_id=group_id,
                        collection_name=collection_name,
                        target_price_yuan=float(parsed.price),
                        target_qty=buy_qty,
                        consignment_password=parsed.consignment_password,
                        payment_platform=payment_platform,
                        purchase_path=purchase_path,
                        extra_payload=extra_payload,
                        ibox_token=str(ibox_token or ""),
                        app_version=app_version,
                        list_pages=list_pages,
                        poll_interval=poll_interval,
                    )

                operation = market_buy_operation
            else:
                path = render_command_path(
                    config,
                    "market-buy",
                    "/order-create-service/batch-purchase-consignment-orders?uid={uid}",
                    uid=uid,
                )
                payload = extra_payload
                if not payload:
                    raise SystemExit(
                        "Error: market-buy requires --collection-name + --price + --支付密码, or --payload"
                    )
                operation = lambda: client.post(path, payload)
        elif cmd == "market-purchase":
            if not uid:
                raise SystemExit(
                    "Error: uid is required for market-purchase. Pass --uid or ensure login response contains uid"
                )
            collection_name = (parsed.collection_name or "").strip()
            if not collection_name:
                raise SystemExit("Error: market-purchase requires --collection-name")
            if not (parsed.consignment_password or "").strip():
                raise SystemExit("Error: market-purchase requires --支付密码")
            extra_payload = parse_payload_arg(parsed.payload) or {}
            payment_platform = (
                parsed.payment_platform
                if parsed.payment_platform is not None
                else int(get_command_default(config, "market-purchase", "payment_platform_code", "30"))
            )
            purchase_path = render_command_path(
                config,
                "market-purchase",
                "/order-create-service/purchase-consignment-orders?uid={uid}",
                uid=uid,
            )
            seller_uid = (parsed.seller_uid or "").strip()
            consign_order_id = (parsed.consign_order_id or "").strip()
            digital_collection_id = (parsed.digital_collection_id or "").strip()
            list_pages = max(1, int(parsed.list_pages or 10))
            buy_qty = max(1, int(parsed.quantity))
            target_price_yuan = float(parsed.price)
            target_price_fen = int(round(target_price_yuan * 100))

            def market_purchase_operation():
                group_id = resolve_group_id_for_market(client, collection_name, config)
                if isinstance(group_id, dict):
                    return group_id

                ibox_token = getattr(client, "token", None) or (
                    (saved_session or {}).get("token") if saved_session else None
                )

                print(
                    f"[market-purchase] group_id={group_id} price={target_price_yuan}yuan "
                    f"quantity={buy_qty} consign_order_id={consign_order_id or 'any'} "
                    f"digital_collection_id={digital_collection_id or '-'}",
                    flush=True,
                )

                results: list[dict] = []
                success_count = 0
                fail_count = 0

                def finalize_purchase(
                    *,
                    consign_id: str,
                    create_result: dict,
                    seller: str | None = None,
                    dc_id: str | None = None,
                ) -> dict:
                    payment = complete_purchase_payment(
                        client,
                        create_result=create_result,
                        consignment_password=parsed.consignment_password,
                        ibox_token=str(ibox_token or ""),
                        app_version=app_version,
                        max_price_yuan=target_price_yuan,
                        config=config,
                    )
                    paid = bool(payment.get("paid"))
                    return {
                        "consign_order_id": consign_id,
                        "seller_uid": seller,
                        "digital_collection_id": dc_id,
                        "ok": paid,
                        "paid": paid,
                        "purchase_order_id": payment.get("purchase_order_id"),
                        "cashierLink": payment.get("cashierLink"),
                        "create": create_result,
                        "payment": payment,
                        "result": create_result if paid else payment,
                    }

                purchase_refs = parse_consign_purchase_ref_list(consign_order_id, digital_collection_id)
                if purchase_refs:
                    if len(purchase_refs) > buy_qty:
                        purchase_refs = purchase_refs[:buy_qty]
                    missing_dc = [order_id for order_id, dc_id in purchase_refs if not dc_id]
                    if missing_dc:
                        return {
                            "code": 1,
                            "error": (
                                "direct purchase requires digitalCollectionId for each listing. "
                                "Use orderId|藏品ID format from consign reply."
                            ),
                            "group_id": group_id,
                            "missing": missing_dc[:5],
                        }
                    direct_total = len(purchase_refs)
                    print(
                        f"[market-purchase] direct batch purchase count={direct_total}",
                        flush=True,
                    )
                    for index, (ref_order_id, ref_dc_id) in enumerate(purchase_refs, start=1):
                        if index > 1 and CONSIGN_POST_INTERVAL_SEC > 0:
                            time.sleep(CONSIGN_POST_INTERVAL_SEC)
                        print(
                            f"[market-purchase] buying {index}/{direct_total} "
                            f"consignOrderId={ref_order_id} digitalCollectionId={ref_dc_id}",
                            flush=True,
                        )
                        payload = build_market_purchase_payload(
                            ref_order_id,
                            payment_platform,
                            parsed.consignment_password,
                            digital_collection_id=ref_dc_id,
                            extra=extra_payload,
                        )
                        direct_result = client_post_with_rate_limit_retry(
                            client,
                            purchase_path,
                            payload,
                            label=f"market-purchase direct {index}/{direct_total}",
                        )
                        if is_success(direct_result):
                            entry = finalize_purchase(
                                consign_id=ref_order_id,
                                create_result=direct_result,
                                dc_id=ref_dc_id,
                            )
                            ok = entry["ok"]
                        else:
                            ok = False
                            print(
                                f"[market-purchase] failed consignOrderId={ref_order_id}: "
                                f"code={direct_result.get('code')} message={direct_result.get('message')}",
                                flush=True,
                            )
                            entry = {
                                "consign_order_id": ref_order_id,
                                "digital_collection_id": ref_dc_id,
                                "ok": False,
                                "paid": False,
                                "result": direct_result,
                            }
                        if ok:
                            success_count += 1
                        else:
                            fail_count += 1
                        results.append(entry)
                        report_task_progress(index, direct_total)

                    if direct_total > 1 or success_count > 0 or fail_count == 0:
                        summary = (
                            f"{collection_name}点对点购买已完成，购买个数：{success_count}，失败个数：{fail_count}"
                        )
                        print(summary, flush=True)
                        return {
                            "code": 0 if fail_count == 0 else 1,
                            "message": summary,
                            "summary": summary,
                            "collectionName": collection_name,
                            "successCount": success_count,
                            "failCount": fail_count,
                            "group_id": group_id,
                            "mode": "direct",
                            "results": results,
                        }
                    ref_order_id, ref_dc_id = purchase_refs[0]
                    print(
                        f"[market-purchase] direct purchase failed for "
                        f"consignOrderId={ref_order_id}; falling back to market list",
                        flush=True,
                    )
                    consign_order_id = ref_order_id
                    digital_collection_id = ref_dc_id
                    success_count = 0
                    fail_count = 0
                    results = []

                single_order_id, single_dc_id = split_consign_purchase_ref(
                    consign_order_id,
                    digital_collection_id,
                )
                if single_order_id and single_dc_id and not purchase_refs:
                    print(
                        f"[market-purchase] trying direct purchase "
                        f"consignOrderId={single_order_id} digitalCollectionId={single_dc_id}",
                        flush=True,
                    )
                    payload = build_market_purchase_payload(
                        single_order_id,
                        payment_platform,
                        parsed.consignment_password,
                        digital_collection_id=single_dc_id,
                        extra=extra_payload,
                    )
                    direct_result = client_post_with_rate_limit_retry(
                        client,
                        purchase_path,
                        payload,
                        label="market-purchase direct",
                    )
                    if is_success(direct_result):
                        entry = finalize_purchase(
                            consign_id=single_order_id,
                            create_result=direct_result,
                            dc_id=single_dc_id,
                        )
                        if entry["ok"]:
                            success_count = 1
                        else:
                            fail_count = 1
                        return {
                            "code": 0 if entry["ok"] else 1,
                            "collectionName": collection_name,
                            "successCount": success_count,
                            "failCount": fail_count,
                            "group_id": group_id,
                            "consign_order_id": single_order_id,
                            "mode": "direct",
                            "paid": entry.get("paid", False),
                            "hint": (entry.get("payment") or {}).get("hint"),
                            "results": [entry],
                        }
                    print(
                        f"[market-purchase] direct purchase failed "
                        f"code={direct_result.get('code')} message={direct_result.get('message')}; "
                        "falling back to market list",
                        flush=True,
                    )
                    consign_order_id = single_order_id

                listings, list_error = fetch_group_market_listings(
                    client,
                    config,
                    group_id,
                    uid,
                    max_pages=list_pages,
                )
                if listings is None:
                    listings = []
                if not listings and list_error:
                    detail = ""
                    if isinstance(list_error, dict):
                        detail = f" (API code={list_error.get('code')} message={list_error.get('message')})"
                    return {
                        "code": 1,
                        "error": f"failed to list market consignments for group {group_id}{detail}",
                        "group_id": group_id,
                    }

                selected = select_market_listings(
                    listings or [],
                    price_fen=target_price_fen,
                    quantity=buy_qty,
                    seller_uid=seller_uid,
                    consign_order_id=consign_order_id,
                    exact_price=True,
                )
                if not selected:
                    hint = f"price={target_price_yuan}yuan"
                    if consign_order_id:
                        hint += f", consign_order_id={consign_order_id}"
                    elif seller_uid:
                        hint += f", seller_uid={seller_uid}"
                    error = (
                        f"no matching consignment listings for {collection_name!r} ({hint}). "
                        f"Ask seller to consign first, then retry with the consign order id."
                    )
                    if consign_order_id:
                        error += " Direct purchase with the consign order id also failed."
                    return {
                        "code": 1,
                        "error": error,
                        "group_id": group_id,
                        "scanned": len(listings or []),
                    }
                if len(selected) < buy_qty:
                    return {
                        "code": 1,
                        "error": (
                            f"need {buy_qty} listing(s) at {target_price_yuan}yuan, "
                            f"only {len(selected)} matched"
                        ),
                        "group_id": group_id,
                        "matched": len(selected),
                    }

                for index, entry in enumerate(selected[:buy_qty], start=1):
                    if index > 1 and CONSIGN_POST_INTERVAL_SEC > 0:
                        time.sleep(CONSIGN_POST_INTERVAL_SEC)
                    order_id = entry["consign_order_id"]
                    payload = build_market_purchase_payload(
                        order_id,
                        payment_platform,
                        parsed.consignment_password,
                        digital_collection_id=entry.get("digital_collection_id") or "",
                        extra=extra_payload,
                    )
                    print(
                        f"[market-purchase] buying {index}/{buy_qty} consignOrderId={order_id} "
                        f"seller_uid={entry.get('seller_uid') or '-'}",
                        flush=True,
                    )
                    result = client_post_with_rate_limit_retry(
                        client,
                        purchase_path,
                        payload,
                        label=f"market-purchase {index}/{buy_qty}",
                    )
                    listing = entry
                    if is_success(result):
                        purchase_entry = finalize_purchase(
                            consign_id=order_id,
                            create_result=result,
                            seller=listing.get("seller_uid"),
                            dc_id=listing.get("digital_collection_id"),
                        )
                        ok = purchase_entry["ok"]
                    else:
                        ok = False
                        print(
                            f"[market-purchase] failed consignOrderId={order_id}: "
                            f"code={result.get('code')} message={result.get('message')}",
                            flush=True,
                        )
                        purchase_entry = {
                            "consign_order_id": order_id,
                            "seller_uid": listing.get("seller_uid"),
                            "digital_collection_id": listing.get("digital_collection_id"),
                            "ok": False,
                            "paid": False,
                            "result": result,
                        }
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                    results.append(purchase_entry)
                    report_task_progress(index, buy_qty)

                summary = (
                    f"{collection_name}点对点购买已完成，购买个数：{success_count}，失败个数：{fail_count}"
                )
                print(summary, flush=True)
                return {
                    "code": 0 if fail_count == 0 else 1,
                    "message": summary,
                    "summary": summary,
                    "collectionName": collection_name,
                    "successCount": success_count,
                    "failCount": fail_count,
                    "group_id": group_id,
                    "seller_uid": seller_uid or None,
                    "price_yuan": target_price_yuan,
                    "results": results,
                }

            operation = market_purchase_operation
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
                item_ids, list_error = list_unlocked_digital_collection_ids(client, group_id, limit=qty)
                if item_ids is None:
                    detail = ""
                    if isinstance(list_error, dict):
                        code = list_error.get("code")
                        msg = list_error.get("message") or list_error.get("error")
                        if code not in (None, "") or msg:
                            detail = f" (API code={code} message={msg})"
                    return {
                        "code": 1,
                        "error": f"failed to list unlocked collections for group {group_id}{detail}",
                        "group_id": group_id,
                        "display_name": display_name,
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
                    if index > 1 and CONSIGN_POST_INTERVAL_SEC > 0:
                        time.sleep(CONSIGN_POST_INTERVAL_SEC)
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
                    result = client_post_with_rate_limit_retry(
                        client,
                        consign_path,
                        payload,
                        label=f"consign-create {index}/{qty}",
                    )
                    ok = is_success(result)
                    create_order_id = extract_consign_order_id_from_result(result) if ok else None
                    consign_order_id = create_order_id
                    seller_entry = None
                    if ok:
                        seller_entry = resolve_seller_consign_entry_after_create(
                            client,
                            group_id,
                            digital_collection_id,
                            parsed.price,
                            create_order_id or "",
                        )
                        if seller_entry:
                            consign_order_id = seller_entry.get("display_id") or seller_entry.get("consign_order_id")
                    if ok and uid and (not seller_entry or not seller_entry.get("market_listing_id")):
                        market_listing_id = resolve_market_listing_id_after_create(
                            client,
                            config,
                            group_id,
                            uid,
                            create_order_id=create_order_id or "",
                            digital_collection_id=digital_collection_id,
                            price_yuan=parsed.price,
                            retries=4,
                            delay_sec=2.0,
                        )
                        if market_listing_id:
                            consign_order_id = market_listing_id
                    elif ok and not consign_order_id:
                        consign_order_id = resolve_consign_order_id_after_create(
                            client,
                            group_id,
                            digital_collection_id,
                            parsed.price,
                        )
                    if ok:
                        success_count += 1
                        if consign_order_id:
                            print(
                                f"[consign-create] ok digitalCollectionId={digital_collection_id} "
                                f"consignOrderId={consign_order_id}"
                                + (
                                    f" createOrderId={create_order_id}"
                                    if create_order_id and create_order_id != consign_order_id
                                    else ""
                                ),
                                flush=True,
                            )
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
                            "consign_order_id": consign_order_id,
                            "create_order_id": create_order_id,
                            "purchase_order_id": (
                                (seller_entry or {}).get("purchase_id")
                                or consign_order_id
                            ),
                            "ok": ok,
                            "result": result,
                        }
                    )
                    report_task_progress(index, qty)

                summary = (
                    f"{display_name}藏品寄售已完成，寄售个数：{success_count}，失败个数：{fail_count}"
                )
                print(summary, flush=True)
                consign_order_ids = []
                consign_pairs = []
                for entry in results:
                    if not entry.get("ok"):
                        continue
                    create_id = str(entry.get("create_order_id") or entry.get("consign_order_id") or "")
                    dc_id = str(entry.get("digitalCollectionId") or "")
                    if create_id and dc_id:
                        token = f"{create_id}|{dc_id}"
                        consign_pairs.append(
                            {
                                "order_id": create_id,
                                "digital_collection_id": dc_id,
                                "display_token": token,
                            }
                        )
                        consign_order_ids.append(token)
                    elif create_id:
                        consign_order_ids.append(create_id)
                purchase_order_ids = [
                    str(entry["purchase_order_id"])
                    for entry in results
                    if entry.get("ok") and entry.get("purchase_order_id")
                ]
                return {
                    "code": 0 if fail_count == 0 else 1,
                    "message": summary,
                    "summary": summary,
                    "collectionName": display_name,
                    "successCount": success_count,
                    "failCount": fail_count,
                    "consignOrderIds": consign_order_ids,
                    "consignPairs": consign_pairs,
                    "purchaseOrderIds": purchase_order_ids,
                    "group_id": group_id,
                    "seller_uid": str(uid or ""),
                    "price_yuan": float(parsed.price),
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
                    if index > 1 and CONSIGN_POST_INTERVAL_SEC > 0:
                        time.sleep(CONSIGN_POST_INTERVAL_SEC)
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
                    result = client_post_with_rate_limit_retry(
                        client,
                        cancel_path,
                        cancel_password_payload,
                        label=f"consign-cancel {index}/{len(consigned)}",
                    )
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
                    report_task_progress(index, len(consigned))

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
                target_qty = max(1, int(parsed.quantity))
                target_min_price_yuan = parsed.min_price
                target_min_price_fen = int(round(target_min_price_yuan * 100))
                consignment_password = parsed.consignment_password or ""
                collection_id_override = (parsed.collection_id or "").strip()
                payment_platform = parsed.payment_platform
                po_page_size = parsed.po_page_size
                po_max_pages = max(1, int(parsed.po_max_pages or 5))
                market_search_pages = max(1, int(parsed.market_search_pages or 1))
                market_segment_id = str(parsed.market_segment_id or "-1")
                poll_interval = parsed.poll_interval
                if poll_interval is None:
                    poll_interval = float(
                        get_command_default(
                            config,
                            "wanted-deal",
                            "poll_interval",
                            str(WANTED_DEAL_DEFAULT_POLL_INTERVAL_SEC),
                        )
                    )
                poll_interval = max(1.0, float(poll_interval))
                extra_payload = parse_payload_arg(parsed.payload) or {}
                dry_run = parsed.dry_run

                def wanted_deal_by_name_operation():
                    auth_token = getattr(client, "token", None) or (
                        (saved_session or {}).get("token") if saved_session else None
                    )
                    webview_headers = build_webview_api_headers(
                        auth_token,
                        origin="https://detail-page.ibox.art",
                        app_version=app_version,
                    )

                    def group_from_market_item(item: dict, source: str) -> dict | None:
                        name = first_present(item, ("name", "groupName", "collectionName", "title"))
                        if not name or (collection_name and not collection_name_matches(collection_name, str(name))):
                            return None
                        gid = first_present(item, ("id", "groupId", "collectionGroupId", "digitalCollectionGroupId"))
                        if gid in (None, ""):
                            return None
                        return {"group_id": str(gid), "name": str(name), "source": source}

                    def find_market_groups() -> list[dict]:
                        if group_id_override:
                            return [
                                {
                                    "group_id": group_id_override,
                                    "name": collection_name or f"group-{group_id_override}",
                                    "source": "cli",
                                }
                            ]
                        seen = set()
                        groups = []
                        for page_no in range(1, market_search_pages + 1):
                            path = (
                                "/public-service/markets"
                                f"?sortType=0&pageNo={page_no}&segmentId={market_segment_id}"
                                "&sortField=2&pageSize=50&timeRange=0"
                            )
                            result, api_error = client_get_with_rate_limit_retry(
                                client,
                                path,
                                label=f"wanted-deal market page={page_no}",
                            )
                            if result is None:
                                print(
                                    f"[wanted-deal] market page {page_no} failed: "
                                    f"code={(api_error or {}).get('code')} message={(api_error or {}).get('message')}",
                                    flush=True,
                                )
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

                    matched_groups = find_market_groups()
                    if not matched_groups:
                        return {
                            "code": 1,
                            "error": f"No public market group found matching name: {collection_name!r}",
                        }

                    print(
                        "[wanted-deal] matched group(s): "
                        + ", ".join(f"{g['name']}({g['group_id']})" for g in matched_groups),
                        flush=True,
                    )
                    print(
                        f"[wanted-deal] target quantity={target_qty} min_price>={target_min_price_yuan}yuan "
                        f"poll_interval={poll_interval:g}s",
                        flush=True,
                    )

                    if dry_run:
                        candidates: list[dict] = []
                        for group in matched_groups:
                            warmup_path = (
                                f"/public-market-service/digital-collection-groups/{group['group_id']}"
                                "/purchase-consignment-info?configType=0"
                            )
                            orders, list_error = list_group_wanted_purchase_orders(
                                client,
                                group["group_id"],
                                _uid,
                                page_size=po_page_size,
                                max_pages=po_max_pages,
                                webview_headers=webview_headers,
                                warmup_path=warmup_path,
                            )
                            if list_error and is_auth_failure(list_error):
                                return list_error
                            for order in orders:
                                candidate = wanted_deal_candidate_from_order(
                                    group,
                                    order,
                                    min_price_fen=target_min_price_fen,
                                    payment_platform=payment_platform,
                                )
                                if candidate:
                                    candidates.append(candidate)
                        candidates.sort(key=lambda c: (c["price_fen"] or 0), reverse=True)
                        print(f"[wanted-deal] dry-run: {len(candidates)} candidate(s)", flush=True)
                        for candidate in candidates[: min(len(candidates), 10)]:
                            print(
                                f"  purchase_order_id={candidate['purchase_order_id']} "
                                f"relation_id={candidate['relation_id']} "
                                f"price={format_price_yuan(candidate['price_fen'])}",
                                flush=True,
                            )
                        return {
                            "code": 0,
                            "dry_run": True,
                            "matched_groups": matched_groups,
                            "candidates": candidates,
                        }

                    if not consignment_password:
                        return {"code": 1, "error": "wanted-deal requires --consignment-password"}

                    remaining = target_qty
                    used_collection_ids: set[str] = set()
                    skip_deal_keys: set[str] = set()
                    skip_purchase_order_ids: set[str] = set()
                    collection_pools: dict[str, list[str]] = {}
                    deal_results: list[dict] = []
                    poll_attempt = 0
                    wait_interval = poll_interval
                    report_task_progress(0, target_qty)

                    while remaining > 0:
                        poll_attempt += 1
                        candidates = []
                        inventory_exhausted_round = False
                        for group in matched_groups:
                            gid = group["group_id"]
                            pool_ready, pool_error = ensure_wanted_deal_collection_pool(
                                client,
                                gid,
                                used_ids=used_collection_ids,
                                min_available=remaining,
                                pools=collection_pools,
                                force_refresh=True,
                            )
                            if pool_error and is_auth_failure(pool_error):
                                message = str(
                                    pool_error.get("message")
                                    or pool_error.get("error")
                                    or "登录已失效"
                                )
                                return {
                                    "code": 401,
                                    "message": message,
                                    "error": (
                                        f"list-owned-items auth failed: {message}. "
                                        "Saved session may have expired — pass SMS code to log in again."
                                    ),
                                    "deal_results": deal_results,
                                    "remaining": remaining,
                                    "matched_groups": matched_groups,
                                }
                            if not pool_ready:
                                print(
                                    f"[wanted-deal] no unlocked collections available in wallet "
                                    f"(remaining={remaining}); stopping.",
                                    flush=True,
                                )
                                inventory_exhausted_round = True
                                break
                            warmup_path = (
                                f"/public-market-service/digital-collection-groups/{gid}"
                                "/purchase-consignment-info?configType=0"
                            )
                            orders, list_error = list_group_wanted_purchase_orders(
                                client,
                                gid,
                                _uid,
                                page_size=po_page_size,
                                max_pages=po_max_pages,
                                webview_headers=webview_headers,
                                warmup_path=warmup_path,
                            )
                            if list_error and is_auth_failure(list_error):
                                return list_error
                            print(
                                f"[wanted-deal] scan group={gid} "
                                f"purchase_orders={len(orders)} (attempt {poll_attempt})",
                                flush=True,
                            )
                            for order in orders:
                                candidate = wanted_deal_candidate_from_order(
                                    group,
                                    order,
                                    min_price_fen=target_min_price_fen,
                                    payment_platform=payment_platform,
                                )
                                if not candidate:
                                    continue
                                po_id = candidate["purchase_order_id"]
                                if po_id in skip_purchase_order_ids:
                                    continue
                                deal_key = wanted_deal_relation_key(po_id, candidate["relation_id"])
                                if deal_key in skip_deal_keys:
                                    continue
                                candidates.append(candidate)

                        if inventory_exhausted_round:
                            break

                        candidates = dedupe_wanted_deal_candidates(candidates)
                        if len(candidates) > WANTED_DEAL_MAX_CANDIDATES_PER_ROUND:
                            print(
                                f"[wanted-deal] trying top {WANTED_DEAL_MAX_CANDIDATES_PER_ROUND}/"
                                f"{len(candidates)} unique purchase order(s) this round",
                                flush=True,
                            )
                            candidates = candidates[:WANTED_DEAL_MAX_CANDIDATES_PER_ROUND]

                        if not candidates:
                            print(
                                f"[wanted-deal] no buy orders with price>={target_min_price_yuan}yuan "
                                f"(remaining={remaining}); retry in {wait_interval:g}s",
                                flush=True,
                            )
                            time.sleep(wait_interval)
                            continue

                        print(
                            f"[wanted-deal] {len(candidates)} unique purchase order(s) at or above min price; "
                            f"remaining={remaining}",
                            flush=True,
                        )
                        progress_this_round = False
                        inventory_exhausted = False

                        for candidate in candidates:
                            if remaining <= 0:
                                break
                            po_id = candidate["purchase_order_id"]
                            if po_id in skip_purchase_order_ids:
                                continue
                            deal_key = wanted_deal_relation_key(po_id, candidate["relation_id"])
                            if deal_key in skip_deal_keys:
                                continue

                            collection_id, pick_error = resolve_wanted_deal_collection_id(
                                client,
                                candidate["group_id"],
                                override=collection_id_override,
                                used_ids=used_collection_ids,
                                pools=collection_pools,
                                min_available=remaining,
                            )
                            if pick_error and is_auth_failure(pick_error):
                                message = str(
                                    pick_error.get("message")
                                    or pick_error.get("error")
                                    or "登录已失效"
                                )
                                return {
                                    "code": 401,
                                    "message": message,
                                    "error": (
                                        f"list-owned-items auth failed: {message}. "
                                        "Saved session may have expired — pass SMS code to log in again."
                                    ),
                                    "deal_results": deal_results,
                                    "remaining": remaining,
                                    "matched_groups": matched_groups,
                                }
                            if not collection_id:
                                print(
                                    f"[wanted-deal] no unlocked collections left in wallet "
                                    f"(remaining={remaining}); stopping.",
                                    flush=True,
                                )
                                inventory_exhausted = True
                                break

                            deal_path = render_command_path(
                                config,
                                "wanted-deal",
                                "/order-create-service/advance-orders/{purchase_order_id}/relation/{relation_id}/deal?uid={uid}",
                                purchase_order_id=candidate["purchase_order_id"],
                                relation_id=candidate["relation_id"],
                                uid=_uid,
                            )
                            deal_payload = build_wanted_deal_payload(
                                payment_platform=int(candidate["payment_platform"]),
                                collection_id=collection_id,
                                consignment_password=consignment_password,
                                extra=extra_payload,
                            )
                            print(
                                f"[wanted-deal] dealing purchase_order_id={candidate['purchase_order_id']} "
                                f"relation_id={candidate['relation_id']} collection_id={collection_id} "
                                f"price={format_price_yuan(candidate['price_fen'])}",
                                flush=True,
                            )
                            deal_result = client_post_with_rate_limit_retry(
                                client,
                                deal_path,
                                deal_payload,
                                label="wanted-deal",
                            )
                            ok = is_success(deal_result)
                            entry = {
                                "purchase_order_id": candidate["purchase_order_id"],
                                "relation_id": candidate["relation_id"],
                                "collection_id": collection_id,
                                "payment_platform": candidate["payment_platform"],
                                "group_name": candidate["group_name"],
                                "price_fen": candidate["price_fen"],
                                "price_yuan": candidate["price_yuan"],
                                "deal": deal_result,
                                "ok": ok,
                            }
                            deal_results.append(entry)

                            if ok:
                                remaining -= 1
                                progress_this_round = True
                                mark_wanted_deal_collection_consumed(
                                    candidate["group_id"],
                                    collection_id,
                                    used_ids=used_collection_ids,
                                    pools=collection_pools,
                                )
                                skip_deal_keys.add(deal_key)
                                wait_interval = poll_interval
                                print(
                                    f"[wanted-deal] deal ok; progress {target_qty - remaining}/{target_qty}",
                                    flush=True,
                                )
                                report_task_progress(target_qty - remaining, target_qty)
                                if remaining > 0 and WANTED_DEAL_POST_INTERVAL_SEC > 0:
                                    time.sleep(WANTED_DEAL_POST_INTERVAL_SEC)
                            else:
                                err_code = deal_result.get("code")
                                err_msg = deal_result.get("message")
                                if is_wanted_deal_stale_order(deal_result):
                                    skip_purchase_order_ids.add(str(candidate["purchase_order_id"]))
                                    print(
                                        f"[wanted-deal] skip purchase_order_id={candidate['purchase_order_id']} "
                                        f"(stale: code={err_code} message={err_msg})",
                                        flush=True,
                                    )
                                else:
                                    skip_deal_keys.add(deal_key)
                                    print(
                                        f"[wanted-deal] deal failed purchase_order_id={candidate['purchase_order_id']}: "
                                        f"code={err_code} message={err_msg}",
                                        flush=True,
                                    )
                                if is_rate_limited(deal_result):
                                    wait_interval = min(max(wait_interval * 1.5, poll_interval), 30.0)
                                    print(
                                        f"[wanted-deal] rate limited; next scan in {wait_interval:g}s",
                                        flush=True,
                                    )
                                    break

                        if remaining <= 0:
                            break
                        if inventory_exhausted:
                            break
                        if not progress_this_round:
                            print(
                                f"[wanted-deal] no successful deal this round (remaining={remaining}); "
                                f"retry in {wait_interval:g}s",
                                flush=True,
                            )
                            time.sleep(wait_interval)

                    success_count = target_qty - remaining
                    unsold_count = remaining
                    attempt_fail_count = sum(1 for item in deal_results if not item.get("ok"))
                    if success_count >= target_qty:
                        outcome = "求购成交已完成"
                    elif success_count > 0:
                        outcome = (
                            f"求购部分完成（目标 {target_qty}，已卖出 {success_count}，"
                            f"未卖出 {unsold_count}）"
                        )
                    else:
                        outcome = "求购成交未完成"
                    summary = (
                        f"{collection_name or matched_groups[0]['name']}{outcome}，"
                        f"成交个数：{success_count}，未卖出个数：{unsold_count}"
                    )
                    if attempt_fail_count:
                        print(
                            f"[wanted-deal] stale/failed order attempts this run: {attempt_fail_count}",
                            flush=True,
                        )
                    print(summary, flush=True)
                    return {
                        "code": 0 if success_count >= target_qty else 1,
                        "message": summary,
                        "summary": summary,
                        "collectionName": collection_name or matched_groups[0]["name"],
                        "successCount": success_count,
                        "failCount": unsold_count,
                        "unsoldCount": unsold_count,
                        "attemptFailCount": attempt_fail_count,
                        "targetQuantity": target_qty,
                        "minPriceYuan": target_min_price_yuan,
                        "remaining": remaining,
                        "deal_results": deal_results,
                        "matched_groups": matched_groups,
                    }

                operation = wanted_deal_by_name_operation
            else:
                # ── direct ID mode ────────────────────────────────────────────
                if not parsed.purchase_order_id or not parsed.relation_id:
                    raise SystemExit(
                        "Error: wanted-deal requires either --collection-name or both positional "
                        "arguments purchase_order_id and relation_id"
                    )
                extra_payload = parse_payload_arg(parsed.payload) or {}
                deal_payload = {}
                if parsed.consignment_password:
                    deal_payload.update(
                        build_consign_password_fields(parsed.consignment_password)
                    )
                if (parsed.collection_id or "").strip():
                    deal_payload["digitalCollectionId"] = int(parsed.collection_id)
                deal_payload["paymentPlatformCode"] = int(parsed.payment_platform)
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
                operation = lambda: client_post_with_rate_limit_retry(
                    client,
                    path,
                    deal_payload,
                    label="wanted-deal direct",
                )
        elif cmd == "sale-rush":
            collection_name = (parsed.collection_name or "").strip()
            sale_id_override = (parsed.sale_id or "").strip()
            group_id_override = (parsed.group_id or "").strip()
            quantity = int(parsed.quantity or 0)
            payment_platform = (
                parsed.payment_platform
                if parsed.payment_platform is not None
                else int(get_command_default(config, "sale-rush", "payment_platform_code", "30"))
            )
            consignment_password = parsed.consignment_password or ""
            dry_run = parsed.dry_run
            extra_payload = parse_payload_arg(parsed.payload) or {}
            wait_for_start = bool(parsed.wait_for_start)
            retry_window = max(float(parsed.retry_window), 0.0)
            retry_interval = max(float(parsed.retry_interval), 0.05)

            if not consignment_password and not dry_run:
                raise SystemExit(
                    "Error: sale-rush requires --支付密码 / --consignment-password (for wallet payment after order create)"
                )

            def sale_rush_operation():
                auto_pick = bool(getattr(parsed, "auto", False)) or (
                    not collection_name and not sale_id_override and not group_id_override
                )
                resolved = resolve_sale_rush_target(
                    client,
                    sale_id=sale_id_override,
                    group_id=group_id_override,
                    collection_name=collection_name,
                    config=config,
                    auto=auto_pick,
                )
                if resolved.get("code") not in (None, 0):
                    return resolved

                if resolved.get("multi") and isinstance(resolved.get("targets"), list):
                    targets = resolved["targets"]
                else:
                    targets = [resolved]

                targets = [t for t in targets if isinstance(t, dict) and str(t.get("sale_id") or "")]
                if not targets:
                    return {"code": 1, "error": "could not resolve sale_id from sale-info"}

                buy_nums = [
                    resolve_sale_rush_buy_quantity(quantity, t.get("max_buy")) for t in targets
                ]
                total_qty = max(1, sum(buy_nums))

                def refresh_auto_targets() -> list[dict] | dict:
                    if not auto_pick:
                        return targets
                    refreshed = resolve_auto_sale_rush_targets(client)
                    if refreshed.get("code") not in (None, 0):
                        return refreshed
                    next_targets = refreshed.get("targets") or []
                    if not next_targets:
                        return {"code": 1, "error": "no auto sale targets after refresh"}
                    return next_targets

                earliest = None
                for target in targets:
                    on_sale_time = target.get("on_sale_time")
                    if isinstance(on_sale_time, datetime):
                        if earliest is None or on_sale_time < earliest:
                            earliest = on_sale_time

                if (
                    earliest is not None
                    and wait_for_start
                    and (earliest - datetime.now()).total_seconds() > 0
                ):
                    seconds_until = (earliest - datetime.now()).total_seconds()
                    print(
                        f"[sale-rush] earliest opens at {earliest.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"({seconds_until:.0f}s away); waiting…",
                        flush=True,
                    )
                    warmup_sale_rush(client)
                    _wait_until_start(earliest)
                    if auto_pick:
                        refreshed = refresh_auto_targets()
                        if isinstance(refreshed, dict) and refreshed.get("code") not in (None, 0):
                            return refreshed
                        if isinstance(refreshed, list) and refreshed:
                            targets = refreshed
                            buy_nums = [
                                resolve_sale_rush_buy_quantity(quantity, t.get("max_buy"))
                                for t in targets
                            ]
                            total_qty = max(1, sum(buy_nums))

                if dry_run:
                    dry_results = []
                    for target, buy_num in zip(targets, buy_nums):
                        sale_id = str(target.get("sale_id") or "")
                        name = str(target.get("collection_name") or collection_name or sale_id)
                        on_sale_time = target.get("on_sale_time")
                        dry_results.append(
                            {
                                "sale_id": sale_id,
                                "group_id": target.get("group_id"),
                                "collection_name": name,
                                "price_yuan": target.get("price_yuan"),
                                "quantity": buy_num,
                                "sale_status": to_int(target.get("sale_status")),
                                "on_sale_time": (
                                    on_sale_time.isoformat()
                                    if isinstance(on_sale_time, datetime)
                                    else None
                                ),
                                "payload": build_sale_order_payload(
                                    buy_num,
                                    payment_platform,
                                    extra=extra_payload,
                                ),
                            }
                        )
                    return {
                        "code": 0,
                        "dry_run": True,
                        "auto": auto_pick,
                        "multi": len(dry_results) > 1,
                        "results": dry_results,
                        **(dry_results[0] if len(dry_results) == 1 else {}),
                    }

                warmup_sale_rush(client)
                report_task_progress(0, total_qty)

                ibox_token = getattr(client, "token", None) or (
                    (saved_session or {}).get("token") if saved_session else None
                )

                results: list[dict] = []
                done_qty = 0

                for index, (target, buy_num) in enumerate(zip(targets, buy_nums), start=1):
                    sale_id = str(target.get("sale_id") or "")
                    name = str(target.get("collection_name") or collection_name or sale_id)
                    price_yuan = target.get("price_yuan")
                    sale_status = to_int(target.get("sale_status"))
                    on_sale_time = target.get("on_sale_time")

                    if isinstance(on_sale_time, datetime):
                        seconds_until = (on_sale_time - datetime.now()).total_seconds()
                        if seconds_until > 0 and not wait_for_start:
                            print(
                                f"[sale-rush] [{index}/{len(targets)}] sale_id={sale_id} name={name!r} "
                                f"opens at {on_sale_time.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"({seconds_until:.0f}s away) price={price_yuan}yuan num={buy_num}",
                                flush=True,
                            )
                            print(
                                "[sale-rush] --no-wait set, attempting order before official start…",
                                flush=True,
                            )
                        else:
                            print(
                                f"[sale-rush] [{index}/{len(targets)}] sale_id={sale_id} name={name!r} "
                                f"price={price_yuan}yuan num={buy_num} status={sale_status}",
                                flush=True,
                            )
                    else:
                        print(
                            f"[sale-rush] [{index}/{len(targets)}] sale_id={sale_id} name={name!r} "
                            f"price={price_yuan}yuan num={buy_num} status={sale_status}",
                            flush=True,
                        )

                    order_payload = build_sale_order_payload(
                        buy_num,
                        payment_platform,
                        extra=extra_payload,
                    )

                    create_result: dict | None = None
                    captcha_failed = False
                    max_captcha_rounds = 6

                    for captcha_round in range(1, max_captcha_rounds + 1):
                        captcha_params, captcha_err = resolve_geetest_captcha_params(
                            device_host=device_host,
                            captcha_mode=parsed.captcha_mode,
                            captcha_timeout=parsed.captcha_timeout,
                            captcha_id=parsed.captcha_id,
                            captcha_headed=False,
                            context=f"sale_id={sale_id} round={captcha_round}",
                            prefer_app=False,
                            sale_rush=True,
                            app_group_id=str(target.get("group_id") or ""),
                            app_sale_id=str(target.get("sale_id") or ""),
                            app_sale_link=str(target.get("link") or ""),
                            app_collection_name=str(target.get("collection_name") or ""),
                        )
                        if captcha_err:
                            results.append(
                                {
                                    "code": 1,
                                    "error": captcha_err,
                                    "paid": False,
                                    "sale_id": sale_id,
                                    "collection_name": name,
                                    "quantity": buy_num,
                                }
                            )
                            captcha_failed = True
                            break

                        order_deadline = time.monotonic() + retry_window
                        attempt = 0
                        captcha_rejected = False
                        create_result = None

                        while time.monotonic() <= order_deadline:
                            attempt += 1
                            order_path = build_sale_order_path(
                                config, sale_id, captcha_params or {}
                            )
                            create_result = client_post_with_rate_limit_retry(
                                client,
                                order_path,
                                order_payload,
                                label=(
                                    f"sale-rush sale_id={sale_id} "
                                    f"captcha_round={captcha_round} attempt={attempt}"
                                ),
                            )
                            if is_success(create_result):
                                break
                            if is_auth_failure(create_result):
                                return create_result
                            code = (
                                create_result.get("code")
                                if isinstance(create_result, dict)
                                else None
                            )
                            message = (
                                create_result.get("message")
                                if isinstance(create_result, dict)
                                else ""
                            )
                            if is_captcha_related_failure(create_result):
                                print(
                                    f"[sale-rush] captcha rejected code={code} "
                                    f"message={message!r}; re-solving…",
                                    flush=True,
                                )
                                captcha_rejected = True
                                break
                            print(
                                f"[sale-rush] create failed code={code} message={message!r}; "
                                f"retry in {retry_interval:.2f}s…",
                                flush=True,
                            )
                            if time.monotonic() + retry_interval > order_deadline:
                                break
                            time.sleep(retry_interval)

                        if isinstance(create_result, dict) and is_success(create_result):
                            break
                        if not captcha_rejected:
                            break

                    if captcha_failed:
                        continue

                    if not isinstance(create_result, dict) or not is_success(create_result):
                        results.append(
                            create_result
                            if isinstance(create_result, dict)
                            else {
                                "code": 1,
                                "error": "sale-rush create order failed",
                                "paid": False,
                                "sale_id": sale_id,
                                "collection_name": name,
                                "quantity": buy_num,
                            }
                        )
                        continue

                    total_yuan = (
                        float(price_yuan) * buy_num if price_yuan is not None else None
                    )
                    payment = complete_purchase_payment(
                        client,
                        create_result=create_result,
                        consignment_password=consignment_password,
                        ibox_token=str(ibox_token or ""),
                        app_version=app_version,
                        max_price_yuan=total_yuan,
                        config=config,
                        payment_initiator_type=0,
                        label=f"sale-rush sale_id={sale_id}",
                    )
                    paid = bool(payment.get("paid"))
                    if paid:
                        done_qty += buy_num
                        report_task_progress(done_qty, total_qty)
                    results.append(
                        {
                            "code": 0 if paid else 1,
                            "message": "ok" if paid else (payment.get("error") or "wallet payment failed"),
                            "paid": paid,
                            "sale_id": sale_id,
                            "group_id": target.get("group_id"),
                            "collection_name": name,
                            "price_yuan": price_yuan,
                            "quantity": buy_num,
                            "create": create_result,
                            "payment": payment,
                        }
                    )

                paid_count = sum(1 for item in results if item.get("paid"))
                any_paid = paid_count > 0
                total_bought = sum(int(item.get("quantity") or 0) for item in results if item.get("paid"))
                summary = (
                    f"首发抢购完成 {paid_count}/{len(results)} 个活动，"
                    f"共购买 {total_bought} 件"
                )
                print(f"[sale-rush] {summary}", flush=True)
                payload = {
                    "code": 0 if any_paid else 1,
                    "message": summary,
                    "summary": summary,
                    "paid": any_paid,
                    "auto": auto_pick,
                    "multi": len(results) > 1,
                    "results": results,
                    "paidCount": paid_count,
                    "totalQuantity": total_bought,
                }
                if len(results) == 1:
                    payload.update(results[0])
                return payload

            operation = sale_rush_operation
        elif cmd == "wanted-buy":
            if not uid:
                raise SystemExit(
                    "Error: uid is required for wanted-buy. Pass --uid or ensure login response contains uid"
                )
            collection_name = (parsed.collection_name or "").strip()
            group_id_override = (parsed.group_id or "").strip()
            price_yuan = parsed.price
            quantity = parsed.quantity
            payment_platform = (
                parsed.payment_platform
                if parsed.payment_platform is not None
                else int(get_command_default(config, "wanted-buy", "payment_platform_code", "30"))
            )
            consignment_password = parsed.consignment_password or ""
            dry_run = parsed.dry_run
            extra_payload = parse_payload_arg(parsed.payload) or {}
            if not consignment_password and not dry_run:
                raise SystemExit(
                    "Error: wanted-buy requires --支付密码 / --consignment-password (for prepayment after order create)"
                )

            def resolve_wanted_buy_group_id() -> str | dict:
                if group_id_override:
                    return group_id_override
                if not collection_name:
                    return {
                        "code": 1,
                        "error": "wanted-buy requires --group-id or --collection-name",
                    }
                group_id = resolve_group_id_for_market(client, collection_name, config)
                if isinstance(group_id, dict):
                    return group_id
                print(
                    f"[wanted-buy] resolved group_id={group_id} name={collection_name!r}",
                    flush=True,
                )
                return group_id

            def wanted_buy_operation():
                group_id = resolve_wanted_buy_group_id()
                if isinstance(group_id, dict):
                    return group_id

                buy_payload = build_wanted_buy_payload(
                    group_id,
                    price_yuan,
                    quantity,
                    payment_platform,
                    extra=extra_payload,
                )

                if dry_run:
                    return {
                        "code": 0,
                        "dry_run": True,
                        "group_id": group_id,
                        "price_yuan": price_yuan,
                        "buy_count": quantity,
                        "payload": buy_payload,
                    }

                ibox_token = getattr(client, "token", None) or (
                    (saved_session or {}).get("token") if saved_session else None
                )
                buy_path = render_command_path(
                    config,
                    "wanted-buy",
                    "/order-create-service/advance-orders?uid={uid}",
                    uid=uid,
                )
                print(
                    f"[wanted-buy] group_id={group_id} price={price_yuan}yuan "
                    f"buyCount={quantity} paymentPlatformCode={payment_platform}",
                    flush=True,
                )
                warmup_wanted_buy(client, group_id)
                report_task_progress(0, max(1, int(quantity)))
                create_result = client_post_with_rate_limit_retry(
                    client,
                    buy_path,
                    buy_payload,
                    label="wanted-buy",
                )
                if not is_success(create_result):
                    return create_result

                total_yuan = float(price_yuan) * max(1, int(quantity))
                payment = complete_purchase_payment(
                    client,
                    create_result=create_result,
                    consignment_password=consignment_password,
                    ibox_token=str(ibox_token or ""),
                    app_version=app_version,
                    max_price_yuan=total_yuan,
                    config=config,
                    payment_initiator_type=1,
                    label="wanted-buy",
                )
                paid = bool(payment.get("paid"))
                if paid:
                    report_task_progress(max(1, int(quantity)), max(1, int(quantity)))
                return {
                    "code": 0 if paid else 1,
                    "message": "ok" if paid else (payment.get("error") or "prepayment failed"),
                    "paid": paid,
                    "group_id": group_id,
                    "price_yuan": price_yuan,
                    "buy_count": quantity,
                    "purchase_order_id": payment.get("purchase_order_id"),
                    "cashierLink": payment.get("cashierLink"),
                    "create": create_result,
                    "payment": payment,
                }

            operation = wanted_buy_operation
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
