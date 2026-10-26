SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP FUNCTION
  "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  text,
  boolean,
  boolean,
  integer[],
  text,
  double precision,
  double precision,
  double precision,
  boolean,
  double precision,
  double precision,
  text,
  text,
  uuid,
  text,
  text,
  integer,
  integer,
  boolean,
  text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid);

DROP FUNCTION
  "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  integer,
  integer,
  integer,
  integer[],
  text,
  text,
  boolean,
  boolean,
  boolean,
  text,
  text,
  uuid,
  double precision,
  double precision,
  double precision,
  text,
  text,
  integer,
  integer,
  boolean,
  boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[]);

CREATE OR REPLACE FUNCTION public.catalog_match_ids (
  p_root                text,
  p_tree                jsonb,
  p_program_slugs       text[]  DEFAULT NULL::text[],
  p_include_unpublished boolean DEFAULT false,
  p_viewer_id           uuid    DEFAULT NULL::uuid
)
  RETURNS integer[]
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_from text;
  v_pred text;
  v_ids integer[];
BEGIN
  v_pred := public.catalog_tree_sql(p_tree, p_root, p_program_slugs, p_include_unpublished, p_viewer_id);
  v_from := CASE WHEN public.dr_browse_tag() IS NULL THEN 'public.' || p_root ELSE 'public.cat_' || p_root END;
  EXECUTE format('SELECT COALESCE(array_agg(r.id), ''{}''::integer[]) FROM %s r WHERE %s', v_from, v_pred) INTO v_ids;
  RETURN v_ids;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_array_literal (
  p_v         jsonb,
  p_elem_type text
)
  RETURNS text
  LANGUAGE plpgsql
  IMMUTABLE
  AS $function$
DECLARE
  parts text[];
  e jsonb;
BEGIN
  IF p_v IS NULL OR jsonb_typeof(p_v) <> 'array' OR jsonb_array_length(p_v) = 0 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: expected a non-empty array';
  END IF;
  IF jsonb_array_length(p_v) > 500 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: at most 500 values in an array';
  END IF;
  FOR e IN SELECT jsonb_array_elements(p_v) LOOP
    parts := array_append(parts, public.catalog_query_literal(e, p_elem_type));
  END LOOP;
  RETURN format('ARRAY[%s]::%s[]', array_to_string(parts, ', '),
                CASE p_elem_type WHEN 'num' THEN 'double precision' WHEN 'int' THEN 'integer' WHEN 'bits' THEN 'integer'
                                 WHEN 'text' THEN 'text' WHEN 'bool' THEN 'boolean' WHEN 'ts' THEN 'timestamptz' END);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_array_literal"(jsonb, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_correlations()
  RETURNS jsonb
  LANGUAGE sql
  IMMUTABLE
  AS $function$
  SELECT $j${
    "objects": {
      "targets":             "{T:targets} {c} WHERE {c}.object_id = {p}.id",
      "spectra":             "{T:spectra} {c} JOIN {T:targets} {c}_t ON {c}_t.target_id = {c}.target_id WHERE {c}_t.object_id = {p}.id",
      "lines":               "{T:lines} {c} WHERE {c}.object_id = {p}.id",
      "line_fits":           "{T:line_fits} {c} JOIN {T:spectra} {c}_s ON {c}_s.id = {c}.spectrum_id JOIN {T:targets} {c}_t ON {c}_t.target_id = {c}_s.target_id WHERE {c}_t.object_id = {p}.id",
      "photometry":          "{T:photometry} {c} WHERE {c}.object_id = {p}.id",
      "catalog_matches":     "{T:catalog_matches} {c} WHERE {c}.object_id = {p}.id",
      "object_list_members": "{T:object_list_members} {c} WHERE {c}.object_id = {p}.id"
    },
    "targets": {
      "objects":             "{T:objects} {c} WHERE {c}.id = {p}.object_id",
      "spectra":             "{T:spectra} {c} WHERE {c}.target_id = {p}.target_id",
      "lines":               "{T:lines} {c} JOIN {T:spectra} {c}_s ON {c}_s.id = {c}.spectrum_id WHERE {c}_s.target_id = {p}.target_id",
      "line_fits":           "{T:line_fits} {c} WHERE {c}.target_id = {p}.target_id",
      "photometry":          "{T:photometry} {c} WHERE {c}.object_id = {p}.object_id",
      "catalog_matches":     "{T:catalog_matches} {c} WHERE {c}.object_id = {p}.object_id",
      "object_list_members": "{T:object_list_members} {c} WHERE {c}.object_id = {p}.object_id"
    },
    "spectra": {
      "targets":             "{T:targets} {c} WHERE {c}.target_id = {p}.target_id",
      "objects":             "{T:objects} {c} JOIN {T:targets} {c}_t ON {c}_t.object_id = {c}.id WHERE {c}_t.target_id = {p}.target_id",
      "lines":               "{T:lines} {c} WHERE {c}.spectrum_id = {p}.id",
      "line_fits":           "{T:line_fits} {c} WHERE {c}.spectrum_id = {p}.id",
      "photometry":          "{T:photometry} {c} JOIN {T:targets} {c}_t ON {c}_t.object_id = {c}.object_id WHERE {c}_t.target_id = {p}.target_id",
      "catalog_matches":     "{T:catalog_matches} {c} JOIN {T:targets} {c}_t ON {c}_t.object_id = {c}.object_id WHERE {c}_t.target_id = {p}.target_id",
      "object_list_members": "{T:object_list_members} {c} JOIN {T:targets} {c}_t ON {c}_t.object_id = {c}.object_id WHERE {c}_t.target_id = {p}.target_id"
    },
    "lines": {
      "line_fits":           "{T:line_fits} {c} WHERE {c}.spectrum_id = {p}.spectrum_id",
      "spectra":             "{T:spectra} {c} WHERE {c}.id = {p}.spectrum_id"
    },
    "line_fits": {
      "lines":               "{T:lines} {c} WHERE {c}.spectrum_id = {p}.spectrum_id",
      "spectra":             "{T:spectra} {c} WHERE {c}.id = {p}.spectrum_id"
    },
    "photometry": {
      "catalog_matches":     "{T:catalog_matches} {c} WHERE {c}.id = {p}.match_id"
    },
    "catalog_matches": {
      "photometry":          "{T:photometry} {c} WHERE {c}.match_id = {p}.id"
    }
  }$j$::jsonb;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_correlations"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_literal (
  p_v    jsonb,
  p_type text
)
  RETURNS text
  LANGUAGE plpgsql
  IMMUTABLE
  AS $function$
DECLARE
  t text := jsonb_typeof(p_v);
BEGIN
  IF p_v IS NULL OR t = 'null' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: a null value needs is_null / not_null';
  END IF;
  CASE p_type
    WHEN 'num' THEN
      IF t <> 'number' THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: expected a number, got %s', t); END IF;
      RETURN format('(%s)::double precision', p_v #>> '{}');
    WHEN 'int', 'bits' THEN
      IF t <> 'number' OR (p_v #>> '{}') !~ '^-?[0-9]+$' THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: expected an integer, got %s', p_v); END IF;
      RETURN format('(%s)::integer', p_v #>> '{}');
    WHEN 'text' THEN
      IF t <> 'string' THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: expected a string, got %s', t); END IF;
      RETURN quote_literal(p_v #>> '{}');
    WHEN 'bool' THEN
      IF t <> 'boolean' THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: expected a boolean, got %s', t); END IF;
      RETURN CASE WHEN (p_v #>> '{}')::boolean THEN 'true' ELSE 'false' END;
    WHEN 'ts' THEN
      IF t <> 'string' THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: expected a timestamp string, got %s', t); END IF;
      RETURN format('%L::timestamptz', (p_v #>> '{}')::timestamptz);
    ELSE
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: no scalar of type %s', p_type);
  END CASE;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_literal"(jsonb, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_node_sql (
  p_node    jsonb,
  p_table   text,
  p_alias   text,
  p_catalog text,
  p_depth   integer,
  p_idx     integer,
  p_scope   jsonb   DEFAULT NULL::jsonb
)
  RETURNS text
  LANGUAGE plpgsql
  STABLE
  AS $function$
DECLARE
  wl jsonb := public.catalog_query_whitelist();
  cols jsonb;
  parts text[] := '{}';
  child jsonb;
  i integer := 0;
  v_col text;
  v_type text;
  v_op text;
  v_val jsonb;
  v_lhs text;
  v_child text;
  v_tmpl text;
  v_alias text;
  v_where text;
  v_cone jsonb;
  v_ra text; v_dec text; v_r text;
BEGIN
  IF p_depth > 6 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: tree nested deeper than 6 levels';
  END IF;
  IF p_node IS NULL OR jsonb_typeof(p_node) <> 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: every node is a JSON object';
  END IF;
  cols := wl -> p_table;
  IF cols IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: unknown table %s', p_table);
  END IF;

  -- group ---------------------------------------------------------------
  IF p_node ? 'and' OR p_node ? 'or' THEN
    child := COALESCE(p_node -> 'and', p_node -> 'or');
    IF jsonb_typeof(child) <> 'array' OR jsonb_array_length(child) = 0 THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: a group is a non-empty array of nodes';
    END IF;
    IF jsonb_array_length(child) > 50 THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: at most 50 nodes in a group';
    END IF;
    FOR v_val IN SELECT jsonb_array_elements(child) LOOP
      i := i + 1;
      parts := array_append(parts, public.catalog_query_node_sql(v_val, p_table, p_alias, p_catalog, p_depth + 1, i, p_scope));
    END LOOP;
    RETURN '(' || array_to_string(parts, CASE WHEN p_node ? 'and' THEN ' AND ' ELSE ' OR ' END) || ')';
  END IF;

  -- cone ----------------------------------------------------------------
  IF p_node ? 'cone' THEN
    IF NOT (cols ? 'ra' AND cols ? 'dec') THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s has no sky position for a cone', p_table);
    END IF;
    v_cone := p_node -> 'cone';
    v_ra := public.catalog_query_literal(v_cone -> 'ra', 'num');
    v_dec := public.catalog_query_literal(v_cone -> 'dec', 'num');
    v_r := public.catalog_query_literal(v_cone -> 'radius', 'num');
    IF (v_cone ->> 'radius')::double precision <= 0 OR (v_cone ->> 'radius')::double precision > 10 THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: cone radius must be in (0, 10] degrees';
    END IF;
    -- the list RPCs' cone: a box pre-filter the index can use (RA widened by
    -- 1/cos(dec), dropped near a pole, and repeated one turn either side so a
    -- cone straddling RA = 0 keeps both edges; Codex on #659), then the
    -- Haversine cut decides (periodic in RA by construction)
    RETURN format(
      '((ABS(%2$s) + %3$s >= 90 '
      'OR %1$I.ra BETWEEN (%4$s - %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6)) AND (%4$s + %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6)) '
      'OR %1$I.ra BETWEEN (%4$s - 360 - %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6)) AND (%4$s - 360 + %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6)) '
      'OR %1$I.ra BETWEEN (%4$s + 360 - %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6)) AND (%4$s + 360 + %3$s / GREATEST(COS(RADIANS(%2$s)), 1e-6))) '
      'AND %1$I.dec BETWEEN (%2$s - %3$s) AND (%2$s + %3$s) '
      'AND 2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(%1$I.dec - %2$s) / 2), 2) + COS(RADIANS(%2$s)) * COS(RADIANS(%1$I.dec)) * POWER(SIN(RADIANS(%1$I.ra - %4$s) / 2), 2)))) <= %3$s)',
      p_alias, v_dec, v_r, v_ra);
  END IF;

  -- exists --------------------------------------------------------------
  IF p_node ? 'exists' THEN
    v_child := p_node ->> 'exists';
    v_tmpl := public.catalog_query_correlations() -> p_table ->> v_child;
    IF v_tmpl IS NULL THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s has no %s to test', p_table, COALESCE(v_child, '(missing)'));
    END IF;
    v_alias := format('x%s_%s', p_depth, p_idx);
    v_tmpl := replace(replace(v_tmpl, '{c}', v_alias), '{p}', p_alias);
    -- table tokens: live base tables, or the cat_* views of the browsed
    -- release (lists are never frozen: object_list_members stays live)
    v_tmpl := regexp_replace(v_tmpl, '\{T:([a-z_]+)\}',
                CASE WHEN p_catalog = 'dr' THEN 'public.cat_\1' ELSE 'public.\1' END, 'g');
    IF p_catalog = 'dr' THEN
      v_tmpl := replace(v_tmpl, 'public.cat_object_list_members', 'public.object_list_members');
    END IF;
    v_where := '';
    IF p_node ? 'where' THEN
      child := p_node -> 'where';
      IF jsonb_typeof(child) <> 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: "where" is an array of nodes';
      END IF;
      IF jsonb_array_length(child) > 0 THEN
        v_where := ' AND ' || public.catalog_query_node_sql(jsonb_build_object('and', child), v_child, v_alias, p_catalog, p_depth + 1, 1, p_scope);
      END IF;
    END IF;
    RETURN format('%sEXISTS (SELECT 1 FROM %s%s%s)',
                  CASE WHEN COALESCE((p_node ->> 'not')::boolean, false) THEN 'NOT ' ELSE '' END,
                  v_tmpl, public.catalog_query_scope_sql(v_child, v_alias, p_catalog, p_scope), v_where);
  END IF;

  -- predicate -----------------------------------------------------------
  IF NOT (p_node ? 'col' AND p_node ? 'op') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: unrecognised node %s', left(p_node::text, 200));
  END IF;
  v_col := p_node ->> 'col';
  IF position('.' in v_col) > 0 THEN
    IF split_part(v_col, '.', 1) <> p_table THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s is not a column of %s', v_col, p_table);
    END IF;
    v_col := split_part(v_col, '.', 2);
  END IF;
  v_type := cols ->> v_col;
  IF v_type IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s has no column %s', p_table, v_col);
  END IF;
  v_op := p_node ->> 'op';
  v_val := p_node -> 'v';
  v_lhs := format('%I.%I', p_alias, v_col);

  IF v_op = 'is_null' THEN RETURN v_lhs || ' IS NULL'; END IF;
  IF v_op = 'not_null' THEN RETURN v_lhs || ' IS NOT NULL'; END IF;

  CASE v_type
    WHEN 'num', 'int', 'ts' THEN
      CASE v_op
        WHEN 'eq'  THEN RETURN format('%s = %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'neq' THEN RETURN format('%s <> %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'gt'  THEN RETURN format('%s > %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'gte' THEN RETURN format('%s >= %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'lt'  THEN RETURN format('%s < %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'lte' THEN RETURN format('%s <= %s', v_lhs, public.catalog_query_literal(v_val, v_type));
        WHEN 'between' THEN
          -- [lo, hi], either end null for open
          IF jsonb_typeof(v_val) <> 'array' OR jsonb_array_length(v_val) <> 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: between takes [lo, hi]';
          END IF;
          IF jsonb_typeof(v_val -> 0) = 'null' AND jsonb_typeof(v_val -> 1) = 'null' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: between needs at least one bound';
          END IF;
          IF jsonb_typeof(v_val -> 0) = 'null' THEN
            RETURN format('%s <= %s', v_lhs, public.catalog_query_literal(v_val -> 1, v_type));
          ELSIF jsonb_typeof(v_val -> 1) = 'null' THEN
            RETURN format('%s >= %s', v_lhs, public.catalog_query_literal(v_val -> 0, v_type));
          END IF;
          RETURN format('%s BETWEEN %s AND %s', v_lhs, public.catalog_query_literal(v_val -> 0, v_type), public.catalog_query_literal(v_val -> 1, v_type));
        WHEN 'in'  THEN RETURN format('%s = ANY(%s)', v_lhs, public.catalog_query_array_literal(v_val, v_type));
        WHEN 'nin' THEN RETURN format('NOT (%s = ANY(%s))', v_lhs, public.catalog_query_array_literal(v_val, v_type));
        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s does not apply to %s.%s', v_op, p_table, v_col);
      END CASE;
    WHEN 'text' THEN
      CASE v_op
        WHEN 'eq'    THEN RETURN format('%s = %s', v_lhs, public.catalog_query_literal(v_val, 'text'));
        WHEN 'neq'   THEN RETURN format('%s <> %s', v_lhs, public.catalog_query_literal(v_val, 'text'));
        WHEN 'in'    THEN RETURN format('%s = ANY(%s)', v_lhs, public.catalog_query_array_literal(v_val, 'text'));
        WHEN 'nin'   THEN RETURN format('NOT (%s = ANY(%s))', v_lhs, public.catalog_query_array_literal(v_val, 'text'));
        WHEN 'ilike' THEN RETURN format('%s ILIKE %s', v_lhs, public.catalog_query_literal(v_val, 'text'));
        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s does not apply to %s.%s', v_op, p_table, v_col);
      END CASE;
    WHEN 'bool' THEN
      CASE v_op
        WHEN 'eq'  THEN RETURN format('%s = %s', v_lhs, public.catalog_query_literal(v_val, 'bool'));
        WHEN 'neq' THEN RETURN format('%s <> %s', v_lhs, public.catalog_query_literal(v_val, 'bool'));
        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s does not apply to %s.%s', v_op, p_table, v_col);
      END CASE;
    WHEN 'text[]', 'int[]' THEN
      CASE v_op
        WHEN 'overlaps' THEN RETURN format('%s && %s', v_lhs, public.catalog_query_array_literal(v_val, rtrim(v_type, '[]')));
        WHEN 'contains' THEN RETURN format('%s @> %s', v_lhs, public.catalog_query_array_literal(v_val, rtrim(v_type, '[]')));
        WHEN 'has'      THEN RETURN format('%s = ANY(%s)', public.catalog_query_literal(v_val, rtrim(v_type, '[]')), v_lhs);
        WHEN 'nhas'     THEN RETURN format('NOT (%s = ANY(%s))', public.catalog_query_literal(v_val, rtrim(v_type, '[]')), v_lhs);
        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s does not apply to %s.%s', v_op, p_table, v_col);
      END CASE;
    WHEN 'bits' THEN
      CASE v_op
        WHEN 'anybits' THEN RETURN format('(COALESCE(%s, 0) & %s) <> 0', v_lhs, public.catalog_query_literal(v_val, 'bits'));
        WHEN 'allbits' THEN RETURN format('(COALESCE(%s, 0) & %s) = %s', v_lhs, public.catalog_query_literal(v_val, 'bits'), public.catalog_query_literal(v_val, 'bits'));
        WHEN 'nobits'  THEN RETURN format('(COALESCE(%s, 0) & %s) = 0', v_lhs, public.catalog_query_literal(v_val, 'bits'));
        WHEN 'eq'      THEN RETURN format('COALESCE(%s, 0) = %s', v_lhs, public.catalog_query_literal(v_val, 'bits'));
        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: %s does not apply to %s.%s', v_op, p_table, v_col);
      END CASE;
    ELSE
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: unhandled type %s', v_type);
  END CASE;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_node_sql"(jsonb, text, text, text, integer, integer, jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_objects (
  p_tree                jsonb,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false,
  p_search              text             DEFAULT NULL::text,
  p_sort_column         text             DEFAULT 'object_id'::text,
  p_sort_direction      text             DEFAULT 'asc'::text,
  p_page                integer          DEFAULT 1,
  p_page_size           integer          DEFAULT 50,
  p_include_count       boolean          DEFAULT true,
  p_after_sort_text     text             DEFAULT NULL::text,
  p_after_sort_num      double precision DEFAULT NULL::double precision,
  p_after_tiebreak      text[]           DEFAULT NULL::text[],
  p_viewer_id           uuid             DEFAULT NULL::uuid,
  p_line                text             DEFAULT NULL::text,
  p_line_snr_min        double precision DEFAULT NULL::double precision,
  p_line_snr_max        double precision DEFAULT NULL::double precision,
  p_line_include_stale  boolean          DEFAULT false,
  p_band                text             DEFAULT NULL::text,
  p_band_mag_min        double precision DEFAULT NULL::double precision,
  p_band_mag_max        double precision DEFAULT NULL::double precision,
  p_band_snr_min        double precision DEFAULT NULL::double precision,
  p_band_snr_max        double precision DEFAULT NULL::double precision
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_ids integer[];
BEGIN
  v_ids := public.catalog_match_ids('objects', p_tree, p_program_slugs, p_include_unpublished, p_viewer_id);
  IF cardinality(v_ids) = 0 THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM public.get_filtered_objects_paginated(
    p_program_slugs := p_program_slugs,
    p_search := p_search,
    p_sort_column := p_sort_column,
    p_sort_direction := p_sort_direction,
    p_page := p_page,
    p_page_size := p_page_size,
    p_include_unpublished := p_include_unpublished,
    p_line := p_line,
    p_line_snr_min := p_line_snr_min,
    p_line_snr_max := p_line_snr_max,
    p_line_include_stale := p_line_include_stale,
    p_band := p_band,
    p_band_mag_min := p_band_mag_min,
    p_band_mag_max := p_band_mag_max,
    p_band_snr_min := p_band_snr_min,
    p_band_snr_max := p_band_snr_max,
    p_include_count := p_include_count,
    p_after_sort_text := p_after_sort_text,
    p_after_sort_num := p_after_sort_num,
    p_after_tiebreak := p_after_tiebreak,
    p_viewer_id := p_viewer_id,
    p_object_ids := v_ids,
    p_measurements_display_only := true);
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_scope_sql (
  p_table   text,
  p_alias   text,
  p_catalog text,
  p_scope   jsonb
)
  RETURNS text
  LANGUAGE plpgsql
  IMMUTABLE
  AS $function$
DECLARE
  v_slugs text;
  v_unpub text;
  v_viewer text;
  v_t text := CASE WHEN p_catalog = 'dr' THEN 'public.cat_' ELSE 'public.' END;
  v_spec text;
  v_obj text;
BEGIN
  IF p_scope IS NULL THEN RETURN ''; END IF;
  v_slugs := public.catalog_query_array_literal(COALESCE(p_scope -> 'slugs', '[]'::jsonb) || CASE WHEN jsonb_array_length(COALESCE(p_scope -> 'slugs', '[]'::jsonb)) = 0 THEN '[""]'::jsonb ELSE '[]'::jsonb END, 'text');
  v_unpub := CASE WHEN COALESCE((p_scope ->> 'unpub')::boolean, false) THEN 'true' ELSE 'false' END;
  v_viewer := CASE WHEN p_scope ->> 'viewer' IS NULL THEN 'NULL::uuid' ELSE format('%L::uuid', (p_scope ->> 'viewer')::uuid) END;
  -- the spectra and objects policies, as the legacy RPCs restate them
  v_spec := format('(%%1$s.program_slug = ANY(%s) AND (%%1$s.deploy_status = ''published'' OR %s))', v_slugs, v_unpub);
  v_obj := format('(%%1$s.programs && %s AND %%1$s.is_active AND (%%1$s.has_published_spectrum OR %s))', v_slugs, v_unpub);
  CASE p_table
    WHEN 'objects' THEN RETURN ' AND ' || format(v_obj, p_alias);
    WHEN 'targets' THEN RETURN format(' AND (%1$I.program_slug = ANY(%2$s) AND (%1$I.has_published_spectrum OR %3$s))', p_alias, v_slugs, v_unpub);
    WHEN 'spectra' THEN RETURN ' AND ' || format(v_spec, p_alias);
    WHEN 'lines', 'line_fits' THEN
      RETURN format(' AND EXISTS (SELECT 1 FROM %sspectra __s WHERE __s.id = %I.spectrum_id AND %s)', v_t, p_alias, format(v_spec, '__s'));
    WHEN 'photometry', 'catalog_matches' THEN
      RETURN format(' AND EXISTS (SELECT 1 FROM %sobjects __o WHERE __o.id = %I.object_id AND %s)', v_t, p_alias, format(v_obj, '__o'));
    WHEN 'object_list_members' THEN
      -- viewable_list_ids() for an explicit viewer: own, public, or shared with
      RETURN format(' AND EXISTS (SELECT 1 FROM public.object_lists __l WHERE __l.id = %1$I.list_id AND (__l.visibility IN (''public_read'', ''public_edit'') OR __l.created_by = %2$s OR __l.id IN (SELECT __sh.list_id FROM public.object_list_shares __sh WHERE __sh.user_id = %2$s)))', p_alias, v_viewer);
    ELSE
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: no access scope for %s', p_table);
  END CASE;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_scope_sql"(text, text, text, jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_spectra (
  p_tree                jsonb,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false,
  p_search              text             DEFAULT NULL::text,
  p_sort_column         text             DEFAULT 'target_id'::text,
  p_sort_direction      text             DEFAULT 'asc'::text,
  p_page                integer          DEFAULT 1,
  p_page_size           integer          DEFAULT 50,
  p_include_thumbnails  boolean          DEFAULT false,
  p_include_count       boolean          DEFAULT true,
  p_after_sort_text     text             DEFAULT NULL::text,
  p_after_sort_num      double precision DEFAULT NULL::double precision,
  p_after_tiebreak      text[]           DEFAULT NULL::text[],
  p_line                text             DEFAULT NULL::text,
  p_line_snr_min        double precision DEFAULT NULL::double precision,
  p_line_snr_max        double precision DEFAULT NULL::double precision,
  p_line_include_stale  boolean          DEFAULT false,
  p_band                text             DEFAULT NULL::text,
  p_band_mag_min        double precision DEFAULT NULL::double precision,
  p_band_mag_max        double precision DEFAULT NULL::double precision,
  p_band_snr_min        double precision DEFAULT NULL::double precision,
  p_band_snr_max        double precision DEFAULT NULL::double precision,
  p_viewer_id           uuid             DEFAULT NULL::uuid
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_ids integer[];
BEGIN
  v_ids := public.catalog_match_ids('spectra', p_tree, p_program_slugs, p_include_unpublished, p_viewer_id);
  IF cardinality(v_ids) = 0 THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM public.get_filtered_spectra_paginated(
    p_program_slugs := p_program_slugs,
    p_search := p_search,
    p_sort_column := p_sort_column,
    p_sort_direction := p_sort_direction,
    p_page := p_page,
    p_page_size := p_page_size,
    p_include_thumbnails := p_include_thumbnails,
    p_include_unpublished := p_include_unpublished,
    p_line := p_line,
    p_line_snr_min := p_line_snr_min,
    p_line_snr_max := p_line_snr_max,
    p_line_include_stale := p_line_include_stale,
    p_band := p_band,
    p_band_mag_min := p_band_mag_min,
    p_band_mag_max := p_band_mag_max,
    p_band_snr_min := p_band_snr_min,
    p_band_snr_max := p_band_snr_max,
    p_include_count := p_include_count,
    p_after_sort_text := p_after_sort_text,
    p_after_sort_num := p_after_sort_num,
    p_after_tiebreak := p_after_tiebreak,
    p_spectrum_ids := v_ids,
    p_measurements_display_only := true);
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, uuid)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_whitelist()
  RETURNS jsonb
  LANGUAGE sql
  IMMUTABLE
  AS $function$
  SELECT $j${
    "objects": {
      "id": "int", "object_id": "text", "field": "text", "ra": "num", "dec": "num",
      "n_targets": "int", "n_spectra": "int", "programs": "text[]", "gratings": "text[]",
      "observations": "text[]", "max_snr": "num", "max_exposure_time": "num",
      "photo_z": "num", "photo_z_err_lo": "num", "photo_z_err_hi": "num", "has_photometry": "bool",
      "redshift": "num", "redshift_quality": "int", "redshift_auto": "num", "redshift_inspected": "num",
      "inspected_used_auto": "bool", "needs_review": "bool", "last_inspected_at": "ts",
      "last_data_change_at": "ts", "version": "int", "is_active": "bool",
      "has_published_spectrum": "bool", "is_public": "bool", "created_at": "ts", "updated_at": "ts"
    },
    "targets": {
      "id": "int", "target_id": "text", "field": "text", "ra": "num", "dec": "num",
      "program_slug": "text", "observation": "text", "max_snr": "num", "max_exposure_time": "num",
      "redshift_auto": "num", "dq_flags": "bits", "spectral_features": "bits",
      "has_published_spectrum": "bool", "is_public": "bool", "object_id": "int"
    },
    "spectra": {
      "id": "int", "spectrum_id": "text", "target_id": "text", "grating": "text",
      "signal_to_noise": "num", "exposure_time": "num", "redshift_auto": "num", "chi2_min": "num",
      "confidence": "num", "dq_flags": "bits", "deploy_status": "text", "program_slug": "text",
      "observation": "text", "date_obs": "text", "cfpipe_version": "text", "jwst_version": "text",
      "crds_context": "text", "is_public": "bool", "reduced_at": "ts", "created_at": "ts", "updated_at": "ts"
    },
    "lines": {
      "spectrum_id": "int", "object_id": "int", "line": "text", "component": "text",
      "wave_rest": "num", "wave_obs": "num", "flux": "num", "flux_err": "num", "snr": "num",
      "ew_rest": "num", "ew_rest_err": "num", "cont": "num", "cont_err": "num",
      "dv": "num", "dv_err": "num", "sigma_v": "num", "sigma_v_err": "num", "sigma_lsf_kms": "num",
      "complex": "int", "chi2": "num", "dof": "int", "npix": "int", "flags": "bits",
      "blend_into": "text", "tied_to": "text", "label": "text", "members": "text[]"
    },
    "line_fits": {
      "spectrum_id": "int", "target_id": "text", "grating": "text", "program_slug": "text",
      "observation": "text", "z_used": "num", "z_source": "text", "z_quality": "int",
      "object_id": "text", "object_version": "int", "z_fit": "num", "z_fit_err": "num",
      "dv": "num", "dv_err": "num", "sigma_v": "num", "sigma_v_err": "num", "kin_source": "text",
      "n_lines": "int", "n_detected": "int", "n_broad": "int", "chi2": "num", "dof": "int",
      "fit_version": "text", "cfpipe_version": "text", "f_lsf": "num", "fitted_at": "ts",
      "stale_redshift": "bool"
    },
    "photometry": {
      "match_id": "int", "object_id": "int", "band": "text", "flux": "num", "flux_err": "num",
      "wav": "num", "wav_min": "num", "wav_max": "num", "snr": "num", "mag": "num"
    },
    "catalog_matches": {
      "id": "int", "object_id": "int", "field": "text", "ra": "num", "dec": "num",
      "catalog_name": "text", "catalog_id": "text", "match_distance_arcsec": "num",
      "photo_z": "num", "photo_z_err_lo": "num", "photo_z_err_hi": "num", "has_pz": "bool"
    },
    "object_list_members": {
      "list_id": "int", "object_id": "int", "added_at": "ts"
    }
  }$j$::jsonb;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_query_whitelist"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_tree_sql (
  p_tree                jsonb,
  p_root                text    DEFAULT 'objects'::text,
  p_program_slugs       text[]  DEFAULT NULL::text[],
  p_include_unpublished boolean DEFAULT false,
  p_viewer_id           uuid    DEFAULT NULL::uuid
)
  RETURNS text
  LANGUAGE plpgsql
  STABLE
  AS $function$
DECLARE
  v_scope jsonb := CASE WHEN p_program_slugs IS NULL THEN NULL
                        ELSE jsonb_build_object('slugs', to_jsonb(p_program_slugs), 'unpub', COALESCE(p_include_unpublished, false), 'viewer', p_viewer_id) END;
  v_catalog text := CASE WHEN public.dr_browse_tag() IS NULL THEN 'live' ELSE 'dr' END;
BEGIN
  IF p_root NOT IN ('objects', 'spectra', 'targets') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: root must be objects, spectra or targets, not %s', p_root);
  END IF;
  IF p_tree IS NULL OR jsonb_typeof(p_tree) <> 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: the tree is a JSON object';
  END IF;
  IF length(p_tree::text) > 20000 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: tree larger than 20 kB';
  END IF;
  -- "v" at the root is the tree version only on a group (a root predicate's
  -- "v" is its value)
  IF (p_tree ? 'and' OR p_tree ? 'or') AND (p_tree ->> 'v') IS NOT NULL AND (p_tree ->> 'v') <> '1' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = format('catalog_query: unknown tree version %s', p_tree ->> 'v');
  END IF;
  -- the root's own scope, then the tree (each exists child carries its own)
  RETURN public.catalog_query_node_sql(CASE WHEN p_tree ? 'and' OR p_tree ? 'or' THEN p_tree - 'v' ELSE p_tree END, p_root, 'r', v_catalog, 0, 1, v_scope)
         || public.catalog_query_scope_sql(p_root, 'r', v_catalog, v_scope);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated (
  p_program_slugs             text[],
  p_filter_programs           text[]           DEFAULT NULL::text[],
  p_fields                    text[]           DEFAULT NULL::text[],
  p_gratings                  text[]           DEFAULT NULL::text[],
  p_gratings_mode             text             DEFAULT 'any'::text,
  p_observations              text[]           DEFAULT NULL::text[],
  p_redshift_quality          integer[]        DEFAULT NULL::integer[],
  p_redshift_min              double precision DEFAULT NULL::double precision,
  p_redshift_max              double precision DEFAULT NULL::double precision,
  p_max_snr_min               double precision DEFAULT NULL::double precision,
  p_max_snr_max               double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min     double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max     double precision DEFAULT NULL::double precision,
  p_search                    text             DEFAULT NULL::text,
  p_inspected_only            boolean          DEFAULT NULL::boolean,
  p_needs_review              boolean          DEFAULT NULL::boolean,
  p_list_ids                  integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode             text             DEFAULT 'any'::text,
  p_coord_ra                  double precision DEFAULT NULL::double precision,
  p_coord_dec                 double precision DEFAULT NULL::double precision,
  p_radius_degrees            double precision DEFAULT NULL::double precision,
  p_has_photometry            boolean          DEFAULT NULL::boolean,
  p_photo_z_min               double precision DEFAULT NULL::double precision,
  p_photo_z_max               double precision DEFAULT NULL::double precision,
  p_comment_search            text             DEFAULT NULL::text,
  p_comment_search_scope      text             DEFAULT NULL::text,
  p_comment_user_id           uuid             DEFAULT NULL::uuid,
  p_sort_column               text             DEFAULT 'object_id'::text,
  p_sort_direction            text             DEFAULT 'asc'::text,
  p_page                      integer          DEFAULT 1,
  p_page_size                 integer          DEFAULT 50,
  p_include_unpublished       boolean          DEFAULT false,
  p_line                      text             DEFAULT NULL::text,
  p_line_snr_min              double precision DEFAULT NULL::double precision,
  p_line_snr_max              double precision DEFAULT NULL::double precision,
  p_line_include_stale        boolean          DEFAULT false,
  p_band                      text             DEFAULT NULL::text,
  p_band_mag_min              double precision DEFAULT NULL::double precision,
  p_band_mag_max              double precision DEFAULT NULL::double precision,
  p_band_snr_min              double precision DEFAULT NULL::double precision,
  p_band_snr_max              double precision DEFAULT NULL::double precision,
  p_include_count             boolean          DEFAULT true,
  p_after_sort_text           text             DEFAULT NULL::text,
  p_after_sort_num            double precision DEFAULT NULL::double precision,
  p_after_tiebreak            text[]           DEFAULT NULL::text[],
  p_viewer_id                 uuid             DEFAULT NULL::uuid,
  p_object_ids                integer[]        DEFAULT NULL::integer[],
  p_measurements_display_only boolean          DEFAULT false
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- T2-F: cursor slot of the resolved sort column (see the spectra RPC).
  v_sort_is_text := p_sort_column IN ('object_id', 'field');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 1);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

  -- Intersect user-accessible programs with filter selection
  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  -- COALESCE: array_length('{}',1) is NULL, and a NULL flag would make the
  -- NOT-flag predicate below reject every row instead of treating an empty
  -- selection as no filter (the pre-#491 predicate's explicit behavior).
  v_observation_filter_active := (p_observations IS NOT NULL AND COALESCE(array_length(p_observations, 1), 0) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  -- Step 1: count — a second full pass over the filter, so only when the
  -- caller doesn't already know the total for this filter set (#501).
  IF p_include_count THEN
    SELECT COUNT(*) INTO v_total_count
    FROM public.cat_objects o
    WHERE
      -- Access control: object must have at least one accessible program
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      -- B1: hide objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR p_measurements_display_only OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR o.id = ANY(v_band_object_ids))
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: collect the object_ids that have a matching
        -- comment ONCE (object-level comments directly + target-level comments
        -- mapped through their parent object), then probe o.id IN (...). The old
        -- correlated EXISTS-inside-OR re-ran a per-object targets subquery for
        -- every (object x matching-comment) pair -> 271k subplan executions /
        -- ~870ms here, multi-second on broad terms or cold cache.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN public.cat_targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      );

  ELSE
    v_total_count := -1;
  END IF;

  -- Step 2: fetch page
  RETURN QUERY
  WITH candidates AS (
    SELECT
      o.id,
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.n_targets,
      o.n_spectra,
      o.programs,
      o.gratings,
      o.max_snr,
      o.max_exposure_time,
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.redshift_auto,
      o.inspected_used_auto,
      o.last_inspected_at,
      o.last_inspected_by,
      o.last_data_change_at,
      o.staleness_reason,
      o.needs_review,
      o.version,
      o.is_active,
      o.photo_z,
      o.has_photometry,
      o.created_at,
      -- the object's best S/N in the filtered line (sort key + 'Line S/N'
      -- column): the same aggregate the line filter tested, from the one
      -- object_line_values scan joined below; NULL without a line filter
      lv.snr AS line_snr,
      -- the object's magnitude and S/N in the filtered band (sort keys + the
      -- 'Mag' / 'Band S/N' columns); the same aggregates the band filter tested
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM public.cat_objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
    LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = o.id
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR p_measurements_display_only OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR o.id = ANY(v_band_object_ids))
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see the count query above for the rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN public.cat_targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  -- T2-F: sort key in one of two typed slots; the ORDER BY, the keyset
  -- predicate and the returned next-cursor all read these (see the spectra
  -- RPC). The CASEs fold to one column at plan time (force_custom_plan).
  keyed AS (
    SELECT c.*,
      CASE p_sort_column
        WHEN 'object_id' THEN c.object_id
        WHEN 'field' THEN c.field
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN c.distance::double precision
        WHEN 'ra' THEN c.ra::double precision
        WHEN 'dec' THEN c."dec"::double precision
        WHEN 'redshift' THEN c.redshift::double precision
        WHEN 'redshift_quality' THEN c.redshift_quality::double precision
        WHEN 'n_targets' THEN c.n_targets::double precision
        WHEN 'n_spectra' THEN c.n_spectra::double precision
        WHEN 'max_snr' THEN c.max_snr::double precision
        WHEN 'max_exposure_time' THEN c.max_exposure_time::double precision
        WHEN 'photo_z' THEN c.photo_z::double precision
        WHEN 'line_snr' THEN c.line_snr
        WHEN 'band_mag' THEN c.band_mag
        WHEN 'band_snr' THEN c.band_snr
      END AS sort_num
    FROM candidates c
  ),
  -- One row past the page so has_more is exact; with_members drops it and the
  -- cursor is taken from row p_page_size.
  filtered_objects AS (
    SELECT *, ROW_NUMBER() OVER () AS rn
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset predicate — same shape as the spectra RPC, tiebreak object_id.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text AND k.object_id > p_after_tiebreak[1])
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num AND k.object_id > p_after_tiebreak[1])
          END
        )
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.object_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  cursor_row AS (
    SELECT fo.sort_text, fo.sort_num, fo.object_id
    FROM filtered_objects fo
    WHERE fo.rn = p_page_size
      AND EXISTS (SELECT 1 FROM filtered_objects x WHERE x.rn > p_page_size)
  ),
  with_members AS (
    SELECT
      fo.rn,
      jsonb_build_object(
        'id', fo.id,
        'object_id', fo.object_id,
        'field', fo.field,
        'ra', fo.ra,
        'dec', fo.dec,
        -- Aggregates scoped to the viewer's accessible programs so mixed-program
        -- objects don't leak proprietary member metadata. Deliberately NOT
        -- narrowed by p_filter_programs: a program filter selects which objects
        -- appear (overlap test above), but each row still shows the object's
        -- full accessible programs/observations. Filter and sort above run on
        -- the global o.* columns; the substitution happens only on the
        -- paginated result set.
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
        'line_snr', fo.line_snr,
        'band_mag', fo.band_mag,
        'band_snr', fo.band_snr,
        'redshift', fo.redshift,
        'redshift_quality', fo.redshift_quality,
        'redshift_inspected', fo.redshift_inspected,
        'redshift_auto', fo.redshift_auto,
        'inspected_used_auto', fo.inspected_used_auto,
        'last_inspected_at', fo.last_inspected_at,
        'last_inspected_by', fo.last_inspected_by,
        'last_data_change_at', fo.last_data_change_at,
        'staleness_reason', fo.staleness_reason,
        'needs_review', fo.needs_review,
        'version', fo.version,
        'is_active', fo.is_active,
        'photo_z', fo.photo_z,
        'has_photometry', fo.has_photometry,
        'created_at', fo.created_at,
        'distance', fo.distance,
        -- Phase D: member_targets becomes provenance only (target_id, program,
        -- observation). Inspection state lives on the object now; redshift_auto
        -- on targets is retained for transitional UI display until Phase E.
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift_auto', t.redshift_auto
            ) ORDER BY t.target_id
          )
          FROM public.cat_targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(p_program_slugs)
            -- Same publication gate as object_scoped_aggregates: the RPC is
            -- reached via the service-role client (/api/v1/objects), so RLS
            -- won't hide draft-only members here.
            AND (p_include_unpublished OR t.has_published_spectrum)
          ),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', ol.id,
              'name', ol.name,
              'slug', ol.slug,
              'icon', ol.icon,
              'color', ol.color
            ) ORDER BY ol.name
          )
          FROM object_list_members olm
          JOIN object_lists ol ON ol.id = olm.list_id
          WHERE olm.object_id = fo.id
            -- Under a user JWT RLS on object_lists is the gate (select_lists:
            -- owner / public / shared, every list for admins). The service-role
            -- client (/api/v1/objects) bypasses RLS, so there the lists are
            -- those p_viewer_id can see: viewable_list_ids() for an explicit
            -- user, as in get_objects_for_sync. Role test as in
            -- object_band_values.
            AND (COALESCE((SELECT auth.role()), '') <> 'service_role'
                 OR ol.created_by = p_viewer_id
                 OR ol.visibility IN ('public_read', 'public_edit')
                 OR ol.id IN (SELECT list_id FROM object_list_shares
                              WHERE user_id = p_viewer_id))),
          '[]'::jsonb
        )
      ) AS obj_json
    FROM filtered_objects fo
    LEFT JOIN LATERAL public.object_scoped_aggregates(fo.id, p_program_slugs, p_include_unpublished) sa ON true
    -- Drop the has_more overflow row before the per-row aggregates run.
    WHERE fo.rn <= p_page_size
  )
  SELECT
    COALESCE(jsonb_agg(wm.obj_json ORDER BY wm.rn), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM filtered_objects x WHERE x.rn > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.object_id] FROM cursor_row cr)
  FROM with_members wm;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated (
  p_program_slugs             text[],
  p_filter_programs           text[]           DEFAULT NULL::text[],
  p_fields                    text[]           DEFAULT NULL::text[],
  p_gratings                  text[]           DEFAULT NULL::text[],
  p_gratings_mode             text             DEFAULT 'any'::text,
  p_observations              text[]           DEFAULT NULL::text[],
  p_redshift_quality          integer[]        DEFAULT NULL::integer[],
  p_redshift_min              double precision DEFAULT NULL::double precision,
  p_redshift_max              double precision DEFAULT NULL::double precision,
  p_max_snr_min               double precision DEFAULT NULL::double precision,
  p_max_snr_max               double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min     double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max     double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any      integer          DEFAULT NULL::integer,
  p_dq_flags_include_all      integer          DEFAULT NULL::integer,
  p_dq_flags_exclude          integer          DEFAULT NULL::integer,
  p_list_ids                  integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode             text             DEFAULT 'any'::text,
  p_search                    text             DEFAULT NULL::text,
  p_inspected_only            boolean          DEFAULT NULL::boolean,
  p_needs_review              boolean          DEFAULT NULL::boolean,
  p_has_photometry            boolean          DEFAULT NULL::boolean,
  p_comment_search            text             DEFAULT NULL::text,
  p_comment_search_scope      text             DEFAULT NULL::text,
  p_comment_user_id           uuid             DEFAULT NULL::uuid,
  p_coord_ra                  double precision DEFAULT NULL::double precision,
  p_coord_dec                 double precision DEFAULT NULL::double precision,
  p_radius_degrees            double precision DEFAULT NULL::double precision,
  p_sort_column               text             DEFAULT 'target_id'::text,
  p_sort_direction            text             DEFAULT 'asc'::text,
  p_page                      integer          DEFAULT 1,
  p_page_size                 integer          DEFAULT 50,
  p_include_thumbnails        boolean          DEFAULT false,
  p_include_unpublished       boolean          DEFAULT false,
  p_line                      text             DEFAULT NULL::text,
  p_line_snr_min              double precision DEFAULT NULL::double precision,
  p_line_snr_max              double precision DEFAULT NULL::double precision,
  p_line_include_stale        boolean          DEFAULT false,
  p_band                      text             DEFAULT NULL::text,
  p_band_mag_min              double precision DEFAULT NULL::double precision,
  p_band_mag_max              double precision DEFAULT NULL::double precision,
  p_band_snr_min              double precision DEFAULT NULL::double precision,
  p_band_snr_max              double precision DEFAULT NULL::double precision,
  p_include_count             boolean          DEFAULT true,
  p_after_sort_text           text             DEFAULT NULL::text,
  p_after_sort_num            double precision DEFAULT NULL::double precision,
  p_after_tiebreak            text[]           DEFAULT NULL::text[],
  p_spectrum_ids              integer[]        DEFAULT NULL::integer[],
  p_measurements_display_only boolean          DEFAULT false
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'spectrum_id';
  END IF;

  IF v_coord_search_active AND p_sort_column IN ('target_id', 'spectrum_id') AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- T2-F: which cursor slot the resolved sort column lives in. Every other
  -- whitelisted column is numeric (double precision, or integer cast to it).
  v_sort_is_text := p_sort_column IN ('target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'grating');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 3);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

  -- Emission-line filter: the spectra with a measurement of p_line in the S/N
  -- bounds, materialized once per call (see spectra_matching_line_filter).
  IF p_line IS NOT NULL THEN
    v_line_spectrum_ids := ARRAY(SELECT public.spectra_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false)));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;

  -- filtered_spectra / distance_filtered are NOT MATERIALIZED (T2-F follow-up,
  -- #511): with the count skipped (every cursor page, every web page after the
  -- first) the count subquery folds away and the page is one scan with a
  -- top-N sort, instead of the ~80 k-row filtered set being materialized to
  -- temp (~32 MB) on every page as it was when the CTE was shared with the
  -- count. With the count requested (page 1) the filter runs twice — a scan
  -- each for the count and the page — which costs about what the spill did.
  -- The CTE carries NO thumbnail columns: the two SVGs are ~1.5 kB per row
  -- (103 MB temp spill per render before T1-5 / #501); they are joined onto
  -- the <= p_page_size page rows at the end, only when p_include_thumbnails.
  RETURN QUERY
  WITH filtered_spectra AS NOT MATERIALIZED (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      -- Phase D: redshift / redshift_quality / inspected flags now live on the
      -- parent object. LEFT JOIN so spectra whose target has no object FK
      -- (shouldn't happen post-reconcile, but safe) still appear.
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.last_inspected_at,
      o.last_inspected_by,
      o.is_active AS object_is_active,
      o.has_photometry AS object_has_photometry,
      o.object_id AS parent_object_id,
      t.max_snr,
      t.max_exposure_time,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_pk,
      s.spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.redshift_auto,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      s.file_hash,
      s.file_size,
      -- the spectrum's S/N in the filtered line (sort key + 'Line S/N' column)
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      -- the parent object's magnitude and S/N in the filtered band (sort keys
      -- + 'Mag' / 'Band S/N' columns). Object-level: photometry belongs to the
      -- sky position, so every grating of one object reports the same pair.
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      -- Hide spectra whose parent object was soft-deleted.
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR p_measurements_display_only OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR t.object_id = ANY(v_band_object_ids))
      AND (p_spectrum_ids IS NULL OR s.id = ANY(p_spectrum_ids))
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.cat_spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: build the set of matching target_ids ONCE
        -- (trgm/seq scan over the tiny comments table) instead of re-probing
        -- comments per outer row. Correlated EXISTS-inside-OR can't be pulled
        -- up and re-executes per spectrum -> timeouts on broad access. See the
        -- objects path below for the object-level analogue.
        OR t.id IN (
          SELECT c.target_id FROM comments c
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS NOT MATERIALIZED (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  -- T2-F: one row = one candidate plus its sort key, materialized in exactly
  -- one of two typed slots. The ORDER BY, the keyset predicate and the
  -- returned next-cursor all read these two columns, so they cannot drift
  -- apart. p_sort_column is a constant under force_custom_plan, so each CASE
  -- folds to the single referenced column at plan time.
  keyed AS (
    SELECT df.*,
      CASE p_sort_column
        WHEN 'target_id' THEN df.target_id
        WHEN 'spectrum_id' THEN df.spectrum_id
        WHEN 'field' THEN df.field
        WHEN 'observation' THEN df.observation
        WHEN 'program_slug' THEN df.program_slug
        WHEN 'grating' THEN df.grating
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN df.distance::double precision
        WHEN 'ra' THEN df.ra::double precision
        WHEN 'dec' THEN df."dec"::double precision
        WHEN 'redshift' THEN df.redshift::double precision
        WHEN 'redshift_quality' THEN df.redshift_quality::double precision
        WHEN 'redshift_auto' THEN df.redshift_auto::double precision
        WHEN 'signal_to_noise' THEN df.signal_to_noise::double precision
        WHEN 'exposure_time' THEN df.exposure_time::double precision
        WHEN 'line_snr' THEN df.line_snr
        WHEN 'band_mag' THEN df.band_mag
        WHEN 'band_snr' THEN df.band_snr
      END AS sort_num
    FROM distance_filtered df
  ),
  -- One row past the page (p_page_size + 1) so has_more is exact; the final
  -- SELECT drops it and the cursor is taken from row p_page_size.
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset: rows strictly after the cursor in (sort key <dir> NULLS
        -- LAST, target_id, grating, spectrum_id) order. With a non-NULL cursor
        -- value that is every row whose key sorts after it plus the whole NULL
        -- tail; with a NULL cursor value (the walk is inside the tail) only
        -- NULL-keyed rows past the tiebreak. Equal keys fall through to the
        -- row-value tiebreak comparison.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          END
        )
      -- Exactly one of the four key terms is live for a given call (the other
      -- three are constant NULL); the tail makes the order total. spectrum_id
      -- was added to the tail in T2-F — (target_id, grating) alone is not
      -- unique (one grating can pair with several filters), which a keyset
      -- cursor cannot tolerate.
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.target_id ASC, k.grating ASC, k.spectrum_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  -- The row the next cursor is built from: the page's last row, and only when
  -- an overflow row proved there is a next page.
  cursor_row AS (
    SELECT pr.sort_text, pr.sort_num, pr.target_id, pr.grating, pr.spectrum_id
    FROM page_rows pr
    WHERE pr.row_num = p_page_size
      AND EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size)
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      -- Phase D: redshift fields are object-level reads
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'line_snr', r.line_snr,
      'band_mag', r.band_mag,
      'band_snr', r.band_snr,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        -- Phase D: per-spectrum auto-z and DQ
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', sth.thumbnail_svg_fnu,
        'thumbnail_svg_flambda', sth.thumbnail_svg_flambda
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    CASE WHEN p_include_count THEN (SELECT COUNT(*) FROM distance_filtered) ELSE -1::BIGINT END,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.target_id, cr.grating, cr.spectrum_id] FROM cursor_row cr)
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug
  -- Thumbnails only for the page, only when asked (constant-false join
  -- condition otherwise, which the planner drops).
  LEFT JOIN public.cat_spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean)
  FROM PUBLIC, "anon";

COMMENT ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) IS 'The ids of the objects / spectra / targets a predicate tree selects (Catalog Read Model phase 3), evaluated under the caller''s RLS against the live tables or the browsed release, plus the explicit scope (p_program_slugs / p_include_unpublished / p_viewer_id) a service-role caller must pass. Feeds the list RPCs'' p_object_ids / p_spectrum_ids.';

COMMENT ON FUNCTION "public"."catalog_query_correlations"() IS 'How an exists node joins its child table to the row it is evaluated against (Catalog Read Model phase 3): the FROM ... WHERE fragment per (parent, child), with the parent key on the parent alias.';

COMMENT ON FUNCTION
  "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
  precision, boolean, text, double precision, double precision, double precision, double precision) IS 'The objects list for a predicate tree (Catalog Read Model phase 3): catalog_match_ids() then get_filtered_objects_paginated(p_object_ids := ...), so the row shape, sort, paging and cursor are the legacy RPC''s. p_line / p_band only drive the Line S/N and Mag columns and sorts (the legacy RPC runs with p_measurements_display_only, so they never filter). The explicit scope goes through to the tree for a service-role caller.';

COMMENT ON FUNCTION
  "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
  precision, boolean, text, double precision, double precision, double precision, double precision, uuid) IS 'The spectra list for a predicate tree rooted at spectra (Catalog Read Model phase 3): catalog_match_ids() then get_filtered_spectra_paginated(p_spectrum_ids := ...).';

COMMENT ON FUNCTION "public"."catalog_query_whitelist"() IS 'The tables, columns and value types a predicate tree may name (Catalog Read Model phase 3). The only source of identifiers in the compiled SQL; mirrored by the portal''s leaf catalogue.';

COMMENT ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) IS 'Compiles a predicate tree (Catalog Read Model phase 3) to the SQL WHERE text it runs as, over the root table aliased r; every identifier from the whitelist, every value quoted. With p_program_slugs the caller''s explicit access scope is appended to every table the tree touches (the bearer routes, whose client RLS does not gate); without it RLS alone decides. Raises 22023 (invalid_parameter_value) on anything the whitelist does not allow.';

GRANT EXECUTE ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_match_ids"(text, jsonb, text[], boolean, uuid) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_array_literal"(jsonb, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_array_literal"(jsonb, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_array_literal"(jsonb, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_array_literal"(jsonb, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_correlations"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_correlations"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_correlations"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_correlations"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_literal"(jsonb, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_literal"(jsonb, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_literal"(jsonb, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_literal"(jsonb, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_node_sql"(jsonb, text, text, text, integer, integer, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_node_sql"(jsonb, text, text, text, integer, integer, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_node_sql"(jsonb, text, text, text, integer, integer, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_node_sql"(jsonb, text, text, text, integer, integer, jsonb) TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision)
  TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_scope_sql"(text, text, text, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_scope_sql"(text, text, text, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_scope_sql"(text, text, text, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_scope_sql"(text, text, text, jsonb) TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, uuid)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, uuid)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, uuid)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, uuid)
  TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_whitelist"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_query_whitelist"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_whitelist"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_query_whitelist"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_tree_sql"(jsonb, text, text[], boolean, uuid) TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean)
  TO "service_role";
