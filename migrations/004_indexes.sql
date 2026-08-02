-- migrate: no-transaction
-- 004_indexes: adds the indexes that back the new query paths.
--
-- All CREATE INDEX statements use CONCURRENTLY so the large tables
-- (transcript_segments at ~7.6M rows, transcript_chunks at ~774K) stay
-- writable during the build. CONCURRENTLY cannot run inside a
-- transaction, so this file is marked no-transaction above and each
-- statement runs on its own.
--
-- NOTE: no indexes are dropped in this migration (per product decision
-- to keep the currently-unused trigram/etc. indexes in place for now).

-- call_metrics indexes (table was created in 002 inside a txn, so building
-- these here keeps all index builds consistent).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cm_host_date
  ON call_metrics (host_user_id, started_date DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cm_company
  ON call_metrics (external_company);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cm_tracker_gin
  ON call_metrics USING gin (tracker_hits);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cm_topic_gin
  ON call_metrics USING gin (topic_durations);

-- calls: composite + generated-column + JSONB GIN
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_calls_user_started
  ON calls (primary_user_id, started DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_calls_started_date
  ON calls (started_date);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_calls_trackers_gin
  ON calls USING gin (trackers_jsonb jsonb_path_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_calls_topics_gin
  ON calls USING gin (topics_jsonb jsonb_path_ops);

-- transcript full-text (expression indexes — no table rewrite).
-- Queries must match the expression exactly to use the index, e.g.
--   WHERE to_tsvector('english', text) @@ websearch_to_tsquery('pricing objection')
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ts_tsv_expr
  ON transcript_segments
  USING gin (to_tsvector('english', coalesce(text, '')));

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tc_tsv_expr
  ON transcript_chunks
  USING gin (to_tsvector('english', coalesce(text, '')));

-- BRIN for locality-aware range scans on the biggest table
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ts_call_start_brin
  ON transcript_segments USING brin (call_id, start_time);
