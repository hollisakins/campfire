-- Add JSONB pointings column to observations and surface it via get_observation_stats.
-- Each observation's pointings array contains one entry per NIRSpec MSA pointing
-- (grouped by MSAMETID + nominal pointing center) with geometry, exposure
-- aggregates, and a 4-quadrant sky footprint. Populated at deploy time from
-- {obs_name}_pointings.ecsv.

alter table "public"."observations" add column "pointings" jsonb;

drop function if exists "public"."get_observation_stats"(p_program_slugs text[]);

create or replace function "public"."get_observation_stats"(p_program_slugs text[])
returns table(
  observation text, program_slug text, program_name text, field text,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  pointings jsonb
) language sql stable as $$
  select t.observation, t.program_slug, p.program_name, t.field,
    count(distinct t.target_id) as target_count,
    count(s.id) as spectrum_count,
    coalesce(sum(s.file_size), 0)::bigint as total_size_bytes,
    o.pointings
  from targets t
  join programs p on p.slug = t.program_slug
  left join spectra s on s.target_id = t.target_id
  left join observations o on o.name = t.observation
  where t.program_slug = any(p_program_slugs)
  group by t.observation, t.program_slug, p.program_name, t.field, o.pointings
  order by t.observation;
$$;

grant execute on function "public"."get_observation_stats"(p_program_slugs text[]) to authenticated;
grant execute on function "public"."get_observation_stats"(p_program_slugs text[]) to anon;
grant execute on function "public"."get_observation_stats"(p_program_slugs text[]) to service_role;
