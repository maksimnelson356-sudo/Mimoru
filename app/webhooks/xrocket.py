"""XRocket webhook handling.

The internal record reference is carried as ``clientInvoiceId`` (``s<id>`` /
``g<id>``) and echoed in the webhook body/callback payload. Non-final invoice
statuses are acknowledged but ignored.
"""

from __future__ import annotations

import json
import re

import structlog

from app.db.session import SessionFactory
from app.services.global_post_payments import activate_global_post_payment
from app.services.subscription_payments import activate_subscription_payment
from app.services.xrocket import is_paid_status

_REF_RE = re.compile(r"^(s|g)(\d+)$")


def _resolve_reference(payload: dict) -> str:
    ref = payload.get("clientInvoiceId") or payload.get("client_invoice_id")
    if isinstance(ref, str) and ref:
        return ref
    callback = payload.get("callback")
    if isinstance(callback, dict):
        inner = callback.get("payload")
        if isinstance(inner, dict):
            inner = inner.get("clientInvoiceId") or inner.get("client_invoice_id") or inner.get("ref")
        if isinstance(inner, str) and inner:
            return inner
    return ""


async def handle_xrocket_webhook(body: bytes) -> tuple[str, bytes]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "400 Bad Request", b"bad json\n"
    if not isinstance(payload, dict):
        return "400 Bad Request", b"bad json\n"
    status = str(payload.get("status") or "").lower()
    if status and not is_paid_status(status):
        return "200 OK", b"ignored\n"
    match = _REF_RE.match(_resolve_reference(payload))
    if not match:
        return "400 Bad Request", b"unknown reference\n"
    kind, num = match.group(1), match.group(2)
    record_id = int(num)
    charge_id = str(payload.get("id") or f"xrocket:{kind}:{record_id}")
    try:
        async with SessionFactory() as session:
            if kind == "s":
                await activate_subscription_payment(
                    session, payment_id=record_id, charge_id=charge_id, provider="xrocket"
                )
            else:
                await activate_global_post_payment(session, request_id=record_id, charge_id=charge_id)
    except Exception:
        structlog.get_logger().exception("xrocket_webhook_failed", kind=kind, record_id=record_id)
        return "500 Internal Server Error", b"error\n"
    return "200 OK", b"OK\n"
