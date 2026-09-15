"""Session/transmission logging. NullDb is the fallback when db.dsn isn't
configured, so the rest of the system runs without standing up Postgres."""

import logging

log = logging.getLogger("db")


class NullDb:
    async def start_session(self, username, radio):
        return None

    async def end_session(self, session_id):
        pass

    async def start_transmission(self, session_id):
        return None

    async def end_transmission(self, tx_id):
        pass


class PostgresDb:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn)
        log.info("connected to PostgreSQL")

    async def _user_id(self, username):
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO users (username) VALUES ($1) "
                "ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username "
                "RETURNING id",
                username,
            )
            return row["id"]

    async def _radio_id(self, name, model):
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM radios WHERE name = $1", name)
            if row:
                return row["id"]
            row = await c.fetchrow(
                "INSERT INTO radios (name, model) VALUES ($1, $2) RETURNING id", name, model
            )
            return row["id"]

    async def start_session(self, username, radio):
        user_id = await self._user_id(username)
        radio_id = await self._radio_id(radio, radio)
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO sessions (user_id, radio_id) VALUES ($1, $2) RETURNING id",
                user_id, radio_id,
            )
            return row["id"]

    async def end_session(self, session_id):
        async with self.pool.acquire() as c:
            await c.execute("UPDATE sessions SET ended_at = now() WHERE id = $1", session_id)

    async def start_transmission(self, session_id):
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO transmissions (session_id) VALUES ($1) RETURNING id", session_id
            )
            return row["id"]

    async def end_transmission(self, tx_id):
        async with self.pool.acquire() as c:
            await c.execute("UPDATE transmissions SET ended_at = now() WHERE id = $1", tx_id)


async def build_db(db_cfg):
    dsn = (db_cfg or {}).get("dsn")
    if not dsn:
        log.warning("no db.dsn configured — session/transmission logging disabled")
        return NullDb()
    db = PostgresDb(dsn)
    await db.connect()
    return db
