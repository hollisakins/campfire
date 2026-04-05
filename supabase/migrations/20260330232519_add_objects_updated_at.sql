alter table "public"."objects" add column "updated_at" timestamp with time zone default now();

-- Backfill existing rows so first incremental sync has a valid marker
update "public"."objects" set "updated_at" = "created_at" where "updated_at" is null;
