SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- #683 part 2: spectra.filter NOT NULL and the unique key moves to
-- (target_id, grating, filter). Precondition, checked on production on
-- 2026-11-17 by scripts/backfill_spectra_filter.sql: no NULL filter, no
-- duplicate (target_id, grating, filter). ~94 k rows: the NOT NULL scan and
-- the index build take seconds.
SET local check_function_bodies = off;

DROP INDEX "public"."idx_dr_spectra_tag_target";

DROP INDEX "public"."idx_spectra_target_grating";

DROP FUNCTION
  "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  integer,
  integer,
  integer,
  integer[],
  text,
  text,
  boolean,
  boolean,
  boolean,
  text,
  text,
  uuid,
  double precision,
  double precision,
  double precision,
  boolean,
  text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer);

ALTER TABLE "public"."dr_spectra"
  ALTER COLUMN "filter" SET NOT NULL;

ALTER TABLE "public"."spectra"
  ALTER COLUMN "filter" SET NOT NULL;

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any  integer          DEFAULT NULL::integer,
  p_dq_flags_include_all  integer          DEFAULT NULL::integer,
  p_dq_flags_exclude      integer          DEFAULT NULL::integer,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_line_columns          jsonb            DEFAULT NULL::jsonb,
  p_band_columns          jsonb            DEFAULT NULL::jsonb,
  p_spectrum_ids          integer[]        DEFAULT NULL::integer[],
  p_after_id              integer          DEFAULT NULL::integer,
  p_page_size             integer          DEFAULT 5000
)
  RETURNS TABLE (
    id                integer,
    spectrum_id       text,
    target_id         text,
    grating           text,
    field             text,
    observation       text,
    ra                double precision,
    "dec"             double precision,
    redshift          numeric,
    redshift_quality  integer,
    redshift_auto     double precision,
    signal_to_noise   double precision,
    exposure_time     double precision,
    fits_path         text,
    program_slug      text,
    program_name      text,
    last_inspected_at timestamp with time zone,
    last_inspected_by text,
    distance          double precision,
    dq_flags          integer,
    lists             text,
    line_snr          double precision,
    band_mag          double precision,
    band_snr          double precision,
    line_columns      jsonb,
    band_columns      jsonb,
    filter            text
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);
  -- Emission-line filter: the spectra with a measurement of p_line in the S/N
  -- bounds, materialized once per call (see spectra_matching_line_filter).
  IF p_line IS NOT NULL THEN
    v_line_spectrum_ids := ARRAY(SELECT public.spectra_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false)));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.id, s.spectrum_id, t.target_id, s.grating, s.filter, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      vl.lists,
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE WHEN jsonb_typeof(p_line_columns) = 'object' THEN
        (SELECT jsonb_object_agg(__l.line, jsonb_build_object('snr', __l.snr, 'flux', __l.flux))
         FROM jsonb_each(p_line_columns) __spec(line, w)
         JOIN public.cat_lines __l ON __l.spectrum_id = s.id AND __l.line = __spec.line
         -- the stale gate the object-level columns apply (the fit's stored flag)
         JOIN public.cat_line_fits __f ON __f.spectrum_id = s.id
         WHERE (COALESCE(p_line_include_stale, false) OR NOT __f.stale_redshift)
           AND ((__spec.w ->> 'snr_min') IS NULL OR __l.snr >= (__spec.w ->> 'snr_min')::double precision)
           AND ((__spec.w ->> 'snr_max') IS NULL OR __l.snr <= (__spec.w ->> 'snr_max')::double precision))
      END AS line_columns,
      bc.band_columns
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
      AND (p_spectrum_ids IS NULL OR s.id = ANY(p_spectrum_ids))
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min) AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR t.object_id = ANY(v_band_object_ids))
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.cat_spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (NOT v_comment_search_active OR t.id IN (
        SELECT c.target_id FROM comments c WHERE c.target_id IS NOT NULL AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.id, df.spectrum_id, df.target_id, df.grating, df.field, df.observation,
    df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists, df.line_snr, df.band_mag, df.band_snr,
    df.line_columns, df.band_columns, df.filter
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.set_spectrum_filter()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SET search_path TO ''
  AS $function$
DECLARE
    v_parsed text;
BEGIN
    IF NEW.filter IS NULL THEN
        -- the file name, else the one filter the grating can take (PRISM
        -- only with CLEAR, G235M/H only with F170LP, G395M/H only with
        -- F290LP); G140M/H take F070LP or F100LP, so they stay NULL and the
        -- write fails NOT NULL rather than guess
        NEW.filter := COALESCE(
            public.spectrum_filter_from_path(NEW.fits_path),
            CASE upper(NEW.grating)
                WHEN 'PRISM' THEN 'CLEAR'
                WHEN 'G235M' THEN 'F170LP' WHEN 'G235H' THEN 'F170LP'
                WHEN 'G395M' THEN 'F290LP' WHEN 'G395H' THEN 'F290LP'
            END);
    ELSIF TG_OP = 'UPDATE'
          AND NEW.fits_path IS DISTINCT FROM OLD.fits_path
          AND NEW.filter IS NOT DISTINCT FROM OLD.filter THEN
        v_parsed := public.spectrum_filter_from_path(NEW.fits_path);
        IF v_parsed IS NOT NULL THEN
            NEW.filter := v_parsed;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE INDEX idx_dr_spectra_tag_target ON public.dr_spectra USING btree (TAG, target_id, grating, filter);

CREATE UNIQUE INDEX idx_spectra_target_grating_filter ON public.spectra USING btree (target_id, grating, filter);

COMMENT ON COLUMN "public"."spectra"."filter" IS 'NIRSpec filter paired with grating (CLEAR, F070LP, F100LP, F170LP, F290LP; #683); (target_id, grating, filter) is the spectrum''s unique key. Written by deploy; filled from fits_path by set_spectrum_filter when NULL (spectrum_filter_from_path).';

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "service_role";
