-- 002_call_metrics: derived per-call metrics table.
--
-- Populated by the sync (or by scripts/backfill_metrics.py for existing
-- rows). Intended to be the first stop for most LLM questions about
-- activity, tracker hits, talk time, etc.

CREATE TABLE IF NOT EXISTS call_metrics (
  call_id                text PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
  host_user_id           text,
  host_email             text,
  guest_count            integer,
  external_company       text,
  duration_sec           integer,
  talk_ratio_host        real,
  question_count_host    integer,
  question_count_guest   integer,
  tracker_hits           jsonb,      -- {"Pricing Objection": 3, "Competitor X": 1}
  topic_durations        jsonb,      -- {"Discovery": 420, "Demo": 780}
  had_next_steps         boolean,
  started                timestamptz,
  started_date           date,
  segment_count          integer,
  word_count             integer,
  computed_at            timestamptz NOT NULL DEFAULT now()
);
