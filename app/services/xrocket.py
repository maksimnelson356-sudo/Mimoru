"""XRocket Pay API v1 integration (https://docs.xrocket.exchange).

Auth is via an `Authorization: Bearer <API_KEY>` header. An invoice is created
for a fiat/crypto currency; the buyer pays inside Telegram using the returned
``links.telegramBotLink``. The internal record id is passed as
``clientInvoiceId`` and echoed in the webhook callback payload.
"""

from __future__ import annotations

import logging

import aiohttp

from app.core.config import get_settings

log = logging.getLogger(__name__)

XROCKET_API_V1 = "https://pay.api.xrocket.exchange/api/v1"

CREATE_INVOICE_URL = f"{XROCKET_API_V1}/invoices"
GET_INVOICE_URL = f"{XROCKET_API_V1}/invoice"

# Terminal "paid" invoice statuses reported by the XRocket webhook/GET invoice.
PAID_STATUSES = {"paid", "finished", "completed"}


class XRocketError(Exception):
    """Provider-level refusal/error."""


class XRocketRequestError(XRocketError):
    """Transport or HTTP-level failure."""


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.xrocket_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def create_invoice(
    *,
    amount: float,
    client_invoice_id: str,
    description: str,
    callback_url: str,
    callback_payload: dict | None = None,
    expires_in: int = 3600,
    currency: str | None = None,
) -> dict:
    """Create an invoice; returns the full provider response dict."""
    settings = get_settings()
    pay_currency = currency or settings.xrocket_currency
    body = {
        "priceCurrency": pay_currency,
        "priceAmount": int(amount),
        "clientInvoiceId": client_invoice_id,
        "description": description,
        "expiresIn": expires_in,
        "callback": {
            "callbackUrl": callback_url,
            "payload": callback_payload or {},
        },
        "payCurrencies": [pay_currency],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(CREATE_INVOICE_URL, headers=_headers(), json=body) as resp:
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise XRocketRequestError(str(exc)) from exc
    if resp.status != 200:
        raise XRocketRequestError(f"XRocket HTTP {resp.status}: {data}")
    return data


async def get_invoice(client_invoice_id: str) -> dict:
    """Fetch an invoice's current state by its client-side id."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(
                GET_INVOICE_URL,
                headers=_headers(),
                params={"clientInvoiceId": client_invoice_id},
            ) as resp:
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise XRocketRequestError(str(exc)) from exc
    if resp.status != 200:
        raise XRocketRequestError(f"XRocket HTTP {resp.status}: {data}")
    return data


def is_paid_status(status: str) -> bool:
    return status in PAID_STATUSES
