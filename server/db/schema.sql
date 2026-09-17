-- Applied automatically by server/db.py (PostgresDb.connect) on every
-- server startup, whenever db.dsn is set in server/config.json — no
-- manual `psql -f schema.sql` step needed. CREATE TABLE IF NOT EXISTS
-- and ADD COLUMN IF NOT EXISTS make re-running this on an already
-- migrated database a safe no-op, so new columns added here reach
-- existing installs automatically too.

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

-- Per-user access grants: which radios/amplifiers a non-admin account may
-- use. Admins bypass these entirely (see db.py user_can_access_*). Keyed
-- by name (not a foreign key to radio_configs/amplifier_configs) so a
-- grant survives deleting and re-adding a radio with the same name, and
-- so it works even against server/config.json-only (non-Postgres-config)
-- radios.
CREATE TABLE IF NOT EXISTS user_radio_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    radio_name TEXT NOT NULL,
    PRIMARY KEY (user_id, radio_name)
);

CREATE TABLE IF NOT EXISTS user_amplifier_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amplifier_name TEXT NOT NULL,
    PRIMARY KEY (user_id, amplifier_name)
);

CREATE TABLE IF NOT EXISTS user_antenna_switch_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    switch_name TEXT NOT NULL,
    PRIMARY KEY (user_id, switch_name)
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
    audio_input_name_contains TEXT,
    audio_input_endpoint_id TEXT,
    audio_output_name_contains TEXT,
    audio_output_endpoint_id TEXT,
    audio_udp_port INTEGER NOT NULL,
    audio_input_gain REAL NOT NULL DEFAULT 1.0,
    audio_output_gain REAL NOT NULL DEFAULT 1.0,
    audio_codec TEXT NOT NULL DEFAULT 'pcm16',
    audio_sample_rate INTEGER NOT NULL DEFAULT 48000,
    audio_ptt_tail_ms INTEGER NOT NULL DEFAULT 0,
    ptt_method TEXT NOT NULL DEFAULT 'civ',
    ptt_civ_address INTEGER,
    ptt_serial_port TEXT,
    cw_udp_port INTEGER NOT NULL,
    cw_method TEXT,       -- null = reuse the ptt_* method (same physical key line)
    cw_civ_address INTEGER,
    cw_serial_port TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 5: ACOM amplifier config + logged telemetry.
CREATE TABLE IF NOT EXISTS amplifier_configs (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL DEFAULT '1200S',
    transport TEXT NOT NULL DEFAULT 'tcp',  -- serial | tcp | http — see server/ebox_transport.py
    host TEXT,
    port INTEGER,
    serial_port TEXT,
    username TEXT,
    password TEXT,
    linked_radio TEXT,  -- null = shared/no CAT mirror or PTT safety lockout
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS amplifier_telemetry (
    id SERIAL PRIMARY KEY,
    amplifier_name TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT,
    output_power_w REAL,
    reflected_power_w REAL,
    swr REAL,
    temp_c REAL,
    fault BOOLEAN
);

-- RSW8A1ER antenna switch config + a log of who switched what port when.
-- Standalone device, not tied to any one radio (an earlier revision
-- linked each switch to exactly one radio for access control + a
-- transmit-safety lockout; dropped per operator feedback — the pairing
-- didn't match real usage) — access is its own per-user ACL table
-- (user_antenna_switch_access above), same shape as amplifiers'.
CREATE TABLE IF NOT EXISTS antenna_switch_configs (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL DEFAULT 'RSW8A1ER',
    serial_port TEXT,
    baud INTEGER NOT NULL DEFAULT 9600,
    -- JSON array of exactly 8 strings, index 0 = port 1's label (e.g.
    -- "20m Dipole") — see common.rsw8a1er_protocol.normalize_port_labels,
    -- always applied before this is written. Plain TEXT, not JSONB: no
    -- other column in this schema uses jsonb, and asyncpg doesn't
    -- auto-decode it without extra codec setup — a flat TEXT column
    -- json.loads()'d in db.py matches every other column's style here.
    port_labels TEXT NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for antenna_switch_configs created before the radio link was
-- dropped and baud became configurable — a no-op on a fresh database
-- (the CREATE TABLE above already has the right shape).
ALTER TABLE antenna_switch_configs ADD COLUMN IF NOT EXISTS baud INTEGER NOT NULL DEFAULT 9600;
ALTER TABLE antenna_switch_configs DROP COLUMN IF EXISTS linked_radio;

CREATE TABLE IF NOT EXISTS antenna_switch_events (
    id SERIAL PRIMARY KEY,
    switch_name TEXT NOT NULL,
    port INTEGER NOT NULL,
    changed_by TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for radio_configs created before audio gain controls existed —
-- a no-op on a fresh database (the CREATE TABLE above already has them).
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_input_gain REAL NOT NULL DEFAULT 1.0;
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_output_gain REAL NOT NULL DEFAULT 1.0;

-- Migration: per-radio audio codec/sample rate settings — a no-op on a
-- fresh database (the CREATE TABLE above already has them).
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_codec TEXT NOT NULL DEFAULT 'pcm16';
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_sample_rate INTEGER NOT NULL DEFAULT 48000;
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_ptt_tail_ms INTEGER NOT NULL DEFAULT 0;

-- Migration: "active" toggle so a radio can be configured but left unused
-- (admin panel checkbox) without deleting it — a no-op on a fresh database.
ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;

-- Migration: split the single audio device into independent input/output
-- devices — mic and speakers are different Windows endpoints even on the
-- same USB codec, so one shared endpoint_id could never open both streams.
-- Guarded on the old columns still existing, so it's a no-op after the
-- first run (they're dropped at the end of this block).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'radio_configs' AND column_name = 'audio_name_contains'
    ) THEN
        ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_input_name_contains TEXT;
        ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_input_endpoint_id TEXT;
        ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_output_name_contains TEXT;
        ALTER TABLE radio_configs ADD COLUMN IF NOT EXISTS audio_output_endpoint_id TEXT;
        UPDATE radio_configs SET
            audio_input_name_contains = audio_name_contains,
            audio_input_endpoint_id = audio_endpoint_id
        WHERE audio_input_name_contains IS NULL;
        ALTER TABLE radio_configs DROP COLUMN audio_name_contains;
        ALTER TABLE radio_configs DROP COLUMN audio_endpoint_id;
    END IF;
END $$;
