-- P-1 security: close the anonymous read path and the PUBLIC-execute hole
-- (docs/design-data-releases-impl-findings.md §0.1-0.2; plan §1 PR 3).
--
-- Before: 22 policies had no TO clause (so they applied to the anon role too),
-- anon held GRANT ALL on every public table and sequence, the schema's default
-- privileges re-granted anon on everything new, and every function was
-- executable by PUBLIC (Postgres' default), so the public anon key could read
-- 32k objects / 67k spectra / 43k audit rows and call SECURITY DEFINER
-- maintenance and token functions through /rest/v1/rpc.
--
-- After: every catalog policy is TO authenticated; anon keeps USAGE on the
-- schema (PostgREST needs it to answer at all) and nothing else; no function
-- is executable by PUBLIC or anon; new functions default to no PUBLIC execute.
-- The web app has no path that reads catalog tables as anon
-- (web/app/api/programs/route.ts expects zero rows signed out); auth flows use
-- the auth schema and the service role. Rollback = the mirror-image GRANTs.
--
-- pg-delta generated the function REVOKEs and the ALTER POLICY statements; the
-- relation revokes, the default-privilege changes and six long-signature RPCs
-- it did not emit are hand-authored below the generated block (pg-delta does
-- not model relation privileges or default privileges; keep tables.sql in step
-- by hand for those).
SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

REVOKE ALL ON FUNCTION "public"."accessible_program_slugs"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."apply_object_reconciliation"(text, jsonb, jsonb, jsonb, integer[], timestamp WITH time zone) FROM "anon";

REVOKE ALL ON FUNCTION "public"."authorize_device_code"(text, uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."bulk_set_target_object_fks"(jsonb, timestamp WITH time zone) FROM "anon";

REVOKE ALL ON FUNCTION "public"."bump_object_version"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."bump_spectra_updated_at"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."can_comment"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."can_inspect"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."check_device_code_status"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."claim_deploy_scope"(text, text, integer, uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."cleanup_expired_device_codes"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."cleanup_expired_refresh_tokens"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."compute_object_redshift_auto"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."consume_device_code"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."count_distinct_inspected_objects"(uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."deny_device_code"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."enforce_object_user_update_scope"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."enforce_profile_role_update_scope"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."enforce_spectra_dq_user_update_scope"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."filter_accessible_storage_keys"(text[], text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."filter_link_storage_keys"(text[], text[], text, text, boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."fitsgl_dataset_is_public"(text, text[], text[], text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_activity_users"() FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision, text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_deploy_events"(text, text, text, text, text, integer, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_exposure_facets"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_exposure_neighbors"(integer, text, text, text, text, text, text, text, text, integer, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_exposures"(text, text, text, text, text, text, text, text, integer, integer, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_storage_facets"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_admin_storage_objects"(text, text, text, text, text, text, text, integer, integer, text) FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer, integer)
  FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_database_overview"(boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_deploy_scope_version"(text, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_download_stats"(integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_field_object_markers"(text, boolean, text, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_field_shutters"(text, integer, integer, double precision, double precision, double precision, double precision) FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[])
  FROM "anon";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[])
  FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_lifecycle_status"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_line_fits_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_lists_for_sync"(uuid, boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_nearby_shutters"(double precision, double precision, double precision, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_nircam_field_summary"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_nircam_fields"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_objects_for_sync"(text[], uuid, timestamp WITH time zone, integer, boolean, boolean, text, text[]) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_objects_near"(double precision, double precision, double precision, text[], integer, text, boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_observation_manifest"(text, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_photometry_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, integer) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_program_stats"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_programs_overview"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_spectra_for_sync"(text[], uuid, timestamp WITH time zone, integer, boolean, boolean, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_storage_budget"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_storage_objects_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, bigint, text[], text[], text[]) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_targets_in_viewport"(double precision, double precision, double precision, double precision, text, integer, boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."get_user_profile_stats"(uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."handle_new_user"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."increment_tile_version"(text, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."is_admin"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."is_group_account"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."is_link_account"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."journal_sync_deletions"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."line_fit_stale_redshift"(integer, integer, double precision, integer, integer, double precision) FROM "anon";

REVOKE ALL ON FUNCTION "public"."link_allows_download"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."link_field"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."link_observation"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."link_sees_drafts"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."log_deploy_event"(text, uuid, integer, text, text, integer, jsonb, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."log_list_membership_change"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."log_object_inspection_changes"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."log_spectrum_dq_changes"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."member_editable_list_ids"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."object_band_values"(text, double precision, double precision, double precision, double precision, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."object_line_snr"(integer, text, boolean, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."object_scoped_aggregates"(integer, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."objects_matching_band_filter"(text, double precision, double precision, double precision, double precision, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."objects_matching_grating_filter"(text[], text, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."objects_matching_line_filter"(text, double precision, double precision, boolean, text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."objects_matching_observation_filter"(text[], text[], boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."pin_redshift_on_signoff"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."propagate_target_publication_to_shutters"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."propagate_target_scope_to_spectra"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."recompute_has_published_spectrum"(text[], text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."recompute_object_search_text"(integer[]) FROM "anon";

REVOKE ALL ON FUNCTION "public"."recompute_target_aggregates"(text[]) FROM "anon";

REVOKE ALL ON FUNCTION "public"."relink_list_members_for_field"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."relink_photometry_for_field"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."revoke_all_user_refresh_tokens"(uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."revoke_refresh_token"(uuid, uuid) FROM "anon";

REVOKE ALL ON FUNCTION "public"."rotate_refresh_token"(text, text, timestamp WITH time zone, text, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."set_deployment_status"(integer, text, uuid, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."set_spectra_deploy_status"(integer[], text, text, uuid, integer, text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."spectra_matching_line_filter"(text, double precision, double precision, boolean) FROM "anon";

REVOKE ALL ON FUNCTION "public"."sync_object_photometry_bands"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."sync_photometry_to_objects"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."sync_shutter_publication"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."sync_spectra_target_scope"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."sync_spectrum_lines"() FROM "anon";

REVOKE ALL ON FUNCTION "public"."update_api_key_last_used"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."validate_api_key"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."validate_refresh_token"(text) FROM "anon";

REVOKE ALL ON FUNCTION "public"."viewable_list_ids"() FROM "anon";

ALTER POLICY "Users can read own redeemed codes" ON "public"."access_codes" TO "authenticated";

ALTER POLICY "admin_manage_codes" ON "public"."access_codes" TO "authenticated";

ALTER POLICY "Users can redeem codes" ON "public"."code_redemptions" TO "authenticated";

ALTER POLICY "Users can see own redemptions" ON "public"."code_redemptions" TO "authenticated";

ALTER POLICY "admin_select_redemptions" ON "public"."code_redemptions" TO "authenticated";

ALTER POLICY "insert_comments_by_access" ON "public"."comments" TO "authenticated";

ALTER POLICY "select_comments_by_access" ON "public"."comments" TO "authenticated";

ALTER POLICY "select_audit_by_access" ON "public"."flag_audit_log" TO "authenticated";

ALTER POLICY "admin_manage_list_members" ON "public"."object_list_members" TO "authenticated";

ALTER POLICY "admin_manage_list_shares" ON "public"."object_list_shares" TO "authenticated";

ALTER POLICY "admin_manage_lists" ON "public"."object_lists" TO "authenticated";

ALTER POLICY "select_object_photometry_by_access" ON "public"."object_photometry" TO "authenticated";

ALTER POLICY "select_object_photometry_bands_by_parent" ON "public"."object_photometry_bands" TO "authenticated";

ALTER POLICY "select_objects_by_access" ON "public"."objects" TO "authenticated";

ALTER POLICY "update_objects_by_access" ON "public"."objects" TO "authenticated";

ALTER POLICY "Users can view own reset logs" ON "public"."password_reset_log" TO "authenticated";

ALTER POLICY "admin_select_reset_logs" ON "public"."password_reset_log" TO "authenticated";

ALTER POLICY "select_spectra_by_access" ON "public"."spectra" TO "authenticated";

ALTER POLICY "select_spectrum_line_fits_by_spectrum" ON "public"."spectrum_line_fits" TO "authenticated";

ALTER POLICY "select_spectrum_lines_by_spectrum" ON "public"."spectrum_lines" TO "authenticated";

ALTER POLICY "select_targets_by_access" ON "public"."targets" TO "authenticated";

ALTER POLICY "update_targets_by_access" ON "public"."targets" TO "authenticated";

REVOKE ALL ON FUNCTION "public"."accessible_program_slugs"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."apply_object_reconciliation"(text, jsonb, jsonb, jsonb, integer[], timestamp WITH time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."authorize_device_code"(text, uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."bulk_set_target_object_fks"(jsonb, timestamp WITH time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."bump_object_version"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."bump_spectra_updated_at"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."can_comment"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."can_inspect"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."check_device_code_status"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."claim_deploy_scope"(text, text, integer, uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."cleanup_expired_device_codes"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."cleanup_expired_refresh_tokens"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."compute_object_redshift_auto"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."consume_device_code"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."count_distinct_inspected_objects"(uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."deny_device_code"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."enforce_object_user_update_scope"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."enforce_profile_role_update_scope"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."enforce_spectra_dq_user_update_scope"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."filter_accessible_storage_keys"(text[], text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."filter_link_storage_keys"(text[], text[], text, text, boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."fitsgl_dataset_is_public"(text, text[], text[], text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_activity_users"() FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision, text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_deploy_events"(text, text, text, text, text, integer, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_exposure_facets"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_exposure_neighbors"(integer, text, text, text, text, text, text, text, text, integer, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_exposures"(text, text, text, text, text, text, text, text, integer, integer, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_storage_facets"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_admin_storage_objects"(text, text, text, text, text, text, text, integer, integer, text) FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer, integer)
  FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_database_overview"(boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_deploy_scope_version"(text, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_download_stats"(integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_field_object_markers"(text, boolean, text, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_field_shutters"(text, integer, integer, double precision, double precision, double precision, double precision) FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[])
  FROM PUBLIC;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[])
  FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_lifecycle_status"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_line_fits_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_lists_for_sync"(uuid, boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_nearby_shutters"(double precision, double precision, double precision, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_nircam_field_summary"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_nircam_fields"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_objects_for_sync"(text[], uuid, timestamp WITH time zone, integer, boolean, boolean, text, text[]) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_objects_near"(double precision, double precision, double precision, text[], integer, text, boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_observation_manifest"(text, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_photometry_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_program_stats"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_programs_overview"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_spectra_for_sync"(text[], uuid, timestamp WITH time zone, integer, boolean, boolean, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_storage_budget"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_storage_objects_for_sync"(text[], timestamp WITH time zone, integer, boolean, boolean, bigint, text[], text[], text[]) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_targets_in_viewport"(double precision, double precision, double precision, double precision, text, integer, boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."get_user_profile_stats"(uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."handle_new_user"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."handle_new_user"() FROM "supabase_auth_admin";

GRANT EXECUTE ON FUNCTION "public"."handle_new_user"() TO "supabase_auth_admin";

REVOKE ALL ON FUNCTION "public"."increment_tile_version"(text, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."is_admin"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."is_group_account"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."is_link_account"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."journal_sync_deletions"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."line_fit_stale_redshift"(integer, integer, double precision, integer, integer, double precision) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."link_allows_download"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."link_field"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."link_observation"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."link_sees_drafts"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."log_deploy_event"(text, uuid, integer, text, text, integer, jsonb, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."log_list_membership_change"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."log_object_inspection_changes"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."log_spectrum_dq_changes"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."member_editable_list_ids"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."object_band_values"(text, double precision, double precision, double precision, double precision, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."object_line_snr"(integer, text, boolean, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."object_scoped_aggregates"(integer, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."objects_matching_band_filter"(text, double precision, double precision, double precision, double precision, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."objects_matching_grating_filter"(text[], text, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."objects_matching_line_filter"(text, double precision, double precision, boolean, text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."objects_matching_observation_filter"(text[], text[], boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."pin_redshift_on_signoff"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."propagate_target_publication_to_shutters"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."propagate_target_scope_to_spectra"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."recompute_has_published_spectrum"(text[], text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."recompute_object_search_text"(integer[]) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."recompute_target_aggregates"(text[]) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."relink_list_members_for_field"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."relink_photometry_for_field"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."revoke_all_user_refresh_tokens"(uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."revoke_refresh_token"(uuid, uuid) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."rotate_refresh_token"(text, text, timestamp WITH time zone, text, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."set_deployment_status"(integer, text, uuid, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."set_spectra_deploy_status"(integer[], text, text, uuid, integer, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."spectra_matching_line_filter"(text, double precision, double precision, boolean) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."sync_object_photometry_bands"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."sync_photometry_to_objects"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."sync_shutter_publication"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."sync_spectra_target_scope"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."sync_spectrum_lines"() FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."update_api_key_last_used"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."validate_api_key"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."validate_refresh_token"(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION "public"."viewable_list_ids"() FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Hand-authored part (not modelled by pg-delta)
-- ---------------------------------------------------------------------------

-- 1. Relations: anon loses every table, view, matview and sequence privilege in
--    public (52 explicit GRANT ALL lines in the baseline, plus the matviews and
--    views). Schema USAGE stays.
REVOKE ALL ON ALL TABLES IN SCHEMA "public" FROM "anon";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA "public" FROM "anon";

-- 2. Default privileges for objects postgres creates from now on: no anon, and
--    no PUBLIC execute on functions.
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON TABLES FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON SEQUENCES FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON FUNCTIONS FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- 3. The six catalog RPCs pg-delta did not emit a revoke for (long signatures
--    with defaults; the schema files carry the same lines).
REVOKE ALL ON FUNCTION "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double precision, double precision, text, boolean, boolean, integer[], text, double precision, double precision, double precision, text, text, boolean, double precision, double precision, text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision) FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision, double precision, text, boolean, boolean, integer[], text, double precision, double precision, double precision, boolean, double precision, double precision, text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer) FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double precision, double precision, integer, integer, integer, integer[], text, text, boolean, boolean, boolean, text, text, uuid, double precision, double precision, double precision, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer, integer) FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double precision, double precision, text, boolean, boolean, integer[], text, double precision, double precision, double precision, boolean, double precision, double precision, text, text, uuid, text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision) FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double precision, double precision, text, boolean, boolean, integer[], text, double precision, double precision, double precision, boolean, double precision, double precision, text, text, uuid, text, text, integer, integer, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[]) FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double precision, double precision, integer, integer, integer, integer[], text, text, boolean, boolean, boolean, text, text, uuid, double precision, double precision, double precision, text, text, integer, integer, boolean, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[]) FROM PUBLIC, "anon";

-- 4. Two sequences outside the authenticated-grant pattern (service-role-only tables).
REVOKE ALL ON SEQUENCE "public"."sync_deletions_id_seq" FROM "anon";
REVOKE ALL ON SEQUENCE "public"."sync_snapshots_id_seq" FROM "anon";

-- 5. Schema-less default privileges for postgres (set at project creation) also
--    grant anon; Postgres merges global and per-schema defaults.
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" REVOKE ALL ON TABLES FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" REVOKE ALL ON SEQUENCES FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" REVOKE ALL ON FUNCTIONS FROM "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
