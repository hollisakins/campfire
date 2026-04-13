alter table "public"."comments" drop constraint "comments_exactly_one_parent";

alter table "public"."comments" drop constraint "comments_object_id_fkey";

alter table "public"."comments" add constraint "comments_at_most_one_parent" CHECK ((num_nonnulls(target_id, object_id) <= 1)) not valid;

alter table "public"."comments" validate constraint "comments_at_most_one_parent";

alter table "public"."comments" add constraint "comments_object_id_fkey" FOREIGN KEY (object_id) REFERENCES public.objects(id) ON DELETE SET NULL not valid;

alter table "public"."comments" validate constraint "comments_object_id_fkey";


