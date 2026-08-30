-- Add review_decided_at to get_admin_exposures so the web client's
-- review-decision overlay can compare its acked/queued stamps against list
-- rows. Without it every list row arrives stampless and a session's older
-- acked decision could shadow a newer decision made from another device.
--
-- Changing a RETURNS TABLE shape requires drop + recreate (CREATE OR REPLACE
-- cannot alter a function's return type); the GRANT is re-applied because it
-- drops with the function.
DROP FUNCTION IF EXISTS public.get_admin_exposures(text, text, text, text, text, text, text, text, integer, integer);

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
  p_page_size integer DEFAULT 50
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
