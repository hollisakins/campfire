-- Issue #491: viewer-scoped observation filtering for the object-level
-- catalog RPCs (same root cause as #488, on the observations axis).
--
-- The observations filter previously tested the stored objects.observations
-- array — a deploy-time aggregate over ALL member targets, blind to
-- publication status and to programs outside the viewer's access — so a
-- viewer could match (and count) objects whose only member in the selected
-- observation belongs to a proprietary program or is draft-only, even though
-- the displayed row (scoped via object_scoped_aggregates) hides that
-- observation. The filter now decides on the viewer-visible observation set:
-- each RPC materializes objects_matching_observation_filter() once per call
-- and tests candidates with a hashed = ANY, keeping the stored-array && test
-- as an index-backed pre-filter.
--
-- Hand-authored (functions only; no docker for `supabase db diff` in this
-- environment). Bodies are verbatim copies from supabase/schemas/functions.sql.

-- =============================================================================
-- objects_matching_observation_filter
-- =============================================================================
-- Viewer-scoped observation filtering for the object-level catalog RPCs
-- (issue #491 — same root cause as #488, on the observations axis).
--
-- The stored objects.observations array is computed at deploy time across ALL
-- member targets — blind to publication status and spanning programs the
-- viewer may not access (python/campfire/deploy/objects.py). Filtering on it
-- alone matched objects whose only member in a selected observation is
-- invisible to the viewer (proprietary program, or draft-only member), while
-- the displayed row — scoped via object_scoped_aggregates() — hides that
-- observation. This helper returns the objects.id set with at least one
-- VISIBLE member target in p_observations: member targets in
-- p_program_slugs, contributing a published spectrum unless
-- p_include_unpublished — the same member gate as object_scoped_aggregates
-- (observations live on targets, so no spectra join is needed; the
-- denormalized targets.has_published_spectrum is the publication gate).
-- Observations only need 'any' semantics — there is no mode parameter.
--
-- It is set-returning (rather than a per-object boolean) so call sites can
-- materialize it ONCE per RPC call (`v_observation_object_ids := ARRAY(SELECT
-- ...)`) and test candidates with a hashed `o.id = ANY(...)`. Call sites also
-- keep the stored array as an index-backed pre-filter: the stored array is a
-- superset of every viewer's visible set, so o.observations && p_observations
-- is a NECESSARY condition. If the deploy-time builder ever narrows what it
-- aggregates into objects.observations, that superset invariant breaks and
-- the call-site pre-filters must be revisited.
--
-- plpgsql + force_custom_plan for the same reason as
-- objects_matching_grating_filter: custom plans with the real argument values
-- pick idx_targets_observation instead of planning symbolically.
--
-- Never returns NULL rows (t.object_id IS NOT NULL). Like
-- object_scoped_aggregates: p_program_slugs must already be access-checked by
-- the caller — it is the access gate, not RLS. Callers pass the full
-- accessible p_program_slugs (not the p_filter_programs-narrowed set) to stay
-- consistent with what the returned rows display.
CREATE OR REPLACE FUNCTION public.objects_matching_observation_filter(
  p_observations TEXT[],
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS SETOF INTEGER
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  SELECT DISTINCT t.object_id
  FROM targets t
  WHERE t.object_id IS NOT NULL
    AND t.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR t.has_published_spectrum)
    AND t.observation = ANY(p_observations);
END;
$$;

GRANT EXECUTE ON FUNCTION public.objects_matching_observation_filter(TEXT[], TEXT[], BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.objects_matching_observation_filter(TEXT[], TEXT[], BOOLEAN) TO service_role;


-- =============================================================================
-- get_filtered_objects_paginated
-- (one row per unique sky position, cross-matched across programs)
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated;

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
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_unpublished BOOLEAN DEFAULT false
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
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
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


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  v_observation_filter_active := (p_observations IS NOT NULL AND array_length(p_observations, 1) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  -- Step 1: count
  SELECT COUNT(*) INTO v_total_count
  FROM objects o
  WHERE
    -- Access control: object must have at least one accessible program
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    -- B1: hide objects with no published spectrum (fail-closed).
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      -- Issue #488: the o.gratings array tests are index-backed pre-filters
      -- only (deploy-time aggregate over ALL member spectra, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_grating_object_ids — see
      -- objects_matching_grating_filter() for the invariants.
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
          OR NOT (o.id = ANY(v_grating_object_ids))))
    )
    AND (
      NOT v_observation_filter_active
      -- Issue #491: the o.observations && test is an index-backed pre-filter
      -- only (deploy-time aggregate over ALL member targets, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_observation_object_ids — see
      -- objects_matching_observation_filter() for the invariants.
      OR (o.observations && p_observations
          AND o.id = ANY(v_observation_object_ids))
    )
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
    AND (
      NOT v_list_filter_active
      OR (v_list_ids_mode = 'any' AND o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      OR (v_list_ids_mode = 'all' AND (
          SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
          WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
      ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
      OR (v_list_ids_mode = 'none' AND o.id NOT IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
    )
    AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
    AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
    AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
    AND (
      NOT v_comment_search_active
      -- Uncorrelated semijoin: collect the object_ids that have a matching
      -- comment ONCE (object-level comments directly + target-level comments
      -- mapped through their parent object), then probe o.id IN (...). The old
      -- correlated EXISTS-inside-OR re-ran a per-object targets subquery for
      -- every (object x matching-comment) pair -> 271k subplan executions /
      -- ~870ms here, multi-second on broad terms or cold cache.
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
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see the count query above for the rationale.
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
        -- Aggregates scoped to the viewer's accessible programs so mixed-program
        -- objects don't leak proprietary member metadata. Deliberately NOT
        -- narrowed by p_filter_programs: a program filter selects which objects
        -- appear (overlap test above), but each row still shows the object's
        -- full accessible programs/observations. Filter and sort above run on
        -- the global o.* columns; the substitution happens only on the
        -- paginated result set.
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
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
            AND t.program_slug = ANY(p_program_slugs)
            -- Same publication gate as object_scoped_aggregates: the RPC is
            -- reached via the service-role client (/api/v1/objects), so RLS
            -- won't hide draft-only members here.
            AND (p_include_unpublished OR t.has_published_spectrum)
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
    LEFT JOIN LATERAL public.object_scoped_aggregates(fo.id, p_program_slugs, p_include_unpublished) sa ON true
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

DROP FUNCTION IF EXISTS public.get_filtered_object_ids;

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids(
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
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(object_id TEXT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
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

  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  v_observation_filter_active := (p_observations IS NOT NULL AND array_length(p_observations, 1) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  SELECT o.object_id
  FROM objects o
  WHERE
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      -- Issue #488: the o.gratings array tests are index-backed pre-filters
      -- only (deploy-time aggregate over ALL member spectra, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_grating_object_ids — see
      -- objects_matching_grating_filter() for the invariants.
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
          OR NOT (o.id = ANY(v_grating_object_ids))))
    )
    AND (
      NOT v_observation_filter_active
      -- Issue #491: the o.observations && test is an index-backed pre-filter
      -- only (deploy-time aggregate over ALL member targets, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_observation_object_ids — see
      -- objects_matching_observation_filter() for the invariants.
      OR (o.observations && p_observations
          AND o.id = ANY(v_observation_object_ids))
    )
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
    AND (
      NOT v_list_filter_active
      OR (v_list_ids_mode = 'any' AND o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      OR (v_list_ids_mode = 'all' AND (
          SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
          WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
      ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
      OR (v_list_ids_mode = 'none' AND o.id NOT IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
    )
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

DROP FUNCTION IF EXISTS public.get_adjacent_objects;

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
  p_list_ids_mode TEXT DEFAULT 'any',
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_has_photometry BOOLEAN DEFAULT NULL,
  p_photo_z_min DOUBLE PRECISION DEFAULT NULL,
  p_photo_z_max DOUBLE PRECISION DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(prev_object_id TEXT, next_object_id TEXT, current_index BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_sort_is_text BOOLEAN;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
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

  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  v_observation_filter_active := (p_observations IS NOT NULL AND array_length(p_observations, 1) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
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
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
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
