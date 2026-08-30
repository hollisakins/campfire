-- Wildcard filename search for the admin NIRCam exposure list: add p_search
-- (an ILIKE pattern applied to filename) to get_admin_exposures and
-- get_admin_exposure_neighbors. Callers pre-translate the operator's glob
-- ("jw01727001*" → 'jw01727001%') in web/lib/admin/exposure-search.ts, so the
-- list, the detail page's prev/next nav, and the PNG-manifest route all agree
-- on what matches. Composes with every existing filter (AND semantics).
--
-- Adding a parameter changes the function signature, and CREATE OR REPLACE
-- with a different argument list would create an ambiguous overload instead
-- of replacing — so drop + recreate (house precedent: 20260817213000). The
-- GRANTs are re-applied because they drop with the functions.
DROP FUNCTION IF EXISTS public.get_admin_exposures(text, text, text, text, text, text, text, text, integer, integer);
DROP FUNCTION IF EXISTS public.get_admin_exposure_neighbors(integer, text, text, text, text, text, text, text, text, integer);

CREATE OR REPLACE FUNCTION public.get_admin_exposures(
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',   -- 'filename' = the compound (field, filter, filename) list order
  p_sort_direction text DEFAULT 'asc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50,
  p_search text DEFAULT NULL               -- ILIKE pattern on filename; callers pre-translate
                                           -- the operator's glob (web/lib/admin/exposure-search.ts)
)
RETURNS TABLE (
  id integer,
  field text,
  filter text,
  detector text,
  filename text,
  visit text,
  date_obs timestamp without time zone,
  ra_center double precision,
  dec_center double precision,
  stage text,
  review_status text,
  correction text,
  png_path text,
  full_png_path text,
  image_width integer,
  image_height integer,
  mask_regions jsonb,
  notes text,
  review_decided_at timestamptz,
  created_at timestamp without time zone,
  updated_at timestamp without time zone,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  SELECT e.id, e.field, e.filter, e.detector, e.filename, e.visit, e.date_obs,
         e.ra_center, e.dec_center, e.stage, e.review_status,
         e.correction, e.png_path, e.full_png_path, e.image_width,
         e.image_height, e.mask_regions, e.notes, e.review_decided_at,
         e.created_at, e.updated_at,
         count(*) OVER ()
  FROM nircam_exposures e
  WHERE (p_field IS NULL OR e.field = p_field)
    AND (p_filter IS NULL OR e.filter = p_filter)
    AND (p_detector IS NULL OR e.detector = p_detector)
    AND (p_review_status IS NULL OR e.review_status = p_review_status)
    AND (p_stage IS NULL OR e.stage = p_stage)
    AND (p_correction IS NULL OR e.correction = p_correction)
    AND (p_search IS NULL OR p_search = '' OR e.filename ILIKE p_search)
  ORDER BY
    -- Keep in lockstep with get_admin_exposure_neighbors.
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
    e.id ASC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposures TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_exposure_neighbors(
  p_current_id integer,
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',
  p_sort_direction text DEFAULT 'asc',
  p_window integer DEFAULT 3,
  p_search text DEFAULT NULL   -- ILIKE pattern on filename; same contract as get_admin_exposures
)
RETURNS TABLE (
  id integer,
  nav_position bigint,   -- 1-based rank in the filtered set ("position" is reserved)
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_window integer := LEAST(GREATEST(COALESCE(p_window, 3), 1), 25);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  WITH ranked AS (
    SELECT e.id AS exp_id,
           row_number() OVER (ORDER BY
             -- Keep in lockstep with get_admin_exposures.
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
             e.id ASC
           ) AS rn,
           count(*) OVER () AS n
    FROM nircam_exposures e
    WHERE (p_field IS NULL OR e.field = p_field)
      AND (p_filter IS NULL OR e.filter = p_filter)
      AND (p_detector IS NULL OR e.detector = p_detector)
      AND (p_review_status IS NULL OR e.review_status = p_review_status)
      AND (p_stage IS NULL OR e.stage = p_stage)
        AND (p_correction IS NULL OR e.correction = p_correction)
        AND (p_search IS NULL OR p_search = '' OR e.filename ILIKE p_search)
  ),
  cur AS (
    SELECT r.rn AS rn0 FROM ranked r WHERE r.exp_id = p_current_id
  )
  SELECT r.exp_id, r.rn, r.n
  FROM ranked r, cur
  WHERE r.rn BETWEEN cur.rn0 - v_window AND cur.rn0 + v_window
  ORDER BY r.rn;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposure_neighbors TO authenticated;
