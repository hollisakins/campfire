-- =============================================================================
-- CAMPFIRE Supabase Schema: Functions
-- =============================================================================
-- Canonical source of truth for all RPC and helper functions.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- =============================================================================
-- RLS Helper Functions
-- =============================================================================

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_admin FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated;

CREATE OR REPLACE FUNCTION public.can_comment()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.can_comment() TO authenticated;

CREATE OR REPLACE FUNCTION public.is_group_account()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_group_account FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_group_account() TO authenticated;

CREATE OR REPLACE FUNCTION public.accessible_program_slugs()
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(array_agg(DISTINCT slug), '{}')
  FROM (
    SELECT program_slug AS slug
    FROM user_program_access
    WHERE user_id = auth.uid()
    UNION
    SELECT slug
    FROM programs
    WHERE is_public = true
  ) sub;
$$;

GRANT EXECUTE ON FUNCTION public.accessible_program_slugs() TO authenticated;


-- =============================================================================
-- Device Code Auth
-- =============================================================================

CREATE OR REPLACE FUNCTION public.authorize_device_code(p_user_code text, p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET
    status = 'authorized',
    user_id = p_user_id,
    authorized_at = NOW()
  WHERE
    user_code = p_user_code
    AND status = 'pending'
    AND expires_at > NOW();

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO anon;
GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO authenticated;
GRANT ALL ON FUNCTION public.authorize_device_code(text, uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.check_device_code_status(p_device_code text)
RETURNS TABLE(status text, user_id uuid, is_expired boolean)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    dc.status,
    dc.user_id,
    (dc.expires_at < NOW())::BOOLEAN AS is_expired
  FROM device_codes dc
  WHERE dc.device_code = p_device_code;
END;
$$;

GRANT ALL ON FUNCTION public.check_device_code_status(text) TO anon;
GRANT ALL ON FUNCTION public.check_device_code_status(text) TO authenticated;
GRANT ALL ON FUNCTION public.check_device_code_status(text) TO service_role;




-- =============================================================================
-- get_objects_for_sync
-- (lightweight bulk fetch for Python client objects catalog sync)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- OFFSET-based pagination is linear in offset: a deep-page request
-- (e.g. OFFSET 29000 on a 30k-row catalog) must materialize the ordered
-- scan up to that point plus run three aggregate CTEs, and started
-- tipping past the default service_role timeout around page ~29 of a
-- --full sync. Bumped to 120s so deep pages finish while the paginator
-- is still offset-based; a future change should switch this RPC to
-- keyset pagination (WHERE object_id > cursor) and then drop this SET.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- matched is MATERIALIZED so the three aggregate CTEs below each see the
  -- same ~p_limit-row set without re-evaluating the WHERE/ORDER/LIMIT.
  WITH matched AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.redshift, o.redshift_quality,
           o.redshift_inspected, o.redshift_auto,
           o.inspected_used_auto,
           o.last_inspected_at, o.last_inspected_by,
           o.last_data_change_at, o.staleness_reason,
           o.version, o.is_active,
           o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      -- Phase D: hide soft-deleted objects from sync. Reactivation rewrites
      -- updated_at, so re-activated rows get re-synced naturally on next pull.
      AND o.is_active = true
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
    ORDER BY o.object_id
    LIMIT p_limit OFFSET p_offset
  ),
  member_targets_agg AS (
    SELECT t.object_id,
           jsonb_agg(t.target_id ORDER BY t.target_id) AS target_ids
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
    GROUP BY t.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT t.object_id,
           jsonb_agg(jsonb_build_object(
             'id', s.id,
             'target_id', s.target_id,
             'grating', s.grating,
             'signal_to_noise', s.signal_to_noise,
             'exposure_time', s.exposure_time,
             'redshift_auto', s.redshift_auto,
             'dq_flags', s.dq_flags
           ) ORDER BY s.target_id, s.grating) AS spectra
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
    GROUP BY t.object_id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit'))
    GROUP BY olm.object_id
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        'n_targets', m.n_targets,
        'n_spectra', m.n_spectra,
        'programs', m.programs,
        'gratings', m.gratings,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
        'inspected_used_auto', m.inspected_used_auto,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'last_data_change_at', m.last_data_change_at,
        'staleness_reason', m.staleness_reason,
        'version', m.version,
        'is_active', m.is_active,
        'has_photometry', m.has_photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(mt.target_ids, '[]'::jsonb),
        'spectra',           COALESCE(sp.spectra,    '[]'::jsonb),
        'lists',             COALESCE(la.list_slugs, '[]'::jsonb)
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN) TO service_role;


-- =============================================================================
-- get_spectra_for_sync
-- (bulk fetch of per-spectrum download-relevant metadata for the Python
-- client; complements get_objects_for_sync which carries display-level
-- spectra fields only)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE
)
RETURNS TABLE(spectra JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Mirrors get_objects_for_sync: offset-based pagination is linear in
-- offset, so deep --full-sync pages can tip past the default timeout.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time, s.reduction_version,
           s.redshift_auto, s.dq_flags,
           t.program_slug, t.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
    ORDER BY s.spectrum_id
    LIMIT p_limit OFFSET p_offset
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'spectrum_id', m.spectrum_id,
        'target_id', m.target_id,
        'object_id', m.object_id,
        'grating', m.grating,
        'fits_path', m.fits_path,
        'file_hash', m.file_hash,
        'file_size', m.file_size,
        'signal_to_noise', m.signal_to_noise,
        'exposure_time', m.exposure_time,
        'reduction_version', m.reduction_version,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN) TO service_role;


-- =============================================================================
-- get_photometry_for_sync
-- (bulk fetch for Python client photometry sync)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(photometry_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Mirrors get_objects_for_sync: offset-based pagination is linear in
-- offset, so deep --full-sync pages can tip past the default timeout.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT op.id, o.object_id, op.field, op.catalog_name, op.catalog_id,
           op.match_distance_arcsec, op.photometry, op.photo_z,
           op.photo_z_err_lo, op.photo_z_err_hi, op.has_pz,
           op.created_at, op.updated_at
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
    ORDER BY op.id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'catalog_name', m.catalog_name,
        'catalog_id', m.catalog_id,
        'match_distance_arcsec', m.match_distance_arcsec,
        'photometry', m.photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'has_pz', m.has_pz,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER) TO service_role;


-- =============================================================================
-- get_lists_for_sync
-- (returns all list metadata for Python client sync)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_lists_for_sync(
  p_user_id UUID DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql STABLE
AS $$
BEGIN
  RETURN COALESCE(
    (SELECT jsonb_agg(jsonb_build_object(
      'id', ol.id,
      'slug', ol.slug,
      'name', ol.name,
      'description', ol.description,
      'visibility', ol.visibility,
      'is_system', ol.is_system,
      'created_by', ol.created_by,
      'created_at', ol.created_at,
      'updated_at', ol.updated_at,
      'member_count', (SELECT COUNT(*) FROM object_list_members olm WHERE olm.list_id = ol.id)
    ) ORDER BY ol.is_system DESC, ol.name)
    FROM object_lists ol
    WHERE ol.created_by = p_user_id
       OR ol.visibility IN ('public_read', 'public_edit')),
    '[]'::jsonb
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID) TO service_role;


-- =============================================================================
-- get_filtered_spectra_paginated
-- (final version: per-spectrum S/N and exposure_time filtering)
-- =============================================================================

-- Phase D: spectra rows now read inspection state through their parent object
-- (targets are stateless provenance). The redshift_quality / redshift filters
-- query objects.redshift_quality / objects.redshift via the targets→objects
-- FK; DQ filters operate on the per-spectrum spectra.dq_flags. The
-- spectral_features parameters are gone (Phase E drop). Parameter list
-- shrunk, so drop the old signature first.
DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, INTEGER, INTEGER, BOOLEAN
);

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_offset INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'spectrum_id';
  END IF;

  IF v_coord_search_active AND p_sort_column IN ('target_id', 'spectrum_id') AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

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

  -- Single-pass CTE: filtered_spectra is referenced by both distance_filtered
  -- and the count subquery, so PostgreSQL materializes it once.
  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      -- Phase D: redshift / redshift_quality / inspected flags now live on the
      -- parent object. LEFT JOIN so spectra whose target has no object FK
      -- (shouldn't happen post-reconcile, but safe) still appear.
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.last_inspected_at,
      o.last_inspected_by,
      o.is_active AS object_is_active,
      o.has_photometry AS object_has_photometry,
      o.object_id AS parent_object_id,
      t.max_snr,
      t.max_exposure_time,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_pk,
      s.spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.redshift_auto,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      s.file_hash,
      s.file_size,
      s.thumbnail_svg_fnu,
      s.thumbnail_svg_flambda,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      -- Hide spectra whose parent object was soft-deleted.
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR t.object_id IN (
          SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_search IS NULL
           OR t.target_id ILIKE '%' || p_search || '%'
           OR s.spectrum_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0)
      )
      AND (
        p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.target_id = t.id
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active
        OR (
          t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM distance_filtered
      ORDER BY
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN target_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN target_id END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'spectrum_id' AND p_sort_direction = 'asc' THEN spectrum_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'spectrum_id' AND p_sort_direction = 'desc' THEN spectrum_id END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN field END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN field END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN observation END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN observation END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'program_slug' AND p_sort_direction = 'asc' THEN program_slug END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'program_slug' AND p_sort_direction = 'desc' THEN program_slug END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN ra END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN ra END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN "dec" END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN "dec" END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN redshift END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN redshift END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN redshift_quality END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN redshift_quality END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_auto' AND p_sort_direction = 'asc' THEN redshift_auto END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_auto' AND p_sort_direction = 'desc' THEN redshift_auto END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN signal_to_noise END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN signal_to_noise END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN exposure_time END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN exposure_time END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN grating END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN grating END DESC NULLS LAST,
        target_id ASC, grating ASC
      LIMIT p_page_size OFFSET v_offset
    ) sorted_page
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      -- Phase D: redshift fields are object-level reads
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        -- Phase D: per-spectrum auto-z and DQ
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_fnu ELSE NULL END,
        'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_flambda ELSE NULL END
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    (SELECT COUNT(*) FROM distance_filtered),
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;


-- =============================================================================
-- get_filtered_objects_paginated
-- (one row per unique sky position, cross-matched across programs)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50
)
RETURNS TABLE(targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
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
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
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
    AND o.is_active = true
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
      OR (p_needs_review = TRUE
          AND o.staleness_reason IS NOT NULL
          AND o.last_inspected_at IS NOT NULL
          AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
      OR (p_needs_review = FALSE
          AND (o.staleness_reason IS NULL
               OR o.last_inspected_at IS NULL
               OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
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
    AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
        SELECT olm.object_id FROM object_list_members olm
        WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
    ))
    AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
    AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
    AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max);

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
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.redshift_auto,
      o.inspected_used_auto,
      o.last_inspected_at,
      o.last_inspected_by,
      o.last_data_change_at,
      o.staleness_reason,
      o.version,
      o.is_active,
      o.photo_z,
      o.has_photometry,
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
      AND o.is_active = true
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
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
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
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
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
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
        'redshift', fo.redshift,
        'redshift_quality', fo.redshift_quality,
        'redshift_inspected', fo.redshift_inspected,
        'redshift_auto', fo.redshift_auto,
        'inspected_used_auto', fo.inspected_used_auto,
        'last_inspected_at', fo.last_inspected_at,
        'last_inspected_by', fo.last_inspected_by,
        'last_data_change_at', fo.last_data_change_at,
        'staleness_reason', fo.staleness_reason,
        'version', fo.version,
        'is_active', fo.is_active,
        'photo_z', fo.photo_z,
        'has_photometry', fo.has_photometry,
        'created_at', fo.created_at,
        'distance', fo.distance,
        -- Phase D: member_targets becomes provenance only (target_id, program,
        -- observation). Inspection state lives on the object now; redshift_auto
        -- on targets is retained for transitional UI display until Phase E.
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift_auto', t.redshift_auto
            )
          )
          FROM targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(v_filtered_program_slugs)
          ),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', ol.id,
              'name', ol.name,
              'slug', ol.slug,
              'icon', ol.icon,
              'color', ol.color
            ) ORDER BY ol.name
          )
          FROM object_list_members olm
          JOIN object_lists ol ON ol.id = olm.list_id
          WHERE olm.object_id = fo.id),
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
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO service_role;


-- =============================================================================
-- get_filtered_object_ids
-- (lightweight: returns only object_id strings for map marker filtering)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(object_id TEXT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

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
    RETURN;
  END IF;

  RETURN QUERY
  SELECT o.object_id
  FROM objects o
  WHERE
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
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
    AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
      OR (p_needs_review = TRUE
          AND o.staleness_reason IS NOT NULL
          AND o.last_inspected_at IS NOT NULL
          AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
      OR (p_needs_review = FALSE
          AND (o.staleness_reason IS NULL
               OR o.last_inspected_at IS NULL
               OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
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
    AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
        SELECT olm.object_id FROM object_list_members olm
        WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
    ))
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
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
    o.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO service_role;



-- =============================================================================
-- get_adjacent_objects
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_adjacent_objects(
  p_current_object_id TEXT,
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_needs_review BOOLEAN DEFAULT NULL,
  p_list_ids INTEGER[] DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL
)
RETURNS TABLE(prev_object_id TEXT, next_object_id TEXT, current_index BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_sort_is_text BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
    p_sort_direction := 'asc';
  END IF;
  v_sort_is_text := p_sort_column IN ('object_id', 'field');

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs))
    INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_objects AS MATERIALIZED (
    SELECT
      o.object_id,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        )))
      ELSE NULL END AS distance,
      o.field, o.ra, o.dec, o.redshift, o.redshift_quality,
      o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
        OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
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
  ),
  distance_filtered AS MATERIALIZED (
    SELECT
      fo.*,
      CASE p_sort_column
        WHEN 'object_id' THEN fo.object_id WHEN 'field' THEN fo.field ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN fo.ra WHEN 'dec' THEN fo.dec
        WHEN 'redshift' THEN fo.redshift
        WHEN 'redshift_quality' THEN fo.redshift_quality::DOUBLE PRECISION
        WHEN 'n_targets' THEN fo.n_targets::DOUBLE PRECISION
        WHEN 'n_spectra' THEN fo.n_spectra::DOUBLE PRECISION
        WHEN 'max_snr' THEN fo.max_snr WHEN 'max_exposure_time' THEN fo.max_exposure_time
        WHEN 'distance' THEN fo.distance ELSE NULL
      END AS sort_num
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  current_obj AS (
    SELECT df.sort_text, df.sort_num, df.object_id FROM distance_filtered df WHERE df.object_id = p_current_object_id
  )
  SELECT
    (SELECT df.object_id FROM distance_filtered df, current_obj c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
       OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
       OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END DESC NULLS FIRST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END ASC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END DESC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END ASC NULLS FIRST,
       df.object_id DESC
     LIMIT 1
    ) AS prev_object_id,
    (SELECT df.object_id FROM distance_filtered df, current_obj c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text ELSE df.sort_text < c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id > c.object_id)
       OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num ELSE df.sort_num < c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id > c.object_id)
       OR (c.sort_num IS NOT NULL AND df.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END ASC NULLS LAST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END DESC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END ASC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END DESC NULLS LAST,
       df.object_id ASC
     LIMIT 1
    ) AS next_object_id,
    CASE WHEN EXISTS (SELECT 1 FROM current_obj) THEN (
      SELECT COUNT(*) + 1
      FROM distance_filtered df, current_obj c
      WHERE CASE WHEN v_sort_is_text THEN
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
        OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
        OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
      ELSE
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
        OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
        OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
      END
    )::BIGINT ELSE 0::BIGINT END AS current_index,
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO service_role;



-- =============================================================================
-- get_csv_export_spectra
-- =============================================================================

-- Phase D: dropped spectral_features filtering (deprecated). redshift_quality
-- and redshift now read from the parent object via the targets→objects FK.
-- redshift_auto + dq_flags are per-spectrum (from spectra). Signature change
-- requires DROP first; CREATE OR REPLACE alone can't widen the RETURNS row.
DROP FUNCTION IF EXISTS public.get_csv_export_spectra(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT
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
  p_sort_column TEXT DEFAULT 'target_id', p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  spectrum_id TEXT, target_id TEXT, grating TEXT, field TEXT, ra DOUBLE PRECISION, "dec" DOUBLE PRECISION,
  redshift NUMERIC, redshift_quality INTEGER, redshift_auto DOUBLE PRECISION,
  signal_to_noise DOUBLE PRECISION,
  exposure_time DOUBLE PRECISION, fits_path TEXT, program_slug TEXT, program_name TEXT,
  last_inspected_at TIMESTAMPTZ, last_inspected_by TEXT, distance DOUBLE PRECISION,
  dq_flags INTEGER,
  lists TEXT
)
LANGUAGE plpgsql STABLE SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'spectrum_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'spectrum_id';
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
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
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
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
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
      AND (p_search IS NULL
           OR t.target_id ILIKE '%' || p_search || '%'
           OR s.spectrum_id ILIKE '%' || p_search || '%')
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
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.spectrum_id, df.target_id, df.grating, df.field, df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'spectrum_id' AND p_sort_direction = 'asc' THEN df.spectrum_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'spectrum_id' AND p_sort_direction = 'desc' THEN df.spectrum_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_auto' AND p_sort_direction = 'asc' THEN df.redshift_auto END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_auto' AND p_sort_direction = 'desc' THEN df.redshift_auto END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN df.signal_to_noise END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN df.signal_to_noise END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN df.exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN df.exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN df.grating END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN df.grating END DESC NULLS LAST,
    df.target_id ASC, df.grating ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_spectra TO authenticated;


-- =============================================================================
-- get_csv_export_objects
-- (one row per sky-object for CSV download in objects view mode)
-- =============================================================================

-- Phase D: RETURNS columns expanded with per-object inspection fields.
-- CREATE OR REPLACE can't widen the row, so drop first.
DROP FUNCTION IF EXISTS public.get_csv_export_objects(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, BOOLEAN, INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  BOOLEAN, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT
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
  p_sort_column TEXT DEFAULT 'object_id', p_sort_direction TEXT DEFAULT 'asc'
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
LANGUAGE plpgsql STABLE SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

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
      o.n_targets, o.n_spectra,
      array_to_string(o.programs, ';') AS programs,
      array_to_string(o.gratings, ';') AS gratings,
      o.max_snr, o.max_exposure_time,
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
    LEFT JOIN LATERAL (
      SELECT op.photometry FROM object_photometry op
      WHERE op.object_id = o.id ORDER BY op.updated_at DESC LIMIT 1
    ) phot ON true
    WHERE o.programs && v_filtered_program_slugs
      AND o.is_active = true
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
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
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
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN df.n_targets END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN df.n_targets END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN df.n_spectra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN df.n_spectra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN df.photo_z END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN df.photo_z END DESC NULLS LAST,
    df.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_objects TO authenticated;


-- =============================================================================
-- get_programs_overview (reads from mv_programs_overview)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_programs_overview();

CREATE OR REPLACE FUNCTION public.get_programs_overview()
RETURNS TABLE(
  slug text, program_name text, pi_name text, description text,
  is_public boolean, cycle integer, target_count bigint,
  gratings text[], fields text[], observations text[], jwst_pids integer[],
  n_observations bigint, last_reduced_at timestamptz
) LANGUAGE sql STABLE AS $$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids,
    mv.n_observations, mv.last_reduced_at
  FROM public.mv_programs_overview mv ORDER BY mv.program_name;
$$;

GRANT EXECUTE ON FUNCTION public.get_programs_overview TO authenticated;


-- =============================================================================
-- refresh_programs_overview
-- =============================================================================

CREATE OR REPLACE FUNCTION public.refresh_programs_overview()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_programs_overview;
END;
$$;

GRANT EXECUTE ON FUNCTION public.refresh_programs_overview TO authenticated;


-- =============================================================================
-- get_observation_stats
-- =============================================================================

-- Aggregate stats first, then LEFT JOIN observations once for the JSONB
-- payload. Keeps the GROUP BY key as cheap text/uuid columns so adding more
-- per-observation metadata (additional JSONB or array columns) doesn't drag
-- through the targets x spectra cross product. Provenance fields come from
-- the most recent FULL deployment (source_ids_filter IS NULL); patch deployments
-- contribute only to n_patches_since_full so per-source re-reductions don't
-- masquerade as observation-level reductions.
DROP FUNCTION IF EXISTS public.get_observation_stats(text[]);

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  pointings jsonb,
  reduction_version text, crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
) LANGUAGE sql STABLE AS $$
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = s.observation AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_stats TO authenticated;


-- =============================================================================
-- get_observations_overview
-- =============================================================================
-- Flat list of observations (scoped to the caller's accessible programs) with
-- provenance + patch counts. Powers the /nirspec/metadata page Observations
-- tab. Caller passes the accessible program slug list (public + explicit
-- access), matching the get_observation_stats pattern; filtering happens in
-- SQL so the targets/spectra aggregate doesn't scan inaccessible rows.
--
-- Gratings are derived from the spectra table (the actual deployed data),
-- with observations.gratings as a fallback when no spectra exist yet — the
-- observations.gratings column is populated from observations.toml at deploy
-- time and is empty for observations that haven't gone through that path.
--
-- deployed_by_username / deployed_by_full_name come from user_profiles via
-- the latest full deployment so the metadata page can show who reduced each
-- observation without an extra client-side join.
DROP FUNCTION IF EXISTS public.get_observations_overview();
DROP FUNCTION IF EXISTS public.get_observations_overview(text[]);

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[])
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  cycle integer, gratings text[], pointing_count integer, pointings jsonb,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  reduction_version text, crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
) LANGUAGE sql STABLE AS $$
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug
  )
  SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    CASE
      WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
      ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(p_program_slugs)
  ORDER BY o.program_slug, o.name;
$$;

GRANT EXECUTE ON FUNCTION public.get_observations_overview TO authenticated;


-- =============================================================================
-- get_database_overview
-- =============================================================================
-- Single-row scope summary for the metadata page header.
CREATE OR REPLACE FUNCTION public.get_database_overview()
RETURNS TABLE(
  n_programs bigint, n_observations bigint, n_pointings bigint,
  n_targets bigint, n_spectra bigint, total_size_bytes bigint,
  latest_deployed_at timestamptz, latest_reduction_version text
) LANGUAGE sql STABLE AS $$
  WITH latest AS (
    SELECT d.deployed_at, d.reduction_version
    FROM public.deployments d
    WHERE d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  )
  SELECT
    (SELECT COUNT(*)::bigint FROM public.programs) AS n_programs,
    (SELECT COUNT(*)::bigint FROM public.observations) AS n_observations,
    (SELECT COALESCE(SUM(jsonb_array_length(pointings)), 0)::bigint
       FROM public.observations
       WHERE pointings IS NOT NULL) AS n_pointings,
    (SELECT COUNT(*)::bigint FROM public.targets) AS n_targets,
    (SELECT COUNT(*)::bigint FROM public.spectra) AS n_spectra,
    (SELECT COALESCE(SUM(file_size), 0)::bigint FROM public.spectra) AS total_size_bytes,
    (SELECT deployed_at FROM latest) AS latest_deployed_at,
    (SELECT reduction_version FROM latest) AS latest_reduction_version;
$$;

GRANT EXECUTE ON FUNCTION public.get_database_overview TO authenticated;


-- =============================================================================
-- get_observation_manifest
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_observation_manifest(TEXT, TEXT[]);

CREATE OR REPLACE FUNCTION public.get_observation_manifest(p_obs_name text, p_program_slugs text[])
RETURNS TABLE(
  spectra_id integer, spectrum_id text, target_id text, grating text, fits_path text,
  file_hash text, file_size bigint, signal_to_noise double precision, reduction_version text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT s.id, s.spectrum_id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.reduction_version
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
  ORDER BY s.spectrum_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_manifest TO authenticated;


-- =============================================================================
-- get_targets_in_viewport
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_targets_in_viewport(
  p_ra_min double precision, p_ra_max double precision,
  p_dec_min double precision, p_dec_max double precision,
  p_field text DEFAULT NULL, p_limit integer DEFAULT 5000
)
RETURNS TABLE(
  "target_id" text, "ra" double precision, "dec" double precision,
  "redshift" double precision, "redshift_quality" integer, "field" text, "program_slug" text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
  RETURN QUERY
  SELECT t.target_id, t.ra, t.dec, t.redshift::double precision, t.redshift_quality, t.field, t.program_slug
  FROM public.targets t
  WHERE t.ra BETWEEN p_ra_min AND p_ra_max AND t.dec BETWEEN p_dec_min AND p_dec_max
    AND (p_field IS NULL OR t.field = p_field)
  ORDER BY t.ra LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_targets_in_viewport TO authenticated;


-- =============================================================================
-- get_nearby_shutters
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_nearby_shutters(
  p_ra double precision,
  p_dec double precision,
  p_radius_arcsec double precision DEFAULT 5.0,
  p_field text DEFAULT NULL
)
RETURNS TABLE (
  object_id text,
  source_id integer,
  center_ra double precision,
  center_dec double precision,
  position_angle double precision,
  shutter_idx smallint,
  dither_id smallint,
  shutter_state text,
  observation text
)
LANGUAGE sql STABLE AS $$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation
  FROM shutters s
  WHERE (p_field IS NULL OR s.field = p_field)
    AND s.center_ra BETWEEN p_ra - p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
                        AND p_ra + p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
    AND s.center_dec BETWEEN p_dec - p_radius_arcsec / 3600.0
                         AND p_dec + p_radius_arcsec / 3600.0;
$$;


-- =============================================================================
-- get_field_object_markers
-- =============================================================================
-- Single-shot fetch of every object in a field for the map viewer. Replaces
-- the paginated PostgREST select that capped at 1000 rows/page and embedded
-- targets(target_id) for the slit-filter bridge — both very expensive on
-- COSMOS-sized fields. RLS on objects still applies (SECURITY INVOKER).

CREATE OR REPLACE FUNCTION public.get_field_object_markers(p_field TEXT)
RETURNS TABLE (
  object_id           TEXT,
  ra                  DOUBLE PRECISION,
  "dec"               DOUBLE PRECISION,
  redshift            DOUBLE PRECISION,
  redshift_quality    INTEGER,
  field               TEXT,
  n_targets           INTEGER,
  n_spectra           INTEGER,
  programs            TEXT[],
  member_target_ids   TEXT[]
)
LANGUAGE sql STABLE
AS $$
  SELECT
    o.object_id,
    o.ra,
    o.dec,
    o.redshift::double precision,
    o.redshift_quality,
    o.field,
    o.n_targets,
    o.n_spectra,
    o.programs,
    COALESCE(
      (SELECT array_agg(t.target_id ORDER BY t.target_id)
         FROM public.targets t
        WHERE t.object_id = o.id),
      ARRAY[]::TEXT[]
    ) AS member_target_ids
  FROM public.objects o
  WHERE o.field = p_field
    AND o.is_active
  ORDER BY o.object_id;
$$;

GRANT EXECUTE ON FUNCTION public.get_field_object_markers TO authenticated;


-- =============================================================================
-- get_field_shutters
-- =============================================================================
-- Single-shot fetch of every shutter in a field for the map viewer. Shutters
-- are public to authenticated users, so SECURITY INVOKER is fine.

CREATE OR REPLACE FUNCTION public.get_field_shutters(p_field TEXT)
RETURNS TABLE (
  object_id        TEXT,
  source_id        INTEGER,
  center_ra        DOUBLE PRECISION,
  center_dec       DOUBLE PRECISION,
  position_angle   DOUBLE PRECISION,
  shutter_idx      SMALLINT,
  dither_id        SMALLINT,
  shutter_state    TEXT,
  observation      TEXT
)
LANGUAGE sql STABLE
AS $$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation
  FROM public.shutters s
  WHERE s.field = p_field
  ORDER BY s.object_id;
$$;

GRANT EXECUTE ON FUNCTION public.get_field_shutters TO authenticated;


-- =============================================================================
-- increment_tile_version
-- =============================================================================

CREATE OR REPLACE FUNCTION public.increment_tile_version(
    p_field text,
    p_filter text
)
RETURNS void
LANGUAGE sql
AS $$
    UPDATE public.map_layers
    SET tile_version = tile_version + 1
    WHERE field = p_field AND filter = p_filter;
$$;

GRANT EXECUTE ON FUNCTION public.increment_tile_version TO service_role;



-- =============================================================================
-- get_program_stats
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_program_stats()
RETURNS TABLE(slug text, target_count bigint, user_access_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT p.slug,
    COALESCE(tc.cnt, 0) AS target_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM targets GROUP BY program_slug) tc ON p.slug = tc.program_slug
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM user_program_access GROUP BY program_slug) a ON p.slug = a.program_slug;
$$;

GRANT ALL ON FUNCTION public.get_program_stats TO anon;
GRANT ALL ON FUNCTION public.get_program_stats TO authenticated;
GRANT ALL ON FUNCTION public.get_program_stats TO service_role;


-- =============================================================================
-- get_user_profile_stats
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_user_profile_stats(p_user_id uuid)
RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  objects_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  -- Phase D: count distinct *objects* (not targets) — inspection state lives
  -- on objects now. Union of post-D rows (object_id NOT NULL) and pre-D rows
  -- mapped via targets.object_id so historical activity counts stay intact.
  SELECT COUNT(DISTINCT obj_id) INTO objects_inspected FROM (
    SELECT object_id AS obj_id
    FROM flag_audit_log
    WHERE user_id = p_user_id AND object_id IS NOT NULL
    UNION
    SELECT t.object_id AS obj_id
    FROM flag_audit_log fal
    JOIN targets t ON t.id = fal.target_id
    WHERE fal.user_id = p_user_id
      AND fal.target_id IS NOT NULL
      AND t.object_id IS NOT NULL
  ) sub;

  SELECT COUNT(*) INTO comments_posted
  FROM comments
  WHERE user_id = p_user_id AND is_deleted = false;

  SELECT created_at INTO last_comment_at
  FROM comments
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT 1;

  SELECT changed_at INTO last_inspection_at
  FROM flag_audit_log
  WHERE user_id = p_user_id
  ORDER BY changed_at DESC
  LIMIT 1;

  last_activity := GREATEST(
    COALESCE(last_comment_at, '1970-01-01'::timestamptz),
    COALESCE(last_inspection_at, '1970-01-01'::timestamptz)
  );
  IF last_activity = '1970-01-01'::timestamptz THEN
    last_activity := NULL;
  END IF;

  result := json_build_object(
    -- Key remains 'targets_inspected' for back-compat with the API contract;
    -- semantically the value now means objects-inspected.
    'targets_inspected', COALESCE(objects_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_user_profile_stats TO authenticated;


-- =============================================================================
-- get_download_stats
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_download_stats(p_days integer DEFAULT 30)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  IF NOT is_admin THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT json_build_object(
    'total_downloads', (
      SELECT COUNT(*) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'unique_users', (
      SELECT COUNT(DISTINCT user_id) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'by_type', (
      SELECT json_object_agg(download_type, count)
      FROM (
        SELECT download_type, COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY download_type
      ) t
    ),
    'total_files', (
      SELECT COALESCE(SUM(file_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'total_targets', (
      SELECT COALESCE(SUM(target_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.target_count,
          dl.file_count,
          dl.requested_at,
          au.email,
          up.full_name
        FROM download_log dl
        LEFT JOIN auth.users au ON dl.user_id = au.id
        LEFT JOIN user_profiles up ON dl.user_id = up.user_id
        WHERE dl.requested_at >= NOW() - (p_days || ' days')::INTERVAL
        ORDER BY dl.requested_at DESC
        LIMIT 50
      ) t
    ),
    'most_downloaded_targets', (
      SELECT json_agg(t)
      FROM (
        SELECT
          target_id,
          COUNT(*) as download_count
        FROM download_log, unnest(target_ids) as target_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY target_id
        ORDER BY download_count DESC
        LIMIT 20
      ) t
    ),
    'downloads_by_day', (
      SELECT json_agg(t ORDER BY day)
      FROM (
        SELECT
          DATE(requested_at) as day,
          COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY DATE(requested_at)
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_download_stats TO authenticated;


-- =============================================================================
-- Device Auth, API Keys, and Refresh Tokens
-- =============================================================================

CREATE OR REPLACE FUNCTION public.cleanup_expired_device_codes()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  DELETE FROM device_codes
  WHERE expires_at < NOW()
  RETURNING 1 INTO deleted_count;

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;

GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO anon;
GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO authenticated;
GRANT ALL ON FUNCTION public.cleanup_expired_device_codes() TO service_role;

CREATE OR REPLACE FUNCTION public.cleanup_expired_refresh_tokens()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  -- Delete tokens that expired more than 30 days ago (keep recent for audit)
  DELETE FROM refresh_tokens
  WHERE expires_at < NOW() - INTERVAL '30 days';

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;

GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO anon;
GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO authenticated;
GRANT ALL ON FUNCTION public.cleanup_expired_refresh_tokens() TO service_role;

CREATE OR REPLACE FUNCTION public.consume_device_code(p_device_code text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  UPDATE device_codes
  SET status = 'consumed'
  WHERE
    device_code = p_device_code
    AND status = 'authorized'
    AND expires_at > NOW()
  RETURNING user_id INTO v_user_id;

  RETURN v_user_id;
END;
$$;

GRANT ALL ON FUNCTION public.consume_device_code(text) TO anon;
GRANT ALL ON FUNCTION public.consume_device_code(text) TO authenticated;
GRANT ALL ON FUNCTION public.consume_device_code(text) TO service_role;

-- Phase D: counts distinct objects a user has inspected. Replaces
-- count_distinct_inspected_targets — inspection state lives on objects now.
-- The query unions audit rows that targeted an object directly with rows
-- that pre-date Phase D (which targeted the parent target); the latter map
-- back to objects via targets.object_id so historical activity counts stay
-- intact across the migration boundary.
CREATE OR REPLACE FUNCTION public.count_distinct_inspected_objects(p_user_id uuid)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT COUNT(DISTINCT obj_id)::INTEGER FROM (
    SELECT object_id AS obj_id
    FROM flag_audit_log
    WHERE user_id = p_user_id AND object_id IS NOT NULL
    UNION
    SELECT t.object_id AS obj_id
    FROM flag_audit_log fal
    JOIN targets t ON t.id = fal.target_id
    WHERE fal.user_id = p_user_id
      AND fal.target_id IS NOT NULL
      AND t.object_id IS NOT NULL
  ) sub;
$$;

COMMENT ON FUNCTION public.count_distinct_inspected_objects(uuid) IS
  'Returns the count of distinct objects a user has inspected (object-level audit rows ∪ pre-Phase-D target-level rows mapped via targets.object_id).';

GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO anon;
GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO authenticated;
GRANT ALL ON FUNCTION public.count_distinct_inspected_objects(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.deny_device_code(p_user_code text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET status = 'denied'
  WHERE
    user_code = p_user_code
    AND status = 'pending';

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.deny_device_code(text) TO anon;
GRANT ALL ON FUNCTION public.deny_device_code(text) TO authenticated;
GRANT ALL ON FUNCTION public.deny_device_code(text) TO service_role;

CREATE OR REPLACE FUNCTION public.refresh_filter_options()
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
END;
$$;

GRANT ALL ON FUNCTION public.refresh_filter_options() TO anon;
GRANT ALL ON FUNCTION public.refresh_filter_options() TO authenticated;
GRANT ALL ON FUNCTION public.refresh_filter_options() TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_all_user_refresh_tokens(p_user_id uuid)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows;
END;
$$;

GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO anon;
GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO authenticated;
GRANT ALL ON FUNCTION public.revoke_all_user_refresh_tokens(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_refresh_token(p_token_id uuid, p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    id = p_token_id
    AND user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;

GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO anon;
GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO authenticated;
GRANT ALL ON FUNCTION public.revoke_refresh_token(uuid, uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.rotate_refresh_token(
  p_old_token_hash text,
  p_new_token_hash text,
  p_expires_at timestamptz,
  p_client_ip text DEFAULT NULL,
  p_user_agent text DEFAULT NULL
)
RETURNS TABLE(success boolean, user_id uuid, new_token_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_user_id UUID;
  v_old_token_id UUID;
  v_new_token_id UUID;
  v_device_name TEXT;
BEGIN
  -- First, validate and get the old token info
  SELECT rt.user_id, rt.id, rt.device_name
  INTO v_user_id, v_old_token_id, v_device_name
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_old_token_hash
    AND rt.is_revoked = FALSE
    AND rt.expires_at > NOW();

  IF v_user_id IS NULL THEN
    -- Token not found or invalid
    RETURN QUERY SELECT FALSE, NULL::UUID, NULL::UUID;
    RETURN;
  END IF;

  -- Create new token
  INSERT INTO refresh_tokens (
    token_hash,
    user_id,
    device_name,
    expires_at,
    client_ip,
    user_agent
  ) VALUES (
    p_new_token_hash,
    v_user_id,
    v_device_name,
    p_expires_at,
    p_client_ip,
    p_user_agent
  )
  RETURNING id INTO v_new_token_id;

  -- Revoke old token and link to new one
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW(),
    replaced_by = v_new_token_id
  WHERE id = v_old_token_id;

  RETURN QUERY SELECT TRUE, v_user_id, v_new_token_id;
END;
$$;

GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO anon;
GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO authenticated;
GRANT ALL ON FUNCTION public.rotate_refresh_token(text, text, timestamptz, text, text) TO service_role;

CREATE OR REPLACE FUNCTION public.update_api_key_last_used(key_hash_input text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  UPDATE api_keys
  SET last_used_at = NOW()
  WHERE key_hash = key_hash_input;
END;
$$;

GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO anon;
GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO authenticated;
GRANT ALL ON FUNCTION public.update_api_key_last_used(text) TO service_role;

CREATE OR REPLACE FUNCTION public.validate_api_key(key_hash_input text)
RETURNS TABLE(user_id uuid, is_valid boolean)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY
  SELECT
    ak.user_id,
    (ak.is_active AND (ak.expires_at IS NULL OR ak.expires_at > NOW()))::BOOLEAN AS is_valid
  FROM api_keys ak
  WHERE ak.key_hash = key_hash_input;
END;
$$;

GRANT ALL ON FUNCTION public.validate_api_key(text) TO anon;
GRANT ALL ON FUNCTION public.validate_api_key(text) TO authenticated;
GRANT ALL ON FUNCTION public.validate_api_key(text) TO service_role;

CREATE OR REPLACE FUNCTION public.validate_refresh_token(p_token_hash text)
RETURNS TABLE(is_valid boolean, user_id uuid, token_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
  -- Also update last_used_at when validating
  UPDATE refresh_tokens
  SET last_used_at = NOW()
  WHERE token_hash = p_token_hash
    AND is_revoked = FALSE
    AND expires_at > NOW();

  RETURN QUERY
  SELECT
    (rt.is_revoked = FALSE AND rt.expires_at > NOW())::BOOLEAN AS is_valid,
    rt.user_id,
    rt.id AS token_id
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_token_hash;
END;
$$;

GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO anon;
GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO authenticated;
GRANT ALL ON FUNCTION public.validate_refresh_token(text) TO service_role;


-- =============================================================================
-- Bulk set target object FK references
-- =============================================================================
-- Used by cfdeploy objects rebuild to set targets.object_id in bulk,
-- avoiding per-object HTTP round-trips through PostgREST.

CREATE OR REPLACE FUNCTION public.bulk_set_target_object_fks(
  p_pairs JSONB,
  p_updated_at TIMESTAMPTZ DEFAULT now()
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE targets t SET
    object_id = (pair->>'object_id')::integer,
    updated_at = p_updated_at
  FROM jsonb_array_elements(p_pairs) AS pair
  WHERE t.id = (pair->>'target_id')::integer;
END;
$$;

GRANT EXECUTE ON FUNCTION public.bulk_set_target_object_fks(JSONB, TIMESTAMPTZ) TO service_role;


-- =============================================================================
-- Recompute target aggregate columns from spectra
-- =============================================================================
-- Bulk-recomputes max_snr and max_exposure_time on targets from the spectra
-- table. Called by the deploy CLI after batch spectra upserts, replacing the
-- old per-row triggers which caused statement timeouts on large batches.

CREATE OR REPLACE FUNCTION public.recompute_target_aggregates(
  p_target_ids TEXT[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
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
$$;

GRANT EXECUTE ON FUNCTION public.recompute_target_aggregates(TEXT[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.recompute_target_aggregates(TEXT[]) TO service_role;

-- =============================================================================
-- Compute objects.redshift_auto from best member spectrum (grating-priority)
-- =============================================================================
-- For each object in the field, set redshift_auto to the redshift_auto of
-- its best member spectrum under a grating-priority hierarchy:
--   1. PRISM (3x wavelength coverage, highest z-confirmation efficiency)
--   2. Medium-resolution gratings (G140M, G235M, G395M)
--   3. High-resolution gratings (G140H, G235H, G395H)
-- Ties within a tier are broken by longest exposure_time, then lowest id.
-- SNR is intentionally not used: contamination can inflate SNR and PRISM's
-- wavelength coverage makes it the most reliable discriminator even at
-- modest SNR. Objects whose members all have NULL redshift_auto are nulled
-- out. Called by reconcile_field_objects() after membership/aggregate
-- updates.
--
-- Replaces the old two-hop path (pipeline → target.redshift_auto → object
-- via update_object_best_redshift trigger) with a direct one-hop path
-- (spectra.redshift_auto → objects.redshift_auto at reconciliation time).

-- The CTE + IS DISTINCT FROM guard ensures we only rewrite rows whose
-- redshift_auto actually changed, and updated_at is bumped in the same
-- statement so get_objects_for_sync (which uses updated_at as its delta
-- cursor) picks the change up on the next client sync. Without the bump,
-- clients would silently miss redshift_auto changes from pipeline reruns.
-- ROW_COUNT is then the true number of objects whose value changed.
--
-- Sign-off pinning is now handled at write time by the
-- pin_redshift_on_signoff trigger: any object reaching quality >= 2 with
-- redshift_inspected = NULL has its current redshift_auto promoted into
-- redshift_inspected and inspected_used_auto = true. The displayed redshift
-- (the generated `redshift` column) is therefore stable across reprocessing
-- for every signed-off object — this function never has to worry about
-- moving a value out from under an inspector.
--
-- Staleness signal: when redshift_auto changes for an already-signed-off
-- object (quality >= 2), we still flag staleness_reason='reprocessed' and
-- bump last_data_change_at so the UI surfaces a "Needs Review" badge.
-- The pinned displayed redshift is unchanged, but the inspector should
-- know the underlying fit shifted in case they want to update their
-- override or reaffirm the existing one.
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
      staleness_reason = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
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

GRANT EXECUTE ON FUNCTION public.compute_object_redshift_auto(TEXT) TO service_role;


-- Re-link object_list_members.object_id after object rebuild for a field.
-- Uses spatial tolerance (0.3 arcsec) to match members to the nearest
-- rebuilt object. Returns JSONB with counts:
--   { "relinked": N, "orphaned": N, "orphaned_details": [...] }
--
-- Operates on members whose previous object was in this field (now NULL
-- after ON DELETE SET NULL) or whose coordinates fall within the field's
-- bounding box.
DROP FUNCTION IF EXISTS public.relink_list_members_for_field(TEXT);

CREATE OR REPLACE FUNCTION public.relink_list_members_for_field(p_field TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_orphaned_details JSONB := '[]'::JSONB;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    -- No objects in field, nothing to re-link
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0, 'orphaned_details', '[]'::jsonb);
  END IF;

  -- Re-link: for each unlinked member whose coords fall in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT olm.id AS member_id,
           olm.ra AS member_ra,
           olm.dec AS member_dec,
           olm.list_id,
           o.id AS obj_id,
           -- Angular distance approximation (sufficient for sub-arcsec)
           SQRT(
             POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
             POWER(olm.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY olm.id
             ORDER BY SQRT(
               POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
               POWER(olm.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM object_list_members olm
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN olm.ra - v_tolerance_deg AND olm.ra + v_tolerance_deg
        AND o.dec BETWEEN olm.dec - v_tolerance_deg AND olm.dec + v_tolerance_deg
    ) o
    WHERE olm.object_id IS NULL
      AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
      AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max
  ),
  best_match AS (
    SELECT member_id, obj_id, dist_deg
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE object_list_members olm
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE olm.id = bm.member_id
    RETURNING olm.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Count orphaned members (still NULL after re-link, coords in field bbox)
  SELECT COUNT(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'list_slug', ol.slug,
           'list_name', ol.name,
           'ra', olm.ra,
           'dec', olm.dec
         )), '[]'::jsonb)
  INTO n_orphaned, v_orphaned_details
  FROM object_list_members olm
  JOIN object_lists ol ON ol.id = olm.list_id
  WHERE olm.object_id IS NULL
    AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
    AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned,
    'orphaned_details', v_orphaned_details
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.relink_list_members_for_field(TEXT) TO service_role;


-- =============================================================================
-- relink_photometry_for_field
-- (re-link object_photometry.object_id after objects rebuild)
-- =============================================================================

DROP FUNCTION IF EXISTS public.relink_photometry_for_field(TEXT);

CREATE OR REPLACE FUNCTION public.relink_photometry_for_field(p_field TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0);
  END IF;

  -- Re-link: for each photometry row in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT op.id AS phot_id,
           o.id AS obj_id,
           SQRT(
             POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
             POWER(op.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY op.id
             ORDER BY SQRT(
               POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
               POWER(op.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM object_photometry op
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN op.ra - v_tolerance_deg AND op.ra + v_tolerance_deg
        AND o.dec BETWEEN op.dec - v_tolerance_deg AND op.dec + v_tolerance_deg
    ) o
    WHERE op.field = p_field
  ),
  best_match AS (
    SELECT phot_id, obj_id
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE object_photometry op
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE op.id = bm.phot_id
    RETURNING op.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Set NULL for unmatched rows in this field
  UPDATE object_photometry
  SET object_id = NULL
  WHERE field = p_field
    AND id NOT IN (SELECT id FROM object_photometry WHERE field = p_field AND object_id IS NOT NULL);

  -- Count orphaned
  SELECT COUNT(*) INTO n_orphaned
  FROM object_photometry
  WHERE field = p_field AND object_id IS NULL;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.relink_photometry_for_field(TEXT) TO service_role;


-- =============================================================================
-- sync_photometry_to_objects
-- (copy photo_z + has_photometry from object_photometry to objects)
-- =============================================================================

DROP FUNCTION IF EXISTS public.sync_photometry_to_objects(TEXT);

CREATE OR REPLACE FUNCTION public.sync_photometry_to_objects(p_field TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  n_updated INTEGER;
BEGIN
  -- Update objects that have linked photometry
  WITH phot AS (
    SELECT DISTINCT ON (op.object_id)
      op.object_id,
      op.photo_z,
      op.photo_z_err_lo,
      op.photo_z_err_hi
    FROM object_photometry op
    WHERE op.field = p_field AND op.object_id IS NOT NULL
    ORDER BY op.object_id, op.updated_at DESC
  )
  UPDATE objects o
  SET photo_z = phot.photo_z,
      photo_z_err_lo = phot.photo_z_err_lo,
      photo_z_err_hi = phot.photo_z_err_hi,
      has_photometry = TRUE
  FROM phot
  WHERE o.id = phot.object_id;

  GET DIAGNOSTICS n_updated = ROW_COUNT;

  -- Clear photometry flags for objects in this field that have no linked photometry
  UPDATE objects o
  SET photo_z = NULL,
      photo_z_err_lo = NULL,
      photo_z_err_hi = NULL,
      has_photometry = FALSE
  WHERE o.field = p_field
    AND o.has_photometry = TRUE
    AND NOT EXISTS (
      SELECT 1 FROM object_photometry op
      WHERE op.object_id = o.id
    );

  RETURN n_updated;
END;
$$;

GRANT EXECUTE ON FUNCTION public.sync_photometry_to_objects(TEXT) TO service_role;
