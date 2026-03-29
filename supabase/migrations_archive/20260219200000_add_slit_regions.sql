-- Slit regions table for MSA shutter overlay on map viewer
-- One row per shutter rectangle (3 per slit: shutter_idx -1, 0, 1)
-- Dimensions are constant (0.22" x 0.46") and hardcoded on the frontend

CREATE TABLE slit_regions (
  id serial PRIMARY KEY,
  field text NOT NULL,
  observation text NOT NULL,
  object_id text NOT NULL,
  grating text,
  center_ra double precision NOT NULL,
  center_dec double precision NOT NULL,
  position_angle double precision NOT NULL,  -- degrees, sky frame (N through E)
  shutter_idx smallint NOT NULL,             -- -1, 0, 1
  created_at timestamptz DEFAULT now()
);

CREATE INDEX idx_slit_regions_field ON slit_regions(field);

-- RLS: authenticated users can read
ALTER TABLE slit_regions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Authenticated users can view slit regions"
  ON slit_regions FOR SELECT TO authenticated USING (true);
