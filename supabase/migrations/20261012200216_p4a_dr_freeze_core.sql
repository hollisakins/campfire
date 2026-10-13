SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE TABLE "public"."dr_exports" (
  "tag"         text                     NOT NULL,
  "stream"      text                     NOT NULL,
  "partition"   text                     NOT NULL,
  "storage_key" text,
  "sha256"      text,
  "size_bytes"  bigint,
  "rows"        integer,
  "state"       text                     NOT NULL DEFAULT 'pending'::text,
  "built_at"    timestamp with time zone,
  "error"       text,
  CONSTRAINT "dr_exports_pkey" PRIMARY KEY (TAG, stream, PARTITION),
  CONSTRAINT "dr_exports_state_check" CHECK ((state = ANY (ARRAY['pending'::text, 'building'::text, 'ready'::text, 'failed'::text])))
);

ALTER TABLE "public"."dr_exports"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_exports" FROM "anon";

CREATE TABLE "public"."dr_line_fits" (
  "tag"                   text                     NOT NULL,
  "spectrum_id"           integer                  NOT NULL,
  "target_id"             text                     NOT NULL,
  "grating"               text                     NOT NULL,
  "program_slug"          text                     NOT NULL,
  "observation"           text                     NOT NULL,
  "object_id"             text,
  "object_version"        integer,
  "z_used"                double precision         NOT NULL,
  "z_source"              text                     NOT NULL,
  "z_quality"             integer                  NOT NULL,
  "z_fit"                 double precision,
  "z_fit_err"             double precision,
  "dv"                    double precision,
  "dv_err"                double precision,
  "sigma_v"               double precision,
  "sigma_v_err"           double precision,
  "kin_source"            text,
  "n_lines"               integer                  NOT NULL DEFAULT 0,
  "n_detected"            integer                  NOT NULL DEFAULT 0,
  "n_broad"               integer                  NOT NULL DEFAULT 0,
  "chi2"                  double precision,
  "dof"                   integer,
  "lines"                 jsonb                    NOT NULL DEFAULT '{}'::jsonb,
  "fit_version"           text                     NOT NULL,
  "cfpipe_version"        text,
  "f_lsf"                 double precision,
  "spectrum_hash"         text,
  "spectrum_science_hash" text,
  "fitted_at"             timestamp with time zone,
  "stale_redshift"        boolean                  NOT NULL DEFAULT false,
  "stale_spectrum"        boolean                  NOT NULL DEFAULT false,
  CONSTRAINT "dr_line_fits_pkey" PRIMARY KEY (TAG, spectrum_id)
);

ALTER TABLE "public"."dr_line_fits"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "anon";

CREATE TABLE "public"."dr_members" (
  "tag"          text                     NOT NULL,
  "storage_key"  text                     NOT NULL,
  "component"    text                     NOT NULL,
  "program_slug" text,
  "field"        text,
  "product_type" text                     NOT NULL,
  "spectrum_id"  text,
  "backend"      text                     NOT NULL,
  "bucket"       text                     NOT NULL,
  "content_hash" text                     NOT NULL,
  "science_hash" text,
  "size_bytes"   bigint,
  "registry_id"  bigint,
  "archive_key"  text,
  "byte_state"   text                     NOT NULL DEFAULT 'pending'::text,
  "stamped_hash" text,
  "byte_error"   text,
  "updated_at"   timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "dr_members_byte_state_check" CHECK ((byte_state = ANY (ARRAY['pending'::text, 'copied'::text, 'stamped'::text, 'failed'::text]))),
  CONSTRAINT "dr_members_pkey" PRIMARY KEY (TAG, storage_key)
);

ALTER TABLE "public"."dr_members"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_members" FROM "anon";

CREATE TABLE "public"."dr_objects" (
  "tag"                  text                     NOT NULL,
  "object_id"            integer                  NOT NULL,
  "name"                 text                     NOT NULL,
  "field"                text                     NOT NULL,
  "ra"                   double precision         NOT NULL,
  "dec"                  double precision         NOT NULL,
  "programs"             text[]                   NOT NULL DEFAULT '{}'::text[],
  "evidence_programs"    text[]                   NOT NULL DEFAULT '{}'::text[],
  "observations"         text[]                   NOT NULL DEFAULT '{}'::text[],
  "gratings"             text[]                   NOT NULL DEFAULT '{}'::text[],
  "n_targets"            integer                  NOT NULL DEFAULT 0,
  "n_spectra"            integer                  NOT NULL DEFAULT 0,
  "max_snr"              double precision,
  "max_exposure_time"    double precision,
  "photo_z"              double precision,
  "photo_z_err_lo"       double precision,
  "photo_z_err_hi"       double precision,
  "has_photometry"       boolean                  NOT NULL DEFAULT false,
  "z_source_spectrum_id" integer,
  "redshift_auto"        double precision,
  "redshift_inspected"   numeric(10,6),
  "redshift"             numeric(10,6),
  "redshift_quality"     integer                  NOT NULL DEFAULT 0,
  "inspected_used_auto"  boolean                  NOT NULL DEFAULT false,
  "last_inspected_at"    timestamp with time zone,
  "last_inspected_by"    uuid,
  "version"              integer                  NOT NULL DEFAULT 1,
  "review_state"         jsonb                    NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT "dr_objects_pkey" PRIMARY KEY (TAG, object_id)
);

ALTER TABLE "public"."dr_objects"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_objects" FROM "anon";

CREATE TABLE "public"."dr_photometry" (
  "tag"                   text             NOT NULL,
  "photometry_id"         integer          NOT NULL,
  "object_id"             integer          NOT NULL,
  "programs"              text[]           NOT NULL DEFAULT '{}'::text[],
  "field"                 text             NOT NULL,
  "catalog_name"          text             NOT NULL,
  "catalog_id"            text,
  "ra"                    double precision NOT NULL,
  "dec"                   double precision NOT NULL,
  "match_distance_arcsec" double precision,
  "photometry"            jsonb            NOT NULL,
  "photo_z"               double precision,
  "photo_z_err_lo"        double precision,
  "photo_z_err_hi"        double precision,
  "has_pz"                boolean          NOT NULL DEFAULT false,
  CONSTRAINT "dr_photometry_pkey" PRIMARY KEY (TAG, photometry_id)
);

ALTER TABLE "public"."dr_photometry"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_photometry" FROM "anon";

CREATE TABLE "public"."dr_releases" (
  "tag"                text                     NOT NULL,
  "state"              text                     NOT NULL DEFAULT 'draft'::text,
  "components"         jsonb                    NOT NULL DEFAULT '[]'::jsonb,
  "dr_format_version"  integer                  NOT NULL DEFAULT 1,
  "created_at"         timestamp with time zone NOT NULL DEFAULT now(),
  "created_by"         uuid,
  "freeze_started_at"  timestamp with time zone,
  "frozen_at"          timestamp with time zone,
  "frozen_by"          uuid,
  "freeze_duration_ms" integer,
  "snapshot_xmin"      bigint,
  "counts"             jsonb                    NOT NULL DEFAULT '{}'::jsonb,
  "error"              text,
  "published_at"       timestamp with time zone,
  "published_by"       uuid,
  "withdrawn_at"       timestamp with time zone,
  "withdrawn_by"       uuid,
  "doi"                text,
  "notes"              text,
  "known_issues"       text,
  "updated_at"         timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "dr_releases_pkey" PRIMARY KEY (TAG),
  CONSTRAINT "dr_releases_state_check"
    CHECK
    ((state = ANY (ARRAY['draft'::text, 'freezing'::text, 'frozen'::text, 'failed'::text, 'exporting'::text, 'exported'::text, 'copying'::text, 'ready'::text, 'published'::text,
    'withdrawn'::text]))),
  CONSTRAINT "dr_releases_tag_check" CHECK ((tag ~ '^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$'::text))
);

ALTER TABLE "public"."dr_releases"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_releases" FROM "anon";

CREATE TABLE "public"."dr_spectra" (
  "tag"             text                     NOT NULL,
  "spectrum_id"     text                     NOT NULL,
  "id"              integer                  NOT NULL,
  "program_slug"    text                     NOT NULL,
  "observation"     text                     NOT NULL,
  "grating"         text                     NOT NULL,
  "target_id"       text                     NOT NULL,
  "object_id"       integer,
  "fits_path"       text                     NOT NULL,
  "file_hash"       text,
  "science_hash"    text,
  "file_size"       bigint,
  "exposure_time"   double precision,
  "signal_to_noise" double precision,
  "cfpipe_version"  text,
  "crds_context"    text,
  "jwst_version"    text,
  "date_obs"        text,
  "reduced_at"      timestamp with time zone,
  "redshift_auto"   double precision,
  "chi2_min"        double precision,
  "confidence"      double precision,
  "dq_flags"        integer                  NOT NULL DEFAULT 0,
  CONSTRAINT "dr_spectra_pkey" PRIMARY KEY (TAG, spectrum_id)
);

ALTER TABLE "public"."dr_spectra"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."dr_spectra" FROM "anon";

CREATE OR REPLACE FUNCTION public.begin_deploy_scope (
  p_scope_type text,
  p_scope_key  text,
  p_actor      uuid    DEFAULT NULL::uuid,
  p_host       text    DEFAULT NULL::text,
  p_steal      boolean DEFAULT false
)
  RETURNS json
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_is_admin boolean;
  v_row deploy_scope_state%ROWTYPE;
  v_live boolean;
  v_previous json;
  v_version integer;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_scope_type NOT IN ('observation', 'field') THEN
    RAISE EXCEPTION 'Invalid scope_type: %', p_scope_type;
  END IF;

  -- Data-release interlock (P4a): a freeze snapshot must not see a deploy's
  -- partial catalog update and a deploy must not start into a freeze. The
  -- same advisory lock is held by dr_freeze_job for the whole freeze
  -- transaction, so a begin that arrives mid-freeze waits for it to commit
  -- (~15 s on the full catalog); a release left in `freezing` (enqueued, the
  -- pg_cron job not yet run) refuses the deploy outright.
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_freeze'));
  IF EXISTS (SELECT 1 FROM dr_releases r WHERE r.state = 'freezing'
              AND r.freeze_started_at > now() - interval '1 hour') THEN
    RAISE EXCEPTION 'deploy_scope_busy: a data-release freeze is running (%); retry when `campfire dr status` shows it frozen',
      (SELECT string_agg(r.tag, ', ') FROM dr_releases r WHERE r.state = 'freezing'
        AND r.freeze_started_at > now() - interval '1 hour')
      USING ERRCODE = 'CF409',
            HINT = 'A freeze takes under a minute on production; --steal-lease does not apply.';
  END IF;

  INSERT INTO deploy_scope_state (scope_type, scope_key)
  VALUES (p_scope_type, p_scope_key)
  ON CONFLICT (scope_type, scope_key) DO NOTHING;

  -- Row lock: two deploys starting at the same instant serialize here, so the
  -- second one sees the first one's lease and is refused.
  SELECT * INTO v_row FROM deploy_scope_state
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key
  FOR UPDATE;

  v_live := v_row.in_progress_since IS NOT NULL
            AND COALESCE(v_row.heartbeat_at, v_row.in_progress_since) > now() - deploy_lease_ttl();

  IF v_row.in_progress_since IS NOT NULL THEN
    v_previous := json_build_object(
      'in_progress_by', v_row.in_progress_by,
      'in_progress_host', v_row.in_progress_host,
      'in_progress_since', v_row.in_progress_since,
      'heartbeat_at', v_row.heartbeat_at,
      'live', v_live);
  END IF;

  IF v_live AND NOT p_steal THEN
    RAISE EXCEPTION 'deploy_scope_busy: % % is being deployed by % from % since % (last heartbeat %)',
      p_scope_type, p_scope_key,
      COALESCE(v_row.in_progress_by::text, 'service-role'),
      COALESCE(v_row.in_progress_host, 'unknown host'),
      to_char(v_row.in_progress_since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS"Z"'),
      to_char(COALESCE(v_row.heartbeat_at, v_row.in_progress_since) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS"Z"')
      USING ERRCODE = 'CF409',
            DETAIL = v_previous::text,
            HINT = 'Wait for that deploy to finish, or re-run with --steal-lease if you are certain it is dead.';
  END IF;

  -- The holder identity the heartbeat / release RPCs match against: the
  -- explicit actor, else the calling user (NULL under service_role). The
  -- version bump is the holder's token (see the block comment above).
  UPDATE deploy_scope_state
  SET version = version + 1,
      in_progress_since = now(),
      in_progress_by = COALESCE(p_actor, auth.uid()),
      in_progress_host = p_host,
      heartbeat_at = now(),
      updated_at = now()
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key
  RETURNING version INTO v_version;

  RETURN json_build_object(
    'version', v_version,
    'took_over_stale', (v_row.in_progress_since IS NOT NULL AND NOT v_live),
    'stolen', (v_live AND p_steal),
    'previous', v_previous);
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_cron_unschedule (
  p_tag text
)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_job text := 'dr-freeze-' || p_tag;
  v_exists boolean;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
    RETURN;
  END IF;
  EXECUTE 'SELECT EXISTS (SELECT 1 FROM cron.job WHERE jobname = $1)' INTO v_exists USING v_job;
  IF v_exists THEN
    EXECUTE 'SELECT cron.unschedule($1)' USING v_job;
  END IF;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_cron_unschedule"(text) FROM PUBLIC, "anon", "authenticated";

CREATE OR REPLACE FUNCTION public.dr_enqueue_freeze (
  p_tag        text,
  p_components jsonb,
  p_notes      text    DEFAULT NULL::text,
  p_inline     boolean DEFAULT false
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '900s'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  comp record;
  v_job text := 'dr-freeze-' || p_tag;
  v_mode text;
BEGIN
  PERFORM dr_require_admin();

  IF p_tag IS NULL OR p_tag !~ '^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$' THEN
    RAISE EXCEPTION 'invalid tag % (letters, digits, ., _ and -; 2-64 characters)', p_tag;
  END IF;
  SELECT * INTO comp FROM dr_parse_components(p_components);

  -- A deploy in flight would make the frozen rows depend on when the
  -- statement started relative to its upserts; readiness refuses to begin.
  IF EXISTS (SELECT 1 FROM deploy_scope_state d
              WHERE d.in_progress_since IS NOT NULL
                AND COALESCE(d.heartbeat_at, d.in_progress_since) >= now() - deploy_lease_ttl()) THEN
    RAISE EXCEPTION 'a deploy is in progress (live deploy lease); wait for it to finish or release it (campfire dr status lists leases)';
  END IF;

  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF FOUND THEN
    IF r.state IN ('published', 'withdrawn') THEN
      RAISE EXCEPTION 'release % is %; a DR is immutable once published (make a new tag)', p_tag, r.state;
    ELSIF r.state = 'freezing' AND r.freeze_started_at > now() - interval '1 hour' THEN
      RAISE EXCEPTION 'release % is already freezing (started %)', p_tag, r.freeze_started_at;
    ELSIF r.state IN ('exporting', 'exported', 'copying', 'ready') THEN
      RAISE EXCEPTION 'release % already has exports or archive bytes (state %); purge it first (campfire dr purge %)', p_tag, r.state, p_tag;
    END IF;
    UPDATE dr_releases
       SET state = 'freezing', components = p_components, notes = COALESCE(p_notes, notes),
           created_by = COALESCE(created_by, auth.uid()),
           freeze_started_at = now(), frozen_at = NULL, frozen_by = NULL,
           freeze_duration_ms = NULL, snapshot_xmin = NULL, counts = '{}'::jsonb,
           error = NULL, updated_at = now()
     WHERE tag = p_tag;
  ELSE
    INSERT INTO dr_releases (tag, state, components, notes, created_by, freeze_started_at)
    VALUES (p_tag, 'freezing', p_components, p_notes, auth.uid(), now());
  END IF;

  IF NOT p_inline AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
    -- One-shot via pg_cron's `[1-59] seconds` interval syntax (pg_cron >= 1.5;
    -- production runs 1.6): the job starts within 5 s of this commit, with no
    -- calendar-minute boundary to miss, and dr_freeze_job unschedules it as
    -- its first action (pg_cron never overlaps runs of one job, so a slow
    -- freeze cannot be started twice).
    PERFORM dr_cron_unschedule(p_tag);
    EXECUTE 'SELECT cron.schedule($1, $2, $3)'
      USING v_job, '5 seconds', format('SELECT public.dr_freeze_job(%L)', p_tag);
    v_mode := 'cron';
  ELSE
    PERFORM dr_freeze_job(p_tag);
    v_mode := 'inline';
  END IF;

  SELECT * INTO r FROM dr_releases WHERE tag = p_tag;
  RETURN jsonb_build_object('tag', r.tag, 'state', r.state, 'mode', v_mode,
                            'components', r.components, 'counts', r.counts, 'error', r.error,
                            'freeze_started_at', r.freeze_started_at, 'frozen_at', r.frozen_at,
                            'freeze_duration_ms', r.freeze_duration_ms);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_freeze_job (
  p_tag text
)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '900s'
  SET lock_timeout TO '30s'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  comp record;
  v_t0 timestamptz := clock_timestamp();
  v_n_spectra bigint; v_n_objects bigint; v_n_lines bigint; v_n_phot bigint; v_n_members bigint;
  v_xmin bigint;
  v_missing text;
  v_err text;
BEGIN
  -- Only the cron runner / seed (postgres) or an admin / service caller.
  IF NOT (session_user = 'postgres' OR current_user = 'postgres'
          OR COALESCE(auth.role(), '') = 'service_role'
          OR (SELECT public.is_admin())) THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  -- The one-shot cron job is a `5 seconds` schedule (dr_enqueue_freeze): drop
  -- it FIRST so whatever this run does, the commit removes the job. A wrong
  -- state is reported and returns (never raised: a raise would roll the
  -- unschedule back and the job would fire again every 5 s).
  PERFORM dr_cron_unschedule(p_tag);
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN
    RAISE NOTICE 'dr_freeze_job: no release %', p_tag;
    RETURN;
  END IF;
  IF r.state <> 'freezing' THEN
    RAISE NOTICE 'dr_freeze_job: release % is in state % (expected freezing); nothing done', p_tag, r.state;
    RETURN;
  END IF;

  -- Interlock with begin_deploy_scope (which takes the same lock and refuses
  -- while a release is `freezing`): held for the whole transaction, so no
  -- deploy begins between here and the commit. A lease taken between the
  -- enqueue and this run (the pg_cron delay) fails the attempt cleanly.
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_freeze'));

  BEGIN
    IF EXISTS (SELECT 1 FROM deploy_scope_state d
                WHERE d.in_progress_since IS NOT NULL
                  AND COALESCE(d.heartbeat_at, d.in_progress_since) >= now() - deploy_lease_ttl()) THEN
      RAISE EXCEPTION 'a deploy started after the freeze was enqueued (live deploy lease); re-run the freeze when it has finished';
    END IF;
    SELECT * INTO comp FROM dr_parse_components(r.components);

    -- Partial rows of a previous attempt under this tag.
    DELETE FROM dr_members    WHERE tag = p_tag;
    DELETE FROM dr_line_fits  WHERE tag = p_tag;
    DELETE FROM dr_photometry WHERE tag = p_tag;
    DELETE FROM dr_spectra    WHERE tag = p_tag;
    DELETE FROM dr_objects    WHERE tag = p_tag;
    DELETE FROM dr_exports    WHERE tag = p_tag;

    -- ONE statement: every sub-statement sees the same snapshot.
    WITH
    pub AS MATERIALIZED (
      -- Published spectra (nirspec component) with the active object they
      -- roll up to (NULL while a target has no active object).
      SELECT s.id, s.spectrum_id, s.program_slug, s.observation, s.grating, s.target_id,
             o.id AS obj_id, s.fits_path, s.file_hash, s.science_hash, s.file_size,
             s.exposure_time, s.signal_to_noise, s.cfpipe_version, s.crds_context,
             s.jwst_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min,
             s.confidence, s.dq_flags,
             CASE s.grating
               WHEN 'PRISM' THEN 0 WHEN 'G395M' THEN 1 WHEN 'G395H' THEN 2
               WHEN 'G235M' THEN 3 WHEN 'G235H' THEN 4 WHEN 'G140M' THEN 5
               WHEN 'G140H' THEN 6 ELSE 7 END AS prio
      FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      LEFT JOIN objects o ON o.id = t.object_id AND o.is_active
      WHERE comp.nirspec AND s.deploy_status = 'published'
    ),
    ev AS (
      -- Evidence programs per object: the programs of its published members.
      SELECT p.obj_id, array_agg(DISTINCT p.program_slug ORDER BY p.program_slug) AS programs
      FROM pub p WHERE p.obj_id IS NOT NULL GROUP BY p.obj_id
    ),
    -- Auto-redshift source: the compute_object_redshift_auto rule, set-based.
    m AS (
      SELECT p.obj_id, p.id, p.redshift_auto AS z, p.exposure_time, p.prio
      FROM pub p WHERE p.obj_id IS NOT NULL AND p.redshift_auto IS NOT NULL
    ),
    pz AS (
      SELECT DISTINCT ON (obj_id) obj_id, z AS prism_z FROM m WHERE prio = 0
      ORDER BY obj_id, exposure_time DESC NULLS LAST, id
    ),
    base AS (
      SELECT DISTINCT ON (obj_id) obj_id, id, z FROM m
      ORDER BY obj_id, prio, exposure_time DESC NULLS LAST, id
    ),
    refined AS (
      SELECT DISTINCT ON (m.obj_id) m.obj_id, m.id, m.z
      FROM m JOIN pz ON pz.obj_id = m.obj_id
      WHERE m.prio BETWEEN 1 AND 6 AND abs(m.z - pz.prism_z) / (1.0 + pz.prism_z) < 0.01
      ORDER BY m.obj_id, m.prio, m.exposure_time DESC NULLS LAST, m.id
    ),
    zsrc AS (
      SELECT b.obj_id, COALESCE(rf.id, b.id) AS id, COALESCE(rf.z, b.z) AS z
      FROM base b LEFT JOIN refined rf ON rf.obj_id = b.obj_id
    ),
    agg AS (
      -- Aggregates over the PUBLISHED members only (the live objects row counts
      -- draft / revoked members too and would make the frozen row inconsistent
      -- with the released spectra).
      SELECT p.obj_id, count(DISTINCT p.target_id) AS n_targets, count(*) AS n_spectra,
             array_agg(DISTINCT p.grating ORDER BY p.grating) AS gratings,
             array_agg(DISTINCT p.observation ORDER BY p.observation) AS observations,
             max(p.signal_to_noise) AS max_snr, max(p.exposure_time) AS max_exposure_time
      FROM pub p WHERE p.obj_id IS NOT NULL GROUP BY p.obj_id
    ),
    phz AS (
      -- photo-z from the SELECTED catalogs only, with the live rule (the newest
      -- cross-match, sync_photometry_to_objects); none selected -> NULL / false,
      -- so dr_objects never advertises photometry dr_photometry does not carry
      SELECT DISTINCT ON (ph.object_id) ph.object_id, ph.photo_z, ph.photo_z_err_lo, ph.photo_z_err_hi
      FROM object_photometry ph
      WHERE ph.object_id IS NOT NULL
        AND (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
      ORDER BY ph.object_id, ph.updated_at DESC
    ),
    objs AS MATERIALIZED (
      SELECT o.id, o.object_id, o.field, o.ra, o.dec,
             -- the frozen access scope IS the evidence: programs of the
             -- published members (a program whose only member is a draft has
             -- nothing in the release)
             ev.programs AS programs, ev.programs AS evidence_programs,
             agg.observations, agg.gratings, agg.n_targets::int AS n_targets, agg.n_spectra::int AS n_spectra,
             agg.max_snr, agg.max_exposure_time,
             phz.photo_z, phz.photo_z_err_lo, phz.photo_z_err_hi, (phz.object_id IS NOT NULL) AS has_photometry,
             zsrc.id AS z_source_spectrum_id,
             -- the auto redshift of the published anchor spectrum (the live
             -- value may come from an excluded member)
             zsrc.z AS redshift_auto,
             o.redshift_inspected,
             (CASE WHEN o.redshift_quality = 1 THEN NULL
                   ELSE COALESCE(o.redshift_inspected::double precision, zsrc.z) END)::numeric(10,6) AS redshift,
             o.redshift_quality, o.inspected_used_auto, o.last_inspected_at, o.last_inspected_by,
             o.version, o.needs_review, o.reviewed_spectrum_ids
      FROM objects o
      JOIN ev ON ev.obj_id = o.id
      JOIN agg ON agg.obj_id = o.id
      LEFT JOIN zsrc ON zsrc.obj_id = o.id
      LEFT JOIN phz ON phz.object_id = o.id
    ),
    ins_spectra AS (
      INSERT INTO dr_spectra (tag, spectrum_id, id, program_slug, observation, grating, target_id,
                              object_id, fits_path, file_hash, science_hash, file_size, exposure_time,
                              signal_to_noise, cfpipe_version, crds_context, jwst_version, date_obs,
                              reduced_at, redshift_auto, chi2_min, confidence, dq_flags)
      SELECT p_tag, p.spectrum_id, p.id, p.program_slug, p.observation, p.grating, p.target_id,
             p.obj_id, p.fits_path, p.file_hash, p.science_hash, p.file_size, p.exposure_time,
             p.signal_to_noise, p.cfpipe_version, p.crds_context, p.jwst_version, p.date_obs,
             p.reduced_at, p.redshift_auto, p.chi2_min, p.confidence, p.dq_flags
      FROM pub p
      RETURNING 1
    ),
    ins_objects AS (
      INSERT INTO dr_objects (tag, object_id, name, field, ra, dec, programs, evidence_programs,
                              observations, gratings, n_targets, n_spectra, max_snr, max_exposure_time,
                              photo_z, photo_z_err_lo, photo_z_err_hi, has_photometry,
                              z_source_spectrum_id, redshift_auto, redshift_inspected, redshift,
                              redshift_quality, inspected_used_auto, last_inspected_at,
                              last_inspected_by, version, review_state)
      SELECT p_tag, o.id, o.object_id, o.field, o.ra, o.dec, o.programs, o.evidence_programs,
             o.observations, o.gratings, o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time,
             o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry,
             o.z_source_spectrum_id, o.redshift_auto, o.redshift_inspected, o.redshift,
             o.redshift_quality, o.inspected_used_auto, o.last_inspected_at,
             o.last_inspected_by, o.version,
             jsonb_build_object('needs_review', o.needs_review,
                                'reviewed_spectrum_ids', o.reviewed_spectrum_ids)
      FROM objs o
      RETURNING 1
    ),
    ins_lines AS (
      INSERT INTO dr_line_fits (tag, spectrum_id, target_id, grating, program_slug, observation,
                                object_id, object_version, z_used, z_source, z_quality, z_fit,
                                z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source, n_lines,
                                n_detected, n_broad, chi2, dof, lines, fit_version, cfpipe_version,
                                f_lsf, spectrum_hash, spectrum_science_hash, fitted_at,
                                stale_redshift, stale_spectrum)
      SELECT p_tag, f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation,
             f.object_id, f.object_version, f.z_used, f.z_source, f.z_quality, f.z_fit,
             f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source, f.n_lines,
             f.n_detected, f.n_broad, f.chi2, f.dof, f.lines, f.fit_version, f.cfpipe_version,
             f.f_lsf, f.spectrum_hash, f.spectrum_science_hash, f.fitted_at,
             -- the spectrum_line_fits_status predicates, in this snapshot
             (o.id IS NOT NULL AND line_fit_stale_redshift(o.version, o.redshift_quality,
                 o.redshift::double precision, f.object_version, f.z_quality, f.z_used)),
             line_fit_stale_spectrum(p.file_hash, p.science_hash, f.spectrum_hash, f.spectrum_science_hash)
      FROM spectrum_line_fits f
      JOIN pub p ON p.id = f.spectrum_id
      LEFT JOIN objs o ON o.id = p.obj_id
      RETURNING 1
    ),
    ins_phot AS (
      INSERT INTO dr_photometry (tag, photometry_id, object_id, programs, field, catalog_name,
                                 catalog_id, ra, dec, match_distance_arcsec, photometry, photo_z,
                                 photo_z_err_lo, photo_z_err_hi, has_pz)
      SELECT p_tag, ph.id, ph.object_id, o.programs, ph.field, ph.catalog_name, ph.catalog_id,
             ph.ra, ph.dec, ph.match_distance_arcsec, ph.photometry, ph.photo_z,
             ph.photo_z_err_lo, ph.photo_z_err_hi, ph.has_pz
      FROM object_photometry ph
      JOIN objs o ON o.id = ph.object_id
      WHERE (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
      RETURNING 1
    ),
    ins_members AS (
      INSERT INTO dr_members (tag, storage_key, component, program_slug, field, product_type,
                              spectrum_id, backend, bucket, content_hash, science_hash, size_bytes,
                              registry_id)
      -- NIRSpec finals + the sidecars registered with a spectrum_id, joined
      -- FROM the published spectra.
      SELECT p_tag, so.storage_key, 'nirspec', p.program_slug, NULL, so.product_type,
             p.spectrum_id, so.backend, so.bucket, so.content_hash, so.science_hash,
             so.size_bytes, so.id
      FROM pub p
      JOIN storage_objects so ON so.spectrum_id = p.spectrum_id
      WHERE so.root = 'live' AND so.status = 'active'
        AND so.product_type IN ('nirspec_spec', 'spectrum_json', 'spectrum_1d_json', 'zfit',
                                'nirspec_lines', 'nirspec_lines_json')
      UNION ALL
      -- The zfit / line-fit sidecars deploy registered WITHOUT a spectrum_id
      -- (every zfit, nirspec_lines and nirspec_lines_json row in production on
      -- 2026-10-12): the layout puts them next to the spectrum under the same
      -- base name (<base>_spec.fits -> <base>_zfit.json, _lines.fits,
      -- _lines.json), so derive the key from the spectrum's registry row.
      SELECT p_tag, so.storage_key, 'nirspec', c.program_slug, NULL, so.product_type,
             c.spectrum_id, so.backend, so.bucket, so.content_hash, so.science_hash,
             so.size_bytes, so.id
      FROM (
        SELECT p.spectrum_id, p.program_slug, p.observation,
               regexp_replace(spec.storage_key, '_spec\.fits$', '') || sfx AS key
        FROM pub p
        JOIN storage_objects spec ON spec.spectrum_id = p.spectrum_id
                                 AND spec.product_type = 'nirspec_spec'
                                 AND spec.root = 'live' AND spec.status = 'active'
        CROSS JOIN unnest(ARRAY['_zfit.json', '_lines.fits', '_lines.json']) AS sfx
      ) c
      JOIN storage_objects so ON so.storage_key = c.key
      WHERE so.spectrum_id IS NULL AND so.root = 'live' AND so.status = 'active'
        AND so.product_type IN ('zfit', 'nirspec_lines', 'nirspec_lines_json')
        AND so.observation IS NOT DISTINCT FROM c.observation
      UNION ALL
      -- Published mosaics of each NIRCam field component, joined FROM nircam_images.
      SELECT p_tag, so.storage_key, 'nircam/' || ni.field, NULL, ni.field, so.product_type,
             NULL, so.backend, so.bucket, so.content_hash, so.science_hash, so.size_bytes, so.id
      FROM nircam_images ni
      JOIN storage_objects so ON so.storage_key = ni.file_path
      WHERE ni.deploy_status = 'published' AND ni.field = ANY (comp.fields)
        AND so.root = 'live' AND so.status = 'active'
      -- (P(z) sidecars are not members: photometry_pz is a key-only product the
      -- layout cannot archive; the frozen dr_photometry rows carry the catalog
      -- values, the sidecars stay live-only -- plan amendment D-P4-5.)
      ON CONFLICT (tag, storage_key) DO NOTHING
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM ins_spectra), (SELECT count(*) FROM ins_objects),
           (SELECT count(*) FROM ins_lines), (SELECT count(*) FROM ins_phot),
           (SELECT count(*) FROM ins_members),
           pg_snapshot_xmin(pg_current_snapshot())::text::bigint
      INTO v_n_spectra, v_n_objects, v_n_lines, v_n_phot, v_n_members, v_xmin;

    -- Every published mosaic of a selected field must have become a member: a
    -- mosaic without an active registry row would otherwise be dropped with no
    -- trace (there is no frozen NIRCam table to recover it from). Raising here
    -- rolls the inserts back and records the release as failed (Codex on #599).
    SELECT string_agg(ni.file_path, ', ' ORDER BY ni.file_path) INTO v_missing
    FROM nircam_images ni
    WHERE ni.deploy_status = 'published' AND ni.field = ANY (comp.fields)
      AND NOT EXISTS (SELECT 1 FROM dr_members m WHERE m.tag = p_tag AND m.storage_key = ni.file_path);
    IF v_missing IS NOT NULL THEN
      RAISE EXCEPTION 'published mosaics without an active registry row (deploy them again, or leave the field out): %',
        left(v_missing, 1500);
    END IF;

    UPDATE dr_releases
       SET state = 'frozen',
           frozen_at = now(),
           frozen_by = COALESCE(auth.uid(), created_by),
           freeze_duration_ms = (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int,
           snapshot_xmin = v_xmin,
           counts = jsonb_build_object(
             'spectra', v_n_spectra, 'objects', v_n_objects, 'line_fits', v_n_lines,
             'photometry', v_n_phot, 'members', v_n_members,
             'nircam_fields', to_jsonb(comp.fields), 'photometry_components', to_jsonb(comp.photometry)),
           error = NULL,
           updated_at = now()
     WHERE tag = p_tag;

    -- Directly (log_deploy_event needs an auth uid; the cron runner has none).
    INSERT INTO deploy_events (actor, action, affected_count, metadata)
    VALUES (COALESCE(auth.uid(), r.created_by), 'freeze', v_n_spectra::int,
            jsonb_build_object('dr_tag', p_tag, 'components', r.components,
                               'counts', jsonb_build_object('spectra', v_n_spectra, 'objects', v_n_objects,
                                 'line_fits', v_n_lines, 'photometry', v_n_phot, 'members', v_n_members),
                               'duration_ms', (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int));
  EXCEPTION WHEN query_canceled OR OTHERS THEN
    -- query_canceled is listed explicitly: OTHERS does not include it, and a
    -- statement_timeout must leave the row in `failed` (not `freezing`
    -- forever) so the next attempt is not refused as "already freezing".
    GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT;
    -- The block's writes are rolled back; record the failure on the row.
    UPDATE dr_releases
       SET state = 'failed', error = left(v_err, 2000), updated_at = now()
     WHERE tag = p_tag;
  END;

  PERFORM dr_cron_unschedule(p_tag);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_freeze_job"(text) FROM PUBLIC, "anon", "authenticated";

CREATE OR REPLACE FUNCTION public.dr_parse_components (
  p_components jsonb,
  OUT          nirspec boolean,
  OUT          fields text[],
  OUT          photometry text[]
)
  RETURNS record
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public'
  AS $function$
DECLARE
  c text;
  v_field text;
  v_catalog text;
BEGIN
  nirspec := false; fields := '{}'; photometry := '{}';
  IF p_components IS NULL OR jsonb_typeof(p_components) <> 'array' OR jsonb_array_length(p_components) = 0 THEN
    RAISE EXCEPTION 'components must be a non-empty JSON array of "nirspec", "nircam/<field>", "photometry/<field>/<catalog_name>"';
  END IF;
  FOR c IN SELECT jsonb_array_elements_text(p_components) LOOP
    IF c = 'nirspec' THEN
      nirspec := true;
    ELSIF c LIKE 'nircam/%' THEN
      v_field := substr(c, 8);
      IF NOT EXISTS (SELECT 1 FROM nircam_images ni WHERE ni.field = v_field AND ni.deploy_status = 'published') THEN
        RAISE EXCEPTION 'component %: no published NIRCam images for field %', c, v_field;
      END IF;
      fields := array_append(fields, v_field);
    ELSIF c LIKE 'photometry/%/%' THEN
      v_field := split_part(substr(c, 12), '/', 1);
      v_catalog := substr(substr(c, 12), length(v_field) + 2);
      IF NOT EXISTS (SELECT 1 FROM object_photometry op WHERE op.field = v_field AND op.catalog_name = v_catalog) THEN
        RAISE EXCEPTION 'component %: no photometry rows for field % catalog %', c, v_field, v_catalog;
      END IF;
      photometry := array_append(photometry, v_field || '/' || v_catalog);
    ELSE
      RAISE EXCEPTION 'unknown component %', c;
    END IF;
  END LOOP;
  SELECT array_agg(DISTINCT f ORDER BY f) INTO fields FROM unnest(fields) f;
  SELECT array_agg(DISTINCT f ORDER BY f) INTO photometry FROM unnest(photometry) f;
  fields := COALESCE(fields, '{}'); photometry := COALESCE(photometry, '{}');
  -- Photometry rows hang off the frozen objects (their access scope is the
  -- object's programs), so a photometry component needs the catalog.
  IF cardinality(photometry) > 0 AND NOT nirspec THEN
    RAISE EXCEPTION 'photometry components need the nirspec component (the rows are scoped by the frozen objects)';
  END IF;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_parse_components"(jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_purge_release (
  p_tag text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '300s'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  n_members bigint; n_lines bigint; n_phot bigint; n_spectra bigint; n_objects bigint; n_exports bigint;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  IF r.state IN ('published', 'withdrawn') THEN
    RAISE EXCEPTION 'release % is %; a published DR is never purged (withdraw hides it, bytes stay)', p_tag, r.state;
  END IF;
  IF r.state = 'freezing' AND r.freeze_started_at > now() - interval '1 hour' THEN
    RAISE EXCEPTION 'release % is freezing (started %); wait for it to finish or fail', p_tag, r.freeze_started_at;
  END IF;
  PERFORM dr_cron_unschedule(p_tag);
  DELETE FROM dr_members    WHERE tag = p_tag; GET DIAGNOSTICS n_members = ROW_COUNT;
  DELETE FROM dr_line_fits  WHERE tag = p_tag; GET DIAGNOSTICS n_lines = ROW_COUNT;
  DELETE FROM dr_photometry WHERE tag = p_tag; GET DIAGNOSTICS n_phot = ROW_COUNT;
  DELETE FROM dr_spectra    WHERE tag = p_tag; GET DIAGNOSTICS n_spectra = ROW_COUNT;
  DELETE FROM dr_objects    WHERE tag = p_tag; GET DIAGNOSTICS n_objects = ROW_COUNT;
  DELETE FROM dr_exports    WHERE tag = p_tag; GET DIAGNOSTICS n_exports = ROW_COUNT;
  DELETE FROM dr_releases   WHERE tag = p_tag;
  INSERT INTO deploy_events (actor, action, affected_count, metadata)
  VALUES (auth.uid(), 'delete', n_spectra::int,
          jsonb_build_object('dr_tag', p_tag, 'purge', true, 'state_before', r.state,
                             'rows', jsonb_build_object('spectra', n_spectra, 'objects', n_objects,
                               'line_fits', n_lines, 'photometry', n_phot, 'members', n_members,
                               'exports', n_exports)));
  RETURN jsonb_build_object('tag', p_tag, 'state_before', r.state,
                            'deleted', jsonb_build_object('spectra', n_spectra, 'objects', n_objects,
                              'line_fits', n_lines, 'photometry', n_phot, 'members', n_members,
                              'exports', n_exports));
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_purge_release"(text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_readiness()
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_is_admin boolean;
  v_spectra jsonb;
  v_objects jsonb;
  v_spectra_status jsonb;
  v_nircam_drafts bigint;
  v_deployments jsonb;
  v_leases jsonb;
  v_redrafted jsonb;
  v_dangling jsonb;
  v_etag jsonb;
  v_lines jsonb;
  v_has_sh_spectra boolean;
  v_has_sh_storage boolean;
  v_sh_spectra bigint;
  v_sh_storage bigint;
  v_releases jsonb;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  -- 0. data releases (P4a): every tag, newest first.
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'tag', r.tag, 'state', r.state, 'components', r.components,
      'frozen_at', r.frozen_at, 'published_at', r.published_at,
      'withdrawn_at', r.withdrawn_at, 'counts', r.counts, 'error', r.error)
    ORDER BY r.created_at DESC), '[]'::jsonb)
  INTO v_releases
  FROM dr_releases r;

  -- 1. published spectra: one pass, grouped on the three keys we need.
  WITH po AS MATERIALIZED (
    SELECT s.cfpipe_version, s.observation, s.program_slug, count(*) AS n
    FROM spectra s
    WHERE s.deploy_status = 'published'
    GROUP BY 1, 2, 3
  ),
  by_version AS (
    SELECT COALESCE(cfpipe_version, 'unknown') AS v,
           sum(n) AS spectra,
           count(DISTINCT observation) AS observations
    FROM po
    GROUP BY 1
  )
  SELECT jsonb_build_object(
    'spectra_by_version',
      (SELECT COALESCE(jsonb_object_agg(v, jsonb_build_object('spectra', spectra, 'observations', observations)), '{}'::jsonb)
       FROM by_version),
    'published',
      (SELECT jsonb_build_object(
         'spectra', COALESCE(sum(n), 0),
         'observations', count(DISTINCT observation),
         'programs', count(DISTINCT program_slug))
       FROM po))
  INTO v_spectra;

  -- 2. draft / revoked spectra (partial index on deploy_status <> 'published').
  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE deploy_status = 'draft'),
    'revoked', count(*) FILTER (WHERE deploy_status = 'revoked'))
  INTO v_spectra_status
  FROM spectra
  WHERE deploy_status <> 'published';

  -- 3. objects: published / needs_review / implicit pins in one pass over active rows.
  SELECT jsonb_build_object(
    'published', count(*) FILTER (WHERE has_published_spectrum),
    'needs_review', count(*) FILTER (WHERE needs_review),
    'implicit_pins', count(*) FILTER (WHERE inspected_used_auto))
  INTO v_objects
  FROM objects
  WHERE is_active;

  -- 4. NIRCam drafts and deployments by status (small tables).
  SELECT count(*) INTO v_nircam_drafts FROM nircam_images WHERE deploy_status = 'draft';

  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE status = 'draft'),
    'revoked', count(*) FILTER (WHERE status = 'revoked'))
  INTO v_deployments
  FROM deployments;

  -- 5. live deploy leases (with the stale flag the takeover rule uses).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'scope_type', d.scope_type,
      'scope_key', d.scope_key,
      'version', d.version,
      'in_progress_by', d.in_progress_by,
      'in_progress_by_username', up.username,
      'in_progress_host', d.in_progress_host,
      'in_progress_since', d.in_progress_since,
      'heartbeat_at', d.heartbeat_at,
      'stale', COALESCE(d.heartbeat_at, d.in_progress_since) < now() - deploy_lease_ttl())
    ORDER BY d.in_progress_since), '[]'::jsonb)
  INTO v_leases
  FROM deploy_scope_state d
  LEFT JOIN user_profiles up ON up.user_id = d.in_progress_by
  WHERE d.in_progress_since IS NOT NULL;

  -- 6. re-drafted NIRCam fields (see the header comment for the definition).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'field', f.field,
      'draft_images', f.n_draft,
      'published_images', f.n_published,
      'published_deployments', f.n_published_deployments)
    ORDER BY f.field), '[]'::jsonb)
  INTO v_redrafted
  FROM (
    SELECT ni.field,
           count(*) FILTER (WHERE ni.deploy_status = 'draft') AS n_draft,
           count(*) FILTER (WHERE ni.deploy_status = 'published') AS n_published,
           (SELECT count(*) FROM deployments dp
             WHERE dp.field = ni.field AND dp.status = 'published') AS n_published_deployments
    FROM nircam_images ni
    GROUP BY ni.field
  ) f
  WHERE f.n_draft > 0 AND (f.n_published > 0 OR f.n_published_deployments > 0);

  -- 7. dangling registry rows: active nirspec_spec rows whose spectrum has no
  --    spectra row any more (deploy remove deletes spectra, not registry rows).
  SELECT jsonb_build_object(
    'count', COALESCE(sum(n), 0),
    'by_observation', COALESCE(jsonb_object_agg(COALESCE(observation, 'unknown'), n), '{}'::jsonb))
  INTO v_dangling
  FROM (
    SELECT so.observation, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.product_type = 'nirspec_spec'
      AND NOT EXISTS (SELECT 1 FROM spectra s WHERE s.spectrum_id = so.spectrum_id)
    GROUP BY so.observation
  ) x;

  -- 8. provisional etag: hashes by product_type.
  SELECT COALESCE(jsonb_object_agg(product_type, n), '{}'::jsonb)
  INTO v_etag
  FROM (
    SELECT so.product_type, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.content_hash LIKE 'etag:%'
      AND (so.backend = 'r2' OR so.product_type IN ('nirspec_spec', 'nircam_mosaic'))
    GROUP BY so.product_type
  ) x;

  -- 9. line-fit staleness from the ledger view.
  SELECT jsonb_build_object(
    'total', count(*),
    'stale_redshift', count(*) FILTER (WHERE stale_redshift),
    'stale_spectrum', count(*) FILTER (WHERE stale_spectrum))
  INTO v_lines
  FROM spectrum_line_fits_status;

  -- 10. science hash coverage, only once the columns exist (P0b, PR #586).
  SELECT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'science_hash'),
         EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'storage_objects' AND column_name = 'science_hash')
  INTO v_has_sh_spectra, v_has_sh_storage;
  IF v_has_sh_spectra THEN
    EXECUTE 'SELECT count(*) FROM public.spectra WHERE deploy_status = ''published'' AND science_hash IS NULL'
      INTO v_sh_spectra;
  END IF;
  IF v_has_sh_storage THEN
    EXECUTE 'SELECT count(*) FROM public.storage_objects WHERE status = ''active'' AND product_type = ''nirspec_spec'' AND science_hash IS NULL'
      INTO v_sh_storage;
  END IF;

  RETURN jsonb_build_object(
    'generated_at', now(),
    'spectra_by_version', v_spectra->'spectra_by_version',
    'published', (v_spectra->'published') || jsonb_build_object('objects', v_objects->'published'),
    'drafts', jsonb_build_object(
      'spectra', v_spectra_status->'draft',
      'nircam_images', v_nircam_drafts,
      'deployments', v_deployments->'draft'),
    'revoked', jsonb_build_object(
      'spectra', v_spectra_status->'revoked',
      'deployments', v_deployments->'revoked'),
    'leases', v_leases,
    'releases', v_releases,
    'redrafted_nircam_fields', v_redrafted,
    'dangling_registry_rows', v_dangling,
    'etag_hash_rows', v_etag,
    'line_fits', v_lines,
    'needs_review', v_objects->'needs_review',
    'implicit_pins', v_objects->'implicit_pins',
    'science_hash_missing', jsonb_build_object(
      'spectra', to_jsonb(v_sh_spectra),
      'storage_objects', to_jsonb(v_sh_storage)));
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_require_admin()
  RETURNS void
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_is_admin boolean;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_require_admin"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_visible_tags()
  RETURNS text[]
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  SELECT COALESCE(array_agg(r.tag), '{}'::text[])
  FROM dr_releases r
  WHERE r.state = 'published' OR (SELECT public.is_admin());
$function$;

REVOKE ALL ON FUNCTION "public"."dr_visible_tags"() FROM PUBLIC, "anon";

CREATE INDEX idx_dr_line_fits_tag_program ON public.dr_line_fits USING btree (TAG, program_slug);

CREATE INDEX idx_dr_members_science_hash ON public.dr_members USING btree (science_hash)
  WHERE (science_hash IS NOT NULL);

CREATE INDEX idx_dr_members_spectrum ON public.dr_members USING btree (spectrum_id)
  WHERE (spectrum_id IS NOT NULL);

CREATE INDEX idx_dr_members_tag_component ON public.dr_members USING btree (TAG, component);

CREATE INDEX idx_dr_members_tag_pending ON public.dr_members USING btree (TAG, storage_key)
  WHERE (byte_state = 'pending'::text);

CREATE INDEX idx_dr_objects_programs ON public.dr_objects USING gin (programs);

CREATE INDEX idx_dr_objects_tag_field ON public.dr_objects USING btree (TAG, field);

CREATE INDEX idx_dr_photometry_tag_field_catalog ON public.dr_photometry USING btree (TAG, field, catalog_name);

CREATE INDEX idx_dr_photometry_tag_object ON public.dr_photometry USING btree (TAG, object_id);

CREATE INDEX idx_dr_spectra_spectrum ON public.dr_spectra USING btree (spectrum_id);

CREATE INDEX idx_dr_spectra_tag_object ON public.dr_spectra USING btree (TAG, object_id);

CREATE INDEX idx_dr_spectra_tag_program ON public.dr_spectra USING btree (TAG, program_slug);

CREATE POLICY "admin_select_dr_exports" ON "public"."dr_exports"
  FOR SELECT
  TO "authenticated"
  USING (( SELECT public.is_admin() AS is_admin));

CREATE POLICY "select_dr_line_fits" ON "public"."dr_line_fits"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[])) AND (program_slug = ANY (( SELECT
    public.accessible_program_slugs() AS accessible_program_slugs)::text[]))));

CREATE POLICY "select_dr_members" ON "public"."dr_members"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[])) AND ((program_slug IS NULL) OR (program_slug = ANY ((
    SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))));

CREATE POLICY "select_dr_objects" ON "public"."dr_objects"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[])) AND (programs && ( SELECT
    public.accessible_program_slugs() AS accessible_program_slugs))));

CREATE POLICY "select_dr_photometry" ON "public"."dr_photometry"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[])) AND (programs && ( SELECT
    public.accessible_program_slugs() AS accessible_program_slugs))));

CREATE POLICY "select_dr_releases" ON "public"."dr_releases"
  FOR SELECT
  TO "authenticated"
  USING ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[]))));

CREATE POLICY "select_dr_spectra" ON "public"."dr_spectra"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (TAG = ANY (( SELECT public.dr_visible_tags() AS dr_visible_tags)::text[])) AND (program_slug = ANY (( SELECT
    public.accessible_program_slugs() AS accessible_program_slugs)::text[]))));

COMMENT ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) IS 'Start a data-release freeze (P4a; `campfire dr freeze`). Admin or service_role. Validates the tag and components, refuses while a deploy lease is live or the tag is published, writes the dr_releases row in state freezing and schedules a one-shot pg_cron job for dr_freeze_job (D-P4-1); runs it inline where pg_cron is absent or when p_inline. Returns the row summary.';

COMMENT ON FUNCTION "public"."dr_freeze_job"(text) IS 'Data-release freeze (P4a): rewrites the six frozen tables of an UNPUBLISHED tag in one statement (one snapshot) from the published live rows, then marks the row frozen with counts, snapshot xmin and duration; any error lands in state failed with the message. Runs as postgres from the one-shot pg_cron job dr_enqueue_freeze schedules, inline where pg_cron is absent, and from the seed. Idempotent per tag.';

COMMENT ON FUNCTION "public"."dr_purge_release"(text) IS 'Delete an UNPUBLISHED data release and all its frozen rows (P4a; `campfire dr purge`). Admin or service_role. Refuses published / withdrawn tags and a freeze that is still running. Archive bytes already copied for the tag are left in place (dedup by hash; gc is the retention path).';

COMMENT ON FUNCTION "public"."dr_readiness"() IS 'Data-release readiness report (P1): published cfpipe_version mix, drafts/revoked, live deploy leases, re-drafted NIRCam fields, dangling nirspec_spec registry rows, etag: hashes, line-fit staleness, needs-review / implicit-pin counts, science_hash coverage, and the data releases (P4a). Admin or service_role; printed by `campfire dr status`.';

COMMENT ON FUNCTION "public"."dr_visible_tags"() IS 'Data-release tags the caller may read: published ones for everyone (link accounts are excluded by the policies themselves), every state for admins. Uncorrelated in the dr_* policies so it is evaluated once per query.';

COMMENT ON TABLE "public"."dr_exports" IS 'Export ledger of a data release (P4b): one JSONL.gz per (stream, partition) under dr/<tag>/exports/, with sha256, size and row count once built. Admin-only; readers get partitions through /api/v1/dr/<tag>/partitions.';

COMMENT ON TABLE "public"."dr_line_fits" IS 'Frozen emission-line fits of a data release (spectrum_id = the live spectra.id at the freeze) with stale_redshift / stale_spectrum computed inside the freeze snapshot, so "reviewed against this DR" is a real statement.';

COMMENT ON TABLE "public"."dr_members" IS 'Storage objects referenced by a data release (one row per live key at the freeze) and the archive copy the byte pass (P4c) makes for each: archive_key, byte_state pending → copied/stamped, stamped_hash. Program-scoped rows carry program_slug; field-level rows (mosaics) carry field.';

COMMENT ON TABLE "public"."dr_objects" IS 'Frozen objects of a data release: catalog aggregates, inspection state (redshift, quality, inspector, review state) and the auto-redshift source spectrum, all evaluated in the freeze snapshot. object_id is the live objects.id at the time, name the public object_id string.';

COMMENT ON TABLE "public"."dr_photometry" IS 'Frozen photometry cross-match rows of a data release (photometry/<field>/<catalog> components), scoped by the object''s programs at the freeze.';

COMMENT ON TABLE "public"."dr_releases" IS 'Data releases (P4a): one row per tag with the freeze/export/byte-pass state machine, provenance (who, when, snapshot xmin, counts) and the publication fields. Readable by authenticated non-link accounts once published, by admins always; written only through the dr_* RPCs.';

COMMENT ON TABLE "public"."dr_spectra" IS 'Frozen spectra rows of a data release (published members at the freeze). id is the live spectra.id at the time; object_id the live objects.id. Row-local program_slug for RLS.';

REVOKE ALL ON FUNCTION "public"."dr_cron_unschedule"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_cron_unschedule"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_cron_unschedule"(text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_enqueue_freeze"(text, jsonb, text, boolean) TO "service_role";

REVOKE ALL ON FUNCTION "public"."dr_freeze_job"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_freeze_job"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_freeze_job"(text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_parse_components"(jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_parse_components"(jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_parse_components"(jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_parse_components"(jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_release"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_purge_release"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_release"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_release"(text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_require_admin"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_require_admin"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_require_admin"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_require_admin"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_visible_tags"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_visible_tags"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_visible_tags"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_visible_tags"() TO "service_role";

REVOKE ALL ON TABLE "public"."dr_exports" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_exports" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_exports" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_exports" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_exports" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_line_fits" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_line_fits" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_line_fits" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_members" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_members" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_members" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_members" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_members" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_objects" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_objects" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_objects" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_objects" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_objects" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_photometry" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_photometry" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_photometry" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_photometry" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_photometry" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_releases" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_releases" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_releases" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_releases" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_releases" TO "service_role";

REVOKE ALL ON TABLE "public"."dr_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."dr_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."dr_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_spectra" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."dr_spectra" TO "service_role";
