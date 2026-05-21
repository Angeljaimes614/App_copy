-- ============================================================
--  004 - Martingala: tracking de resultados y cadenas
-- ============================================================

-- Resultado del trade y nivel de martingala
ALTER TABLE executions ADD COLUMN result_at REAL;
ALTER TABLE executions ADD COLUMN martingala_level INTEGER NOT NULL DEFAULT 0;
ALTER TABLE executions ADD COLUMN martingala_parent_id INTEGER REFERENCES executions(id);

-- Indice para encontrar rapido las ejecutadas sin resultado
CREATE INDEX IF NOT EXISTS idx_exec_sin_result
  ON executions(state, result, result_at)
  WHERE state='ejecutada' AND result IS NULL;

-- schema meta
UPDATE schema_meta SET value='4' WHERE key='version';
INSERT OR REPLACE INTO schema_meta(key,value)
  VALUES ('mig_004_at', strftime('%s','now'));
