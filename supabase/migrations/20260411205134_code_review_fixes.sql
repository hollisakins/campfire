drop policy "select_list_members" on "public"."object_list_members";

CREATE INDEX idx_list_members_list_id_object_id ON public.object_list_members USING btree (list_id, object_id) WHERE (object_id IS NOT NULL);


  create policy "update_list_members"
  on "public"."object_list_members"
  as permissive
  for update
  to authenticated
using ((public.can_comment() AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = auth.uid()) OR (object_lists.visibility = 'public_edit'::text))))))
with check ((public.can_comment() AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = auth.uid()) OR (object_lists.visibility = 'public_edit'::text))))));



  create policy "select_list_members"
  on "public"."object_list_members"
  as permissive
  for select
  to authenticated
using (((list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = auth.uid()) OR (object_lists.visibility = ANY (ARRAY['public_read'::text, 'public_edit'::text]))))) AND (((object_id IS NULL) AND (list_id IN ( SELECT object_lists.id
   FROM public.object_lists
  WHERE ((object_lists.created_by = auth.uid()) OR (object_lists.visibility = 'public_edit'::text))))) OR (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && public.accessible_program_slugs()))))));



