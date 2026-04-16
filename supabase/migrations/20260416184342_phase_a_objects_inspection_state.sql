-- Note: ADD COLUMN order matters here — the generated `redshift` column references
-- `redshift_quality`, `redshift_inspected`, and `redshift_auto`, so those must exist
-- first. (The diff tool alphabetized the statements; we reorder manually.)
alter table "public"."objects" add column "redshift_auto" double precision;

alter table "public"."objects" add column "redshift_inspected" numeric(10,6);

alter table "public"."objects" add column "redshift_quality" integer not null default 0;

alter table "public"."objects" add column "redshift" numeric(10,6) generated always as (
CASE
    WHEN (redshift_quality = 1) THEN NULL::double precision
    ELSE COALESCE((redshift_inspected)::double precision, redshift_auto)
END) stored;

alter table "public"."objects" add column "last_inspected_at" timestamp with time zone;

alter table "public"."objects" add column "last_inspected_by" uuid;

alter table "public"."objects" add column "last_data_change_at" timestamp with time zone;

alter table "public"."objects" add column "staleness_reason" text;

alter table "public"."objects" add column "version" integer not null default 1;

alter table "public"."objects" add column "is_active" boolean not null default true;

alter table "public"."spectra" add column "dq_flags" integer not null default 0;

alter table "public"."spectra" add column "redshift_auto" double precision;

CREATE INDEX idx_objects_is_active ON public.objects USING btree (is_active) WHERE (is_active = false);

CREATE INDEX idx_objects_redshift ON public.objects USING btree (redshift);

CREATE INDEX idx_objects_redshift_quality ON public.objects USING btree (redshift_quality);

CREATE INDEX idx_spectra_dq_flags ON public.spectra USING btree (dq_flags) WHERE (dq_flags <> 0);

alter table "public"."objects" add constraint "objects_last_inspected_by_fkey" FOREIGN KEY (last_inspected_by) REFERENCES auth.users(id) not valid;

alter table "public"."objects" validate constraint "objects_last_inspected_by_fkey";


  create policy "update_objects_by_access"
  on "public"."objects"
  as permissive
  for update
  to public
using (((programs && public.accessible_program_slugs()) AND public.can_comment()));


-- Column comments (migra does not diff these; applied manually to keep schema files
-- and production in sync).
COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation as the redshift_auto of the highest-SNR member spectrum. Empty until Phase D migration.';
COMMENT ON COLUMN "public"."objects"."redshift_inspected" IS 'Phase A: user-set redshift override at the object level. Empty until Phase D migration.';
COMMENT ON COLUMN "public"."objects"."redshift_quality" IS 'Phase A: 0=uninspected, 1=Impossible, 2=Tentative, 3=Probable, 4=Secure. Default 0. Empty until Phase D migration.';
COMMENT ON COLUMN "public"."objects"."redshift" IS 'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). Mirrors targets.redshift semantics at object level.';
COMMENT ON COLUMN "public"."objects"."staleness_reason" IS 'Phase A: one of new_target | reprocessed | membership_changed | migration_conflict. Set by reconcile_field_objects() (Phase C) when last_data_change_at advances past last_inspected_at.';
COMMENT ON COLUMN "public"."objects"."version" IS 'Phase A: optimistic-locking counter. Incremented by trigger only when redshift_inspected or redshift_quality changes. Clients pass expected_version on PATCH; mismatch → 409 Conflict.';
COMMENT ON COLUMN "public"."objects"."is_active" IS 'Phase A: false = soft-deleted (orphaned by reconciliation). Hidden from list/map/queue/CSV; reachable via direct URL with banner; admin endpoint reactivates.';
COMMENT ON COLUMN "public"."spectra"."redshift_auto" IS 'Phase A: per-grating zfit redshift_auto from the pipeline ECSV. Populated in Phase B by deploy pipeline; backfilled to per-target value by Phase D.1b migration.';
COMMENT ON COLUMN "public"."spectra"."dq_flags" IS 'Phase A: per-spectrum DQ bitmask. Populated in Phase B by deploy pipeline; backfilled from targets.dq_flags by Phase D.1c migration.';
