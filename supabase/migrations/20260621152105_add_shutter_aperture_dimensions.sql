-- Add per-aperture geometry to the shutters table so the web overlay can render
-- NIRSpec fixed-slit apertures (e.g. S200A2 = 0.2"x3.2") as well as MSA shutters
-- from data, instead of hardcoding 0.22"x0.46" in the frontend.
--
-- Defaults match the MSA dimensions the frontend previously hardcoded, so every
-- existing (MSA) row renders unchanged with no re-deploy.
alter table "public"."shutters"
  add column "aperture_name" text not null default 'MSA',
  add column "aperture_width_arcsec" double precision not null default 0.22,
  add column "aperture_height_arcsec" double precision not null default 0.46;

-- get_nearby_shutters now returns the aperture columns. The RETURNS TABLE
-- signature changes, so the function must be dropped and recreated (CREATE OR
-- REPLACE cannot change a function's output columns).
drop function if exists public.get_nearby_shutters(
  double precision, double precision, double precision, text
);

create or replace function public.get_nearby_shutters(
  p_ra double precision,
  p_dec double precision,
  p_radius_arcsec double precision default 5.0,
  p_field text default null
)
returns table (
  object_id text,
  source_id integer,
  center_ra double precision,
  center_dec double precision,
  position_angle double precision,
  shutter_idx smallint,
  dither_id smallint,
  shutter_state text,
  observation text,
  aperture_name text,
  aperture_width_arcsec double precision,
  aperture_height_arcsec double precision
)
language sql stable as $$
  select s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  from shutters s
  where (p_field is null or s.field = p_field)
    and s.center_ra between p_ra - p_radius_arcsec / 3600.0 / cos(radians(p_dec))
                        and p_ra + p_radius_arcsec / 3600.0 / cos(radians(p_dec))
    and s.center_dec between p_dec - p_radius_arcsec / 3600.0
                         and p_dec + p_radius_arcsec / 3600.0;
$$;

grant all on function public.get_nearby_shutters(double precision, double precision, double precision, text) to anon;
grant all on function public.get_nearby_shutters(double precision, double precision, double precision, text) to authenticated;
grant all on function public.get_nearby_shutters(double precision, double precision, double precision, text) to service_role;

-- get_field_shutters (full map viewer) returns the aperture columns too.
drop function if exists public.get_field_shutters(text);

create or replace function public.get_field_shutters(p_field text)
returns table (
  object_id        text,
  source_id        integer,
  center_ra        double precision,
  center_dec       double precision,
  position_angle   double precision,
  shutter_idx      smallint,
  dither_id        smallint,
  shutter_state    text,
  observation      text,
  aperture_name           text,
  aperture_width_arcsec    double precision,
  aperture_height_arcsec   double precision
)
language sql stable as $$
  select s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  from public.shutters s
  where s.field = p_field
  order by s.object_id;
$$;

grant execute on function public.get_field_shutters(text) to authenticated;
