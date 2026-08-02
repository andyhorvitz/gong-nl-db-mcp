-- 001_enrich_calls: typed per-call stats + JSONB mirrors of text-JSON columns.
--
-- Adds columns only. Existing text columns (trackers, topics, speakers,
-- questions, key_points) remain in place for a transition period; a follow-up
-- migration will drop them after parity checks.
--
-- Safe to run inside a single transaction.

ALTER TABLE calls
  ADD COLUMN IF NOT EXISTS talk_ratio_host       real,
  ADD COLUMN IF NOT EXISTS talk_ratio_guest      real,
  ADD COLUMN IF NOT EXISTS longest_monologue_sec integer,
  ADD COLUMN IF NOT EXISTS interactivity         real,
  ADD COLUMN IF NOT EXISTS patience_sec          real,
  ADD COLUMN IF NOT EXISTS question_count        integer,
  ADD COLUMN IF NOT EXISTS sentiment_host        real,
  ADD COLUMN IF NOT EXISTS sentiment_guest       real,
  ADD COLUMN IF NOT EXISTS word_count            integer,
  ADD COLUMN IF NOT EXISTS started_date          date
    GENERATED ALWAYS AS (((started AT TIME ZONE 'UTC')::date)) STORED,
  ADD COLUMN IF NOT EXISTS trackers_jsonb        jsonb,
  ADD COLUMN IF NOT EXISTS topics_jsonb          jsonb,
  ADD COLUMN IF NOT EXISTS speakers_jsonb        jsonb,
  ADD COLUMN IF NOT EXISTS questions_jsonb       jsonb,
  ADD COLUMN IF NOT EXISTS key_points_jsonb      jsonb;

-- Backfill JSONB from existing text payloads.
--
-- NULLIF guards against empty strings. Some transcripts / questions have
-- been stored with literal ``\u0000`` escape sequences from upstream
-- Gong payloads — JSONB cannot represent NUL, so we strip those six-char
-- sequences first. Any other malformed JSON will raise, which is what
-- we want (surface it now rather than silently on sync).
UPDATE calls SET
  trackers_jsonb   = NULLIF(replace(trackers,   '\u0000', ''), '')::jsonb,
  topics_jsonb     = NULLIF(replace(topics,     '\u0000', ''), '')::jsonb,
  speakers_jsonb   = NULLIF(replace(speakers,   '\u0000', ''), '')::jsonb,
  questions_jsonb  = NULLIF(replace(questions,  '\u0000', ''), '')::jsonb,
  key_points_jsonb = NULLIF(replace(key_points, '\u0000', ''), '')::jsonb
WHERE trackers_jsonb   IS NULL
   OR topics_jsonb     IS NULL
   OR speakers_jsonb   IS NULL
   OR questions_jsonb  IS NULL
   OR key_points_jsonb IS NULL;
