-- Storage sync stream: index-only keyset walk over active finals.
--
-- On 2026-09-24 a first-time `campfire sync` took the production database
-- down for ~70 minutes. The storage stream (get_storage_objects_for_sync with
-- the client's FINAL_PRODUCT_TYPES) keysets on storage_objects.id, but finals
-- are ~82k of ~773k registry rows and interleaved with their sidecars, so each
-- 10k-row page walked ~234k rows of the pkey and fetched their heap pages
-- (3.8 s, 200-430 MB read per page; 7.7 s for the first page, whose count
-- ran the publish-scope check over every final). A full sync read the whole
-- ~800 MB table, far past the instance's cache and disk-throughput budget.
-- The per-row scope check and the page's GREATEST(updated_at) join also
-- fetched the spectra heap, whose rows are ~3 KB wide.
--
-- Fix (supabase/schemas/indexes.sql, functions.sql):
--   * idx_storage_objects_sync_finals, NEW: partial btree on (id) over active
--     nirspec_spec / nircam_mosaic rows, INCLUDE-ing every column the RPC
--     reads, so the walk is index-only (~20-30 MB).
--   * idx_spectra_spectrum_id_scope, NEW: (spectrum_id) INCLUDE
--     (deploy_status, program_slug, updated_at) for the scope probe and the
--     updated_at join, index-only.
--   * get_storage_objects_for_sync: `matched` selects explicit columns (not
--     so.*, which would defeat index-only), and the spectrum scope check uses
--     the row-local spectra.program_slug (perf T2-A, #504) instead of a
--     targets hop. Output is unchanged (verified: identical full keyset walks
--     on a production-shaped copy for finals / few-program / unpublished /
--     all-types / incremental callers).
--
-- Hand-authored (no Docker here for `supabase db diff`); the function and
-- index definitions are copied verbatim from the schema files, which remain
-- the source of truth. The index builds take a brief write lock on
-- storage_objects and spectra (a migration runs in a transaction, so no
-- CONCURRENTLY); both build in seconds at production size.

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT FALSE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the storage_objects_pkey btree. storage_key is NOT usable as a cursor
  -- (only UNIQUE as (backend, bucket, storage_key)), and sync order is
  -- irrelevant to the client (it upserts by key), so this orders by the PK.
  -- Keyset-only since T2-F (#511): the scope predicate (published EXISTS
  -- checks) is evaluated on only ~p_limit rows past the seek.
  p_after_id BIGINT DEFAULT NULL,
  -- Mirror slimming: the client mirrors only the product kinds it can
  -- download (finals by default) and refreshes intermediates per observation /
  -- field when `pull --intermediate` asks for them, so it never pages through
  -- the sidecar and intermediate rows that make up most of the registry.
  -- NULL = unfiltered (the admin full mirror). observation / field scope is a
  -- union, mirroring the client's pending-object query.
  p_product_types TEXT[] DEFAULT NULL,
  p_observations TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT,
              deleted_ids BIGINT[])
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Timeout backstop for the first-page counts; see get_objects_for_sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at, so.spectrum_id
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              -- Row-local program_slug (perf T2-A, #504): no targets hop, and
              -- idx_spectra_spectrum_id_scope answers it index-only.
              SELECT 1 FROM spectra s
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND s.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    -- Explicit columns, not so.*: with the client's final product types this
    -- is an index-only scan of idx_storage_objects_sync_finals, whose INCLUDE
    -- list must cover everything read here.
    SELECT so.id, so.backend, so.bucket, so.storage_key, so.content_hash,
           so.sci_dq_hash, so.size_bytes, so.content_type, so.product_type,
           so.instrument, so.status, so.observation, so.field, so.filter,
           so.spectrum_id, so.exposure_ref, so.deployment_id, so.cfpipe_version,
           so.created_at, so.updated_at
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              -- Row-local program_slug (perf T2-A, #504): no targets hop, and
              -- idx_spectra_spectrum_id_scope answers it index-only.
              SELECT 1 FROM spectra s
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND s.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
      -- Incremental: the row itself changed, or its spectrum did. The
      -- spectrum clause is the resurrection path for non-admins: rows
      -- tombstoned when their spectrum was un-published come back on
      -- republish (which stamps spectra.updated_at) even though the registry
      -- row did not change. A semi-join, not a per-row EXISTS: the recently
      -- changed spectra are a small hashed set. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR so.updated_at > p_updated_since
           OR so.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                 WHERE s2.updated_at > p_updated_since))
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL
           OR scoped.updated_at > p_updated_since
           OR scoped.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                     WHERE s2.updated_at > p_updated_since))
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only. Rows
  -- that left the active state, and active rows whose spectrum was
  -- un-published (invisible to non-admins from now on; the spectrum's
  -- updated_at moves on a deploy_status change). Program-scoped like the
  -- sibling CTEs: a non-admin's tombstones are drawn from rows whose
  -- observation (or spectrum) is in an accessible program, plus field-only
  -- products, which the main scope shows to everyone when published. Only
  -- integer ids travel, and an id the client never mirrored deletes nothing.
  -- Hard deletes (deploy remove) still need a full sync to clear.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::BIGINT[]) AS ids
    FROM (
      SELECT so.id
      FROM storage_objects so
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND so.status <> 'active'
        AND so.updated_at > p_updated_since
        AND (p_include_unpublished
             OR (so.observation IS NULL AND so.field IS NOT NULL)
             OR EXISTS (SELECT 1 FROM observations ob
                        WHERE ob.name = so.observation
                          AND ob.program_slug = ANY(p_program_slugs)))
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      UNION
      SELECT so.id
      FROM storage_objects so
      JOIN spectra s ON s.spectrum_id = so.spectrum_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND NOT p_include_unpublished
        AND so.status = 'active'
        AND s.program_slug = ANY(p_program_slugs)
        AND s.deploy_status <> 'published'
        AND s.updated_at > p_updated_since
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        -- Sync timestamp: the later of the row's own and its spectrum's, so a
        -- row re-sent because the spectrum changed advances the client's
        -- cursor (see get_spectra_for_sync).
        'updated_at', GREATEST(m.updated_at, s.updated_at)
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m
  LEFT JOIN spectra s ON s.spectrum_id = m.spectrum_id;
END;
$$;
CREATE INDEX IF NOT EXISTS idx_spectra_spectrum_id_scope
    ON public.spectra USING btree (spectrum_id)
    INCLUDE (deploy_status, program_slug, updated_at);
CREATE INDEX IF NOT EXISTS idx_storage_objects_sync_finals
    ON public.storage_objects USING btree (id)
    INCLUDE (backend, bucket, storage_key, content_hash, sci_dq_hash, size_bytes,
             content_type, product_type, instrument, status, observation, field,
             "filter", spectrum_id, exposure_ref, deployment_id, cfpipe_version,
             created_at, updated_at)
    WHERE status = 'active'
      AND product_type IN ('nirspec_spec', 'nircam_mosaic');

