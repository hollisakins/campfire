-- =============================================================================
-- Migration: page-first evaluation for get_csv_export_objects (issue #490)
-- =============================================================================
-- The objects-view CSV export ran its expensive per-row work — the
-- object_scoped_aggregates and photometry laterals, plus member_targets /
-- visible_lists CTEs aggregated over the entire catalog — for every candidate
-- row BEFORE keyset pagination applied the LIMIT, so per-page latency scaled
-- with catalog size instead of page size (~1.3s+ per 5000-row page at 40k
-- objects; worse as filters got sparser). The function now selects the page of
-- ids first (cheap objects-only filters + keyset + LIMIT, with the exact
-- haversine cut folded into the page selection) and joins the expensive work
-- against just that page. Output rows and columns are unchanged — verified by
-- diffing full paginated exports old-vs-new across 24 filter scenarios on a
-- seeded 40k-object catalog.
--
-- Two planner-facing fixes ride along:
--   * object_scoped_aggregates is declared ROWS 1 (it always returns exactly
--     one row); the SRF default of 1000 inflated every lateral call site's
--     row estimates ~1000x.
--   * get_csv_export_objects sets jit = off: force_custom_plan replans every
--     call, so JIT compilation (triggered by the inflated estimates) was pure
--     per-call overhead (~800ms/page).
--
-- Function bodies are verbatim copies of supabase/schemas/functions.sql.
-- =============================================================================

-- =============================================================================
-- object_scoped_aggregates
-- =============================================================================
-- Viewer-scoped recompute of an object's aggregate columns.
--
-- The objects table stores aggregate columns (programs, gratings, observations,
-- n_targets, n_spectra, max_snr, max_exposure_time) computed across ALL member
-- targets at deploy time (see python/campfire/deploy/objects.py). Object row
-- visibility is granted by array overlap (policies.sql: programs && accessible),
-- so a viewer who can access only SOME member programs still sees the row — and
-- the stored aggregates would leak the existence/metadata of proprietary members
-- they cannot access (programs[] names them; counts/snr/exposure quantify them).
--
-- This helper recomputes those aggregates restricted to p_program_slugs, using
-- the SAME semantics as the deploy-time builder so that a full-access viewer
-- (p_program_slugs ⊇ the object's programs) gets values identical to the stored
-- columns. Member-level payloads (member_targets, spectra) are already filtered
-- in each read RPC; this covers the object's own columns.
--
-- Callers MUST pass an already-access-checked slug array (the caller's accessible
-- set, optionally narrowed by an active program filter). Do not rely on RLS
-- inside this function: it is SECURITY INVOKER, but when called from a
-- SECURITY DEFINER context it would run as the owner with RLS bypassed — the
-- explicit p_program_slugs filter is the access gate.
DROP FUNCTION IF EXISTS public.object_scoped_aggregates(INTEGER, TEXT[]);

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
-- Exactly one row per call, always. Without ROWS 1 the planner assumes the
-- SRF default of 1000 rows per call, which multiplies through every
-- `LEFT JOIN LATERAL object_scoped_aggregates(...)` call site and inflates
-- those plans' cost estimates ~1000x (issue #490: enough to trip per-call JIT
-- compilation on the CSV export).
ROWS 1
AS $$
BEGIN
  -- Fast path (perf, issue #103): when the caller can access every program this
  -- object belongs to AND unpublished members are excluded, the viewer-scoped
  -- recompute below is provably identical to the aggregate columns already
  -- stored on the object -- both are the deploy-time builder's aggregation over
  -- published members (reconcile_field_objects keeps the stored columns in
  -- lockstep with the object row, and targets/spectra only change at deploy).
  -- Restricting the recompute to a superset of the object's programs drops
  -- nothing, so `o.programs <@ p_program_slugs` is exactly the "recompute ==
  -- stored" condition. Returning the stored columns via a single PK lookup skips
  -- the per-row targets+spectra scans that dominated get_objects_for_sync (and
  -- the catalog list RPCs) at scale. Partial-access or draft-inclusive callers
  -- fall through to the recompute, preserving the anti-leak scoping (see the
  -- header comment and supabase/tests/check_object_aggregate_scoping.sql).
  IF NOT p_include_unpublished THEN
    RETURN QUERY
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM objects o
    WHERE o.id = p_object_id
      AND o.programs <@ p_program_slugs;
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

DROP FUNCTION IF EXISTS public.get_csv_export_objects;

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
-- force_custom_plan replans (and would re-JIT) on every call, so JIT
-- compilation is pure per-call overhead here — ~800ms/page when the CTE join
-- misestimates push the plan cost over the JIT thresholds (issue #490).
SET jit = 'off'
SET statement_timeout = '120s'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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

  RETURN QUERY
  -- Issue #490: page-first evaluation. The previous shape ran the per-row
  -- laterals (object_scoped_aggregates, photometry) for every row that passed
  -- the cheap filters — before the LIMIT — and the member_targets /
  -- visible_lists CTEs aggregated over the whole catalog, so per-page cost
  -- scaled with catalog size instead of page size. Select the page of ids
  -- first (cheap objects-only filters + keyset + LIMIT), then join the
  -- expensive work against just that page. MATERIALIZED fences the page so
  -- the planner can't push the laterals back under the LIMIT.
  WITH page_objects AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, o.last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.version,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi
    FROM objects o
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
        -- Exact haversine cut lives inside the page selection (it used to be a
        -- post-CTE distance_filtered pass) so the LIMIT counts only surviving
        -- rows and the keyset cursor stays correct.
        AND 2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)))) <= p_radius_degrees
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
    ORDER BY o.object_id ASC
    LIMIT v_page_size
  ),
  -- Both aggregation CTEs are restricted to the page's ids — previously they
  -- aggregated targets / list memberships for the entire catalog on every page.
  member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM targets t
    WHERE t.object_id IN (SELECT po.id FROM page_objects po)
      AND t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT po.id FROM page_objects po)
      AND (ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid()))
    GROUP BY olm.object_id
  )
  SELECT po.object_id, po.field, po.ra, po.dec,
    po.redshift, po.redshift_quality,
    po.redshift_inspected, po.redshift_auto,
    po.last_inspected_at, up.full_name AS last_inspected_by,
    po.last_data_change_at, po.staleness_reason, po.version,
    -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
    -- objects don't export proprietary member metadata. See
    -- object_scoped_aggregates().
    sa.n_targets, sa.n_spectra,
    array_to_string(sa.programs, ';') AS programs,
    array_to_string(sa.gratings, ';') AS gratings,
    sa.max_snr, sa.max_exposure_time,
    mt.member_target_ids, po.distance, vl.lists,
    po.has_photometry, po.photo_z, po.photo_z_err_lo, po.photo_z_err_hi,
    phot.photometry
  FROM page_objects po
  LEFT JOIN member_targets mt ON mt.object_id = po.id
  LEFT JOIN visible_lists vl ON vl.object_id = po.id
  LEFT JOIN user_profiles up ON up.user_id = po.last_inspected_by
  LEFT JOIN LATERAL public.object_scoped_aggregates(po.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
  LEFT JOIN LATERAL (
    SELECT op.photometry FROM object_photometry op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_objects TO authenticated;
