-- Perf T2-C (#506, epic #515, decision D-C): reads as GET routes.
--
-- Hand-authored: no local Docker for `supabase db diff`. Both function bodies
-- below are copied verbatim from supabase/schemas/functions.sql (the source of
-- truth); this file carries nothing else.
--
--   1. get_adjacent_objects — same signature and return type, new body: one
--      window-function pass over a three-column projection instead of a
--      MATERIALIZED copy of the filtered catalog + three ordered LIMIT 1
--      subqueries + a count (138 ms and a 1 430-block temp spill on prod for
--      two ids; the new statement measured 125 ms / 107 ms on prod with no
--      temp blocks — the scan and the collation sort are what remain). Same total order as the list RPC (sort key NULLS LAST, then
--      object_id ASC), photo_z added to the sortable set for parity, and the
--      coordinate filter now applies the Haversine cut in the WHERE exactly as
--      get_filtered_objects_paginated does. Validated against the old body on
--      a 42 k-object fixture: 6 000 (object x sort x direction x filter)
--      cases, identical wherever the object is inside the filter; outside it
--      the old body returned (NULL, NULL, 0, <count>) and this returns
--      (NULL, NULL, 0, 0), which the UI renders the same way.
--   2. get_objects_near — new: k nearest visible objects to a point (box on
--      the (ra, dec) index + Haversine), for the object page's Nearby card
--      and the inspection overlay, replacing a 33-parameter list-RPC cone
--      search. SECURITY INVOKER; RLS on objects gates rows.

-- =============================================================================
-- get_adjacent_objects
-- =============================================================================
-- Prev/next ids and the 1-based position of one object within the filtered,
-- sorted catalog, for the object page's navigation arrows. Same filter
-- contract as get_filtered_objects_paginated (the web layer builds both
-- parameter sets from one function, web/lib/actions/filter-params.ts), and
-- the total order MUST match the list RPC's — sort key NULLS LAST, then
-- object_id ASC — or the arrows walk a different sequence than the table.
--
-- Perf T2-C (#506, audit DB-08): one pass instead of a materialized catalog.
-- The old body MATERIALIZED every filtered row with a dozen columns, then ran
-- three ordered LIMIT 1 subqueries and a count over that copy: 138 ms and a
-- temp spill (1 430 temp blocks, ~11 MB) on prod for two ids, on every object
-- page that missed the client's session cache. This is a single window pass
-- over a three-column projection (object_id + the two sort keys): LAG/LEAD
-- give the neighbours, ROW_NUMBER the position, COUNT(*) the total — one
-- scan, one in-memory sort, one WindowAgg, no temp. Measured on prod as a
-- standalone statement (non-admin, 27 400 visible objects): 125 ms for the
-- object_id sort, 107 ms for redshift, zero temp blocks. The remaining cost
-- is the filtered scan (~35 ms) plus the sort — text sorts pay ICU
-- collation on object_ids whose long shared prefixes defeat abbreviated
-- keys. Keyset probes on the object_id index were measured too (2–15 ms per
-- neighbour) but the position and total need the same filtered scan
-- regardless, and the one caller that could skip them (a client that already
-- knows its position) is served by the sessionStorage navigation cache
-- (web/lib/navigation-cache.ts) without any server call.
--
-- Returns (NULL, NULL, 0, 0) when the object is not in the filtered set.

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
  -- Same sortable set as get_filtered_objects_paginated (photo_z included).
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
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
  -- COALESCE: array_length('{}',1) is NULL, and a NULL flag would make the
  -- NOT-flag predicate below reject every row instead of treating an empty
  -- selection as no filter (the pre-#491 predicate's explicit behavior).
  v_observation_filter_active := (p_observations IS NOT NULL AND COALESCE(array_length(p_observations, 1), 0) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  WITH filtered AS (
    -- Narrow projection on purpose: this is what gets sorted.
    SELECT
      o.object_id,
      CASE p_sort_column
        WHEN 'object_id' THEN o.object_id WHEN 'field' THEN o.field ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN o.ra WHEN 'dec' THEN o.dec
        WHEN 'redshift' THEN o.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN o.redshift_quality::DOUBLE PRECISION
        WHEN 'n_targets' THEN o.n_targets::DOUBLE PRECISION
        WHEN 'n_spectra' THEN o.n_spectra::DOUBLE PRECISION
        WHEN 'max_snr' THEN o.max_snr WHEN 'max_exposure_time' THEN o.max_exposure_time
        WHEN 'photo_z' THEN o.photo_z
        WHEN 'distance' THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS sort_num
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
  ranked AS (
    SELECT
      f.object_id,
      LAG(f.object_id) OVER w AS prev_id,
      LEAD(f.object_id) OVER w AS next_id,
      ROW_NUMBER() OVER w AS rn,
      -- Same window as the others (LAG/LEAD/ROW_NUMBER ignore the frame), so
      -- all four run in ONE WindowAgg; a separate COUNT(*) OVER () added a
      -- second pass with its own tuplestore, which spilled at prod's 3.5 MB
      -- work_mem.
      COUNT(*) OVER w AS total
    FROM filtered f
    WINDOW w AS (ORDER BY
      CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN f.sort_text END ASC NULLS LAST,
      CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN f.sort_text END DESC NULLS LAST,
      CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN f.sort_num END ASC NULLS LAST,
      CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN f.sort_num END DESC NULLS LAST,
      f.object_id ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
  )
  SELECT r.prev_id, r.next_id, r.rn::BIGINT, r.total::BIGINT
  FROM ranked r
  WHERE r.object_id = p_current_object_id;

  IF NOT FOUND THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
  END IF;
END;
$$;
GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO service_role;


-- =============================================================================
-- get_objects_near
-- =============================================================================
-- The k nearest visible objects to a point, for the object page's "Nearby
-- objects" card and the inspection overlay's nearby list (perf T2-C, #506,
-- audit PP-05). Those cards used to run the 33-parameter list RPC with a
-- coordinate filter — 32 ms on prod for ≤10 rows, most of it the member and
-- list aggregates the cards never render. This is get_nearby_shutters' shape:
-- a box on the (ra, dec) index, the Haversine cut, and only the columns the
-- cards show. The RA half-width is widened by 1/cos(dec) so the box holds the
-- whole cone at any declination. SECURITY INVOKER — RLS on objects gates the
-- rows; p_program_slugs is the caller's accessible set (accessible_program_slugs)
-- passed as a parameter so the planner sees an array, not a per-row function
-- call. No RA-wrap handling, as in the rest of the coordinate RPCs.
CREATE OR REPLACE FUNCTION public.get_objects_near(
  p_ra DOUBLE PRECISION,
  p_dec DOUBLE PRECISION,
  p_radius_degrees DOUBLE PRECISION,
  p_program_slugs TEXT[],
  p_limit INTEGER DEFAULT 10,
  p_exclude_object_id TEXT DEFAULT NULL,
  p_include_unpublished BOOLEAN DEFAULT false
)
RETURNS TABLE(
  id INTEGER,
  object_id TEXT,
  field TEXT,
  ra DOUBLE PRECISION,
  "dec" DOUBLE PRECISION,
  redshift DOUBLE PRECISION,
  redshift_quality INTEGER,
  gratings TEXT[],
  n_spectra INTEGER,
  distance DOUBLE PRECISION
)
LANGUAGE sql STABLE AS $$
  SELECT o.id, o.object_id, o.field, o.ra, o.dec,
         o.redshift::DOUBLE PRECISION, o.redshift_quality, o.gratings, o.n_spectra,
         2 * DEGREES(ASIN(SQRT(
           POWER(SIN(RADIANS(o.dec - p_dec) / 2), 2) +
           COS(RADIANS(p_dec)) * COS(RADIANS(o.dec)) *
           POWER(SIN(RADIANS(o.ra - p_ra) / 2), 2)
         ))) AS distance
  FROM objects o
  WHERE o.programs && p_program_slugs
    AND o.is_active = true
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND o.dec BETWEEN p_dec - p_radius_degrees AND p_dec + p_radius_degrees
    AND o.ra BETWEEN p_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_dec)), 1e-6)
                 AND p_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_dec)), 1e-6)
    AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_dec) / 2), 2) +
          COS(RADIANS(p_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_ra) / 2), 2)
        ))) <= p_radius_degrees
    AND (p_exclude_object_id IS NULL OR o.object_id <> p_exclude_object_id)
  ORDER BY distance ASC, o.object_id ASC
  LIMIT LEAST(GREATEST(COALESCE(p_limit, 10), 1), 100);
$$;
GRANT EXECUTE ON FUNCTION public.get_objects_near TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_near TO service_role;
