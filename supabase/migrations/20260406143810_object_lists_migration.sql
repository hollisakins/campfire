-- =============================================================================
-- Migration: Object Lists
-- =============================================================================
-- Replaces the object_flags bitmask column on targets with a flexible
-- object_lists + object_list_members system. This migration:
--   1. Creates new tables (object_lists, object_list_members, list_audit_log)
--   2. Seeds 9 system lists from existing OBJECT_FLAGS definitions
--   3. Migrates existing object_flags bitmask data to list memberships
--   4. Drops the object_flags column from targets
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Part 0: Drop old-signature functions (PostgreSQL treats different param
-- lists as different functions, so CREATE OR REPLACE won't remove the old
-- ones when signatures change)
-- ---------------------------------------------------------------------------

drop function if exists "public"."count_distinct_inspected_objects"(p_user_id uuid);

drop function if exists "public"."get_adjacent_targets"(p_current_target_id text, p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features integer, p_object_flags integer, p_dq_flags integer, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_objects"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_spectra"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_filtered_object_ids"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision);

drop function if exists "public"."get_filtered_objects_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer);

drop function if exists "public"."get_filtered_spectra_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_include_thumbnails boolean);

drop function if exists "public"."get_filtered_target_ids"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_updated_since timestamp without time zone);

drop function if exists "public"."get_filtered_targets_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features integer, p_object_flags integer, p_dq_flags integer, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_object_flags_include_any integer, p_object_flags_include_all integer, p_object_flags_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_include_thumbnails boolean, p_updated_since timestamp without time zone);


-- ---------------------------------------------------------------------------
-- Part A: Create new tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.object_lists (
    id integer NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    description text,
    visibility text DEFAULT 'private'::text NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    color text,
    icon text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT object_lists_visibility_check CHECK (visibility = ANY (ARRAY['private'::text, 'public_read'::text, 'public_edit'::text]))
);

ALTER TABLE public.object_lists OWNER TO postgres;

COMMENT ON TABLE public.object_lists IS 'User-created or system-seeded lists of objects. Visibility controls who can see and edit the list. System lists (is_system=true) are seeded at migration time and cannot be deleted by users.';

CREATE SEQUENCE IF NOT EXISTS public.object_lists_id_seq
    AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.object_lists_id_seq OWNER TO postgres;
ALTER SEQUENCE public.object_lists_id_seq OWNED BY public.object_lists.id;
ALTER TABLE ONLY public.object_lists ALTER COLUMN id SET DEFAULT nextval('public.object_lists_id_seq'::regclass);

ALTER TABLE ONLY public.object_lists ADD CONSTRAINT object_lists_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.object_lists ADD CONSTRAINT object_lists_slug_key UNIQUE (slug);
ALTER TABLE ONLY public.object_lists ADD CONSTRAINT object_lists_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id);


CREATE TABLE IF NOT EXISTS public.object_list_members (
    id integer NOT NULL,
    list_id integer NOT NULL,
    object_id integer,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    notes text,
    added_by uuid,
    added_at timestamp with time zone DEFAULT now()
);

ALTER TABLE public.object_list_members OWNER TO postgres;

COMMENT ON TABLE public.object_list_members IS 'Members of object lists. Coordinates (ra, dec) are the durable positional key; object_id is a fast query key that gets refreshed after each objects rebuild via coordinate cross-matching.';

CREATE SEQUENCE IF NOT EXISTS public.object_list_members_id_seq
    AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.object_list_members_id_seq OWNER TO postgres;
ALTER SEQUENCE public.object_list_members_id_seq OWNED BY public.object_list_members.id;
ALTER TABLE ONLY public.object_list_members ALTER COLUMN id SET DEFAULT nextval('public.object_list_members_id_seq'::regclass);

ALTER TABLE ONLY public.object_list_members ADD CONSTRAINT object_list_members_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.object_list_members ADD CONSTRAINT object_list_members_list_id_ra_dec_key UNIQUE (list_id, ra, dec);
ALTER TABLE ONLY public.object_list_members ADD CONSTRAINT object_list_members_list_id_fkey FOREIGN KEY (list_id) REFERENCES public.object_lists(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.object_list_members ADD CONSTRAINT object_list_members_object_id_fkey FOREIGN KEY (object_id) REFERENCES public.objects(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.object_list_members ADD CONSTRAINT object_list_members_added_by_fkey FOREIGN KEY (added_by) REFERENCES auth.users(id);


CREATE TABLE IF NOT EXISTS public.list_audit_log (
    id integer NOT NULL,
    list_id integer NOT NULL,
    object_id integer,
    user_id uuid,
    action text NOT NULL,
    ra double precision,
    dec double precision,
    changed_at timestamp with time zone DEFAULT now(),
    CONSTRAINT list_audit_log_action_check CHECK (action = ANY (ARRAY['add'::text, 'remove'::text]))
);

ALTER TABLE public.list_audit_log OWNER TO postgres;

CREATE SEQUENCE IF NOT EXISTS public.list_audit_log_id_seq
    AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.list_audit_log_id_seq OWNER TO postgres;
ALTER SEQUENCE public.list_audit_log_id_seq OWNED BY public.list_audit_log.id;
ALTER TABLE ONLY public.list_audit_log ALTER COLUMN id SET DEFAULT nextval('public.list_audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.list_audit_log ADD CONSTRAINT list_audit_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.list_audit_log ADD CONSTRAINT list_audit_log_list_id_fkey FOREIGN KEY (list_id) REFERENCES public.object_lists(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.list_audit_log ADD CONSTRAINT list_audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id);


-- Grants
GRANT ALL ON TABLE public.object_lists TO anon;
GRANT ALL ON TABLE public.object_lists TO authenticated;
GRANT ALL ON TABLE public.object_lists TO service_role;

GRANT ALL ON TABLE public.object_list_members TO anon;
GRANT ALL ON TABLE public.object_list_members TO authenticated;
GRANT ALL ON TABLE public.object_list_members TO service_role;

GRANT ALL ON TABLE public.list_audit_log TO anon;
GRANT ALL ON TABLE public.list_audit_log TO authenticated;
GRANT ALL ON TABLE public.list_audit_log TO service_role;

GRANT ALL ON SEQUENCE public.object_lists_id_seq TO anon;
GRANT ALL ON SEQUENCE public.object_lists_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.object_lists_id_seq TO service_role;

GRANT ALL ON SEQUENCE public.object_list_members_id_seq TO anon;
GRANT ALL ON SEQUENCE public.object_list_members_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.object_list_members_id_seq TO service_role;

GRANT ALL ON SEQUENCE public.list_audit_log_id_seq TO anon;
GRANT ALL ON SEQUENCE public.list_audit_log_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.list_audit_log_id_seq TO service_role;


-- ---------------------------------------------------------------------------
-- Part B: Seed system lists (with visual metadata from OBJECT_FLAGS)
-- ---------------------------------------------------------------------------

INSERT INTO public.object_lists (name, slug, description, visibility, is_system, color, icon)
VALUES
    ('Little Red Dots',       'lrd',                 'Little red dot candidates',                    'public_edit', true, '#ffcccb', '🔴'),
    ('Broad Line AGN',        'broad-line',          'Broad emission line sources',                  'public_edit', true, '#c8e6c9', '🌋'),
    ('Lyα Emitters',          'lya-emitter',         'Strong Lyman-alpha emitters',                  'public_edit', true, '#bbdefb', '✨'),
    ('Balmer Break Galaxies', 'balmer-break-galaxy',  'Strong Balmer break',                         'public_edit', true, '#e1bee7', '🌌'),
    ('[OIII] Emitters',       'oiii-emitter',         'Strong [OIII] emitters',                      'public_edit', true, '#fff59d', '⚡️'),
    ('Hα Emitters',           'ha-emitter',           'Strong H-alpha emitters',                     'public_edit', true, '#f398ad', '🔥'),
    ('Quiescent Galaxies',    'passive',              'Quiescent galaxy with little star formation',  'public_edit', true, '#d7ccc8', '😴'),
    ('Dusty Galaxies',        'dusty',                'Significant dust attenuation',                'public_edit', true, '#ffccbc', '🌫️'),
    ('Stars',                 'star',                 'Stellar spectra',                             'public_edit', true, '#ffeb3b', '⭐');


-- ---------------------------------------------------------------------------
-- Part C: Data migration — decode object_flags bitmask → list members
-- ---------------------------------------------------------------------------
-- For each of the 9 flag bits, insert a list member for every target that has
-- that bit set. Uses the parent object's coordinates as the durable key.
-- ON CONFLICT DO NOTHING handles multiple targets in the same object with
-- the same flag.
-- NOTE: Targets with object_id IS NULL are skipped (not yet clustered).

-- bit 0 (value 1): LRD
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'lrd'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 1) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 1 (value 2): Broad Line
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'broad-line'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 2) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 2 (value 4): Lyα Emitter
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'lya-emitter'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 4) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 3 (value 8): Balmer Break Galaxy
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'balmer-break-galaxy'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 8) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 4 (value 16): [OIII] Emitter
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'oiii-emitter'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 16) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 5 (value 32): Hα Emitter
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'ha-emitter'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 32) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 6 (value 64): Quiescent / Passive
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'passive'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 64) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 7 (value 128): Dusty
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'dusty'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 128) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;

-- bit 8 (value 256): Star
INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by)
SELECT (SELECT id FROM public.object_lists WHERE slug = 'star'),
       t.object_id, o.ra, o.dec, t.last_inspected_by
FROM public.targets t
JOIN public.objects o ON o.id = t.object_id
WHERE (t.object_flags & 256) != 0 AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Part D: Drop views that depend on object_flags, then drop the column
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS public.target_flag_summary;
DROP VIEW IF EXISTS public.targets_with_flags;

ALTER TABLE public.targets DROP COLUMN IF EXISTS object_flags;


-- ---------------------------------------------------------------------------
-- Part E: Indexes for new tables
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_object_lists_created_by ON public.object_lists USING btree (created_by);
CREATE INDEX IF NOT EXISTS idx_object_lists_visibility ON public.object_lists USING btree (visibility);

CREATE INDEX IF NOT EXISTS idx_list_members_object_id ON public.object_list_members USING btree (object_id) WHERE (object_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_list_members_list_id ON public.object_list_members USING btree (list_id);
CREATE INDEX IF NOT EXISTS idx_list_members_coords ON public.object_list_members USING btree (ra, dec);

CREATE INDEX IF NOT EXISTS idx_list_audit_list_id ON public.list_audit_log USING btree (list_id);
CREATE INDEX IF NOT EXISTS idx_list_audit_changed_at ON public.list_audit_log USING btree (changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_list_audit_user_id ON public.list_audit_log USING btree (user_id);


-- ---------------------------------------------------------------------------
-- Part F: RLS policies for new tables
-- ---------------------------------------------------------------------------

ALTER TABLE public.object_lists ENABLE ROW LEVEL SECURITY;

CREATE POLICY "select_lists" ON public.object_lists FOR SELECT TO authenticated
    USING (created_by = auth.uid() OR visibility IN ('public_read', 'public_edit'));

CREATE POLICY "insert_lists" ON public.object_lists FOR INSERT TO authenticated
    WITH CHECK (created_by = auth.uid() AND is_system = false);

CREATE POLICY "update_own_lists" ON public.object_lists FOR UPDATE TO authenticated
    USING (created_by = auth.uid() AND is_system = false)
    WITH CHECK (created_by = auth.uid() AND is_system = false);

CREATE POLICY "delete_own_lists" ON public.object_lists FOR DELETE TO authenticated
    USING (created_by = auth.uid() AND is_system = false);

CREATE POLICY "admin_manage_lists" ON public.object_lists
    USING (public.is_admin());


ALTER TABLE public.object_list_members ENABLE ROW LEVEL SECURITY;

CREATE POLICY "select_list_members" ON public.object_list_members FOR SELECT TO authenticated
    USING (
        list_id IN (
            SELECT id FROM public.object_lists
            WHERE created_by = auth.uid() OR visibility IN ('public_read', 'public_edit')
        )
        AND (
            (object_id IS NULL AND list_id IN (
                SELECT id FROM public.object_lists WHERE created_by = auth.uid()
            ))
            OR object_id IN (
                SELECT o.id FROM public.objects o
                WHERE o.programs && public.accessible_program_slugs()
            )
        )
    );

CREATE POLICY "insert_list_members" ON public.object_list_members FOR INSERT TO authenticated
    WITH CHECK (
        public.can_comment()
        AND list_id IN (
            SELECT id FROM public.object_lists
            WHERE created_by = auth.uid() OR visibility = 'public_edit'
        )
    );

CREATE POLICY "delete_list_members" ON public.object_list_members FOR DELETE TO authenticated
    USING (
        public.can_comment()
        AND list_id IN (
            SELECT id FROM public.object_lists
            WHERE created_by = auth.uid() OR visibility = 'public_edit'
        )
    );

CREATE POLICY "admin_manage_list_members" ON public.object_list_members
    USING (public.is_admin());


ALTER TABLE public.list_audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "select_list_audit" ON public.list_audit_log FOR SELECT TO authenticated
    USING (
        list_id IN (
            SELECT id FROM public.object_lists
            WHERE created_by = auth.uid() OR visibility IN ('public_read', 'public_edit')
        )
    );

CREATE POLICY "admin_select_list_audit" ON public.list_audit_log FOR SELECT TO authenticated
    USING (public.is_admin());


-- ---------------------------------------------------------------------------
-- Part G: Trigger for list audit logging
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_list_membership_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO public.list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER track_list_member_insert
  AFTER INSERT ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();

CREATE TRIGGER track_list_member_delete
  AFTER DELETE ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();


-- ---------------------------------------------------------------------------
-- Part H: Update log_flag_changes trigger (remove object_flags block)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_flag_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.spectral_features IS DISTINCT FROM NEW.spectral_features THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'spectral_features', OLD.spectral_features, NEW.spectral_features);
    END IF;
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- ---------------------------------------------------------------------------
-- Part I: Update views (remove object_flags references)
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS public.target_flag_summary;
CREATE VIEW public.target_flag_summary AS
SELECT
    t.id,
    t.target_id,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'spectral_features' AND (t.spectral_features & fd.value) > 0) AS spectral_features_labels,
    array_agg(DISTINCT fd.label) FILTER (WHERE fd.category = 'dq_flags' AND (t.dq_flags & fd.value) > 0) AS dq_flags_labels
FROM public.targets t
CROSS JOIN public.flag_definitions fd
GROUP BY t.id, t.target_id;

GRANT ALL ON TABLE public.target_flag_summary TO anon;
GRANT ALL ON TABLE public.target_flag_summary TO authenticated;
GRANT ALL ON TABLE public.target_flag_summary TO service_role;

DROP VIEW IF EXISTS public.targets_with_flags;
CREATE VIEW public.targets_with_flags AS
SELECT
    t.id,
    t.target_id,
    t.program_slug,
    t.field,
    t.ra,
    t.dec,
    t.redshift_auto AS redshift,
    t.redshift_quality,
    t.spectral_features,
    t.dq_flags,
    t.created_at,
    t.updated_at,
    rq.label AS redshift_quality_label,
    rq.icon AS redshift_quality_icon,
    rq.color AS redshift_quality_color
FROM public.targets t
LEFT JOIN public.flag_definitions rq ON (rq.category = 'redshift_quality' AND rq.value = t.redshift_quality);

GRANT ALL ON TABLE public.targets_with_flags TO anon;
GRANT ALL ON TABLE public.targets_with_flags TO authenticated;
GRANT ALL ON TABLE public.targets_with_flags TO service_role;
