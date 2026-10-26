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
       OR OLD.is_public IS DISTINCT FROM NEW.is_public
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

-- ---------------------------------------------------------------------------
-- sync_spectrum_lines
--     Unnest NEW.lines (jsonb keyed by catalog line name) into spectrum_lines.
--     SECURITY DEFINER: the deploy CLI writes spectrum_line_fits as an admin
--     under RLS, and the derived table's own policies must not decide whether
--     the mirror is rebuilt. Only object-valued entries are rows; NaN was
--     already null in the payload, so a missing/null field is a NULL column.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.sync_spectrum_lines() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  DELETE FROM public.spectrum_lines WHERE spectrum_id = NEW.spectrum_id;
  INSERT INTO public.spectrum_lines (
    spectrum_id, line, component, wave_rest, flux, flux_err, snr,
    ew_rest, ew_rest_err, flags, blend_into, object_id
  )
  SELECT
    NEW.spectrum_id,
    e.key,
    COALESCE(e.value ->> 'component', 'narrow'),
    (e.value ->> 'wave_rest')::double precision,
    (e.value ->> 'flux')::double precision,
    (e.value ->> 'flux_err')::double precision,
    (e.value ->> 'snr')::double precision,
    (e.value ->> 'ew_rest')::double precision,
    (e.value ->> 'ew_rest_err')::double precision,
    COALESCE((e.value ->> 'flags')::integer, 0),
    e.value ->> 'blend_into',
    -- phase 2a: the object link, so a line row is addressable by object
    (SELECT t.object_id FROM public.spectra s
       JOIN public.targets t ON t.target_id = s.target_id
      WHERE s.id = NEW.spectrum_id)
  FROM jsonb_each(NEW.lines) AS e
  WHERE jsonb_typeof(e.value) = 'object';
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- sync_object_photometry_bands
--     Unnest NEW.photometry -> 'bands' (jsonb keyed by band name) into
--     object_photometry_bands. The exact counterpart of sync_spectrum_lines
--     on the photometry side, and SECURITY DEFINER for the same reason: the
--     deploy CLI upserts object_photometry as an admin under RLS, and the
--     derived table's own policies must not decide whether the mirror is
--     rebuilt. snr and mag are generated columns, so only the raw flux pair
--     and the pivot wavelength are copied; a missing or null field is a NULL
--     column (the payload already wrote NaN as null).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.sync_object_photometry_bands() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  DELETE FROM public.object_photometry_bands WHERE photometry_id = NEW.id;
  INSERT INTO public.object_photometry_bands (photometry_id, band, flux, flux_err, wav, object_id)
  SELECT
    NEW.id,
    e.key,
    (e.value ->> 'flux')::double precision,
    (e.value ->> 'flux_err')::double precision,
    (e.value ->> 'wav')::double precision,
    NEW.object_id   -- phase 2a: the object link, copied from the cross-match
  FROM jsonb_each(COALESCE(NEW.photometry -> 'bands', '{}'::jsonb)) AS e
  WHERE jsonb_typeof(e.value) = 'object';
  RETURN NEW;
END;
$$;


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
       -- zfit scalars on the row (perf T2-D2, #508): deploy / backfill only.
       OR OLD.chi2_min IS DISTINCT FROM NEW.chi2_min
       OR OLD.confidence IS DISTINCT FROM NEW.confidence
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.program_slug IS DISTINCT FROM NEW.program_slug
       OR OLD.observation IS DISTINCT FROM NEW.observation
       OR OLD.is_public IS DISTINCT FROM NEW.is_public
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

-- ---------------------------------------------------------------------------
-- Needs-review (docs/design-needs-review.md, data releases P3)
-- ---------------------------------------------------------------------------
-- stamp_reviewed_spectra: at every sign-off (any UPDATE that moves
-- last_inspected_at: the inspect route, "mark as reviewed", admin SQL) record
-- the published member spectrum ids the inspector saw and clear the badge.
-- The database computes the set so no client can write a stale one; the
-- column is not in enforce_object_user_update_scope's list, and the trigger
-- is SECURITY DEFINER so a mixed-program object's members are all seen.
DROP FUNCTION IF EXISTS public.stamp_reviewed_spectra CASCADE;

CREATE OR REPLACE FUNCTION public.stamp_reviewed_spectra() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NEW.last_inspected_at IS NOT NULL
       AND NEW.last_inspected_at IS DISTINCT FROM OLD.last_inspected_at THEN
        NEW.reviewed_spectrum_ids := COALESCE((
            SELECT array_agg(s.id ORDER BY s.id)
            FROM targets t
            JOIN spectra s ON s.target_id = t.target_id
            WHERE t.object_id = NEW.id
              AND s.deploy_status = 'published'
        ), '{}'::integer[]);
        NEW.needs_review := false;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS stamp_reviewed_spectra_trigger ON public.objects;
CREATE TRIGGER stamp_reviewed_spectra_trigger
  BEFORE UPDATE OF last_inspected_at ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.stamp_reviewed_spectra();

-- recompute_needs_review_*: statement-level AFTER triggers that hand the
-- touched objects to recompute_needs_review(). Postgres does not allow a
-- column list (UPDATE OF ...) on a trigger with transition tables, so each
-- UPDATE trigger fires on every statement and the function itself narrows to
-- the rows whose relevant columns changed (old vs new joined on id); a
-- statement that touched none of them costs one small join and no recompute.
-- One function per table reads the transition tables its events declare (a
-- transition table the firing event did not declare must not be referenced,
-- hence the TG_OP branches). The recompute's own UPDATE changes only
-- needs_review / updated_at, so the objects trigger it re-fires narrows to
-- nothing and the chain stops there.
DROP FUNCTION IF EXISTS public.recompute_needs_review_from_spectra CASCADE;

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_spectra() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_ids integer[] := '{}';
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id FROM new_rows n
            JOIN targets t ON t.target_id = n.target_id
            WHERE t.object_id IS NOT NULL), '{}'::integer[]);
    ELSIF TG_OP = 'DELETE' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id FROM old_rows o
            JOIN targets t ON t.target_id = o.target_id
            WHERE t.object_id IS NOT NULL), '{}'::integer[]);
    ELSE
        -- Only a publication flip or a re-targeting changes an object's
        -- published member set; everything else (hashes, flags, timestamps)
        -- is not a membership event.
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT t.object_id
            FROM old_rows o
            JOIN new_rows n ON n.id = o.id
            JOIN targets t ON t.target_id IN (o.target_id, n.target_id)
            WHERE t.object_id IS NOT NULL
              AND (o.deploy_status IS DISTINCT FROM n.deploy_status
                   OR o.target_id IS DISTINCT FROM n.target_id)), '{}'::integer[]);
    END IF;
    PERFORM public.recompute_needs_review(v_ids);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS recompute_needs_review_spectra_insert ON public.spectra;
CREATE TRIGGER recompute_needs_review_spectra_insert
  AFTER INSERT ON public.spectra REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

DROP TRIGGER IF EXISTS recompute_needs_review_spectra_update ON public.spectra;
CREATE TRIGGER recompute_needs_review_spectra_update
  AFTER UPDATE ON public.spectra
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

DROP TRIGGER IF EXISTS recompute_needs_review_spectra_delete ON public.spectra;
CREATE TRIGGER recompute_needs_review_spectra_delete
  AFTER DELETE ON public.spectra REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_spectra();

DROP FUNCTION IF EXISTS public.recompute_needs_review_from_targets CASCADE;

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_targets() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_ids integer[] := '{}';
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT o.object_id FROM old_rows o WHERE o.object_id IS NOT NULL), '{}'::integer[]);
    ELSE
        -- A target changing object: both the losing and the receiving object.
        v_ids := COALESCE(ARRAY(
            SELECT DISTINCT x.object_id
            FROM old_rows o
            JOIN new_rows n ON n.id = o.id
            CROSS JOIN LATERAL (VALUES (o.object_id), (n.object_id)) AS x(object_id)
            WHERE o.object_id IS DISTINCT FROM n.object_id
              AND x.object_id IS NOT NULL), '{}'::integer[]);
    END IF;
    PERFORM public.recompute_needs_review(v_ids);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS recompute_needs_review_targets_update ON public.targets;
CREATE TRIGGER recompute_needs_review_targets_update
  AFTER UPDATE ON public.targets
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_targets();

DROP TRIGGER IF EXISTS recompute_needs_review_targets_delete ON public.targets;
CREATE TRIGGER recompute_needs_review_targets_delete
  AFTER DELETE ON public.targets REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_targets();

DROP FUNCTION IF EXISTS public.recompute_needs_review_from_objects CASCADE;

CREATE OR REPLACE FUNCTION public.recompute_needs_review_from_objects() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    -- The rule's own inputs: quality, sign-off (last_inspected_at, which
    -- stamp_reviewed_spectra turns into a fresh reviewed set — in the list so
    -- "mark as reviewed" recomputes too), the reviewed set, activity.
    PERFORM public.recompute_needs_review(COALESCE(ARRAY(
        SELECT n.id
        FROM old_rows o
        JOIN new_rows n ON n.id = o.id
        WHERE o.redshift_quality IS DISTINCT FROM n.redshift_quality
           OR o.last_inspected_at IS DISTINCT FROM n.last_inspected_at
           OR o.reviewed_spectrum_ids IS DISTINCT FROM n.reviewed_spectrum_ids
           OR o.is_active IS DISTINCT FROM n.is_active), '{}'::integer[]));
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS recompute_needs_review_objects_update ON public.objects;
CREATE TRIGGER recompute_needs_review_objects_update
  AFTER UPDATE ON public.objects
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_needs_review_from_objects();


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
-- spectrum_line_fits.updated_at: any UPDATE (a re-fit replaces the whole row)
-- must advance updated_at so get_line_fits_for_sync's incremental cursor sees it.
-- Reuses bump_spectra_updated_at (it only sets NEW.updated_at = NOW()).
DROP TRIGGER IF EXISTS bump_spectrum_line_fits_updated_at_trigger ON public.spectrum_line_fits;
CREATE TRIGGER bump_spectrum_line_fits_updated_at_trigger
  BEFORE UPDATE ON public.spectrum_line_fits
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();

-- spectrum_lines is the unnested mirror of spectrum_line_fits.lines: rebuild a
-- spectrum's rows whenever its fit row is written (deploy upserts the whole
-- row; a re-fit that drops a line must drop its row too, hence delete +
-- insert rather than upsert). Deletes cascade through the FK.
DROP TRIGGER IF EXISTS sync_spectrum_lines_trigger ON public.spectrum_line_fits;
CREATE TRIGGER sync_spectrum_lines_trigger
  AFTER INSERT OR UPDATE OF lines ON public.spectrum_line_fits
  FOR EACH ROW EXECUTE FUNCTION public.sync_spectrum_lines();

DROP TRIGGER IF EXISTS sync_object_photometry_bands_trigger ON public.object_photometry;
CREATE TRIGGER sync_object_photometry_bands_trigger
  AFTER INSERT OR UPDATE OF photometry ON public.object_photometry
  FOR EACH ROW EXECUTE FUNCTION public.sync_object_photometry_bands();

DROP TRIGGER IF EXISTS bump_spectra_updated_at_trigger ON public.spectra;
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF
    dq_flags,
    redshift_auto,
    signal_to_noise,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    fits_path,
    file_hash,
    -- Science-content hash (P0b, decision D-P0-1): the backfill and every
    -- deploy write it; `campfire identify` reads it from the local mirror, so a
    -- change must enter the incremental delta.
    science_hash,
    -- zfit scalars on the row (perf T2-D2, #508): written by deploy and the
    -- backfill; a sync client showing the fit summary must see them change.
    chi2_min,
    confidence,
    -- Publication state: a publish must enter a non-admin client's incremental
    -- delta (a draft inserted before its sync cursor was otherwise never
    -- seen), and a revoke is what get_spectra_for_sync tombstones.
    deploy_status
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


-- =============================================================================
-- journal_sync_deletions: hard deletes from the synced tables -> sync_deletions
-- =============================================================================
-- Statement-level with a transition table, so a bulk delete (a photometry
-- supersede, `deploy remove`, a cascade from objects) journals in one INSERT.
-- TG_ARGV: (stream name, key column). SECURITY DEFINER: the deleting role may
-- be an admin session that cannot write the service-role-only journal.
CREATE OR REPLACE FUNCTION public.journal_sync_deletions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
BEGIN
    EXECUTE format(
        'INSERT INTO public.sync_deletions (stream, row_id) SELECT %L, %I FROM old_rows',
        TG_ARGV[0], TG_ARGV[1]);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.objects;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.objects REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('objects', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.spectra;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.spectra REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('spectra', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.storage_objects;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.storage_objects REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('storage', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.object_photometry;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.object_photometry REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('photometry', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.spectrum_line_fits;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.spectrum_line_fits REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('line_fits', 'spectrum_id');

-- ===========================================================================
-- Privilege hardening (P-1): trigger functions are not RPC endpoints
-- Postgres grants EXECUTE to PUBLIC on every new function, and the schema's
-- default privileges used to add anon on top; both made every RPC here callable
-- with the public anon key (findings §0.2). Explicit per-function revokes keep the
-- grants to authenticated / service_role that each function declares above.
-- Generated 2026-10-11 from the catalog; add a line here for every new function.
-- ===========================================================================
REVOKE ALL ON FUNCTION public.bump_object_version() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.bump_spectra_updated_at() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.enforce_object_user_update_scope() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.enforce_profile_role_update_scope() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.enforce_spectra_dq_user_update_scope() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.journal_sync_deletions() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.log_list_membership_change() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.log_object_inspection_changes() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.log_spectrum_dq_changes() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.pin_redshift_on_signoff() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.propagate_target_publication_to_shutters() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.propagate_target_scope_to_spectra() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.sync_object_photometry_bands() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.sync_shutter_publication() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.sync_spectra_target_scope() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.sync_spectrum_lines() FROM PUBLIC, anon;
-- GoTrue inserts auth.users as supabase_auth_admin; the trigger fires without an
-- EXECUTE check, but keep the grant explicit so the intent is readable.
GRANT EXECUTE ON FUNCTION public.handle_new_user() TO supabase_auth_admin;

-- ---------------------------------------------------------------------------
-- protect_archive_rows (data releases, P2)
--
-- Archive rows (root = 'archive') describe write-once objects: the content-
-- addressed store is the thing a DR and a manifest point at, so the registry
-- must not be able to forget or rewrite one. The only permitted change is the
-- mosaic-retention transition archived -> collected (status, with
-- collected_at assigned by the trigger, never by the caller); every other
-- column is frozen (whole-row comparison, so a new column is frozen by
-- default). Deletes are refused outright. Live rows are untouched. Bypass (for a genuine mistake) means
-- disabling the trigger as postgres in a migration, on purpose, with a note.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.protect_archive_rows() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.root = 'archive' THEN
      RAISE EXCEPTION 'archive registry rows are immutable: cannot delete %', OLD.storage_key
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
  END IF;
  IF TG_OP = 'INSERT' THEN
    -- collected_at is assigned by the archived -> collected transition, never
    -- supplied: an archived row starts without one, a row inserted as
    -- collected (a backfill) is stamped now.
    IF NEW.root = 'archive' THEN
      NEW.collected_at := CASE WHEN NEW.status = 'collected' THEN now() ELSE NULL END;
    END IF;
    RETURN NEW;
  END IF;
  -- UPDATE
  IF OLD.root <> 'archive' AND NEW.root = 'archive' THEN
    RAISE EXCEPTION 'a live registry row cannot become an archive row (%); insert the archive row instead', OLD.storage_key
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.root = 'archive' THEN
    -- The whole row is frozen except the status transition, its timestamp and
    -- the bookkeeping updated_at (bumped by the generic trigger).
    IF (to_jsonb(NEW) - 'status' - 'collected_at' - 'updated_at')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'collected_at' - 'updated_at')
    THEN
      RAISE EXCEPTION 'archive registry rows are immutable: % may only move archived -> collected', OLD.storage_key
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      IF NOT (OLD.status = 'archived' AND NEW.status = 'collected') THEN
        RAISE EXCEPTION 'archive registry row % status may only move archived -> collected (got % -> %)',
          OLD.storage_key, OLD.status, NEW.status USING ERRCODE = 'integrity_constraint_violation';
      END IF;
      NEW.collected_at := now();
    ELSIF NEW.collected_at IS DISTINCT FROM OLD.collected_at THEN
      RAISE EXCEPTION 'archive registry row %: collected_at follows the status transition', OLD.storage_key
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
-- P-1 convention: no PUBLIC / anon execute on any function.
REVOKE ALL ON FUNCTION public.protect_archive_rows() FROM PUBLIC, anon;

DROP TRIGGER IF EXISTS protect_archive_rows_trigger ON public.storage_objects;
CREATE TRIGGER protect_archive_rows_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON public.storage_objects
  FOR EACH ROW EXECUTE FUNCTION public.protect_archive_rows();


-- =============================================================================
-- is_public: planner-visible access scope (Catalog Read Model phase 1, #533)
-- =============================================================================
-- objects.is_public (any member program public), targets.is_public and
-- spectra.is_public (the row's program is public) are stored copies of a fact
-- that lives on programs, so the read policies can test a real column with
-- statistics before falling back to the opaque accessible_program_slugs()
-- array. Three triggers keep them exact: a BEFORE trigger on each table when
-- its program reference changes, and an AFTER trigger on programs when a
-- program's is_public flips (rare: `campfire config push --programs`), which
-- rewrites the rows of that program. Nothing else may write these columns:
-- the BEFORE triggers also fire on an explicit write of is_public itself and
-- recompute it, so a value supplied by any writer (PostgREST, a script, a
-- mistaken deploy) is replaced by the truth before the row lands, and the
-- update-scope enforcers reject the attempt for non-admins outright.
-- SECURITY DEFINER: the row writer (an inspector's PostgREST update, the
-- deploy's service-role upsert) need not be able to read programs.
--
-- Concurrency: the row-side triggers read programs FOR SHARE. Without it a
-- row inserted while a program is being made private could commit with the
-- flag computed from the old value (the propagation trigger cannot see an
-- uncommitted row) and stay public for good. FOR SHARE conflicts with the
-- row lock an UPDATE of programs takes, so the two paths serialize: whichever
-- starts first finishes first, and the other reads or rewrites the committed
-- state. A deploy holding object rows while a program flips holds its
-- program rows too, so in the worst case one side deadlocks and is retried;
-- a program flip is a rare admin action. python/tests/test_is_public_race.py
-- is the two-session check.
--
-- Spectra: program_slug is itself derived, by sync_spectra_target_scope
-- (BEFORE INSERT / UPDATE OF target_id, program_slug, observation), from the
-- parent target. An UPDATE OF event matches the statement's SET list, not a
-- column another BEFORE trigger rewrote, so the is_public trigger lists
-- target_id too and relies on firing AFTER the scope trigger: BEFORE ROW
-- triggers run in name order and 'sync_spectra_target_scope_trigger' sorts
-- before 'sync_spectrum_is_public_trigger'. Keep both names.

DROP FUNCTION IF EXISTS public.sync_object_is_public CASCADE;
CREATE OR REPLACE FUNCTION public.sync_object_is_public() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- Lock every referenced program row (public or not) so a concurrent flip
  -- of any of them waits for this row, or this row waits for it.
  NEW.is_public := COALESCE(
    (SELECT bool_or(l.is_public)
       FROM (SELECT p.is_public FROM programs p
              WHERE p.slug = ANY(NEW.programs) FOR SHARE) l),
    false);
  RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.sync_row_is_public CASCADE;
CREATE OR REPLACE FUNCTION public.sync_row_is_public() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  NEW.is_public := COALESCE(
    (SELECT p.is_public FROM programs p WHERE p.slug = NEW.program_slug FOR SHARE),
    false);
  RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.propagate_program_is_public CASCADE;
CREATE OR REPLACE FUNCTION public.propagate_program_is_public() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.is_public IS NOT DISTINCT FROM OLD.is_public THEN
    RETURN NULL;
  END IF;
  -- Program flips serialize among themselves. Two concurrent flips of
  -- different programs that share an object would each recompute it from a
  -- snapshot in which the other program is still public, both leave it
  -- public, and both commit (Codex on #651). With this transaction-scoped
  -- advisory lock the second flip's statements below start after the first
  -- has committed and see its value. Flips are rare admin actions; the lock
  -- costs nothing in normal operation.
  PERFORM pg_advisory_xact_lock(hashtext('campfire.is_public.propagate'));
  -- Only the rows whose value changes are written, so the per-row triggers
  -- on these tables (updated_at bumps, needs_review) see nothing else move.
  UPDATE targets t SET is_public = NEW.is_public
   WHERE t.program_slug = NEW.slug AND t.is_public IS DISTINCT FROM NEW.is_public;
  UPDATE spectra s SET is_public = NEW.is_public
   WHERE s.program_slug = NEW.slug AND s.is_public IS DISTINCT FROM NEW.is_public;
  UPDATE objects o SET is_public = v.pub
    FROM (SELECT o2.id,
                 EXISTS (SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(o2.programs)) AS pub
            FROM objects o2 WHERE NEW.slug = ANY(o2.programs)) v
   WHERE v.id = o.id AND o.is_public IS DISTINCT FROM v.pub;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS sync_object_is_public_trigger ON public.objects;
CREATE TRIGGER sync_object_is_public_trigger
  BEFORE INSERT OR UPDATE OF programs, is_public ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.sync_object_is_public();

DROP TRIGGER IF EXISTS sync_target_is_public_trigger ON public.targets;
CREATE TRIGGER sync_target_is_public_trigger
  BEFORE INSERT OR UPDATE OF program_slug, is_public ON public.targets
  FOR EACH ROW EXECUTE FUNCTION public.sync_row_is_public();

DROP TRIGGER IF EXISTS sync_spectrum_is_public_trigger ON public.spectra;
CREATE TRIGGER sync_spectrum_is_public_trigger
  BEFORE INSERT OR UPDATE OF target_id, program_slug, is_public ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.sync_row_is_public();

DROP TRIGGER IF EXISTS propagate_program_is_public_trigger ON public.programs;
CREATE TRIGGER propagate_program_is_public_trigger
  AFTER INSERT OR UPDATE OF is_public ON public.programs
  FOR EACH ROW EXECUTE FUNCTION public.propagate_program_is_public();


-- =============================================================================
-- Measurement rows addressable by object (Catalog Read Model phase 2a)
-- =============================================================================
-- spectrum_lines.object_id and object_photometry_bands.object_id are copies
-- of the object the row belongs to (targets.object_id through the spectrum;
-- object_photometry.object_id through the cross-match), and
-- spectrum_line_fits.stale_redshift is the stored value of
-- line_fit_stale_redshift(object, fit). They let the catalog filters and the
-- object page read one indexed table each instead of a three-hop join, and
-- they are exact because every path that can move them is covered:
--   * a line row is written            -> sync_spectrum_lines sets object_id
--   * a fit row is written             -> compute_line_fit_stale (BEFORE) sets stale_redshift
--   * a target regroups                -> relink_lines_from_targets (lines + stale)
--   * a spectrum moves to a target     -> relink_lines_from_spectra (lines + stale)
--   * an object is inspected / bumped  -> recompute_line_fit_stale_from_objects
--   * a cross-match is re-matched      -> relink_bands_from_photometry
--   * objects are rebuilt              -> the FKs' ON DELETE SET NULL, then the
--                                         target re-point above
-- An explicit write of stale_redshift is recomputed away by the BEFORE
-- trigger; the two object_id columns are only writable by admins (the
-- tables' insert/update policies) and are rewritten by the relinks.
-- check_measurement_object_links.sql asserts each path and the catalog-wide
-- invariants.

DROP FUNCTION IF EXISTS public.compute_line_fit_stale CASCADE;
CREATE OR REPLACE FUNCTION public.compute_line_fit_stale() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  NEW.stale_redshift := COALESCE(
    (SELECT public.line_fit_stale_redshift(
              o.version, o.redshift_quality, (o.redshift)::double precision,
              NEW.object_version, NEW.z_quality, NEW.z_used)
       FROM spectra s
       JOIN targets t ON t.target_id = s.target_id
       JOIN objects o ON o.id = t.object_id
      WHERE s.id = NEW.spectrum_id),
    false);
  RETURN NEW;
END;
$$;

-- Recompute stale_redshift for a set of spectra (their fits), writing only
-- rows whose value changes. Called by the statement triggers below.
DROP FUNCTION IF EXISTS public.recompute_line_fit_stale CASCADE;
CREATE OR REPLACE FUNCTION public.recompute_line_fit_stale(p_spectrum_ids integer[]) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  n integer;
BEGIN
  IF p_spectrum_ids IS NULL OR cardinality(p_spectrum_ids) = 0 THEN
    RETURN 0;
  END IF;
  UPDATE spectrum_line_fits f SET stale_redshift = v.stale
    FROM (SELECT f2.spectrum_id,
                 COALESCE(o.id IS NOT NULL AND public.line_fit_stale_redshift(
                   o.version, o.redshift_quality, (o.redshift)::double precision,
                   f2.object_version, f2.z_quality, f2.z_used), false) AS stale
            FROM spectrum_line_fits f2
            JOIN spectra s ON s.id = f2.spectrum_id
            LEFT JOIN targets t ON t.target_id = s.target_id
            LEFT JOIN objects o ON o.id = t.object_id
           WHERE f2.spectrum_id = ANY(p_spectrum_ids)) v
   WHERE v.spectrum_id = f.spectrum_id
     AND f.stale_redshift IS DISTINCT FROM v.stale;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

DROP FUNCTION IF EXISTS public.relink_lines_from_targets CASCADE;
CREATE OR REPLACE FUNCTION public.relink_lines_from_targets() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- statement trigger on every targets UPDATE (a transition table cannot be
  -- combined with UPDATE OF): keep only the rows whose object changed
  CREATE TEMP TABLE IF NOT EXISTS _moved_targets (target_id text) ON COMMIT DROP;
  TRUNCATE _moved_targets;
  INSERT INTO _moved_targets
    SELECT n.target_id FROM new_rows n JOIN old_rows o ON o.id = n.id
     WHERE o.object_id IS DISTINCT FROM n.object_id;
  IF NOT EXISTS (SELECT 1 FROM _moved_targets) THEN RETURN NULL; END IF;
  UPDATE spectrum_lines l SET object_id = t.object_id
    FROM spectra s
    JOIN _moved_targets m ON m.target_id = s.target_id
    JOIN targets t ON t.target_id = s.target_id
   WHERE l.spectrum_id = s.id
     AND l.object_id IS DISTINCT FROM t.object_id;
  PERFORM public.recompute_line_fit_stale(ARRAY(
    SELECT s.id FROM spectra s JOIN _moved_targets m ON m.target_id = s.target_id));
  RETURN NULL;
END;
$$;

DROP FUNCTION IF EXISTS public.relink_lines_from_spectra CASCADE;
CREATE OR REPLACE FUNCTION public.relink_lines_from_spectra() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- statement trigger on every spectra UPDATE: keep the rows whose target changed
  UPDATE spectrum_lines l SET object_id = t.object_id
    FROM new_rows n
    JOIN old_rows o ON o.id = n.id AND o.target_id IS DISTINCT FROM n.target_id
    JOIN targets t ON t.target_id = n.target_id
   WHERE l.spectrum_id = n.id
     AND l.object_id IS DISTINCT FROM t.object_id;
  PERFORM public.recompute_line_fit_stale(ARRAY(
    SELECT n.id FROM new_rows n JOIN old_rows o ON o.id = n.id
     WHERE o.target_id IS DISTINCT FROM n.target_id));
  RETURN NULL;
END;
$$;

DROP FUNCTION IF EXISTS public.recompute_line_fit_stale_from_objects CASCADE;
CREATE OR REPLACE FUNCTION public.recompute_line_fit_stale_from_objects() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- statement trigger on every objects UPDATE: keep the rows whose redshift,
  -- quality or version changed (redshift is generated from the inspected /
  -- auto values and the quality)
  PERFORM public.recompute_line_fit_stale(ARRAY(
    SELECT s.id
      FROM new_rows n
      JOIN old_rows o ON o.id = n.id
       AND (o.version IS DISTINCT FROM n.version
            OR o.redshift_quality IS DISTINCT FROM n.redshift_quality
            OR o.redshift IS DISTINCT FROM n.redshift)
      JOIN targets t ON t.object_id = n.id
      JOIN spectra s ON s.target_id = t.target_id));
  RETURN NULL;
END;
$$;

DROP FUNCTION IF EXISTS public.relink_bands_from_photometry CASCADE;
CREATE OR REPLACE FUNCTION public.relink_bands_from_photometry() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  UPDATE object_photometry_bands b SET object_id = NEW.object_id
   WHERE b.photometry_id = NEW.id
     AND b.object_id IS DISTINCT FROM NEW.object_id;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS compute_line_fit_stale_trigger ON public.spectrum_line_fits;
CREATE TRIGGER compute_line_fit_stale_trigger
  BEFORE INSERT OR UPDATE ON public.spectrum_line_fits
  FOR EACH ROW EXECUTE FUNCTION public.compute_line_fit_stale();

DROP TRIGGER IF EXISTS relink_lines_from_targets_trigger ON public.targets;
CREATE TRIGGER relink_lines_from_targets_trigger
  AFTER UPDATE ON public.targets
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.relink_lines_from_targets();

DROP TRIGGER IF EXISTS relink_lines_from_spectra_trigger ON public.spectra;
CREATE TRIGGER relink_lines_from_spectra_trigger
  AFTER UPDATE ON public.spectra
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.relink_lines_from_spectra();

-- Fires on every objects UPDATE (a transition table cannot be combined with
-- UPDATE OF); the function keeps only the rows whose redshift, quality or
-- version changed, which inspection and reconciliation are the writers of.
DROP TRIGGER IF EXISTS recompute_line_fit_stale_from_objects_trigger ON public.objects;
CREATE TRIGGER recompute_line_fit_stale_from_objects_trigger
  AFTER UPDATE ON public.objects
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.recompute_line_fit_stale_from_objects();

DROP TRIGGER IF EXISTS relink_bands_from_photometry_trigger ON public.object_photometry;
CREATE TRIGGER relink_bands_from_photometry_trigger
  AFTER UPDATE OF object_id ON public.object_photometry
  FOR EACH ROW EXECUTE FUNCTION public.relink_bands_from_photometry();
