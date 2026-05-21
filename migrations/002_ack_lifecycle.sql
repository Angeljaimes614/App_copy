-- ============================================================
--  002 - ACK lifecycle: estados expandidos + pending_acks queue
-- ============================================================

-- 1) Recrear executions con la state machine ampliada y last_seq
ALTER TABLE executions RENAME TO _executions_v1;

CREATE TABLE executions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id       TEXT NOT NULL REFERENCES signals(signal_id),
  follower_id     INTEGER NOT NULL REFERENCES followers(id),
  state           TEXT NOT NULL CHECK (state IN (
                    'enviada','recibida','validada','encolada','ejecutando',
                    'ejecutada','fallida_broker','fallida_sistema',
                    'timeout','duplicada')),
  last_seq        INTEGER NOT NULL DEFAULT 0,   -- anti out-of-order
  -- timestamps por etapa
  enviada_at      REAL,
  received_at     REAL,
  validated_at    REAL,
  enqueued_at     REAL,
  executing_at    REAL,
  executed_at     REAL,
  finished_at     REAL,
  -- datos de la operacion
  order_id        TEXT,
  amount          REAL,
  error           TEXT,
  error_kind      TEXT CHECK (error_kind IN ('broker','system','unknown')
                              OR error_kind IS NULL),
  retry_count     INTEGER NOT NULL DEFAULT 0,
  latency_ms      INTEGER,
  result          TEXT CHECK (result IN ('win','loss','equal')
                              OR result IS NULL),
  profit          REAL,
  UNIQUE (signal_id, follower_id)
);

-- migrar lo que hubiera (esperado: 0 filas pero por si acaso)
INSERT INTO executions (id, signal_id, follower_id, state, received_at,
                        executing_at, executed_at, finished_at, order_id, amount,
                        error, error_kind, retry_count, latency_ms, result, profit)
SELECT id, signal_id, follower_id,
       CASE state
         WHEN 'fallida' THEN 'fallida_sistema'
         WHEN 'enviada' THEN 'enviada'
         ELSE state
       END,
       received_at, executing_at, executed_at, finished_at, order_id, amount,
       error, error_kind, retry_count, latency_ms, result, profit
FROM _executions_v1;

DROP TABLE _executions_v1;

CREATE INDEX idx_exec_signal    ON executions(signal_id);
CREATE INDEX idx_exec_follower  ON executions(follower_id);
CREATE INDEX idx_exec_state     ON executions(state);
CREATE INDEX idx_exec_finished  ON executions(finished_at);
CREATE INDEX idx_exec_open      ON executions(state)
  WHERE state NOT IN ('ejecutada','fallida_broker','fallida_sistema','timeout','duplicada');

-- 2) Cola persistente de ACKs en el seguidor
--    Si el seguidor reinicia, los ACKs no enviados sobreviven.
CREATE TABLE IF NOT EXISTS pending_acks (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id         TEXT NOT NULL,
  seq               INTEGER NOT NULL,
  state             TEXT NOT NULL,
  payload           TEXT NOT NULL,            -- JSON completo del ACK
  created_at        REAL NOT NULL,
  next_attempt_at   REAL NOT NULL,
  retry_count       INTEGER NOT NULL DEFAULT 0,
  sent_at           REAL,                     -- NULL = pendiente
  UNIQUE (signal_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_pending_acks_next
  ON pending_acks(sent_at, next_attempt_at);

-- 3) Marca schema_meta
UPDATE schema_meta SET value='2' WHERE key='version';
INSERT OR REPLACE INTO schema_meta(key,value)
  VALUES ('mig_002_at', strftime('%s','now'));
