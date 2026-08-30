-- Issue #412: catalog CSV export timeout.
--
-- The two CSV-export RPCs were offset-paged from the web client via PostgREST
-- .range(), which applies LIMIT/OFFSET OUTSIDE a set-returning function — every
-- page re-materialized and re-sorted the ENTIRE filtered result set, so a 16k-row
-- export cost pages x O(N log N) and hit the authenticated role's 8s
-- statement_timeout (neither function carried the 120s override the other heavy
-- RPCs use).
--
-- Fix: keyset pagination. Both functions take an explicit cursor
-- (get_csv_export_spectra: p_after_id on the spectra.id PK, since spectrum_id is
-- not uniquely constrained; get_csv_export_objects: p_after_object_id on the
-- UNIQUE objects.object_id) plus p_page_size, applied INSIDE the query, so each
-- page is one index-bounded scan and total export work stays O(N). The
-- p_sort_column/p_sort_direction params are dropped — rows return in cursor-key
-- order and the web action re-sorts in JS for cosmetic CSV ordering.
-- get_csv_export_spectra's RETURNS gains id + observation. Both functions also
-- get SET statement_timeout = '120s' to match the repo's other heavy RPCs.
--
-- Signature + RETURNS changes require DROP first; definitions below are verbatim
-- from supabase/schemas/functions.sql.

-- =============================================================================
-- get_csv_export_spectra
-- =============================================================================

-- Issue #412: keyset pagination. PostgREST applies .range() LIMIT/OFFSET
-- OUTSIDE a set-returning function, so the old offset-paged export fully
-- materialized and sorted the WHOLE filtered result set on every page —
-- pages × O(N log N) — and blew through the authenticated role's 8s
-- statement_timeout at ~16k rows. The caller now passes p_after_id (spectra.id
-- PK cursor; spectrum_id is not uniquely constrained) and p_page_size; the
-- LIMIT lives inside the query, each page is one index-bounded scan in id
-- order, and total work across an export stays O(N). Rows come back in
-- spectra.id order — the web action re-sorts in JS for cosmetic CSV ordering,
-- so p_sort_column/p_sort_direction are gone. RETURNS gains id + observation;
-- signature/RETURNS changes require DROP first.
DROP FUNCTION IF EXISTS public.get_csv_export_spectra(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, BOOLEAN
);

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any', p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL, p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_after_id INTEGER DEFAULT NULL, p_page_size INTEGER DEFAULT 5000
)
RETURNS TABLE(
  id INTEGER, spectrum_id TEXT, target_id TEXT, grating TEXT, field TEXT, observation TEXT,
  ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER, redshift_auto DOUBLE PRECISION,
  signal_to_noise DOUBLE PRECISION,
  exposure_time DOUBLE PRECISION, fits_path TEXT, program_slug TEXT, program_name TEXT,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT, distance DOUBLE PRECISION,
  dq_flags INTEGER,
  lists TEXT
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
SET statement_timeout = '120s'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);
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
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.id, s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      vl.lists
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
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
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR t.object_id IN (
          SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0))
      AND (p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at))))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (NOT v_comment_search_active OR t.id IN (
        SELECT c.target_id FROM comments c WHERE c.target_id IS NOT NULL AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.id, df.spectrum_id, df.target_id, df.grating, df.field, df.observation,
    df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_spectra TO authenticated;


-- =============================================================================
-- get_csv_export_objects
-- (one row per sky-object for CSV download in objects view mode)
-- =============================================================================

-- Issue #412: keyset pagination — same rationale as get_csv_export_spectra
-- above. Cursor is objects.object_id (UNIQUE, objects_object_id_key); the
-- LIMIT lives inside the query so each page is one index-bounded scan and an
-- export's total work stays O(N). Sort params removed (the web action
-- re-sorts in JS). Signature change requires DROP first.
DROP FUNCTION IF EXISTS public.get_csv_export_objects(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, BOOLEAN, BOOLEAN, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  BOOLEAN, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, UUID, TEXT, TEXT, BOOLEAN
);

CREATE OR REPLACE FUNCTION public.get_csv_export_objects(
  p_program_slugs TEXT[], p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL, p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL, p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL, p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL, p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_search TEXT DEFAULT NULL, p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL, p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL, p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL, p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_after_object_id TEXT DEFAULT NULL, p_page_size INTEGER DEFAULT 5000
)
RETURNS TABLE(
  object_id TEXT, field TEXT, ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER,
  redshift_inspected NUMERIC, redshift_auto DOUBLE PRECISION,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT,
  last_data_change_at TIMESTAMPTZ, staleness_reason TEXT, version INTEGER,
  n_targets INTEGER, n_spectra INTEGER,
  programs TEXT, gratings TEXT,
  max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION,
  member_target_ids TEXT, distance DOUBLE PRECISION,
  lists TEXT,
  has_photometry BOOLEAN, photo_z DOUBLE PRECISION,
  photo_z_err_lo DOUBLE PRECISION, photo_z_err_hi DOUBLE PRECISION,
  photometry JSONB
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
SET statement_timeout = '120s'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM targets t
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
    GROUP BY olm.object_id
  ),
  filtered_objects AS (
    SELECT o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, up.full_name AS last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.version,
      -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
      -- objects don't export proprietary member metadata. See
      -- object_scoped_aggregates().
      sa.n_targets, sa.n_spectra,
      array_to_string(sa.programs, ';') AS programs,
      array_to_string(sa.gratings, ';') AS gratings,
      sa.max_snr, sa.max_exposure_time,
      mt.member_target_ids,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      vl.lists,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
      phot.photometry
    FROM objects o
    LEFT JOIN member_targets mt ON mt.object_id = o.id
    LEFT JOIN visible_lists vl ON vl.object_id = o.id
    LEFT JOIN user_profiles up ON up.user_id = o.last_inspected_by
    LEFT JOIN LATERAL public.object_scoped_aggregates(o.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
    LEFT JOIN LATERAL (
      SELECT op.photometry FROM object_photometry op
      WHERE op.object_id = o.id ORDER BY op.updated_at DESC LIMIT 1
    ) phot ON true
    WHERE o.programs && v_filtered_program_slugs
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at))))
      AND (NOT v_coord_search_active OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  distance_filtered AS (SELECT fo.* FROM filtered_objects fo WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees)
  SELECT df.object_id, df.field, df.ra, df.dec,
    df.redshift, df.redshift_quality,
    df.redshift_inspected, df.redshift_auto,
    df.last_inspected_at, df.last_inspected_by,
    df.last_data_change_at, df.staleness_reason, df.version,
    df.n_targets, df.n_spectra,
    df.programs, df.gratings,
    df.max_snr, df.max_exposure_time,
    df.member_target_ids, df.distance, df.lists,
    df.has_photometry, df.photo_z, df.photo_z_err_lo, df.photo_z_err_hi,
    df.photometry
  FROM distance_filtered df
  ORDER BY df.object_id ASC
  LIMIT v_page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_objects TO authenticated;
