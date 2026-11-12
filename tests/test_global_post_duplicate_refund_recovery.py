from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_duplicate_refund_schema_is_created_at_startup() -> None:
    schema = (ROOT / "app/services/ad_market_schema.py").read_text(encoding="utf-8")
    assert "GlobalPostDuplicateRefund" in schema
    assert "GlobalPostDuplicateRefund.__table__" in schema


def test_live_refund_claim_is_committed_before_telegram_side_effect() -> None:
    service = (ROOT / "app/services/global_post_refunds.py").read_text(encoding="utf-8")
    ensure = service.split("async def ensure_duplicate_refund", 1)[1].split(
        "async def attempt_duplicate_refund", 1
    )[0]
    attempt = service.split("async def attempt_duplicate_refund", 1)[1].split(
        "async def record_and_attempt_duplicate_refund", 1
    )[0]
    live = service.split("async def record_and_attempt_duplicate_refund", 1)[1].split(
        "async def recover_pending_duplicate_refunds", 1
    )[0]

    assert 'status="pending"' in ensure
    assert "await session.commit()" in ensure
    assert live.index("await ensure_duplicate_refund(") < live.index(
        "await attempt_duplicate_refund(bot, session, refund_id)"
    )
    assert ".with_for_update()" in attempt
    assert "refund_star_payment" in attempt
    assert attempt.index(".with_for_update()") < attempt.index("refund_star_payment")


def test_refund_retry_is_idempotent_and_keeps_failed_obligation_pending() -> None:
    service = (ROOT / "app/services/global_post_refunds.py").read_text(encoding="utf-8")
    attempt = service.split("async def attempt_duplicate_refund", 1)[1].split(
        "async def record_and_attempt_duplicate_refund", 1
    )[0]

    assert 'if row.status == "refunded":' in attempt
    assert "_already_refunded(error)" in attempt
    assert 'row.status = "refunded"' in attempt
    failed = attempt.split("if not _already_refunded(error):", 1)[1].split(
        'row.status = "refunded"', 1
    )[0]
    assert "row.last_error =" in failed
    assert "await session.commit()" in failed
    assert 'row.status = "pending"' not in failed


def test_leader_worker_runs_and_stops_duplicate_refund_recovery() -> None:
    leader = (ROOT / "app/services/background_leader.py").read_text(encoding="utf-8")
    assert "recover_pending_duplicate_refunds" in leader
    assert "_recover_duplicate_refunds_periodically" in leader
    assert 'name="global-post-duplicate-refund-recovery"' in leader
    worker = leader.split("async def _run_leader_worker", 1)[1].split(
        "async def leader_background_loop", 1
    )[0]
    assert "refund_recovery = asyncio.create_task(" in worker
    assert "await stop_task(refund_recovery, timeout=2.0)" in worker


def test_duplicate_charge_id_is_unique_and_original_request_charge_is_not_overwritten() -> None:
    model = (ROOT / "app/db/payment_refund_models.py").read_text(encoding="utf-8")
    service = (ROOT / "app/services/global_post_payments.py").read_text(encoding="utf-8")
    assert "telegram_payment_charge_id" in model
    assert "unique=True" in model

    # duplicate guards short-circuit before any charge overwrite in the card flow
    assert "if item.payment_charge_id == charge_id and item.status in {\"paid\", \"completed\"}:" in service
    assert "if item.payment_charge_id is not None and item.payment_charge_id != charge_id:" in service
    assert "return \"duplicate\"" in service
