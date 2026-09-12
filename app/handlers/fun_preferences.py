from __future__ import annotations

from aiogram import Router

from app import game_friendly_history, game_friendly_results, group_help_full
from app.games import admin_handlers as game_admin_handlers
from app.games import handlers as game_handlers
from app.games import limit_handlers as game_limit_handlers
from app.games import settings_handlers as game_settings_handlers
from app.games import text_entry as game_text_entry
from app.games.arena import handlers as arena_handlers
from app.games.battleship import handlers as battleship_handlers
from app.games.cards import handlers as cards_handlers
from app.games.crocodile import handlers as crocodile_handlers
from app.games.detective import handlers as detective_handlers
from app.games.mafia import handlers as mafia_handlers
from app.games.quiz import handlers as quiz_handlers
from app.games.roulette import handlers as roulette_handlers
from app.games.spy import handlers as spy_handlers
from app.games.words import handlers as words_handlers

router = Router(name=__name__)
_INCLUDED_CALLBACK_FAMILIES = ("gm", "fsfriendly", "fshfriendly", "fsrel")
router.include_router(mafia_handlers.router)
router.include_router(spy_handlers.router)
router.include_router(quiz_handlers.router)
router.include_router(battleship_handlers.router)
router.include_router(roulette_handlers.router)
router.include_router(crocodile_handlers.router)
router.include_router(cards_handlers.router)
router.include_router(arena_handlers.router)
router.include_router(words_handlers.router)
router.include_router(detective_handlers.router)
router.include_router(game_admin_handlers.router)
router.include_router(game_limit_handlers.router)
router.include_router(game_text_entry.router)
router.include_router(game_settings_handlers.router)
router.include_router(game_handlers.router)
router.include_router(group_help_full.router)
router.include_router(game_friendly_results.router)
router.include_router(game_friendly_history.router)