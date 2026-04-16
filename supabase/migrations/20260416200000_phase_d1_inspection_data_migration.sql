-- =============================================================================
-- Phase D.1 — One-time data migration: lift inspection state targets → objects
-- =============================================================================
-- Migrates per-target inspection state (redshift_inspected, redshift_quality,
-- last_inspected_at, last_inspected_by) up to the parent object, backfills
-- spectra.redshift_auto and spectra.dq_flags from targets, and seeds
-- last_data_change_at so the staleness machinery has a starting baseline.
--
-- This is pure DML — no schema changes. Schema-level changes (function/trigger
-- replacements, flag_audit_log retargeting) are in the companion phase_d2
-- migration which runs immediately after.
--
-- Ordering note: this migration runs *before* the trigger replacements in D.2.
-- The legacy `update_object_best_redshift` trigger on targets is not affected
-- here (we don't UPDATE targets); the new `bump_object_version` trigger on
-- objects doesn't exist yet, so D.1a's bulk UPDATE doesn't gratuitously
-- increment `version`. (Once D.2 lands, every UPDATE goes through the new
-- trigger.)
-- =============================================================================


-- D.1a — Copy inspection state from the best member target to each object.
--
-- "Best" = highest redshift_quality, ties broken by max_snr. DISTINCT ON keeps
-- one row per object_id. Targets with quality=0 are excluded so an object with
-- one inspected member and several uninspected ones doesn't get its state
-- nulled out by the wrong DISTINCT ON pick.
WITH best_targets AS (
    SELECT DISTINCT ON (t.object_id)
        t.object_id,
        t.redshift_inspected,
        t.redshift_quality,
        t.last_inspected_at,
        t.last_inspected_by
    FROM public.targets t
    WHERE t.object_id IS NOT NULL
      AND t.redshift_quality > 0
    ORDER BY t.object_id,
             t.redshift_quality DESC NULLS LAST,
             t.max_snr DESC NULLS LAST
)
UPDATE public.objects o SET
    redshift_inspected = bt.redshift_inspected,
    redshift_quality = bt.redshift_quality,
    last_inspected_at = bt.last_inspected_at,
    last_inspected_by = bt.last_inspected_by
FROM best_targets bt
WHERE o.id = bt.object_id;


-- D.1a (cont) — Flag migration conflicts: objects whose member targets each
-- carry a Secure (quality=4) inspected redshift, but those redshifts disagree
-- (>0.01 apart, after rounding to two decimals to absorb display jitter).
-- Surfaces in the UI as a "Needs Review" badge with `staleness_reason =
-- 'migration_conflict'`. The Phase D.1a copy above picked one of the
-- conflicting values somewhat arbitrarily (highest quality + max_snr); the
-- inspector resolves it post-migration.
WITH conflict_objects AS (
    SELECT t.object_id
    FROM public.targets t
    WHERE t.object_id IS NOT NULL AND t.redshift_quality = 4
    GROUP BY t.object_id
    HAVING COUNT(DISTINCT ROUND(t.redshift_inspected::numeric, 2)) > 1
)
UPDATE public.objects o SET
    last_data_change_at = NOW(),
    staleness_reason = 'migration_conflict'
FROM conflict_objects c
WHERE o.id = c.object_id;


-- D.1b — Backfill spectra.redshift_auto from targets.redshift_auto for any
-- spectrum that wasn't already populated by Phase B (e.g., observations that
-- haven't been re-deployed since Phase B shipped). Conditional on NULL so we
-- don't clobber per-grating values the pipeline already wrote.
UPDATE public.spectra s SET redshift_auto = t.redshift_auto
FROM public.targets t
WHERE s.target_id = t.target_id
  AND s.redshift_auto IS NULL
  AND t.redshift_auto IS NOT NULL;


-- D.1b (cont) — Compute objects.redshift_auto = redshift_auto of the
-- highest-S/N member spectrum. This is the same logic as
-- compute_object_redshift_auto() but applied across all fields at once.
-- Future deploys re-run it per-field via reconcile_field_objects().
WITH best_spectrum AS (
    SELECT DISTINCT ON (t.object_id)
        t.object_id, s.redshift_auto
    FROM public.spectra s
    JOIN public.targets t ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL AND s.redshift_auto IS NOT NULL
    ORDER BY t.object_id, s.signal_to_noise DESC NULLS LAST, s.id ASC
)
UPDATE public.objects o SET redshift_auto = bs.redshift_auto
FROM best_spectrum bs WHERE o.id = bs.object_id;


-- D.1c — Copy DQ flags from targets to all their member spectra.
-- Conservative: a target with dq_flags=4 marks all of its spectra dq_flags=4,
-- even if only one grating is actually compromised. Inspectors refine
-- per-spectrum after migration.
UPDATE public.spectra s SET dq_flags = t.dq_flags
FROM public.targets t
WHERE s.target_id = t.target_id
  AND t.dq_flags IS NOT NULL
  AND t.dq_flags != 0;


-- D.1d — Seed last_data_change_at on already-inspected objects so the
-- staleness machinery has a reference timestamp. Without this, every
-- post-migration reconcile would mark every inspected object stale (because
-- last_data_change_at IS NULL is treated as "never reconciled").
-- Use updated_at as the proxy for the data freshness moment of the migration.
UPDATE public.objects SET last_data_change_at = updated_at
WHERE last_inspected_at IS NOT NULL
  AND last_data_change_at IS NULL;
