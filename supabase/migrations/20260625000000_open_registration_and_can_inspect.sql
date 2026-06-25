-- Open registration + granular inspection role.
--
-- 1. Splits the single can_comment permission into two: can_comment continues
--    to gate comments + tag/list editing, while a new can_inspect gates
--    inspection writes (object redshift/quality, target inspection fields,
--    per-spectrum DQ flags). Existing commenters are backfilled to can_inspect
--    so nobody loses a capability they had.
-- 2. Adds a handle_new_user trigger so OPEN self-registrations auto-provision a
--    default-role profile (can_comment = true, can_inspect = false).
-- 3. Adds inspection_access_requests so users can ask an admin for can_inspect.
-- 4. Removes the now-defunct account_requests approval queue.

-- ---------------------------------------------------------------------------
-- 1. can_inspect column + backfill
-- ---------------------------------------------------------------------------

alter table "public"."user_profiles"
  add column if not exists "can_inspect" boolean default false;

-- Preserve current capabilities: everyone who could comment could also inspect
-- under the old single-flag model.
update "public"."user_profiles"
  set can_inspect = true
  where can_comment = true;

alter table "public"."pending_invites"
  add column if not exists "can_inspect" boolean default false;

-- can_inspect() RLS helper (mirrors can_comment()).
create or replace function public.can_inspect()
returns boolean
language sql stable security definer
set search_path = public
as $$
  select coalesce(
    (select can_inspect from user_profiles where user_id = auth.uid()),
    false
  );
$$;

grant execute on function public.can_inspect() to authenticated;

-- ---------------------------------------------------------------------------
-- 2. Repoint inspection RLS policies from can_comment() to can_inspect()
-- ---------------------------------------------------------------------------

drop policy if exists "update_targets_by_access" on targets;
create policy "update_targets_by_access"
  on targets for update
  using (
    program_slug = any((select public.accessible_program_slugs())::text[])
    and (select public.can_inspect())
  );

drop policy if exists "update_objects_by_access" on objects;
create policy "update_objects_by_access"
  on objects for update
  using (
    programs && (select public.accessible_program_slugs())
    and (select public.can_inspect())
  )
  with check (
    programs && (select public.accessible_program_slugs())
    and (select public.can_inspect())
  );

drop policy if exists "update_spectra_dq_by_access" on spectra;
create policy "update_spectra_dq_by_access"
  on spectra for update to authenticated
  using (
    (select public.can_inspect())
    and target_id in (
      select t.target_id from targets t
      where t.program_slug = any((select public.accessible_program_slugs())::text[])
    )
  )
  with check (
    (select public.can_inspect())
    and target_id in (
      select t.target_id from targets t
      where t.program_slug = any((select public.accessible_program_slugs())::text[])
    )
  );

-- ---------------------------------------------------------------------------
-- 3. handle_new_user trigger for open self-registration
-- ---------------------------------------------------------------------------

create or replace function public.handle_new_user() returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
    v_base text;
    v_username text;
    v_full_name text;
    v_suffix integer := 0;
begin
    if new.raw_user_meta_data->>'self_signup' is distinct from 'true' then
        return new;
    end if;

    v_base := lower(coalesce(
        nullif(new.raw_user_meta_data->>'username', ''),
        split_part(new.email, '@', 1)
    ));
    v_base := regexp_replace(v_base, '[^a-z0-9._-]', '', 'g');
    v_base := regexp_replace(v_base, '^[._-]+', '');
    v_base := regexp_replace(v_base, '[._-]+$', '');
    if length(v_base) < 2 then
        v_base := 'user' || v_base;
    end if;
    v_base := left(v_base, 38);
    v_base := regexp_replace(v_base, '[._-]+$', '');

    v_full_name := coalesce(nullif(new.raw_user_meta_data->>'full_name', ''), v_base);

    v_username := v_base;
    while exists (select 1 from public.user_profiles where username = v_username) loop
        v_suffix := v_suffix + 1;
        v_username := left(v_base, 39 - length(v_suffix::text)) || v_suffix::text;
    end loop;

    insert into public.user_profiles (
        user_id, username, full_name,
        is_group_account, can_comment, can_inspect, is_admin
    )
    values (
        new.id, v_username, v_full_name,
        false, true, false, false
    )
    on conflict (user_id) do nothing;

    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- 4. inspection_access_requests
-- ---------------------------------------------------------------------------

create table if not exists "public"."inspection_access_requests" (
    "id" integer not null,
    "user_id" uuid not null,
    "status" text default 'pending'::text not null,
    "message" text,
    "created_at" timestamp with time zone default now(),
    "reviewed_at" timestamp with time zone,
    "reviewed_by" uuid,
    constraint "inspection_access_requests_status_check"
        check ((status = any (array['pending'::text, 'approved'::text, 'rejected'::text])))
);

alter table "public"."inspection_access_requests" owner to "postgres";

create sequence if not exists "public"."inspection_access_requests_id_seq"
    as integer start with 1 increment by 1 no minvalue no maxvalue cache 1;
alter sequence "public"."inspection_access_requests_id_seq" owner to "postgres";
alter sequence "public"."inspection_access_requests_id_seq"
    owned by "public"."inspection_access_requests"."id";
alter table only "public"."inspection_access_requests"
    alter column "id" set default nextval('"public"."inspection_access_requests_id_seq"'::regclass);

alter table only "public"."inspection_access_requests"
    add constraint "inspection_access_requests_pkey" primary key ("id");
alter table only "public"."inspection_access_requests"
    add constraint "inspection_access_requests_user_id_fkey"
    foreign key ("user_id") references "auth"."users"("id") on delete cascade;
alter table only "public"."inspection_access_requests"
    add constraint "inspection_access_requests_reviewed_by_fkey"
    foreign key ("reviewed_by") references "auth"."users"("id");

create index if not exists idx_inspection_access_requests_status
    on public.inspection_access_requests using btree (status);
create unique index if not exists uniq_inspection_access_requests_pending
    on public.inspection_access_requests using btree (user_id)
    where (status = 'pending');

grant all on table "public"."inspection_access_requests" to "anon";
grant all on table "public"."inspection_access_requests" to "authenticated";
grant all on table "public"."inspection_access_requests" to "service_role";
grant all on sequence "public"."inspection_access_requests_id_seq" to "anon";
grant all on sequence "public"."inspection_access_requests_id_seq" to "authenticated";
grant all on sequence "public"."inspection_access_requests_id_seq" to "service_role";

alter table inspection_access_requests enable row level security;

drop policy if exists "select_own_inspection_requests" on inspection_access_requests;
create policy "select_own_inspection_requests"
  on inspection_access_requests for select to authenticated
  using (user_id = (select auth.uid()) or (select public.is_admin()));

drop policy if exists "insert_own_inspection_request" on inspection_access_requests;
create policy "insert_own_inspection_request"
  on inspection_access_requests for insert to authenticated
  with check (user_id = (select auth.uid()) and status = 'pending');

drop policy if exists "admin_update_inspection_requests" on inspection_access_requests;
create policy "admin_update_inspection_requests"
  on inspection_access_requests for update to authenticated
  using ((select public.is_admin()));

-- ---------------------------------------------------------------------------
-- 5. Remove the defunct account_requests approval queue
-- ---------------------------------------------------------------------------

drop table if exists "public"."account_requests" cascade;
