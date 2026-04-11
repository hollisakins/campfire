drop trigger if exists "update_max_exposure_time_trigger" on "public"."spectra";

drop trigger if exists "update_max_snr_trigger" on "public"."spectra";

drop function if exists "public"."update_target_max_exposure_time"();

drop function if exists "public"."update_target_max_snr"();

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.recompute_target_aggregates(p_target_ids text[])
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
  n INTEGER;
BEGIN
  UPDATE targets t SET
    max_snr = sub.max_snr,
    max_exposure_time = sub.max_exposure_time
  FROM (
    SELECT
      s.target_id,
      MAX(s.signal_to_noise) AS max_snr,
      MAX(s.exposure_time) AS max_exposure_time
    FROM spectra s
    WHERE s.target_id = ANY(p_target_ids)
    GROUP BY s.target_id
  ) sub
  WHERE t.target_id = sub.target_id;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$
;


