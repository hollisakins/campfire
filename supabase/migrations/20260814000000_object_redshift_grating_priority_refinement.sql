-- compute_object_redshift_auto: explicit grating priority + PRISM-consistent
-- refinement + solution-level staleness badging.
--
-- 1. The tiered grating hierarchy (PRISM > medium > high-res) becomes an
--    explicit per-grating priority:
--      PRISM > G395M > G395H > G235M > G235H > G140M > G140H
--    exposure_time remains the tiebreak between spectra of equal priority
--    (i.e. same grating), with spectrum id as the deterministic final
--    tiebreak.
--
-- 2. New refinement layer: when the anchor is PRISM and a grating
--    spectrum's auto-fit agrees with it within
--    |z_grating - z_prism| / (1 + z_prism) < 0.01, the best such grating
--    redshift is adopted instead for its higher precision. Objects with no
--    PRISM member keep the plain priority-ordered pick; objects whose
--    gratings all disagree with PRISM keep the PRISM value.
--
-- 3. The staleness_reason='reprocessed' badge on signed-off objects
--    (quality >= 2) now fires only on solution-level changes: a shift
--    within the same (1+z)-scaled tolerance is by definition the same
--    redshift solution at different precision (e.g. the PRISM anchor being
--    swapped for a consistent grating fit) and does not warrant re-review.
--    Appearing/disappearing values always badge. This prevents a one-time
--    "Needs Review" wave across the inspected archive when this migration's
--    redefinition first recomputes the field.
--
-- The no-op skip and the statement_timeout guard are unchanged.
--
-- Hand-authored (comment changes are not tracked by `supabase db diff`);
-- definition mirrors supabase/schemas/functions.sql.

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
DECLARE
  n INTEGER;
  -- (1+z)-scaled tolerance for a grating auto-fit to count as consistent
  -- with the PRISM anchor: |z_grating - z_prism| / (1 + z_prism) < z_delta.
  -- Also the re-review threshold: auto-fit shifts smaller than this are the
  -- same solution at different precision and do not badge inspected objects.
  z_delta CONSTANT DOUBLE PRECISION := 0.01;
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_quality AS quality,
           best.new_val
    FROM objects o
    LEFT JOIN LATERAL (
      WITH members AS (
        SELECT s.redshift_auto AS z,
               s.exposure_time,
               s.id,
               CASE
                 WHEN s.grating = 'PRISM' THEN 0
                 WHEN s.grating = 'G395M' THEN 1
                 WHEN s.grating = 'G395H' THEN 2
                 WHEN s.grating = 'G235M' THEN 3
                 WHEN s.grating = 'G235H' THEN 4
                 WHEN s.grating = 'G140M' THEN 5
                 WHEN s.grating = 'G140H' THEN 6
                 ELSE 7
               END AS priority
        FROM targets t
        JOIN spectra s ON s.target_id = t.target_id
        WHERE t.object_id = o.id
          AND s.redshift_auto IS NOT NULL
      ),
      prism AS (
        SELECT z FROM members WHERE priority = 0
        ORDER BY exposure_time DESC NULLS LAST, id ASC
        LIMIT 1
      ),
      -- Best member outright: PRISM if present, else best grating.
      base AS (
        SELECT z FROM members
        ORDER BY priority ASC, exposure_time DESC NULLS LAST, id ASC
        LIMIT 1
      ),
      -- PRISM-consistent refinement: best known grating whose auto-fit
      -- agrees with the PRISM anchor. Empty when no PRISM member exists.
      refined AS (
        SELECT m.z
        FROM members m, prism p
        WHERE m.priority BETWEEN 1 AND 6
          AND ABS(m.z - p.z) / (1.0 + p.z) < z_delta
        ORDER BY m.priority ASC, m.exposure_time DESC NULLS LAST, m.id ASC
        LIMIT 1
      )
      SELECT COALESCE((SELECT z FROM refined), (SELECT z FROM base)) AS new_val
    ) best ON TRUE
    WHERE o.field = p_field
  )
  UPDATE objects o
  SET redshift_auto = c.new_val,
      staleness_reason = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
             AND (c.old_auto IS NULL OR c.new_val IS NULL
                  OR ABS(c.new_val - c.old_auto) / (1.0 + c.old_auto) >= z_delta)
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
             AND (c.old_auto IS NULL OR c.new_val IS NULL
                  OR ABS(c.new_val - c.old_auto) / (1.0 + c.old_auto) >= z_delta)
        THEN NOW()
        ELSE o.last_data_change_at
      END,
      updated_at = NOW()
  FROM computed c
  WHERE o.id = c.id
    AND o.redshift_auto IS DISTINCT FROM c.new_val;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.compute_object_redshift_auto(TEXT) TO service_role;

COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation by compute_object_redshift_auto() from the best member spectrum under a grating-priority hierarchy (PRISM > G395M > G395H > G235M > G235H > G140M > G140H, tiebreak on exposure_time); when a grating auto-fit agrees with the PRISM anchor within |dz|/(1+z) < 0.01, the best such grating redshift is adopted for its higher precision. Empty until Phase D migration.';
