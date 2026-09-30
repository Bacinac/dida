"""Apply db/migrations to the database the POSTGRES_* environment names."""

import asyncio

from dida_core import apply_migrations, pg_pool


async def main() -> None:
    pool = await pg_pool(min_size=1, max_size=1)
    try:
        await apply_migrations(pool, "db/migrations")
    finally:
        await pool.close()


asyncio.run(main())
