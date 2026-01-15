-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.comments (
  id integer NOT NULL DEFAULT nextval('comments_id_seq'::regclass),
  object_id integer NOT NULL,
  user_id uuid NOT NULL,
  content text NOT NULL,
  created_at timestamp without time zone DEFAULT now(),
  edited_at timestamp without time zone,
  is_deleted boolean DEFAULT false,
  CONSTRAINT comments_pkey PRIMARY KEY (id),
  CONSTRAINT comments_object_id_fkey FOREIGN KEY (object_id) REFERENCES public.objects(id),
  CONSTRAINT comments_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);
CREATE TABLE public.flag_audit_log (
  id integer NOT NULL DEFAULT nextval('flag_audit_log_id_seq'::regclass),
  object_id integer NOT NULL,
  user_id uuid NOT NULL,
  field_name text NOT NULL,
  old_value integer,
  new_value integer,
  changed_at timestamp without time zone DEFAULT now(),
  CONSTRAINT flag_audit_log_pkey PRIMARY KEY (id),
  CONSTRAINT flag_audit_log_object_id_fkey FOREIGN KEY (object_id) REFERENCES public.objects(id),
  CONSTRAINT flag_audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);
CREATE TABLE public.flag_definitions (
  category text NOT NULL,
  bit_position integer,
  value integer NOT NULL,
  label text NOT NULL,
  short_label text,
  icon text,
  color text,
  description text,
  CONSTRAINT flag_definitions_pkey PRIMARY KEY (category, value)
);
CREATE TABLE public.nircam_images (
  id integer NOT NULL DEFAULT nextval('nircam_images_id_seq'::regclass),
  field text NOT NULL,
  tile text NOT NULL,
  filter text NOT NULL,
  pixel_scale double precision NOT NULL,
  version text NOT NULL,
  extension text NOT NULL,
  file_path text NOT NULL,
  created_at timestamp without time zone DEFAULT now(),
  CONSTRAINT nircam_images_pkey PRIMARY KEY (id)
);
CREATE TABLE public.objects (
  id integer NOT NULL DEFAULT nextval('objects_id_seq'::regclass),
  object_id text NOT NULL UNIQUE,
  program_id integer NOT NULL,
  field text NOT NULL,
  ra double precision NOT NULL,
  dec double precision NOT NULL,
  redshift double precision,
  redshift_quality integer DEFAULT 0,
  spectral_features integer DEFAULT 0,
  object_flags integer DEFAULT 0,
  dq_flags integer DEFAULT 0,
  created_at timestamp without time zone DEFAULT now(),
  updated_at timestamp without time zone DEFAULT now(),
  CONSTRAINT objects_pkey PRIMARY KEY (id),
  CONSTRAINT objects_program_id_fkey FOREIGN KEY (program_id) REFERENCES public.programs(program_id)
);
CREATE TABLE public.programs (
  program_id integer NOT NULL,
  program_name text,
  pi_name text,
  description text,
  created_at timestamp without time zone DEFAULT now(),
  CONSTRAINT programs_pkey PRIMARY KEY (program_id)
);
CREATE TABLE public.spectra (
  id integer NOT NULL DEFAULT nextval('spectra_id_seq'::regclass),
  object_id integer NOT NULL,
  grating text NOT NULL,
  fits_path text NOT NULL,
  reduction_version text DEFAULT 'v1.0'::text,
  signal_to_noise double precision,
  created_at timestamp without time zone DEFAULT now(),
  CONSTRAINT spectra_pkey PRIMARY KEY (id),
  CONSTRAINT spectra_object_id_fkey FOREIGN KEY (object_id) REFERENCES public.objects(id)
);
CREATE TABLE public.user_profiles (
  user_id uuid NOT NULL,
  full_name text NOT NULL,
  created_at timestamp without time zone DEFAULT now(),
  is_group_account boolean DEFAULT false,
  can_comment boolean DEFAULT true,
  CONSTRAINT user_profiles_pkey PRIMARY KEY (user_id),
  CONSTRAINT user_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);
CREATE TABLE public.user_program_access (
  user_id uuid NOT NULL,
  program_id integer NOT NULL,
  granted_at timestamp without time zone DEFAULT now(),
  granted_by uuid,
  CONSTRAINT user_program_access_pkey PRIMARY KEY (user_id, program_id),
  CONSTRAINT user_program_access_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id),
  CONSTRAINT user_program_access_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES auth.users(id)
);