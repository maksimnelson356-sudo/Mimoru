"""CrocoPay webhook handling.

The webhook body carries no invoice id, so the internal record id is read from
the callback URL query string (``kind=s|g`` + ``id=<id>``) that we embed at
invoice creation. Only the HMAC signature authenticates the payment.
"""

from __future__ import annotations

import json

import structlog

from app.db.session import SessionFactory
from app.services.crocopay import verify_webhook_sign
from app.services.global_post_payments import activate_global_post_payment
from app.services.subscription_payments import activate_subscription_payment


def _provider_charge_id(kind: str, record_id: int) -> str:
    return f"crocopay:{kind}:{record_id}"


async def handle_crocopay_webhook(body: bytes, query: dict | None) -> tuple[str, bytes]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "400 Bad Request", b"bad json\n"
    if not isinstance(payload, dict) or not verify_webhook_sign(payload):
        return "403 Forbidden", b"bad sign\n"
    query = query or {}
    kind = query.get("kind")
    raw_id = query.get("id")
    if not raw_id or not str(raw_id).isdigit():
        return "400 Bad Request", b"unknown reference\n"
    record_id = int(raw_id)
    charge_id = _provider_charge_id(kind or "", record_id)
    try:
        async with SessionFactory() as session:
            if kind == "s":
                await activate_subscription_payment(
                    session, payment_id=record_id, charge_id=charge_id, provider="crocopay"
                )
            elif kind == "g":
                await activate_global_post_payment(session, request_id=record_id, charge_id=charge_id)
            else:
                return "400 Bad Request", b"unknown reference\n"
    except Exception:
        structlog.get_logger().exception("crocopay_webhook_failed", kind=kind, record_id=record_id)
        return "500 Internal Server Error", b"error\n"
    return "200 OK", b"OK\n"
