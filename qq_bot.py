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
  验证码 13800138000
  登录 13800138000 123456
  寄售 13800138000 - 123456 藏品名 99 1
  下架 13800138000 - 123456 藏品名 99 1
  求购 13800138000 - 123456 藏品名 88 1
  捡漏 13800138000 - 藏品名 5000 1

If default_mobile / default_pay_password are set in config/qq_bot.yaml,
you can omit mobile/password, e.g.:
  寄售 - 藏品名 99 1
  寄售 藏品名 99 1
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
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

HELP_TEXT = """iBox QQ 指令帮助

验证码 <手机号>
登录 <手机号> <验证码>
寄售 [手机号] [验证码|-] [支付密码] <藏品名> <价格> <数量>
下架 [手机号] [验证码|-] [支付密码] <藏品名> <价格> <数量>
求购 [手机号] [验证码|-] [支付密码] <藏品名> <出价> <数量>
捡漏 [手机号] [验证码|-] <藏品名> <最高价> <数量>

说明：
- 验证码可写 - 表示复用已保存 session
- 可在 config/qq_bot.yaml 配置 default_mobile / default_pay_password 省略参数
- 藏品名含空格请用引号，例如：寄售 - 123456 "2026喜糖熊猫" 199 1
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


def parse_command(text: str, defaults: dict) -> tuple[str, list[str] | None, str | None]:
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
        "consign-create": "consign-create",
        "寄售": "consign-create",
        "consign-cancel": "consign-cancel",
        "下架": "consign-cancel",
        "wanted-buy": "wanted-buy",
        "求购": "wanted-buy",
        "market-buy": "market-buy",
        "捡漏": "market-buy",
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
        summary = f"{action} {name} 价格={price} 数量={qty}"
        return action, run_cmd, summary

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
            "--consignment-password",
            password,
        ]
        return action, run_cmd, f"求购 {name} 出价={price} 数量={qty}"

    if action == "market-buy":
        idx = 1
        mobile = defaults.get("default_mobile") or ""
        code = defaults.get("default_code", "-")
        if idx < len(tokens) and MOBILE_RE.fullmatch(tokens[idx]):
            mobile = tokens[idx]
            idx += 1
        if idx < len(tokens) and CODE_RE.fullmatch(tokens[idx]):
            code = tokens[idx]
            idx += 1
        if len(tokens) - idx < 3:
            raise ValueError("参数不足，需要：藏品名 最高价 数量")
        qty = int(tokens[-1])
        price = float(tokens[-2])
        name = " ".join(tokens[idx:-2]).strip()
        if not mobile:
            raise ValueError("缺少手机号")
        if not name:
            raise ValueError("缺少藏品名")
        run_cmd = [
            "market-buy",
            mobile,
            code,
            "--collection-name",
            name,
            "--price",
            str(price),
            "--quantity",
            str(qty),
        ]
        return action, run_cmd, f"捡漏 {name} 最高价={price} 数量={qty}"

    raise ValueError(f"未知指令：{tokens[0]}，发送「帮助」查看用法")


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
        timeout=timeout,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


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
    if hdrs:
        try:
            return websockets.connect(ws_url, additional_headers=hdrs, **connect_kwargs)
        except TypeError:
            return websockets.connect(ws_url, extra_headers=list(hdrs.items()), **connect_kwargs)
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

    def send_private(self, user_id: int, message: str):
        self._post("send_private_msg", {"user_id": int(user_id), "message": message})

    def send_group(self, group_id: int, message: str):
        self._post("send_group_msg", {"group_id": int(group_id), "message": message})

    async def listen(self, handler):
        # Prefer token in URL; only pass Authorization header if token is set and URL auth fails.
        headers = {}
        if self.access_token:
            headers["Authorization"] = self.headers["Authorization"]
        try:
            ws_cm = open_websocket(self.ws_url, headers if self.access_token else None)
        except TypeError:
            ws_cm = open_websocket(self.ws_url, None)
        async with ws_cm as ws:
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
        self.timeout = int(config.get("command_timeout") or 600)
        self.defaults = {
            "default_mobile": str(config.get("default_mobile") or "").strip(),
            "default_code": str(config.get("default_code") if config.get("default_code") is not None else "-"),
            "default_pay_password": str(config.get("default_pay_password") or "").strip(),
        }
        self._busy = False
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

    def handle_text(self, text: str) -> str:
        action, run_cmd, summary = parse_command(text, self.defaults)
        if action == "help":
            return HELP_TEXT
        if action == "empty":
            return "发送「帮助」查看指令"
        assert run_cmd is not None
        if self._busy:
            return "上一条指令仍在执行，请稍候"
        self._busy = True
        try:
            print(f"[qq-bot] running: {' '.join(run_cmd)}", flush=True)
            exit_code, output = run_ibox_command(run_cmd, self.run_args, self.timeout)
            data = extract_json_result(output)
            reply = format_result(data, output, exit_code)
            if summary:
                reply = f"{summary}\n{reply}".strip()
            return reply
        finally:
            self._busy = False

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
            reply = await asyncio.to_thread(self.handle_text, text)
        except subprocess.TimeoutExpired:
            reply = f"指令执行超时（>{self.timeout}s）"
        except Exception as exc:
            reply = f"指令解析/执行失败：{exc}"
        try:
            self.reply(event, reply)
        except Exception as exc:
            print(f"[qq-bot] failed to send reply: {exc}", flush=True)


async def main_async():
    config = load_bot_config()
    bot = QQBot(config)
    print("[qq-bot] waiting for QQ messages...", flush=True)
    await bot.client.listen(bot.on_event)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[qq-bot] stopped", flush=True)


if __name__ == "__main__":
    main()
