-- ============================================================
--  003 - Tabla de configuracion dinamica
--
-- Pares clave/valor con TTL de cache corto (en el codigo).
-- Permite cambiar comportamiento en CALIENTE sin reiniciar nada:
--   - encender/apagar el bot
--   - cambiar monto
--   - stop win / stop loss (Tier 2)
--   - martingala (Tier 3)
-- ============================================================

CREATE TABLE IF NOT EXISTS settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- Configuracion por seguidor (cuando el master quiera controlar remoto)
CREATE TABLE IF NOT EXISTS follower_settings (
  follower_id INTEGER NOT NULL REFERENCES followers(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  updated_at  REAL NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY (follower_id, key)
);

-- Valores por defecto
INSERT OR IGNORE INTO settings(key,value) VALUES
  ('madre_activa',     'true'),
  ('seguidor_activo',  'true'),
  ('monto_default',    '5'),
  -- Placeholders para Tier 2/3 (vacios = sin limite)
  ('stop_win',         ''),
  ('stop_loss',        ''),
  ('max_trades_dia',   ''),
  ('martingala_activa','false'),
  ('martingala_mult',  '2.2'),
  ('martingala_niveles','2');

UPDATE schema_meta SET value='3' WHERE key='version';
INSERT OR REPLACE INTO schema_meta(key,value)
  VALUES ('mig_003_at', strftime('%s','now'));
