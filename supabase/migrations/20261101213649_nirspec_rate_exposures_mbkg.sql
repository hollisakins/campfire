SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_storage_key" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_source" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_rate_source" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_test" text;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_exposures" text[];

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_n_empty_shutters" integer;

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD COLUMN "mbkg_version" text;

CREATE OR REPLACE FUNCTION public.get_program_mbkg (
  p_program_slug text
)
  RETURNS TABLE (
    observation           text,
    exposure_root         text,
    detector              text,
    grating               text,
    mbkg_source           text,
    mbkg_rate_source      text,
    mbkg_test             text,
    mbkg_exposures        text[],
    mbkg_n_empty_shutters integer,
    mbkg_version          text,
    has_product           boolean
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  SELECT r.observation, r.exposure_root, r.detector, r.grating,
         r.mbkg_source, r.mbkg_rate_source, r.mbkg_test, r.mbkg_exposures,
         r.mbkg_n_empty_shutters, r.mbkg_version, r.mbkg_storage_key IS NOT NULL
  FROM public.nirspec_rate_exposures r
  JOIN public.observations o ON o.name = r.observation
  WHERE o.program_slug = p_program_slug
    AND p_program_slug = ANY(public.accessible_program_slugs())
    AND (NOT public.is_link_account() OR r.observation = public.link_observation())
    AND (r.mbkg_source IS NOT NULL OR r.mbkg_rate_source IS NOT NULL)
  ORDER BY r.observation, r.exposure_root, r.detector;
$function$;

REVOKE ALL ON FUNCTION "public"."get_program_mbkg"(text) FROM PUBLIC, "anon";

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD CONSTRAINT "nirspec_rate_exposures_mbkg_rate_source_check" CHECK ((mbkg_rate_source = ANY (ARRAY['combined'::text, 'exposure'::text])));

ALTER TABLE "public"."nirspec_rate_exposures"
  ADD CONSTRAINT "nirspec_rate_exposures_mbkg_source_check" CHECK ((mbkg_source = ANY (ARRAY['combined'::text, 'exposure'::text])));

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_exposures" IS 'Exposures (nod/detector) combined into the master background.';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_n_empty_shutters" IS 'Empty shutters of this exposure x detector that passed (MBKGNSH).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_rate_source" IS 'The same choice for the count-rate master background derived from the rate file (master background step 1).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_source" IS 'Which master background the exposure uses: combined (over the exposures that agree) or exposure (its own empty shutters, when it differs from the others).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_storage_key" IS 'Canonical key of the exposure''s master background (<root>_<nrsN>_mbkg.fits, product nirspec_mbkg).';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_test" IS 'Outcome of the test against the other exposures: consistent, differs <band> <deviation> (<significance>), or too few empty shutters.';

COMMENT ON COLUMN "public"."nirspec_rate_exposures"."mbkg_version" IS 'Master-background algorithm version (CFMBKGV).';

GRANT EXECUTE ON FUNCTION "public"."get_program_mbkg"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_program_mbkg"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_program_mbkg"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_program_mbkg"(text) TO "service_role";
