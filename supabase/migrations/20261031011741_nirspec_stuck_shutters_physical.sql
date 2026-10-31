SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE SEQUENCE "public"."nirspec_stuck_shutters_id_seq" AS integer INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 START WITH 1 CACHE 1 NO CYCLE;

REVOKE ALL ON SEQUENCE "public"."nirspec_stuck_shutters_id_seq" FROM "anon";

CREATE TABLE "public"."nirspec_stuck_shutters" (
  "id"            integer                  NOT NULL DEFAULT nextval('public.nirspec_stuck_shutters_id_seq'::regclass),
  "observation"   text                     NOT NULL,
  "exposure_root" text                     NOT NULL,
  "shutter"       text                     NOT NULL,
  "origin"        text                     NOT NULL,
  "stuck"         boolean                  NOT NULL,
  "note"          text,
  "created_at"    timestamp with time zone NOT NULL DEFAULT now(),
  "updated_at"    timestamp with time zone NOT NULL DEFAULT now(),
  "updated_by"    uuid,
  CONSTRAINT "nirspec_stuck_shutters_origin_check" CHECK ((origin = ANY (ARRAY['manual'::text, 'web'::text]))),
  CONSTRAINT "nirspec_stuck_shutters_pkey" PRIMARY KEY (id),
  CONSTRAINT "nirspec_stuck_shutters_shutter_check" CHECK ((shutter ~ '^q[1-4]r[0-9]+c[0-9]+$'::text)),
  CONSTRAINT "nirspec_stuck_shutters_unique" UNIQUE (observation, exposure_root, shutter)
);

ALTER TABLE "public"."nirspec_stuck_shutters"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."nirspec_stuck_shutters" FROM "anon";

ALTER SEQUENCE "public"."nirspec_stuck_shutters_id_seq" OWNED BY "public"."nirspec_stuck_shutters"."id";

ALTER TABLE "public"."spectrum_exposures"
  ADD COLUMN "msa_shutters" text[];

CREATE OR REPLACE FUNCTION public.stamp_nirspec_stuck_shutter()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SET search_path TO ''
  AS $function$
BEGIN
    NEW.updated_at = NOW();
    NEW.updated_by = COALESCE(auth.uid(), NEW.updated_by);
    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."stamp_nirspec_stuck_shutter"() FROM PUBLIC, "anon";

CREATE TRIGGER stamp_nirspec_stuck_shutter_trigger
  BEFORE INSERT OR UPDATE ON public.nirspec_stuck_shutters
  FOR EACH ROW
  EXECUTE FUNCTION public.stamp_nirspec_stuck_shutter();

CREATE POLICY "admin_delete_nirspec_stuck_shutters" ON "public"."nirspec_stuck_shutters"
  FOR DELETE
  TO "authenticated"
  USING (( SELECT public.is_admin() AS is_admin));

CREATE POLICY "admin_insert_nirspec_stuck_shutters" ON "public"."nirspec_stuck_shutters"
  FOR INSERT
  TO "authenticated"
  WITH CHECK (( SELECT public.is_admin() AS is_admin));

CREATE POLICY "admin_select_nirspec_stuck_shutters" ON "public"."nirspec_stuck_shutters"
  FOR SELECT
  TO "authenticated"
  USING (( SELECT public.is_admin() AS is_admin));

CREATE POLICY "admin_update_nirspec_stuck_shutters" ON "public"."nirspec_stuck_shutters"
  FOR UPDATE
  TO "authenticated"
  USING (( SELECT public.is_admin() AS is_admin))
  WITH CHECK (( SELECT public.is_admin() AS is_admin));

COMMENT ON COLUMN "public"."nirspec_stuck_shutters"."exposure_root" IS 'Pipeline 2-token root = one MSA configuration (e.g. jw07076001001_05101); same semantics as nirspec_source_review.exposure_root. A stuck shutter never carries over to another root.';

COMMENT ON COLUMN "public"."nirspec_stuck_shutters"."shutter" IS 'Physical shutter key q{shutter_quadrant}r{shutter_row}c{shutter_column} from the MSA metafile SHUTTER_INFO (unpadded).';

COMMENT ON COLUMN "public"."nirspec_stuck_shutters"."updated_by" IS 'auth.uid() of the last writer (stamped by trigger); NULL for service-role writes.';

COMMENT ON COLUMN "public"."spectrum_exposures"."msa_shutters" IS 'Physical shutters "q<quadrant>r<row>c<column>" of the source''s slitlet in this exposure root (every dither point of its MSA configuration), from the original metafile''s SHUTTER_INFO as stage 2a edits it for the source (MetaFile.rows_for_source); sorted by (column, row), so the k-th distinct column is the legacy stuck-shutter ordinal k. Deploy-populated; NULL for rows deployed before the column or when no metafile was found.';

COMMENT ON TABLE "public"."nirspec_stuck_shutters" IS 'Stuck-closed NIRSpec MSA shutters, one row per (observation, exposure_root, physical shutter "q<quadrant>r<row>c<column>"). origin manual (pushed from the local stuck_closed_shutters.toml on deploy) or web (the nods review); auto calls stay local. stuck=false vetoes an auto call. Precedence manual > web > auto. Admin-only; supersedes nirspec_source_review.stuck_shutters.';

GRANT EXECUTE ON FUNCTION "public"."stamp_nirspec_stuck_shutter"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."stamp_nirspec_stuck_shutter"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."stamp_nirspec_stuck_shutter"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."stamp_nirspec_stuck_shutter"() TO "service_role";

GRANT SELECT, UPDATE, USAGE ON SEQUENCE "public"."nirspec_stuck_shutters_id_seq" TO "authenticated";

REVOKE ALL ON SEQUENCE "public"."nirspec_stuck_shutters_id_seq" FROM "postgres";

GRANT SELECT, UPDATE, USAGE ON SEQUENCE "public"."nirspec_stuck_shutters_id_seq" TO "postgres";

GRANT SELECT, UPDATE, USAGE ON SEQUENCE "public"."nirspec_stuck_shutters_id_seq" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."nirspec_stuck_shutters" TO "authenticated";

REVOKE ALL ON TABLE "public"."nirspec_stuck_shutters" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."nirspec_stuck_shutters" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."nirspec_stuck_shutters" TO "service_role";
