from types import SimpleNamespace

from app.handlers.features import _is_placeholder_text, rules_input_text


def _command(text: str, quoted: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_to_message=quoted)


def test_inline_rules_text_still_works() -> None:
    assert rules_input_text(_command("изменить правила Без мата и ссылок")) == "Без мата и ссылок"


def test_rules_can_be_taken_from_quoted_message() -> None:
    quoted = SimpleNamespace(text="1. Без мата\n2. Без спама", caption=None)

    assert rules_input_text(_command("изменить правила", quoted)) == "1. Без мата\n2. Без спама"


def test_quoted_caption_is_used_for_media_posts() -> None:
    quoted = SimpleNamespace(text=None, caption="Правила канала")

    assert rules_input_text(_command("изменить правила", quoted)) == "Правила канала"


def test_inline_text_wins_over_quoted_message() -> None:
    quoted = SimpleNamespace(text="старые правила", caption=None)

    assert rules_input_text(_command("изменить правила новые правила", quoted)) == "новые правила"


def test_command_without_any_text_stays_empty() -> None:
    assert rules_input_text(_command("изменить правила")) == ""
    assert (
        rules_input_text(_command("изменить правила", SimpleNamespace(text="   ", caption=None)))
        == ""
    )


def test_help_placeholder_is_not_accepted_as_rules() -> None:
    for value in ("Текст", "текст", "«текст»", "<текст>", "TEXT", "..."):
        assert _is_placeholder_text(value) is True, value
    assert _is_placeholder_text("Без мата и оскорблений") is False
