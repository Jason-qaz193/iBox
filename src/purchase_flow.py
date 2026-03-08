"""
Full flow: login -> add to cart -> create order.
Uses same session/token from login for subsequent calls.
"""

from typing import Optional

from .login_flow import login
from .api_client import IBoxClient


def run_purchase_flow(
    base_url: str,
    login_path: str,
    cart_path: str,
    order_path: str,
    mobile: str,
    verification_code: str,
    c_id: str,
    invitation_code: str = "",
    product_id: Optional[str] = None,
    quantity: int = 1,
    cart_payload: Optional[dict] = None,
    order_payload: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> tuple[IBoxClient, dict]:
    """
    Login, then add to cart (if product_id or cart_payload given), then create order.
    Returns (client, {"login": result_dict, "cart": result or None, "order": result or None}).
    """
    client, login_result = login(
        base_url=base_url,
        mobile=mobile,
        verification_code=verification_code,
        c_id=c_id,
        invitation_code=invitation_code,
        login_path=login_path,
        headers=headers,
    )
    results: dict = {"login": login_result}

    if not (isinstance(login_result, dict) and login_result.get("code") == 0):
        return client, results

    if cart_path and (cart_payload is not None or product_id is not None):
        payload = cart_payload if cart_payload is not None else {"productId": product_id, "quantity": quantity}
        results["cart"] = client.add_cart(cart_path, payload=payload)

    if order_path:
        results["order"] = client.create_order(order_path, payload=order_payload)

    return client, results
