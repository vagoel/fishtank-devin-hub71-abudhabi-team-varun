import logging
from pathlib import Path

import asyncpg

from .config import settings

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        statement_cache_size=settings.db_statement_cache_size,
        command_timeout=60,
    )
    if settings.db_auto_migrate:
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
        log.info("schema ensured")
    return pool
