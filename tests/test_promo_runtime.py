from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_redemption_locks_promo_and_group_before_mutation() -> None:
    source = (ROOT / "app/services/promo_redemption.py").read_text(encoding="utf-8")
    helper = source.split("async def _redeem_locked_promo", 1)[1].split(
        "async def redeem_promo_code", 1
    )[0]
    code_path = source.split("async def redeem_promo_code", 1)[1].split(
        "async def redeem_promo_id", 1
    )[0]
    id_path = source.split("async def redeem_promo_id", 1)[1]

    # In helper: mutations happen after locks (Group already locked by caller)
    use_increment = helper.index("promo.current_uses += 1")
    expiry_write = helper.index("group.plan_expires_at = extend_plan")
    flush = helper.index("await session.flush()")
    assert use_increment < flush
    assert expiry_write < flush

    # In redeem_promo_code: Group lock before PromoCode lock
    code_group_lock = code_path.index("select(Group).where(")
    code_promo_lock = code_path.index("select(PromoCode).where(PromoCode.code == code).with_for_update()")
    code_redeem = code_path.index("return await _redeem_locked_promo(")
    assert code_group_lock < code_promo_lock < code_redeem

    # In redeem_promo_id: Group lock before PromoCode lock
    id_group_lock = id_path.index("select(Group).where(")
    id_promo_lock = id_path.index("select(PromoCode).where(PromoCode.id == promo_id).with_for_update()")
    id_redeem = id_path.index("return await _redeem_locked_promo(")
    assert id_group_lock < id_promo_lock < id_redeem


def test_redemption_enforces_owner_and_one_use_per_user() -> None:
    source = (ROOT / "app/services/promo_redemption.py").read_text(encoding="utf-8")
    assert "PromoCodeUse.promo_code_id == promo.id" in source
    assert "PromoCodeUse.user_telegram_id == user_telegram_id" in source
    assert "Group.owner_telegram_id == user_telegram_id" in source
    assert "Group.is_active.is_(True)" in source
    assert "promo_is_available(" in source
    assert "session.add(PromoCodeUse(" in source
    assert 'event_type="promo_redeem"' in source


def test_user_runtime_exposes_stateless_owned_group_choice() -> None:
    source = (ROOT / "app/handlers/promo_redemption.py").read_text(encoding="utf-8")
    assert '@router.message(Command("promo"), F.chat.type == "private")' in source
    assert "Group.owner_telegram_id == message.from_user.id" in source
    assert 'callback_data=f"promo_redeem:{group.id}:{promo_id}"' in source
    assert 'F.data.regexp(r"^promo_redeem:\\d+:\\d+$")' in source
    assert 'callback.message.chat.type != "private"' in source
    assert "await redeem_promo_code(" in source
    assert "await redeem_promo_id(" in source
    assert "FSMContext" not in source
    assert "StatesGroup" not in source
    assert "await session.commit()" in source
    assert "await session.rollback()" not in source

    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert main.index("promo_redemption.router") < main.index("plan_legacy_redirect.router")


def test_legacy_promo_text_routes_through_atomic_service() -> None:
    source = (ROOT / "app/handlers/plan_legacy_redirect.py").read_text(encoding="utf-8")
    handler = source.split("async def legacy_promo_text", 1)[1].split(
        "@router.callback_query", 1
    )[0]
    assert 'F.text.regexp(r"(?i)^промокод [A-Za-z0-9_-]+ \\d+$")' in source
    assert "await redeem_code_for_group(" in handler
    assert "group.plan_code" not in handler
    assert "group.plan_expires_at" not in handler
    assert "PromoCodeUse(" not in handler

    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert main.index("plan_legacy_redirect.router") < main.index("client_management.router")


def test_service_owner_runtime_can_manage_promos() -> None:
    source = (ROOT / "app/handlers/service_management_fixes.py").read_text(encoding="utf-8")
    assert '@router.message(Command("promos"), F.chat.type == "private")' in source
    assert '@router.message(Command("promo_create"), F.chat.type == "private")' in source
    assert '@router.message(Command("promo_off"), F.chat.type == "private")' in source
    assert source.count("is_service_owner(message.from_user.id)") >= 3
    assert "normalize_promo_code(parts[1])" in source
    assert "promo.active = False" in source
    assert "except IntegrityError:" in source


def test_legacy_promo_disable_shares_locked_service_owner_path() -> None:
    source = (ROOT / "app/handlers/service_management_fixes.py").read_text(encoding="utf-8")
    helper = source.split("async def _disable_promo_locked", 1)[1].split(
        '@router.message(Command("promo_off")', 1
    )[0]
    modern = source.split("async def service_promo_off", 1)[1].split(
        "@router.message(F.chat.type", 1
    )[0]
    legacy = source.split("async def legacy_promo_off_serialized", 1)[1].split(
        "@router.message(F.chat.type", 1
    )[0]

    assert ".with_for_update()" in helper
    assert helper.index(".with_for_update()") < helper.index("promo.active = False")
    assert helper.index("promo.active = False") < helper.index("await session.commit()")
    assert "await _disable_promo_locked(" in modern
    assert "await _disable_promo_locked(" in legacy
    assert "is_service_owner(message.from_user.id)" in legacy
    assert 'F.text.regexp(r"(?i)^отключить промокод [A-Za-z0-9_-]+$")' in source

    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert main.index("service_management_fixes.router") < main.index("client_management.router")


def test_runtime_help_documents_both_promo_flows() -> None:
    user_source = (ROOT / "app/handlers/promo_redemption.py").read_text(encoding="utf-8")
    owner_source = (ROOT / "app/handlers/service_management_fixes.py").read_text(encoding="utf-8")
    assert "/promo START-7" in user_source
    assert "/promo_create CODE PLAN DAYS MAX_USES [YYYY-MM-DD]" in owner_source
    assert "/promo_off CODE" in owner_source
