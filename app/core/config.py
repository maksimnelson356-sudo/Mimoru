from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


_PLACEHOLDER_TOKENS = {
    "123456:replace_me",
    "replace_me",
    "changeme",
    "change_me",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    database_url: str = "postgresql+asyncpg://moderator:moderator@localhost:5432/moderator"
    redis_url: str = "redis://localhost:6379/0"
    service_owner_ids: Annotated[tuple[int, ...], NoDecode] = ()
    support_chat_id: int | None = None
    default_mute_seconds: int = 3600
    antiflood_limit: int = 6
    antiflood_window_seconds: int = 10
    antiflood_mute_seconds: int = 1800
    captcha_timeout_seconds: int = 120
    service_name: str = "Mimoru"
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    global_post_price_stars: int = 100
    devtools_enabled: bool = False

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        token = value.strip()
        if not token or token.lower() in _PLACEHOLDER_TOKENS:
            raise ValueError("BOT_TOKEN is missing or still contains a placeholder")
        prefix, separator, secret = token.partition(":")
        if not separator or not prefix.isdigit() or len(secret) < 20:
            raise ValueError("BOT_TOKEN must look like '<numeric_bot_id>:<secret>'")
        return token

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(value.replace("postgresql+asyncpg://", "postgresql://", 1))
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL with host and database name")
        return url

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must use redis:// or rediss:// and contain a host")
        return url

    @field_validator("service_owner_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value: object) -> tuple[int, ...]:
        if value in (None, ""):
            return ()
        if isinstance(value, int):
            raw = (value,)
        elif isinstance(value, str):
            raw = tuple(int(item.strip()) for item in value.split(",") if item.strip())
        elif isinstance(value, (list, tuple, set)):
            raw = tuple(int(item) for item in value)
        else:
            raise ValueError("SERVICE_OWNER_IDS must contain Telegram IDs separated by commas")
        if any(item <= 0 for item in raw):
            raise ValueError("SERVICE_OWNER_IDS must contain positive Telegram IDs")
        return tuple(dict.fromkeys(raw))

    @field_validator("support_chat_id", mode="before")
    @classmethod
    def parse_optional_int(cls, value: object) -> int | None:
        if value in (None, ""):
            return None
        result = int(value)
        if result == 0:
            raise ValueError("SUPPORT_CHAT_ID cannot be zero")
        return result

    @field_validator(
        "default_mute_seconds",
        "antiflood_limit",
        "antiflood_window_seconds",
        "antiflood_mute_seconds",
        "captcha_timeout_seconds",
        "global_post_price_stars",
    )
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("numeric moderation/marketplace settings must be greater than zero")
        return value

    @field_validator("health_port")
    @classmethod
    def validate_health_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("HEALTH_PORT must be between 1 and 65535")
        return value

    @field_validator("service_name", "health_host")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("value must not be empty")
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
