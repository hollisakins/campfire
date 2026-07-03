-- =============================================================================
-- CAMPFIRE Supabase Schema: Indexes
-- =============================================================================
-- Canonical source of truth for all database indexes.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- =============================================================================
-- targets (renamed from objects)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_targets_coords
    ON public.targets USING btree (ra, dec);

CREATE INDEX IF NOT EXISTS idx_targets_field_observation
    ON public.targets USING btree (field, observation);

CREATE INDEX IF NOT EXISTS idx_targets_has_sed_plot
    ON public.targets USING btree (has_sed_plot) WHERE (has_sed_plot = true);

CREATE INDEX IF NOT EXISTS idx_targets_target_id_trgm
    ON public.targets USING gin (target_id public.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug_field
    ON public.targets USING btree (program_slug, field);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug_quality
    ON public.targets USING btree (program_slug, redshift_quality);

CREATE INDEX IF NOT EXISTS idx_targets_observation
    ON public.targets USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug_observation
    ON public.targets USING btree (program_slug, observation);

CREATE INDEX IF NOT EXISTS idx_targets_updated_at
    ON public.targets USING btree (updated_at);


CREATE INDEX IF NOT EXISTS idx_targets_object_id
    ON public.targets USING btree (object_id);


-- =============================================================================
-- objects
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_objects_coords
    ON public.objects USING btree (ra, dec);

CREATE INDEX IF NOT EXISTS idx_objects_field
    ON public.objects USING btree (field);

CREATE INDEX IF NOT EXISTS idx_objects_programs
    ON public.objects USING gin (programs);

CREATE INDEX IF NOT EXISTS idx_objects_gratings
    ON public.objects USING gin (gratings);

CREATE INDEX IF NOT EXISTS idx_objects_object_id_trgm
    ON public.objects USING gin (object_id public.gin_trgm_ops);

-- Unified p_search blob (object_id + member target_ids + programs + observations).
-- Replaces the cross-table object_id-ILIKE-OR-EXISTS(targets) predicate, which the
-- planner could not index and which full-scanned the catalog under ORDER BY + LIMIT.
CREATE INDEX IF NOT EXISTS idx_objects_search_text_trgm
    ON public.objects USING gin (search_text public.gin_trgm_ops);

-- Phase A: support filtering/sorting by the new object-level inspection fields.
CREATE INDEX IF NOT EXISTS idx_objects_redshift_quality
    ON public.objects USING btree (redshift_quality);

CREATE INDEX IF NOT EXISTS idx_objects_redshift
    ON public.objects USING btree (redshift);

-- Soft-deleted objects are the rare case; partial index keeps it cheap.
CREATE INDEX IF NOT EXISTS idx_objects_is_active
    ON public.objects USING btree (is_active) WHERE (is_active = false);

-- Backs the incremental-sync path: get_objects_for_sync filters on
-- updated_at > p_updated_since, and the total count CTE uses the same
-- predicate.
CREATE INDEX IF NOT EXISTS idx_objects_updated_at
    ON public.objects USING btree (updated_at);

-- =============================================================================
-- object_photometry
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_object_photometry_field
    ON public.object_photometry USING btree (field);

CREATE INDEX IF NOT EXISTS idx_object_photometry_object_id
    ON public.object_photometry USING btree (object_id) WHERE (object_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_object_photometry_coords
    ON public.object_photometry USING btree (ra, dec);


-- =============================================================================
-- object_lists
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_object_lists_created_by
    ON public.object_lists USING btree (created_by);

CREATE INDEX IF NOT EXISTS idx_object_lists_visibility
    ON public.object_lists USING btree (visibility);


-- =============================================================================
-- object_list_members
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_list_members_object_id
    ON public.object_list_members USING btree (object_id) WHERE (object_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_list_members_list_id
    ON public.object_list_members USING btree (list_id);

-- Composite covering index for the p_list_ids filter subquery in RPCs:
-- WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
CREATE INDEX IF NOT EXISTS idx_list_members_list_id_object_id
    ON public.object_list_members USING btree (list_id, object_id)
    WHERE (object_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_list_members_coords
    ON public.object_list_members USING btree (ra, dec);


-- =============================================================================
-- list_audit_log
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_list_audit_list_id
    ON public.list_audit_log USING btree (list_id);

CREATE INDEX IF NOT EXISTS idx_list_audit_changed_at
    ON public.list_audit_log USING btree (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_list_audit_user_id
    ON public.list_audit_log USING btree (user_id);


-- =============================================================================
-- spectra
-- =============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_spectra_target_grating
    ON public.spectra USING btree (target_id, grating);

CREATE UNIQUE INDEX IF NOT EXISTS idx_spectra_fits_path
    ON public.spectra USING btree (fits_path);

-- Generated spectrum_id (filename basename) — unique because fits_path is unique
-- and the regex is deterministic. Btree supports both equality lookups (search)
-- and ORDER BY (sort) for the spectra table view.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spectra_spectrum_id
    ON public.spectra USING btree (spectrum_id);

-- Trigram index for substring (ILIKE) search on the search bar.
CREATE INDEX IF NOT EXISTS idx_spectra_spectrum_id_trgm
    ON public.spectra USING gin (spectrum_id public.gin_trgm_ops);

-- Unified p_search blob (target_id + spectrum_id). Replaces the cross-table
-- target_id-ILIKE-OR-spectrum_id-ILIKE predicate with a single indexable column.
CREATE INDEX IF NOT EXISTS idx_spectra_search_text_trgm
    ON public.spectra USING gin (search_text public.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_spectra_grating
    ON public.spectra USING btree (grating);

-- Phase A: filter spectra by DQ flag presence (rare, partial keeps it small).
CREATE INDEX IF NOT EXISTS idx_spectra_dq_flags
    ON public.spectra USING btree (dq_flags) WHERE (dq_flags != 0);

-- B1 (#217): non-published spectra are the rare case (zero in B1, a minority in
-- B2); a partial index on the exclusion keeps the admin "show draft/revoked"
-- triage cheap, mirroring idx_objects_is_active / idx_spectra_dq_flags. The
-- common published-only path is served as a residual filter while all rows are
-- published; B2 should add an EXPLAIN-driven partial/covering index on the hot
-- filter+sort key once real draft volume exists (do NOT guess it here).
CREATE INDEX IF NOT EXISTS idx_spectra_deploy_status
    ON public.spectra USING btree (deploy_status) WHERE (deploy_status <> 'published');

-- Phase E: incremental spectra sync keys on updated_at.
CREATE INDEX IF NOT EXISTS idx_spectra_updated_at
    ON public.spectra USING btree (updated_at);


-- =============================================================================
-- deployments
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_deployments_observation
    ON public.deployments USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_deployments_deployed_by
    ON public.deployments USING btree (deployed_by);

CREATE INDEX IF NOT EXISTS idx_deployments_deployed_at
    ON public.deployments USING btree (deployed_at DESC);

CREATE INDEX IF NOT EXISTS idx_deployments_full_obs_recent
    ON public.deployments USING btree (observation, deployed_at DESC)
    WHERE source_ids_filter IS NULL;

-- NIRCam deployments are field-scoped (observation IS NULL); latest-deployment
-- and lifecycle lookups by field had no index (admin audit 2026-07-03, B5).
CREATE INDEX IF NOT EXISTS idx_deployments_field
    ON public.deployments USING btree (field)
    WHERE field IS NOT NULL;


-- =============================================================================
-- comments
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_comments_target
    ON public.comments USING btree (target_id) WHERE (target_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_comments_object_id
    ON public.comments USING btree (object_id) WHERE (object_id IS NOT NULL);


-- =============================================================================
-- flag_audit_log
-- =============================================================================

-- target_id is now nullable (Phase D); keep the index but make it partial so
-- it only covers rows that still target a target (mostly pre-migration).
CREATE INDEX IF NOT EXISTS idx_audit_target
    ON public.flag_audit_log USING btree (target_id) WHERE (target_id IS NOT NULL);

-- New (Phase D): object-level audit lookups for "history of inspection on
-- this object" panels.
CREATE INDEX IF NOT EXISTS idx_audit_object
    ON public.flag_audit_log USING btree (object_id) WHERE (object_id IS NOT NULL);

-- New (Phase D): per-spectrum DQ audit lookups.
CREATE INDEX IF NOT EXISTS idx_audit_spectrum
    ON public.flag_audit_log USING btree (spectrum_id) WHERE (spectrum_id IS NOT NULL);


-- =============================================================================
-- shutters
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_shutters_field
    ON public.shutters USING btree (field);

CREATE INDEX IF NOT EXISTS idx_shutters_object_id
    ON public.shutters USING btree (object_id);

CREATE INDEX IF NOT EXISTS idx_shutters_ra_dec
    ON public.shutters USING btree (center_ra, center_dec);


-- =============================================================================
-- slit_regions
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_slit_regions_field
    ON public.slit_regions USING btree (field);


-- =============================================================================
-- map_layers
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_map_layers_field
    ON public.map_layers USING btree (field);


-- =============================================================================
-- observations
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_observations_program_slug
    ON public.observations USING btree (program_slug);

CREATE INDEX IF NOT EXISTS idx_observations_jwst_pid
    ON public.observations USING btree (jwst_program_id);


-- =============================================================================
-- comments (additional)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_comments_content_trgm
    ON public.comments USING gin (content public.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_comments_created
    ON public.comments USING btree (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_comments_user
    ON public.comments USING btree (user_id);


-- =============================================================================
-- flag_audit_log (additional)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_audit_time
    ON public.flag_audit_log USING btree (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_user
    ON public.flag_audit_log USING btree (user_id);

-- =============================================================================
-- download_log (additional)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_download_log_download_type
    ON public.download_log USING btree (download_type);

CREATE INDEX IF NOT EXISTS idx_download_log_requested_at
    ON public.download_log USING btree (requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_download_log_user_id
    ON public.download_log USING btree (user_id);


-- =============================================================================
-- nircam_images
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_images_field
    ON public.nircam_images USING btree (field);

CREATE INDEX IF NOT EXISTS idx_images_filter
    ON public.nircam_images USING btree (filter);


-- =============================================================================
-- nircam_exposures
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_nircam_exposures_field_filter
    ON public.nircam_exposures USING btree (field, filter);

CREATE INDEX IF NOT EXISTS idx_nircam_exposures_review
    ON public.nircam_exposures USING btree (review_status)
    WHERE review_status != 'approved';


-- =============================================================================
-- spectrum_exposures (epic #210, B2)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_spectrum_exposures_spectrum_id
    ON public.spectrum_exposures USING btree (spectrum_id);

CREATE INDEX IF NOT EXISTS idx_spectrum_exposures_exposure_ref
    ON public.spectrum_exposures USING btree (exposure_ref);

CREATE INDEX IF NOT EXISTS idx_spectrum_exposures_review
    ON public.spectrum_exposures USING btree (review_status)
    WHERE review_status != 'approved';


-- =============================================================================
-- deploy_events (epic #210, B2/B3)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_deploy_events_occurred_at
    ON public.deploy_events USING btree (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_deploy_events_deployment_id
    ON public.deploy_events USING btree (deployment_id)
    WHERE deployment_id IS NOT NULL;

-- The admin audit log filters by observation (admin audit 2026-07-03, P4).
CREATE INDEX IF NOT EXISTS idx_deploy_events_observation
    ON public.deploy_events USING btree (observation)
    WHERE observation IS NOT NULL;


-- =============================================================================
-- storage_objects (epic #210, F1)
-- =============================================================================

-- One current object per (product_type, exposure_ref). Partial so superseded /
-- revoked tombstones don't collide with the live object — must be an index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_objects_product_exposure_active
    ON public.storage_objects USING btree (product_type, exposure_ref)
    WHERE status = 'active';

-- Copy/verify + budget walks scope by backend and status.
CREATE INDEX IF NOT EXISTS idx_storage_objects_backend_status
    ON public.storage_objects USING btree (backend, status);

-- Copy-verify and dedup are by content hash.
CREATE INDEX IF NOT EXISTS idx_storage_objects_content_hash
    ON public.storage_objects USING btree (content_hash);

-- Cascade a deployment to its objects (revoke/recover).
CREATE INDEX IF NOT EXISTS idx_storage_objects_deployment_id
    ON public.storage_objects USING btree (deployment_id);

-- Reconcile looks objects up by key; scope joins by observation / spectrum_id.
CREATE INDEX IF NOT EXISTS idx_storage_objects_storage_key
    ON public.storage_objects USING btree (storage_key);

CREATE INDEX IF NOT EXISTS idx_storage_objects_observation
    ON public.storage_objects USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_storage_objects_spectrum_id
    ON public.storage_objects USING btree (spectrum_id);

-- The admin registry browser's default sort is newest-first — the hot path on
-- the registry's ever-growing table (admin audit 2026-07-03, P4).
CREATE INDEX IF NOT EXISTS idx_storage_objects_created_at
    ON public.storage_objects USING btree (created_at DESC);


-- =============================================================================
-- access_codes
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_access_codes_code
    ON public.access_codes USING btree (code);


-- =============================================================================
-- inspection_access_requests
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_inspection_access_requests_status
    ON public.inspection_access_requests USING btree (status);

-- At most one open ('pending') request per user.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_inspection_access_requests_pending
    ON public.inspection_access_requests USING btree (user_id)
    WHERE (status = 'pending');


-- =============================================================================
-- api_keys
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_api_keys_is_active
    ON public.api_keys USING btree (is_active) WHERE (is_active = true);

CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash
    ON public.api_keys USING btree (key_hash);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id
    ON public.api_keys USING btree (user_id);


-- =============================================================================
-- code_redemptions
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_code_redemptions_user
    ON public.code_redemptions USING btree (user_id);


-- =============================================================================
-- device_codes
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_device_codes_device_code
    ON public.device_codes USING btree (device_code);

CREATE INDEX IF NOT EXISTS idx_device_codes_expires_at
    ON public.device_codes USING btree (expires_at);

CREATE INDEX IF NOT EXISTS idx_device_codes_status
    ON public.device_codes USING btree (status) WHERE (status = 'pending'::text);

CREATE INDEX IF NOT EXISTS idx_device_codes_user_code
    ON public.device_codes USING btree (user_code);


-- =============================================================================
-- password_reset_log
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_password_reset_log_reset_at
    ON public.password_reset_log USING btree (reset_at DESC);

CREATE INDEX IF NOT EXISTS idx_password_reset_log_user_id
    ON public.password_reset_log USING btree (user_id);


-- =============================================================================
-- pending_invites
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_pending_invites_email
    ON public.pending_invites USING btree (email);


-- =============================================================================
-- refresh_tokens
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active
    ON public.refresh_tokens USING btree (user_id, expires_at) WHERE (is_revoked = false);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token_hash
    ON public.refresh_tokens USING btree (token_hash);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id
    ON public.refresh_tokens USING btree (user_id);


-- =============================================================================
-- user_profiles
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_user_profiles_preferences
    ON public.user_profiles USING gin (preferences);


-- =============================================================================
-- user_program_access
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_user_program_access_user_slug
    ON public.user_program_access USING btree (user_id, program_slug);


-- =============================================================================
-- programs (access control)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_programs_is_public_slug
    ON public.programs USING btree (is_public, slug) WHERE (is_public = true);


-- NOTE: Materialized view indexes (mv_programs_overview, mv_filter_options)
-- are defined in views.sql alongside the view definitions.
