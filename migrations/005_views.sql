-- 005_views: wide call-overview view + materialized daily-per-user rollup.

CREATE OR REPLACE VIEW v_call_overview AS
SELECT
  c.id,
  c.title,
  c.started,
  c.started_date,
  c.duration_sec,
  c.direction,
  c.language,
  c.company_name,
  c.primary_user_id,
  COALESCE(u.first_name || ' ' || u.last_name, NULL) AS host_name,
  u.email        AS host_email,
  m.talk_ratio_host,
  m.question_count_host,
  m.tracker_hits,
  m.topic_durations,
  m.had_next_steps,
  c.brief,
  c.call_outcome
FROM calls c
LEFT JOIN users u        ON u.id = c.primary_user_id
LEFT JOIN call_metrics m ON m.call_id = c.id;

DROP MATERIALIZED VIEW IF EXISTS mv_user_daily;
CREATE MATERIALIZED VIEW mv_user_daily AS
SELECT
  host_user_id,
  host_email,
  started_date,
  COUNT(*)                  AS calls,
  SUM(duration_sec)         AS total_sec,
  AVG(talk_ratio_host)      AS avg_talk_ratio,
  SUM(question_count_host)  AS questions_asked
FROM call_metrics
WHERE started_date IS NOT NULL
GROUP BY host_user_id, host_email, started_date;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_user_daily_pk
  ON mv_user_daily (host_user_id, started_date);
