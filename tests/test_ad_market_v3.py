from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_current_marketplace_models_exist_without_legacy_runtime_models() -> None:
    models = _read("app/db/ad_market_models.py")
    for model in (
        "RequiredAdListing",
        "RequiredAdDealRequest",
        "GlobalPostRequest",
        "GlobalPostDelivery",
        "DirectRequiredRule",
    ):
        assert f"class {model}" in models
    for legacy in (
        "RequiredSubscriptionSeller",
        "RequiredSubscriptionCampaign",
        "RequiredSubscriptionConversion",
        "PostAdSellerSettings",
        "PostAdCampaign",
        "PostAdDelivery",
        "AdPlacementApproval",
    ):
        assert f"class {legacy}" not in models


def test_global_post_requires_service_owner_review_before_payment() -> None:
    handler = _read("app/handlers/ad_market_v3.py")
    billing = _read("app/handlers/billing.py")
    assert 'item.status = "pending_review"' in handler
    assert 'callback.from_user.id not in settings.service_owner_ids' in handler
    assert 'item.status = "approved"' in handler
    assert 'payload=f"globalpost:{item.id}"' in handler
    assert 'parts[0] == "globalpost"' in billing
    assert 'item.status != "approved"' in billing
    assert 'item.status = "paid"' in billing


def test_global_post_distribution_targets_all_active_groups() -> None:
    tasks = _read("app/tasks_ad_market.py")
    assert 'select(Group).where(Group.is_active.is_(True))' in tasks
    assert 'GlobalPostRequest.status == "paid"' in tasks
    assert "PostAdSellerSettings" not in tasks
    assert "AdPlacementApproval" not in tasks


def test_required_marketplace_is_direct_buyer_seller_flow() -> None:
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'callback_data="ads:buy:required"' in _read("app/handlers/ad_navigation.py")
    assert "RequiredAdListing" in handler
    assert "RequiredAdDealRequest" in handler
    assert 'text="💬 Связаться с продавцом"' in handler
    assert 'text="💬 Связаться с покупателем"' in handler
    assert 'callback_data=f"reqdeal:accept:{deal.id}"' in handler
    assert 'callback_data=f"reqdeal:reject:{deal.id}"' in handler
    required_section = handler.split("# Required-subscription marketplace", 1)[1]
    assert "answer_invoice" not in required_section


def test_catalog_does_not_expose_group_id_or_username() -> None:
    """#2: каталог покупателя не раскрывает ID/@username группы.

    Вместо проверки точной строки label — проверяем смысл:
    - блок required_market не использует telegram_chat_id в UI;
    - используется for_buyer=True для безопасного fallback;
    - в ad_navigation есть предупреждение про ID и @username.
    """
    handler = _read("app/handlers/ad_market_v3.py")
    catalog = handler.split("async def required_market(", 1)[1].split(
        "async def required_market_detail", 1
    )[0]
    # В каталоге покупателя не должно быть прямого раскрытия telegram_chat_id
    assert "telegram_chat_id" not in catalog, (
        "Каталог покупателя не должен показывать telegram_chat_id"
    )
    # Используется безопасный fallback
    assert "for_buyer=True" in catalog, (
        "Каталог покупателя должен использовать for_buyer=True"
    )
    # В ad_navigation предупреждение для UI сохранено
    assert "ID и @username площадки" in _read("app/handlers/ad_navigation.py")


def test_direct_required_commands_support_days_members_and_disable() -> None:
    handler = _read("app/handlers/required_direct.py")
    assert 'mode == "days"' in handler
    assert 'mode == "members"' in handler
    assert "подключить @channel 7 дней" in handler
    assert "подключить @channel 100 участников" in handler
    assert 'F.text.regexp(r"(?i)^отключить\\s+\\S+$")' in handler
    assert 'plan_limit(group, "channels")' in handler
    assert "ChatMemberStatus.ADMINISTRATOR" in handler
    assert "ChatMemberStatus.CREATOR" in handler
    assert "event.new_chat_member.user.is_bot" in handler


def test_new_advertising_text_inputs_have_contextual_cancel() -> None:
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'text="✖️ Отменить ввод"' in handler or '"✖️ Отменить ввод"' in handler
    assert 'await state.clear()' in handler
    assert 'gpost:editor:{item.id}' in handler
    assert 'reqlist:group:{group.id}' in handler
    assert 'reqmarket:{listing.id}' in handler


def test_old_advertising_buttons_redirect_instead_of_hanging() -> None:
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'F.data == "ads:sell:post"' in handler
    assert 'F.data.regexp(r"^ads:placement:\\d+$")' in handler
    assert "Отдельная продажа рекламных постов по группам больше не используется" in handler


def test_only_current_advertising_routers_are_registered() -> None:
    main = _read("app/main.py")
    order = main.split("dp.include_routers(", 1)[1].split(")", 1)[0]
    assert order.index("ad_invoice_safety.router") < order.index("ad_market_v3.router")
    assert order.index("ad_market_v3.router") < order.index("ad_navigation.router")
    assert order.index("ad_legacy_payment_guard.router") < order.index("billing.router")
    assert order.index("required_direct.router") < order.index("group.router")
    for legacy in (
        "ad_approvals.router",
        "ad_market_dashboard.router",
        "ad_post_market.router",
        "ad_required_market.router",
        "ad_required_safety.router",
        "advertising.router",
        "ad_input.router",
    ):
        assert legacy not in order


def test_legacy_ad_invoice_guard_only_blocks_obsolete_payloads() -> None:
    guard = _read("app/handlers/ad_legacy_payment_guard.py")
    assert "reqad|postad|adorder" in guard
    assert "globalpost" not in guard
    billing = _read("app/handlers/billing.py")
    for legacy in ("reqad", "postad", "adorder"):
        assert f'parts[0] == "{legacy}"' not in billing
