"""
iBox RPC client — TCP socket.

rpc_bridge.js runs a TCP server (port 27042) inside iBox's process (injected via 算法助手).
Python connects to it. Two connection modes:

  Mode A — WiFi (recommended, no USB needed):
      Phone and PC on the same WiFi.
      Python connects directly to phone's IP: IBoxRPCClient(device_host="192.168.x.x")

  Mode B — USB + adb forward:
      USB cable connected.
      Run once: adb forward tcp:27042 tcp:27042
      Python connects to localhost: IBoxRPCClient()  (default host=127.0.0.1)
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import uuid

import requests

RPC_HOST = "127.0.0.1"   # override with phone IP for WiFi mode
RPC_PORT = 27042
_id_counter = 0


# ── adb forward (USB mode only) ───────────────────────────────────────────────

def setup_adb_forward(local_port: int = RPC_PORT, remote_port: int = RPC_PORT):
    """USB mode: run adb forward once to map PC port → device port."""
    subprocess.run(
        ["adb", "forward", f"tcp:{local_port}", f"tcp:{remote_port}"],
        check=True, capture_output=True
    )
    print(f"[rpc] adb forward tcp:{local_port} → tcp:{remote_port} (USB mode)")


# ── TCP RPC connection ────────────────────────────────────────────────────────

class RPCConnection:
    """
    Persistent TCP connection to rpc_bridge.js inside iBox.

    WiFi mode  (no USB): RPCConnection(host="192.168.x.x")
    USB mode   (adb fwd): RPCConnection()  — call setup_adb_forward() first
    """

    def __init__(self, host: str = RPC_HOST, port: int = RPC_PORT):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._file = None

    def connect(self, retry: int = 10, delay: float = 1.0):
        for i in range(retry):
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(10)
                self._sock.connect((self.host, self.port))
                self._file = self._sock.makefile("r", encoding="utf-8")
                mode = "WiFi" if self.host != "127.0.0.1" else "USB/adb-forward"
                print(f"[rpc] Connected ({mode}) → {self.host}:{self.port}")
                return
            except (ConnectionRefusedError, OSError):
                if i < retry - 1:
                    print(f"[rpc] Waiting for bridge... ({i+1}/{retry}) [{self.host}:{self.port}]")
                    time.sleep(delay)
        tip = (
            f"Cannot connect to rpc_bridge on {self.host}:{self.port}.\n"
            "Checklist:\n"
            "  1) rpc_bridge.js is active in 算法助手\n"
            "  2) iBox is running on the device\n"
        )
        if self.host == "127.0.0.1":
            tip += "  3) USB connected and `adb forward tcp:27042 tcp:27042` has been run\n"
        else:
            tip += f"  3) Phone and PC are on the same WiFi (phone IP = {self.host})\n"
        raise ConnectionRefusedError(tip)

    def call(self, cmd: dict, timeout: float = 10.0) -> dict:
        global _id_counter
        _id_counter += 1
        cmd["id"] = _id_counter

        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        self._sock.settimeout(timeout)
        self._sock.sendall(line.encode("utf-8"))

        resp_line = self._file.readline()
        if not resp_line:
            raise ConnectionError("Bridge closed connection")
        return json.loads(resp_line.strip())

    def ping(self) -> bool:
        try:
            r = self.call({"type": "ping"}, timeout=15)
            return r.get("ok") and r.get("msg") == "pong"
        except Exception:
            return False

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Module-level default connection ──────────────────────────────────────────

_conn: RPCConnection | None = None


def get_connection(device_host: str = RPC_HOST) -> RPCConnection:
    """
    Return (or create) the global RPC connection.

    WiFi mode:  get_connection("192.168.x.x")  — no USB, no adb needed
    USB mode:   get_connection()                — needs adb forward first
    """
    global _conn
    if _conn is None:
        _conn = RPCConnection(host=device_host)
        if device_host == "127.0.0.1":
            # USB mode: set up adb forward automatically
            try:
                setup_adb_forward()
            except Exception as e:
                print(f"[rpc] adb forward failed (is USB connected?): {e}")
        _conn.connect()
        if not _conn.ping():
            raise RuntimeError("Bridge ping failed — rpc_bridge.js may not be loaded")
        # Pre-cache Java instances before any decryptResp calls.
        # Java.choose() is safe here (clean heap, no beans created yet).
        try:
            r = _conn.call({"type": "warmup"})
            arg1 = r.get("lastEncryptArg1")
            arg2 = r.get("lastEncryptArg2")
            print(
                f"[rpc] warmup: encryptReady={r.get('encryptReady')}, "
                f"decryptReady={r.get('decryptReady')}, "
                f"encryptArg1={repr(arg1)}, encryptArg2={repr(arg2)}"
            )
            if not r.get("decryptReady"):
                print("[rpc] WARNING: DecryptInterceptor not found yet. "
                      "Open iBox and navigate to any screen, then retry.")
            if arg1 is None:
                print("[rpc] NOTE: b(String,String) requestKey not yet captured. "
                      "Make any request in the iBox app first, or let the bridge generate a random 16-char key.")
        except Exception as e:
            print(f"[rpc] warmup failed (non-fatal): {e}")
    return _conn


def rpc(cmd: dict, timeout: float = 10.0, device_host: str = RPC_HOST) -> dict:
    resp = get_connection(device_host).call(cmd, timeout=timeout)
    if not resp.get("ok"):
        raise RuntimeError(f"RPC error: {resp.get('error')}")
    return resp


# ── High-level API ────────────────────────────────────────────────────────────

def encrypt_body(payload: dict, device_host: str = RPC_HOST) -> str:
    """Call EncryptDataImpl.b() inside iBox. Returns encrypted HTTP body string."""
    body_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return rpc({"type": "encrypt", "body": body_str}, device_host=device_host)["encBody"]


def decrypt_body(cipher_b64: str, key: str, device_host: str = RPC_HOST) -> dict:
    """AES/ECB decrypt a response body inside iBox."""
    result = rpc({"type": "decrypt", "cipherB64": cipher_b64, "key": key}, device_host=device_host)
    try:
        return json.loads(result["plaintext"])
    except Exception:
        return {"_raw": result["plaintext"]}


def get_capture(device_host: str = RPC_HOST) -> dict | None:
    """Return last captured HTTP exchange (for debugging response key field)."""
    result = rpc({"type": "capture"}, device_host=device_host)
    return result.get("capture")


def print_capture(device_host: str = RPC_HOST):
    """Print last HTTP exchange to find response key field."""
    c = get_capture(device_host)
    if not c:
        print("[capture] No capture yet — trigger a request in iBox first.")
        return
    print(f"\n{'='*60}")
    print(f"[capture] {c.get('method')} {c.get('url')}")
    print(f"\n--- Request Headers ---")
    for k, v in (c.get("reqHeaders") or {}).items():
        print(f"  {k}: {v}")
    print(f"\n--- Encrypted Request Body ---")
    print(f"  {str(c.get('encBody', ''))[:500]}")
    print(f"\n--- Response {c.get('respCode')} Headers ---")
    for k, v in (c.get("respHeaders") or {}).items():
        print(f"  {k}: {v}")
    print(f"\n--- Response Body (first 500 chars) ---")
    print(f"  {str(c.get('respBody', ''))[:500]}")
    print(f"{'='*60}\n")


# ── Response key extraction ───────────────────────────────────────────────────

def _extract_resp_key(resp: requests.Response) -> str | None:
    """
    Find the AES decryption key in a response.
    TODO: call print_capture() after first run to see actual field name,
    then update this list.
    """
    # Try response headers
    for h in ("X-Response-Key", "X-Key", "Enc-Key", "x-enc-key", "x-data-key"):
        v = resp.headers.get(h, "")
        if len(v) == 16:
            return v

    # Try JSON wrapper field
    try:
        wrapper = resp.json()
        for field in ("key", "encKey", "dataKey", "responseKey"):
            v = str(wrapper.get(field, ""))
            if len(v) == 16:
                return v
    except Exception:
        pass

    return None


# ── High-level client ─────────────────────────────────────────────────────────

class IBoxRPCClient:
    """
    iBox API client. Encryption is done by the app itself via rpc_bridge.js.
    Python just calls encrypt_body / HTTP / decrypt_body.

    WiFi mode  (recommended): IBoxRPCClient(device_host="192.168.x.x")
    USB mode:                  IBoxRPCClient()  (needs adb forward)
    """

    def __init__(
        self,
        base_url: str = "https://sail-api.ibox.art",
        device_host: str = RPC_HOST,
        headers: dict = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.device_host = device_host
        # Ensure connection is established
        get_connection(device_host)
        self._http = requests.Session()
        self._http.headers.update({
            "Authorization": "Bearer",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ibox/2.2.3(Android;12;Redmi)",
            "platform-type": "1",
            "app-version": "2.2.3",
            "allowouttest": "1",
            "device-id": "",
        })
        if headers:
            self._http.headers.update(headers)
        self.token: str | None = None

    def set_token(self, token: str):
        self.token = token

    def _url(self, path: str) -> str:
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def _headers(self) -> dict:
        hdrs = dict(self._http.headers)
        hdrs["msg-id"] = f"{uuid.uuid4()}_android"
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    def _decode_response(self, resp: requests.Response) -> dict:
        import json as _json

        try:
            body = resp.json()
        except Exception:
            return {"_raw": resp.text[:500]}

        # Standard iBox encrypted response: {"data": "<AES b64>", "encryptKey": "<RSA b64>"}
        # Delegate decryption entirely to the app's own EncryptDataImpl.a() via RPC —
        # no crypto reimplemented in Python.
        if isinstance(body, dict) and "data" in body and "encryptKey" in body:
            result = rpc(
                {
                    "type": "decryptResp",
                    "data": body["data"],
                    "encryptKey": body["encryptKey"],
                    # Pass the full server response body text so DecryptInterceptor.a(String)
                    # receives the exact JSON it would normally process (including "code",
                    # "message", etc. that may be required for decryption).
                    "bodyJson": resp.text,
                },
                timeout=15.0,
                device_host=self.device_host,
            )
            if result.get("ok"):
                try:
                    return _json.loads(result["plaintext"])
                except Exception:
                    return {"_raw": result.get("plaintext", "")}
            return {"_decrypt_error": result.get("error"), "_enc": str(body)[:200]}

        return body

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        kwargs = {
            "headers": self._headers(),
            "timeout": 30,
        }
        if payload is not None:
            kwargs["data"] = encrypt_body(payload, device_host=self.device_host)
        resp = self._http.request(method.upper(), self._url(path), **kwargs)
        return self._decode_response(resp)

    def send_sms(
        self,
        phone: str,
        path: str = "/personal-center-service/login/sendSms",
    ) -> dict:
        return self._request("POST", path, {"phone": phone, "smsType": "1"})

    def login(
        self,
        mobile: str,
        verification_code: str,
        c_id: str,
        invitation_code: str = "",
        enable: int = 1,
        path: str = "/personal-center-service/login/mobile",
    ) -> dict:
        result = self._request("POST", path, {
            "mobile": mobile,
            "verificationCode": verification_code,
            "invitationCode": invitation_code,
            "cId": c_id,
            "enable": enable,
        })
        if isinstance(result, dict) and result.get("code") == 0:
            token = (result.get("data") or {}).get("token")
            if token:
                self.set_token(token)
        return result

    def add_cart(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    def create_order(self, path: str, payload: dict = None) -> dict:
        return self._request("POST", path, payload or {})

    def get_synthesis_activity_list(self, path: str) -> dict:
        return self._request("GET", path)

    def get_synthesis_activity_detail(self, path: str) -> dict:
        return self._request("GET", path)

    def get_synthesis_center(self, path: str) -> dict:
        return self._request("GET", path)

    def get_synthesis_work_status(self, path: str) -> dict:
        return self._request("GET", path)

    def submit_synthesis(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    def confirm_synthesis(self, path: str, payload: dict | None = None) -> dict:
        return self._request("POST", path, payload or {})

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, payload: dict | None = None) -> dict:
        return self._request("POST", path, payload)
