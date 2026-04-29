-- PRISM-priority decision tree for objects.redshift_auto.
--
-- Supersedes the previous implementation (highest-SNR member spectrum).
-- New priority hierarchy:
--   1. PRISM (3x wavelength coverage, highest z-confirmation efficiency)
--   2. Medium-resolution gratings (G140M, G235M, G395M)
--   3. High-resolution gratings (G140H, G235H, G395H)
-- Tiebreaks within a tier: longest exposure_time, then lowest spectra.id.
-- SNR is intentionally dropped: contamination can inflate SNR, and PRISM's
-- broad wavelength coverage makes it the most reliable discriminator even
-- at modest SNR.

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field text)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
  n INTEGER;
BEGIN
  UPDATE objects o
  SET redshift_auto = (
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
  )
  WHERE o.field = p_field;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$
;
