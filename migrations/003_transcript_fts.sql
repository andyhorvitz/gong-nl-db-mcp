-- 003_transcript_fts: (rewritten) FTS support via expression indexes.
--
-- The original version of this migration added a STORED GENERATED
-- ``tsv`` column on transcript_segments (2.97 GB) and transcript_chunks
-- (5.38 GB). That forced a full table rewrite, which on the clone's
-- disk ran past 1h45m on segments alone before being cancelled.
--
-- New approach: skip the stored column entirely. The GIN indexes
-- in 004 are built directly on ``to_tsvector('english', coalesce(text,''))``
-- so the planner can use them whenever a query matches that exact
-- expression — which it does for
-- ``WHERE to_tsvector('english', text) @@ websearch_to_tsquery(...)``.
--
-- This migration is intentionally near-empty. A marker row in
-- schema_migrations keeps the numbering honest.

SELECT 1 AS fts_via_expression_index;
