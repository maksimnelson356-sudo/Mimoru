from pathlib import Path

from app import game_friendly_results as friendly
from app.entertainment_contracts import ENTERTAINMENT_ACTIONS, RELATIONSHIP_ACTIONS
from app.game_contracts import PROPOSALS


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_friendly_router_is_the_production_entertainment_and_relationship_implementation() -> None:
    preferences = _source("app/handlers/fun_preferences.py")
    social = _source("app/handlers/fun_social.py")
    results = _source("app/game_friendly_results.py")
    assert "router.include_router(game_friendly_results.router)" in preferences
    assert "@router.message" not in social
    assert "@router.callback_query" not in social
    assert "from app.entertainment_contracts import" in results
    assert "from app.game_contracts import PROPOSALS, PROPOSAL_ACTIONS" in results


def test_proposal_callback_is_serialized_and_rejects_replay() -> None:
    results = _source("app/game_friendly_results.py")
    callback_block = results.split("async def friendly_answer", 1)[1].split("async def friendly_divorce", 1)[0]
    assert ".with_for_update()" in callback_block
    assert 'GameEvent.outcome == "pending"' in callback_block
    assert '"На это предложение уже ответили."' in callback_block
    assert 'event.outcome = "cancelled"' in callback_block


def test_render_uses_clickable_mentions_without_raw_ids() -> None:
    text, entities = friendly._render(
        "💞 {actor} и {target}",
        {"actor": ("Алексей", 111111111), "target": ("Мария", 222222222)},
    )
    assert text == "💞 Алексей и Мария"
    assert "111111111" not in text
    assert "222222222" not in text
    assert [entity.url for entity in entities] == ["tg://user?id=111111111", "tg://user?id=222222222"]


def test_entertainment_actions_have_short_result_variants() -> None:
    assert len(friendly.ACTION_VARIANTS) >= 5
    assert all("{actor}" in item and "{target}" in item for item in friendly.ACTION_VARIANTS)
    assert all(len(item) < 180 for item in friendly.ACTION_VARIANTS)
    assert {"обнять", "поцеловать", "ударить", "уебать", "выебать", "дать дошик", "покормить"} <= ENTERTAINMENT_ACTIONS
    assert {"поссориться", "поругаться", "подраться", "помириться"} <= RELATIONSHIP_ACTIONS


def test_only_marriage_uses_confirmation_proposal_flow() -> None:
    assert set(friendly.PROPOSAL_TEMPLATES) == {"marry"}
    assert set(friendly.RESULT_VARIANTS) == {"marry"}
    assert len(friendly.PROPOSAL_TEMPLATES["marry"]) >= 3
    assert len(friendly.RESULT_VARIANTS["marry"]) >= 3
    assert {"пожениться", "выйти замуж", "сделать предложение"} <= set(PROPOSALS)
    assert all(kind == "marry" for kind, _ in PROPOSALS.values())


def test_user_facing_relationship_text_uses_mentions_not_raw_ids() -> None:
    results = _source("app/game_friendly_results.py")
    assert "tg://user?id=" in results
    assert "Пользователь {target_id}" not in results
    assert "Победитель: {winner}" not in results


# ── #3b: Redis cooldown ──────────────────────────────────────────────────

def test_cooldown_uses_redis_not_in_memory_dict() -> None:
    """#3b: cooldown должен жить в Redis, а не в in-memory dict."""
    results = _source("app/game_friendly_results.py")
    assert "from redis.asyncio import Redis" in results
    assert "_check_cooldown" in results
    assert "COOLDOWN_KEY_TEMPLATE" in results
    assert "mimoru:action-cooldown:" in results
    # In-memory dict убран
    assert "_action_cooldowns" not in results
    # import time убран
    assert "import time" not in results


def test_cooldown_uses_set_nx_ex() -> None:
    """#3b: атомарная установка ключа через SET NX EX."""
    results = _source("app/game_friendly_results.py")
    # Ищем вызов redis.set(..., nx=True, ex=...)
    assert "nx=True" in results
    assert "ex=ACTION_COOLDOWN_SECONDS" in results
    # Проверяем, что _check_cooldown возвращает bool
    assert "def _check_cooldown" in results or "async def _check_cooldown" in results


def test_friendly_fun_action_requires_redis() -> None:
    """#3b: хендлер friendly_fun_action должен принимать redis: Redis."""
    import inspect
    from app.game_friendly_results import friendly_fun_action

    params = inspect.signature(friendly_fun_action).parameters
    assert "redis" in params, f"redis не найден в {list(params)}"
    # Проверяем, что тип — Redis (строкой, потому что аннотация postponed)
    redis_annotation = str(params["redis"].annotation)
    assert "Redis" in redis_annotation, f"Ожидали Redis, получили: {redis_annotation}"