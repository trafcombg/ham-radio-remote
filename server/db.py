"""Session/transmission logging + admin auth + radio config storage.
NullDb is the fallback when db.dsn isn't configured: bridges still run off
server/config.json, but the admin panel's login and config editing need a
real database (there's nowhere else to durably store users/radios)."""

import hashlib
import hmac
import json
import logging
import os

from common.app_paths import app_dir
from common.rsw8a1er_protocol import normalize_port_labels

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
        "audio_input_name_contains": cfg["audio"].get("input_name_contains"),
        "audio_input_endpoint_id": cfg["audio"].get("input_endpoint_id"),
        "audio_output_name_contains": cfg["audio"].get("output_name_contains"),
        "audio_output_endpoint_id": cfg["audio"].get("output_endpoint_id"),
        "audio_udp_port": cfg["audio"]["udp_port"],
        "audio_input_gain": cfg["audio"].get("input_gain", 1.0),
        "audio_output_gain": cfg["audio"].get("output_gain", 1.0),
        "audio_codec": cfg["audio"].get("codec", "pcm16"),
        "audio_sample_rate": cfg["audio"].get("sample_rate", 48000),
        "audio_ptt_tail_ms": cfg["audio"].get("ptt_tail_ms", 0),
        "ptt_method": cfg["ptt"]["method"],
        "ptt_civ_address": cfg["ptt"].get("civ_address"),
        "ptt_serial_port": cfg["ptt"].get("serial_port"),
        "cw_udp_port": cfg["cw_udp_port"],
        "cw_method": (cfg.get("cw") or {}).get("method"),
        "cw_civ_address": (cfg.get("cw") or {}).get("civ_address"),
        "cw_serial_port": (cfg.get("cw") or {}).get("serial_port"),
        "active": cfg.get("active", True),
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
            "input_name_contains": row["audio_input_name_contains"],
            "input_endpoint_id": row["audio_input_endpoint_id"],
            "output_name_contains": row["audio_output_name_contains"],
            "output_endpoint_id": row["audio_output_endpoint_id"],
            "udp_port": row["audio_udp_port"],
            "input_gain": row["audio_input_gain"], "output_gain": row["audio_output_gain"],
            "codec": row["audio_codec"], "sample_rate": row["audio_sample_rate"],
            "ptt_tail_ms": row["audio_ptt_tail_ms"],
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
        "active": row["active"],
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

    async def list_users(self):
        return []

    async def upsert_user(self, username, password, is_admin, radio_names, amplifier_names, antenna_switch_names=()):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази потребители")

    async def delete_user(self, username):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази потребители")

    async def user_can_access_radio(self, username, radio_name) -> bool:
        return True  # no accounts to check without a db — same degraded-open behavior as require_admin's 503

    async def user_can_access_amplifier(self, username, amplifier_name) -> bool:
        return True

    async def user_can_access_antenna_switch(self, username, switch_name) -> bool:
        return True

    async def list_radio_configs(self):
        return []

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

    async def list_antenna_switch_configs(self):
        return []

    async def upsert_antenna_switch_config(self, cfg):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази антенни суичове")

    async def delete_antenna_switch_config(self, name):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази антенни суичове")

    async def log_antenna_switch_event(self, name, port, username):
        pass

    async def list_tapo_configs(self):
        return []

    async def upsert_tapo_config(self, cfg):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази Tapo контакти")

    async def delete_tapo_config(self, name):
        raise RuntimeError("PostgreSQL не е конфигуриран (db.dsn) — админ панелът не може да пази Tapo контакти")


class PostgresDb:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        import asyncpg

        self.pool = await asyncpg.create_pool(self.dsn)
        log.info("connected to PostgreSQL")
        await self._apply_schema()

    async def _apply_schema(self):
        """Creates whatever tables are missing — CREATE TABLE IF NOT
        EXISTS throughout schema.sql makes this safe to run on every
        startup, so there's no separate manual "set up the database"
        step: point db.dsn at any empty (or already-migrated) Postgres
        database and this does the rest."""
        schema_path = app_dir(__file__) / "db" / "schema.sql"
        sql = schema_path.read_text(encoding="utf-8")
        async with self.pool.acquire() as c:
            await c.execute(sql)
        log.info("database schema up to date (%s)", schema_path)

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

    async def has_any_admin(self) -> bool:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT 1 FROM users WHERE is_admin = true LIMIT 1")
        return row is not None

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

    async def list_users(self):
        """Only accounts with a password (real, admin-created logins) —
        excludes the bare username rows _user_id() auto-creates for
        session/transmission logging, which aren't "users" to manage."""
        async with self.pool.acquire() as c:
            users = await c.fetch(
                "SELECT id, username, is_admin FROM users WHERE password_hash IS NOT NULL ORDER BY username"
            )
            radio_rows = await c.fetch("SELECT user_id, radio_name FROM user_radio_access")
            amp_rows = await c.fetch("SELECT user_id, amplifier_name FROM user_amplifier_access")
            switch_rows = await c.fetch("SELECT user_id, switch_name FROM user_antenna_switch_access")
        radios_by_user: dict = {}
        for r in radio_rows:
            radios_by_user.setdefault(r["user_id"], []).append(r["radio_name"])
        amps_by_user: dict = {}
        for r in amp_rows:
            amps_by_user.setdefault(r["user_id"], []).append(r["amplifier_name"])
        switches_by_user: dict = {}
        for r in switch_rows:
            switches_by_user.setdefault(r["user_id"], []).append(r["switch_name"])
        return [
            {
                "username": u["username"],
                "is_admin": u["is_admin"],
                "radios": sorted(radios_by_user.get(u["id"], [])),
                "amplifiers": sorted(amps_by_user.get(u["id"], [])),
                "antenna_switches": sorted(switches_by_user.get(u["id"], [])),
            }
            for u in users
        ]

    async def upsert_user(self, username, password, is_admin, radio_names, amplifier_names, antenna_switch_names=()):
        """password=None keeps the existing hash (editing a user without
        changing their password); a brand-new user needs a password —
        admin_api.py enforces that before calling this."""
        async with self.pool.acquire() as c:
            async with c.transaction():
                if password:
                    digest, salt = hash_password(password)
                    row = await c.fetchrow(
                        "INSERT INTO users (username, password_hash, password_salt, is_admin) "
                        "VALUES ($1,$2,$3,$4) "
                        "ON CONFLICT (username) DO UPDATE SET "
                        "password_hash = EXCLUDED.password_hash, password_salt = EXCLUDED.password_salt, "
                        "is_admin = EXCLUDED.is_admin "
                        "RETURNING id",
                        username, digest, salt, is_admin,
                    )
                else:
                    row = await c.fetchrow(
                        "INSERT INTO users (username, is_admin) VALUES ($1,$2) "
                        "ON CONFLICT (username) DO UPDATE SET is_admin = EXCLUDED.is_admin "
                        "RETURNING id",
                        username, is_admin,
                    )
                user_id = row["id"]
                await c.execute("DELETE FROM user_radio_access WHERE user_id = $1", user_id)
                await c.execute("DELETE FROM user_amplifier_access WHERE user_id = $1", user_id)
                await c.execute("DELETE FROM user_antenna_switch_access WHERE user_id = $1", user_id)
                for name in radio_names:
                    await c.execute(
                        "INSERT INTO user_radio_access (user_id, radio_name) VALUES ($1,$2)", user_id, name,
                    )
                for name in amplifier_names:
                    await c.execute(
                        "INSERT INTO user_amplifier_access (user_id, amplifier_name) VALUES ($1,$2)", user_id, name,
                    )
                for name in antenna_switch_names:
                    await c.execute(
                        "INSERT INTO user_antenna_switch_access (user_id, switch_name) VALUES ($1,$2)", user_id, name,
                    )

    async def delete_user(self, username):
        """Soft-delete: clears the login (password + is_admin + access
        grants) instead of DELETEing the row, since sessions.user_id
        references it without ON DELETE CASCADE — removing the row would
        either violate that FK or silently erase session history."""
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not row:
                return
            user_id = row["id"]
            await c.execute(
                "UPDATE users SET password_hash = NULL, password_salt = NULL, is_admin = false WHERE id = $1",
                user_id,
            )
            await c.execute("DELETE FROM user_radio_access WHERE user_id = $1", user_id)
            await c.execute("DELETE FROM user_amplifier_access WHERE user_id = $1", user_id)
            await c.execute("DELETE FROM user_antenna_switch_access WHERE user_id = $1", user_id)

    async def user_can_access_radio(self, username, radio_name) -> bool:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT u.is_admin, EXISTS("
                "  SELECT 1 FROM user_radio_access a WHERE a.user_id = u.id AND a.radio_name = $2"
                ") AS has_access "
                "FROM users u WHERE u.username = $1 AND u.password_hash IS NOT NULL",
                username, radio_name,
            )
        return bool(row and (row["is_admin"] or row["has_access"]))

    async def user_can_access_amplifier(self, username, amplifier_name) -> bool:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT u.is_admin, EXISTS("
                "  SELECT 1 FROM user_amplifier_access a WHERE a.user_id = u.id AND a.amplifier_name = $2"
                ") AS has_access "
                "FROM users u WHERE u.username = $1 AND u.password_hash IS NOT NULL",
                username, amplifier_name,
            )
        return bool(row and (row["is_admin"] or row["has_access"]))

    async def user_can_access_antenna_switch(self, username, switch_name) -> bool:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT u.is_admin, EXISTS("
                "  SELECT 1 FROM user_antenna_switch_access a WHERE a.user_id = u.id AND a.switch_name = $2"
                ") AS has_access "
                "FROM users u WHERE u.username = $1 AND u.password_hash IS NOT NULL",
                username, switch_name,
            )
        return bool(row and (row["is_admin"] or row["has_access"]))

    async def list_radio_configs(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM radio_configs ORDER BY name")
        return [_row_to_radio_cfg(r) for r in rows]

    async def upsert_radio_config(self, cfg: dict):
        r = _radio_cfg_to_row(cfg)
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO radio_configs (
                    name, model, cat_vid, cat_pid, cat_serial_number, cat_location, cat_serial_port,
                    cat_baud, cat_tcp_port, control_port,
                    audio_input_name_contains, audio_input_endpoint_id,
                    audio_output_name_contains, audio_output_endpoint_id,
                    audio_udp_port, audio_input_gain, audio_output_gain, audio_codec, audio_sample_rate, audio_ptt_tail_ms,
                    ptt_method, ptt_civ_address, ptt_serial_port,
                    cw_udp_port, cw_method, cw_civ_address, cw_serial_port, active, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28, now())
                ON CONFLICT (name) DO UPDATE SET
                    model = EXCLUDED.model, cat_vid = EXCLUDED.cat_vid, cat_pid = EXCLUDED.cat_pid,
                    cat_serial_number = EXCLUDED.cat_serial_number, cat_location = EXCLUDED.cat_location,
                    cat_serial_port = EXCLUDED.cat_serial_port, cat_baud = EXCLUDED.cat_baud,
                    cat_tcp_port = EXCLUDED.cat_tcp_port, control_port = EXCLUDED.control_port,
                    audio_input_name_contains = EXCLUDED.audio_input_name_contains,
                    audio_input_endpoint_id = EXCLUDED.audio_input_endpoint_id,
                    audio_output_name_contains = EXCLUDED.audio_output_name_contains,
                    audio_output_endpoint_id = EXCLUDED.audio_output_endpoint_id,
                    audio_udp_port = EXCLUDED.audio_udp_port,
                    audio_input_gain = EXCLUDED.audio_input_gain, audio_output_gain = EXCLUDED.audio_output_gain,
                    audio_codec = EXCLUDED.audio_codec, audio_sample_rate = EXCLUDED.audio_sample_rate,
                    audio_ptt_tail_ms = EXCLUDED.audio_ptt_tail_ms,
                    ptt_method = EXCLUDED.ptt_method,
                    ptt_civ_address = EXCLUDED.ptt_civ_address, ptt_serial_port = EXCLUDED.ptt_serial_port,
                    cw_udp_port = EXCLUDED.cw_udp_port, cw_method = EXCLUDED.cw_method,
                    cw_civ_address = EXCLUDED.cw_civ_address, cw_serial_port = EXCLUDED.cw_serial_port,
                    active = EXCLUDED.active,
                    updated_at = now()
                """,
                r["name"], r["model"], r["cat_vid"], r["cat_pid"], r["cat_serial_number"],
                r["cat_location"], r["cat_serial_port"], r["cat_baud"], r["cat_tcp_port"],
                r["control_port"],
                r["audio_input_name_contains"], r["audio_input_endpoint_id"],
                r["audio_output_name_contains"], r["audio_output_endpoint_id"],
                r["audio_udp_port"], r["audio_input_gain"], r["audio_output_gain"],
                r["audio_codec"], r["audio_sample_rate"], r["audio_ptt_tail_ms"],
                r["ptt_method"], r["ptt_civ_address"], r["ptt_serial_port"],
                r["cw_udp_port"], r["cw_method"], r["cw_civ_address"], r["cw_serial_port"],
                r["active"],
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

    async def list_antenna_switch_configs(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM antenna_switch_configs ORDER BY name")
        result = []
        for r in rows:
            cfg = dict(r)
            cfg["port_labels"] = json.loads(cfg["port_labels"])
            result.append(cfg)
        return result

    async def upsert_antenna_switch_config(self, cfg: dict):
        port_labels = json.dumps(normalize_port_labels(cfg.get("port_labels")))
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO antenna_switch_configs (name, model, serial_port, baud, port_labels, updated_at)
                VALUES ($1,$2,$3,$4,$5, now())
                ON CONFLICT (name) DO UPDATE SET
                    model = EXCLUDED.model, serial_port = EXCLUDED.serial_port,
                    baud = EXCLUDED.baud, port_labels = EXCLUDED.port_labels, updated_at = now()
                """,
                cfg["name"], cfg.get("model", "RSW8A1ER"), cfg.get("serial_port"), cfg.get("baud", 9600), port_labels,
            )

    async def delete_antenna_switch_config(self, name):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM antenna_switch_configs WHERE name = $1", name)

    async def log_antenna_switch_event(self, name, port, username):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO antenna_switch_events (switch_name, port, changed_by) VALUES ($1,$2,$3)",
                name, port, username,
            )

    async def list_tapo_configs(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM tapo_configs ORDER BY name")
        return [dict(r) for r in rows]

    async def upsert_tapo_config(self, cfg: dict):
        # password: COALESCE keeps the existing one when the admin edits a
        # device without retyping it — a blank field must not wipe a
        # working credential (see the admin panel's Tapo edit form).
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO tapo_configs (name, host, username, password, linked_radio, linked_amplifier, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6, now())
                ON CONFLICT (name) DO UPDATE SET
                    host = EXCLUDED.host, username = EXCLUDED.username,
                    password = COALESCE(EXCLUDED.password, tapo_configs.password),
                    linked_radio = EXCLUDED.linked_radio, linked_amplifier = EXCLUDED.linked_amplifier,
                    updated_at = now()
                """,
                cfg["name"], cfg["host"], cfg.get("username"), cfg.get("password"),
                cfg.get("linked_radio"), cfg.get("linked_amplifier"),
            )

    async def delete_tapo_config(self, name):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM tapo_configs WHERE name = $1", name)


async def build_db(db_cfg):
    dsn = (db_cfg or {}).get("dsn")
    if not dsn:
        log.warning(
            "no db.dsn configured — session/transmission logging and the admin panel are disabled. "
            "To enable them: install PostgreSQL, create a database (e.g. `createdb hamradio`), "
            "then set db.dsn in config.json (next to this .exe) to "
            "postgresql://<user>:<password>@<host>:<port>/<database>, "
            "e.g. postgresql://postgres:mypassword@localhost:5432/hamradio — and restart the server. "
            "See README.md / packaging/README.md for the full walkthrough."
        )
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

    cfg = {
        "name": "TEST", "model": "TEST",
        "cat": {"baud": 19200, "tcp_port": 4532},
        "control_port": 4632,
        "audio": {"udp_port": 5004},
        "ptt": {"method": "civ"},
        "cw_udp_port": 5104,
        "active": False,
    }
    row = _radio_cfg_to_row(cfg)
    assert row["active"] is False
    back = _row_to_radio_cfg(row)
    assert back["active"] is False
    row2 = _radio_cfg_to_row({**cfg, "active": True})
    assert _row_to_radio_cfg(row2)["active"] is True
    assert _radio_cfg_to_row({k: v for k, v in cfg.items() if k != "active"})["active"] is True, "active defaults to True"

    print("db.py: ok")
