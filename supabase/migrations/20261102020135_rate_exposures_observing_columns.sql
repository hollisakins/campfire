SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP FUNCTION "public"."get_program_mbkg"(text);

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "filter" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "exposure_time" double precision;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "date_obs" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "readout_pattern" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "n_groups" integer;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "n_integrations" integer;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "dither_position" integer;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "n_dither_positions" integer;

CREATE OR REPLACE FUNCTION public.get_program_exposures (
  p_program_slug text
)
  RETURNS TABLE (
    observation        text,
    exposure_root      text,
    detector           text,
    grating            text,
    filter             text,
    date_obs           text,
    exposure_time      double precision,
    readout_pattern    text,
    n_groups           integer,
    n_integrations     integer,
    dither_position    integer,
    n_dither_positions integer,
    mbkg_source        text
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  SELECT r.observation, r.exposure_root, r.detector, r.grating, r.filter,
         r.date_obs, r.exposure_time, r.readout_pattern,
         r.n_groups, r.n_integrations, r.dither_position,
         r.n_dither_positions, r.mbkg_source
  FROM public.nirspec_rate_exposures r
  JOIN public.observations o ON o.name = r.observation
  WHERE o.program_slug = p_program_slug
    AND p_program_slug = ANY(public.accessible_program_slugs())
    AND (NOT public.is_link_account() OR r.observation = public.link_observation())
  ORDER BY r.observation, r.exposure_root, r.detector;
$function$;

REVOKE ALL ON FUNCTION "public"."get_program_exposures"(text) FROM PUBLIC, "anon";

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."date_obs" IS 'UTC date the exposure started (DATE-OBS).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."dither_position" IS 'Position of the exposure in the dither / nod pattern (PATT_NUM), 1-based.';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."exposure_time" IS 'Effective exposure time in seconds (EFFEXPTM).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."filter" IS 'FILTER of the exposure.';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."n_dither_positions" IS 'Points in the dither / nod pattern (NUMDTHPT).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."n_groups" IS 'Groups per integration (NGROUPS).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."n_integrations" IS 'Integrations in the exposure (NINTS).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."readout_pattern" IS 'Detector readout pattern (READPATT), e.g. NRSIRS2.';

GRANT EXECUTE ON FUNCTION "public"."get_program_exposures"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_program_exposures"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_program_exposures"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_program_exposures"(text) TO "service_role";
