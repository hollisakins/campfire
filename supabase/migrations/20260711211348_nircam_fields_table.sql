-- NIRCam field registry (issue #303) — the first-class `fields` table, the
-- missing third leg of the cloud-as-source-of-truth config loop (programs /
-- observations / fields), symmetric with `observations`. Plus the two read RPCs
-- for the NIRCam page redesign.
--
-- NOTE: the spurious `drop … / create` churn for mv_filter_options,
-- mv_programs_overview, nircam_reduction_progress, and spectrum_flag_summary that
-- `supabase db diff` emits (migra cannot track (mat)views) has been stripped —
-- this migration touches only the new fields table + RPCs + the storage_objects
-- product_type CHECK.

-- Allow the three new deployable NIRCam product types (registered in the layout
-- contract in the prior PR) so the deploy can register them in storage_objects.
alter table "public"."storage_objects" drop constraint "storage_objects_product_type_check";

alter table "public"."storage_objects" add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'zfit'::text, 'nirspec_spectrum_exposure'::text, 'nirspec_rate'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'nircam_expmap_plot'::text, 'nircam_mosaic_thumbnail'::text, 'nircam_layout'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_product_type_check";


  create table "public"."fields" (
    "name" text not null,
    "display_name" text,
    "filters" text[] not null default '{}'::text[],
    "tiles" text[] not null default '{}'::text[],
    "fiducial_tiles" text[] not null default '{}'::text[],
    "epochs" text[] not null default '{}'::text[],
    "programs" text[] not null default '{}'::text[],
    "jwst_program_ids" integer[] not null default '{}'::integer[],
    "file_globs" text[] not null default '{}'::text[],
    "center_ra" double precision,
    "center_dec" double precision,
    "config" jsonb,
    "coverage_area_arcmin2" double precision,
    "coverage_area_deg2" double precision,
    "latest_deployment_id" integer,
    "created_at" timestamp with time zone default now()
      );


alter table "public"."fields" enable row level security;

CREATE UNIQUE INDEX fields_pkey ON public.fields USING btree (name);

alter table "public"."fields" add constraint "fields_pkey" PRIMARY KEY using index "fields_pkey";

alter table "public"."fields" add constraint "fields_latest_deployment_fkey" FOREIGN KEY (latest_deployment_id) REFERENCES public.deployments(id) ON DELETE SET NULL not valid;

alter table "public"."fields" validate constraint "fields_latest_deployment_fkey";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_nircam_field_summary(p_field text)
 RETURNS TABLE(field text, display_name text, center_ra double precision, center_dec double precision, coverage_area_arcmin2 double precision, coverage_area_deg2 double precision, filters text[], tiles text[], pixel_scales text[], extensions text[], epochs text[], n_files bigint, total_bytes bigint, last_updated timestamp without time zone, layout_key text)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    array_agg(DISTINCT ni.filter ORDER BY ni.filter)           AS filters,
    array_agg(DISTINCT ni.tile ORDER BY ni.tile)               AS tiles,
    array_agg(DISTINCT ni.pixel_scale ORDER BY ni.pixel_scale) AS pixel_scales,
    array_agg(DISTINCT ni.extension ORDER BY ni.extension)     AS extensions,
    array_agg(DISTINCT ni.epoch ORDER BY ni.epoch)             AS epochs,
    COUNT(*)::bigint                       AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                     AS last_updated,
    (SELECT so.storage_key FROM storage_objects so
       WHERE so.product_type = 'nircam_layout'
         AND so.field = ni.field
         AND so.status = 'active'
       LIMIT 1)                            AS layout_key
  FROM nircam_images ni
  LEFT JOIN fields f ON f.name = ni.field
  WHERE ni.field = p_field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2;
$function$
;

CREATE OR REPLACE FUNCTION public.get_nircam_fields()
 RETURNS TABLE(field text, display_name text, center_ra double precision, center_dec double precision, coverage_area_arcmin2 double precision, coverage_area_deg2 double precision, n_filters integer, n_tiles integer, n_files bigint, total_bytes bigint, last_updated timestamp without time zone, layout_key text)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    COUNT(DISTINCT ni.filter)::integer AS n_filters,
    COUNT(DISTINCT ni.tile)::integer   AS n_tiles,
    COUNT(*)::bigint                    AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                  AS last_updated,
    (SELECT so.storage_key FROM storage_objects so
       WHERE so.product_type = 'nircam_layout'
         AND so.field = ni.field
         AND so.status = 'active'
       LIMIT 1)                         AS layout_key
  FROM nircam_images ni
  LEFT JOIN fields f ON f.name = ni.field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2
  ORDER BY ni.field;
$function$
;

grant delete on table "public"."fields" to "anon";

grant insert on table "public"."fields" to "anon";

grant references on table "public"."fields" to "anon";

grant select on table "public"."fields" to "anon";

grant trigger on table "public"."fields" to "anon";

grant truncate on table "public"."fields" to "anon";

grant update on table "public"."fields" to "anon";

grant delete on table "public"."fields" to "authenticated";

grant insert on table "public"."fields" to "authenticated";

grant references on table "public"."fields" to "authenticated";

grant select on table "public"."fields" to "authenticated";

grant trigger on table "public"."fields" to "authenticated";

grant truncate on table "public"."fields" to "authenticated";

grant update on table "public"."fields" to "authenticated";

grant delete on table "public"."fields" to "service_role";

grant insert on table "public"."fields" to "service_role";

grant references on table "public"."fields" to "service_role";

grant select on table "public"."fields" to "service_role";

grant trigger on table "public"."fields" to "service_role";

grant truncate on table "public"."fields" to "service_role";

grant update on table "public"."fields" to "service_role";


  create policy "accessible_fields_select"
  on "public"."fields"
  as permissive
  for select
  to authenticated
using (((EXISTS ( SELECT 1
   FROM public.nircam_images ni
  WHERE ((ni.field = fields.name) AND (ni.deploy_status = 'published'::text)))) OR ( SELECT public.is_admin() AS is_admin)));



  create policy "admin_fields_insert"
  on "public"."fields"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));



  create policy "admin_fields_update"
  on "public"."fields"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));



