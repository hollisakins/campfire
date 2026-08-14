-- compute_object_redshift_auto: replace the tiered grating hierarchy
-- (PRISM > medium > high-res) with an explicit per-grating priority:
--
--   PRISM > G395M > G395H > G235M > G235H > G140M > G140H
--
-- exposure_time remains the tiebreak between spectra of equal priority
-- (i.e. same grating), with spectrum id as the deterministic final tiebreak.
-- Everything else — staleness stamping for inspected objects, the no-op
-- skip, the statement_timeout guard — is unchanged.
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
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_quality AS quality,
           (
             SELECT s.redshift_auto
             FROM targets t
             JOIN spectra s ON s.target_id = t.target_id
             WHERE t.object_id = o.id
               AND s.redshift_auto IS NOT NULL
             ORDER BY
               CASE
                 WHEN s.grating = 'PRISM' THEN 0
                 WHEN s.grating = 'G395M' THEN 1
                 WHEN s.grating = 'G395H' THEN 2
                 WHEN s.grating = 'G235M' THEN 3
                 WHEN s.grating = 'G235H' THEN 4
                 WHEN s.grating = 'G140M' THEN 5
                 WHEN s.grating = 'G140H' THEN 6
                 ELSE 7
               END ASC,
               s.exposure_time DESC NULLS LAST,
               s.id ASC
             LIMIT 1
           ) AS new_val
    FROM objects o
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

COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation by compute_object_redshift_auto() from the best member spectrum under a grating-priority hierarchy (PRISM > G395M > G395H > G235M > G235H > G140M > G140H, tiebreak on exposure_time). Empty until Phase D migration.';
