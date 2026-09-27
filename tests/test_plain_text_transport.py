from aiogram.methods import SendMessage

from app.main import _plain_method


def test_private_transport_preserves_technical_ids() -> None:
    method = SendMessage(chat_id=42, text="ID: 123456789")

    cleaned = _plain_method(method)

    assert cleaned.text == "ID: 123456789"


def test_group_transport_hides_technical_ids() -> None:
    method = SendMessage(chat_id=-100123, text="ID: 123456789")

    cleaned = _plain_method(method)

    assert cleaned.text == ""
