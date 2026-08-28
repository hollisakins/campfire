-- Issue #488: viewer-scoped grating filtering for the object-level catalog RPCs.
--
-- The grating filter previously tested the stored objects.gratings array — a
-- deploy-time aggregate over ALL member spectra, blind to publication status
-- and to programs outside the viewer's access — so a PRISM-only row (as
-- displayed) could match an M/H-grating filter via an unpublished or
-- proprietary sibling spectrum, and inversely be dropped from a "none of M"
-- filter. The filter now decides on the viewer-visible grating set: each RPC
-- materializes objects_matching_grating_filter() once per call and tests
-- candidates with a hashed = ANY, keeping the stored array tests as
-- index-backed pre-filters.
--
-- object_scoped_aggregates' stored-columns fast path additionally gains a
-- no-unpublished-member guard so the DISPLAYED aggregates agree with the
-- scoped filter (the stored columns are publication-blind).
--
-- Hand-authored (functions only; no docker for `supabase db diff` in this
-- environment). Bodies are verbatim copies from supabase/schemas/functions.sql.

CREATE OR REPLACE FUNCTION public.object_scoped_aggregates(
  p_object_id INTEGER,
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(
  programs          TEXT[],
  gratings          TEXT[],
  observations      TEXT[],
  n_targets         INTEGER,
  n_spectra         INTEGER,
  max_snr           DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
  -- Fast path (perf, issue #103): when the caller can access every program this
  -- object belongs to AND unpublished members are excluded AND every member
  -- spectrum is published, the viewer-scoped recompute below is provably
  -- identical to the aggregate columns already stored on the object.
  -- The stored columns come from the deploy-time builder
  -- (python/campfire/deploy/objects.py + reconcile), which aggregates ALL
  -- member spectra blind to publication status — so the all-published guard is
  -- load-bearing: without it, an object carrying a draft/revoked sibling
  -- spectrum would display the unpublished grating (and count it in
  -- n_spectra/max_snr) while the viewer-scoped grating FILTER (issue #488,
  -- objects_matching_grating_filter) correctly excludes it — a filter/display
  -- contradiction. Restricting the recompute to a superset of the object's
  -- programs drops nothing, so `o.programs <@ p_program_slugs` plus the
  -- no-unpublished guard is exactly the "recompute == stored" condition.
  -- Returning the stored columns via a single PK lookup (plus a few indexed
  -- member probes for the guard) skips the per-row targets+spectra aggregate
  -- scans that dominated get_objects_for_sync (and the catalog list RPCs) at
  -- scale. Partial-access, draft-inclusive, or unpublished-carrying objects
  -- fall through to the recompute, preserving the anti-leak scoping (see the
  -- header comment and supabase/tests/check_object_aggregate_scoping.sql).
  IF NOT p_include_unpublished THEN
    RETURN QUERY
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE o.id = p_object_id
      AND o.programs <@ p_program_slugs
      AND NOT EXISTS (
        SELECT 1
        FROM targets t
        JOIN spectra s ON s.target_id = t.target_id
        WHERE t.object_id = p_object_id
          AND s.deploy_status <> 'published'
      );
    IF FOUND THEN
      RETURN;
    END IF;
  END IF;

  RETURN QUERY
  WITH m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM targets t
    WHERE t.object_id = p_object_id
      AND t.program_slug = ANY(p_program_slugs)
      -- B1: only count targets that contribute a published spectrum so
      -- n_targets / programs / observations don't include draft-only members.
      AND (p_include_unpublished OR t.has_published_spectrum)
  ),
  sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM spectra s
    WHERE s.target_id IN (SELECT target_id FROM m)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT
    COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[],
    (SELECT COUNT(*) FROM m)::integer,
    (SELECT COUNT(*) FROM sp)::integer,
    (SELECT MAX(sp.signal_to_noise) FROM sp),
    (SELECT MAX(sp.exposure_time) FROM sp);
END;
$$;

GRANT EXECUTE ON FUNCTION public.object_scoped_aggregates(INTEGER, TEXT[], BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.object_scoped_aggregates(INTEGER, TEXT[], BOOLEAN) TO service_role;

-- =============================================================================
-- objects_matching_grating_filter
-- =============================================================================
-- Viewer-scoped grating filtering for the object-level catalog RPCs
-- (issue #488).
--
-- The stored objects.gratings array is computed at deploy time across ALL
-- member spectra — blind to publication status and spanning programs the
-- viewer may not access (python/campfire/deploy/objects.py). Filtering on it
-- alone matched objects whose only *visible* spectra carry none of the
-- selected gratings (a PRISM-only row matching an M-grating filter via an
-- unpublished or proprietary sibling spectrum), while the displayed row is
-- scoped via object_scoped_aggregates(). This helper returns the objects.id
-- set whose VISIBLE spectra — member targets in p_program_slugs, published
-- unless p_include_unpublished — satisfy the grating selection:
--
--   'any' / 'none': objects with at least one visible spectrum in p_gratings
--                   (callers apply IN for 'any', NOT IN for 'none')
--   'all':          objects whose visible spectra cover every requested grating
--
-- It is set-returning (rather than a per-object boolean) so call sites can
-- materialize it ONCE per RPC call (`v_grating_object_ids := ARRAY(SELECT
-- ...)`) and test candidates with a hashed `o.id = ANY(...)` — a per-object
-- boolean probe measured ~2x slower on a 40k-object catalog, and an
-- uncorrelated IN-subplan gets rebuilt per statement (twice in the
-- count+page RPCs). Call sites also keep the stored array as an index-backed
-- pre-filter: the stored array is a superset of every viewer's visible set,
-- so o.gratings &&/@> p_gratings is a NECESSARY condition for 'any'/'all' and
-- NOT o.gratings && p_gratings is a SUFFICIENT condition for 'none'. If the
-- deploy-time builder ever narrows what it aggregates into objects.gratings,
-- that superset invariant breaks and the call-site pre-filters must be
-- revisited.
--
-- plpgsql + force_custom_plan (not LANGUAGE sql) is deliberate: a non-inlined
-- SQL function body is planned with symbolic parameters, which made the
-- planner seq-scan spectra past idx_spectra_grating and pay the 'all'-mode
-- DISTINCT sort in every mode; custom plans with the real argument values
-- pick the index and skip the aggregate for 'any'/'none'.
--
-- Never returns NULL rows (callers rely on this: NOT (id = ANY(set)) must
-- never go three-valued NULL). Like object_scoped_aggregates: p_program_slugs
-- must already be access-checked by the caller — it is the access gate, not
-- RLS.
CREATE OR REPLACE FUNCTION public.objects_matching_grating_filter(
  p_gratings TEXT[],
  p_gratings_mode TEXT,
  p_program_slugs TEXT[],
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS SETOF INTEGER
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  IF p_gratings_mode = 'all' THEN
    RETURN QUERY
    SELECT t.object_id
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND s.grating = ANY(p_gratings)
    GROUP BY t.object_id
    HAVING COUNT(DISTINCT s.grating) = (SELECT COUNT(DISTINCT g) FROM unnest(p_gratings) g);
  ELSE
    -- 'any' and 'none' need the same set (callers negate for 'none').
    RETURN QUERY
    SELECT DISTINCT t.object_id
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND s.grating = ANY(p_gratings);
  END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION public.objects_matching_grating_filter(TEXT[], TEXT, TEXT[], BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION public.objects_matching_grating_filter(TEXT[], TEXT, TEXT[], BOOLEAN) TO service_role;

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
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
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
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
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
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
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
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
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
  p_list_ids_mode TEXT DEFAULT 'any',
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
  v_grating_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
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
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

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
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
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

