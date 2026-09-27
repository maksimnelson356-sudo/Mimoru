import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
import app.db.ad_market_models  # noqa: F401
import app.db.moderation_command_models  # noqa: F401
import app.db.moderation_operation_models  # noqa: F401
import app.db.payment_refund_models  # noqa: F401
from app.db import broadcast_models  # noqa: F401
from app.db import deleted_cleanup_retry_models  # noqa: F401
from app.db import fun_models  # noqa: F401
from app.db import game_models  # noqa: F401
from app.db import group_disconnect_models  # noqa: F401
from app.db import invite_operation_models  # noqa: F401
from app.db import models  # noqa: F401
from app.db import permission_transition_models  # noqa: F401
from app.db import rank_models  # noqa: F401
from app.db import rank_provisioning_models  # noqa: F401
from app.db import required_reconcile_models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _ensure_version_column(connection):
    """Widen alembic_version.version_num to VARCHAR(255) if needed.

    Alembic <1.13 creates this column as VARCHAR(32) which is too short for
    some of our revision IDs (e.g. 0025_group_members_deleted_accounts = 35
    chars).  Pre-create or widen the column inside the migration transaction
    so that long revision IDs can be stored.
    """
    if connection.dialect.has_table(connection, "alembic_version"):
        col = connection.exec_driver_sql(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'alembic_version' AND column_name = 'version_num'"
        ).fetchone()
        if col and col[0] < 255:
            connection.exec_driver_sql(
                "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"
            )
    else:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(255) NOT NULL)"
        )


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        _ensure_version_column(connection)
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
