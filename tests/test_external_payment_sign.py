"""Unit tests for external payment provider helpers (CrocoPay / XRocket)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.services.crocopay import verify_webhook_sign
from app.services.xrocket import is_paid_status


class _FakeSettings:
    croco_client_secret = "test-secret"


@pytest.fixture(autouse=True)
def _fake_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.crocopay.get_settings", lambda: _FakeSettings())


def _sign(payload: dict, secret: str = "test-secret") -> str:
    message = (
        "{timestamp}|{subtotal}|{percentage}|{charge_percentage}|{charge_fixed}|{total}".format(**payload)
    )
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _payload() -> dict:
    return {
        "timestamp": "1",
        "subtotal": "500000",
        "percentage": "0",
        "charge_percentage": "0",
        "charge_fixed": "0",
        "total": "500000",
    }


def test_crocopay_verify_sign_accepts_valid() -> None:
    payload = _payload()
    payload["sign"] = _sign(payload)
    assert verify_webhook_sign(payload) is True


def test_crocopay_verify_sign_rejects_wrong_secret() -> None:
    payload = _payload()
    payload["sign"] = _sign(payload, secret="other-secret")
    assert verify_webhook_sign(payload) is False


def test_crocopay_verify_sign_rejects_missing_fields() -> None:
    assert verify_webhook_sign({"sign": "abc"}) is False
    assert verify_webhook_sign({}) is False


def test_xrocket_paid_statuses() -> None:
    assert is_paid_status("paid") is True
    assert is_paid_status("finished") is True
    assert is_paid_status("completed") is True
    assert is_paid_status("pending") is False
    assert is_paid_status("expired") is False
    assert is_paid_status("failed") is False
