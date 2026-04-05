-- Multi-user deployment: deployments table, provenance columns, admin RLS policies
-- Generated for branch: claude/multi-user-reduction-setup-lxkNA

-- =============================================================================
-- 1. New columns on spectra
-- =============================================================================

ALTER TABLE "public"."spectra"
  ADD COLUMN IF NOT EXISTS "crds_context" text,
  ADD COLUMN IF NOT EXISTS "jwst_version" text,
  ADD COLUMN IF NOT EXISTS "cfpipe_version" text;


-- =============================================================================
-- 2. Deployments table
-- =============================================================================

CREATE TABLE IF NOT EXISTS "public"."deployments" (
    "id" integer NOT NULL,
    "observation" text NOT NULL,
    "deployed_by" uuid NOT NULL,
    "deployed_at" timestamptz DEFAULT now(),
    "cfpipe_version" text,
    "jwst_version" text,
    "crds_context" text,
    "reduction_version" text,
    "config_snapshot" jsonb,
    "n_targets" integer,
    "n_spectra" integer,
    "n_new_targets" integer,
    "force_overwrite" boolean DEFAULT false,
    "source_ids_filter" integer[],
    "supabase_only" boolean DEFAULT false
);

ALTER TABLE "public"."deployments" OWNER TO "postgres";

CREATE SEQUENCE IF NOT EXISTS "public"."deployments_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "public"."deployments_id_seq" OWNED BY "public"."deployments"."id";

ALTER TABLE ONLY "public"."deployments"
  ALTER COLUMN "id" SET DEFAULT nextval('"public"."deployments_id_seq"'::regclass);

ALTER TABLE ONLY "public"."deployments"
  ADD CONSTRAINT "deployments_pkey" PRIMARY KEY ("id");

-- Grants (match existing table grant pattern)
GRANT ALL ON TABLE "public"."deployments" TO "anon";
GRANT ALL ON TABLE "public"."deployments" TO "authenticated";
GRANT ALL ON TABLE "public"."deployments" TO "service_role";

GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "service_role";


-- =============================================================================
-- 3. New columns on observations
-- =============================================================================

ALTER TABLE "public"."observations"
  ADD COLUMN IF NOT EXISTS "latest_deployment_id" integer,
  ADD COLUMN IF NOT EXISTS "file_globs" text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS "gratings" text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS "data_subdir" text;


-- =============================================================================
-- 4. Foreign keys
-- =============================================================================

ALTER TABLE ONLY "public"."deployments"
  ADD CONSTRAINT "deployments_observation_fkey"
  FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");

ALTER TABLE ONLY "public"."deployments"
  ADD CONSTRAINT "deployments_deployed_by_fkey"
  FOREIGN KEY ("deployed_by") REFERENCES "public"."user_profiles"("user_id");

ALTER TABLE ONLY "public"."observations"
  ADD CONSTRAINT "observations_latest_deployment_fkey"
  FOREIGN KEY ("latest_deployment_id") REFERENCES "public"."deployments"("id");


-- =============================================================================
-- 5. Indexes
-- =============================================================================

-- Upgrade spectra target+grating index to UNIQUE
DROP INDEX IF EXISTS "public"."idx_spectra_target_grating";
CREATE UNIQUE INDEX "idx_spectra_target_grating"
  ON "public"."spectra" USING btree ("target_id", "grating");

-- Deployments indexes
CREATE INDEX IF NOT EXISTS "idx_deployments_observation"
  ON "public"."deployments" USING btree ("observation");

CREATE INDEX IF NOT EXISTS "idx_deployments_deployed_by"
  ON "public"."deployments" USING btree ("deployed_by");

CREATE INDEX IF NOT EXISTS "idx_deployments_deployed_at"
  ON "public"."deployments" USING btree ("deployed_at" DESC);


-- =============================================================================
-- 6. RLS policies — admin write access for deploy CLI
-- =============================================================================

-- Programs: admin insert
DROP POLICY IF EXISTS "admin_programs_insert" ON programs;
CREATE POLICY "admin_programs_insert"
  ON programs FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Observations: admin insert + update
DROP POLICY IF EXISTS "admin_observations_insert" ON observations;
CREATE POLICY "admin_observations_insert"
  ON observations FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "admin_observations_update" ON observations;
CREATE POLICY "admin_observations_update"
  ON observations FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Targets: admin insert + update
DROP POLICY IF EXISTS "admin_targets_insert" ON targets;
CREATE POLICY "admin_targets_insert"
  ON targets FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "admin_targets_update" ON targets;
CREATE POLICY "admin_targets_update"
  ON targets FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Spectra: admin insert + update
DROP POLICY IF EXISTS "admin_spectra_insert" ON spectra;
CREATE POLICY "admin_spectra_insert"
  ON spectra FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "admin_spectra_update" ON spectra;
CREATE POLICY "admin_spectra_update"
  ON spectra FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Map layers: admin full access
DROP POLICY IF EXISTS "admin_map_layers_all" ON map_layers;
CREATE POLICY "admin_map_layers_all"
  ON map_layers FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Slit regions: admin insert + delete
DROP POLICY IF EXISTS "admin_slit_regions_insert" ON slit_regions;
CREATE POLICY "admin_slit_regions_insert"
  ON slit_regions FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "admin_slit_regions_delete" ON slit_regions;
CREATE POLICY "admin_slit_regions_delete"
  ON slit_regions FOR DELETE TO authenticated
  USING (public.is_admin());

-- Shutters: admin insert + delete
DROP POLICY IF EXISTS "admin_shutters_insert" ON shutters;
CREATE POLICY "admin_shutters_insert"
  ON shutters FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "admin_shutters_delete" ON shutters;
CREATE POLICY "admin_shutters_delete"
  ON shutters FOR DELETE TO authenticated
  USING (public.is_admin());

-- Deployments: RLS + policies
ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "authenticated_select_deployments" ON deployments;
CREATE POLICY "authenticated_select_deployments"
  ON deployments FOR SELECT TO authenticated
  USING (true);

DROP POLICY IF EXISTS "admin_deployments_insert" ON deployments;
CREATE POLICY "admin_deployments_insert"
  ON deployments FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());
