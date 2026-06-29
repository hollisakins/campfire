-- F1: storage_objects registry (epic #210). Additive — only the new table, its
-- indexes/constraints/policies/grants, and the get_storage_budget RPC. The matview
-- and view drop/recreate churn that `supabase db diff` emits (mv_filter_options,
-- mv_programs_overview, nircam_reduction_progress, spectrum_flag_summary) was
-- removed by hand: migra cannot introspect materialized views, so it re-emits them
-- on every unrelated migration (CLAUDE.md caveat). They are unchanged here, and
-- dropping the matviews would discard their unique indexes (restore_mv_unique_indexes).

create sequence "public"."storage_objects_id_seq";


  create table "public"."storage_objects" (
    "id" bigint not null default nextval('public.storage_objects_id_seq'::regclass),
    "backend" text not null,
    "bucket" text not null,
    "storage_key" text not null,
    "content_hash" text not null,
    "size_bytes" bigint not null,
    "content_type" text not null,
    "product_type" text not null,
    "instrument" text,
    "status" text not null default 'active'::text,
    "observation" text,
    "field" text,
    "spectrum_id" text,
    "exposure_ref" text,
    "deployment_id" integer,
    "cfpipe_version" text,
    "uploaded_by" uuid,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
      );


alter table "public"."storage_objects" enable row level security;

alter sequence "public"."storage_objects_id_seq" owned by "public"."storage_objects"."id";

CREATE INDEX idx_storage_objects_backend_status ON public.storage_objects USING btree (backend, status);

CREATE INDEX idx_storage_objects_content_hash ON public.storage_objects USING btree (content_hash);

CREATE INDEX idx_storage_objects_deployment_id ON public.storage_objects USING btree (deployment_id);

CREATE INDEX idx_storage_objects_observation ON public.storage_objects USING btree (observation);

CREATE UNIQUE INDEX idx_storage_objects_product_exposure_active ON public.storage_objects USING btree (product_type, exposure_ref) WHERE (status = 'active'::text);

CREATE INDEX idx_storage_objects_spectrum_id ON public.storage_objects USING btree (spectrum_id);

CREATE INDEX idx_storage_objects_storage_key ON public.storage_objects USING btree (storage_key);

CREATE UNIQUE INDEX storage_objects_backend_bucket_key_unique ON public.storage_objects USING btree (backend, bucket, storage_key);

CREATE UNIQUE INDEX storage_objects_pkey ON public.storage_objects USING btree (id);

alter table "public"."storage_objects" add constraint "storage_objects_pkey" PRIMARY KEY using index "storage_objects_pkey";

alter table "public"."storage_objects" add constraint "storage_objects_backend_bucket_key_unique" UNIQUE using index "storage_objects_backend_bucket_key_unique";

alter table "public"."storage_objects" add constraint "storage_objects_backend_check" CHECK ((backend = ANY (ARRAY['r2'::text, 'osn'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_backend_check";

alter table "public"."storage_objects" add constraint "storage_objects_bucket_check" CHECK ((bucket = ANY (ARRAY['data'::text, 'tiles'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_bucket_check";

alter table "public"."storage_objects" add constraint "storage_objects_content_hash_check" CHECK ((content_hash ~ '^(sha256|etag):'::text)) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_content_hash_check";

alter table "public"."storage_objects" add constraint "storage_objects_deployment_id_fkey" FOREIGN KEY (deployment_id) REFERENCES public.deployments(id) ON DELETE SET NULL not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_deployment_id_fkey";

alter table "public"."storage_objects" add constraint "storage_objects_instrument_check" CHECK (((instrument IS NULL) OR (instrument = ANY (ARRAY['nirspec'::text, 'nircam'::text])))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_instrument_check";

alter table "public"."storage_objects" add constraint "storage_objects_observation_fkey" FOREIGN KEY (observation) REFERENCES public.observations(name) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_observation_fkey";

alter table "public"."storage_objects" add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'zfit'::text, 'nirspec_spectrum_exposure'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_product_type_check";

alter table "public"."storage_objects" add constraint "storage_objects_status_check" CHECK ((status = ANY (ARRAY['active'::text, 'superseded'::text, 'revoked'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_status_check";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_storage_budget()
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  result JSON;
  is_admin BOOLEAN;
  cap_bytes BIGINT := 20::BIGINT * 1024 * 1024 * 1024 * 1024;  -- 20 TB
  registry_bytes BIGINT;
  tile_bytes BIGINT;
  total_bytes BIGINT;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  -- Admin (web/CLI login) OR service_role (CLI --local / headless deploy). Both are
  -- trusted callers of the registry; everyone else is denied. NULL is_admin (no uid /
  -- no profile) coalesces to false so the gate is fail-closed — stronger than the
  -- get_download_stats pattern, which relies on authenticated-only EXECUTE to mask it.
  IF NOT (COALESCE(is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT COALESCE(SUM(size_bytes), 0) INTO registry_bytes
  FROM storage_objects
  WHERE status = 'active';

  SELECT COALESCE(SUM(total_size_bytes), 0) INTO tile_bytes
  FROM map_layers;

  total_bytes := registry_bytes + tile_bytes;

  SELECT json_build_object(
    'cap_bytes', cap_bytes,
    'total_bytes', total_bytes,
    'pct_used', ROUND((total_bytes::NUMERIC / NULLIF(cap_bytes, 0)) * 100, 2),
    'registry_bytes', registry_bytes,
    'tile_bytes', tile_bytes,
    'by_backend', (
      SELECT COALESCE(json_object_agg(backend, bytes), '{}'::json)
      FROM (
        SELECT backend, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY backend
      ) t
    ),
    'by_bucket', (
      SELECT COALESCE(json_object_agg(bucket, bytes), '{}'::json)
      FROM (
        SELECT bucket, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY bucket
      ) t
    ),
    'by_product_type', (
      SELECT COALESCE(json_object_agg(product_type, bytes), '{}'::json)
      FROM (
        SELECT product_type, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY product_type
      ) t
    ),
    'by_status', (
      SELECT COALESCE(json_object_agg(status, json_build_object('count', cnt, 'bytes', bytes)), '{}'::json)
      FROM (
        SELECT status, COUNT(*) AS cnt, SUM(size_bytes) AS bytes
        FROM storage_objects
        GROUP BY status
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$function$
;

grant execute on function public.get_storage_budget() to authenticated, service_role;


grant delete on table "public"."storage_objects" to "anon";

grant insert on table "public"."storage_objects" to "anon";

grant references on table "public"."storage_objects" to "anon";

grant select on table "public"."storage_objects" to "anon";

grant trigger on table "public"."storage_objects" to "anon";

grant truncate on table "public"."storage_objects" to "anon";

grant update on table "public"."storage_objects" to "anon";

grant delete on table "public"."storage_objects" to "authenticated";

grant insert on table "public"."storage_objects" to "authenticated";

grant references on table "public"."storage_objects" to "authenticated";

grant select on table "public"."storage_objects" to "authenticated";

grant trigger on table "public"."storage_objects" to "authenticated";

grant truncate on table "public"."storage_objects" to "authenticated";

grant update on table "public"."storage_objects" to "authenticated";

grant delete on table "public"."storage_objects" to "service_role";

grant insert on table "public"."storage_objects" to "service_role";

grant references on table "public"."storage_objects" to "service_role";

grant select on table "public"."storage_objects" to "service_role";

grant trigger on table "public"."storage_objects" to "service_role";

grant truncate on table "public"."storage_objects" to "service_role";

grant update on table "public"."storage_objects" to "service_role";


  create policy "admin_insert_storage_objects"
  on "public"."storage_objects"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));



  create policy "admin_select_storage_objects"
  on "public"."storage_objects"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));



  create policy "admin_update_storage_objects"
  on "public"."storage_objects"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


-- Column/table comments (migra does not track COMMENT ON; hand-authored, mirrors tables.sql).
comment on table "public"."storage_objects" is 'Shadow index of every object in cloud storage (epic #210, F1). One row per data-bucket object; written by deploy and a backfill/reconcile pass. Tiles are intentionally NOT indexed per-object (kept on R2, aggregated in map_layers.total_size_bytes); the budget RPC unions both. Additive and inert until a coverage gate proves it complete.';

comment on column "public"."storage_objects"."content_hash" is 'Scheme-prefixed integrity token. ''sha256:<hex>'' is authoritative (deploy hashes the local file; spectra backfill reuses spectra.file_hash). ''etag:<hex>'' is provisional from an S3 LIST/HEAD (no GET) for backfilled objects with no stored sha256; the A1 copy+verify pass (#215) upgrades it to sha256.';

comment on column "public"."storage_objects"."spectrum_id" is 'Typed, indexed scope column (not an FK). spectra.spectrum_id is GENERATED from fits_path and not uniquely constrained, so an FK would over-constrain; an index provides the joinability the registry needs.';

comment on column "public"."storage_objects"."exposure_ref" is 'Stable per-exposure reference for intermediate products (nircam rootname; nirspec (root,nod,detector,source) tuple). Backs the partial unique (product_type, exposure_ref) WHERE status=''active'' — one current object per product/exposure.';

comment on column "public"."storage_objects"."status" is 'active = current object; superseded = replaced by a newer hash (tombstone, GC-eligible later); revoked = un-published. Only active rows count toward the budget and the partial-unique constraint.';



