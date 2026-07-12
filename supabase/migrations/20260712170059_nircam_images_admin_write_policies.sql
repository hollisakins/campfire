-- nircam_images admin write policies (deploy RLS gap).
--
-- nircam_images had RLS enabled with only a SELECT policy. Since the deploy-auth
-- epic (#250) made `login` mode (which operates through RLS) the default deploy
-- path, the NIRCam mosaic deploy's _upsert_nircam_images (an upsert =
-- INSERT ... ON CONFLICT DO UPDATE) had no permissive INSERT/UPDATE policy to
-- satisfy and failed with 42501 "new row violates row-level security policy for
-- table nircam_images". Previously deploys ran under service_role (RLS bypassed),
-- so the gap was invisible. This mirrors the admin write policies already present
-- on nircam_exposures and fields. (Publish/revoke is unaffected: it flips
-- deploy_status via the SECURITY DEFINER set_deployment_status RPC, which
-- bypasses RLS.)

  create policy "admin_insert_nircam"
  on "public"."nircam_images"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));


  create policy "admin_update_nircam"
  on "public"."nircam_images"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));
