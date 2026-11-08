SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- get_program_exposures: an observation with no published spectrum (a draft
-- deploy) is listed only to admins and drafts links, like every other catalog read.

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_program_exposures (
  p_program_slug text
)
  RETURNS TABLE (
    observation        text,
    exposure_root      text,
    detector           text,
    grating            text,
    filter             text,
    date_obs           text,
    exposure_time      double precision,
    readout_pattern    text,
    n_groups           integer,
    n_integrations     integer,
    dither_position    integer,
    n_dither_positions integer,
    mbkg_source        text
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  SELECT r.observation, r.exposure_root, r.detector, r.grating, r.filter,
         r.date_obs, r.exposure_time, r.readout_pattern,
         r.n_groups, r.n_integrations, r.dither_position,
         r.n_dither_positions, r.mbkg_source
  FROM public.nirspec_rate_exposures r
  JOIN public.observations o ON o.name = r.observation
  WHERE o.program_slug = p_program_slug
    AND p_program_slug = ANY(public.accessible_program_slugs())
    AND (NOT public.is_link_account() OR r.observation = public.link_observation())
    AND (public.is_admin() OR public.link_sees_drafts()
         OR EXISTS (SELECT 1 FROM public.spectra s
                    WHERE s.observation = r.observation AND s.deploy_status = 'published'))
  ORDER BY r.observation, r.exposure_root, r.detector;
$function$;
