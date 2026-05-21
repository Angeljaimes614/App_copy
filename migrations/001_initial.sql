-- ============================================================
--  Schema inicial Copy Trading IQ Option
--  Version: 1
--  WAL + foreign keys + busy_timeout: configurados en core/db.py
-- ============================================================

-- ------------------- SEGUIDORES -----------------------------
CREATE TABLE IF NOT EXISTS followers (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id       INTEGER UNIQUE NOT NULL,
  telegram_username TEXT,
  telegram_name     TEXT,
  iq_email          TEXT,
  iq_account_type   TEXT
        CHECK (iq_account_type IN ('PRACTICE','REAL') OR iq_account_type IS NULL),
  app_version       TEXT,
  state             TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (state IN ('pendiente','activo','bloqueado','vencido')),
  created_at        REAL NOT NULL,
  expires_at        TEXT,            -- ISO date
  last_seen         REAL,
  last_balance      REAL,
  notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_followers_state    ON followers(state);
CREATE INDEX IF NOT EXISTS idx_followers_expires  ON followers(expires_at);

-- ------------------- SENALES (lo que la madre emite) --------
CREATE TABLE IF NOT EXISTS signals (
  signal_id         TEXT PRIMARY KEY,                  -- ULID
  v                 INTEGER NOT NULL DEFAULT 1,        -- version del protocolo
  emitted_at        REAL NOT NULL,
  par               TEXT NOT NULL,
  dir               TEXT NOT NULL CHECK (dir IN ('call','put')),
  tf                INTEGER NOT NULL,
  created           INTEGER NOT NULL,                  -- timestamp IQ
  exp               INTEGER NOT NULL,                  -- timestamp expiracion
  tipo              TEXT NOT NULL,                     -- turbo / binary / digital
  master_order_id   TEXT,
  payload           TEXT                               -- JSON crudo
);
CREATE INDEX IF NOT EXISTS idx_signals_emitted ON signals(emitted_at);

-- ------------------- EJECUCIONES ----------------------------
-- Una fila por (signal_id, follower_id). Estado avanza:
--   enviada -> recibida -> ejecutando -> ejecutada
--                                     \-> fallida (broker)
--                                     \-> timeout (sin ACK)
CREATE TABLE IF NOT EXISTS executions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id         TEXT NOT NULL REFERENCES signals(signal_id),
  follower_id       INTEGER NOT NULL REFERENCES followers(id),
  state             TEXT NOT NULL
        CHECK (state IN ('enviada','recibida','ejecutando',
                         'ejecutada','fallida','timeout')),
  received_at       REAL,
  executing_at      REAL,
  executed_at       REAL,
  finished_at       REAL,
  order_id          TEXT,
  amount            REAL,
  error             TEXT,
  error_kind        TEXT
        CHECK (error_kind IN ('broker','system','unknown') OR error_kind IS NULL),
  retry_count       INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER,
  result            TEXT
        CHECK (result IN ('win','loss','equal') OR result IS NULL),
  profit            REAL,
  UNIQUE (signal_id, follower_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_signal    ON executions(signal_id);
CREATE INDEX IF NOT EXISTS idx_exec_follower  ON executions(follower_id);
CREATE INDEX IF NOT EXISTS idx_exec_state     ON executions(state);
CREATE INDEX IF NOT EXISTS idx_exec_finished  ON executions(finished_at);

-- ------------------- HEARTBEATS -----------------------------
CREATE TABLE IF NOT EXISTS heartbeats (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  follower_id       INTEGER NOT NULL REFERENCES followers(id),
  at                REAL NOT NULL,
  v                 INTEGER NOT NULL DEFAULT 1,
  app_version       TEXT,
  balance           REAL,
  health_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_hb_follower_at ON heartbeats(follower_id, at);

-- ------------------- LICENCIAS (para cobro) -----------------
CREATE TABLE IF NOT EXISTS licenses (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  follower_id       INTEGER NOT NULL REFERENCES followers(id),
  started_at        REAL NOT NULL,
  expires_at        TEXT NOT NULL,
  plan              TEXT,
  payment_ref       TEXT,
  amount_paid       REAL,
  notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_licenses_follower ON licenses(follower_id);

-- ------------------- LOGS estructurados ---------------------
-- Opcional: tambien existe el log en archivo JSON. Esta tabla
-- es util para queries (ej. "todos los errores de Fulano hoy").
CREATE TABLE IF NOT EXISTS logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  at                REAL NOT NULL,
  level             TEXT NOT NULL,
  component         TEXT,
  correlation_id    TEXT,
  follower_id       INTEGER,
  signal_id         TEXT,
  message           TEXT,
  meta              TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_at           ON logs(at);
CREATE INDEX IF NOT EXISTS idx_logs_correlation  ON logs(correlation_id);

-- ------------------- DEDUPE (en seguidor) -------------------
-- Se persiste para sobrevivir reinicios del bot del seguidor.
CREATE TABLE IF NOT EXISTS processed_signals (
  signal_id         TEXT PRIMARY KEY,
  seen_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_signals(seen_at);

-- ------------------- META -----------------------------------
-- Para saber en que version de schema esta cada DB.
CREATE TABLE IF NOT EXISTS schema_meta (
  key               TEXT PRIMARY KEY,
  value             TEXT NOT NULL
);
INSERT OR REPLACE INTO schema_meta(key, value)
  VALUES ('version', '1');
INSERT OR REPLACE INTO schema_meta(key, value)
  VALUES ('applied_at', strftime('%s','now'));
