from __future__ import annotations

import aiohttp
import structlog

log = structlog.get_logger(__name__)

ACTION_TO_WAIFU: dict[str, str] = {
    "обнять": "hug",
    "поцеловать": "kiss",
    "засосать": "kiss",
    "сделать комплимент": "smile",
    "погладить": "pat",
    "пощекотать": "smile",
    "пнуть": "slap",
    "пнуть под зад": "kick",
    "дать леща": "slap",
    "дать подзатыльник": "slap",
    "ударить": "slap",
    "уебать": "slap",
    "укусить": "bite",
    "покусать": "bite",
    "арестовать": "bonk",
    "накормить": "feed",
    "покормить": "feed",
    "дать дошик": "feed",
    "превратить в кота": "neko",
    "превратить в жабу": "neko",
    "превратить в дошик": "feed",
    "похвалить": "smile",
    "уважить": "handhold",
    "взорвать": "kill",
    "воскресить": "smile",
    "заморозить": "cry",
    "благословить": "smile",
    "проклясть": "kill",
    "украсть сердце": "blush",
}

FALLBACK_CATEGORY = "smile"
TIMEOUT_SECONDS = 3


async def fetch_waifu_url(action: str) -> str | None:
    category = ACTION_TO_WAIFU.get(action, FALLBACK_CATEGORY)
    url = f"https://api.waifu.pics/sfw/{category}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as resp:
                if resp.status != 200:
                    log.warning("waifu_api_non_200", category=category, status=resp.status)
                    return None
                data = await resp.json()
                gif_url = data.get("url")
                if isinstance(gif_url, str) and gif_url.startswith("http"):
                    return gif_url
                log.warning("waifu_api_bad_payload", category=category, payload=str(data)[:200])
                return None
    except Exception as exc:
        log.warning("waifu_api_failed", category=category, error=str(exc))
        return None
