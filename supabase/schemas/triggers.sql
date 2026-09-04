-- =============================================================================
-- CAMPFIRE Supabase Schema: Triggers
-- =============================================================================
-- Canonical source of truth for all trigger functions and triggers.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- ============================================================
-- TRIGGER FUNCTIONS
-- ============================================================

-- 1. log_object_inspection_changes
--    Logs object-level redshift_quality and redshift_inspected changes into
--    flag_audit_log (subject = object_id) and bumps updated_at. Replaces the
--    targets flavor of log_flag_changes for inspection state.
--    Redshift_inspected is numeric; flag_audit_log.old/new_value are integers,
--    so we scale by 1e6 (matching numeric(10,6) precision) for lossless
--    storage. No downstream consumer reverses the scale — audit rows exist
--    purely to attribute edits (count_distinct_inspected_objects just counts
--    DISTINCT object_id, field_name disambiguates in the UI).
DROP FUNCTION IF EXISTS public.log_object_inspection_changes CASCADE;

CREATE OR REPLACE FUNCTION public.log_object_inspection_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.redshift_inspected IS DISTINCT FROM NEW.redshift_inspected THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (
            NEW.id, auth.uid(), 'redshift_inspected',
            (OLD.redshift_inspected * 1000000)::integer,
            (NEW.redshift_inspected * 1000000)::integer
        );
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- 2. bump_object_version
--    Optimistic-locking counter: increments objects.version *only* when
--    user-editable inspection fields change. Aggregate column updates from
--    reconcile_field_objects() (n_targets, programs, max_snr, etc.) do not
--    bump the version, so the deploy pipeline never invalidates an
--    in-progress edit.
DROP FUNCTION IF EXISTS public.bump_object_version CASCADE;

CREATE OR REPLACE FUNCTION public.bump_object_version() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.redshift_inspected IS DISTINCT FROM NEW.redshift_inspected
       OR OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        NEW.version = OLD.version + 1;
    END IF;
    RETURN NEW;
END;
$$;


-- 3. log_spectrum_dq_changes
--    Logs per-spectrum dq_flags changes into flag_audit_log
--    (subject = spectrum_id). DQ flags are now per-spectrum; target-level
--    DQ logging is gone with the targets-list view.
DROP FUNCTION IF EXISTS public.log_spectrum_dq_changes CASCADE;

CREATE OR REPLACE FUNCTION public.log_spectrum_dq_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (spectrum_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    RETURN NEW;
END;
$$;


-- 4. enforce_object_user_update_scope
--    Non-admin users (via `update_objects_by_access` RLS) can legitimately
--    write inspection fields. The RLS policy has no WITH CHECK and no
--    column-level filter, so without this trigger a user with can_inspect
--    can hit PostgREST directly and rewrite anything on objects
--    (programs, is_active, aggregates, etc.). This trigger enforces the
--    column scope at the DB level: anything except the inspection set
--    raises an exception for non-admin callers. Admins and service-role
--    writes (auth.uid() IS NULL) pass through.
DROP FUNCTION IF EXISTS public.enforce_object_user_update_scope CASCADE;

CREATE OR REPLACE FUNCTION public.enforce_object_user_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only touch the inspection set:
    --   redshift_inspected, redshift_quality, last_inspected_at,
    --   last_inspected_by. version and updated_at are maintained by sibling
    --   triggers; we explicitly allow them to change so this trigger
    --   doesn't reject writes that went through the legitimate path.
    IF OLD.object_id IS DISTINCT FROM NEW.object_id
       OR OLD.field IS DISTINCT FROM NEW.field
       OR OLD.ra IS DISTINCT FROM NEW.ra
       OR OLD.dec IS DISTINCT FROM NEW.dec
       OR OLD.n_targets IS DISTINCT FROM NEW.n_targets
       OR OLD.n_spectra IS DISTINCT FROM NEW.n_spectra
       OR OLD.programs IS DISTINCT FROM NEW.programs
       OR OLD.gratings IS DISTINCT FROM NEW.gratings
       OR OLD.observations IS DISTINCT FROM NEW.observations
       OR OLD.max_snr IS DISTINCT FROM NEW.max_snr
       OR OLD.max_exposure_time IS DISTINCT FROM NEW.max_exposure_time
       OR OLD.photo_z IS DISTINCT FROM NEW.photo_z
       OR OLD.photo_z_err_lo IS DISTINCT FROM NEW.photo_z_err_lo
       OR OLD.photo_z_err_hi IS DISTINCT FROM NEW.photo_z_err_hi
       OR OLD.has_photometry IS DISTINCT FROM NEW.has_photometry
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.last_data_change_at IS DISTINCT FROM NEW.last_data_change_at
       OR OLD.staleness_reason IS DISTINCT FROM NEW.staleness_reason
       OR OLD.inspected_used_auto IS DISTINCT FROM NEW.inspected_used_auto
       OR OLD.is_active IS DISTINCT FROM NEW.is_active
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.search_text IS DISTINCT FROM NEW.search_text
    THEN
        RAISE EXCEPTION 'Non-admin updates to objects may only change inspection fields (redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by)'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$$;


-- 4b. pin_redshift_on_signoff
--     Pins the displayed redshift at the moment an inspector signs off.
--
--     The generated `redshift` column is COALESCE(redshift_inspected, redshift_auto)
--     (when quality != 1). If an inspector commits quality >= 2 without entering
--     a numeric override, the displayed redshift falls through to redshift_auto
--     — which compute_object_redshift_auto() can move under their feet on the
--     next reprocess. This trigger eliminates that "implicit sign-off" failure
--     mode by promoting the current redshift_auto into redshift_inspected at
--     sign-off time, recording inspected_used_auto = true so the UI knows the
--     value wasn't typed.
--
--     Cases handled (only fires on inspector-driven UPDATEs by being scoped to
--     OF redshift_inspected, redshift_quality — reconcile-driven redshift_auto
--     bumps don't trip it):
--       - quality >= 2 AND inspected IS NULL AND auto IS NOT NULL:
--           pin → inspected = auto, used_auto = true
--       - quality >= 2 AND inspected changes to a NEW non-null value:
--           explicit override → used_auto = false
--       - quality < 2 (uninspected or Impossible):
--           drop the pin → inspected = NULL, used_auto = false
--
--     Trigger ordering: PostgreSQL fires BEFORE triggers in alphabetical order,
--     so on UPDATE we get bump → enforce → pin → track. enforce runs before
--     pin can mutate inspected_used_auto, so it correctly rejects direct
--     non-admin writes to the column. track runs after pin, so the audit log
--     captures the final NEW.redshift_inspected value (matches what's stored).
DROP FUNCTION IF EXISTS public.pin_redshift_on_signoff CASCADE;

CREATE OR REPLACE FUNCTION public.pin_redshift_on_signoff() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.redshift_quality >= 2 THEN
        IF NEW.redshift_inspected IS NULL AND NEW.redshift_auto IS NOT NULL THEN
            -- Implicit sign-off: pin to the current auto-fit so reprocessing
            -- can't silently move the displayed redshift.
            NEW.redshift_inspected := NEW.redshift_auto::numeric;
            NEW.inspected_used_auto := true;
        ELSIF TG_OP = 'UPDATE'
              AND NEW.redshift_inspected IS NOT NULL
              AND NEW.redshift_inspected IS DISTINCT FROM OLD.redshift_inspected THEN
            -- Explicit override (newly typed or changed): clear the auto flag.
            NEW.inspected_used_auto := false;
        ELSIF TG_OP = 'INSERT' AND NEW.redshift_inspected IS NOT NULL THEN
            -- Initial insert with an explicit override.
            NEW.inspected_used_auto := false;
        END IF;
    ELSE
        -- quality < 2: object is uninspected or Impossible. Drop the pin so
        -- redshift_inspected reflects "no user override" again. The generated
        -- redshift column handles Impossible (quality=1 → NULL) on its own.
        NEW.redshift_inspected := NULL;
        NEW.inspected_used_auto := false;
    END IF;
    RETURN NEW;
END;
$$;


-- 5. bump_spectra_updated_at
--    Sets spectra.updated_at = NOW() on any UPDATE so incremental sync
--    (p_updated_since) can pick up per-spectrum changes regardless of
--    which column was touched.
DROP FUNCTION IF EXISTS public.bump_spectra_updated_at CASCADE;

CREATE OR REPLACE FUNCTION public.bump_spectra_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- 5b. enforce_spectra_dq_user_update_scope
--     Mirrors enforce_object_user_update_scope for the spectra table.
--     Non-admin users authenticated under `update_spectra_dq_by_access`
--     RLS may update spectra whose parent target is in an accessible
--     program, but only to change dq_flags. This trigger rejects any
--     other column delta for non-admin callers so PostgREST writes can't
--     rewrite fits_path, thumbnails, provenance, etc.
DROP FUNCTION IF EXISTS public.enforce_spectra_dq_user_update_scope CASCADE;

CREATE OR REPLACE FUNCTION public.enforce_spectra_dq_user_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only change dq_flags. updated_at is maintained
    -- by bump_spectra_updated_at; allow it through.
    IF OLD.grating IS DISTINCT FROM NEW.grating
       OR OLD.fits_path IS DISTINCT FROM NEW.fits_path
       OR OLD.signal_to_noise IS DISTINCT FROM NEW.signal_to_noise
       OR OLD.target_id IS DISTINCT FROM NEW.target_id
       OR OLD.thumbnail_svg_fnu IS DISTINCT FROM NEW.thumbnail_svg_fnu
       OR OLD.thumbnail_svg_flambda IS DISTINCT FROM NEW.thumbnail_svg_flambda
       OR OLD.file_hash IS DISTINCT FROM NEW.file_hash
       OR OLD.file_size IS DISTINCT FROM NEW.file_size
       OR OLD.exposure_time IS DISTINCT FROM NEW.exposure_time
       OR OLD.crds_context IS DISTINCT FROM NEW.crds_context
       OR OLD.jwst_version IS DISTINCT FROM NEW.jwst_version
       OR OLD.cfpipe_version IS DISTINCT FROM NEW.cfpipe_version
       OR OLD.date_obs IS DISTINCT FROM NEW.date_obs
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.program_slug IS DISTINCT FROM NEW.program_slug
       OR OLD.observation IS DISTINCT FROM NEW.observation
    THEN
        RAISE EXCEPTION 'Non-admin updates to spectra may only change dq_flags'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$$;


-- 5d. Row-local RLS scope columns (perf T2-A, #504, decision D-A)
--
--     spectra.program_slug / spectra.observation and
--     shutters.has_published_spectrum are copies of parent-target state that
--     let the read policies on those tables test the row itself instead of
--     materializing every accessible target (spectra) or probing objects once
--     per row (shutters). Four triggers own the copies; nothing else writes
--     them, and a client-supplied value is only ever accepted when it already
--     agrees with the parent:
--
--       sync_spectra_target_scope            spectra  BEFORE INSERT / UPDATE OF target_id, program_slug, observation
--       propagate_target_scope_to_spectra    targets  AFTER  UPDATE OF program_slug, observation
--       sync_shutter_publication             shutters BEFORE INSERT / UPDATE OF object_id, has_published_spectrum
--       propagate_target_publication_to_shutters  targets AFTER UPDATE OF has_published_spectrum
--
--     shutters.object_id is the target_id namespace (both come from the
--     observation's ECSV object_id), so a shutter follows its TARGET's
--     publication state -- the same gate select_targets_by_access applies --
--     and one with no targets row stays visible (fail-closed default true),
--     matching the orphan behaviour of the old NOT EXISTS form.
--
--     SECURITY DEFINER: the BEFORE triggers run for non-admin DQ updates too,
--     and those callers only see targets through RLS. The parent lookup must
--     not depend on the caller's visibility.
DROP FUNCTION IF EXISTS public.sync_spectra_target_scope CASCADE;

CREATE OR REPLACE FUNCTION public.sync_spectra_target_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    SELECT t.program_slug, t.observation
      INTO NEW.program_slug, NEW.observation
      FROM targets t
     WHERE t.target_id = NEW.target_id;
    -- No parent row: both land NULL and the NOT NULL constraints reject the
    -- row (the FK spectra_target_id_fkey would too). No RAISE needed here.
    RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.propagate_target_scope_to_spectra CASCADE;

CREATE OR REPLACE FUNCTION public.propagate_target_scope_to_spectra() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE spectra s
       SET program_slug = NEW.program_slug,
           observation  = NEW.observation
     WHERE s.target_id = NEW.target_id
       AND (s.program_slug IS DISTINCT FROM NEW.program_slug
            OR s.observation IS DISTINCT FROM NEW.observation);
    RETURN NULL;
END;
$$;

DROP FUNCTION IF EXISTS public.sync_shutter_publication CASCADE;

CREATE OR REPLACE FUNCTION public.sync_shutter_publication() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    NEW.has_published_spectrum := COALESCE(
        (SELECT t.has_published_spectrum FROM targets t WHERE t.target_id = NEW.object_id),
        true);
    RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.propagate_target_publication_to_shutters CASCADE;

CREATE OR REPLACE FUNCTION public.propagate_target_publication_to_shutters() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE shutters s
       SET has_published_spectrum = NEW.has_published_spectrum
     WHERE s.object_id = NEW.target_id
       AND s.has_published_spectrum IS DISTINCT FROM NEW.has_published_spectrum;
    RETURN NULL;
END;
$$;


-- 5c. handle_new_user
--     Auto-provisions a user_profiles row for OPEN self-registrations.
--
--     Only fires when the signup carried raw_user_meta_data.self_signup = 'true'
--     (set by the /signup page). The admin-invite path (inviteUserByEmail) and
--     seeded/test users do NOT set that flag, so their profiles are still
--     created by the /welcome accept flow and seed.sql respectively — this
--     trigger never clobbers them or interferes with the invite /welcome
--     routing (which keys off profile absence).
--
--     New self-signups get the default role: can_comment = true (may comment
--     and tag), can_inspect = false (must request inspection rights). A unique
--     username is derived from metadata/email and de-duplicated with a numeric
--     suffix. SECURITY DEFINER so it can write user_profiles regardless of the
--     (as yet unconfirmed) caller.
DROP FUNCTION IF EXISTS public.handle_new_user CASCADE;

CREATE OR REPLACE FUNCTION public.handle_new_user() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_base text;
    v_username text;
    v_full_name text;
    v_suffix integer := 0;
BEGIN
    -- Only handle genuine self-service signups.
    IF NEW.raw_user_meta_data->>'self_signup' IS DISTINCT FROM 'true' THEN
        RETURN NEW;
    END IF;

    -- Derive a username candidate from metadata or the email local-part, then
    -- coerce it to satisfy user_profiles_username_check
    -- (^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$).
    v_base := lower(coalesce(
        nullif(NEW.raw_user_meta_data->>'username', ''),
        split_part(NEW.email, '@', 1)
    ));
    v_base := regexp_replace(v_base, '[^a-z0-9._-]', '', 'g');
    v_base := regexp_replace(v_base, '^[._-]+', '');
    v_base := regexp_replace(v_base, '[._-]+$', '');
    IF length(v_base) < 2 THEN
        v_base := 'user' || v_base;
    END IF;
    v_base := left(v_base, 38);
    v_base := regexp_replace(v_base, '[._-]+$', '');

    v_full_name := coalesce(nullif(NEW.raw_user_meta_data->>'full_name', ''), v_base);

    -- De-duplicate with a numeric suffix if needed.
    v_username := v_base;
    WHILE EXISTS (SELECT 1 FROM public.user_profiles WHERE username = v_username) LOOP
        v_suffix := v_suffix + 1;
        v_username := left(v_base, 39 - length(v_suffix::text)) || v_suffix::text;
    END LOOP;

    INSERT INTO public.user_profiles (
        user_id, username, full_name,
        is_group_account, can_comment, can_inspect, is_admin
    )
    VALUES (
        NEW.id, v_username, v_full_name,
        false, true, false, false
    )
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;


-- 6. log_list_membership_change
--    Logs additions and removals from object lists into list_audit_log.
DROP FUNCTION IF EXISTS public.log_list_membership_change CASCADE;

CREATE OR REPLACE FUNCTION public.log_list_membership_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    v_object_id integer;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_object_id := NEW.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
    ELSIF TG_OP = 'DELETE' THEN
        v_object_id := OLD.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
    END IF;

    -- Bump objects.updated_at so incremental sync picks up tag changes
    UPDATE objects SET updated_at = NOW() WHERE id = v_object_id;

    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;


-- ============================================================
-- TRIGGERS
-- ============================================================

-- Phase D: drop the old targets-side log/aggregate triggers. Inspection
-- state has moved to objects; targets are stateless provenance now.
DROP TRIGGER IF EXISTS track_flag_changes ON public.targets;
DROP TRIGGER IF EXISTS update_object_best_redshift_trigger ON public.targets;
DROP FUNCTION IF EXISTS public.log_flag_changes CASCADE;
DROP FUNCTION IF EXISTS public.update_object_best_redshift CASCADE;

DROP TRIGGER IF EXISTS update_max_snr_trigger ON public.spectra;
DROP TRIGGER IF EXISTS update_max_exposure_time_trigger ON public.spectra;


-- Object inspection: BEFORE UPDATE so version bump and updated_at land in
-- the same row write. Two triggers because PostgreSQL fires triggers in
-- alphabetical order — `bump_object_version` should run first to set
-- NEW.version, then `track_object_inspection` records the change and bumps
-- updated_at.
DROP TRIGGER IF EXISTS bump_object_version_trigger ON public.objects;
CREATE TRIGGER bump_object_version_trigger
  BEFORE UPDATE OF redshift_inspected, redshift_quality ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.bump_object_version();

DROP TRIGGER IF EXISTS track_object_inspection_trigger ON public.objects;
CREATE TRIGGER track_object_inspection_trigger
  BEFORE UPDATE OF redshift_quality, redshift_inspected ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.log_object_inspection_changes();

-- Belt-and-suspenders: the `update_objects_by_access` RLS policy has no
-- WITH CHECK clause, so this trigger enforces the column scope at the
-- row level. Must run AFTER bump_object_version so the version bump
-- from the legitimate inspection write isn't misclassified as a
-- forbidden aggregate change.
DROP TRIGGER IF EXISTS enforce_object_user_update_scope_trigger ON public.objects;
CREATE TRIGGER enforce_object_user_update_scope_trigger
  BEFORE UPDATE ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.enforce_object_user_update_scope();

-- Pin the displayed redshift at sign-off time. Scoped to inspector-driven
-- columns so reconcile-driven redshift_auto updates don't fire it. Fires
-- alphabetically after enforce_object_user_update_scope so any auto-pinning
-- it does is invisible to the column-scope check (which has already passed).
DROP TRIGGER IF EXISTS pin_redshift_on_signoff_trigger ON public.objects;
CREATE TRIGGER pin_redshift_on_signoff_trigger
  BEFORE INSERT OR UPDATE OF redshift_inspected, redshift_quality ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.pin_redshift_on_signoff();


-- Per-spectrum DQ flag changes
DROP TRIGGER IF EXISTS track_spectrum_dq_changes ON public.spectra;
CREATE TRIGGER track_spectrum_dq_changes
  AFTER UPDATE OF dq_flags ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.log_spectrum_dq_changes();


-- Keep spectra.updated_at fresh only when user-visible columns change, so
-- incremental sync (get_spectra_for_sync p_updated_since) doesn't force a
-- full re-sync on every pipeline provenance touch (crds_context bumps,
-- cfpipe_version bumps, etc.).  Scope matches what clients actually
-- need to re-fetch for: flags, redshift, SNR, thumbnails, file identity.
-- A real re-reduction also changes file_hash, so refreshed provenance rides
-- along on the next incremental sync without a provenance-only bump.
DROP TRIGGER IF EXISTS bump_spectra_updated_at_trigger ON public.spectra;
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF
    dq_flags,
    redshift_auto,
    signal_to_noise,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    fits_path,
    file_hash
  ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();

-- Belt-and-suspenders for update_spectra_dq_by_access — restricts non-admin
-- writes to dq_flags only. See enforce_spectra_dq_user_update_scope function.
DROP TRIGGER IF EXISTS enforce_spectra_dq_user_update_scope_trigger ON public.spectra;
CREATE TRIGGER enforce_spectra_dq_user_update_scope_trigger
  BEFORE UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.enforce_spectra_dq_user_update_scope();


-- Row-local RLS scope columns (perf T2-A, #504). See section 5d above.
-- spectra.program_slug / observation follow the parent target ...
DROP TRIGGER IF EXISTS sync_spectra_target_scope_trigger ON public.spectra;
CREATE TRIGGER sync_spectra_target_scope_trigger
  BEFORE INSERT OR UPDATE OF target_id, program_slug, observation ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.sync_spectra_target_scope();

-- ... and cascade when the target itself moves (never in practice; keeps the
-- invariant structural rather than procedural).
DROP TRIGGER IF EXISTS propagate_target_scope_to_spectra_trigger ON public.targets;
CREATE TRIGGER propagate_target_scope_to_spectra_trigger
  AFTER UPDATE OF program_slug, observation ON public.targets
  FOR EACH ROW
  WHEN (OLD.program_slug IS DISTINCT FROM NEW.program_slug
        OR OLD.observation IS DISTINCT FROM NEW.observation)
  EXECUTE FUNCTION public.propagate_target_scope_to_spectra();

-- shutters.has_published_spectrum follows the target on insert ...
DROP TRIGGER IF EXISTS sync_shutter_publication_trigger ON public.shutters;
CREATE TRIGGER sync_shutter_publication_trigger
  BEFORE INSERT OR UPDATE OF object_id, has_published_spectrum ON public.shutters
  FOR EACH ROW EXECUTE FUNCTION public.sync_shutter_publication();

-- ... and whenever recompute_has_published_spectrum flips the target
-- (publish / revoke / recover / reconcile).
DROP TRIGGER IF EXISTS propagate_target_publication_to_shutters_trigger ON public.targets;
CREATE TRIGGER propagate_target_publication_to_shutters_trigger
  AFTER UPDATE OF has_published_spectrum ON public.targets
  FOR EACH ROW
  WHEN (OLD.has_published_spectrum IS DISTINCT FROM NEW.has_published_spectrum)
  EXECUTE FUNCTION public.propagate_target_publication_to_shutters();


-- List membership audit (unchanged from pre-Phase-D)
DROP TRIGGER IF EXISTS track_list_member_insert ON public.object_list_members;
CREATE TRIGGER track_list_member_insert
  AFTER INSERT ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();

DROP TRIGGER IF EXISTS track_list_member_delete ON public.object_list_members;
CREATE TRIGGER track_list_member_delete
  AFTER DELETE ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();


-- Open self-registration: provision a default-role profile on auth.users
-- insert. Gated to self_signup signups inside the function so the admin-invite
-- and seed flows are unaffected.
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();


-- Role/privilege columns on user_profiles are admin-set only. RLS's
-- self_update_profile is row-level (user_id = auth.uid()) and cannot withhold
-- columns, and `authenticated` holds table-wide UPDATE, so without this a user
-- could escalate their own row via PostgREST (is_admin = true) -- and a share-
-- link visitor could clear is_link_account, stepping out of every
-- NOT is_link_account() narrowing conjunct in one statement (the readonly CHECK
-- constraint accepts is_link_account = false with anything). Same
-- belt-and-suspenders shape as enforce_object_user_update_scope: service-role
-- writes (auth.uid() IS NULL) and admins pass through.
DROP FUNCTION IF EXISTS public.enforce_profile_role_update_scope CASCADE;

CREATE OR REPLACE FUNCTION public.enforce_profile_role_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    IF OLD.is_admin IS DISTINCT FROM NEW.is_admin
       OR OLD.can_comment IS DISTINCT FROM NEW.can_comment
       OR OLD.can_inspect IS DISTINCT FROM NEW.can_inspect
       OR OLD.is_group_account IS DISTINCT FROM NEW.is_group_account
       OR OLD.is_link_account IS DISTINCT FROM NEW.is_link_account THEN
        RAISE EXCEPTION 'Only admins may change role columns on user_profiles'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS enforce_profile_role_update_scope_trigger ON public.user_profiles;
CREATE TRIGGER enforce_profile_role_update_scope_trigger
  BEFORE UPDATE ON public.user_profiles
  FOR EACH ROW EXECUTE FUNCTION public.enforce_profile_role_update_scope();
