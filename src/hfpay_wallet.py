"""Huifu wallet H5 cashier helpers for iBox secondary-market payments."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from Crypto.Cipher import DES3
from Crypto.Util.Padding import pad

HFPAY_API_BASE = "https://hfpay.cloudpnr.com/api/hfpwalleth5"
DEFAULT_WALLET_PAGE_VERSION = "20260305101149"
DEV_INFO_JSON = '{"devType":"2","devSysType":"H5","mobileFlag":"Y"}'
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; M2006J10C Build/SP1A.210812.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/89.0.4389.72 "
    "MQQBrowser/6.2 TBS/046295 Mobile Safari/537.36 ibox_app ;kyc"
)


def parse_wallet_uuid(cashier_link: str) -> str:
    query = parse_qs(urlparse(str(cashier_link or "")).query)
    values = query.get("uuid") or []
    if not values:
        raise ValueError(f"cannot parse wallet uuid from cashier link: {cashier_link!r}")
    return str(values[0])


def parse_mer_cust_id(wallet_uuid: str) -> str:
    text = str(wallet_uuid or "")
    if len(text) >= 25:
        return text[9:25]
    match = re.search(r"(\d{16})", text)
    if match:
        return match.group(1)
    raise ValueError(f"cannot parse mer_cust_id from wallet uuid: {wallet_uuid!r}")


def _set_sign(body: dict) -> str:
    keys = sorted(body.keys())
    parts: list[str] = []
    for index, key in enumerate(keys):
        value = body[key]
        suffix = "&" if index < len(keys) - 1 else ""
        if isinstance(value, dict):
            parts.append(f"{key}={json.dumps(value, separators=(',', ':'), ensure_ascii=False)}{suffix}")
        else:
            parts.append(f"{key}={value}{suffix}")
    return "".join(parts)


def compute_check_value(body: dict | None) -> str:
    payload = body or {}
    sign = _set_sign(payload)
    return hmac.new(b"chinapnr", sign.encode("utf-8"), hashlib.sha256).hexdigest()


def encrypt_wallet_password(plain_password: str, wallet_uuid: str) -> str:
    key = str(wallet_uuid).encode("utf-8")[:24]
    if len(key) < 24:
        key = key.ljust(24, b"\0")
    cipher = DES3.new(key, DES3.MODE_CBC, iv=b"chinapnr")
    encrypted = cipher.encrypt(pad(str(plain_password).encode("utf-8"), DES3.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def resolve_encrypted_wallet_password(
    config: dict | None,
    *,
    encrypted_b64: str = "",
    plain_password: str = "",
    wallet_uuid: str = "",
) -> tuple[str, str]:
    encrypted = (encrypted_b64 or "").strip()
    if encrypted:
        return encrypted, "using provided encrypted wallet password"
    plain = (plain_password or "").strip()
    if not plain:
        return "", "missing wallet password"
    if not wallet_uuid:
        return "", "missing wallet uuid for password encryption"
    return (
        encrypt_wallet_password(plain, wallet_uuid),
        "encrypted plain wallet password (TripleDES, key=uuid)",
    )


def _hf_request(
    session: requests.Session,
    *,
    endpoint: str,
    wallet_uuid: str,
    body: dict | None = None,
    cashier_link: str,
) -> dict[str, Any]:
    payload = body or {}
    headers = {
        "content-type": "application/json",
        "uuid": wallet_uuid,
        "mer_cust_id": parse_mer_cust_id(wallet_uuid),
        "check_value": compute_check_value(payload),
        "hide_head": "0",
        "origin": "https://hfpay.cloudpnr.com",
        "referer": cashier_link,
        "user-agent": DEFAULT_USER_AGENT,
        "x-requested-with": "com.box.art",
    }
    response = session.post(
        f"{HFPAY_API_BASE}/{endpoint.lstrip('/')}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"{endpoint} returned non-object JSON")
    return data


def _resp_ok(result: dict[str, Any]) -> bool:
    return str(result.get("resp_code") or "") == "C00000"


def pay_via_wallet_cashier(
    *,
    cashier_link: str,
    ibox_token: str,
    encrypted_password: str,
    app_version: str = "2.3.2",
    max_trans_amt_yuan: float | None = None,
    wallet_page_version: str = DEFAULT_WALLET_PAGE_VERSION,
) -> dict[str, Any]:
    del app_version  # reserved for future header tuning
    wallet_uuid = parse_wallet_uuid(cashier_link)
    session = requests.Session()
    session.headers.update({"user-agent": DEFAULT_USER_AGENT})
    session.cookies.set("token", str(ibox_token), domain="hfpay.cloudpnr.com")
    steps: dict[str, Any] = {}

    page_style = _hf_request(
        session,
        endpoint="queryWalletPageStyle",
        wallet_uuid=wallet_uuid,
        body={"version": wallet_page_version},
        cashier_link=cashier_link,
    )
    steps["queryWalletPageStyle"] = page_style

    desk_info = _hf_request(
        session,
        endpoint="queryCashDeskInfo",
        wallet_uuid=wallet_uuid,
        body={},
        cashier_link=cashier_link,
    )
    steps["queryCashDeskInfo"] = desk_info
    trans_amt = desk_info.get("trans_amt")
    if max_trans_amt_yuan is not None and trans_amt not in (None, ""):
        try:
            if float(trans_amt) > float(max_trans_amt_yuan) + 1e-9:
                return {
                    "ok": False,
                    "aborted_before_pay": True,
                    "trans_amt": trans_amt,
                    "error": (
                        f"cashier amount {trans_amt} yuan exceeds limit {max_trans_amt_yuan:g} yuan"
                    ),
                    "steps": steps,
                }
        except (TypeError, ValueError):
            pass

    for name in ("paystatquery", "avlamtquery"):
        steps[name] = _hf_request(
            session,
            endpoint=name,
            wallet_uuid=wallet_uuid,
            body={},
            cashier_link=cashier_link,
        )

    steps["transverifyquery"] = _hf_request(
        session,
        endpoint="transverifyquery",
        wallet_uuid=wallet_uuid,
        body={"trans_type": "30", "dev_info_json": DEV_INFO_JSON},
        cashier_link=cashier_link,
    )

    balance_body = {"dev_info_json": DEV_INFO_JSON, "password": encrypted_password}
    balancepay = _hf_request(
        session,
        endpoint="balancepay",
        wallet_uuid=wallet_uuid,
        body=balance_body,
        cashier_link=cashier_link,
    )
    steps["balancepay"] = balancepay
    if not _resp_ok(balancepay):
        return {
            "ok": False,
            "trans_amt": trans_amt,
            "error": f"balancepay failed: {balancepay.get('resp_code')} {balancepay.get('resp_desc')}",
            "steps": steps,
        }

    paid_stat = _hf_request(
        session,
        endpoint="paystatquery",
        wallet_uuid=wallet_uuid,
        body={},
        cashier_link=cashier_link,
    )
    steps["paystatquery_after"] = paid_stat
    trans_stat = str(paid_stat.get("trans_stat") or "")
    ok = trans_stat in {"S", "SUCCESS", "success"}
    return {
        "ok": ok,
        "trans_amt": trans_amt,
        "trans_stat": trans_stat,
        "steps": steps,
        "error": None if ok else f"paystatquery trans_stat={trans_stat or '?'}",
    }
