from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile


def default_session_path(root_dir: str) -> str:
    return os.path.join(root_dir, "config", "session.json")


def load_sessions(path: str) -> dict:
    if not os.path.exists(path):
        return {"accounts": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"accounts": {}}
    if not isinstance(data, dict):
        return {"accounts": {}}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        data["accounts"] = {}
    return data


def load_account_session(path: str, mobile: str) -> dict | None:
    sessions = load_sessions(path)
    account = sessions.get("accounts", {}).get(str(mobile))
    return account if isinstance(account, dict) else None


def save_account_session(path: str, mobile: str, session_data: dict) -> None:
    sessions = load_sessions(path)
    sessions.setdefault("accounts", {})[str(mobile)] = dict(session_data)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as tmp:
        json.dump(sessions, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def delete_account_session(path: str, mobile: str) -> None:
    sessions = load_sessions(path)
    accounts = sessions.get("accounts", {})
    if str(mobile) not in accounts:
        return
    del accounts[str(mobile)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as tmp:
        json.dump(sessions, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def build_session_payload(mobile: str, token: str, uid: str | None = None, extra: dict | None = None) -> dict:
    payload = {
        "mobile": str(mobile),
        "token": token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if uid not in (None, ""):
        payload["uid"] = str(uid)
    if extra:
        payload.update(extra)
    return payload
