"""
iBox HTTP client.

Request encryption (confirmed from log):
  1. Generate random 16-char hex AES key
  2. AES/ECB/PKCS5Padding encrypt the JSON body
  3. RSA/ECB/PKCS1Padding encrypt the AES key with server's public key
  4. Send both — HTTP body format still TBD (need chucker capture to confirm).
     Two likely candidates:
       A) JSON body: {"data": "<body_b64>", "key": "<rsa_key_b64>"}
       B) Body = body_b64 plain, header like "X-Key: <rsa_key_b64>"

Response decryption (confirmed from log):
  - Response is also AES/ECB/PKCS5Padding encrypted
  - The decryption key comes from the HTTP response (header or wrapper field)
  - Exact field name TBD — use chucker/HTTP capture to confirm
  - Placeholder: assume response header "X-Response-Key" (adjust after capture)
"""

import base64
import json
import requests

from .crypto_utils import aes_ecb_decrypt, generate_aes_key, aes_ecb_encrypt_b64, rsa_encrypt_key


# TODO: confirm actual HTTP body format from chucker capture
# Options: "json_wrapper" | "body_only_header_key"
REQUEST_BODY_FORMAT = "json_wrapper"

# TODO: confirm actual response key field name from chucker capture
RESPONSE_KEY_HEADER = "X-Response-Key"
RESPONSE_KEY_JSON_FIELD = "key"   # if response is a JSON wrapper


class IBoxClient:
    def __init__(self, base_url: str, headers: dict = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "iBox/4.6.0 (Android)",
        })
        if headers:
            self.session.headers.update(headers)
        self.token: str | None = None

    def _url(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        return self.base_url + p

    def _set_token(self, token: str):
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _encrypt_request(self, payload: dict) -> tuple[dict | str, dict]:
        """
        Encrypt a JSON payload.

        Returns (body, extra_headers) depending on REQUEST_BODY_FORMAT.
        Caller sends: session.post(url, json=body, headers=extra_headers) or
                      session.post(url, data=body, headers=extra_headers).

        NOTE: exact format is unconfirmed — update REQUEST_BODY_FORMAT after
        comparing with a chucker HTTP log.
        """
        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        aes_key = generate_aes_key()
        body_b64 = aes_ecb_encrypt_b64(aes_key, plaintext)
        rsa_key_b64 = rsa_encrypt_key(aes_key)

        if REQUEST_BODY_FORMAT == "json_wrapper":
            # Candidate A: {"data": "<body_b64>", "key": "<rsa_key_b64>"}
            body = {"data": body_b64, "key": rsa_key_b64}
            return body, {}
        else:
            # Candidate B: body = raw base64, key in header
            return body_b64, {"X-Key": rsa_key_b64}

    def _decrypt_response(self, resp: requests.Response) -> dict:
        """
        Decrypt an AES/ECB encrypted response.

        The response key comes from the HTTP response — exact field is TBD.
        Tries header first, then falls back to JSON wrapper field.
        Returns parsed JSON dict, or raw text if decryption fails.
        """
        # Try response header
        resp_key_str = resp.headers.get(RESPONSE_KEY_HEADER)

        # Fallback: maybe a JSON wrapper {"key": "...", "data": "..."}
        if not resp_key_str:
            try:
                wrapper = resp.json()
                resp_key_str = wrapper.get(RESPONSE_KEY_JSON_FIELD) or wrapper.get("key")
                if resp_key_str and "data" in wrapper:
                    ciphertext_b64 = wrapper["data"]
                    key_bytes = resp_key_str.encode("ascii")
                    plaintext = aes_ecb_decrypt(key_bytes, base64.b64decode(ciphertext_b64))
                    return json.loads(plaintext.decode("utf-8"))
            except Exception:
                pass

        if resp_key_str:
            try:
                key_bytes = resp_key_str.encode("ascii")
                plaintext = aes_ecb_decrypt(key_bytes, base64.b64decode(resp.text.strip()))
                return json.loads(plaintext.decode("utf-8"))
            except Exception:
                pass

        # Last resort: return raw text as-is (for debugging)
        try:
            return resp.json()
        except Exception:
            return {"_raw": resp.text}

    def send_sms_code(self, phone: str, sms_type: str = "1", path: str = "/personal-center-service/login/sendSms") -> dict:
        """
        Send SMS verification code request.
        Confirmed payload from log: {"phone": "...", "smsType": "1"}
        """
        body, extra = self._encrypt_request({"phone": phone, "smsType": sms_type})
        url = self._url(path)
        resp = self.session.post(url, json=body, headers=extra, timeout=30)
        return self._decrypt_response(resp)

    def login(
        self,
        mobile: str,
        verification_code: str,
        c_id: str,
        invitation_code: str = "",
        enable: int = 1,
        path: str = "/personal-center-service/login/mobile",
    ) -> dict:
        """
        Login with phone + SMS code.
        Confirmed payload from log:
          {"mobile": "...", "verificationCode": "...", "invitationCode": "...",
           "cId": "...", "enable": 1}
        Saves token if login succeeds.
        """
        payload = {
            "mobile": mobile,
            "verificationCode": verification_code,
            "invitationCode": invitation_code,
            "cId": c_id,
            "enable": enable,
        }
        body, extra = self._encrypt_request(payload)
        url = self._url(path)
        resp = self.session.post(url, json=body, headers=extra, timeout=30)
        result = self._decrypt_response(resp)

        # Save token from response
        if isinstance(result, dict) and result.get("code") == 0:
            data = result.get("data", {})
            token = data.get("token")
            if token:
                self._set_token(token)

        return result

    def add_cart(self, path: str, payload: dict = None) -> dict:
        """Add to cart. Encrypts request if payload given."""
        if payload:
            body, extra = self._encrypt_request(payload)
            resp = self.session.post(self._url(path), json=body, headers=extra, timeout=30)
        else:
            resp = self.session.post(self._url(path), timeout=30)
        return self._decrypt_response(resp)

    def create_order(self, path: str, payload: dict = None) -> dict:
        """Create order / checkout."""
        if payload:
            body, extra = self._encrypt_request(payload)
            resp = self.session.post(self._url(path), json=body, headers=extra, timeout=30)
        else:
            resp = self.session.post(self._url(path), timeout=30)
        return self._decrypt_response(resp)
