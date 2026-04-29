
  create policy "admin_spectra_delete"
  on "public"."spectra"
  as permissive
  for delete
  to authenticated
using (public.is_admin());



  create policy "admin_targets_delete"
  on "public"."targets"
  as permissive
  for delete
  to authenticated
using (public.is_admin());



