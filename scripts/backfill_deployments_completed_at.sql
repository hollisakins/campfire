-- One-time backfill for deployments.completed_at (rows that predate the column).
--
-- The deploy CLI opens a deployments row before it uploads and, since the column
-- landed, stamps completed_at as the deploy's last write, only when no upload
-- failed. Older rows are NULL, which the admin views show as "incomplete". This
-- fills the ones the audit log proves finished: every deploy path writes its
-- deploy_events 'upload' event (deployment_id set, metadata counts.failed the
-- upload failure count) in the same finalize step that now stamps completed_at, so a
-- row with such an event and zero failures gets that event's occurred_at.
--
-- Left NULL on purpose: rows with no upload event (the deploy died before its
-- finalize step, or predates the audit log) and rows whose event recorded failed
-- uploads. There is no reliable signal for those, and NULL is what a deploy
-- that left failures behind now gets anyway. One known gap: a NIRCam field
-- event counts exposure upload failures only, so a field deploy whose mosaic
-- uploads failed (and nothing else) is stamped here although the CLI would not.
--
-- Idempotent and resumable: only NULL rows are written, the table is small (one
-- row per deploy), and nothing triggers on deployments. Run once on the VM after
-- the migration, as the database superuser:
--   docker exec -i campfire-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < scripts/backfill_deployments_completed_at.sql
\timing on
BEGIN;
SET LOCAL statement_timeout = '5min';

UPDATE deployments d
   SET completed_at = e.finished_at
  FROM (SELECT deployment_id, max(occurred_at) AS finished_at
          FROM deploy_events
         WHERE action = 'upload'
           AND deployment_id IS NOT NULL
           -- the normalized envelope (counts.failed, flags.partial); a bare
           -- top-level 'failed' from older producers counts too
           AND COALESCE(metadata #>> '{flags,partial}', 'false') = 'false'
           AND COALESCE(metadata #>> '{counts,failed}', metadata->>'failed', '0') = '0'
         GROUP BY deployment_id) e
 WHERE e.deployment_id = d.id
   AND d.completed_at IS NULL;

COMMIT;

-- What is left incomplete, by status (review before shrugging it off).
SELECT status, count(*) AS incomplete
  FROM deployments
 WHERE completed_at IS NULL
 GROUP BY status
 ORDER BY status;
