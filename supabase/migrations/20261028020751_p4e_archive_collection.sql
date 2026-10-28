SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

ALTER TABLE "public"."dr_releases"
  DROP CONSTRAINT "dr_releases_state_check";

ALTER TABLE "public"."storage_objects"
  DROP CONSTRAINT "storage_objects_root_status_check";

ALTER TABLE "public"."storage_objects"
  DROP CONSTRAINT "storage_objects_status_check";

DROP FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text);

CREATE TABLE "public"."archive_collections" (
  "storage_key"  text                     NOT NULL,
  "collected_at" timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "archive_collections_pkey" PRIMARY KEY (storage_key)
);

ALTER TABLE "public"."archive_collections"
  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE "public"."archive_collections" FROM "anon", "authenticated";

ALTER TABLE "public"."storage_objects"
  ADD COLUMN "collect_claim" uuid;

CREATE OR REPLACE FUNCTION public.archive_collectable (
  p_after_id bigint  DEFAULT 0,
  p_limit    integer DEFAULT 1000
)
  RETURNS TABLE (
    id                bigint,
    backend           text,
    bucket            text,
    storage_key       text,
    content_hash      text,
    size_bytes        bigint,
    stored_size_bytes bigint,
    product_type      text,
    field             text,
    filter            text,
    observation       text,
    archived_from_key text,
    archived_at       timestamp with time zone,
    status            text
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  PERFORM dr_require_admin();
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 2000 THEN
    RAISE EXCEPTION 'archive_collectable: p_limit must be 1..2000 (got %)', p_limit;
  END IF;
  RETURN QUERY
    SELECT so.id, so.backend, so.bucket, so.storage_key, so.content_hash,
           so.size_bytes, so.stored_size_bytes, so.product_type, so.field, so.filter,
           so.observation, so.archived_from_key, so.archived_at, so.status
    FROM storage_objects so
    WHERE so.root = 'archive'
      AND so.id > COALESCE(p_after_id, 0)
      AND (so.status = 'collecting'    -- a collection claimed it and did not finish
           OR (so.status = 'archived'
               AND archive_object_references(so.storage_key, so.archived_from_key, so.content_hash) = '{}'::text[]))
    ORDER BY so.id
    LIMIT p_limit;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."archive_collectable"(bigint, integer) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.archive_object_references (
  p_storage_key       text,
  p_archived_from_key text,
  p_content_hash      text
)
  RETURNS text[]
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
  SELECT COALESCE(array_agg(DISTINCT m.tag ORDER BY m.tag), '{}'::text[])
  FROM dr_manifest m
  WHERE m.archive_key = p_storage_key
     OR (p_archived_from_key IS NOT NULL AND m.storage_key = p_archived_from_key
         AND m.content_hash = p_content_hash);
$function$;

REVOKE ALL ON FUNCTION "public"."archive_object_references"(text, text, text) FROM PUBLIC, "anon", "authenticated";

CREATE OR REPLACE FUNCTION public.archive_register_retained (
  p_row jsonb
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_key text := p_row->>'storage_key';
  v_status text;
  n int;
BEGIN
  PERFORM dr_require_admin();
  IF v_key IS NULL OR v_key NOT LIKE 'archive/%' THEN
    RAISE EXCEPTION 'archive_register_retained: storage_key must be an archive key (got %)', v_key;
  END IF;
  IF (p_row->>'content_hash') IS NULL OR (p_row->>'archived_from_key') IS NULL OR (p_row->>'product_type') IS NULL
     OR (p_row->>'size_bytes') IS NULL THEN
    RAISE EXCEPTION 'archive_register_retained: content_hash, archived_from_key, product_type and size_bytes are required';
  END IF;
  -- the existing row, if any, is locked first: a collection finishing under
  -- us (its row delete) is waited for, and then seen as absent, never as a
  -- row that was there a moment ago
  SELECT so.status INTO v_status FROM storage_objects so
   WHERE so.backend = COALESCE(p_row->>'backend', 'osn') AND so.bucket = COALESCE(p_row->>'bucket', 'data')
     AND so.storage_key = v_key
   FOR UPDATE;
  IF FOUND THEN
    IF v_status IS DISTINCT FROM 'archived' THEN
      RAISE EXCEPTION 'archive object % is being collected (status %); retry once the collection has finished', v_key, v_status;
    END IF;
    RETURN jsonb_build_object('inserted', false);
  END IF;
  INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, size_bytes, stored_size_bytes,
                               content_type, product_type, instrument, observation, spectrum_id, program_slug,
                               deployment_id, cfpipe_version, archived_from_key, archived_at, archived_by_deployment,
                               uploaded_by, updated_at)
  VALUES (COALESCE(p_row->>'backend', 'osn'), COALESCE(p_row->>'bucket', 'data'), v_key, 'archive', 'archived',
          p_row->>'content_hash', (p_row->>'size_bytes')::bigint, (p_row->>'stored_size_bytes')::bigint,
          COALESCE(p_row->>'content_type', 'application/octet-stream'), p_row->>'product_type',
          p_row->>'instrument',
          (SELECT o.name FROM observations o WHERE o.name = p_row->>'observation'),
          p_row->>'spectrum_id', p_row->>'program_slug',
          (SELECT d.id FROM deployments d WHERE d.id = (p_row->>'deployment_id')::integer),
          p_row->>'cfpipe_version', p_row->>'archived_from_key', now(), NULL, auth.uid(), now())
  ON CONFLICT (backend, bucket, storage_key) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN
    -- inserted under us between the lock probe and the insert (another gate
    -- for the same bytes): the same content, fine
    RETURN jsonb_build_object('inserted', false);
  END IF;
  RETURN jsonb_build_object('inserted', true);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."archive_register_retained"(jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_archive_claim (
  p_tag   text,
  p_claim uuid DEFAULT NULL::uuid
)
  RETURNS uuid
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF p_claim IS NOT NULL THEN
    -- a heartbeat: refresh our own claim, which nobody has taken over
    IF r.archive_claim IS DISTINCT FROM p_claim THEN
      RAISE EXCEPTION 'the archive claim on % is not held by this run (another `campfire dr archive` took it over)', p_tag;
    END IF;
  ELSIF r.state NOT IN ('frozen', 'exporting', 'exported', 'copying', 'ready') THEN
    -- the byte pass needs a frozen, unpublished release that no purge has claimed (P4e)
    RAISE EXCEPTION 'release % is % (the byte pass needs a frozen, unpublished release)', p_tag, r.state;
  ELSIF r.archive_claim IS NOT NULL AND r.archive_claimed_at > now() - interval '10 minutes' THEN
    RAISE EXCEPTION 'release % is being archived by another run (claimed %, refreshed %); wait for it to finish',
      p_tag, r.archive_claim, r.archive_claimed_at;
  END IF;
  UPDATE dr_releases
     SET archive_claim = COALESCE(p_claim, gen_random_uuid()), archive_claimed_at = now()
   WHERE tag = p_tag
  RETURNING archive_claim INTO p_claim;
  RETURN p_claim;
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_archive_collect_begin (
  p_keys  text[],
  p_tag   text   DEFAULT NULL::text,
  p_claim uuid   DEFAULT NULL::uuid
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  k text;
  so storage_objects%ROWTYPE;
  r dr_releases%ROWTYPE;
  refs text[];
  v_claim uuid := COALESCE(p_claim, gen_random_uuid());
  n_claimed int := 0;
  n_already int := 0;
  v_bytes bigint := 0;
BEGIN
  PERFORM dr_require_admin();
  IF p_keys IS NULL OR cardinality(p_keys) = 0 THEN
    RETURN jsonb_build_object('claim', v_claim, 'claimed', 0, 'already_collecting', 0, 'bytes', 0);
  END IF;
  IF cardinality(p_keys) > 2000 THEN
    RAISE EXCEPTION 'dr_archive_collect_begin: at most 2000 keys per call (got %)', cardinality(p_keys);
  END IF;
  -- No freeze can add a manifest row (a reference) while this runs: the same
  -- advisory lock dr_freeze_job holds for the whole freeze (begin_deploy_scope
  -- tries it and refuses while it is held, so a deploy waits for us too).
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_freeze'));
  IF p_tag IS NOT NULL THEN
    -- ...and the tag cannot be published under us: dr_publish locks this row,
    -- and the `purging` state it is left in refuses publish, archive, export
    -- and re-freeze until dr_purge_release removes the tag.
    SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'no release %', p_tag;
    END IF;
    IF r.state IN ('published', 'withdrawn') THEN
      RAISE EXCEPTION 'release % is %; its archive objects are never collected', p_tag, r.state;
    END IF;
    IF r.state = 'freezing' AND r.freeze_started_at > now() - interval '1 hour' THEN
      RAISE EXCEPTION 'release % is freezing (started %); wait for it to finish or fail', p_tag, r.freeze_started_at;
    END IF;
    IF r.state <> 'purging' THEN
      UPDATE dr_releases SET state = 'purging', updated_at = now() WHERE tag = p_tag;
    END IF;
  END IF;
  FOREACH k IN ARRAY p_keys LOOP
    SELECT * INTO so FROM storage_objects WHERE storage_key = k AND root = 'archive' FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'dr_archive_collect_begin: % is not an archive registry row', k;
    END IF;
    IF so.status = 'collected' THEN
      n_already := n_already + 1;
      CONTINUE;
    END IF;
    IF so.status = 'collecting' THEN
      -- one collector per object: our own claim is refreshed; another run's
      -- claim refreshed in the last 10 minutes belongs to a live run; an older
      -- one is a dead run's and is taken over (the same rule as the archive
      -- claim). The token is what fences the dead run if it ever resumes:
      -- dr_archive_collect_verify / _finish act only on rows it still owns.
      IF so.collect_claim IS DISTINCT FROM v_claim AND so.updated_at > now() - interval '10 minutes' THEN
        RAISE EXCEPTION 'dr_archive_collect_begin: % is being collected by another run (claimed %); wait for it to finish, or re-run after 10 minutes',
          k, so.updated_at;
      END IF;
      UPDATE storage_objects SET collect_claim = v_claim, updated_at = now() WHERE id = so.id;
      n_already := n_already + 1;
      CONTINUE;
    END IF;
    refs := archive_object_references(so.storage_key, so.archived_from_key, so.content_hash);
    IF p_tag IS NOT NULL THEN
      refs := array_remove(refs, p_tag);
    END IF;
    IF cardinality(refs) > 0 THEN
      RAISE EXCEPTION 'dr_archive_collect_begin: % is still referenced by release(s) %', k, array_to_string(refs, ', ');
    END IF;
    UPDATE storage_objects SET status = 'collecting', collect_claim = v_claim, updated_at = now() WHERE id = so.id;
    n_claimed := n_claimed + 1;
    v_bytes := v_bytes + COALESCE(so.stored_size_bytes, so.size_bytes, 0);
  END LOOP;
  RETURN jsonb_build_object('claim', v_claim, 'claimed', n_claimed, 'already_collecting', n_already, 'bytes', v_bytes);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_archive_collect_finish (
  p_keys  text[],
  p_tag   text   DEFAULT NULL::text,
  p_claim uuid   DEFAULT NULL::uuid
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  n_deleted int := 0;
  n_skipped int := 0;
  v_bytes bigint := 0;
BEGIN
  PERFORM dr_require_admin();
  IF p_keys IS NULL OR cardinality(p_keys) = 0 THEN
    RETURN jsonb_build_object('collected', 0, 'not_collecting', 0, 'bytes', 0);
  END IF;
  IF cardinality(p_keys) > 2000 THEN
    RAISE EXCEPTION 'dr_archive_collect_finish: at most 2000 keys per call (got %)', cardinality(p_keys);
  END IF;
  IF p_claim IS NULL THEN
    RAISE EXCEPTION 'dr_archive_collect_finish: the claim dr_archive_collect_begin returned is required';
  END IF;
  PERFORM set_config('campfire.archive_collect', 'on', true);
  WITH gone AS (
    DELETE FROM storage_objects so
    WHERE so.root = 'archive' AND so.status = 'collecting' AND so.collect_claim = p_claim
      AND so.storage_key = ANY (p_keys)
    RETURNING so.storage_key, COALESCE(so.stored_size_bytes, so.size_bytes, 0) AS b
  ), stoned AS (
    -- the tombstone: a byte pass that found the object while it was being
    -- deleted must not register it after this (dr_bytes_pending / dr_bytes_mark)
    INSERT INTO archive_collections (storage_key, collected_at)
    SELECT storage_key, now() FROM gone
    ON CONFLICT (storage_key) DO UPDATE SET collected_at = EXCLUDED.collected_at
    RETURNING 1
  )
  SELECT count(*), COALESCE(sum(b), 0) INTO n_deleted, v_bytes FROM gone;
  PERFORM set_config('campfire.archive_collect', 'off', true);
  DELETE FROM archive_collections WHERE collected_at < now() - interval '7 days';
  n_skipped := cardinality(p_keys) - n_deleted;
  IF n_deleted > 0 THEN
    INSERT INTO deploy_events (actor, action, affected_count, metadata)
    VALUES (auth.uid(), 'collect', n_deleted,
            jsonb_build_object('dr_tag', p_tag, 'bytes', v_bytes, 'not_collecting', n_skipped));
  END IF;
  RETURN jsonb_build_object('collected', n_deleted, 'not_collecting', n_skipped, 'bytes', v_bytes);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_archive_collect_verify (
  p_keys  text[],
  p_claim uuid
)
  RETURNS text[]
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  held text[];
BEGIN
  PERFORM dr_require_admin();
  IF p_keys IS NULL OR cardinality(p_keys) = 0 OR p_claim IS NULL THEN
    RETURN '{}'::text[];
  END IF;
  IF cardinality(p_keys) > 2000 THEN
    RAISE EXCEPTION 'dr_archive_collect_verify: at most 2000 keys per call (got %)', cardinality(p_keys);
  END IF;
  WITH mine AS (
    UPDATE storage_objects so SET updated_at = now()
    WHERE so.root = 'archive' AND so.status = 'collecting' AND so.collect_claim = p_claim
      AND so.storage_key = ANY (p_keys)
    RETURNING so.storage_key
  )
  SELECT COALESCE(array_agg(storage_key ORDER BY storage_key), '{}'::text[]) INTO held FROM mine;
  RETURN held;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_bytes_mark (
  p_tag     text,
  p_results jsonb,
  p_claim   uuid
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_state text;
  v_claim uuid;
  e jsonb;
  m dr_manifest%ROWTYPE;
  v_new text; v_key text; v_hash text; v_existing text;
  n_marked int := 0; n_registered int := 0; n_failed int := 0;
  n_copied int := 0; n_stamped int := 0; n_pending int := 0; n_stale int := 0;
  v_partitions text[] := '{}';
  v_rows int;
BEGIN
  PERFORM dr_require_admin();
  IF jsonb_typeof(p_results) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'results must be a json array';
  END IF;
  IF jsonb_array_length(p_results) > 2000 THEN
    RAISE EXCEPTION 'at most 2000 results per call (got %)', jsonb_array_length(p_results);
  END IF;
  SELECT state, archive_claim INTO v_state, v_claim FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF v_state IS NULL THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF v_state NOT IN ('frozen', 'exporting', 'exported', 'copying', 'ready') THEN
    RAISE EXCEPTION 'release % is % (the byte pass needs a frozen, unpublished release)', p_tag, v_state;
  END IF;
  -- one archive run per release: only the run holding the claim records
  -- outcomes, and each batch refreshes it
  IF p_claim IS NULL OR v_claim IS DISTINCT FROM p_claim THEN
    RAISE EXCEPTION 'the archive claim on % is not held by this run (another `campfire dr archive` took it over)', p_tag;
  END IF;
  UPDATE dr_releases SET archive_claimed_at = now() WHERE tag = p_tag;

  FOR e IN SELECT * FROM jsonb_array_elements(p_results) LOOP
    v_new := e->>'state';
    v_key := e->>'archive_key';
    v_hash := e->>'stamped_hash';
    IF v_new IS NULL OR v_new NOT IN ('pending', 'copied', 'stamped', 'failed') THEN
      RAISE EXCEPTION 'invalid byte state % for %', v_new, e->>'storage_key';
    END IF;
    IF v_new IN ('copied', 'stamped') AND (v_key IS NULL OR v_key NOT LIKE 'archive/%') THEN
      RAISE EXCEPTION 'a % member needs an archive key under archive/ (%)', v_new, e->>'storage_key';
    END IF;
    IF v_new = 'stamped' AND (v_hash IS NULL OR v_hash !~ '^sha256:[0-9a-f]{64}$') THEN
      RAISE EXCEPTION 'a stamped member needs the sha256 of the stamped bytes (%)', e->>'storage_key';
    END IF;
    SELECT * INTO m FROM dr_manifest WHERE tag = p_tag AND storage_key = e->>'storage_key' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'no member % in %', e->>'storage_key', p_tag; END IF;
    -- The outcome must be for THIS member's frozen bytes: a pass that fetched
    -- its page before the tag was purged and re-frozen carries the old
    -- generation's content hash (Codex on #601). Same hash = same bytes, so an
    -- old outcome is only applied when it is still true.
    IF e->>'content_hash' IS DISTINCT FROM m.content_hash THEN
      RAISE EXCEPTION 'outcome for % is for content % but the member froze %; refetch and rerun',
        m.storage_key, COALESCE(e->>'content_hash', '<none>'), m.content_hash;
    END IF;
    IF v_new = 'stamped' AND m.product_type NOT IN ('nirspec_spec', 'nirspec_lines') THEN
      RAISE EXCEPTION 'only NIRSpec FITS finals are stamped (% is %)', m.storage_key, m.product_type;
    END IF;
    -- A science hash the pass computed for a member frozen without one is kept
    -- (member + archive row), so later releases can reuse the stamped object
    -- (Codex on #601). A frozen hash is never overwritten.
    IF e->>'science_hash' IS NOT NULL THEN
      IF e->>'science_hash' !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'malformed science hash for %', m.storage_key;
      END IF;
      IF m.science_hash IS NOT NULL AND m.science_hash <> e->>'science_hash' THEN
        RAISE EXCEPTION 'science hash of % disagrees with the frozen one', m.storage_key;
      END IF;
      m.science_hash := COALESCE(m.science_hash, e->>'science_hash');
    END IF;
    -- A member already archived stays archived: a slower concurrent run's
    -- failed / pending outcome for the same bytes is stale and ignored
    -- (Codex on #601); it is counted, not applied.
    IF m.byte_state IN ('copied', 'stamped') AND v_new IN ('failed', 'pending') THEN
      n_stale := n_stale + 1;
      CONTINUE;
    END IF;

    -- The archive row the outcome builds on must not be one a collection has
    -- claimed (its bytes are going), and the object the outcome names (found
    -- or written) must not have been deleted by a collection since the pass
    -- fetched its page (page_at, the server clock when dr_bytes_pending handed
    -- the row out; the tombstone dr_archive_collect_finish writes): either way
    -- the member fails with a reason and the next run, on a fresh page, copies
    -- again (P4e). The existing row is locked first so a collection finishing
    -- under us is waited for, then seen as absent plus its tombstone.
    IF v_new IN ('copied', 'stamped') THEN
      SELECT so.status INTO v_existing FROM storage_objects so
       WHERE so.backend = m.backend AND so.bucket = m.bucket AND so.storage_key = v_key AND so.root = 'archive'
       FOR UPDATE;
      IF FOUND AND v_existing <> 'archived' THEN
        v_new := 'failed';
        e := e || jsonb_build_object('error', format('archive object %s is being collected; copy it again', v_key));
      ELSIF NOT FOUND AND (e->>'page_at') IS NOT NULL
            AND EXISTS (SELECT 1 FROM archive_collections c
                         WHERE c.storage_key = v_key AND c.collected_at > (e->>'page_at')::timestamptz) THEN
        v_new := 'failed';
        e := e || jsonb_build_object('error', format('archive object %s was collected at %s, after this pass fetched its page; copy it again',
                                                     v_key, (SELECT c.collected_at FROM archive_collections c WHERE c.storage_key = v_key)));
      END IF;
    END IF;

    -- A newly created archive object gets its registry row (an existing row:
    -- the same object reused, or a crash after the insert, is kept as it is;
    -- archive rows are immutable).
    IF v_new IN ('copied', 'stamped') AND (e->'registry') IS NOT NULL AND jsonb_typeof(e->'registry') = 'object' THEN
      INSERT INTO storage_objects (backend, bucket, storage_key, root, status, content_hash, science_hash,
                                   size_bytes, stored_size_bytes, content_type, product_type, instrument,
                                   observation, field, filter, spectrum_id, program_slug, deployment_id,
                                   cfpipe_version, archived_from_key, archived_at, uploaded_by, updated_at)
      VALUES (m.backend, m.bucket, v_key, 'archive', 'archived',
              CASE WHEN v_new = 'stamped' THEN v_hash ELSE m.content_hash END,
              m.science_hash,
              COALESCE((e->'registry'->>'size_bytes')::bigint, m.size_bytes),
              (e->'registry'->>'stored_size_bytes')::bigint,
              COALESCE(e->'registry'->>'content_type', 'application/octet-stream'),
              m.product_type, e->'registry'->>'instrument',
              -- provenance only: an observation or deployment removed since
              -- the freeze must not abort the batch on its foreign key
              (SELECT o.name FROM observations o WHERE o.name = e->'registry'->>'observation'),
              m.field, e->'registry'->>'filter', m.spectrum_id, m.program_slug,
              (SELECT d.id FROM deployments d WHERE d.id = (e->'registry'->>'deployment_id')::integer),
              e->'registry'->>'cfpipe_version',
              m.storage_key, now(), auth.uid(), now())
      ON CONFLICT (backend, bucket, storage_key) DO NOTHING;
      GET DIAGNOSTICS v_rows = ROW_COUNT;
      n_registered := n_registered + v_rows;
    END IF;
    IF v_new IN ('copied', 'stamped') AND NOT EXISTS (
         SELECT 1 FROM storage_objects so
         WHERE so.backend = m.backend AND so.bucket = m.bucket AND so.storage_key = v_key
           AND so.root = 'archive' AND so.status = 'archived') THEN
      RAISE EXCEPTION 'archive object % is not registered as archived (pass its registry row; a collecting row is being deleted, retry later)', v_key;
    END IF;

    -- ...and on the live registry row a later freeze copies it from, while
    -- that row still describes these exact bytes and has none (Codex on #601)
    IF e->>'science_hash' IS NOT NULL THEN
      UPDATE storage_objects so
         SET science_hash = e->>'science_hash', updated_at = now()
       WHERE so.backend = m.backend AND so.bucket = m.bucket AND so.storage_key = m.storage_key
         AND so.root = 'live' AND so.content_hash = m.content_hash AND so.science_hash IS NULL;
    END IF;
    UPDATE dr_manifest
       SET byte_state = v_new,
           science_hash = m.science_hash,
           archive_key = CASE WHEN v_new IN ('copied', 'stamped') THEN v_key END,
           stamped_hash = CASE WHEN v_new = 'stamped' THEN v_hash END,
           byte_error = CASE WHEN v_new = 'failed' THEN left(COALESCE(e->>'error', 'failed'), 2000) END,
           updated_at = now()
     WHERE tag = p_tag AND storage_key = m.storage_key;
    n_marked := n_marked + 1;
    CASE v_new WHEN 'copied' THEN n_copied := n_copied + 1;
               WHEN 'stamped' THEN n_stamped := n_stamped + 1;
               WHEN 'failed' THEN n_failed := n_failed + 1;
               ELSE n_pending := n_pending + 1; END CASE;
    v_partitions := array_append(v_partitions, COALESCE(m.program_slug, m.component));
  END LOOP;

  -- Built member files no longer match the rows: back to pending.
  UPDATE dr_exports
     SET state = 'pending', sha256 = NULL, size_bytes = NULL, rows = NULL, built_at = NULL, error = NULL,
         claim_token = NULL, claimed_at = NULL
   -- `building` too: a builder mid-way through this file read the old rows,
   -- and dr_export_mark refuses its ready mark once the row is not building
   -- (lock order: the release row is already held above, as in dr_export_mark)
   WHERE tag = p_tag AND stream = 'manifest' AND state IN ('ready', 'building') AND partition = ANY (v_partitions);

  IF n_copied + n_stamped > 0 THEN
    INSERT INTO deploy_events (actor, action, affected_count, metadata)
    VALUES (auth.uid(), 'archive', n_copied + n_stamped,
            jsonb_build_object('dr_tag', p_tag, 'copied', n_copied, 'stamped', n_stamped,
                               'registered', n_registered, 'failed', n_failed));
  END IF;

  RETURN jsonb_build_object('tag', p_tag, 'marked', n_marked, 'copied', n_copied, 'stamped', n_stamped,
                            'failed', n_failed, 'pending', n_pending, 'registered', n_registered,
                            'stale', n_stale, 'state', dr_derive_state(p_tag));
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_bytes_pending (
  p_tag            text,
  p_after          text    DEFAULT NULL::text,
  p_limit          integer DEFAULT 500,
  p_include_failed boolean DEFAULT false,
  p_prefix         text    DEFAULT NULL::text
)
  RETURNS TABLE (
    storage_key         text,
    product_type        text,
    component           text,
    program_slug        text,
    field               text,
    spectrum_id         text,
    backend             text,
    bucket              text,
    content_hash        text,
    science_hash        text,
    size_bytes          bigint,
    byte_state          text,
    source_key          text,
    source_root         text,
    source_stored_size  bigint,
    reuse_archive_key   text,
    reuse_stamped_hash  text,
    reuse_size_bytes    bigint,
    reuse_candidates    jsonb,
    registry            jsonb,
    target_collected_at timestamp with time zone,
    page_at             timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  PERFORM dr_require_admin();
  IF NOT EXISTS (SELECT 1 FROM dr_releases r WHERE r.tag = p_tag) THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  RETURN QUERY
  WITH page AS (
    SELECT m.* FROM dr_manifest m
    WHERE m.tag = p_tag
      AND m.byte_state IN ('pending', 'failed')
      AND (m.byte_state = 'pending' OR p_include_failed)
      AND (p_after IS NULL OR m.storage_key > p_after)
      -- one observation / field first (the S gate): a key prefix
      AND (p_prefix IS NULL OR (m.storage_key >= p_prefix AND left(m.storage_key, length(p_prefix)) = p_prefix))
    ORDER BY m.storage_key
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 500), 1), 2000)
  )
  SELECT pg.storage_key, pg.product_type, pg.component, pg.program_slug, pg.field,
         pg.spectrum_id, pg.backend, pg.bucket, pg.content_hash, pg.science_hash,
         pg.size_bytes, pg.byte_state,
         CASE WHEN live.id IS NOT NULL THEN live.storage_key ELSE ret.storage_key END,
         CASE WHEN live.id IS NOT NULL THEN 'live' WHEN ret.id IS NOT NULL THEN 'archive' END,
         -- the STORED size; a transport-compressed object (.gz key) without a
         -- recorded stored size is unknown (NULL), never its logical size
         -- (Codex on #601)
         CASE WHEN live.id IS NOT NULL THEN
                CASE WHEN pg.storage_key LIKE '%.gz' THEN live.stored_size_bytes
                     ELSE COALESCE(live.stored_size_bytes, live.size_bytes) END
              ELSE
                CASE WHEN pg.storage_key LIKE '%.gz' THEN ret.stored_size_bytes
                     ELSE COALESCE(ret.stored_size_bytes, ret.size_bytes) END END,
         reuse.cands->0->>'archive_key', reuse.cands->0->>'stamped_hash',
         (reuse.cands->0->>'size_bytes')::bigint, COALESCE(reuse.cands, '[]'::jsonb),
         jsonb_build_object(
           'content_type', COALESCE(live.content_type, ret.content_type, src.content_type),
           'stored_size_bytes', COALESCE(live.stored_size_bytes, ret.stored_size_bytes),
           'instrument', COALESCE(live.instrument, ret.instrument, src.instrument),
           'observation', COALESCE(live.observation, ret.observation, src.observation),
           'filter', COALESCE(live.filter, ret.filter, src.filter),
           'deployment_id', COALESCE(live.deployment_id, ret.deployment_id, src.deployment_id),
           'cfpipe_version', COALESCE(live.cfpipe_version, ret.cfpipe_version, src.cfpipe_version),
           'retained_key', ret.storage_key),
         -- the tombstone of the member's unstamped copy target, if a collection
         -- deleted that key in the last 7 days: the pass copies again instead of
         -- healing on an object that predates it (stamped targets carry the tag
         -- in their bytes, so only this tag's own purge could have deleted one)
         (SELECT c.collected_at FROM archive_collections c
           WHERE c.storage_key = 'archive/' || substr(pg.content_hash, 8) || '/' || substr(pg.storage_key, 6)),
         -- when this page was handed out (server clock): an outcome built on
         -- it is refused by dr_bytes_mark if a collection deleted its key after
         now()
  FROM page pg
  -- the registry row the freeze captured (its metadata survives a redeploy)
  LEFT JOIN storage_objects src ON src.id = pg.registry_id
  -- the live object, while it still holds the frozen bytes
  LEFT JOIN LATERAL (
    SELECT so.* FROM storage_objects so
    WHERE so.backend = pg.backend AND so.bucket = pg.bucket AND so.storage_key = pg.storage_key
      AND so.root = 'live' AND so.status = 'active' AND so.content_hash = pg.content_hash
    LIMIT 1
  ) live ON true
  -- an unstamped archive copy of exactly the frozen bytes (retain-on-supersede,
  -- or an earlier release's server-side copy)
  LEFT JOIN LATERAL (
    SELECT so.* FROM storage_objects so
    WHERE so.root = 'archive' AND so.status = 'archived'
      AND so.content_hash = pg.content_hash AND so.archived_from_key = pg.storage_key
    ORDER BY so.id
    LIMIT 1
  ) ret ON true
  -- stamped objects another release made from the same science (D-P4-2,
  -- D-P4-6): the first PUBLISHED release to archive a file owns its stamp,
  -- and every later pass reuses that object. Only a published (or withdrawn)
  -- release's stamp is offered, so an unpublished tag (a rehearsal, a failed
  -- attempt) can never reach a published file's CMPFRDR, and a purge of that
  -- tag collects its stamped copies without anything pointing at them. Every
  -- candidate, earliest archived first: the pass reuses the first whose
  -- object still exists, so a missing one falls through to the next (or to a
  -- fresh stamp).
  LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('archive_key', d.archive_key, 'stamped_hash', d.stamped_hash,
                                        'size_bytes', COALESCE(so.stored_size_bytes, so.size_bytes))
                     ORDER BY so.archived_at, d.tag) AS cands
    FROM dr_manifest d
    JOIN dr_releases dr ON dr.tag = d.tag AND dr.state IN ('published', 'withdrawn')
    JOIN storage_objects so ON so.root = 'archive' AND so.status = 'archived'
                           AND so.storage_key = d.archive_key
    WHERE pg.product_type IN ('nirspec_spec', 'nirspec_lines') AND pg.science_hash IS NOT NULL
      AND d.science_hash = pg.science_hash AND d.storage_key = pg.storage_key
      AND d.tag <> pg.tag AND d.byte_state = 'stamped'
  ) reuse ON true
  ORDER BY pg.storage_key;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) FROM PUBLIC, "anon";

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
  -- One freeze at a time: two jobs would contend for the freeze lock and the
  -- loser would fail and retry on every pg_cron tick until the first is done.
  -- Enqueues are serialized (a lock of their own, held to commit), so two
  -- concurrent requests cannot both pass this check (Codex on #599).
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_enqueue_freeze'));
  IF EXISTS (SELECT 1 FROM dr_releases o WHERE o.tag <> p_tag AND o.state = 'freezing'
              AND o.freeze_started_at > now() - interval '1 hour') THEN
    RAISE EXCEPTION 'another release is freezing (%); wait for it (campfire dr status)',
      (SELECT string_agg(o.tag, ', ') FROM dr_releases o WHERE o.tag <> p_tag AND o.state = 'freezing'
        AND o.freeze_started_at > now() - interval '1 hour');
  END IF;

  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF FOUND THEN
    IF r.state IN ('published', 'withdrawn') THEN
      RAISE EXCEPTION 'release % is %; a DR is immutable once published (make a new tag)', p_tag, r.state;
    ELSIF r.state = 'freezing' AND r.freeze_started_at > now() - interval '1 hour' THEN
      RAISE EXCEPTION 'release % is already freezing (started %)', p_tag, r.freeze_started_at;
    ELSIF r.state = 'purging' THEN
      RAISE EXCEPTION 'release % is being purged; finish it first (campfire dr purge %)', p_tag, p_tag;
    ELSIF r.state IN ('exporting', 'exported', 'copying', 'ready') THEN
      RAISE EXCEPTION 'release % already has exports or archive bytes (state %); purge it first (campfire dr purge %)', p_tag, r.state, p_tag;
    END IF;
    UPDATE dr_releases
       SET state = 'freezing', components = p_components, notes = COALESCE(p_notes, notes),
           created_by = COALESCE(created_by, auth.uid()), freeze_requested_by = auth.uid(),
           freeze_started_at = now(), frozen_at = NULL, frozen_by = NULL,
           freeze_duration_ms = NULL, snapshot_xmin = NULL, counts = '{}'::jsonb,
           error = NULL, updated_at = now()
     WHERE tag = p_tag;
  ELSE
    INSERT INTO dr_releases (tag, state, components, notes, created_by, freeze_requested_by, freeze_started_at)
    VALUES (p_tag, 'freezing', p_components, p_notes, auth.uid(), auth.uid(), now());
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

CREATE OR REPLACE FUNCTION public.dr_purge_archive_plan (
  p_tag      text,
  p_after_id bigint  DEFAULT 0,
  p_limit    integer DEFAULT 1000
)
  RETURNS TABLE (
    id                bigint,
    backend           text,
    bucket            text,
    storage_key       text,
    content_hash      text,
    size_bytes        bigint,
    stored_size_bytes bigint,
    product_type      text,
    status            text
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
BEGIN
  PERFORM dr_require_admin();
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 2000 THEN
    RAISE EXCEPTION 'dr_purge_archive_plan: p_limit must be 1..2000 (got %)', p_limit;
  END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  IF r.state IN ('published', 'withdrawn') THEN
    RAISE EXCEPTION 'release % is %; a published DR''s archive objects are never collected', p_tag, r.state;
  END IF;
  -- the same waits as dr_purge_release: a live archive run would still be
  -- creating objects for this tag
  IF r.archive_claim IS NOT NULL AND r.archive_claimed_at > now() - interval '10 minutes' THEN
    RAISE EXCEPTION 'release % is being archived (a claim is live); wait for `campfire dr archive` to finish', p_tag;
  END IF;
  RETURN QUERY
    WITH mine AS (
      -- every archived (or half-collected) object this tag references, either way
      SELECT so.*
      FROM storage_objects so
      WHERE so.root = 'archive' AND so.status IN ('archived', 'collecting')
        AND so.id > COALESCE(p_after_id, 0)
        AND (EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.archive_key = so.storage_key)
             OR EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag
                        AND m.storage_key = so.archived_from_key AND m.content_hash = so.content_hash))
    )
    SELECT mine.id, mine.backend, mine.bucket, mine.storage_key, mine.content_hash,
           mine.size_bytes, mine.stored_size_bytes, mine.product_type, mine.status
    FROM mine
    WHERE mine.status = 'collecting'
       OR archive_object_references(mine.storage_key, mine.archived_from_key, mine.content_hash) = ARRAY[p_tag]
    ORDER BY mine.id
    LIMIT p_limit;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_storage_budget()
  RETURNS json
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  result JSON;
  is_admin BOOLEAN;
  cap_bytes BIGINT := 20::BIGINT * 1024 * 1024 * 1024 * 1024;  -- 20 TB (OSN allocation)
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

  -- Everything that still occupies the bucket, at its stored size (see the
  -- header comment: every status but 'collected').
  SELECT COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0) INTO registry_bytes
  FROM storage_objects
  WHERE status <> 'collected';

  SELECT COALESCE(SUM(total_size_bytes), 0) INTO tile_bytes
  FROM map_layers;

  total_bytes := registry_bytes + tile_bytes;

  SELECT json_build_object(
    'cap_bytes', cap_bytes,
    'total_bytes', total_bytes,
    'pct_used', ROUND((total_bytes::NUMERIC / NULLIF(cap_bytes, 0)) * 100, 2),
    'registry_bytes', registry_bytes,
    'tile_bytes', tile_bytes,
    -- P2 classes. live + archive + gc_pending = registry_bytes; collected occupies nothing.
    'by_class', json_build_object(
      'live', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
               FROM storage_objects WHERE status = 'active'),
      'archive', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
                  FROM storage_objects WHERE status IN ('archived', 'collecting')),
      'gc_pending', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
                     FROM storage_objects WHERE status IN ('superseded', 'revoked')),
      'collected', (SELECT json_build_object('count', COUNT(*), 'bytes', 0,
                                             'logical_bytes', COALESCE(SUM(size_bytes), 0))
                    FROM storage_objects WHERE status = 'collected')
    ),
    'by_backend', (
      SELECT COALESCE(json_object_agg(backend, bytes), '{}'::json)
      FROM (
        SELECT backend, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY backend
      ) t
    ),
    'by_bucket', (
      SELECT COALESCE(json_object_agg(bucket, bytes), '{}'::json)
      FROM (
        SELECT bucket, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY bucket
      ) t
    ),
    'by_product_type', (
      SELECT COALESCE(json_object_agg(product_type, bytes), '{}'::json)
      FROM (
        SELECT product_type, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY product_type
      ) t
    ),
    -- Every status, counted and at logical size (what the files measure
    -- locally); the bucket cost of each class is in by_class.
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
$function$;

CREATE OR REPLACE FUNCTION public.protect_archive_rows()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.root = 'archive' THEN
      IF OLD.status = 'collecting' AND current_setting('campfire.archive_collect', true) = 'on' THEN
        RETURN OLD;   -- dr_archive_collect_finish, after the bytes went
      END IF;
      RAISE EXCEPTION 'archive registry rows are immutable: cannot delete % (only dr_archive_collect_finish removes a collecting row)', OLD.storage_key
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
    IF (to_jsonb(NEW) - 'status' - 'collected_at' - 'updated_at' - 'collect_claim')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'collected_at' - 'updated_at' - 'collect_claim')
    THEN
      RAISE EXCEPTION 'archive registry rows are immutable: % may only move archived -> collecting / collected', OLD.storage_key
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      IF NOT (OLD.status = 'archived' AND NEW.status IN ('collecting', 'collected')) THEN
        RAISE EXCEPTION 'archive registry row % status may only move archived -> collecting / collected (got % -> %)',
          OLD.storage_key, OLD.status, NEW.status USING ERRCODE = 'integrity_constraint_violation';
      END IF;
      NEW.collected_at := CASE WHEN NEW.status = 'collected' THEN now() ELSE OLD.collected_at END;
    ELSIF NEW.collected_at IS DISTINCT FROM OLD.collected_at THEN
      RAISE EXCEPTION 'archive registry row %: collected_at follows the status transition', OLD.storage_key
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$function$;

ALTER TABLE "public"."dr_releases"
  ADD CONSTRAINT "dr_releases_state_check"
    CHECK
    ((state = ANY (ARRAY['draft'::text, 'freezing'::text, 'frozen'::text, 'failed'::text, 'exporting'::text, 'exported'::text, 'copying'::text, 'ready'::text, 'purging'::text,
    'published'::text, 'withdrawn'::text])));

ALTER TABLE "public"."storage_objects"
  ADD CONSTRAINT "storage_objects_root_status_check"
    CHECK
    ((((root = 'live'::text) AND (status <> ALL (ARRAY['archived'::text, 'collected'::text]))) OR ((root = 'archive'::text) AND (status = ANY (ARRAY['archived'::text,
    'collecting'::text, 'collected'::text])) AND (storage_key ~~ 'archive/%'::text))));

ALTER TABLE "public"."storage_objects"
  ADD CONSTRAINT "storage_objects_status_check"
    CHECK ((status = ANY (ARRAY['active'::text, 'superseded'::text, 'revoked'::text, 'archived'::text, 'collecting'::text, 'collected'::text])));

CREATE INDEX idx_dr_manifest_archive_key ON public.dr_manifest USING btree (archive_key)
  WHERE (archive_key IS NOT NULL);

CREATE INDEX idx_dr_manifest_storage_key_hash ON public.dr_manifest USING btree (storage_key, content_hash);

COMMENT ON FUNCTION "public"."archive_collectable"(bigint, integer) IS 'One keyset page (by id, at most 2000) of archive objects no data release''s manifest references (P4e, D-P4-7; `campfire archive gc`): a purged release''s leftovers, every product type alike, plus the collecting rows an interrupted collection left behind. There is no time window: an object lives while any release references it. Admin or service_role.';

COMMENT ON FUNCTION "public"."archive_object_references"(text, text, text) IS 'The release tags whose manifest references an archive object (P4e): by archive_key (the byte pass''s copy) or by (storage_key, content_hash) against the object''s archived_from_key / content_hash (a retained copy). Empty = collectable. Internal to archive_collectable / dr_purge_archive_plan / dr_collect_archive.';

COMMENT ON FUNCTION "public"."archive_register_retained"(jsonb) IS 'Register a copy the retain-on-supersede gate is about to make (P4e): the gate registers BEFORE it copies, so a collection can never claim the row while the copy is in flight (the row is referenced by the member the gate retains). Inserts the archive row for p_row (an archive key under archive/, content_hash, archived_from_key, product_type, size_bytes and the registry metadata), or accepts an existing ARCHIVED row for the same key (the same content, registered by someone else; the gate then only ensures the object exists); a row a collection has claimed (collecting) is refused, since its bytes are about to go. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) IS 'Collection phase 1 (P4e, D-P4-6 / D-P4-7): claim archive objects for deletion, archived -> collecting, owned by p_claim (generated when NULL; returned as claim), under the freeze advisory lock (no new reference can appear) and, with p_tag, the release row lock; the tag is left in state purging, which publish, archive, export and re-freeze refuse until dr_purge_release removes it (a published or withdrawn tag is refused here). Refuses a key any release other than p_tag references, and a key another run''s claim refreshed in the last 10 minutes (an older claim is a dead run''s and is taken over; the token fences that run if it resumes). The caller then verifies, deletes the bytes and calls dr_archive_collect_finish with the same claim. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) IS 'Collection phase 3 (P4e): delete the collecting registry rows of the given keys that p_claim owns (the trigger allows exactly this, under the transaction-local campfire.archive_collect) once their bytes are gone, write a 7-day tombstone per key (archive_collections) and log one `collect` deploy event (p_tag is provenance only). A key that is not a collecting archive row of this claim is skipped and counted, so a re-run after a crash, or a run taken over, is harmless. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) IS 'Collection phase 2 gate (P4e): of the given keys, those still claimed by p_claim (status collecting, same token), with the claim refreshed. The caller deletes exactly these from the bucket and finishes them; a key another run took over, or recreated since, is not returned and is left alone. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) IS 'One keyset page (by storage_key, at most 2000) of a data release''s members still to archive (P4c): pending, plus failed when asked, optionally under one key prefix; per member the source of its frozen bytes (the live key while it still holds them, else the retained archive copy, else NULL), a stamped archive object of the same science from another PUBLISHED (or withdrawn) release to reuse (D-P4-6), the registry metadata for the archive row, the tombstone of its copy target if a collection deleted that key lately (target_collected_at, P4e) and page_at, the server time the page was handed out, which the pass carries into its outcomes so dr_bytes_mark can refuse one whose key a collection deleted after. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) IS 'One keyset page (by id, at most 2000) of the archive objects only this UNPUBLISHED release references (P4e, D-P4-6): the byte pass''s copies and the retained copies made for its members, minus anything another release''s manifest also names, plus the collecting rows an interrupted purge left. `campfire dr purge` runs dr_archive_collect_begin, deletes the bytes, dr_archive_collect_finish, then dr_purge_release. Refuses published / withdrawn tags and waits for a live archive claim. Admin or service_role.';

COMMENT ON FUNCTION "public"."dr_purge_release"(text) IS 'Delete an UNPUBLISHED data release and all its frozen rows (P4a; `campfire dr purge`). Admin JWT or service_role claims (dr_require_admin): a bare postgres superuser session is refused; from psql, set request.jwt.claims to {"role":"service_role"} first. Refuses published / withdrawn tags and a freeze that is still running. Touches no archive object: `campfire dr purge` first collects the archive objects only this tag references (dr_purge_archive_plan, dr_archive_collect_begin, the bucket delete, dr_archive_collect_finish; D-P4-6), then calls this.';

GRANT EXECUTE ON FUNCTION "public"."archive_collectable"(bigint, integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."archive_collectable"(bigint, integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_collectable"(bigint, integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_collectable"(bigint, integer) TO "service_role";

REVOKE ALL ON FUNCTION "public"."archive_object_references"(text, text, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_object_references"(text, text, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_object_references"(text, text, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."archive_register_retained"(jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."archive_register_retained"(jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_register_retained"(jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."archive_register_retained"(jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_begin"(text[], text, uuid) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_finish"(text[], text, uuid) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_archive_collect_verify"(text[], uuid) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_bytes_pending"(text, text, integer, boolean, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_purge_archive_plan"(text, bigint, integer) TO "service_role";

REVOKE ALL ON TABLE "public"."archive_collections" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."archive_collections" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."archive_collections" TO "service_role";
