-- Shutters table for MSA shutter overlay on map viewer and spectra detail page.
-- One row per unique shutter sky position after deduplication across gratings
-- and overlapping nod positions. Dimensions are constant (0.22" x 0.46") and
-- hardcoded on the frontend.

CREATE TABLE shutters (
  id serial PRIMARY KEY,
  field text NOT NULL,
  observation text NOT NULL,
  object_id text NOT NULL,
  source_id integer NOT NULL,
  center_ra double precision NOT NULL,
  center_dec double precision NOT NULL,
  position_angle double precision NOT NULL,  -- degrees, sky frame (N through E)
  shutter_idx smallint NOT NULL,             -- relative to source shutter (source = 0)
  dither_id smallint NOT NULL DEFAULT 0,     -- sequential index per (object_id, shutter_idx)
  shutter_state text NOT NULL DEFAULT 'open', -- 'source' or 'open'
  created_at timestamptz DEFAULT now()
);

CREATE INDEX idx_shutters_field ON shutters(field);
CREATE INDEX idx_shutters_observation ON shutters(observation);
CREATE INDEX idx_shutters_object_id ON shutters(object_id);
CREATE INDEX idx_shutters_ra_dec ON shutters(center_ra, center_dec);

-- RLS: authenticated users can read
ALTER TABLE shutters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Authenticated users can view shutters"
  ON shutters FOR SELECT TO authenticated USING (true);

-- RPC for nearby shutter queries (used by spectra detail page cutout)
CREATE OR REPLACE FUNCTION get_nearby_shutters(
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
