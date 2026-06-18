-- Atomic object reconciliation apply (GitHub #184).
--
-- Hand-authored (not raw `supabase db diff` output). The diff for this change
-- is contaminated by migra limitations: it emits spurious drop+recreate of the
-- materialized views / views (mv_filter_options, mv_programs_overview,
-- nircam_reduction_progress, spectrum_flag_summary) and an unrelated
-- accessible_program_slugs redefinition, and it MISSES the compute_object_
-- redshift_auto change (migra does not track a function's SET attributes, and
-- that change only adds `SET statement_timeout`). This migration therefore
-- carries exactly the three function changes made in supabase/schemas/functions.sql:
--   1. apply_object_reconciliation  (new) — one-transaction reconcile apply
--   2. bulk_set_target_object_fks   (changed) — statement_timeout + typed plan
--   3. compute_object_redshift_auto (changed) — statement_timeout guard

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.apply_object_reconciliation(p_field text, p_inserts jsonb DEFAULT '[]'::jsonb, p_revivals jsonb DEFAULT '[]'::jsonb, p_updates jsonb DEFAULT '[]'::jsonb, p_orphan_ids integer[] DEFAULT '{}'::integer[], p_updated_at timestamp with time zone DEFAULT now())
 RETURNS jsonb
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
DECLARE
  v_insert_id_map JSONB := '{}'::jsonb;
  v_revived_ids   INTEGER[] := '{}';
  v_updated_ids   INTEGER[] := '{}';
  v_orphaned      INTEGER := 0;
  v_reactivated   INTEGER := 0;
  v_fk_count      INTEGER := 0;
BEGIN
  -- 1. Orphans FIRST: soft-delete active objects that lost all members, but
  --    EXCLUDE any whose object_id an insert is about to re-adopt. Ghost
  --    recovery: a stranded active ghost is classified as an orphan while the
  --    real new cluster is an insert holding the same IAU name; the upsert in
  --    step 2 adopts that row, so it must not be soft-deleted out from under it.
  UPDATE objects o SET
    is_active = false,
    last_data_change_at = p_updated_at,
    staleness_reason = 'membership_changed',
    updated_at = p_updated_at
  WHERE o.id = ANY(p_orphan_ids)
    AND o.is_active
    AND o.object_id NOT IN (
      SELECT x.object_id FROM jsonb_to_recordset(p_inserts) AS x(object_id TEXT)
    );
  GET DIAGNOSTICS v_orphaned = ROW_COUNT;

  -- 2. Inserts (idempotent). ON CONFLICT re-adopts a ghost / soft-deleted row
  --    holding this object_id. We deliberately do NOT touch version /
  --    redshift_* / last_inspected_* so inspection state — and the triggers
  --    scoped to those columns — stay untouched; a collision is only ever a
  --    freshly stranded ghost with no inspection history.
  WITH ins AS (
    INSERT INTO objects (
      object_id, field, ra, dec, n_targets, n_spectra,
      programs, gratings, observations, max_snr, max_exposure_time, updated_at
    )
    SELECT x.object_id, p_field, x.ra, x.dec, x.n_targets, x.n_spectra,
           x.programs, x.gratings, x.observations, x.max_snr,
           x.max_exposure_time, p_updated_at
    FROM jsonb_to_recordset(p_inserts) AS x(
      object_id TEXT, ra DOUBLE PRECISION, dec DOUBLE PRECISION,
      n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    ON CONFLICT (object_id) DO UPDATE SET
      field = EXCLUDED.field,
      ra = EXCLUDED.ra,
      dec = EXCLUDED.dec,
      n_targets = EXCLUDED.n_targets,
      n_spectra = EXCLUDED.n_spectra,
      programs = EXCLUDED.programs,
      gratings = EXCLUDED.gratings,
      observations = EXCLUDED.observations,
      max_snr = EXCLUDED.max_snr,
      max_exposure_time = EXCLUDED.max_exposure_time,
      is_active = true,
      last_data_change_at = p_updated_at,
      staleness_reason = 'membership_changed',
      updated_at = p_updated_at
    RETURNING id, object_id
  )
  SELECT coalesce(jsonb_object_agg(object_id, id), '{}'::jsonb)
  INTO v_insert_id_map
  FROM ins;

  -- 3. Revivals: reactivate inactive objects matched by position; refresh
  --    centroid + aggregates.
  WITH rev AS (
    UPDATE objects o SET
      is_active = true,
      object_id = r.object_id,
      ra = r.ra,
      dec = r.dec,
      n_targets = r.n_targets,
      n_spectra = r.n_spectra,
      programs = r.programs,
      gratings = r.gratings,
      observations = r.observations,
      max_snr = r.max_snr,
      max_exposure_time = r.max_exposure_time,
      last_data_change_at = p_updated_at,
      staleness_reason = 'membership_changed',
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_revivals) AS r(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    WHERE o.id = r.object_db_id
    RETURNING o.id
  )
  SELECT coalesce(array_agg(id), '{}') INTO v_revived_ids FROM rev;

  -- 4. Updates: refresh aggregates; set staleness only when provided;
  --    reactivate a membership-matched inactive object when reactivate=true.
  WITH upd AS (
    UPDATE objects o SET
      object_id = u.object_id,
      ra = u.ra,
      dec = u.dec,
      n_targets = u.n_targets,
      n_spectra = u.n_spectra,
      programs = u.programs,
      gratings = u.gratings,
      observations = u.observations,
      max_snr = u.max_snr,
      max_exposure_time = u.max_exposure_time,
      is_active = CASE WHEN u.reactivate THEN true ELSE o.is_active END,
      staleness_reason = CASE
        WHEN u.reactivate THEN 'membership_changed'
        WHEN u.staleness_reason IS NOT NULL THEN u.staleness_reason
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN u.reactivate OR u.staleness_reason IS NOT NULL THEN p_updated_at
        ELSE o.last_data_change_at
      END,
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_updates) AS u(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION,
      staleness_reason TEXT, reactivate BOOLEAN
    )
    WHERE o.id = u.object_db_id
    RETURNING o.id AS id, u.reactivate AS reactivated
  )
  SELECT coalesce(array_agg(id), '{}'),
         coalesce(count(*) FILTER (WHERE reactivated), 0)
  INTO v_updated_ids, v_reactivated
  FROM upd;

  -- 5. Target FK assignment — the actual fix. Derive (target, object) pairs
  --    from each operation's member_target_db_ids (resolving insert object ids
  --    via the RETURNING map) and apply them in ONE typed, index-friendly
  --    UPDATE, inside this same transaction as the inserts above.
  WITH pairs AS (
    SELECT unnest(x.member_target_db_ids) AS target_id,
           (v_insert_id_map ->> x.object_id)::INTEGER AS object_id
    FROM jsonb_to_recordset(p_inserts)
      AS x(object_id TEXT, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(r.member_target_db_ids), r.object_db_id
    FROM jsonb_to_recordset(p_revivals)
      AS r(object_db_id INTEGER, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(u.member_target_db_ids), u.object_db_id
    FROM jsonb_to_recordset(p_updates)
      AS u(object_db_id INTEGER, member_target_db_ids INTEGER[])
  )
  UPDATE targets t SET
    object_id = p.object_id,
    updated_at = p_updated_at
  FROM pairs p
  WHERE t.id = p.target_id;
  GET DIAGNOSTICS v_fk_count = ROW_COUNT;

  RETURN jsonb_build_object(
    'insert_id_map', v_insert_id_map,
    'revived_ids', to_jsonb(v_revived_ids),
    'updated_ids', to_jsonb(v_updated_ids),
    'inserted_count', (SELECT count(*) FROM jsonb_object_keys(v_insert_id_map)),
    'revived_count', coalesce(array_length(v_revived_ids, 1), 0),
    'updated_count', coalesce(array_length(v_updated_ids, 1), 0),
    'reactivated_count', v_reactivated,
    'orphaned_count', v_orphaned,
    'target_fks_set', v_fk_count
  );
END;
$function$
;

CREATE OR REPLACE FUNCTION public.bulk_set_target_object_fks(p_pairs jsonb, p_updated_at timestamp with time zone DEFAULT now())
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
BEGIN
  UPDATE targets t SET
    object_id = pair.object_id,
    updated_at = p_updated_at
  FROM jsonb_to_recordset(p_pairs) AS pair(target_id integer, object_id integer)
  WHERE t.id = pair.target_id;
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.apply_object_reconciliation(text, jsonb, jsonb, jsonb, integer[], timestamptz) TO service_role;

GRANT EXECUTE ON FUNCTION public.bulk_set_target_object_fks(jsonb, timestamptz) TO service_role;

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field TEXT)
 RETURNS INTEGER
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
DECLARE
  n INTEGER;
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_quality AS quality,
           (
             SELECT s.redshift_auto
             FROM targets t
             JOIN spectra s ON s.target_id = t.target_id
             WHERE t.object_id = o.id
               AND s.redshift_auto IS NOT NULL
             ORDER BY
               CASE
                 WHEN s.grating = 'PRISM' THEN 0
                 WHEN s.grating IN ('G140M', 'G235M', 'G395M') THEN 1
                 WHEN s.grating IN ('G140H', 'G235H', 'G395H') THEN 2
                 ELSE 3
               END ASC,
               s.exposure_time DESC NULLS LAST,
               s.id ASC
             LIMIT 1
           ) AS new_val
    FROM objects o
    WHERE o.field = p_field
  )
  UPDATE objects o
  SET redshift_auto = c.new_val,
      staleness_reason = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
        THEN 'reprocessed'
        ELSE o.staleness_reason
      END,
      last_data_change_at = CASE
        WHEN c.quality >= 2
             AND c.old_auto IS DISTINCT FROM c.new_val
        THEN NOW()
        ELSE o.last_data_change_at
      END,
      updated_at = NOW()
  FROM computed c
  WHERE o.id = c.id
    AND o.redshift_auto IS DISTINCT FROM c.new_val;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.compute_object_redshift_auto(text) TO service_role;
