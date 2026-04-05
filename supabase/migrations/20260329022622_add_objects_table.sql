create sequence "public"."objects_id_seq";


  create table "public"."objects" (
    "id" integer not null default nextval('public.objects_id_seq'::regclass),
    "object_id" text not null,
    "field" text not null,
    "ra" double precision not null,
    "dec" double precision not null,
    "created_at" timestamp with time zone default now(),
    "n_targets" integer not null default 0,
    "n_spectra" integer not null default 0,
    "programs" text[] not null default '{}'::text[],
    "gratings" text[] not null default '{}'::text[],
    "max_snr" double precision,
    "max_exposure_time" double precision,
    "best_redshift" double precision,
    "best_redshift_quality" integer default 0
      );


alter table "public"."objects" enable row level security;

alter table "public"."targets" add column "object_id" integer;

alter sequence "public"."objects_id_seq" owned by "public"."objects"."id";

CREATE INDEX idx_objects_best_redshift ON public.objects USING btree (best_redshift) WHERE (best_redshift IS NOT NULL);

CREATE INDEX idx_objects_best_redshift_quality ON public.objects USING btree (best_redshift_quality);

CREATE INDEX idx_objects_coords ON public.objects USING btree (ra, "dec");

CREATE INDEX idx_objects_field ON public.objects USING btree (field);

CREATE INDEX idx_objects_gratings ON public.objects USING gin (gratings);

CREATE INDEX idx_objects_max_snr ON public.objects USING btree (max_snr) WHERE (max_snr IS NOT NULL);

CREATE INDEX idx_objects_programs ON public.objects USING gin (programs);

CREATE INDEX idx_targets_object_id ON public.targets USING btree (object_id);

CREATE UNIQUE INDEX objects_object_id_key ON public.objects USING btree (object_id);

CREATE UNIQUE INDEX objects_pkey ON public.objects USING btree (id);

alter table "public"."objects" add constraint "objects_pkey" PRIMARY KEY using index "objects_pkey";

alter table "public"."objects" add constraint "objects_object_id_key" UNIQUE using index "objects_object_id_key";

alter table "public"."targets" add constraint "fk_targets_object" FOREIGN KEY (object_id) REFERENCES public.objects(id) not valid;

alter table "public"."targets" validate constraint "fk_targets_object";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50)
 RETURNS TABLE(targets jsonb, total_count bigint, page integer, page_size integer)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'best_redshift', 'best_redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

  -- Intersect user-accessible programs with filter selection
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Step 1: count
  SELECT COUNT(*) INTO v_total_count
  FROM objects o
  WHERE
    -- Access control: object must have at least one accessible program
    o.programs && v_filtered_program_slugs
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.best_redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.best_redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.best_redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.best_redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.best_redshift_quality = 0)
    )
    AND (
      NOT v_coord_search_active
      OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    );

  -- Step 2: fetch page
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.id,
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.n_targets,
      o.n_spectra,
      o.programs,
      o.gratings,
      o.max_snr,
      o.max_exposure_time,
      o.best_redshift,
      o.best_redshift_quality,
      o.created_at,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM objects o
    WHERE
      o.programs && v_filtered_program_slugs
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.best_redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.best_redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.best_redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.best_redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.best_redshift_quality = 0)
      )
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN o.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN o.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN o.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN o.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN o.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN o.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN o.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN o.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift' AND p_sort_direction = 'asc' THEN o.best_redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift' AND p_sort_direction = 'desc' THEN o.best_redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'asc' THEN o.best_redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'desc' THEN o.best_redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
      o.object_id ASC
    LIMIT p_page_size OFFSET v_offset
  ),
  with_members AS (
    SELECT
      jsonb_build_object(
        'id', fo.id,
        'object_id', fo.object_id,
        'field', fo.field,
        'ra', fo.ra,
        'dec', fo.dec,
        'n_targets', fo.n_targets,
        'n_spectra', fo.n_spectra,
        'programs', fo.programs,
        'gratings', fo.gratings,
        'max_snr', fo.max_snr,
        'max_exposure_time', fo.max_exposure_time,
        'best_redshift', fo.best_redshift,
        'best_redshift_quality', fo.best_redshift_quality,
        'created_at', fo.created_at,
        'distance', fo.distance,
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift', t.redshift,
              'redshift_quality', t.redshift_quality
            )
          )
          FROM targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(v_filtered_program_slugs)
          ),
          '[]'::jsonb
        )
      ) AS obj_json
    FROM filtered_objects fo
  )
  SELECT
    COALESCE(jsonb_agg(wm.obj_json), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size
  FROM with_members wm;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.update_object_best_redshift()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.object_id IS NULL THEN RETURN NEW; END IF;

    UPDATE objects SET
        best_redshift = sub.redshift,
        best_redshift_quality = sub.redshift_quality
    FROM (
        SELECT redshift::double precision, redshift_quality
        FROM targets
        WHERE object_id = NEW.object_id
          AND redshift IS NOT NULL
        ORDER BY redshift_quality DESC NULLS LAST,
                 max_snr DESC NULLS LAST
        LIMIT 1
    ) sub
    WHERE objects.id = NEW.object_id;

    -- Handle case where no member has a redshift (all Impossible)
    IF NOT FOUND THEN
        UPDATE objects SET
            best_redshift = NULL,
            best_redshift_quality = (
                SELECT MAX(redshift_quality) FROM targets WHERE object_id = NEW.object_id
            )
        WHERE objects.id = NEW.object_id;
    END IF;

    RETURN NEW;
END;
$function$
;

grant delete on table "public"."objects" to "anon";

grant insert on table "public"."objects" to "anon";

grant references on table "public"."objects" to "anon";

grant select on table "public"."objects" to "anon";

grant trigger on table "public"."objects" to "anon";

grant truncate on table "public"."objects" to "anon";

grant update on table "public"."objects" to "anon";

grant delete on table "public"."objects" to "authenticated";

grant insert on table "public"."objects" to "authenticated";

grant references on table "public"."objects" to "authenticated";

grant select on table "public"."objects" to "authenticated";

grant trigger on table "public"."objects" to "authenticated";

grant truncate on table "public"."objects" to "authenticated";

grant update on table "public"."objects" to "authenticated";

grant delete on table "public"."objects" to "service_role";

grant insert on table "public"."objects" to "service_role";

grant references on table "public"."objects" to "service_role";

grant select on table "public"."objects" to "service_role";

grant trigger on table "public"."objects" to "service_role";

grant truncate on table "public"."objects" to "service_role";

grant update on table "public"."objects" to "service_role";


  create policy "select_objects_by_access"
  on "public"."objects"
  as permissive
  for select
  to public
using ((programs && public.accessible_program_slugs()));


CREATE TRIGGER update_object_best_redshift_trigger AFTER UPDATE OF redshift_quality, redshift_inspected ON public.targets FOR EACH ROW WHEN ((new.object_id IS NOT NULL)) EXECUTE FUNCTION public.update_object_best_redshift();


