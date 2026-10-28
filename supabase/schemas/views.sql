-- =============================================================================
-- CAMPFIRE Supabase Schema: Views
-- =============================================================================
-- Canonical source of truth for all views and materialized views.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- ============================================================
-- MATERIALIZED VIEWS
-- ============================================================

-- 1. mv_filter_options
--    Cached distinct filter options (fields, observations, gratings).
--    Refresh after data deployments using refresh_filter_options().
DROP MATERIALIZED VIEW IF EXISTS public.mv_filter_options;

-- Published-only (B1): this is a GLOBAL matview with no per-viewer scope, so it
-- is restricted to published data for everyone. fields/observations are derived
-- from targets via has_published_spectrum; gratings from spectra via
-- deploy_status. Admins get draft filter options through a live query elsewhere,
-- not this matview. In B1 every predicate is a no-op (nothing is unpublished yet).
CREATE MATERIALIZED VIEW public.mv_filter_options AS
SELECT 1 AS id,
    ARRAY(SELECT DISTINCT targets.field FROM public.targets WHERE targets.has_published_spectrum ORDER BY targets.field) AS fields,
    ARRAY(SELECT DISTINCT targets.observation FROM public.targets WHERE targets.observation IS NOT NULL AND targets.has_published_spectrum ORDER BY targets.observation) AS observations,
    ARRAY(SELECT DISTINCT spectra.grating FROM public.spectra WHERE spectra.deploy_status = 'published' ORDER BY spectra.grating) AS gratings,
    -- Photometry bands the catalog's band filter can offer, in WAVELENGTH order
    -- (the picker is read by eye, and f090w..f444w is not alphabetical once a
    -- field mixes HST and JWST filters). Band names are per-field catalog
    -- configuration ([field.bands] in photometry.toml), so this list is data,
    -- not a fixed enum like the line catalog. Same published-only scope as the
    -- rest of the matview, via the parent object.
    ARRAY(
        SELECT b.band
        FROM public.photometry b
        JOIN public.catalog_matches p ON p.id = b.match_id
        JOIN public.objects o ON o.id = p.object_id
        WHERE o.has_published_spectrum AND o.is_active
        GROUP BY b.band
        ORDER BY min(b.wav) NULLS LAST, b.band
    ) AS photometry_bands
WITH DATA;

CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);

REVOKE ALL ON TABLE public.mv_filter_options FROM anon;
GRANT ALL ON TABLE public.mv_filter_options TO authenticated;
GRANT ALL ON TABLE public.mv_filter_options TO service_role;


-- 2. mv_programs_overview
--    Pre-aggregated program stats (target counts, gratings, fields, observations,
--    last full-deployment timestamp). Refresh via refresh_programs_overview().
DROP MATERIALIZED VIEW IF EXISTS public.mv_programs_overview;

CREATE MATERIALIZED VIEW public.mv_programs_overview AS
SELECT
    p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.target_count, 0)::bigint AS target_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids,
    COALESCE(pids.n_observations, 0)::bigint AS n_observations,
    last_red.last_reduced_at
FROM programs p
LEFT JOIN (
    SELECT t.program_slug,
        COUNT(DISTINCT t.target_id) AS target_count,
        ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
            FILTER (WHERE s.grating IS NOT NULL) AS gratings,
        ARRAY_AGG(DISTINCT t.field ORDER BY t.field) AS fields,
        ARRAY_AGG(DISTINCT t.observation ORDER BY t.observation) AS observations
    FROM targets t
    -- Published-only (B1): restrict the grating aggregation to published
    -- spectra. Predicate lives in the LEFT JOIN ON clause so targets with no
    -- published spectrum are still counted (just contribute no gratings).
    -- No-op in B1 (nothing is unpublished yet); guards B2 draft data.
    LEFT JOIN spectra s ON s.target_id = t.target_id AND s.deploy_status = 'published'
    GROUP BY t.program_slug
) stats ON p.slug = stats.program_slug
LEFT JOIN (
    SELECT program_slug,
        ARRAY_AGG(DISTINCT jwst_program_id ORDER BY jwst_program_id) AS jwst_pids,
        COUNT(*)::bigint AS n_observations
    FROM observations
    GROUP BY program_slug
) pids ON p.slug = pids.program_slug
LEFT JOIN (
    SELECT o.program_slug,
        MAX(d.reduced_at) AS last_reduced_at
    FROM observations o
    JOIN deployments d ON d.observation = o.name
    WHERE d.source_ids_filter IS NULL
    GROUP BY o.program_slug
) last_red ON p.slug = last_red.program_slug
WITH DATA;

CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview (slug);

GRANT SELECT ON public.mv_programs_overview TO authenticated;


-- 3. mv_observations_overview  (perf T1-5 / #501)
--    Per-observation stats + provenance for EVERY observation, published-only:
--    exactly the row shape get_observations_overview() returns, minus the
--    caller's program scope. That scope is applied at read time by the two
--    reader RPCs (get_observations_overview, get_observation_stats), which are
--    SECURITY DEFINER and intersect the requested slugs with
--    accessible_program_slugs() (scoped_program_slugs). Hence NO grant to
--    authenticated: observations are program-gated by RLS and a matview has
--    none, so it must not be readable through PostgREST directly.
--    Refresh: refresh_observations_overview() at deploy, refresh_all_matviews()
--    nightly via pg_cron.
-- BEGIN mv_observations_overview
DROP MATERIALIZED VIEW IF EXISTS public.mv_observations_overview;

CREATE MATERIALIZED VIEW public.mv_observations_overview AS
WITH stats AS (
    SELECT t.observation, t.program_slug,
        COUNT(DISTINCT t.target_id) AS target_count,
        COUNT(s.id) AS spectrum_count,
        COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
        ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
            FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    -- Published-only: unpublished spectra don't contribute to counts/size/gratings.
    LEFT JOIN public.spectra s ON s.target_id = t.target_id AND s.deploy_status = 'published'
    -- ...and draft-only targets don't count at all (what targets RLS enforced
    -- for the old invoker query; this view is built by the owner).
    WHERE t.has_published_spectrum
    GROUP BY t.observation, t.program_slug
)
SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    -- Gratings from the deployed spectra, falling back to observations.toml's
    -- declared list for observations with no spectra yet.
    CASE
        WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
        ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
FROM public.observations o
JOIN public.programs p ON p.slug = o.program_slug
LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
-- Provenance from the most recent PUBLISHED full deployment (source_ids_filter
-- IS NULL). Published-only like the rest of the snapshot: a draft re-reduction
-- must not surface its version stamps here before it is published.
LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
      AND d.status = 'published'
    ORDER BY d.deployed_at DESC
    LIMIT 1
) full_dep ON true
-- Published patch deployments since that full one.
LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND d.status = 'published'
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
) patches ON true
WITH DATA;

CREATE UNIQUE INDEX mv_observations_overview_observation
    ON public.mv_observations_overview (observation);

-- Default privileges in this schema hand every new relation to anon /
-- authenticated; take that back — the reader RPCs (SECURITY DEFINER) are the
-- only way in.
REVOKE ALL ON public.mv_observations_overview FROM anon, authenticated;
GRANT SELECT ON public.mv_observations_overview TO service_role;
-- END mv_observations_overview


-- ============================================================
-- VIEWS
-- ============================================================

-- 3. spectrum_flag_summary
--    Expands per-spectrum dq_flags bitmask into a label array via cross join
--    with flag_definitions. Replaces the Phase-D-deprecated target_flag_summary
--    (which also covered spectral_features — that flag category is dropped).
DROP VIEW IF EXISTS public.spectrum_flag_summary;
DROP VIEW IF EXISTS public.target_flag_summary;
-- security_invoker (B1): this was a plain (definer) view that bypassed the
-- spectra RLS policy. Flip to invoker semantics so the caller's RLS applies, and
-- add a published predicate so draft spectra are hidden from non-admins. In B1
-- every spectrum is published, so this is a no-op; it guards B2 draft data.
CREATE VIEW public.spectrum_flag_summary
WITH (security_invoker = true) AS
SELECT
    s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'dq_flags' AND (s.dq_flags & fd.value) > 0) AS dq_flags_labels
FROM public.spectra s
CROSS JOIN public.flag_definitions fd
WHERE (s.deploy_status = 'published' OR public.is_admin())
GROUP BY s.id, s.target_id, s.grating;

REVOKE ALL ON TABLE public.spectrum_flag_summary FROM anon;
GRANT ALL ON TABLE public.spectrum_flag_summary TO authenticated;
GRANT ALL ON TABLE public.spectrum_flag_summary TO service_role;


-- 4. targets_with_flags — dropped in Phase D (no consumers after the
-- targets-list view was removed).
DROP VIEW IF EXISTS public.targets_with_flags;


-- 5. nircam_reduction_progress
--    Inspection-triage progress per field/filter/detector for the admin
--    dashboard. The per-stage at_<step> distribution columns were dropped
--    (2026-07 dashboard redesign): whole filters sit at the same pipeline
--    stage in practice, so the distribution carried no signal. The view now
--    answers the questions reviewers actually have — how far along is
--    inspection (approved+excluded vs total), how many masks exist, and
--    which detectors still have pending exposures (feeding the dashboard's
--    quick-filter links). Detector granularity is aggregated back up to
--    filter/field client-side.
DROP VIEW IF EXISTS public.nircam_reduction_progress;
-- security_invoker (B1): this was a plain (definer) view that bypassed the
-- admin-only RLS on nircam_exposures, leaking QA aggregates to all authenticated
-- users. Flip to invoker semantics so the nircam_exposures RLS policy applies.
-- (NIRCam deploy_status gating is deferred to B2; this is only the leak fix.)
CREATE VIEW public.nircam_reduction_progress
WITH (security_invoker = true) AS
SELECT
    field,
    filter,
    detector,
    count(*) AS total,
    -- Triage summary. "Masked" is derived state: a non-null mask_regions is
    -- the sole signal (matches the admin exposure table's Masked column).
    count(*) FILTER (WHERE review_status = 'pending')  AS pending_review,
    count(*) FILTER (WHERE review_status = 'approved') AS approved,
    count(*) FILTER (WHERE review_status = 'excluded') AS excluded,
    count(*) FILTER (WHERE mask_regions IS NOT NULL)   AS masked,
    count(*) FILTER (WHERE correction = 'needed')      AS needs_correction
FROM public.nircam_exposures
GROUP BY field, filter, detector;

GRANT SELECT ON public.nircam_reduction_progress TO authenticated;


-- N. line_fits_status
--    Staleness ledger for the emission-line catalog: each fit joined to the
--    live inspection state of its object and the current spectrum hash.
--    stale_redshift: the object's redshift / quality / version moved since the
--    fit (re-pull + `cfpipe nirspec linefit` refits exactly those);
--    stale_spectrum: the spectrum's science content was re-deployed since the
--    fit (science hash when both sides carry one, whole-file hash otherwise).
--    security_invoker: rows follow the caller's RLS on line_fits.
DROP VIEW IF EXISTS public.line_fits_status;

CREATE VIEW public.line_fits_status
WITH (security_invoker = true) AS
SELECT f.spectrum_id,
       f.target_id,
       f.grating,
       f.program_slug,
       f.observation,
       f.z_used,
       f.z_source,
       f.z_quality,
       f.object_id,
       f.object_version,
       f.fit_version,
       f.fitted_at,
       o.object_id AS current_object_id,
       o.redshift AS current_redshift,
       o.redshift_quality AS current_quality,
       o.version AS current_object_version,
       -- one definition of "the inspected redshift moved since the fit"
       -- (line_fit_stale_redshift, functions.sql), shared with the catalog
       -- filter helpers so a filtered list and this ledger cannot disagree
       (o.id IS NOT NULL AND public.line_fit_stale_redshift(
          o.version, o.redshift_quality, (o.redshift)::double precision,
          f.object_version, f.z_quality, f.z_used)) AS stale_redshift,
       -- likewise one definition of "the spectrum changed since the fit"
       -- (line_fit_stale_spectrum): science hash when both sides carry one,
       -- whole-file hash otherwise (P0c)
       public.line_fit_stale_spectrum(
          s.file_hash, s.science_hash,
          f.spectrum_hash, f.spectrum_science_hash) AS stale_spectrum
FROM public.line_fits f
JOIN public.spectra s ON s.id = f.spectrum_id
LEFT JOIN public.targets t ON t.target_id = f.target_id
LEFT JOIN public.objects o ON o.id = t.object_id
-- soft-deleted objects are hidden everywhere else; keep the ledger consistent
WHERE (o.id IS NULL OR o.is_active = true);

REVOKE ALL ON TABLE public.line_fits_status FROM anon;
GRANT ALL ON TABLE public.line_fits_status TO authenticated;
GRANT ALL ON TABLE public.line_fits_status TO service_role;

-- ============================================================================
-- reader.objects / reader.dr_objects (Catalog Read Model phase 4)
-- ============================================================================
-- The objects tables carry aggregates over every member (programs,
-- observations, gratings, n_targets, n_spectra, max_snr, max_exposure_time,
-- search_text; evidence_programs and z_source_spectrum_id on the twin), private
-- members included: the API recomputes them per caller
-- (object_scoped_aggregates, get_objects_for_sync) and the query whitelist
-- leaves them out. For the public SQL reader these views are the same rows
-- and columns with the aggregates computed over the public, published members
-- only. They run with their owner's privileges (postgres, not under RLS), so
-- the row predicate and the member predicates are explicit here and equal the
-- reader_select_* policies; check_reader_role.sql compares them. As in
-- object_scoped_aggregates, an object whose members are all visible keeps its
-- stored aggregates (one probe); only a mixed object is recomputed.

CREATE OR REPLACE VIEW "reader"."objects" AS
WITH pub AS (
  SELECT COALESCE(array_agg(p.slug), '{}'::text[]) AS slugs FROM public.programs p WHERE p.is_public
)
SELECT o.id, o.object_id, o.field, o.ra, o.dec, o.created_at,
       CASE WHEN a.use_stored THEN o.n_targets ELSE a.n_targets END AS n_targets,
       CASE WHEN a.use_stored THEN o.n_spectra ELSE a.n_spectra END AS n_spectra,
       CASE WHEN a.use_stored THEN o.programs ELSE a.programs END AS programs,
       CASE WHEN a.use_stored THEN o.gratings ELSE a.gratings END AS gratings,
       CASE WHEN a.use_stored THEN o.observations ELSE a.observations END AS observations,
       CASE WHEN a.use_stored THEN o.max_snr ELSE a.max_snr END AS max_snr,
       CASE WHEN a.use_stored THEN o.max_exposure_time ELSE a.max_exposure_time END AS max_exposure_time,
       o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry, o.updated_at,
       o.redshift_auto, o.redshift_inspected, o.redshift_quality, o.inspected_used_auto, o.redshift,
       o.last_inspected_at, o.last_inspected_by, o.last_data_change_at, o.staleness_reason,
       o.reviewed_spectrum_ids, o.needs_review, o.version, o.is_active, o.has_published_spectrum,
       CASE WHEN a.use_stored THEN o.search_text ELSE a.search_text END AS search_text,
       o.is_public
FROM public.objects o
CROSS JOIN pub
CROSS JOIN LATERAL (
  WITH m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM public.targets t
    WHERE t.object_id = o.id AND t.is_public AND t.has_published_spectrum
  ), sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM public.spectra s
    WHERE s.target_id IN (SELECT target_id FROM m) AND s.is_public AND s.deploy_status = 'published'
  )
  SELECT (o.programs <@ pub.slugs
          AND NOT EXISTS (SELECT 1 FROM public.targets t JOIN public.spectra s ON s.target_id = t.target_id
                          WHERE t.object_id = o.id
                            AND (NOT t.is_public OR NOT s.is_public OR s.deploy_status <> 'published'))) AS use_stored,
         COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[] AS programs,
         COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[] AS gratings,
         COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[] AS observations,
         (SELECT count(*) FROM m)::integer AS n_targets,
         (SELECT count(*) FROM sp)::integer AS n_spectra,
         (SELECT max(sp.signal_to_noise) FROM sp) AS max_snr,
         (SELECT max(sp.exposure_time) FROM sp) AS max_exposure_time,
         concat_ws(' ', o.object_id,
                   (SELECT string_agg(DISTINCT m.target_id, ' ') FROM m),
                   (SELECT string_agg(DISTINCT m.program_slug, ' ') FROM m),
                   (SELECT string_agg(DISTINCT m.observation, ' ') FROM m)) AS search_text
) a
WHERE o.is_public AND o.is_active AND o.has_published_spectrum;

ALTER VIEW "reader"."objects" OWNER TO "postgres";
COMMENT ON VIEW "reader"."objects" IS 'objects for the public SQL reader (campfire_reader): the public, active objects with a published spectrum, their member aggregates computed over public published members only. Same columns as public.objects.';

CREATE OR REPLACE VIEW "reader"."dr_objects" AS
WITH pub AS (
  SELECT COALESCE(array_agg(p.slug), '{}'::text[]) AS slugs FROM public.programs p WHERE p.is_public
)
SELECT o.tag, o.id, o.object_id, o.field, o.ra, o.dec, o.created_at,
       CASE WHEN a.use_stored THEN o.n_targets ELSE a.n_targets END AS n_targets,
       CASE WHEN a.use_stored THEN o.n_spectra ELSE a.n_spectra END AS n_spectra,
       CASE WHEN a.use_stored THEN o.programs ELSE a.programs END AS programs,
       CASE WHEN a.use_stored THEN o.gratings ELSE a.gratings END AS gratings,
       CASE WHEN a.use_stored THEN o.observations ELSE a.observations END AS observations,
       CASE WHEN a.use_stored THEN o.max_snr ELSE a.max_snr END AS max_snr,
       CASE WHEN a.use_stored THEN o.max_exposure_time ELSE a.max_exposure_time END AS max_exposure_time,
       o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry, o.updated_at,
       o.redshift_auto, o.redshift_inspected, o.redshift_quality, o.inspected_used_auto, o.redshift,
       o.last_inspected_at, o.last_inspected_by, o.last_data_change_at, o.staleness_reason,
       o.reviewed_spectrum_ids, o.needs_review, o.version, o.is_active, o.has_published_spectrum,
       CASE WHEN a.use_stored THEN o.search_text ELSE a.search_text END AS search_text,
       CASE WHEN a.use_stored THEN o.evidence_programs
            ELSE ARRAY(SELECT e FROM unnest(o.evidence_programs) AS e WHERE e = ANY (a.programs) ORDER BY e) END AS evidence_programs,
       CASE WHEN a.use_stored OR EXISTS (SELECT 1 FROM public.dr_spectra s WHERE s.tag = o.tag AND s.id = o.z_source_spectrum_id
                                           AND s.is_public AND s.deploy_status = 'published')
            THEN o.z_source_spectrum_id ELSE NULL END AS z_source_spectrum_id,
       o.is_public
FROM public.dr_objects o
CROSS JOIN pub
CROSS JOIN LATERAL (
  WITH m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM public.dr_targets t
    WHERE t.tag = o.tag AND t.object_id = o.id AND t.is_public AND t.has_published_spectrum
  ), sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM public.dr_spectra s
    WHERE s.tag = o.tag AND s.target_id IN (SELECT target_id FROM m) AND s.is_public AND s.deploy_status = 'published'
  )
  SELECT (o.programs <@ pub.slugs
          AND NOT EXISTS (SELECT 1 FROM public.dr_targets t JOIN public.dr_spectra s ON s.tag = t.tag AND s.target_id = t.target_id
                          WHERE t.tag = o.tag AND t.object_id = o.id
                            AND (NOT t.is_public OR NOT s.is_public OR s.deploy_status <> 'published'))) AS use_stored,
         COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[] AS programs,
         COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[] AS gratings,
         COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[] AS observations,
         (SELECT count(*) FROM m)::integer AS n_targets,
         (SELECT count(*) FROM sp)::integer AS n_spectra,
         (SELECT max(sp.signal_to_noise) FROM sp) AS max_snr,
         (SELECT max(sp.exposure_time) FROM sp) AS max_exposure_time,
         concat_ws(' ', o.object_id,
                   (SELECT string_agg(DISTINCT m.target_id, ' ') FROM m),
                   (SELECT string_agg(DISTINCT m.program_slug, ' ') FROM m),
                   (SELECT string_agg(DISTINCT m.observation, ' ') FROM m)) AS search_text
) a
WHERE o.is_public AND o.is_active AND o.has_published_spectrum
  AND EXISTS (SELECT 1 FROM public.dr_releases r WHERE r.tag = o.tag AND r.state = 'published');

ALTER VIEW "reader"."dr_objects" OWNER TO "postgres";
COMMENT ON VIEW "reader"."dr_objects" IS 'dr_objects for the public SQL reader (campfire_reader): the frozen public objects of published releases, their member aggregates computed over the frozen public published members only. Same columns as public.dr_objects.';

GRANT SELECT ON TABLE "reader"."objects" TO "campfire_reader";
GRANT SELECT ON TABLE "reader"."dr_objects" TO "campfire_reader";
