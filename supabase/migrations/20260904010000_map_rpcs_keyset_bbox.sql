-- Perf T1-6 (#502, epic #515): keyset + viewport-scoped map RPCs.
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/functions.sql / indexes.sql.
--
-- get_field_shutters and get_field_object_markers were paged by the client
-- with PostgREST .range() — LIMIT/OFFSET applied OUTSIDE the set-returning
-- function, so every page re-executed the whole thing (COSMOS shutters: 14
-- sequential full passes with disk sorts, 20 MB; markers 1.3 s prod mean,
-- 8 s max). Both now take a cursor + page size (LIMIT inside), shutters take
-- an optional RA/Dec box, and markers aggregate members for the page only.
-- New trailing parameters with defaults => new overloads, so the previous
-- signatures are dropped first. Plain CREATE INDEX (transactional runner).

DROP FUNCTION IF EXISTS public.get_field_shutters(text);
DROP FUNCTION IF EXISTS public.get_field_object_markers(text, boolean);

CREATE OR REPLACE FUNCTION public.get_field_shutters(
  p_field TEXT,
  p_cursor INTEGER DEFAULT NULL,
  p_page_size INTEGER DEFAULT 5000,
  p_ra_min DOUBLE PRECISION DEFAULT NULL,
  p_ra_max DOUBLE PRECISION DEFAULT NULL,
  p_dec_min DOUBLE PRECISION DEFAULT NULL,
  p_dec_max DOUBLE PRECISION DEFAULT NULL
)
RETURNS TABLE (
  id               INTEGER,
  object_id        TEXT,
  source_id        INTEGER,
  center_ra        DOUBLE PRECISION,
  center_dec       DOUBLE PRECISION,
  position_angle   DOUBLE PRECISION,
  shutter_idx      SMALLINT,
  dither_id        SMALLINT,
  shutter_state    TEXT,
  observation      TEXT,
  aperture_name           TEXT,
  aperture_width_arcsec    DOUBLE PRECISION,
  aperture_height_arcsec   DOUBLE PRECISION
)
LANGUAGE sql STABLE
AS $$
  SELECT s.id, s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  FROM public.shutters s
  WHERE s.field = p_field
    AND (p_cursor IS NULL OR s.id > p_cursor)
    AND (p_ra_min IS NULL OR p_ra_max IS NULL OR s.center_ra BETWEEN p_ra_min AND p_ra_max)
    AND (p_dec_min IS NULL OR p_dec_max IS NULL OR s.center_dec BETWEEN p_dec_min AND p_dec_max)
  ORDER BY s.id
  LIMIT GREATEST(1, LEAST(COALESCE(p_page_size, 5000), 50000));
$$;

GRANT EXECUTE ON FUNCTION public.get_field_shutters TO authenticated;

CREATE OR REPLACE FUNCTION public.get_field_object_markers(
  p_field TEXT,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_cursor TEXT DEFAULT NULL,
  p_page_size INTEGER DEFAULT 5000
)
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
  -- programs, n_targets, n_spectra and member_target_ids are scoped to the
  -- caller's accessible programs so mixed-program objects don't leak proprietary
  -- member metadata on the map. redshift / redshift_quality stay visible (object
  -- science, per the access policy). Object row visibility is enforced by RLS
  -- (programs && accessible); this function is SECURITY INVOKER and additionally
  -- gates the member CTEs by an explicit accessible_program_slugs() filter.
  -- The logic mirrors object_scoped_aggregates(), set-based over the page.
  WITH acc AS (
    SELECT public.accessible_program_slugs() AS slugs
  ),
  page AS (
    SELECT o.id, o.object_id, o.ra, o.dec, o.redshift, o.redshift_quality, o.field
    FROM public.objects o
    CROSS JOIN acc
    WHERE o.field = p_field
      AND o.is_active
      AND (p_cursor IS NULL OR o.object_id > p_cursor)
      -- Same membership rule as the old inner JOIN: an object appears only if
      -- it has >= 1 visible member (accessible program; B1: contributing a
      -- published spectrum unless the admin opts in). Applied BEFORE the
      -- LIMIT so a page is never short for a reason other than exhaustion.
      AND EXISTS (
        SELECT 1 FROM public.targets t
        WHERE t.object_id = o.id
          AND t.program_slug = ANY(acc.slugs)
          AND (p_include_unpublished OR t.has_published_spectrum)
      )
    ORDER BY o.object_id
    LIMIT GREATEST(1, LEAST(COALESCE(p_page_size, 5000), 50000))
  ),
  mt AS (
    SELECT t.object_id,
           array_agg(t.target_id ORDER BY t.target_id)              AS member_target_ids,
           array_agg(DISTINCT t.program_slug ORDER BY t.program_slug) AS programs,
           COUNT(*)::int                                            AS n_targets
    FROM public.targets t
    CROSS JOIN acc
    WHERE t.object_id IN (SELECT p.id FROM page p)
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR t.has_published_spectrum)
    GROUP BY t.object_id
  ),
  sp AS (
    SELECT t.object_id, COUNT(*)::int AS n_spectra
    FROM public.spectra s
    JOIN public.targets t ON t.target_id = s.target_id
    CROSS JOIN acc
    WHERE t.object_id IN (SELECT p.id FROM page p)
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
    GROUP BY t.object_id
  )
  SELECT
    p.object_id,
    p.ra,
    p.dec,
    p.redshift::double precision,
    p.redshift_quality,
    p.field,
    COALESCE(mt.n_targets, 0)                       AS n_targets,
    COALESCE(sp.n_spectra, 0)                       AS n_spectra,
    COALESCE(mt.programs, ARRAY[]::TEXT[])          AS programs,
    COALESCE(mt.member_target_ids, ARRAY[]::TEXT[]) AS member_target_ids
  FROM page p
  LEFT JOIN mt ON mt.object_id = p.id
  LEFT JOIN sp ON sp.object_id = p.id
  ORDER BY p.object_id;
$$;

GRANT EXECUTE ON FUNCTION public.get_field_object_markers TO authenticated;

CREATE INDEX IF NOT EXISTS idx_shutters_field_id
    ON public.shutters USING btree (field, id);

CREATE INDEX IF NOT EXISTS idx_objects_field_object_id
    ON public.objects USING btree (field, object_id);
