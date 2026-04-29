-- Skip no-op rewrites in compute_object_redshift_auto and bump updated_at
-- for rows whose redshift_auto actually changes, so get_objects_for_sync
-- (which uses updated_at > p_updated_since as its delta cursor) picks the
-- change up on the next client sync. Prior version unconditionally
-- rewrote every object in the field and did not touch updated_at, which
-- meant pipeline-driven redshift_auto changes were silently invisible to
-- incremental sync.

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
  n INTEGER;
BEGIN
  WITH computed AS (
    SELECT o.id,
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
      updated_at = NOW()
  FROM computed c
  WHERE o.id = c.id
    AND o.redshift_auto IS DISTINCT FROM c.new_val;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;
