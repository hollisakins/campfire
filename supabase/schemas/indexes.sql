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

CREATE INDEX IF NOT EXISTS idx_targets_field
    ON public.targets USING btree (field);

CREATE INDEX IF NOT EXISTS idx_targets_field_observation
    ON public.targets USING btree (field, observation);

CREATE INDEX IF NOT EXISTS idx_targets_has_sed_plot
    ON public.targets USING btree (has_sed_plot) WHERE (has_sed_plot = true);

CREATE INDEX IF NOT EXISTS idx_targets_max_snr
    ON public.targets USING btree (max_snr) WHERE (max_snr IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_targets_max_exposure_time
    ON public.targets USING btree (max_exposure_time) WHERE (max_exposure_time IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_targets_target_id_trgm
    ON public.targets USING gin (target_id public.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug
    ON public.targets USING btree (program_slug);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug_field
    ON public.targets USING btree (program_slug, field);

CREATE INDEX IF NOT EXISTS idx_targets_program_slug_quality
    ON public.targets USING btree (program_slug, redshift_quality);

CREATE INDEX IF NOT EXISTS idx_targets_observation
    ON public.targets USING btree (observation);

CREATE INDEX IF NOT EXISTS idx_targets_updated_at
    ON public.targets USING btree (updated_at);


-- =============================================================================
-- spectra
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_spectra_target_id
    ON public.spectra USING btree (target_id) INCLUDE (grating, fits_path);

CREATE INDEX IF NOT EXISTS idx_spectra_target_grating
    ON public.spectra USING btree (target_id, grating);

CREATE UNIQUE INDEX IF NOT EXISTS idx_spectra_fits_path
    ON public.spectra USING btree (fits_path);

CREATE INDEX IF NOT EXISTS idx_spectra_file_hash
    ON public.spectra USING btree (file_hash) WHERE (file_hash IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_spectra_grating
    ON public.spectra USING btree (grating);


-- =============================================================================
-- comments
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_comments_target
    ON public.comments USING btree (target_id);


-- =============================================================================
-- flag_audit_log
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_audit_target
    ON public.flag_audit_log USING btree (target_id);


-- =============================================================================
-- download_log
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_download_log_target_ids
    ON public.download_log USING gin (target_ids);


-- =============================================================================
-- shutters
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_shutters_field
    ON public.shutters USING btree (field);

CREATE INDEX IF NOT EXISTS idx_shutters_observation
    ON public.shutters USING btree (observation);

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
-- targets (legacy index names from before objects → targets rename)
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_objects_redshift_generated
    ON public.targets USING btree (redshift) WHERE (redshift IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_objects_redshift_quality
    ON public.targets USING btree (redshift_quality);


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

CREATE INDEX IF NOT EXISTS idx_flag_audit_log_target_id
    ON public.flag_audit_log USING btree (target_id);


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
-- access_codes
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_access_codes_code
    ON public.access_codes USING btree (code);


-- =============================================================================
-- account_requests
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_account_requests_created_at
    ON public.account_requests USING btree (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_account_requests_email
    ON public.account_requests USING btree (email);

CREATE INDEX IF NOT EXISTS idx_account_requests_status
    ON public.account_requests USING btree (status);


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


-- NOTE: Materialized view indexes (mv_programs_overview, mv_filter_options)
-- are defined in views.sql alongside the view definitions.
