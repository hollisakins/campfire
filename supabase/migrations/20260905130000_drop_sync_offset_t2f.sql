-- Perf T2-F follow-ups (#511): drop p_offset from the four /api/v1/sync/*
-- RPCs; stop materializing the spectra list's filtered set on every page.
--
-- #536 retired OFFSET pagination at the route (offset>0 is a 400, client floor
-- 0.5.0) but kept p_offset on the RPC signatures because the migration and the
-- Vercel deploy land independently on merge and the previously deployed routes
-- sent p_offset on every call. Those routes are gone from production, so the
-- with-offset overloads are dropped and the functions recreated without the
-- parameter. Bodies are otherwise unchanged.
--
-- get_filtered_spectra_paginated: filtered_spectra / distance_filtered become
-- NOT MATERIALIZED. Measured on prod after #536: 521 ms and a 32 MB temp spill
-- per 1000-row page (~80 k spectra materialized for every page, count or not);
-- with the count skipped the page is now one scan + top-N sort.
--
-- Hand-authored (no Docker on the authoring machine): definitions copied
-- verbatim from supabase/schemas/functions.sql (source of truth); verified by
-- applying to a database built from main's schema files and comparing
-- pg_get_functiondef with the branch schema.

DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);
DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);
-- T2-F follow-up (#511): the p_offset signature, retained through the #536
-- rollout, is dropped now that no deployed route sends it.
DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT);

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the object_id of the last row of the previous page.
  -- When non-NULL the scan seeks straight to the next id via the
  -- objects_object_id_key UNIQUE btree, so each page costs O(log N + limit).
  -- Keyset is the only pagination (T2-F, #511): OFFSET is refused at the
  -- route (client floor 0.5.0), p_offset is gone from the signature, and so
  -- is the 120 s statement_timeout exemption that existed for deep OFFSET
  -- pages — every page runs under the role's default timeout.
  p_after_object_id TEXT DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
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
      -- B1: drop objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
      -- Keyset (#103): seek past the previous page's last object_id. object_id
      -- is UNIQUE, so a strict > needs no (sort_col, id) tiebreaker. Any future
      -- change to this ORDER BY must keep the ordering column UNIQUE (or switch
      -- to a row-value cursor) or keyset will skip/duplicate rows.
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
    ORDER BY o.object_id
    LIMIT p_limit
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
      AND (p_include_unpublished OR s.deploy_status = 'published')
    GROUP BY t.object_id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id))
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
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        -- Aggregates scoped to the caller's accessible programs so a sync that
        -- pulls a mixed-program object doesn't leak proprietary member metadata
        -- into the Python catalog. See object_scoped_aggregates().
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
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
      -- Keyset (#103): the client uses the LAST element's object_id as the next
      -- page's cursor, so the page array MUST be in object_id order. matched is
      -- ORDER BY object_id, but the LEFT JOINs below can reorder it, so pin the
      -- aggregate order explicitly.
      ORDER BY m.object_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  LEFT JOIN LATERAL public.object_scoped_aggregates(m.id, p_program_slugs, p_include_unpublished) sa ON true;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;

DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);
DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);
-- T2-F follow-up (#511): p_offset signature dropped (see get_objects_for_sync).
DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT);

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the spectrum_id of the last row of the previous page,
  -- seeked via the idx_spectra_spectrum_id UNIQUE btree. See
  -- get_objects_for_sync for the design; keyset-only since T2-F (#511).
  p_after_spectrum_id TEXT DEFAULT NULL
)
RETURNS TABLE(spectra JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           t.program_slug, t.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit
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
      AND (p_include_unpublished OR s.deploy_status = 'published')
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
      AND (p_include_unpublished OR s.deploy_status = 'published')
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
        'cfpipe_version', m.cfpipe_version,
        'crds_context', m.crds_context,
        'jwst_version', m.jwst_version,
        'date_obs', m.date_obs,
        'reduced_at', m.reduced_at,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be spectrum_id-ordered (client cursors on
      -- the last element). matched has no post-ORDER joins, but pin it anyway.
      ORDER BY m.spectrum_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;

DROP FUNCTION IF EXISTS public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER);
DROP FUNCTION IF EXISTS public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN);
-- T2-F follow-up (#511): p_offset signature dropped (see get_objects_for_sync).
DROP FUNCTION IF EXISTS public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, INTEGER);

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Count gating (#103): only the keyset first page needs the count; skip the
  -- COUNT(*) scan on every subsequent page, matching the other /sync/* RPCs.
  p_include_counts BOOLEAN DEFAULT TRUE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the object_photometry PK btree. Keyset-only since T2-F (#511).
  p_after_id INTEGER DEFAULT NULL
)
RETURNS TABLE(photometry_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
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
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit
  ),
  -- Count CTE gated on p_include_counts; when FALSE the planner collapses it to
  -- One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND (p_include_unpublished OR o.has_published_spectrum)
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
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;

DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER, INTEGER, INTEGER,
  INTEGER[], TEXT, BOOLEAN, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT, INTEGER, INTEGER, BOOLEAN
);

DROP FUNCTION IF EXISTS public.get_filtered_spectra_paginated;

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
  p_list_ids_mode TEXT DEFAULT 'any',
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
  p_include_thumbnails BOOLEAN DEFAULT false,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Perf T1-5 (#501): the exact COUNT(*) over the whole filtered set is only
  -- needed once per filter combination; the client caches it and passes
  -- false on later pages / sorts. total_count is -1 when skipped.
  p_include_count BOOLEAN DEFAULT true,
  -- Perf T2-F (#511): keyset cursor for /api/v1/spectra/list. The cursor is the
  -- (sort value, tiebreak) of the last row of the previous page: exactly one of
  -- p_after_sort_text / p_after_sort_num carries the sort value (which one is
  -- decided here from the resolved p_sort_column, so callers round-trip both
  -- opaquely), and p_after_tiebreak is [target_id, grating, spectrum_id] — the
  -- full ORDER BY tail, which is a total order because spectrum_id is UNIQUE.
  -- When set, p_page is ignored (offset 0) and the page is the next
  -- p_page_size rows strictly after the cursor under the same sort. The
  -- function hands the next cursor back in next_sort_text / next_sort_num /
  -- next_tiebreak (NULL when has_more is false), computed from the same
  -- columns the ORDER BY sorts on, so the caller never re-derives it from the
  -- JSON payload (whose aggregate columns can be viewer-scoped).
  p_after_sort_text TEXT DEFAULT NULL,
  p_after_sort_num DOUBLE PRECISION DEFAULT NULL,
  p_after_tiebreak TEXT[] DEFAULT NULL
)
RETURNS TABLE(
  targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER,
  -- T2-F: has_more is exact (one row past the page is fetched and dropped), so
  -- cursor walks end without a trailing empty request.
  has_more BOOLEAN, next_sort_text TEXT, next_sort_num DOUBLE PRECISION, next_tiebreak TEXT[]
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
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

  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
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

  -- T2-F: which cursor slot the resolved sort column lives in. Every other
  -- whitelisted column is numeric (double precision, or integer cast to it).
  v_sort_is_text := p_sort_column IN ('target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'grating');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 3);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

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
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;

  -- filtered_spectra / distance_filtered are NOT MATERIALIZED (T2-F follow-up,
  -- #511): with the count skipped (every cursor page, every web page after the
  -- first) the count subquery folds away and the page is one scan with a
  -- top-N sort, instead of the ~80 k-row filtered set being materialized to
  -- temp (~32 MB) on every page as it was when the CTE was shared with the
  -- count. With the count requested (page 1) the filter runs twice — a scan
  -- each for the count and the page — which costs about what the spill did.
  -- The CTE carries NO thumbnail columns: the two SVGs are ~1.5 kB per row
  -- (103 MB temp spill per render before T1-5 / #501); they are joined onto
  -- the <= p_page_size page rows at the end, only when p_include_thumbnails.
  RETURN QUERY
  WITH filtered_spectra AS NOT MATERIALIZED (
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
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
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
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
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
        -- Uncorrelated semijoin: build the set of matching target_ids ONCE
        -- (trgm/seq scan over the tiny comments table) instead of re-probing
        -- comments per outer row. Correlated EXISTS-inside-OR can't be pulled
        -- up and re-executes per spectrum -> timeouts on broad access. See the
        -- objects path below for the object-level analogue.
        OR t.id IN (
          SELECT c.target_id FROM comments c
          WHERE c.target_id IS NOT NULL
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
  distance_filtered AS NOT MATERIALIZED (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  -- T2-F: one row = one candidate plus its sort key, materialized in exactly
  -- one of two typed slots. The ORDER BY, the keyset predicate and the
  -- returned next-cursor all read these two columns, so they cannot drift
  -- apart. p_sort_column is a constant under force_custom_plan, so each CASE
  -- folds to the single referenced column at plan time.
  keyed AS (
    SELECT df.*,
      CASE p_sort_column
        WHEN 'target_id' THEN df.target_id
        WHEN 'spectrum_id' THEN df.spectrum_id
        WHEN 'field' THEN df.field
        WHEN 'observation' THEN df.observation
        WHEN 'program_slug' THEN df.program_slug
        WHEN 'grating' THEN df.grating
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN df.distance::double precision
        WHEN 'ra' THEN df.ra::double precision
        WHEN 'dec' THEN df."dec"::double precision
        WHEN 'redshift' THEN df.redshift::double precision
        WHEN 'redshift_quality' THEN df.redshift_quality::double precision
        WHEN 'redshift_auto' THEN df.redshift_auto::double precision
        WHEN 'signal_to_noise' THEN df.signal_to_noise::double precision
        WHEN 'exposure_time' THEN df.exposure_time::double precision
      END AS sort_num
    FROM distance_filtered df
  ),
  -- One row past the page (p_page_size + 1) so has_more is exact; the final
  -- SELECT drops it and the cursor is taken from row p_page_size.
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset: rows strictly after the cursor in (sort key <dir> NULLS
        -- LAST, target_id, grating, spectrum_id) order. With a non-NULL cursor
        -- value that is every row whose key sorts after it plus the whole NULL
        -- tail; with a NULL cursor value (the walk is inside the tail) only
        -- NULL-keyed rows past the tiebreak. Equal keys fall through to the
        -- row-value tiebreak comparison.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          END
        )
      -- Exactly one of the four key terms is live for a given call (the other
      -- three are constant NULL); the tail makes the order total. spectrum_id
      -- was added to the tail in T2-F — (target_id, grating) alone is not
      -- unique (one grating can pair with several filters), which a keyset
      -- cursor cannot tolerate.
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.target_id ASC, k.grating ASC, k.spectrum_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  -- The row the next cursor is built from: the page's last row, and only when
  -- an overflow row proved there is a next page.
  cursor_row AS (
    SELECT pr.sort_text, pr.sort_num, pr.target_id, pr.grating, pr.spectrum_id
    FROM page_rows pr
    WHERE pr.row_num = p_page_size
      AND EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size)
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
        'thumbnail_svg_fnu', sth.thumbnail_svg_fnu,
        'thumbnail_svg_flambda', sth.thumbnail_svg_flambda
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    CASE WHEN p_include_count THEN (SELECT COUNT(*) FROM distance_filtered) ELSE -1::BIGINT END,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.target_id, cr.grating, cr.spectrum_id] FROM cursor_row cr)
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug
  -- Thumbnails only for the page, only when asked (constant-false join
  -- condition otherwise, which the planner drops).
  LEFT JOIN spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;

DROP FUNCTION IF EXISTS public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN);
-- T2-F follow-up (#511): p_offset signature dropped (see get_objects_for_sync).
DROP FUNCTION IF EXISTS public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, BIGINT);

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT FALSE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the storage_objects_pkey btree. storage_key is NOT usable as a cursor
  -- (only UNIQUE as (backend, bucket, storage_key)), and sync order is
  -- irrelevant to the client (it upserts by key), so this orders by the PK.
  -- Keyset-only since T2-F (#511): the scope predicate (published EXISTS
  -- checks) is evaluated on only ~p_limit rows past the seek.
  p_after_id BIGINT DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
      AND (p_updated_since IS NULL OR so.updated_at > p_updated_since)
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO service_role;
