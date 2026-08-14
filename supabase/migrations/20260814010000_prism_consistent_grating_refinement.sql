-- compute_object_redshift_auto: adopt a PRISM-consistent grating redshift
-- when one exists.
--
-- The best member spectrum by explicit grating priority (PRISM > G395M >
-- G395H > G235M > G235H > G140M > G140H, tiebreak on exposure_time, then id)
-- still anchors the value. New layer: when that anchor is PRISM and a
-- grating spectrum's auto-fit agrees with it within |dz| <= 0.03 (absolute,
-- not (1+z)-scaled), the best such grating redshift is adopted instead for
-- its higher precision. Objects with no PRISM member keep the plain
-- priority-ordered pick; objects whose gratings all disagree with PRISM
-- keep the PRISM value.
--
-- Staleness stamping, the no-op skip, and the statement_timeout guard are
-- unchanged.
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
  -- Absolute |dz| tolerance for a grating auto-fit to count as consistent
  -- with the PRISM anchor (not (1+z)-scaled).
  z_delta CONSTANT DOUBLE PRECISION := 0.03;
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
          AND ABS(m.z - p.z) <= z_delta
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
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
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

COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation by compute_object_redshift_auto() from the best member spectrum under a grating-priority hierarchy (PRISM > G395M > G395H > G235M > G235H > G140M > G140H, tiebreak on exposure_time); when a grating auto-fit agrees with the PRISM anchor within |dz| <= 0.03, the best such grating redshift is adopted for its higher precision. Empty until Phase D migration.';
