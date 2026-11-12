"""CrocoPay Host-to-Host integration (https://crocopay.tech/developer).

Auth is via the `Client-Id` / `Client-Secret` headers. A successful payment is
confirmed ONLY by the signed webhook callback; a redirect to `success_url` is
not proof of payment. The webhook body carries no invoice id, so the internal
record id must be embedded in the `callback_url` query string.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import aiohttp

from app.core.config import get_settings

log = logging.getLogger(__name__)

CROCOPAY_HOST = "https://crocopay.tech"
API_PREFIX = "/api/v2/h2h"

CREATE_INVOICE_URL = f"{CROCOPAY_HOST}{API_PREFIX}/invoices"
GET_INVOICE_URL = f"{CROCOPAY_HOST}{API_PREFIX}/invoices"
AVAILABLE_METHODS_URL = f"{CROCOPAY_HOST}{API_PREFIX}/payment-method/available"


class CrocoPayError(Exception):
    """Provider-level refusal/error."""


class CrocoPayRequestError(CrocoPayError):
    """Transport or HTTP-level failure."""


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Client-Id": settings.croco_client_id,
        "Client-Secret": settings.croco_client_secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def create_invoice(
    *,
    amount: int,
    callback_url: str,
    success_url: str | None = None,
    cancel_url: str | None = None,
    currency: str | None = None,
    payment_option: str | None = None,
) -> dict:
    """Create a payment invoice and return the provider response dict.

    The response's ``card`` field is either a card/phone number or (when it
    starts with http:// or https://) a payment link / QR-code URL.
    """
    settings = get_settings()
    payload: dict[str, object] = {
        "amount": int(amount),
        "currency": currency or settings.croco_currency,
        "payment_option": payment_option or settings.croco_payment_option,
        "callback_url": callback_url,
    }
    if success_url:
        payload["success_url"] = success_url
    if cancel_url:
        payload["cancel_url"] = cancel_url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(CREATE_INVOICE_URL, headers=_headers(), json=payload) as resp:
                body = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise CrocoPayRequestError(str(exc)) from exc
    if resp.status != 200:
        raise CrocoPayRequestError(f"CrocoPay HTTP {resp.status}: {body}")
    if isinstance(body, dict) and body.get("status") == "error":
        raise CrocoPayError(str(body.get("message") or "crocopay error"))
    return body


async def get_invoice(invoice_id: str) -> dict:
    """Fetch an invoice's current status and payment details."""
    url = f"{GET_INVOICE_URL}/{invoice_id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers=_headers()) as resp:
                body = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise CrocoPayRequestError(str(exc)) from exc
    if resp.status != 200:
        raise CrocoPayRequestError(f"CrocoPay HTTP {resp.status}: {body}")
    return body


def verify_webhook_sign(payload: dict) -> bool:
    """Verify the CrocoPay HMAC-SHA256 webhook signature.

    Expected signature is computed over the pipe-separated fields using the
    merchant's ``Client-Secret``. The comparison is constant-time.
    """
    try:
        expected_sign = str(payload["sign"])
        message = "{timestamp}|{subtotal}|{percentage}|{charge_percentage}|{charge_fixed}|{total}".format(**payload)
    except (KeyError, TypeError, ValueError):
        return False
    digest = hmac.new(
        get_settings().croco_client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, expected_sign)
