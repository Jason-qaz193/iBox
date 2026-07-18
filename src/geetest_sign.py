"""
GeeTest V4 ``w`` parameter encryption (ported from GeekedTest sign.py).

Used by http_solve_sale_rush() for direct load → verify without a browser.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import random
import re
import urllib.parse

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey.RSA import construct
from Crypto.Util.Padding import pad


class LotParser:
    def __init__(self) -> None:
        self.mapping = {"n[20:20]+n[8:8]+n[11:11]+n[30:30]": "n[16:21]"}
        self.lot: list = []
        self.lot_res: list = []
        for key, value in self.mapping.items():
            self.lot = self._parse(key)
            self.lot_res = self._parse(value)

    @staticmethod
    def _parse_slice(segment: str) -> list[int]:
        return [int(x) for x in segment.split(":")]

    @staticmethod
    def _extract(part: str) -> str:
        return re.search(r"\[(.*?)\]", part).group(1)

    def _parse(self, expression: str) -> list:
        parts = expression.split("+.+")
        parsed = []
        for part in parts:
            if "+" in part:
                subs = part.split("+")
                parsed.append([self._parse_slice(self._extract(sub)) for sub in subs])
            else:
                parsed.append([self._parse_slice(self._extract(part))])
        return parsed

    @staticmethod
    def _build_str(parsed: list, lot_number: str) -> str:
        result = []
        for group in parsed:
            current = []
            for segment in group:
                start = segment[0]
                end = segment[1] + 1 if len(segment) > 1 else start + 1
                current.append(lot_number[start:end])
            result.append("".join(current))
        return ".".join(result)

    def get_dict(self, lot_number: str) -> dict:
        inner = self._build_str(self.lot, lot_number)
        resolved = self._build_str(self.lot_res, lot_number)
        parts = inner.split(".")
        root: dict = {}
        current = root
        for idx, part in enumerate(parts):
            if idx == len(parts) - 1:
                current[part] = resolved
            else:
                current[part] = current.get(part, {})
                current = current[part]
        return root


_lot_parser = LotParser()


class GeetestSigner:
    _encryptor_pubkey = construct(
        (
            int(
                "00C1E3934D1614465B33053E7F48EE4EC87B14B95EF88947713D25EECBFF7E74C"
                "7977D02DC1D9451F79DD5D1C10C29ACB6A9B4D6FB7D0A0279B6719E1772565F"
                "09AF627715919221AEF91899CAE08C0D686D748B20A3603BE2318CA6BC2B5970"
                "6592A9219D0BF05C9F65023A21D2330807252AE0066D59CEEFA5F2748EA80BAB81",
                16,
            ),
            int("10001", 16),
        )
    )

    @staticmethod
    def _rand_uid() -> str:
        parts = []
        for _ in range(4):
            parts.append(hex(int(65536 * (1 + random.random())))[2:].zfill(4)[-4:])
        return "".join(parts)

    @staticmethod
    def _encrypt_symmetrical(plaintext: str, random_key: str) -> bytes:
        key = random_key.encode("utf-8")
        iv = b"0000000000000000"
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))

    @staticmethod
    def _encrypt_asymmetric(message: str) -> str:
        cipher = PKCS1_v1_5.new(GeetestSigner._encryptor_pubkey)
        encrypted = cipher.encrypt(message.encode("utf-8"))
        return binascii.hexlify(encrypted).decode("utf-8")

    @staticmethod
    def encrypt_w(raw_input: str, pt: str) -> str:
        if not pt or pt == "0":
            return urllib.parse.quote_plus(raw_input)
        random_uid = GeetestSigner._rand_uid()
        enc_key = GeetestSigner._encrypt_asymmetric(random_uid)
        enc_input = GeetestSigner._encrypt_symmetrical(raw_input, random_uid)
        return binascii.hexlify(enc_input).decode() + enc_key

    @staticmethod
    def generate_pow(
        lot_number: str,
        captcha_id: str,
        hash_func: str,
        hash_version: str,
        bits: int,
        date: str,
        empty: str = "",
    ) -> dict:
        bit_remainder = bits % 4
        bit_division = bits // 4
        prefix = "0" * bit_division
        pow_string = (
            f"{hash_version}|{bits}|{hash_func}|{date}|{captcha_id}|{lot_number}|{empty}|"
        )
        while True:
            suffix = GeetestSigner._rand_uid()
            combined = pow_string + suffix
            if hash_func == "md5":
                hashed = hashlib.md5(combined.encode("utf-8")).hexdigest()
            elif hash_func == "sha1":
                hashed = hashlib.sha1(combined.encode("utf-8")).hexdigest()
            else:
                hashed = hashlib.sha256(combined.encode("utf-8")).hexdigest()

            if bit_remainder == 0:
                if hashed.startswith(prefix):
                    return {"pow_msg": pow_string + suffix, "pow_sign": hashed}
            elif hashed.startswith(prefix):
                threshold = {"1": 7, "2": 3, "3": 1}.get(str(bit_remainder), 1)
                if len(prefix) <= threshold:
                    return {"pow_msg": pow_string + suffix, "pow_sign": hashed}

    @staticmethod
    def generate_w(
        load_data: dict,
        captcha_id: str,
        *,
        userresponse: object,
        passtime: int | None = None,
    ) -> str:
        lot_number = load_data["lot_number"]
        pow_detail = load_data["pow_detail"]
        base = {
            "jCpk": "yZ7D",
            **GeetestSigner.generate_pow(
                lot_number,
                captcha_id,
                pow_detail["hashfunc"],
                pow_detail["version"],
                pow_detail["bits"],
                pow_detail["datetime"],
            ),
            **_lot_parser.get_dict(lot_number),
            "biht": "1426265548",
            "device_id": "",
            "em": {
                "cp": 0,
                "ek": "11",
                "nt": 0,
                "ph": 0,
                "sc": 0,
                "si": 0,
                "wd": 0,
            },
            "gee_guard": {
                "roe": {
                    "auh": "3",
                    "aup": "3",
                    "cdc": "3",
                    "egp": "3",
                    "res": "3",
                    "rew": "3",
                    "sep": "3",
                    "snh": "3",
                }
            },
            "ep": "123",
            "geetest": "captcha",
            "lang": "zh",
            "lot_number": lot_number,
            "passtime": passtime or random.randint(800, 1500),
            "userresponse": userresponse,
        }
        return GeetestSigner.encrypt_w(
            json.dumps(base, separators=(",", ":")),
            str(load_data.get("pt") or "1"),
        )
