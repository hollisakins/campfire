-- Map viewer performance: replace paginated table queries with single-shot
-- RPCs. The previous PostgREST select on objects/shutters paginated at
-- 1000 rows/page (sequential round-trips) and embedded targets(target_id)
-- for the slit-filter bridge — both very expensive on COSMOS-sized fields
-- (~10K objects, ~30-50K shutters → 2-3 min total). The RPCs return all
-- rows in one call and aggregate member_target_ids server-side.

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
