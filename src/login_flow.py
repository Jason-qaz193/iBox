"""
Login flow: send SMS code -> login with verification code.

No Frida / key file needed — encryption is handled fully in Python
using per-request random AES keys + RSA key transport.
"""

from __future__ import annotations

from .api_client import IBoxClient


def login(
    base_url: str,
    mobile: str,
    verification_code: str,
    c_id: str,
    invitation_code: str = "",
    enable: int = 1,
    login_path: str = "/personal-center-service/login/mobile",
    headers: dict = None,
) -> tuple[IBoxClient, dict]:
    """
    Encrypt login payload and call iBox login API.

    Returns (client, response_dict).
    client.token is set if login succeeds.
    """
    client = IBoxClient(base_url, headers=headers)
    result = client.login(
        mobile=mobile,
        verification_code=verification_code,
        c_id=c_id,
        invitation_code=invitation_code,
        enable=enable,
        path=login_path,
    )
    return client, result


def send_sms_and_login(
    base_url: str,
    mobile: str,
    verification_code: str,
    c_id: str,
    invitation_code: str = "",
    sms_path: str = "/personal-center-service/login/sendSms",
    login_path: str = "/personal-center-service/login/mobile",
    headers: dict = None,
) -> tuple[IBoxClient, dict, dict]:
    """
    Send SMS code first, then login.

    Returns (client, sms_result, login_result).
    """
    client = IBoxClient(base_url, headers=headers)
    sms_result = client.send_sms_code(mobile, path=sms_path)
    login_result = client.login(
        mobile=mobile,
        verification_code=verification_code,
        c_id=c_id,
        invitation_code=invitation_code,
        path=login_path,
    )
    return client, sms_result, login_result
