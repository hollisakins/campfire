-- compute_object_redshift_auto: preserve implicit sign-offs when the auto-fit
-- changes under an already-inspected object.
--
-- Previously this function silently overwrote objects.redshift_auto whenever
-- the best member spectrum produced a new value. For objects where an
-- inspector had committed a quality flag without entering a numeric override
-- (redshift_quality >= 2 AND redshift_inspected IS NULL), the generated
-- ``redshift`` column simply tracked redshift_auto — meaning reprocessing
-- could silently move the object's displayed redshift off the value the
-- inspector actually reviewed, while keeping the quality flag intact.
--
-- New behavior: when such a sign-off exists AND the auto-fit is about to
-- change, we first promote the OLD redshift_auto into redshift_inspected
-- (pinning the generated redshift to what was reviewed) and flag
-- ``staleness_reason = 'reprocessed'`` so the UI surfaces a "Needs Review"
-- badge. Uninspected objects (quality = 0), explicit-override objects
-- (redshift_inspected IS NOT NULL), and Impossible objects (quality = 1,
-- generated redshift is already NULL) are unaffected.
--
-- See scripts/repair_implicit_signoff_redshifts.py for the one-time cleanup
-- of sign-offs already lost in Phase D.1 / post-migration re-deploys.

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
  n INTEGER;
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_inspected AS old_inspected,
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
                 WHEN s.grating IN ('G140M', 'G235M', 'G395M') THEN 1
                 WHEN s.grating IN ('G140H', 'G235H', 'G395H') THEN 2
                 ELSE 3
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
      redshift_inspected = CASE
        WHEN c.quality >= 2
             AND c.old_inspected IS NULL
             AND c.old_auto IS NOT NULL
             AND c.new_val IS DISTINCT FROM c.old_auto
        THEN c.old_auto::numeric
        ELSE o.redshift_inspected
      END,
      staleness_reason = CASE
        WHEN c.quality >= 2
             AND c.old_inspected IS NULL
             AND c.old_auto IS NOT NULL
             AND c.new_val IS DISTINCT FROM c.old_auto
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_inspected IS NULL
             AND c.old_auto IS NOT NULL
             AND c.new_val IS DISTINCT FROM c.old_auto
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
