drop policy "insert_comments_by_access" on "public"."comments";

drop policy "select_comments_by_access" on "public"."comments";

drop index if exists "public"."idx_comments_target";

alter table "public"."comments" add column "object_id" integer;

alter table "public"."comments" alter column "target_id" drop not null;

CREATE INDEX idx_comments_object_id ON public.comments USING btree (object_id) WHERE (object_id IS NOT NULL);

CREATE INDEX idx_comments_target ON public.comments USING btree (target_id) WHERE (target_id IS NOT NULL);

alter table "public"."comments" add constraint "comments_object_id_fkey" FOREIGN KEY (object_id) REFERENCES public.objects(id) ON DELETE CASCADE not valid;

alter table "public"."comments" validate constraint "comments_object_id_fkey";


  create policy "insert_comments_by_access"
  on "public"."comments"
  as permissive
  for insert
  to public
with check (((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs()))))) OR ((target_id IS NULL) AND (object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT array_agg(s.s) AS array_agg
           FROM unnest(public.accessible_program_slugs()) s(s))))))) AND public.can_comment()));



  create policy "select_comments_by_access"
  on "public"."comments"
  as permissive
  for select
  to public
using ((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs()))))) OR ((target_id IS NULL) AND (object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE (o.programs && ( SELECT array_agg(s.s) AS array_agg
           FROM unnest(public.accessible_program_slugs()) s(s))))))));



