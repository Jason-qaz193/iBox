#!/usr/bin/env python3
"""
QQ bot bridge for iBox CLI via OneBot v11.

Works with NapCat, Lagrange.OneBot, LLOneBot, etc.

Usage:
  1. Copy config/qq_bot.example.yaml -> config/qq_bot.yaml
  2. Start your OneBot implementation (NapCat recommended)
  3. python qq_bot.py

Chat commands (private or allowed group):
  帮助
  登录 13800138000 123456
  寄售 13800138000 - 123456 藏品名 99 1
  下架 13800138000 - 123456 藏品名 99 1
  求购 13800138000 - 123456 藏品名 6 6
  捡漏 13800138000 - 123456 藏品名 5000 1
  点对点 13800138000 - 123456 藏品名 5 1 orderId|藏品ID
  合成 13800138000 - 3
  首发 13800138000 - 123456 藏品名 1

If default_mobile / default_pay_password are set in config/qq_bot.yaml,
you can omit mobile/password, e.g.:
  寄售 - 藏品名 99 1
  寄售 藏品名 99 1
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
import yaml

try:
    import websockets
except ImportError as exc:
    raise SystemExit("Missing dependency: pip install websockets") from exc

ROOT = Path(__file__).resolve().parent
MOBILE_RE = re.compile(r"^1\d{10}$")
CODE_RE = re.compile(r"^-|\d{4,8}$")


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def decode_subprocess_bytes(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def task_collection_display_name(ctx: TaskContext, result: dict | None = None) -> str:
    """Prefer the name parsed from the QQ command; subprocess JSON may be mojibake on Windows."""
    name = (ctx.collection_name or "").strip()
    if name:
        return name
    if isinstance(result, dict):
        fallback = str(result.get("collectionName") or "").strip()
        if fallback:
            return fallback
    return "该藏品"
PROGRESS_LINE_RES = (
    re.compile(r"\[ibox-progress\]\s*(\d+)/(\d+)"),
    re.compile(r"\[consign-create\]\s+posting\s+(\d+)/(\d+)"),
    re.compile(r"\[consign-cancel\]\s+cancel\s+(\d+)/(\d+)"),
    re.compile(r"\[market-purchase\]\s+buying\s+(\d+)/(\d+)"),
)
SYNTHESIS_BATCH_RE = re.compile(r"\[ibox-synthesis-batch\]\s*(\{.*\})\s*$")
TASK_ACTIONS = frozenset(
    {
        "consign-create",
        "consign-cancel",
        "wanted-deal",
        "wanted-buy",
        "market-buy",
        "market-purchase",
        "synthesis-auto",
        "sale-rush",
    }
)
# Long-running poll loops; 0 = no limit (see command_timeout in qq_bot.yaml).
UNLIMITED_QQ_TASK_TIMEOUT_ACTIONS = frozenset({"synthesis-auto", "market-buy"})

TASK_START_META = {
    "consign-create": {
        "collection_label": "当前投递藏品",
        "price_label": "投递价格",
        "verb": "寄售",
    },
    "consign-cancel": {
        "collection_label": "当前下架藏品",
        "price_label": "下架价格",
        "verb": "下架",
    },
    "wanted-deal": {
        "collection_label": "当前成交藏品",
        "price_label": "最低成交价",
        "verb": "求购成交",
    },
    "wanted-buy": {
        "collection_label": "当前发求购藏品",
        "price_label": "求购出价",
        "verb": "发求购",
    },
    "market-buy": {
        "collection_label": "当前捡漏藏品",
        "price_label": "捡漏价格",
        "verb": "捡漏",
    },
    "market-purchase": {
        "collection_label": "当前购买藏品",
        "price_label": "购买价格",
        "verb": "点对点",
    },
    "synthesis-auto": {
        "verb": "合成",
    },
    "sale-rush": {
        "collection_label": "当前抢购藏品",
        "price_label": "首发价格",
        "verb": "首发抢购",
    },
}

TASK_DONE_META = {
    "consign-create": {"label": "寄售完成了", "count_label": "寄售个数"},
    "consign-cancel": {"label": "下架完成了", "count_label": "下架个数"},
    "wanted-deal": {"label": "求购成交完成了", "count_label": "成交个数", "unsold_label": "未卖出个数"},
    "wanted-buy": {"label": "发求购完成了", "count_label": "求购数量"},
    "market-buy": {"label": "捡漏完成了", "count_label": "购买数量"},
    "market-purchase": {"label": "点对点完成了", "count_label": "购买个数"},
    "synthesis-auto": {"label": "合成完成了", "count_label": "合成次数"},
    "sale-rush": {"label": "首发抢购完成了", "count_label": "购买数量"},
}


@dataclass
class TaskContext:
    action: str
    mobile: str
    collection_name: str = ""
    price: float | None = None
    quantity: int | None = None
    target_count: int | None = None
    consign_order_id: str = ""
    digital_collection_id: str = ""


HELP_TEXT = """iBox QQ 指令帮助

登陆-手机号-验证码
寄售-手机号-验证码-寄售密码-藏品名-价格-数量
下架-手机号-验证码-支付密码-藏品名-价格-数量
求购-手机号-验证码-寄售密码-藏品名-最低成交价-数量
捡漏-手机号-验证码-支付密码-藏品名-价格-数量
点对点-手机号-验证码-支付密码-藏品名-价格-数量-寄售ID|藏品ID
点对点-手机号-验证码-支付密码-藏品名-价格-数量-寄售ID1|藏品ID1、寄售ID2|藏品ID2
合成-手机号-验证码-合成数量
首发-手机号-验证码-支付密码-藏品名-数量

说明：
- 参数用 - 分隔；也支持用空格分隔（验证码可写 - 表示复用已保存 session）
- 点对点：填写 B 寄售成功后返回的「寄售ID|藏品ID」；多个用 、 连接可一次买完
- 批量扫货：若 B 独占某价位，A 可用「捡漏」按价格+数量批量买，无需逐个寄售ID
- 全自动：config/qq_bot.yaml 配置 p2p_auto 后，B 寄售成功会自动触发 A 点对点购买
- 求购：把仓库藏品卖给市场上已有的求购单
- 点对点流程：B「寄售」→ A「点对点」（可手动粘贴 ID，或开启 p2p_auto 全自动）
- 合成数量可省略，省略则尽量全部合成；材料用尽或活动结束时会推送本轮结果并继续监控新活动
- 首发/优先购：按藏品名匹配首发活动，开售后自动下单并钱包支付
- 藏品名含空格请用引号，例如：寄售-123456-"2026喜糖熊猫"-199-1
"""


def load_bot_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "qq_bot.yaml"
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Copy config/qq_bot.example.yaml to config/qq_bot.yaml first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def tokenize_message(text: str) -> list[str]:
    text = (text or "").strip()
    text = re.sub(r"^\[@\d+\s*\]", "", text).strip()
    if not text:
        return []

    # 寄售-手机号-验证码-... 整行用 - 分隔（与帮助格式一致）
    if "-" in text and not re.search(r"\s", text):
        parts = [part.strip() for part in text.split("-") if part.strip()]
        if len(parts) >= 2:
            return parts

    tokens: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None

    def flush():
        nonlocal buf
        if buf:
            tokens.append("".join(buf))
            buf = []

    for ch in text:
        if in_quote:
            if ch == in_quote:
                in_quote = None
            else:
                buf.append(ch)
            continue
        if ch in ('"', "'") or ch in ("\u201c", "\u201d"):
            flush()
            in_quote = '"'
            continue
        if ch.isspace():
            flush()
            continue
        buf.append(ch)
    flush()
    return tokens


def parse_trade_args(tokens: list[str], start: int, defaults: dict) -> tuple[str, str, str, str, float, int, int]:
    idx = start
    mobile = defaults.get("default_mobile") or ""
    code = defaults.get("default_code", "-")
    password = defaults.get("default_pay_password") or ""

    if idx < len(tokens) and MOBILE_RE.fullmatch(tokens[idx]):
        mobile = tokens[idx]
        idx += 1
    if idx < len(tokens) and CODE_RE.fullmatch(tokens[idx]):
        code = tokens[idx]
        idx += 1
    if idx < len(tokens) and re.fullmatch(r"\d{4,12}", tokens[idx]):
        password = tokens[idx]
        idx += 1

    if len(tokens) - idx < 3:
        raise ValueError("参数不足，需要：藏品名 价格 数量")

    qty = int(tokens[-1])
    price = float(tokens[-2])
    name = " ".join(tokens[idx:-2]).strip()
    if not mobile:
        raise ValueError("缺少手机号，请在命令中提供或在 config/qq_bot.yaml 设置 default_mobile")
    if not password:
        raise ValueError("缺少支付密码，请在命令中提供或在 config/qq_bot.yaml 设置 default_pay_password")
    if not name:
        raise ValueError("缺少藏品名")
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    return mobile, code, password, name, price, qty, idx


def is_consign_order_id_token(value: str) -> bool:
    if MOBILE_RE.fullmatch(value):
        return False
    if re.fullmatch(r"\d{5,12}", value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return True
    return False


def is_digital_collection_id_token(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", value))


def is_consign_ref_bundle(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "|" in text or "、" in text or "," in text or ";" in text:
        return True
    return is_consign_order_id_token(text)


def resolve_p2p_auto_buyer(seller_mobile: str, config: dict) -> dict | None:
    auto = config.get("p2p_auto")
    if not auto:
        return None
    seller = str(seller_mobile or "").strip()
    if not seller:
        return None
    if isinstance(auto, dict):
        entry = auto.get(seller)
        if isinstance(entry, str) and entry.strip():
            return {"buyer_mobile": entry.strip()}
        if isinstance(entry, dict):
            return entry
    if isinstance(auto, list):
        for item in auto:
            if not isinstance(item, dict):
                continue
            if str(item.get("seller") or item.get("seller_mobile") or "").strip() == seller:
                return item
    return None


def build_p2p_auto_purchase_cmd(
    *,
    buyer_mobile: str,
    buyer_code: str,
    buyer_password: str,
    collection_name: str,
    price: float,
    consign_ids: list[str],
) -> list[str]:
    qty = max(1, len(consign_ids))
    run_cmd = [
        "market-purchase",
        buyer_mobile,
        buyer_code,
        "--支付密码",
        buyer_password,
        "--collection-name",
        collection_name,
        "--price",
        str(price),
        "--quantity",
        str(qty),
    ]
    if consign_ids:
        run_cmd += ["--consign-order-id", "、".join(str(item) for item in consign_ids)]
    return run_cmd


def parse_direct_purchase_args(
    tokens: list[str], start: int, defaults: dict
) -> tuple[str, str, str, str, float, int, str, str, int]:
    idx = start
    mobile = defaults.get("default_mobile") or ""
    code = defaults.get("default_code", "-")
    password = defaults.get("default_pay_password") or ""

    if idx < len(tokens) and MOBILE_RE.fullmatch(tokens[idx]):
        mobile = tokens[idx]
        idx += 1
    if idx < len(tokens) and CODE_RE.fullmatch(tokens[idx]):
        code = tokens[idx]
        idx += 1
    if idx < len(tokens) and re.fullmatch(r"\d{4,12}", tokens[idx]):
        password = tokens[idx]
        idx += 1

    tail = tokens[idx:]
    if len(tail) < 3:
        raise ValueError("参数不足，需要：藏品名 价格 数量 [寄售ID|藏品ID]")
    consign_order_id = ""
    digital_collection_id = ""
    if len(tail) >= 4:
        last = tail[-1]
        if is_consign_ref_bundle(last):
            consign_order_id = last.strip()
            tail = tail[:-1]
        elif (
            len(tail) >= 5
            and is_digital_collection_id_token(last)
            and is_consign_order_id_token(tail[-2])
        ):
            digital_collection_id = last
            consign_order_id = tail[-2]
            tail = tail[:-2]
        elif is_consign_order_id_token(last):
            consign_order_id = last
            tail = tail[:-1]
    if len(tail) < 3:
        raise ValueError("参数不足，需要：藏品名 价格 数量 [寄售ID|藏品ID]")

    qty = int(tail[-1])
    price = float(tail[-2])
    name = " ".join(tail[:-2]).strip()
    if not mobile:
        raise ValueError("缺少手机号，请在命令中提供或在 config/qq_bot.yaml 设置 default_mobile")
    if not password:
        raise ValueError("缺少支付密码，请在命令中提供或在 config/qq_bot.yaml 设置 default_pay_password")
    if not name:
        raise ValueError("缺少藏品名")
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    return mobile, code, password, name, price, qty, consign_order_id, digital_collection_id, idx


def parse_synthesis_args(
    tokens: list[str], start: int, defaults: dict
) -> tuple[str, str, int | None]:
    idx = start
    mobile = defaults.get("default_mobile") or ""
    code = defaults.get("default_code", "-")
    target_count: int | None = None

    if idx < len(tokens) and MOBILE_RE.fullmatch(tokens[idx]):
        mobile = tokens[idx]
        idx += 1
    if idx < len(tokens) and CODE_RE.fullmatch(tokens[idx]):
        code = tokens[idx]
        idx += 1
    if idx < len(tokens):
        if len(tokens) - idx != 1:
            raise ValueError("用法：合成 [手机号] [验证码|-] [合成数量]")
        try:
            target_count = int(tokens[idx])
        except ValueError as exc:
            raise ValueError("合成数量必须是正整数") from exc
        if target_count <= 0:
            raise ValueError("合成数量必须大于 0")

    if not mobile:
        raise ValueError("缺少手机号，请在命令中提供或在 config/qq_bot.yaml 设置 default_mobile")
    return mobile, code, target_count


def parse_sale_rush_args(
    tokens: list[str], start: int, defaults: dict
) -> tuple[str, str, str, str, int]:
    idx = start
    mobile = defaults.get("default_mobile") or ""
    code = defaults.get("default_code", "-")
    password = defaults.get("default_pay_password") or ""

    if idx < len(tokens) and MOBILE_RE.fullmatch(tokens[idx]):
        mobile = tokens[idx]
        idx += 1
    if idx < len(tokens) and CODE_RE.fullmatch(tokens[idx]):
        code = tokens[idx]
        idx += 1
    if idx < len(tokens) and re.fullmatch(r"\d{4,12}", tokens[idx]):
        password = tokens[idx]
        idx += 1

    if len(tokens) - idx < 2:
        raise ValueError("参数不足，需要：藏品名 数量")

    qty = int(tokens[-1])
    name = " ".join(tokens[idx:-1]).strip()
    if not mobile:
        raise ValueError("缺少手机号，请在命令中提供或在 config/qq_bot.yaml 设置 default_mobile")
    if not password:
        raise ValueError("缺少支付密码，请在命令中提供或在 config/qq_bot.yaml 设置 default_pay_password")
    if not name:
        raise ValueError("缺少藏品名")
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    return mobile, code, password, name, qty


def parse_command(text: str, defaults: dict) -> tuple[str, list[str] | None, TaskContext | None]:
    tokens = tokenize_message(text)
    if not tokens:
        return "empty", None, None

    cmd = tokens[0].lower()
    alias = {
        "help": "help",
        "帮助": "help",
        "?": "help",
        "sms": "sms",
        "验证码": "sms",
        "login": "login",
        "登录": "login",
        "登陆": "login",
        "consign-create": "consign-create",
        "寄售": "consign-create",
        "consign-cancel": "consign-cancel",
        "下架": "consign-cancel",
        "wanted-deal": "wanted-deal",
        "求购": "wanted-deal",
        "卖求购": "wanted-deal",
        "wanted-buy": "wanted-buy",
        "发求购": "wanted-buy",
        "market-buy": "market-buy",
        "捡漏": "market-buy",
        "market-purchase": "market-purchase",
        "点对点": "market-purchase",
        "直购": "market-purchase",
        "购买": "market-purchase",
        "synthesis-auto": "synthesis-auto",
        "合成": "synthesis-auto",
        "sale-rush": "sale-rush",
        "首发": "sale-rush",
        "优先购": "sale-rush",
        "抢购": "sale-rush",
    }
    action = alias.get(cmd, cmd)

    if action == "help":
        return action, None, None

    if action == "sms":
        if len(tokens) < 2:
            raise ValueError("用法：验证码 <手机号>")
        return action, ["sms", tokens[1]], None

    if action == "login":
        if len(tokens) < 3:
            raise ValueError("用法：登录 <手机号> <验证码>")
        return action, ["login", tokens[1], tokens[2]], None

    if action in {"consign-create", "consign-cancel"}:
        mobile, code, password, name, price, qty, _ = parse_trade_args(tokens, 1, defaults)
        run_cmd = [
            action,
            mobile,
            code,
            "--支付密码",
            password,
            "--藏品名字",
            name,
        ]
        if action == "consign-create":
            run_cmd += ["--出售价格", str(price), "--出售数量", str(qty)]
        else:
            run_cmd += ["--下架价格", str(price), "--下架数量", str(qty)]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            price=price,
            quantity=qty,
        )
        return action, run_cmd, ctx

    if action == "wanted-deal":
        mobile, code, password, name, price, qty, _ = parse_trade_args(tokens, 1, defaults)
        run_cmd = [
            "wanted-deal",
            mobile,
            code,
            "--collection-name",
            name,
            "--min-price",
            str(price),
            "--quantity",
            str(qty),
            "--consignment-password",
            password,
        ]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            price=price,
            quantity=qty,
        )
        return action, run_cmd, ctx

    if action == "wanted-buy":
        mobile, code, password, name, price, qty, _ = parse_trade_args(tokens, 1, defaults)
        run_cmd = [
            "wanted-buy",
            mobile,
            code,
            "--collection-name",
            name,
            "--price",
            str(price),
            "--quantity",
            str(qty),
            "--支付密码",
            password,
        ]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            price=price,
            quantity=qty,
        )
        return action, run_cmd, ctx

    if action == "market-buy":
        mobile, code, password, name, price, qty, _ = parse_trade_args(tokens, 1, defaults)
        run_cmd = [
            "market-buy",
            mobile,
            code,
            "--支付密码",
            password,
            "--collection-name",
            name,
            "--price",
            str(price),
            "--quantity",
            str(qty),
        ]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            price=price,
            quantity=qty,
        )
        return action, run_cmd, ctx

    if action == "market-purchase":
        mobile, code, password, name, price, qty, consign_order_id, digital_collection_id, _ = (
            parse_direct_purchase_args(tokens, 1, defaults)
        )
        purchase_ref = consign_order_id
        if consign_order_id and digital_collection_id:
            purchase_ref = f"{consign_order_id}|{digital_collection_id}"
        run_cmd = [
            "market-purchase",
            mobile,
            code,
            "--支付密码",
            password,
            "--collection-name",
            name,
            "--price",
            str(price),
            "--quantity",
            str(qty),
        ]
        if purchase_ref:
            run_cmd += ["--consign-order-id", purchase_ref]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            price=price,
            quantity=qty,
            consign_order_id=consign_order_id,
            digital_collection_id=digital_collection_id,
        )
        return action, run_cmd, ctx

    if action == "synthesis-auto":
        mobile, code, target_count = parse_synthesis_args(tokens, 1, defaults)
        run_cmd = ["synthesis-auto", mobile, code]
        if target_count is not None:
            run_cmd += ["--target-count", str(target_count)]
        ctx = TaskContext(action=action, mobile=mobile, target_count=target_count)
        return action, run_cmd, ctx

    if action == "sale-rush":
        mobile, code, password, name, qty = parse_sale_rush_args(tokens, 1, defaults)
        run_cmd = [
            "sale-rush",
            mobile,
            code,
            "--支付密码",
            password,
            "--collection-name",
            name,
            "--quantity",
            str(qty),
        ]
        ctx = TaskContext(
            action=action,
            mobile=mobile,
            collection_name=name,
            quantity=qty,
        )
        return action, run_cmd, ctx

    raise ValueError(f"未知指令：{tokens[0]}，发送「帮助」查看用法")


def format_login_success_line(mobile: str, *, plain: bool = False) -> str:
    return "登陆成功" if plain else f"{mobile}-登录成功"


def extract_json_result(output: str) -> dict | None:
    text = (output or "").strip()
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        try:
            obj, end = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


def format_login_reply(data: dict | None, raw: str, exit_code: int) -> str:
    if exit_code != 0:
        return format_result(data, raw, exit_code)
    if isinstance(data, dict):
        login = data.get("login")
        if login is None and data.get("code") == 0 and isinstance(data.get("data"), dict):
            login = data
        if isinstance(login, dict) and login.get("code") == 0:
            return format_login_success_line("", plain=True)
    return format_result(data, raw, exit_code)


def format_price(price: float | None) -> str:
    if price is None:
        return "-"
    if price == int(price):
        return str(int(price))
    return str(price)


def format_task_start(ctx: TaskContext) -> str:
    meta = TASK_START_META[ctx.action]
    line1 = format_login_success_line(ctx.mobile)
    if ctx.action == "synthesis-auto":
        count_label = str(ctx.target_count) if ctx.target_count is not None else "全部"
        line2 = (
            f"当前合成任务：数量={count_label}，将持续监控合成活动；"
            "材料用尽或活动结束时会自动推送合成藏品名称与数量。"
        )
    elif ctx.action == "market-purchase" and ctx.consign_order_id:
        ref = ctx.consign_order_id
        if ctx.digital_collection_id:
            ref = f"{ctx.consign_order_id}|{ctx.digital_collection_id}"
        line2 = (
            f"{meta['collection_label']}：{ctx.collection_name}，"
            f"{meta['price_label']}：{format_price(ctx.price)}，"
            f"任务数量：{ctx.quantity}，寄售信息：{ref}，"
            "任务已开始，请耐心等待推送！"
        )
    else:
        line2 = (
            f"{meta['collection_label']}：{ctx.collection_name}，"
            f"{meta['price_label']}：{format_price(ctx.price)}，"
            f"任务数量：{ctx.quantity}，任务已开始，请耐心等待推送！"
        )
    line3 = (
        f"预计在30秒后开始{meta['verb']}，请核对信息！"
        "如有错误请立马顶号！开始后因填写有误概不负责！"
    )
    return f"{line1}\n{line2}\n{line3}"


class TaskProgressTracker:
    def __init__(self) -> None:
        self._reported: set[int] = set()

    @staticmethod
    def milestone_threshold(total: int, decile: int) -> int:
        return max(1, (total * decile + 9) // 10)

    def feed(self, line: str) -> tuple[int, int, int] | None:
        done: int | None = None
        total: int | None = None
        for pattern in PROGRESS_LINE_RES:
            match = pattern.search(line)
            if match:
                done = int(match.group(1))
                total = int(match.group(2))
                break
        if done is None or total is None or total <= 0:
            return None

        latest_decile = 0
        for decile in range(1, 11):
            if decile in self._reported:
                continue
            if done >= self.milestone_threshold(total, decile):
                self._reported.add(decile)
                latest_decile = decile
        if latest_decile:
            return done, total, latest_decile
        return None


def format_synthesis_batch_reply(ctx: TaskContext, payload: dict) -> str:
    mobile = ctx.mobile
    reason = str(payload.get("reason") or "")
    total = int(payload.get("total_submitted") or 0)
    reward_totals = payload.get("reward_totals") or []
    rewards = payload.get("rewards") or []

    def _format_reward_names() -> str:
        if reward_totals:
            parts: list[str] = []
            for item in reward_totals[:10]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                count = int(item.get("count") or 0)
                if not name:
                    continue
                parts.append(f"{name}×{count}" if count > 1 else name)
            if parts:
                return "、".join(parts)
        if rewards:
            text = "、".join(str(item) for item in rewards[:10])
            if len(rewards) > 10:
                text += "…"
            return text or "无"
        return "无"

    if payload.get("terminal"):
        reason = str(payload.get("reason") or "")
        if reason == "activity_ended":
            status = "合成活动已结束"
        elif reason == "no_materials":
            status = "仓库材料已全部合成"
        elif reason == "target_reached":
            status = "已达到目标合成数量"
        else:
            status = "合成任务已完成"
        return (
            f"{mobile}-合成完成了\n"
            f"藏品名称：{_format_reward_names()}\n"
            f"合成数量：{total}\n"
            f"（{status}）"
        )

    batch_times = int(payload.get("batch_times") or 0)
    batch_count = int(payload.get("batch_submitted_count") or 0)
    target = payload.get("target_count")
    target_label = str(target) if target is not None else "全部"
    reward_text = _format_reward_names()

    if reason == "target_reached":
        headline = f"{mobile}-本轮合成完成"
        detail = f"本轮合成 {batch_times} 次（目标 {target_label}），累计 {total} 次"
    elif reason == "no_materials":
        headline = f"{mobile}-本轮暂无可合成材料"
        detail = f"仓库材料不足或未开放，累计已成功 {total} 次；将继续监控新活动"
    elif reason == "activity_ended":
        headline = f"{mobile}-本轮合成活动已结束"
        detail = f"累计已成功 {total} 次；将继续监控新活动"
    else:
        headline = f"{mobile}-合成阶段汇报"
        detail = f"本轮成功 {batch_count} 次，累计 {total} 次"

    return f"{headline}\n{detail}\n获得：{reward_text}\n（合成任务仍在后台继续运行）"


def format_task_progress(ctx: TaskContext, done: int, total: int, decile: int) -> str:
    verb = TASK_START_META[ctx.action]["verb"]
    pct = decile * 10
    return f"{ctx.mobile}-{verb}进度：已完成 {done}/{total}（{pct}%）"


def _task_result_dict(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    return result if isinstance(result, dict) else None


def _extract_similar_collections(error_text: str) -> list[str]:
    marker = "similar owned collections:"
    if marker not in error_text:
        return []
    tail = error_text.split(marker, 1)[1].strip()
    if not tail:
        return []
    if tail.startswith("("):
        tail = tail[1:]
    if tail.endswith(")"):
        tail = tail[:-1]
    return [part.strip() for part in tail.split("|") if part.strip()]


def explain_task_failure(ctx: TaskContext, data: dict | None, exit_code: int) -> str:
    mobile = ctx.mobile
    verb = TASK_START_META[ctx.action]["verb"]
    result = _task_result_dict(data)
    login = data.get("login") if isinstance(data, dict) else None

    if isinstance(login, dict) and login.get("code") not in (None, 0):
        login_msg = str(login.get("message") or login.get("code") or "")
        if "session" in login_msg.lower() or "验证码" in login_msg or login.get("code") in (401, "401"):
            reason = "登录失败，验证码无效或 session 已过期。"
            tip = "请先在 App 获取新验证码，发送「登陆-手机号-验证码」重新登录后再执行任务。"
        else:
            reason = "登录失败，账号未能通过验证。"
            tip = "请检查手机号、验证码是否正确，或重新登录后再试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    error = str(result.get("error") or "") if isinstance(result, dict) else ""
    message = str(result.get("message") or result.get("msg") or "") if isinstance(result, dict) else ""
    code = result.get("code") if isinstance(result, dict) else None
    combined = f"{error} {message}".strip()
    combined_lower = combined.lower()

    if "found " in combined_lower and "on market" in combined_lower and "failed to list" in combined_lower:
        reason = f"已找到「{ctx.collection_name or '该藏品'}」的市场信息，但无法读取您钱包中的藏品列表。"
        if "10002" in combined or "过于频繁" in combined:
            tip = "接口限流，请等待 1～2 分钟后重试。"
        else:
            tip = "请确认 App 中该藏品是否正常显示，检查网络与 RPC 连接后重试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "no owned collection group matching" in combined_lower:
        similar = _extract_similar_collections(error)
        name = ctx.collection_name or "该藏品"
        reason = f"钱包中未找到与「{name}」匹配的藏品系列。"
        if similar:
            tip = f"请核对 App 内藏品名称是否完全一致，可尝试：{' | '.join(similar[:5])}"
        else:
            tip = "请打开 iBox App 确认是否持有该藏品，并使用与 App 完全一致的名称重新发送指令。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "do not own any unlocked items to consign" in combined_lower:
        reason = f"市场上有「{ctx.collection_name or '该藏品'}」，但您的钱包中没有可寄售的未锁定藏品。"
        tip = "请在 App 中确认是否持有该系列、是否已在寄售中或被锁定，或改用您实际持有的藏品名称。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "no matching consignment listings" in combined_lower or "请选择藏品" in combined:
        reason = f"未找到「{ctx.collection_name or '该藏品'}」在 {format_price(ctx.price)} 元价位的可购买挂单。"
        if "请选择藏品" in combined or "digitalcollectionid" in combined_lower:
            reason = "点对点缺少藏品ID，或寄售信息不完整。"
            tip = (
                "请复制 B 寄售成功回复中的整段「寄售ID|藏品ID」用于点对点；"
                "批量购买可改用「捡漏」指令。"
            )
        elif ctx.consign_order_id:
            tip = (
                "请复制 B 返回的完整「寄售ID|藏品ID」；"
                "若 B 独占该价位，A 也可直接用「捡漏」批量购买。"
            )
        else:
            tip = "请确认卖家已完成寄售、价格一致，或补充寄售ID|藏品ID后再试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "failed to list market consignments" in combined_lower:
        if "10002" in combined or "过于频繁" in combined:
            reason = "iBox 接口限流（请求过于频繁），暂时无法查询市场挂单列表。"
            tip = "请等待 1～2 分钟后重试。"
        else:
            reason = "无法查询该藏品系列下的市场挂单列表。"
            tip = "请确认 App 中该藏品市场页可正常打开，检查网络与 RPC 连接后稍后重试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "failed to list unlocked collections" in combined_lower:
        if "10002" in combined or "过于频繁" in combined:
            reason = "iBox 接口限流（请求过于频繁），暂时无法查询可寄售藏品列表。"
            tip = "请等待 1～2 分钟后重试，并适当减少单次寄售数量，避免连续多次发送指令。"
        else:
            reason = "无法获取该藏品系列下的可寄售列表。"
            tip = "请确认 App 中该藏品是否可正常查看，检查网络与 RPC 连接后稍后重试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "need " in combined_lower and "unlocked item(s) to consign" in combined_lower:
        reason = "可寄售的未锁定藏品数量不足，无法满足指令中的数量。"
        tip = "请在 App 中查看实际可寄售数量，调小指令中的「数量」参数后重新发送。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "no active consignment listings" in combined_lower:
        reason = f"未找到「{ctx.collection_name or '该藏品'}」在指定价格下的寄售订单。"
        tip = "请核对 App 中当前寄售价格与数量，确认参数与实际情况一致后再试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "failed to list consigned items" in combined_lower:
        if "10002" in combined or "过于频繁" in combined:
            reason = "iBox 接口限流（请求过于频繁），暂时无法查询寄售中的订单。"
            tip = "请等待 1～2 分钟后重试。"
        else:
            reason = "无法查询该藏品系列下的寄售订单列表。"
            tip = "请确认 App 中寄售状态正常，稍后重试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if "10002" in combined or "过于频繁" in combined:
        reason = "iBox 接口限流（请求过于频繁）。"
        tip = "请等待 1～2 分钟后重试，避免短时间内重复发送相同指令。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if ctx.action == "synthesis-auto":
        submitted = result.get("submitted_count", 0) if isinstance(result, dict) else 0
        if submitted == 0:
            reason = "本次未成功完成任何合成。"
            if ctx.target_count is not None:
                tip = f"请确认当前是否有可合成活动、材料是否充足，或稍后再试。目标数量：{ctx.target_count}"
            else:
                tip = "请确认当前是否有可合成活动、材料是否充足；如需指定次数，请在指令中填写合成数量。"
            return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if code not in (None, 0) and message and not error:
        reason = f"接口返回失败：{message}"
        tip = "请核对指令参数与 App 内信息是否一致，稍后重试。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    if error:
        if any(token in error for token in ("rpcBridgeNotReady", "adb", "frida", "27042")):
            reason = "设备 RPC 连接异常，任务未能正常执行。"
            tip = "请确认手机已连接、iBox App 与 LSPosed 模块正常运行，然后重试。"
            return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"
        reason = error[:200]
        tip = "请核对指令参数与 App 内信息是否一致，稍后重试；若仍失败请联系管理员。"
        return f"{mobile}-{verb}失败\n原因：{reason}\n建议：{tip}"

    return (
        f"{mobile}-{verb}失败\n"
        "原因：任务未能完成。\n"
        "建议：请核对指令格式与参数，稍后重试；若多次失败请联系管理员。"
    )


def format_task_reply(ctx: TaskContext, data: dict | None, raw: str, exit_code: int) -> str:
    mobile = ctx.mobile
    result = _task_result_dict(data)
    login = data.get("login") if isinstance(data, dict) else None
    if isinstance(login, dict) and login.get("code") not in (None, 0):
        return explain_task_failure(ctx, data, exit_code)

    meta = TASK_DONE_META[ctx.action]

    if ctx.action in {"consign-create", "consign-cancel", "market-purchase", "wanted-deal"}:
        if isinstance(result, dict) and result.get("successCount") is not None:
            name = task_collection_display_name(ctx, result)
            success = result.get("successCount", 0)
            fail = result.get("failCount", 0)
            extra = ""
            if ctx.action == "market-purchase" and ctx.consign_order_id:
                ref = ctx.consign_order_id
                if ctx.digital_collection_id:
                    ref = f"{ctx.consign_order_id}|{ctx.digital_collection_id}"
                extra = f"，寄售信息：{ref}"
            elif ctx.action == "wanted-deal":
                unsold = int(result.get("unsoldCount", fail) or 0)
                return (
                    f"{mobile}-{meta['label']}，藏品名称：{name}，"
                    f"{meta['count_label']}：{success}，{meta.get('unsold_label', '未卖出个数')}：{unsold}"
                )
            return (
                f"{mobile}-{meta['label']}，藏品名称：{name}，"
                f"{meta['count_label']}：{success}，失败个数：{fail}{extra}"
            )

    if ctx.action == "synthesis-auto":
        if exit_code == 0 and isinstance(result, dict):
            submitted = int(result.get("submitted_count") or 0)
            reward_totals = result.get("reward_totals") or []
            if reward_totals:
                names = "、".join(
                    f"{item['name']}×{item['count']}"
                    if int(item.get("count") or 0) > 1
                    else str(item["name"])
                    for item in reward_totals[:10]
                    if isinstance(item, dict) and item.get("name")
                )
            else:
                names = "无"
            return (
                f"{mobile}-{meta['label']}，"
                f"藏品名称：{names}，{meta['count_label']}：{submitted}"
            )

    if ctx.action in {"wanted-buy", "market-buy", "sale-rush"}:
        if exit_code == 0:
            price_text = format_price(ctx.price)
            if ctx.action == "sale-rush":
                result = _task_result_dict(data)
                paid = bool(result.get("paid")) if isinstance(result, dict) else False
                price_val = result.get("price_yuan") if isinstance(result, dict) else None
                price_text = format_price(float(price_val) if price_val is not None else ctx.price)
                status = "已支付" if paid else "订单已创建但未支付"
                return (
                    f"{mobile}-{meta['label']}，藏品名称：{task_collection_display_name(ctx, result)}，"
                    f"价格：{price_text}，{meta['count_label']}：{ctx.quantity}（{status}）"
                )
            return (
                f"{mobile}-{meta['label']}，藏品名称：{task_collection_display_name(ctx, result)}，"
                f"{'出价' if ctx.action == 'wanted-buy' else '价格'}：{price_text}，"
                f"{meta['count_label']}：{ctx.quantity}"
            )

    return explain_task_failure(ctx, data, exit_code)


def format_result(data: dict | None, raw: str, exit_code: int) -> str:
    if isinstance(data, dict):
        result = data.get("result", data)
        login = data.get("login")
        lines = []
        if isinstance(login, dict):
            if login.get("message") == "using saved session":
                lines.append("登录：复用已保存 session")
            elif login.get("code") == 0:
                lines.append("登录：成功")
            elif login.get("code") not in (None, 0):
                lines.append(f"登录失败：{login.get('message') or login.get('code')}")
        if isinstance(result, dict):
            if result.get("summary"):
                lines.append(str(result["summary"]))
            elif result.get("message"):
                lines.append(str(result["message"]))
            elif result.get("error"):
                lines.append(f"失败：{result['error']}")
            elif result.get("code") == 0:
                lines.append("执行成功")
            else:
                code = result.get("code")
                msg = result.get("message") or result.get("msg") or result.get("error")
                lines.append(f"结果 code={code} {msg or ''}".strip())
        if lines:
            return "\n".join(lines[:20])
    tail = raw.strip()[-1500:] if raw else ""
    if exit_code == 0:
        return tail or "执行完成"
    return f"执行失败 (exit={exit_code})\n{tail}"


def run_ibox_command(run_args: list[str], extra_args: list[str], timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, str(ROOT / "run.py"), *extra_args, *run_args]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout if timeout > 0 else None,
        env=subprocess_env(),
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


async def run_ibox_command_streaming(
    run_args: list[str],
    extra_args: list[str],
    timeout: int,
    on_line,
) -> tuple[int, str]:
    cmd = [sys.executable, str(ROOT / "run.py"), *extra_args, *run_args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=subprocess_env(),
    )
    chunks: list[str] = []
    unlimited = timeout <= 0
    deadline = None if unlimited else asyncio.get_running_loop().time() + timeout
    try:
        assert proc.stdout is not None
        while True:
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    proc.kill()
                    await proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            else:
                line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = decode_subprocess_bytes(line_bytes)
            chunks.append(line)
            maybe_coro = on_line(line)
            if inspect.isawaitable(maybe_coro):
                await maybe_coro
        return_code = await proc.wait()
        return return_code, "".join(chunks)
    except asyncio.TimeoutError as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise subprocess.TimeoutExpired(cmd, timeout) from exc
    except subprocess.TimeoutExpired:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise


def build_ws_url(ws_url: str, access_token: str = "") -> str:
    """Append access_token to WS URL (NapCat/OneBot common pattern)."""
    token = (access_token or "").strip()
    if not token:
        return ws_url
    parts = urlparse(ws_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("access_token", token)
    return urlunparse(parts._replace(query=urlencode(query)))


def open_websocket(ws_url: str, headers: dict | None = None):
    """Open OneBot WS with version-tolerant header handling."""
    connect_kwargs = {"ping_interval": 20, "ping_timeout": 20}
    hdrs = dict(headers or {})
    if not hdrs:
        return websockets.connect(ws_url, **connect_kwargs)

    header_items = list(hdrs.items())
    params = inspect.signature(websockets.connect).parameters
    if "extra_headers" in params:
        return websockets.connect(ws_url, extra_headers=header_items, **connect_kwargs)
    if "additional_headers" in params:
        return websockets.connect(ws_url, additional_headers=hdrs, **connect_kwargs)
    return websockets.connect(ws_url, **connect_kwargs)


class OneBotClient:
    def __init__(self, http_url: str, ws_url: str, access_token: str = ""):
        self.http_url = http_url.rstrip("/")
        self.access_token = (access_token or "").strip()
        self.ws_url = build_ws_url(ws_url, self.access_token)
        self.headers = {"Content-Type": "application/json"}
        if self.access_token:
            self.headers["Authorization"] = f"Bearer {self.access_token}"

    def _post(self, action: str, params: dict) -> dict:
        resp = requests.post(
            f"{self.http_url}/{action}",
            headers=self.headers,
            json=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "failed":
            raise RuntimeError(data.get("msg") or data.get("wording") or "OneBot API failed")
        return data

    def check_connection(self) -> str:
        """Verify OneBot HTTP is reachable before starting WS listener."""
        try:
            data = self._post("get_login_info", {})
        except requests.RequestException as exc:
            raise RuntimeError(
                f"OneBot HTTP 不可用 ({self.http_url}): {exc}\n"
                "请在 NapCat WebUI 开启 OneBot11 HTTP（默认 3000 端口）。"
            ) from exc
        if data.get("status") == "failed":
            raise RuntimeError(data.get("msg") or data.get("wording") or "get_login_info failed")
        user_id = (data.get("data") or {}).get("user_id")
        return str(user_id) if user_id is not None else "unknown"

    def send_private(self, user_id: int, message: str):
        self._post("send_private_msg", {"user_id": int(user_id), "message": message})

    def send_group(self, group_id: int, message: str):
        self._post("send_group_msg", {"group_id": int(group_id), "message": message})

    async def listen(self, handler):
        # access_token is already appended to ws_url; avoid duplicate auth headers.
        async with open_websocket(self.ws_url) as ws:
            print(f"[qq-bot] connected to {self.ws_url}", flush=True)
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await handler(self, event)


class QQBot:
    def __init__(self, config: dict):
        self.config = config
        self.client = OneBotClient(
            config.get("onebot_http_url", "http://127.0.0.1:3000"),
            config.get("onebot_ws_url", "ws://127.0.0.1:3001"),
            config.get("access_token", "") or "",
        )
        self.bot_qq = int(config.get("bot_qq") or 0)
        self.allow_all_senders = bool(config.get("allow_all_senders", False))
        self.allowed_users = {int(x) for x in (config.get("allowed_users") or [])}
        if not self.allow_all_senders and not self.allowed_users:
            raise SystemExit(
                "config/qq_bot.yaml: set allow_all_senders: true, or add at least one allowed_users"
            )
        self.allowed_groups = {int(x) for x in (config.get("allowed_groups") or [])}
        self.run_args = list(config.get("run_args") or [])
        self.timeout = int(config.get("command_timeout") if config.get("command_timeout") is not None else 0)
        if self.timeout <= 0:
            print("[qq-bot] command_timeout=0 (no limit on task runtime)", flush=True)
        max_concurrent = int(config.get("max_concurrent_tasks") or 0)
        self._max_concurrent = max(0, max_concurrent)
        self._task_semaphore = (
            asyncio.Semaphore(self._max_concurrent) if self._max_concurrent > 0 else None
        )
        self._task_seq = 0
        self._background_tasks: set[asyncio.Task] = set()
        self.defaults = {
            "default_mobile": str(config.get("default_mobile") or "").strip(),
            "default_code": str(config.get("default_code") if config.get("default_code") is not None else "-"),
            "default_pay_password": str(config.get("default_pay_password") or "").strip(),
        }
        if self._max_concurrent > 0:
            print(f"[qq-bot] max_concurrent_tasks={self._max_concurrent}", flush=True)
        if self.bot_qq:
            mode = "allow_all_senders" if self.allow_all_senders else f"allowed_users={sorted(self.allowed_users)}"
            print(f"[qq-bot] bot_qq={self.bot_qq} mode={mode}", flush=True)

    def is_allowed(self, event: dict) -> bool:
        message_type = event.get("message_type")
        if message_type == "group":
            if not self.allowed_groups:
                return False
            group_id = int(event.get("group_id") or 0)
            if group_id not in self.allowed_groups:
                return False
            if self.allow_all_senders:
                return True
            user_id = int(event.get("user_id") or event.get("sender", {}).get("user_id") or 0)
            return user_id in self.allowed_users

        # private chat: OneBot only delivers messages sent to the logged-in bot account
        if self.allow_all_senders:
            return True
        user_id = int(event.get("user_id") or event.get("sender", {}).get("user_id") or 0)
        return user_id in self.allowed_users

    def reply(self, event: dict, message: str):
        message = message[:4000]
        if event.get("message_type") == "group":
            self.client.send_group(int(event["group_id"]), message)
        else:
            self.client.send_private(int(event["user_id"]), message)

    async def _maybe_auto_p2p_purchase(
        self,
        event: dict,
        seller_ctx: TaskContext,
        data: dict | None,
    ) -> None:
        buyer_cfg = resolve_p2p_auto_buyer(seller_ctx.mobile, self.config)
        if not buyer_cfg:
            return
        result = _task_result_dict(data)
        if not isinstance(result, dict):
            return
        consign_ids = [
            str(item).strip()
            for item in (result.get("consignOrderIds") or [])
            if str(item).strip()
        ]
        if not consign_ids:
            return
        buyer_mobile = str(
            buyer_cfg.get("buyer_mobile") or buyer_cfg.get("buyer") or ""
        ).strip()
        if not buyer_mobile:
            print("[qq-bot] p2p_auto: missing buyer_mobile", flush=True)
            return
        buyer_code = str(
            buyer_cfg.get("buyer_code")
            or buyer_cfg.get("code")
            or self.defaults.get("default_code")
            or "-"
        ).strip()
        buyer_password = str(
            buyer_cfg.get("buyer_pay_password")
            or buyer_cfg.get("pay_password")
            or buyer_cfg.get("password")
            or self.defaults.get("default_pay_password")
            or ""
        ).strip()
        if not buyer_password:
            try:
                await asyncio.to_thread(
                    self.reply,
                    event,
                    f"{seller_ctx.mobile}-寄售已完成，但 p2p_auto 未配置买家支付密码，"
                    "请手动发送点对点指令。",
                )
            except Exception as exc:
                print(f"[qq-bot] failed to send p2p_auto notice: {exc}", flush=True)
            return

        collection_name = task_collection_display_name(seller_ctx, result)
        price = float(seller_ctx.price or result.get("price_yuan") or 0)
        run_cmd = build_p2p_auto_purchase_cmd(
            buyer_mobile=buyer_mobile,
            buyer_code=buyer_code,
            buyer_password=buyer_password,
            collection_name=collection_name,
            price=price,
            consign_ids=consign_ids,
        )
        buyer_ctx = TaskContext(
            action="market-purchase",
            mobile=buyer_mobile,
            collection_name=collection_name,
            price=price,
            quantity=len(consign_ids),
        )
        try:
            await asyncio.to_thread(
                self.reply,
                event,
                f"{seller_ctx.mobile}-寄售已完成，已自动触发 {buyer_mobile} 点对点购买 "
                f"（{len(consign_ids)} 件）…",
            )
        except Exception as exc:
            print(f"[qq-bot] failed to send p2p_auto start notice: {exc}", flush=True)
        self._spawn_command_task(event, "market-purchase", run_cmd, buyer_ctx)

    def _spawn_command_task(
        self,
        event: dict,
        action: str,
        run_cmd: list[str] | None,
        task_ctx: TaskContext | None,
    ) -> None:
        self._task_seq += 1
        task_no = self._task_seq
        bg = asyncio.create_task(
            self._execute_command(event, action, run_cmd, task_ctx, task_no),
            name=f"ibox-{task_no}-{action}",
        )
        self._background_tasks.add(bg)
        bg.add_done_callback(self._background_tasks.discard)

    async def _execute_command(
        self,
        event: dict,
        action: str,
        run_cmd: list[str] | None,
        task_ctx: TaskContext | None,
        task_no: int,
    ) -> None:
        if self._task_semaphore is not None:
            async with self._task_semaphore:
                await self._run_command(event, action, run_cmd, task_ctx, task_no)
        else:
            await self._run_command(event, action, run_cmd, task_ctx, task_no)

    async def _run_command(
        self,
        event: dict,
        action: str,
        run_cmd: list[str] | None,
        task_ctx: TaskContext | None,
        task_no: int,
    ) -> None:
        if run_cmd is None:
            return

        label = task_ctx.mobile if task_ctx is not None else action
        active = len(self._background_tasks)
        try:
            print(
                f"[qq-bot] task#{task_no} start ({label}, active={active}): {' '.join(run_cmd)}",
                flush=True,
            )
            if action in TASK_ACTIONS and task_ctx is not None:
                progress = TaskProgressTracker()
                synthesis_terminal_reply_sent = False

                async def handle_progress_line(line: str) -> None:
                    nonlocal synthesis_terminal_reply_sent
                    batch_match = SYNTHESIS_BATCH_RE.search(line)
                    if batch_match and task_ctx.action == "synthesis-auto":
                        try:
                            payload = json.loads(batch_match.group(1))
                        except json.JSONDecodeError:
                            payload = None
                        if isinstance(payload, dict):
                            message = format_synthesis_batch_reply(task_ctx, payload)
                            if payload.get("terminal"):
                                synthesis_terminal_reply_sent = True
                            try:
                                await asyncio.to_thread(self.reply, event, message)
                            except Exception as exc:
                                print(f"[qq-bot] failed to send synthesis batch: {exc}", flush=True)
                            return

                    update = progress.feed(line)
                    if update is None:
                        return
                    done, total, decile = update
                    message = format_task_progress(task_ctx, done, total, decile)
                    try:
                        await asyncio.to_thread(self.reply, event, message)
                    except Exception as exc:
                        print(f"[qq-bot] failed to send progress: {exc}", flush=True)

                task_timeout = (
                    0
                    if task_ctx.action in UNLIMITED_QQ_TASK_TIMEOUT_ACTIONS or self.timeout <= 0
                    else self.timeout
                )
                exit_code, output = await run_ibox_command_streaming(
                    run_cmd,
                    self.run_args,
                    task_timeout,
                    handle_progress_line,
                )
            else:
                cmd_timeout = self.timeout if self.timeout > 0 else 0
                exit_code, output = await asyncio.to_thread(
                    run_ibox_command, run_cmd, self.run_args, cmd_timeout
                )
            data = extract_json_result(output)
            if action == "login":
                reply = format_login_reply(data, output, exit_code)
            elif action in TASK_ACTIONS and task_ctx is not None:
                if action == "synthesis-auto" and synthesis_terminal_reply_sent:
                    reply = None
                else:
                    reply = format_task_reply(task_ctx, data, output, exit_code)
            else:
                reply = format_result(data, output, exit_code)
            try:
                if reply:
                    await asyncio.to_thread(self.reply, event, reply)
            except Exception as exc:
                print(f"[qq-bot] failed to send reply: {exc}", flush=True)
            if (
                action == "consign-create"
                and exit_code == 0
                and task_ctx is not None
            ):
                await self._maybe_auto_p2p_purchase(event, task_ctx, data)
        except subprocess.TimeoutExpired:
            try:
                if action in TASK_ACTIONS and task_ctx is not None:
                    verb = TASK_START_META[task_ctx.action]["verb"]
                    await asyncio.to_thread(
                        self.reply,
                        event,
                        f"{task_ctx.mobile}-{verb}失败\n"
                        "原因：任务执行超时。\n"
                        f"建议：任务可能仍在后台运行，请稍后在 App 中确认结果；"
                        f"超时阈值 {self.timeout}s。",
                    )
                else:
                    await asyncio.to_thread(
                        self.reply,
                        event,
                        "指令执行超时。\n"
                        f"建议：请稍后重试，或检查任务是否已在 App 中完成（超时阈值 {self.timeout}s）。",
                    )
            except Exception as exc:
                print(f"[qq-bot] failed to send reply: {exc}", flush=True)
        except Exception as exc:
            print(f"[qq-bot] task#{task_no} failed ({label}): {exc}", flush=True)
            try:
                await asyncio.to_thread(self.reply, event, f"任务执行异常：{exc}")
            except Exception as reply_exc:
                print(f"[qq-bot] failed to send reply: {reply_exc}", flush=True)
        finally:
            print(f"[qq-bot] task#{task_no} done ({label})", flush=True)

    async def on_event(self, client: OneBotClient, event: dict):
        if event.get("post_type") != "message":
            return
        if not self.is_allowed(event):
            return
        text = event.get("raw_message") or event.get("message") or ""
        if isinstance(text, list):
            parts = []
            for seg in text:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            text = "".join(parts)
        text = str(text).strip()
        if not text:
            return

        try:
            action, run_cmd, task_ctx = parse_command(text, self.defaults)
        except Exception as exc:
            try:
                self.reply(event, f"指令解析/执行失败：{exc}")
            except Exception as reply_exc:
                print(f"[qq-bot] failed to send reply: {reply_exc}", flush=True)
            return

        if action == "help":
            try:
                self.reply(event, HELP_TEXT)
            except Exception as exc:
                print(f"[qq-bot] failed to send reply: {exc}", flush=True)
            return
        if action == "empty":
            try:
                self.reply(event, "发送「帮助」查看指令")
            except Exception as exc:
                print(f"[qq-bot] failed to send reply: {exc}", flush=True)
            return
        if run_cmd is None:
            return

        if action in TASK_ACTIONS and task_ctx is not None:
            try:
                self.reply(event, format_task_start(task_ctx))
            except Exception as exc:
                print(f"[qq-bot] failed to send start notice: {exc}", flush=True)

        self._spawn_command_task(event, action, run_cmd, task_ctx)


async def main_async():
    config = load_bot_config()
    bot = QQBot(config)
    try:
        login_qq = bot.client.check_connection()
        print(f"[qq-bot] OneBot HTTP ok, login_qq={login_qq}", flush=True)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print("[qq-bot] waiting for QQ messages...", flush=True)
    print(
        "[qq-bot] NapCat 请只开「HTTP + 正向 WebSocket」，关闭「反向 WebSocket」。"
        " 若 NapCat 报「不支持的Api undefined」，通常是反向 WS 地址配错。",
        flush=True,
    )
    await bot.client.listen(bot.on_event)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[qq-bot] stopped", flush=True)


if __name__ == "__main__":
    main()
