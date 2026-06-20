drop policy "admin_manage_codes" on "public"."access_codes";

drop policy "admin_select_requests" on "public"."account_requests";

drop policy "admin_update_requests" on "public"."account_requests";

drop policy "Users can create own API keys" on "public"."api_keys";

drop policy "Users can delete own API keys" on "public"."api_keys";

drop policy "Users can update own API keys" on "public"."api_keys";

drop policy "Users can view own API keys" on "public"."api_keys";

drop policy "Users can redeem codes" on "public"."code_redemptions";

drop policy "Users can see own redemptions" on "public"."code_redemptions";

drop policy "admin_select_redemptions" on "public"."code_redemptions";

drop policy "insert_comments_by_access" on "public"."comments";

drop policy "select_comments_by_access" on "public"."comments";

drop policy "admin_deployments_insert" on "public"."deployments";

drop policy "Users can view own downloads" on "public"."download_log";

drop policy "admin_select_downloads" on "public"."download_log";

drop policy "insert_audit_by_access" on "public"."flag_audit_log";

drop policy "select_audit_by_access" on "public"."flag_audit_log";

drop policy "admin_select_list_audit" on "public"."list_audit_log";

drop policy "select_list_audit" on "public"."list_audit_log";

drop policy "admin_map_layers_all" on "public"."map_layers";

drop policy "admin_insert_exposures" on "public"."nircam_exposures";

drop policy "admin_select_exposures" on "public"."nircam_exposures";

drop policy "admin_update_exposures" on "public"."nircam_exposures";

drop policy "admin_manage_list_members" on "public"."object_list_members";

drop policy "delete_list_members" on "public"."object_list_members";

drop policy "insert_list_members" on "public"."object_list_members";

drop policy "select_list_members" on "public"."object_list_members";

drop policy "update_list_members" on "public"."object_list_members";

drop policy "admin_manage_lists" on "public"."object_lists";

drop policy "delete_own_lists" on "public"."object_lists";

drop policy "insert_lists" on "public"."object_lists";

drop policy "select_lists" on "public"."object_lists";

drop policy "update_own_lists" on "public"."object_lists";

drop policy "admin_object_photometry_delete" on "public"."object_photometry";

drop policy "admin_object_photometry_insert" on "public"."object_photometry";

drop policy "admin_object_photometry_update" on "public"."object_photometry";

drop policy "select_object_photometry_by_access" on "public"."object_photometry";

drop policy "admin_objects_delete" on "public"."objects";

drop policy "admin_objects_insert" on "public"."objects";

drop policy "admin_objects_update" on "public"."objects";

drop policy "select_objects_by_access" on "public"."objects";

drop policy "update_objects_by_access" on "public"."objects";

drop policy "accessible_observations_select" on "public"."observations";

drop policy "admin_observations_insert" on "public"."observations";

drop policy "admin_observations_update" on "public"."observations";

drop policy "Users can view own reset logs" on "public"."password_reset_log";

drop policy "admin_select_reset_logs" on "public"."password_reset_log";

drop policy "Users can read own invite by email" on "public"."pending_invites";

drop policy "admin_delete_invites" on "public"."pending_invites";

drop policy "admin_insert_invites" on "public"."pending_invites";

drop policy "admin_select_invites" on "public"."pending_invites";

drop policy "admin_update_invites" on "public"."pending_invites";

drop policy "accessible_programs_select" on "public"."programs";

drop policy "admin_programs_insert" on "public"."programs";

drop policy "admin_programs_select" on "public"."programs";

drop policy "admin_programs_update" on "public"."programs";

drop policy "Users can update own tokens" on "public"."refresh_tokens";

drop policy "Users can view own tokens" on "public"."refresh_tokens";

drop policy "admin_shutters_delete" on "public"."shutters";

drop policy "admin_shutters_insert" on "public"."shutters";

drop policy "admin_slit_regions_delete" on "public"."slit_regions";

drop policy "admin_slit_regions_insert" on "public"."slit_regions";

drop policy "admin_spectra_delete" on "public"."spectra";

drop policy "admin_spectra_insert" on "public"."spectra";

drop policy "admin_spectra_update" on "public"."spectra";

drop policy "select_spectra_by_access" on "public"."spectra";

drop policy "update_spectra_dq_by_access" on "public"."spectra";

drop policy "admin_targets_delete" on "public"."targets";

drop policy "admin_targets_insert" on "public"."targets";

drop policy "admin_targets_update" on "public"."targets";

drop policy "select_targets_by_access" on "public"."targets";

drop policy "update_targets_by_access" on "public"."targets";

drop policy "admin_delete_profile" on "public"."user_profiles";

drop policy "admin_insert_profile" on "public"."user_profiles";

drop policy "admin_update_profile" on "public"."user_profiles";

drop policy "self_update_profile" on "public"."user_profiles";

drop policy "admin_delete_access" on "public"."user_program_access";

drop policy "admin_insert_access" on "public"."user_program_access";

drop policy "admin_select_access" on "public"."user_program_access";

drop policy "self_select_access" on "public"."user_program_access";


  create policy "admin_manage_codes"
  on "public"."access_codes"
  as permissive
  for all
  to public
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_select_requests"
  on "public"."account_requests"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_update_requests"
  on "public"."account_requests"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "Users can create own API keys"
  on "public"."api_keys"
  as permissive
  for insert
  to authenticated
with check ((( SELECT auth.uid() AS uid) = user_id));


  create policy "Users can delete own API keys"
  on "public"."api_keys"
  as permissive
  for delete
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id));


  create policy "Users can update own API keys"
  on "public"."api_keys"
  as permissive
  for update
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id))
with check ((( SELECT auth.uid() AS uid) = user_id));


  create policy "Users can view own API keys"
  on "public"."api_keys"
  as permissive
  for select
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id));


  create policy "Users can redeem codes"
  on "public"."code_redemptions"
  as permissive
  for insert
  to public
with check ((user_id = ( SELECT auth.uid() AS uid)));


  create policy "Users can see own redemptions"
  on "public"."code_redemptions"
  as permissive
  for select
  to public
using ((user_id = ( SELECT auth.uid() AS uid)));


  create policy "admin_select_redemptions"
  on "public"."code_redemptions"
  as permissive
  for select
  to public
using (( SELECT public.is_admin() AS is_admin));


  create policy "insert_comments_by_access"
  on "public"."comments"
  as permissive
  for insert
  to public
with check (((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))) OR ((target_id IS NULL) AND (object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)))))) AND ( SELECT public.can_comment() AS can_comment)));


  create policy "select_comments_by_access"
  on "public"."comments"
  as permissive
  for select
  to public
using ((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))) OR ((target_id IS NULL) AND (object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)))))));


  create policy "admin_deployments_insert"
  on "public"."deployments"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "Users can view own downloads"
  on "public"."download_log"
  as permissive
  for select
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id));


  create policy "admin_select_downloads"
  on "public"."download_log"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "insert_audit_by_access"
  on "public"."flag_audit_log"
  as permissive
  for insert
  to authenticated
with check ((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))) OR ((object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs))))) OR ((spectrum_id IS NOT NULL) AND (spectrum_id IN ( SELECT s.id
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))))));


  create policy "select_audit_by_access"
  on "public"."flag_audit_log"
  as permissive
  for select
  to public
using ((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))) OR ((object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs))))) OR ((spectrum_id IS NOT NULL) AND (spectrum_id IN ( SELECT s.id
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))))));


  create policy "admin_select_list_audit"
  on "public"."list_audit_log"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "select_list_audit"
  on "public"."list_audit_log"
  as permissive
  for select
  to authenticated
using ((list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = ANY (ARRAY['public_read'::text, 'public_edit'::text]))))));


  create policy "admin_map_layers_all"
  on "public"."map_layers"
  as permissive
  for all
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_insert_exposures"
  on "public"."nircam_exposures"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_select_exposures"
  on "public"."nircam_exposures"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_update_exposures"
  on "public"."nircam_exposures"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_manage_list_members"
  on "public"."object_list_members"
  as permissive
  for all
  to public
using (( SELECT public.is_admin() AS is_admin));


  create policy "delete_list_members"
  on "public"."object_list_members"
  as permissive
  for delete
  to authenticated
using ((( SELECT public.can_comment() AS can_comment) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = 'public_edit'::text))))));


  create policy "insert_list_members"
  on "public"."object_list_members"
  as permissive
  for insert
  to authenticated
with check ((( SELECT public.can_comment() AS can_comment) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = 'public_edit'::text))))));


  create policy "select_list_members"
  on "public"."object_list_members"
  as permissive
  for select
  to authenticated
using (((list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = ANY (ARRAY['public_read'::text, 'public_edit'::text]))))) AND (((object_id IS NULL) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = 'public_edit'::text))))) OR (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)))))));


  create policy "update_list_members"
  on "public"."object_list_members"
  as permissive
  for update
  to authenticated
using ((( SELECT public.can_comment() AS can_comment) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = 'public_edit'::text))))))
with check ((( SELECT public.can_comment() AS can_comment) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = ( SELECT auth.uid() AS uid)) OR (object_lists.visibility = 'public_edit'::text))))));


  create policy "admin_manage_lists"
  on "public"."object_lists"
  as permissive
  for all
  to public
using (( SELECT public.is_admin() AS is_admin));


  create policy "delete_own_lists"
  on "public"."object_lists"
  as permissive
  for delete
  to authenticated
using (((created_by = ( SELECT auth.uid() AS uid)) AND (is_system = false)));


  create policy "insert_lists"
  on "public"."object_lists"
  as permissive
  for insert
  to authenticated
with check (((created_by = ( SELECT auth.uid() AS uid)) AND (is_system = false) AND ( SELECT public.can_comment() AS can_comment) AND (NOT ( SELECT public.is_group_account() AS is_group_account))));


  create policy "select_lists"
  on "public"."object_lists"
  as permissive
  for select
  to authenticated
using (((created_by = ( SELECT auth.uid() AS uid)) OR (visibility = ANY (ARRAY['public_read'::text, 'public_edit'::text]))));


  create policy "update_own_lists"
  on "public"."object_lists"
  as permissive
  for update
  to authenticated
using (((created_by = ( SELECT auth.uid() AS uid)) AND (is_system = false)))
with check (((created_by = ( SELECT auth.uid() AS uid)) AND (is_system = false)));


  create policy "admin_object_photometry_delete"
  on "public"."object_photometry"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_object_photometry_insert"
  on "public"."object_photometry"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_object_photometry_update"
  on "public"."object_photometry"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "select_object_photometry_by_access"
  on "public"."object_photometry"
  as permissive
  for select
  to public
using ((object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)))));


  create policy "admin_objects_delete"
  on "public"."objects"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_objects_insert"
  on "public"."objects"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_objects_update"
  on "public"."objects"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "select_objects_by_access"
  on "public"."objects"
  as permissive
  for select
  to public
using ((programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)));


  create policy "update_objects_by_access"
  on "public"."objects"
  as permissive
  for update
  to public
using (((programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND ( SELECT public.can_comment() AS can_comment)))
with check (((programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND ( SELECT public.can_comment() AS can_comment)));


  create policy "accessible_observations_select"
  on "public"."observations"
  as permissive
  for select
  to authenticated
using ((program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])));


  create policy "admin_observations_insert"
  on "public"."observations"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_observations_update"
  on "public"."observations"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "Users can view own reset logs"
  on "public"."password_reset_log"
  as permissive
  for select
  to public
using ((user_id = ( SELECT auth.uid() AS uid)));


  create policy "admin_select_reset_logs"
  on "public"."password_reset_log"
  as permissive
  for select
  to public
using (( SELECT public.is_admin() AS is_admin));


  create policy "Users can read own invite by email"
  on "public"."pending_invites"
  as permissive
  for select
  to authenticated
using ((email = (( SELECT users.email
   FROM auth.users
  WHERE (users.id = ( SELECT auth.uid() AS uid))))::text));


  create policy "admin_delete_invites"
  on "public"."pending_invites"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_insert_invites"
  on "public"."pending_invites"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_select_invites"
  on "public"."pending_invites"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_update_invites"
  on "public"."pending_invites"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "accessible_programs_select"
  on "public"."programs"
  as permissive
  for select
  to authenticated
using (((is_public = true) OR (slug IN ( SELECT user_program_access.program_slug
   FROM public.user_program_access
  WHERE (user_program_access.user_id = ( SELECT auth.uid() AS uid))))));


  create policy "admin_programs_insert"
  on "public"."programs"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_programs_select"
  on "public"."programs"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_programs_update"
  on "public"."programs"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "Users can update own tokens"
  on "public"."refresh_tokens"
  as permissive
  for update
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id))
with check ((( SELECT auth.uid() AS uid) = user_id));


  create policy "Users can view own tokens"
  on "public"."refresh_tokens"
  as permissive
  for select
  to authenticated
using ((( SELECT auth.uid() AS uid) = user_id));


  create policy "admin_shutters_delete"
  on "public"."shutters"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_shutters_insert"
  on "public"."shutters"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_slit_regions_delete"
  on "public"."slit_regions"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_slit_regions_insert"
  on "public"."slit_regions"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_spectra_delete"
  on "public"."spectra"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_spectra_insert"
  on "public"."spectra"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_spectra_update"
  on "public"."spectra"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "select_spectra_by_access"
  on "public"."spectra"
  as permissive
  for select
  to public
using ((target_id IN ( SELECT t.target_id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))));


  create policy "update_spectra_dq_by_access"
  on "public"."spectra"
  as permissive
  for update
  to authenticated
using ((( SELECT public.can_comment() AS can_comment) AND (target_id IN ( SELECT t.target_id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))))
with check ((( SELECT public.can_comment() AS can_comment) AND (target_id IN ( SELECT t.target_id
   FROM public.targets t
  WHERE (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))));


  create policy "admin_targets_delete"
  on "public"."targets"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_targets_insert"
  on "public"."targets"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_targets_update"
  on "public"."targets"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "select_targets_by_access"
  on "public"."targets"
  as permissive
  for select
  to public
using ((program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])));


  create policy "update_targets_by_access"
  on "public"."targets"
  as permissive
  for update
  to public
using (((program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND ( SELECT public.can_comment() AS can_comment)));


  create policy "admin_delete_profile"
  on "public"."user_profiles"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_insert_profile"
  on "public"."user_profiles"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_update_profile"
  on "public"."user_profiles"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));


  create policy "self_update_profile"
  on "public"."user_profiles"
  as permissive
  for update
  to authenticated
using ((user_id = ( SELECT auth.uid() AS uid)))
with check ((user_id = ( SELECT auth.uid() AS uid)));


  create policy "admin_delete_access"
  on "public"."user_program_access"
  as permissive
  for delete
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "admin_insert_access"
  on "public"."user_program_access"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_select_access"
  on "public"."user_program_access"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));


  create policy "self_select_access"
  on "public"."user_program_access"
  as permissive
  for select
  to authenticated
using ((user_id = ( SELECT auth.uid() AS uid)));

