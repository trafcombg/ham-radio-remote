-- Applied by server/db.py (PostgresDb) starting Phase 2, when db.dsn is set
-- in server/config.json. Run this once against that database before
-- starting the server: psql <dsn> -f server/db/schema.sql

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    password_salt TEXT,
    is_admin BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS radios (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    radio_id INTEGER NOT NULL REFERENCES radios(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS transmissions (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

-- Phase 3: radio configuration itself lives here, editable from the admin
-- panel and hot-reloadable per radio without restarting the server.
CREATE TABLE IF NOT EXISTS radio_configs (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    cat_vid INTEGER,
    cat_pid INTEGER,
    cat_serial_number TEXT,
    cat_location TEXT,
    cat_serial_port TEXT,
    cat_baud INTEGER NOT NULL DEFAULT 19200,
    cat_tcp_port INTEGER NOT NULL,
    control_port INTEGER NOT NULL,
    audio_name_contains TEXT,
    audio_endpoint_id TEXT,
    audio_udp_port INTEGER NOT NULL,
    ptt_method TEXT NOT NULL DEFAULT 'civ',
    ptt_civ_address INTEGER,
    ptt_serial_port TEXT,
    cw_udp_port INTEGER NOT NULL,
    cw_method TEXT,       -- null = reuse the ptt_* method (same physical key line)
    cw_civ_address INTEGER,
    cw_serial_port TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
