"""
Crypto utilities for iBox API.

Encryption mechanism (confirmed from log analysis):
  - Per-request: client generates a random 16-char lowercase hex string as AES key
  - Body: AES/ECB/PKCS5Padding with the random key
  - Key transport: RSA/ECB/PKCS1Padding with the server's fixed public key
  - Response: AES/ECB/PKCS5Padding; key comes from the HTTP response
    (header or wrapper field — exact format requires HTTP-level capture to confirm)
"""

import base64
import secrets
import string

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


# Fixed server RSA public key (extracted from log)
_SERVER_RSA_PUBKEY_B64 = (
    "MIGdMA0GCSqGSIb3DQEBAQUAA4GLADCBhwKBgQCYTCBrxwIYTvKmvg3C2/FPm+M4"
    "smdeKXIiBONCXzOOxfHdOgGRvea1LtoBR/kJ4OpePsF/2CNy3vS591XhY3ipUAap"
    "NJqTUHGf01L4Y6+NKdiyUC2VcrLrEEjAGuCsWH8yiccQ6bBEdFJDX0b+VXRRPVQm"
    "kagSnVu5TKq3XGnMcQIBAw=="
)

_HEX_CHARS = string.digits + "abcdef"


def generate_aes_key() -> bytes:
    """
    Generate a random 16-char lowercase hex string, returned as 16 ASCII bytes.
    Matches iBox app: key is a hex-looking string (e.g. b'5d3c243acb0c4a58'),
    NOT raw random binary — each byte is an ASCII hex digit.
    """
    key_str = "".join(secrets.choice(_HEX_CHARS) for _ in range(16))
    return key_str.encode("ascii")


def aes_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES/ECB/PKCS5Padding encrypt; returns raw ciphertext bytes."""
    if len(key) != 16:
        raise ValueError(f"AES key must be exactly 16 bytes, got {len(key)}")
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(pad(plaintext, block_size=16))


def aes_ecb_encrypt_b64(key: bytes, plaintext: bytes) -> str:
    """AES/ECB/PKCS5Padding encrypt; returns Base64 string."""
    return base64.b64encode(aes_ecb_encrypt(key, plaintext)).decode("ascii")


def aes_ecb_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """AES/ECB/PKCS5Padding decrypt; returns plaintext bytes."""
    if len(key) != 16:
        raise ValueError(f"AES key must be exactly 16 bytes, got {len(key)}")
    cipher = AES.new(key, AES.MODE_ECB)
    return unpad(cipher.decrypt(ciphertext), block_size=16)


def aes_ecb_decrypt_b64(key: bytes, ciphertext_b64: str) -> bytes:
    """AES/ECB decrypt from Base64 ciphertext; returns plaintext bytes."""
    return aes_ecb_decrypt(key, base64.b64decode(ciphertext_b64))


def rsa_encrypt_key(aes_key: bytes, pubkey_b64: str = _SERVER_RSA_PUBKEY_B64) -> str:
    """
    RSA/ECB/PKCS1Padding encrypt the AES key with the server's public key.
    Returns Base64-encoded encrypted key.
    """
    pub = RSA.import_key(base64.b64decode(pubkey_b64))
    cipher = PKCS1_v1_5.new(pub)
    encrypted = cipher.encrypt(aes_key)
    return base64.b64encode(encrypted).decode("ascii")


def make_request_pair(plaintext: bytes) -> tuple[bytes, str, str]:
    """
    Prepare a complete encrypted request pair.

    Returns:
        (aes_key, encrypted_body_b64, encrypted_key_b64)
        - aes_key: raw 16-byte key used (keep for response decryption if needed)
        - encrypted_body_b64: AES/ECB encrypted body, Base64
        - encrypted_key_b64: RSA encrypted AES key, Base64

    Usage (HTTP body format TBD — confirm from chucker/HTTP capture):
        Likely: {"data": encrypted_body_b64, "key": encrypted_key_b64}
        or: body = encrypted_body_b64, header X-Key = encrypted_key_b64
    """
    aes_key = generate_aes_key()
    body_b64 = aes_ecb_encrypt_b64(aes_key, plaintext)
    key_b64 = rsa_encrypt_key(aes_key)
    return aes_key, body_b64, key_b64
