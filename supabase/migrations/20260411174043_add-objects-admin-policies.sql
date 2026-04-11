
  create policy "admin_objects_delete"
  on "public"."objects"
  as permissive
  for delete
  to authenticated
using (public.is_admin());



  create policy "admin_objects_insert"
  on "public"."objects"
  as permissive
  for insert
  to authenticated
with check (public.is_admin());



  create policy "admin_objects_update"
  on "public"."objects"
  as permissive
  for update
  to authenticated
using (public.is_admin())
with check (public.is_admin());



