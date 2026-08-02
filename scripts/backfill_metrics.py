"""Backfill call_metrics from data already stored in ``calls``.

Uses ONLY the JSONB columns populated by migration 001 plus the raw
``duration_sec`` / ``primary_user_id`` / ``company_name`` fields — does
NOT re-hit the Gong API. Run this after migrations 001/002 apply and
before the sync starts dual-writing live metrics.

Fields we can derive today (from existing payloads):

* ``talk_ratio_host``    — sum(speakers[].talkTime where userId == primary_user_id) / duration_sec
* ``question_count``     — length of questions array (split into host/guest by speakerId when possible)
* ``tracker_hits``       — {name: sum(count)} over trackers where count > 0
* ``topic_durations``    — {name: duration} from topics array
* ``guest_count``        — external parties count from call_parties
* ``external_company``   — most common external-party company for this call
* ``word_count``         — approx: sum over transcript_segments words (lazy: count spaces + 1)
* ``segment_count``      — count(*) from transcript_segments
* ``had_next_steps``     — EXISTS topic named 'Next Steps' / 'Wrap-Up' with duration > 0,
                           OR calls.call_outcome mentions "next step"

Fields left NULL until the sync changes land:
* ``longest_monologue_sec``, ``interactivity``, ``patience_sec``,
  ``sentiment_host``, ``sentiment_guest``  (need personInteractionStats)

Usage:
    PGPASSWORD_FILE=/tmp/.gongclone_pg_pw PYTHONPATH=src \\
      .venv/bin/python scripts/backfill_metrics.py --batch 500
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from google.cloud.sql.connector import Connector, IPTypes

log = logging.getLogger("backfill")


def connect():
    icn = os.environ.get(
        "INSTANCE_CONNECTION_NAME",
        "planar-ray-494004-b8:us-central1:gong-nl-db-clone",
    )
    db = os.environ.get("DB_NAME", "gong")
    ip_type = IPTypes.PRIVATE if os.environ.get("IP_TYPE", "").upper() == "PRIVATE" else IPTypes.PUBLIC
    connector = Connector(refresh_strategy="lazy")
    pw_file = os.environ.get("PGPASSWORD_FILE")
    if not pw_file:
        raise RuntimeError("PGPASSWORD_FILE required for backfill (needs INSERT privileges)")
    pw = Path(pw_file).read_text().strip()
    user = os.environ.get("PGUSER", "postgres")
    conn = connector.connect(
        icn, "pg8000", user=user, password=pw, db=db, ip_type=ip_type
    )
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
    cur.execute("SET default_transaction_read_only = off")
    conn.commit()
    cur.close()
    return connector, conn


BACKFILL_SQL = """
WITH src AS (
  SELECT
    c.id AS call_id,
    c.primary_user_id,
    u.email AS host_email,
    c.duration_sec,
    c.started,
    c.started_date,
    c.company_name AS external_company,
    c.speakers_jsonb,
    c.trackers_jsonb,
    c.topics_jsonb,
    c.questions_jsonb,
    c.call_outcome
  FROM calls c
  LEFT JOIN users u ON u.id = c.primary_user_id
  WHERE c.id = ANY(%s)
),
host_talk AS (
  SELECT
    src.call_id,
    SUM((s->>'talkTime')::float) FILTER (WHERE (s->>'userId') = src.primary_user_id) AS host_talk_time
  FROM src
  LEFT JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(src.speakers_jsonb) = 'array'
         THEN src.speakers_jsonb ELSE '[]'::jsonb END
  ) s ON true
  GROUP BY src.call_id
),
tracker_hits AS (
  SELECT
    src.call_id,
    COALESCE(jsonb_object_agg(t->>'name', (t->>'count')::int)
             FILTER (WHERE (t->>'count')::int > 0),
             '{}'::jsonb) AS tracker_hits
  FROM src
  LEFT JOIN LATERAL jsonb_array_elements(src.trackers_jsonb) t ON true
  GROUP BY src.call_id
),
topic_dur AS (
  SELECT
    src.call_id,
    COALESCE(jsonb_object_agg(tp->>'name', (tp->>'duration')::int)
             FILTER (WHERE tp ? 'name'),
             '{}'::jsonb) AS topic_durations,
    bool_or(
      (tp->>'name') ILIKE '%next step%'
      AND (tp->>'duration')::int > 0
    ) AS had_next_steps
  FROM src
  LEFT JOIN LATERAL jsonb_array_elements(src.topics_jsonb) tp ON true
  GROUP BY src.call_id
),
q_counts AS (
  -- In the Gong extensive response, `questions` is already aggregated as
  -- {companyCount, nonCompanyCount}. Map directly: host == companyCount,
  -- guest == nonCompanyCount.
  SELECT
    src.call_id,
    NULLIF((src.questions_jsonb->>'companyCount')::int, 0)    AS qc_host,
    NULLIF((src.questions_jsonb->>'nonCompanyCount')::int, 0) AS qc_guest
  FROM src
),
guest_counts AS (
  SELECT call_id, COUNT(*) FILTER (WHERE affiliation = 'External') AS guest_count
  FROM call_parties
  WHERE call_id = ANY(%s)
  GROUP BY call_id
),
seg_agg AS (
  SELECT call_id, COUNT(*) AS segment_count,
         SUM((CASE WHEN text IS NULL OR text='' THEN 0
                   ELSE array_length(regexp_split_to_array(text, E'\\\\s+'), 1) END)) AS word_count
  FROM transcript_segments
  WHERE call_id = ANY(%s)
  GROUP BY call_id
)
INSERT INTO call_metrics (
  call_id, host_user_id, host_email, guest_count, external_company,
  duration_sec, talk_ratio_host, question_count_host, question_count_guest,
  tracker_hits, topic_durations, had_next_steps,
  started, started_date, segment_count, word_count, computed_at
)
SELECT
  src.call_id, src.primary_user_id, src.host_email,
  gc.guest_count, src.external_company, src.duration_sec,
  CASE WHEN src.duration_sec > 0 AND ht.host_talk_time IS NOT NULL
       THEN (ht.host_talk_time / src.duration_sec)::real
       ELSE NULL END,
  qc.qc_host, qc.qc_guest,
  th.tracker_hits, td.topic_durations, td.had_next_steps,
  src.started, src.started_date, sa.segment_count, sa.word_count, now()
FROM src
LEFT JOIN host_talk     ht ON ht.call_id = src.call_id
LEFT JOIN tracker_hits  th ON th.call_id = src.call_id
LEFT JOIN topic_dur     td ON td.call_id = src.call_id
LEFT JOIN q_counts      qc ON qc.call_id = src.call_id
LEFT JOIN guest_counts  gc ON gc.call_id = src.call_id
LEFT JOIN seg_agg       sa ON sa.call_id = src.call_id
ON CONFLICT (call_id) DO UPDATE SET
  host_user_id         = EXCLUDED.host_user_id,
  host_email           = EXCLUDED.host_email,
  guest_count          = EXCLUDED.guest_count,
  external_company     = EXCLUDED.external_company,
  duration_sec         = EXCLUDED.duration_sec,
  talk_ratio_host      = EXCLUDED.talk_ratio_host,
  question_count_host  = EXCLUDED.question_count_host,
  question_count_guest = EXCLUDED.question_count_guest,
  tracker_hits         = EXCLUDED.tracker_hits,
  topic_durations      = EXCLUDED.topic_durations,
  had_next_steps       = EXCLUDED.had_next_steps,
  started              = EXCLUDED.started,
  started_date         = EXCLUDED.started_date,
  segment_count        = EXCLUDED.segment_count,
  word_count           = EXCLUDED.word_count,
  computed_at          = now();
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--only-missing", action="store_true", default=True,
                    help="Only process calls not yet in call_metrics.")
    ap.add_argument("--all", dest="only_missing", action="store_false",
                    help="Recompute for every call.")
    ap.add_argument("--limit", type=int, default=None, help="Cap total calls processed (for smoke tests).")
    args = ap.parse_args()

    connector, conn = connect()
    try:
        cur = conn.cursor()
        if args.only_missing:
            cur.execute(
                "SELECT c.id FROM calls c "
                "LEFT JOIN call_metrics m ON m.call_id = c.id "
                "WHERE m.call_id IS NULL ORDER BY c.started DESC NULLS LAST"
            )
        else:
            cur.execute("SELECT id FROM calls ORDER BY started DESC NULLS LAST")
        ids = [r[0] for r in cur.fetchall()]
        if args.limit:
            ids = ids[: args.limit]
        cur.close()

        total = len(ids)
        log.info("processing %d calls in batches of %d", total, args.batch)
        done = 0
        t0 = time.time()
        for i in range(0, total, args.batch):
            batch = ids[i : i + args.batch]
            cur = conn.cursor()
            try:
                cur.execute(BACKFILL_SQL, (batch, batch, batch))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
            done += len(batch)
            rate = done / max(time.time() - t0, 0.001)
            log.info("  %d/%d (%.1f calls/s)", done, total, rate)
        log.info("done.")
        return 0
    finally:
        conn.close()
        connector.close()


if __name__ == "__main__":
    sys.exit(main())
