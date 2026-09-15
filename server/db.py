"""Session/transmission logging + admin auth + radio config storage.
NullDb is the fallback when db.dsn isn't configured: bridges still run off
server/config.json, but the admin panel's login and config editing need a
real database (there's nowhere else to durably store users/radios)."""

import hashlib
import hmac
import logging
import os

log = logging.getLogger("db")


def hash_password(password: str) -> tuple[str, str]:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return digest, salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return hmac.compare_digest(check, digest)


def _radio_cfg_to_row(cfg: dict) -> dict:
    return {
        "name": cfg["name"],
        "model": cfg.get("model", cfg["name"]),
        "cat_vid": cfg["cat"].get("vid"),
        "cat_pid": cfg["cat"].get("pid"),
        "cat_serial_number": cfg["cat"].get("serial_number"),
        "cat_location": cfg["cat"].get("location"),
        "cat_serial_port": cfg["cat"].get("serial_port"),
        "cat_baud": cfg["cat"]["baud"],
        "cat_tcp_port": cfg["cat"]["tcp_port"],
        "control_port": cfg["control_port"],
        "audio_name_contains": cfg["audio"].get("name_contains"),
        "audio_endpoint_id": cfg["audio"].get("endpoint_id"),
        "audio_udp_port": cfg["audio"]["udp_port"],
        "ptt_method": cfg["ptt"]["method"],
        "ptt_civ_address": cfg["ptt"].get("civ_address"),
        "ptt_serial_port": cfg["ptt"].get("serial_port"),
        "cw_udp_port": cfg["cw_udp_port"],
        "cw_method": (cfg.get("cw") or {}).get("method"),
        "cw_civ_address": (cfg.get("cw") or {}).get("civ_address"),
        "cw_serial_port": (cfg.get("cw") or {}).get("serial_port"),
    }


def _row_to_radio_cfg(row) -> dict:
    return {
        "name": row["name"],
        "model": row["model"],
        "cat": {
            "vid": row["cat_vid"], "pid": row["cat_pid"],
            "serial_number": row["cat_serial_number"], "location": row["cat_location"],
            "serial_port": row["cat_serial_port"], "baud": row["cat_baud"],
            "tcp_host": "0.0.0.0", "tcp_port": row["cat_tcp_port"],
        },
        "control_port": row["control_port"],
        "audio": {
            "name_contains": row["audio_name_contains"], "endpoint_id": row["audio_endpoint_id"],
            "udp_port": row["audio_udp_port"],
        },
        "ptt": {
            "method": row["ptt_method"], "civ_address": row["ptt_civ_address"],
            "serial_port": row["ptt_serial_port"],
        },
        "cw_udp_port": row["cw_udp_port"],
        "cw": (
            {
                "method": row["cw_method"], "civ_address": row["cw_civ_address"],
                "serial_port": row["cw_serial_port"],
            }
            if row["cw_method"] else None
        ),
    }


class NullDb:
    async def start_session(self, username, radio):
        return None

    async def end_session(self, session_id):
        pass

    async def start_transmission(self, session_id):
        return None

    async def end_transmission(self, tx_id):
        pass

    async def authenticate(self, username, password):
        return None

    async def list_radio_configs(self):
        return []

    async def get_radio_config(self, name):
        return None

    async def upsert_radio_config(self, cfg):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази радиа")

    async def delete_radio_config(self, name):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази радиа")

    async def list_amplifier_configs(self):
        return []

    async def upsert_amplifier_config(self, cfg):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази усилватели")

    async def delete_amplifier_config(self, name):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази усилватели")

    async def log_amplifier_telemetry(self, name, telemetry):
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

    async def create_user(self, username, password, is_admin=False):
        digest, salt = hash_password(password)
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO users (username, password_hash, password_salt, is_admin) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (username) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, password_salt = EXCLUDED.password_salt, "
                "is_admin = EXCLUDED.is_admin",
                username, digest, salt, is_admin,
            )

    async def authenticate(self, username, password):
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT password_hash, password_salt, is_admin FROM users WHERE username = $1", username
            )
        if not row or not row["password_hash"]:
            return None
        if not verify_password(password, row["password_hash"], row["password_salt"]):
            return None
        return {"username": username, "is_admin": row["is_admin"]}

    async def list_radio_configs(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM radio_configs ORDER BY name")
        return [_row_to_radio_cfg(r) for r in rows]

    async def get_radio_config(self, name):
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM radio_configs WHERE name = $1", name)
        return _row_to_radio_cfg(row) if row else None

    async def upsert_radio_config(self, cfg: dict):
        r = _radio_cfg_to_row(cfg)
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO radio_configs (
                    name, model, cat_vid, cat_pid, cat_serial_number, cat_location, cat_serial_port,
                    cat_baud, cat_tcp_port, control_port, audio_name_contains, audio_endpoint_id,
                    audio_udp_port, ptt_method, ptt_civ_address, ptt_serial_port,
                    cw_udp_port, cw_method, cw_civ_address, cw_serial_port, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20, now())
                ON CONFLICT (name) DO UPDATE SET
                    model = EXCLUDED.model, cat_vid = EXCLUDED.cat_vid, cat_pid = EXCLUDED.cat_pid,
                    cat_serial_number = EXCLUDED.cat_serial_number, cat_location = EXCLUDED.cat_location,
                    cat_serial_port = EXCLUDED.cat_serial_port, cat_baud = EXCLUDED.cat_baud,
                    cat_tcp_port = EXCLUDED.cat_tcp_port, control_port = EXCLUDED.control_port,
                    audio_name_contains = EXCLUDED.audio_name_contains,
                    audio_endpoint_id = EXCLUDED.audio_endpoint_id,
                    audio_udp_port = EXCLUDED.audio_udp_port, ptt_method = EXCLUDED.ptt_method,
                    ptt_civ_address = EXCLUDED.ptt_civ_address, ptt_serial_port = EXCLUDED.ptt_serial_port,
                    cw_udp_port = EXCLUDED.cw_udp_port, cw_method = EXCLUDED.cw_method,
                    cw_civ_address = EXCLUDED.cw_civ_address, cw_serial_port = EXCLUDED.cw_serial_port,
                    updated_at = now()
                """,
                r["name"], r["model"], r["cat_vid"], r["cat_pid"], r["cat_serial_number"],
                r["cat_location"], r["cat_serial_port"], r["cat_baud"], r["cat_tcp_port"],
                r["control_port"], r["audio_name_contains"], r["audio_endpoint_id"],
                r["audio_udp_port"], r["ptt_method"], r["ptt_civ_address"], r["ptt_serial_port"],
                r["cw_udp_port"], r["cw_method"], r["cw_civ_address"], r["cw_serial_port"],
            )

    async def delete_radio_config(self, name):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM radio_configs WHERE name = $1", name)

    async def list_amplifier_configs(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM amplifier_configs ORDER BY name")
        return [dict(r) for r in rows]

    async def upsert_amplifier_config(self, cfg: dict):
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO amplifier_configs (
                    name, model, transport, host, port, serial_port, username, password, linked_radio, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())
                ON CONFLICT (name) DO UPDATE SET
                    model = EXCLUDED.model, transport = EXCLUDED.transport, host = EXCLUDED.host,
                    port = EXCLUDED.port, serial_port = EXCLUDED.serial_port, username = EXCLUDED.username,
                    password = EXCLUDED.password, linked_radio = EXCLUDED.linked_radio, updated_at = now()
                """,
                cfg["name"], cfg.get("model", "1200S"), cfg["transport"], cfg.get("host"), cfg.get("port"),
                cfg.get("serial_port"), cfg.get("username"), cfg.get("password"), cfg.get("linked_radio"),
            )

    async def delete_amplifier_config(self, name):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM amplifier_configs WHERE name = $1", name)

    async def log_amplifier_telemetry(self, name, telemetry: dict):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO amplifier_telemetry "
                "(amplifier_name, status, output_power_w, reflected_power_w, swr, temp_c, fault) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                name, telemetry.get("status"), telemetry.get("output_power_w"),
                telemetry.get("reflected_power_w"), telemetry.get("swr"), telemetry.get("temp_c"),
                telemetry.get("fault"),
            )


async def build_db(db_cfg):
    dsn = (db_cfg or {}).get("dsn")
    if not dsn:
        log.warning("no db.dsn configured — session/transmission logging and the admin panel are disabled")
        return NullDb()
    db = PostgresDb(dsn)
    await db.connect()
    return db


if __name__ == "__main__":
    h1, s1 = hash_password("hunter2")
    h2, s2 = hash_password("hunter2")
    assert s1 != s2, "salt must be random per call"
    assert verify_password("hunter2", h1, s1)
    assert not verify_password("wrong", h1, s1)
    print("db.py: ok")
