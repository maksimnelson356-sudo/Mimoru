from pathlib import Path

from app.services.plans import PLAN_CATALOG


ROOT = Path(__file__).resolve().parents[1]


def test_plan_catalog_is_registered_before_legacy_navigation():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    router_block = main.split("dp.include_routers(", 1)[1]
    routers = [line.strip().rstrip(",") for line in router_block.splitlines() if line.strip().endswith(".router,")]
    assert "plan_catalog.router" in routers
    assert routers.index("plan_catalog.router") < routers.index("navigation.router")
    assert routers.index("plan_catalog.router") < routers.index("dashboard.router")


def test_catalog_selects_tariff_before_group():
    code = (ROOT / "app/handlers/plan_catalog.py").read_text(encoding="utf-8")
    assert 'callback_data="plans_catalog:standard"' in code
    assert 'callback_data="plans_catalog:pro"' in code
    assert 'callback_data=f"plans_choose_group:{plan_code}"' in code
    assert 'callback_data=f"plans_apply:{plan_code}:{group.id}:catalog"' in code


def test_invoice_back_edits_to_plan_description_instead_of_deleting():
    code = (ROOT / "app/handlers/plan_catalog.py").read_text(encoding="utf-8")
    assert "plan_invoice_back:" in code
    block = code.split("async def invoice_back", 1)[1]
    assert "await callback.message.edit_text(" in block


def test_plan_descriptions_show_real_differences_and_equal_op_limit():
    code = (ROOT / "app/handlers/plan_catalog.py").read_text(encoding="utf-8")
    for text in (
        "до 10 запрещённых слов",
        "до 100 запрещённых слов",
        "до 1000 запрещённых слов",
        "до 5 обязательных подписок",
        "ежедневные отчёты",
        "рекламному маркетплейсу",
        "приоритетная поддержка",
    ):
        assert text in code
    assert "1 обязательный канал" not in code
    assert "до 3 обязательных каналов" not in code
    assert "до 10 обязательных каналов" not in code


def test_moderator_and_required_channel_limits_are_not_tariff_differences():
    moderator_values = {plan["limits"]["moderators"] for plan in PLAN_CATALOG.values()}
    channel_values = {plan["limits"]["channels"] for plan in PLAN_CATALOG.values()}
    assert moderator_values == {1_000_000}
    assert channel_values == {5}
