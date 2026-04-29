-- =============================================================================
-- Post-review hardening for the object-centric migration
-- =============================================================================
-- Three fixes that surfaced during PR review of `feat/overhaul-objects`:
--
--  1. Add foreign keys on `flag_audit_log.object_id` and `spectrum_id` so
--     PostgREST embedded selects (used by `/api/admin/activity`) can resolve
--     the relationship. ON DELETE CASCADE matches the existing target FK
--     and also cleans up audit rows when an admin hard-deletes an inactive
--     object via `/api/admin/objects/inactive`.
--
--  2. Add a column-scope trigger on `objects` so non-admin callers hitting
--     PostgREST directly (i.e. bypassing the API layer) cannot write
--     arbitrary columns under the permissive `update_objects_by_access`
--     policy. Service role and admins pass through unchanged. Also add a
--     WITH CHECK mirror to the RLS policy so a row can't be moved out of
--     the caller's program scope.
--
--  3. Extend `get_filtered_spectra_paginated` and `get_csv_export_spectra`
--     to accept `redshift_auto` as a sort column (the spectra table header
--     advertises it as sortable, but the RPC previously didn't honor it).
-- =============================================================================


-- 1. flag_audit_log FKs ------------------------------------------------------

alter table "public"."flag_audit_log"
  add constraint "flag_audit_log_object_id_fkey"
  foreign key ("object_id") references "public"."objects"("id") on delete cascade;

alter table "public"."flag_audit_log"
  add constraint "flag_audit_log_spectrum_id_fkey"
  foreign key ("spectrum_id") references "public"."spectra"("id") on delete cascade;


-- 2. Column-scope trigger + RLS WITH CHECK -----------------------------------

drop function if exists public.enforce_object_user_update_scope cascade;

create or replace function public.enforce_object_user_update_scope() returns trigger
language plpgsql security definer
as $$
begin
    -- Service role (no JWT) and admins can write any column.
    if auth.uid() is null or public.is_admin() then
        return new;
    end if;

    -- Non-admin users may only touch the inspection set:
    --   redshift_inspected, redshift_quality, last_inspected_at,
    --   last_inspected_by. version and updated_at are maintained by sibling
    --   triggers; we explicitly allow them to change so this trigger
    --   doesn't reject writes that went through the legitimate path.
    if old.object_id is distinct from new.object_id
       or old.field is distinct from new.field
       or old.ra is distinct from new.ra
       or old.dec is distinct from new.dec
       or old.n_targets is distinct from new.n_targets
       or old.n_spectra is distinct from new.n_spectra
       or old.programs is distinct from new.programs
       or old.gratings is distinct from new.gratings
       or old.observations is distinct from new.observations
       or old.max_snr is distinct from new.max_snr
       or old.max_exposure_time is distinct from new.max_exposure_time
       or old.best_redshift is distinct from new.best_redshift
       or old.best_redshift_quality is distinct from new.best_redshift_quality
       or old.photo_z is distinct from new.photo_z
       or old.photo_z_err_lo is distinct from new.photo_z_err_lo
       or old.photo_z_err_hi is distinct from new.photo_z_err_hi
       or old.has_photometry is distinct from new.has_photometry
       or old.redshift_auto is distinct from new.redshift_auto
       or old.last_data_change_at is distinct from new.last_data_change_at
       or old.staleness_reason is distinct from new.staleness_reason
       or old.is_active is distinct from new.is_active
       or old.created_at is distinct from new.created_at
    then
        raise exception 'Non-admin updates to objects may only change inspection fields (redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by)'
            using errcode = '42501';
    end if;

    return new;
end;
$$;

drop trigger if exists enforce_object_user_update_scope_trigger on public.objects;
create trigger enforce_object_user_update_scope_trigger
  before update on public.objects
  for each row execute function public.enforce_object_user_update_scope();

-- Add WITH CHECK to update_objects_by_access so a row can't be moved out of
-- the caller's program access mid-update.
drop policy if exists "update_objects_by_access" on objects;
create policy "update_objects_by_access"
  on objects for update
  using (
    programs && public.accessible_program_slugs()
    and public.can_comment()
  )
  with check (
    programs && public.accessible_program_slugs()
    and public.can_comment()
  );


-- 3. redshift_auto sort in spectra RPCs --------------------------------------

set check_function_bodies = off;

create or replace function public.get_filtered_spectra_paginated(
  p_program_slugs text[],
  p_filter_programs text[] default null,
  p_fields text[] default null,
  p_gratings text[] default null,
  p_gratings_mode text default 'any',
  p_observations text[] default null,
  p_redshift_quality integer[] default null,
  p_redshift_min double precision default null,
  p_redshift_max double precision default null,
  p_max_snr_min double precision default null,
  p_max_snr_max double precision default null,
  p_max_exposure_time_min double precision default null,
  p_max_exposure_time_max double precision default null,
  p_dq_flags_include_any integer default null,
  p_dq_flags_include_all integer default null,
  p_dq_flags_exclude integer default null,
  p_list_ids integer[] default null,
  p_search text default null,
  p_inspected_only boolean default null,
  p_has_photometry boolean default null,
  p_comment_search text default null,
  p_comment_search_scope text default null,
  p_comment_user_id uuid default null,
  p_coord_ra double precision default null,
  p_coord_dec double precision default null,
  p_radius_degrees double precision default null,
  p_sort_column text default 'target_id',
  p_sort_direction text default 'asc',
  p_page integer default 1,
  p_page_size integer default 50,
  p_include_thumbnails boolean default false
)
returns table(targets jsonb, total_count bigint, page integer, page_size integer)
language plpgsql stable
set plan_cache_mode to 'force_custom_plan'
as $function$
declare
  v_filtered_program_slugs text[];
  v_coord_search_active boolean;
  v_comment_search_active boolean;
  v_grating_filter_active boolean;
  v_gratings_mode text;
  v_offset integer;
begin
  v_coord_search_active := (p_coord_ra is not null and p_coord_dec is not null and p_radius_degrees is not null);
  v_comment_search_active := (p_comment_search is not null and p_comment_search != '' and p_comment_search_scope in ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings is not null and array_length(p_gratings, 1) > 0);
  v_gratings_mode := coalesce(p_gratings_mode, 'any');
  if v_gratings_mode not in ('any', 'all', 'none') then v_gratings_mode := 'any'; end if;
  if p_sort_direction not in ('asc', 'desc') then p_sort_direction := 'asc'; end if;
  if not (p_sort_column in (
    'target_id', 'spectrum_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) or (p_sort_column = 'distance' and v_coord_search_active)) then
    p_sort_column := 'spectrum_id';
  end if;
  if v_coord_search_active and p_sort_column in ('target_id', 'spectrum_id') and p_sort_direction = 'asc' then
    p_sort_column := 'distance';
  end if;
  v_offset := (coalesce(p_page, 1) - 1) * coalesce(p_page_size, 50);

  if p_filter_programs is not null and array_length(p_filter_programs, 1) > 0 then
    select array(select unnest(p_program_slugs) intersect select unnest(p_filter_programs))
    into v_filtered_program_slugs;
  else
    v_filtered_program_slugs := p_program_slugs;
  end if;

  if v_filtered_program_slugs is null or array_length(v_filtered_program_slugs, 1) is null then
    return query select '[]'::jsonb, 0::bigint, p_page, p_page_size;
    return;
  end if;

  return query
  with filtered_spectra as (
    select
      t.id as tgt_db_id, t.target_id, t.program_slug, t.field, t.observation, t.ra, t.dec,
      o.redshift, o.redshift_quality, o.redshift_inspected,
      o.last_inspected_at, o.last_inspected_by,
      o.is_active as object_is_active, o.has_photometry as object_has_photometry,
      o.object_id as parent_object_id,
      t.max_snr, t.max_exposure_time, t.created_at, t.updated_at,
      s.id as spectrum_pk, s.spectrum_id, s.grating, s.fits_path,
      s.signal_to_noise, s.exposure_time, s.redshift_auto,
      coalesce(s.dq_flags, 0) as dq_flags,
      s.file_hash, s.file_size, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda,
      case when v_coord_search_active then
        2 * degrees(asin(sqrt(
          power(sin(radians(t.dec - p_coord_dec) / 2), 2) +
          cos(radians(p_coord_dec)) * cos(radians(t.dec)) *
          power(sin(radians(t.ra - p_coord_ra) / 2), 2)
        )))
      else null end as distance
    from targets t
    join spectra s on s.target_id = t.target_id
    left join objects o on o.id = t.object_id
    where t.program_slug = any(v_filtered_program_slugs)
      and (o.id is null or o.is_active = true)
      and (not v_grating_filter_active or s.grating = any(p_gratings))
      and (p_fields is null or array_length(p_fields, 1) is null or t.field = any(p_fields))
      and (p_observations is null or array_length(p_observations, 1) is null or t.observation = any(p_observations))
      and (p_redshift_quality is null or array_length(p_redshift_quality, 1) is null or o.redshift_quality = any(p_redshift_quality))
      and (p_redshift_min is null or o.redshift >= p_redshift_min)
      and (p_redshift_max is null or o.redshift <= p_redshift_max)
      and (p_max_snr_min is null or s.signal_to_noise >= p_max_snr_min)
      and (p_max_snr_max is null or s.signal_to_noise <= p_max_snr_max)
      and (p_max_exposure_time_min is null or s.exposure_time >= p_max_exposure_time_min)
      and (p_max_exposure_time_max is null or s.exposure_time <= p_max_exposure_time_max)
      and (p_dq_flags_include_any is null or (coalesce(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      and (p_dq_flags_include_all is null or (coalesce(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      and (p_dq_flags_exclude is null or (coalesce(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      and (p_list_ids is null or array_length(p_list_ids, 1) is null or t.object_id in (
          select olm.object_id from object_list_members olm where olm.list_id = any(p_list_ids) and olm.object_id is not null
      ))
      and (p_search is null
           or t.target_id ilike '%' || p_search || '%'
           or s.spectrum_id ilike '%' || p_search || '%')
      and (p_inspected_only is null
           or (p_inspected_only = true and o.redshift_quality > 0)
           or (p_inspected_only = false and coalesce(o.redshift_quality, 0) = 0))
      and (p_has_photometry is null or o.has_photometry = p_has_photometry)
      and (not v_comment_search_active or exists (
        select 1 from comments c where c.target_id = t.id and c.is_deleted = false
          and c.content ilike '%' || p_comment_search || '%'
          and (p_comment_search_scope = 'everyone' or (p_comment_search_scope = 'just_me' and c.user_id = p_comment_user_id))))
      and (not v_coord_search_active or (
        t.ra between (p_coord_ra - p_radius_degrees) and (p_coord_ra + p_radius_degrees)
        and t.dec between (p_coord_dec - p_radius_degrees) and (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered as (
    select fs.* from filtered_spectra fs
    where not v_coord_search_active or fs.distance <= p_radius_degrees
  ),
  page_rows as (
    select *, row_number() over () as row_num
    from (
      select * from distance_filtered
      order by
        case when p_sort_column = 'distance' and p_sort_direction = 'asc' then distance end asc nulls last,
        case when p_sort_column = 'distance' and p_sort_direction = 'desc' then distance end desc nulls last,
        case when p_sort_column = 'target_id' and p_sort_direction = 'asc' then target_id end asc nulls last,
        case when p_sort_column = 'target_id' and p_sort_direction = 'desc' then target_id end desc nulls last,
        case when p_sort_column = 'spectrum_id' and p_sort_direction = 'asc' then spectrum_id end asc nulls last,
        case when p_sort_column = 'spectrum_id' and p_sort_direction = 'desc' then spectrum_id end desc nulls last,
        case when p_sort_column = 'field' and p_sort_direction = 'asc' then field end asc nulls last,
        case when p_sort_column = 'field' and p_sort_direction = 'desc' then field end desc nulls last,
        case when p_sort_column = 'observation' and p_sort_direction = 'asc' then observation end asc nulls last,
        case when p_sort_column = 'observation' and p_sort_direction = 'desc' then observation end desc nulls last,
        case when p_sort_column = 'ra' and p_sort_direction = 'asc' then ra end asc nulls last,
        case when p_sort_column = 'ra' and p_sort_direction = 'desc' then ra end desc nulls last,
        case when p_sort_column = 'dec' and p_sort_direction = 'asc' then "dec" end asc nulls last,
        case when p_sort_column = 'dec' and p_sort_direction = 'desc' then "dec" end desc nulls last,
        case when p_sort_column = 'redshift' and p_sort_direction = 'asc' then redshift end asc nulls last,
        case when p_sort_column = 'redshift' and p_sort_direction = 'desc' then redshift end desc nulls last,
        case when p_sort_column = 'redshift_quality' and p_sort_direction = 'asc' then redshift_quality end asc nulls last,
        case when p_sort_column = 'redshift_quality' and p_sort_direction = 'desc' then redshift_quality end desc nulls last,
        case when p_sort_column = 'redshift_auto' and p_sort_direction = 'asc' then redshift_auto end asc nulls last,
        case when p_sort_column = 'redshift_auto' and p_sort_direction = 'desc' then redshift_auto end desc nulls last,
        case when p_sort_column = 'signal_to_noise' and p_sort_direction = 'asc' then signal_to_noise end asc nulls last,
        case when p_sort_column = 'signal_to_noise' and p_sort_direction = 'desc' then signal_to_noise end desc nulls last,
        case when p_sort_column = 'exposure_time' and p_sort_direction = 'asc' then exposure_time end asc nulls last,
        case when p_sort_column = 'exposure_time' and p_sort_direction = 'desc' then exposure_time end desc nulls last,
        case when p_sort_column = 'grating' and p_sort_direction = 'asc' then grating end asc nulls last,
        case when p_sort_column = 'grating' and p_sort_direction = 'desc' then grating end desc nulls last,
        target_id asc, grating asc
      limit p_page_size offset v_offset
    ) sorted_page
  )
  select
    coalesce(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', case when v_coord_search_active then r.distance else null end,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', case when p_include_thumbnails then r.thumbnail_svg_fnu else null end,
        'thumbnail_svg_flambda', case when p_include_thumbnails then r.thumbnail_svg_flambda else null end
      ))
    ) order by r.row_num), '[]'::jsonb),
    (select count(*) from distance_filtered),
    p_page,
    p_page_size
  from page_rows r
  left join programs pr on pr.slug = r.program_slug;
end;
$function$
;


create or replace function public.get_csv_export_spectra(
  p_program_slugs text[],
  p_filter_programs text[] default null,
  p_fields text[] default null,
  p_gratings text[] default null,
  p_gratings_mode text default 'any',
  p_observations text[] default null,
  p_redshift_quality integer[] default null,
  p_redshift_min double precision default null,
  p_redshift_max double precision default null,
  p_max_snr_min double precision default null,
  p_max_snr_max double precision default null,
  p_max_exposure_time_min double precision default null,
  p_max_exposure_time_max double precision default null,
  p_dq_flags_include_any integer default null,
  p_dq_flags_include_all integer default null,
  p_dq_flags_exclude integer default null,
  p_list_ids integer[] default null,
  p_search text default null,
  p_inspected_only boolean default null,
  p_has_photometry boolean default null,
  p_comment_search text default null,
  p_comment_search_scope text default null,
  p_comment_user_id uuid default null,
  p_coord_ra double precision default null,
  p_coord_dec double precision default null,
  p_radius_degrees double precision default null,
  p_sort_column text default 'target_id',
  p_sort_direction text default 'asc'
)
returns table(
  spectrum_id text, target_id text, grating text, field text, ra double precision, "dec" double precision,
  redshift numeric, redshift_quality integer, redshift_auto double precision,
  signal_to_noise double precision, exposure_time double precision, fits_path text,
  program_slug text, program_name text, last_inspected_at timestamptz, last_inspected_by text,
  distance double precision, dq_flags integer, lists text
)
language plpgsql stable
set plan_cache_mode to 'force_custom_plan'
as $function$
declare
  v_filtered_program_slugs text[];
  v_coord_search_active boolean;
  v_comment_search_active boolean;
  v_grating_filter_active boolean;
begin
  v_coord_search_active := (p_coord_ra is not null and p_coord_dec is not null and p_radius_degrees is not null);
  v_comment_search_active := (p_comment_search is not null and p_comment_search != '' and p_comment_search_scope in ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings is not null and array_length(p_gratings, 1) > 0);
  if p_sort_direction not in ('asc', 'desc') then p_sort_direction := 'asc'; end if;
  if not (p_sort_column in ('target_id', 'spectrum_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating')
       or (p_sort_column = 'distance' and v_coord_search_active)) then
    p_sort_column := 'spectrum_id';
  end if;
  if p_filter_programs is not null and array_length(p_filter_programs, 1) > 0 then
    select array(select unnest(p_program_slugs) intersect select unnest(p_filter_programs)) into v_filtered_program_slugs;
  else v_filtered_program_slugs := p_program_slugs; end if;
  if v_filtered_program_slugs is null or array_length(v_filtered_program_slugs, 1) is null then return; end if;

  return query
  with visible_lists as (
    select olm.object_id, string_agg(ol.slug, ';' order by ol.slug) as lists
    from object_list_members olm
    join object_lists ol on ol.id = olm.list_id
    where ol.created_by = auth.uid() or ol.visibility in ('public_read', 'public_edit')
    group by olm.object_id
  ),
  filtered_spectra as (
    select s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      case when v_coord_search_active then
        2 * degrees(asin(sqrt(power(sin(radians(t.dec - p_coord_dec) / 2), 2) + cos(radians(p_coord_dec)) * cos(radians(t.dec)) * power(sin(radians(t.ra - p_coord_ra) / 2), 2))))
      else null end as distance,
      coalesce(s.dq_flags, 0) as dq_flags,
      vl.lists
    from targets t
    join spectra s on s.target_id = t.target_id
    left join objects o on o.id = t.object_id
    left join visible_lists vl on vl.object_id = t.object_id
    where t.program_slug = any(v_filtered_program_slugs)
      and (o.id is null or o.is_active = true)
      and (not v_grating_filter_active or s.grating = any(p_gratings))
      and (p_fields is null or array_length(p_fields, 1) is null or t.field = any(p_fields))
      and (p_observations is null or array_length(p_observations, 1) is null or t.observation = any(p_observations))
      and (p_redshift_quality is null or array_length(p_redshift_quality, 1) is null or o.redshift_quality = any(p_redshift_quality))
      and (p_redshift_min is null or o.redshift >= p_redshift_min) and (p_redshift_max is null or o.redshift <= p_redshift_max)
      and (p_max_snr_min is null or s.signal_to_noise >= p_max_snr_min) and (p_max_snr_max is null or s.signal_to_noise <= p_max_snr_max)
      and (p_max_exposure_time_min is null or s.exposure_time >= p_max_exposure_time_min) and (p_max_exposure_time_max is null or s.exposure_time <= p_max_exposure_time_max)
      and (p_dq_flags_include_any is null or (coalesce(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      and (p_dq_flags_include_all is null or (coalesce(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      and (p_dq_flags_exclude is null or (coalesce(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      and (p_list_ids is null or array_length(p_list_ids, 1) is null or t.object_id in (
          select olm.object_id from object_list_members olm where olm.list_id = any(p_list_ids) and olm.object_id is not null
      ))
      and (p_search is null
           or t.target_id ilike '%' || p_search || '%'
           or s.spectrum_id ilike '%' || p_search || '%')
      and (p_inspected_only is null or (p_inspected_only = true and o.redshift_quality > 0) or (p_inspected_only = false and coalesce(o.redshift_quality, 0) = 0))
      and (p_has_photometry is null or o.has_photometry = p_has_photometry)
      and (not v_comment_search_active or exists (
        select 1 from comments c where c.target_id = t.id and c.is_deleted = false
          and c.content ilike '%' || p_comment_search || '%'
          and (p_comment_search_scope = 'everyone' or (p_comment_search_scope = 'just_me' and c.user_id = p_comment_user_id))))
      and (not v_coord_search_active or (
        t.ra between (p_coord_ra - p_radius_degrees) and (p_coord_ra + p_radius_degrees)
        and t.dec between (p_coord_dec - p_radius_degrees) and (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered as (select fs.* from filtered_spectra fs where not v_coord_search_active or fs.distance <= p_radius_degrees)
  select df.spectrum_id, df.target_id, df.grating, df.field, df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name as last_inspected_by,
    df.distance, df.dq_flags, df.lists
  from distance_filtered df
  left join programs pr on pr.slug = df.program_slug
  left join user_profiles up on up.user_id = df.last_inspected_by
  order by
    case when v_coord_search_active then df.distance end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'spectrum_id' and p_sort_direction = 'asc' then df.spectrum_id end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'spectrum_id' and p_sort_direction = 'desc' then df.spectrum_id end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'target_id' and p_sort_direction = 'asc' then df.target_id end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'target_id' and p_sort_direction = 'desc' then df.target_id end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'field' and p_sort_direction = 'asc' then df.field end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'field' and p_sort_direction = 'desc' then df.field end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'observation' and p_sort_direction = 'asc' then df.observation end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'observation' and p_sort_direction = 'desc' then df.observation end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'ra' and p_sort_direction = 'asc' then df.ra end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'ra' and p_sort_direction = 'desc' then df.ra end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'dec' and p_sort_direction = 'asc' then df.dec end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'dec' and p_sort_direction = 'desc' then df.dec end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift' and p_sort_direction = 'asc' then df.redshift end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift' and p_sort_direction = 'desc' then df.redshift end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift_quality' and p_sort_direction = 'asc' then df.redshift_quality end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift_quality' and p_sort_direction = 'desc' then df.redshift_quality end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift_auto' and p_sort_direction = 'asc' then df.redshift_auto end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'redshift_auto' and p_sort_direction = 'desc' then df.redshift_auto end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'signal_to_noise' and p_sort_direction = 'asc' then df.signal_to_noise end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'signal_to_noise' and p_sort_direction = 'desc' then df.signal_to_noise end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'exposure_time' and p_sort_direction = 'asc' then df.exposure_time end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'exposure_time' and p_sort_direction = 'desc' then df.exposure_time end desc nulls last,
    case when not v_coord_search_active and p_sort_column = 'grating' and p_sort_direction = 'asc' then df.grating end asc nulls last,
    case when not v_coord_search_active and p_sort_column = 'grating' and p_sort_direction = 'desc' then df.grating end desc nulls last,
    df.target_id asc, df.grating asc;
end;
$function$
;
