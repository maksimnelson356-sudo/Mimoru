from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_blocked_precheckout_is_explicitly_rejected() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    blocked = source.split("if user.service_blocked and not completed_payment:", 1)[1].split(
        '# "Недотрога"', 1
    )[0]
    assert "isinstance(event, PreCheckoutQuery)" in blocked
    assert "ok=False" in blocked
    assert "Новая оплата сейчас недоступна" in blocked
    assert "return None" in blocked


def test_successful_payment_bypasses_service_block_short_circuit() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    assert "completed_payment = (" in source
    assert "isinstance(event, Message)" in source
    assert "event.successful_payment is not None" in source
    assert "if user.service_blocked and not completed_payment:" in source


def test_ordinary_blocked_messages_and_callbacks_stay_denied() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    blocked = source.split("if user.service_blocked and not completed_payment:", 1)[1].split(
        '# "Недотрога"', 1
    )[0]
    assert "isinstance(event, CallbackQuery)" in blocked
    assert 'await event.answer(' in blocked
    assert "Доступ к Mimoru ограничен" in blocked
    assert "isinstance(event, Message)" in blocked
    assert "Обратитесь в поддержку" in blocked


def test_completed_payment_handler_keeps_idempotent_charge_claims() -> None:
    billing = (ROOT / "app/handlers/billing.py").read_text(encoding="utf-8")
    assert "@router.message(F.successful_payment)" in billing
    assert "await _locked_payment" in billing
    assert "await _locked_global_post" in billing
    assert "await _commit_payment_once" in billing
    assert "successful.telegram_payment_charge_id" in billing
