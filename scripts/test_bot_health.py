#!/usr/bin/env python3
"""Quick bot health check script."""
import asyncio
import os
import sys

try:
    from aiogram import Bot
except ImportError:
    print("❌ aiogram не установлен")
    sys.exit(1)


async def check_bot():
    """Проверить подключение бота к Telegram API."""
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ BOT_TOKEN не установлен")
        return False
    
    try:
        bot = Bot(token)
        me = await bot.get_me()
        print(f"✅ Бот @{me.username} (id={me.id}) подключен")
        
        # Проверить базовые права
        try:
            commands = await bot.get_my_commands()
            print(f"✅ Команды бота настроены: {len(commands)} команды")
        except Exception as e:
            print(f"⚠️  Не удалось получить команды: {e}")
        
        await bot.session.close()
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к Telegram: {e}")
        return False


async def check_redis():
    """Проверить подключение к Redis."""
    try:
        from redis.asyncio import Redis
    except ImportError:
        print("❌ redis не установлен")
        return False
    
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    
    try:
        redis = Redis.from_url(redis_url)
        await redis.ping()
        print(f"✅ Redis подключен: {redis_url}")
        await redis.aclose()
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к Redis: {e}")
        return False


async def check_postgres():
    """Проверить подключение к PostgreSQL."""
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError:
        print("❌ sqlalchemy не установлен")
        return False
    
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL не установлен")
        return False
    
    try:
        engine = create_async_engine(db_url)
        async with engine.connect() as conn:
            await conn.execute("SELECT 1")
        print(f"✅ PostgreSQL подключен")
        await engine.dispose()
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к PostgreSQL: {e}")
        return False


async def main():
    print("=" * 50)
    print("Проверка здоровья Mimoru бота")
    print("=" * 50)
    
    results = []
    results.append(await check_bot())
    results.append(await check_redis())
    results.append(await check_postgres())
    
    print("=" * 50)
    if all(results):
        print("✅ Все сервисы работают!")
    else:
        print("❌ Найдены проблемы")
    print("=" * 50)
    
    return all(results)


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)