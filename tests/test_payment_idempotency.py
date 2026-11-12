from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _billing_source() -> str:
    return (ROOT / "app/handlers/billing.py").read_text(encoding="utf-8")


def test_successful_payment_uses_row_locks_for_claims() -> None:
    source = _billing_source()
    payment_lock = source.split("async def _locked_payment", 1)[1].split(
        "async def _locked_group", 1
    )[0]
    group_lock = source.split("async def _locked_group", 1)[1].split(
        "async def _charge_already_recorded", 1
    )[0]
    global_lock = source.split("async def _locked_global_post", 1)[1].split(
        "async def _locked_payment", 1
    )[0]
    assert ".with_for_update()" in payment_lock
    assert ".with_for_update()" in group_lock
    assert ".with_for_update()" in global_lock


def test_subscription_group_is_locked_before_expiry_extension() -> None:
    source = _billing_source()
    handler = source.split("async def successful_payment", 1)[1]
    subscription = handler.split('if len(parts) == 4 and parts[0] == "payment":', 1)[1]
    assert subscription.index("await _locked_group") < subscription.index("group.plan_expires_at =")
    assert subscription.index("payment.status = \"paid\"") < subscription.index(
        "await _commit_payment_once"
    )


def test_duplicate_charge_integrity_error_is_verified_before_suppression() -> None:
    source = _billing_source()
    commit_helper = source.split("async def _commit_payment_once", 1)[1].split(
        "def _already_refunded", 1
    )[0]
    assert "except IntegrityError:" in commit_helper
    assert "await session.rollback()" in commit_helper
    assert "await _charge_already_recorded" in commit_helper
    assert "raise" in commit_helper


def test_subscription_locks_group_before_payment() -> None:
    """Group must be locked before Payment, never the reverse.

    The refund recovery services lock Payment on its own, so a handler that took
    Payment first would form a Payment -> Group order against Group -> Payment
    and deadlock under concurrent payment and refund.
    """
    source = _billing_source()
    handler = source.split("async def successful_payment", 1)[1]
    subscription = handler.split('if len(parts) == 4 and parts[0] == "payment":', 1)[1]
    assert subscription.index("await _locked_group") < subscription.index("await _locked_payment")


def test_payment_timestamp_is_bound_before_first_use() -> None:
    """Regression guard: hoisting the Group lock above the validation block dropped
    the `now` assignment while its uses stayed, so every successful Stars payment
    raised NameError. Static gates caught it as F821 only after it shipped."""
    source = _billing_source()
    handler = source.split("async def successful_payment", 1)[1]
    subscription = handler.split('if len(parts) == 4 and parts[0] == "payment":', 1)[1]
    binding = "now = datetime.now(UTC)"
    assert subscription.count(binding) == 1
    first_use = min(
        subscription.index(needle)
        for needle in ("now=now", "else now", "paid_at = now")
    )
    assert subscription.index(binding) < first_use


def test_terminal_global_post_distinguishes_same_charge_from_distinct_charge() -> None:
    source = (ROOT / "app/services/global_post_payments.py").read_text(encoding="utf-8")
    assert "if item.payment_charge_id == charge_id and item.status in {\"paid\", \"completed\"}:" in source
    assert "if item.payment_charge_id is not None and item.payment_charge_id != charge_id:" in source
    assert "return \"duplicate\"" in source


def test_duplicate_global_post_charge_returns_without_overwriting_original() -> None:
    source = (ROOT / "app/services/global_post_payments.py").read_text(encoding="utf-8")
    assert "return \"duplicate\"" in source
    # idempotency guards must run before the paid/charge assignment
    assert source.index("return \"duplicate\"") < source.index('item.status = "paid"')
    assert "item.payment_charge_id = charge_id" in source


def test_terminal_subscription_records_distinguish_replay_from_distinct_charge() -> None:
    source = _billing_source()
    handler = source.split("async def successful_payment", 1)[1]
    subscription = handler.split('if len(parts) == 4 and parts[0] == "payment":', 1)[1]
    terminal = subscription.split('if payment.status in {"paid", "refunded"}:', 1)[1].split(
        'if payment.status == "refund_pending":', 1
    )[0]
    assert "payment.provider_payment_id == charge_id" in terminal
    assert "record_and_attempt_subscription_duplicate_refund" in terminal
    assert "payment.provider_payment_id = charge_id" not in terminal
    assert 'payment.status = "paid"' not in terminal
    assert 'payment.status = "refunded"' not in terminal


def test_both_payment_kinds_store_charge_ids() -> None:
    source = _billing_source()
    global_post = (ROOT / "app/services/global_post_payments.py").read_text(encoding="utf-8")
    assert "item.payment_charge_id = charge_id" in global_post
    assert "payment.provider_payment_id = charge_id" in source


def test_billing_is_production_successful_payment_winner() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "billing.router" in main
    legacy_guard = (ROOT / "app/handlers/ad_legacy_payment_guard.py").read_text(encoding="utf-8")
    assert "pre_checkout_query" in legacy_guard
    assert "successful_payment" not in legacy_guard
