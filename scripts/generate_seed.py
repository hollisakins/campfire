#!/usr/bin/env python3
"""
Generate Supabase seed data from production database.

Queries the production Supabase for a representative subset of real objects
(with files already in R2) and generates supabase/seed.sql for local development.

Usage:
    python scripts/generate_seed.py
    python scripts/generate_seed.py --objects-per-program 10
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime

try:
    import tomllib
except ImportError:
    import tomli as tomllib

try:
    from supabase import create_client
except ImportError:
    print("Error: supabase-py not installed. Install with: pip install supabase")
    sys.exit(1)


# === Configuration Loading (reused from deploy.py) ===

def load_toml(path: Path) -> dict:
    """Load a TOML file."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_config() -> dict:
    """Load deployment configuration from $CAMPFIRE_ROOT/config/deploy.toml."""
    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    if not campfire_root:
        print("Error: $CAMPFIRE_ROOT environment variable is not set.")
        sys.exit(1)
    config_path = Path(campfire_root) / 'config' / 'deploy.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    return load_toml(config_path)


def load_programs() -> list[dict]:
    """Load program definitions from $CAMPFIRE_ROOT/config/programs.toml.

    File format: each top-level key is the program slug.
    Returns list of dicts, each with a 'slug' key injected.
    """
    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    if not campfire_root:
        print("Error: $CAMPFIRE_ROOT environment variable is not set.")
        sys.exit(1)
    programs_path = Path(campfire_root) / 'config' / 'programs.toml'
    if not programs_path.exists():
        print(f"Error: Programs file not found: {programs_path}")
        sys.exit(1)
    data = load_toml(programs_path)
    return [{**info, 'slug': slug} for slug, info in data.items()]


# === SQL Escaping ===

def sql_escape(value) -> str:
    """Escape a value for SQL insertion."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Escape single quotes by doubling them
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, list):
        # PostgreSQL array literal
        if not value:
            return "'{}'::integer[]"
        if isinstance(value[0], int):
            items = ', '.join(str(v) for v in value)
            return f"ARRAY[{items}]"
        items = ', '.join(sql_escape(v) for v in value)
        return f"ARRAY[{items}]"
    return sql_escape(str(value))


def sql_value_or_null(row: dict, key: str) -> str:
    """Get a SQL-escaped value from a dict, returning NULL if missing."""
    return sql_escape(row.get(key))


# === Test Users ===

ADMIN_UUID = '11111111-1111-1111-1111-111111111111'
USER_UUID = '22222222-2222-2222-2222-222222222222'
VIEWER_UUID = '33333333-3333-3333-3333-333333333333'

# Password will be set using crypt() function in SQL
# This ensures compatibility with Supabase Auth's bcrypt implementation
# Note: gen_salt is in the extensions schema
PASSWORD_SQL = "extensions.crypt('password123', extensions.gen_salt('bf'))"

TEST_USERS = [
    {
        'id': ADMIN_UUID,
        'email': 'admin@campfire.dev',
        'full_name': 'Admin User',
        'is_admin': True,
        'can_comment': True,
    },
    {
        'id': USER_UUID,
        'email': 'user@campfire.dev',
        'full_name': 'Regular User',
        'is_admin': False,
        'can_comment': True,
    },
    {
        'id': VIEWER_UUID,
        'email': 'viewer@campfire.dev',
        'full_name': 'Viewer User',
        'is_admin': False,
        'can_comment': False,
    },
]


# === Flag Definitions (from web/lib/flags.ts) ===

FLAG_DEFINITIONS = [
    # Redshift quality (enum, not bitmask)
    ('redshift_quality', None, 0, 'Not Inspected', 'None', '⚪', '#e0e0e0', 'Not yet visually inspected'),
    ('redshift_quality', None, 1, 'Impossible', 'Bad', '🔴', '#dc3545', 'Impossible to determine redshift from available data'),
    ('redshift_quality', None, 2, 'Tentative', 'Tent.', '🟠', '#ffc107', 'Redshift uncertain but plausible (~50% confidence)'),
    ('redshift_quality', None, 3, 'Probable', 'Prob.', '🟡', '#ff9800', 'Redshift likely correct (~80% confidence)'),
    ('redshift_quality', None, 4, 'Secure', 'Secure', '🟢', '#28a745', 'Redshift definitely correct (>95% confidence)'),
    # Spectral features (bitmask)
    ('spectral_features', 0, 1, 'Continuum Shape', 'Cont', '📊', '#e8f5e9', 'Redshift constrained by the overall continuum shape'),
    ('spectral_features', 1, 2, 'Lyman Break', 'LB', '💧', '#e3f2fd', 'Clear Lyman break'),
    ('spectral_features', 2, 4, 'Balmer Break', 'BB', '📈', '#f3e5f5', 'Clear Balmer break'),
    ('spectral_features', 3, 8, 'Absorption Features', 'ABS', '〰️', '#f1f8e9', 'Absorption lines/features identified'),
    ('spectral_features', 4, 16, 'Single Emission Line', '1EM', '☝️', '#fff3e0', 'Single emission line'),
    ('spectral_features', 5, 32, 'Multiple Emission Lines', 'MEM', '✌️', '#ffebee', 'Multiple emission lines'),
    # Object flags (bitmask)
    ('object_flags', 0, 1, 'Little Red Dot', 'LRD', '🔴', '#ffcccb', 'Little red dot'),
    ('object_flags', 1, 2, 'Broad Line', 'BL', '🌋', '#c8e6c9', 'Broad emission line'),
    ('object_flags', 2, 4, 'Lyα Emitter', 'LAE', '✨', '#bbdefb', 'Strong Lyman-alpha emission'),
    ('object_flags', 3, 8, 'Balmer Break Galaxy', 'BBG', '🌌', '#e1bee7', 'Strong Balmer break indicating evolved stellar population'),
    ('object_flags', 4, 16, '[OIII] Emitter', 'O3E', '⚡️', '#fff59d', 'Strong [OIII]4959,5007 emitter'),
    ('object_flags', 5, 32, 'Hα Emitter', 'HAE', '🔥', '#f398ad', 'Strong H-alpha emitter'),
    ('object_flags', 6, 64, 'Quiescent', 'QG', '😴', '#d7ccc8', 'Quiescent galaxy with little star formation'),
    ('object_flags', 7, 128, 'Dusty', 'DUST', '🌫️', '#ffccbc', 'Significant dust attenuation'),
    ('object_flags', 8, 256, 'Star', 'STAR', '⭐', '#ffeb3b', 'Stellar spectrum'),
    # DQ flags (bitmask)
    ('dq_flags', 0, 1, 'Chip Gap', 'GAP', '⚠️', '#fff9c4', 'Spectrum affected by detector chip gap'),
    ('dq_flags', 1, 2, 'Contamination', 'CONTAM', '🚫', '#ffe0b2', 'Contamination from nearby source or open shutter'),
    ('dq_flags', 2, 4, 'Stuck Closed Shutter', 'CLOSED', '🔒', '#ffcdd2', 'Possible stuck closed shutter'),
    ('dq_flags', 3, 8, 'Multiple Sources', 'MULT', '👥', '#b3e5fc', 'Multiple sources in slitlet'),
    ('dq_flags', 4, 16, 'No Detection', 'NONE', '❌', '#e0e0e0', 'No source detected in spectrum'),
    ('dq_flags', 5, 32, 'Low S/N', 'SNR', '📉', '#ffecb3', 'Low signal-to-noise ratio'),
    ('dq_flags', 6, 64, 'Spectral Overlap', 'OVER', '🔗', '#f3e5f5', 'Spectral overlap in grating spectrum'),
    ('dq_flags', 7, 128, 'PRISM Corrupted', 'P-BAD', '🌈❌', '#ffccbc', 'PRISM data corrupted or unusable'),
    ('dq_flags', 8, 256, 'Grating Corrupted', 'G-BAD', '🔴❌', '#ffcdd2', 'Grating data corrupted or unusable'),
]


# === PID → slug mapping (production still uses program_id) ===

PID_TO_SLUG = {
    6368: 'capers', 7076: 'ember', 7417: 'zenith', 6585: 'cosmos_ddt',
    5224: 'mom', 4233: 'rubies', 1345: 'ceers', 2750: 'ceers_ddt',
    9214: 'spurs', 2561: 'uncover', 1214: 'gto_wide', 1213: 'gto_wide',
    8018: 'diver', 8410: 'oceans', 5997: 'oasis', 3543: 'excels',
    4287: 'egs_bubbles', 3215: 'jades', 1433: 'macs0647jd_coe',
}


# === Query Production Data ===

def select_objects(supabase, objects_per_program: int) -> list[dict]:
    """
    Select a representative subset of objects from production.

    Production still uses program_id (integer). We query by program_id
    and map to program_slug locally for the new schema.

    For each program, picks objects with variety across quality levels:
    - 1-2 with quality 4 (secure) with flags set
    - 1 with quality 2-3 (tentative/probable)
    - 1 with quality 0 (uninspected)
    - 1 with quality 1 (impossible) if available
    """
    # Get all distinct program_ids from production
    programs_resp = supabase.table('objects').select('program_id').execute()
    program_ids = sorted(set(row['program_id'] for row in programs_resp.data))

    print(f"Found {len(program_ids)} programs in production: {program_ids}")

    all_objects = []
    seen_ids = set()

    for pid in program_ids:
        program_objects = []

        # Quality 4 (secure) - prefer objects with flags set
        q4 = supabase.table('objects').select('*') \
            .eq('program_id', pid) \
            .eq('redshift_quality', 4) \
            .gt('spectral_features', 0) \
            .limit(2) \
            .execute()
        for obj in q4.data:
            if obj['object_id'] not in seen_ids:
                program_objects.append(obj)
                seen_ids.add(obj['object_id'])

        # If we didn't get 2 quality-4, get more without flag requirement
        if len([o for o in program_objects if o['redshift_quality'] == 4]) < 2:
            q4b = supabase.table('objects').select('*') \
                .eq('program_id', pid) \
                .eq('redshift_quality', 4) \
                .limit(2) \
                .execute()
            for obj in q4b.data:
                if obj['object_id'] not in seen_ids:
                    program_objects.append(obj)
                    seen_ids.add(obj['object_id'])
                    if len(program_objects) >= 2:
                        break

        # Quality 2-3 (tentative/probable)
        for q in [3, 2]:
            qn = supabase.table('objects').select('*') \
                .eq('program_id', pid) \
                .eq('redshift_quality', q) \
                .limit(1) \
                .execute()
            for obj in qn.data:
                if obj['object_id'] not in seen_ids:
                    program_objects.append(obj)
                    seen_ids.add(obj['object_id'])
                    break

        # Quality 0 (uninspected)
        q0 = supabase.table('objects').select('*') \
            .eq('program_id', pid) \
            .eq('redshift_quality', 0) \
            .limit(1) \
            .execute()
        for obj in q0.data:
            if obj['object_id'] not in seen_ids:
                program_objects.append(obj)
                seen_ids.add(obj['object_id'])

        # Quality 1 (impossible)
        q1 = supabase.table('objects').select('*') \
            .eq('program_id', pid) \
            .eq('redshift_quality', 1) \
            .limit(1) \
            .execute()
        for obj in q1.data:
            if obj['object_id'] not in seen_ids:
                program_objects.append(obj)
                seen_ids.add(obj['object_id'])

        # Cap at objects_per_program
        program_objects = program_objects[:objects_per_program]

        slug = PID_TO_SLUG.get(pid, f'unknown_{pid}')
        print(f"  Program {pid} ({slug}): selected {len(program_objects)} objects "
              f"(qualities: {[o['redshift_quality'] for o in program_objects]})")
        all_objects.extend(program_objects)

    # Map production fields to new schema fields
    for obj in all_objects:
        pid = obj['program_id']
        obj['program_slug'] = PID_TO_SLUG.get(pid, f'unknown_{pid}')
        # observation is a generated column in production — keep it as-is

    return all_objects


def build_observations_from_objects(objects: list[dict]) -> list[dict]:
    """Build observations records from selected objects (production has no observations table)."""
    seen = set()
    observations = []
    for obj in objects:
        obs_name = obj.get('observation', '')
        if not obs_name or obs_name in seen:
            continue
        seen.add(obs_name)
        observations.append({
            'name': obs_name,
            'program_slug': obj['program_slug'],
            'jwst_program_id': obj['program_id'],
            'field': obj['field'],
        })
    return observations


def fetch_spectra(supabase, object_ids: list[str]) -> list[dict]:
    """Fetch all spectra for the given object_ids."""
    if not object_ids:
        return []

    all_spectra = []
    # Batch to avoid URL length limits
    batch_size = 50
    for i in range(0, len(object_ids), batch_size):
        batch = object_ids[i:i + batch_size]
        resp = supabase.table('spectra').select('*').in_('object_id', batch).execute()
        all_spectra.extend(resp.data)

    return all_spectra


def fetch_comments(supabase, object_int_ids: list[int]) -> list[dict]:
    """Fetch comments for the given object integer IDs."""
    if not object_int_ids:
        return []

    all_comments = []
    batch_size = 50
    for i in range(0, len(object_int_ids), batch_size):
        batch = object_int_ids[i:i + batch_size]
        resp = supabase.table('comments').select('*').in_('object_id', batch).execute()
        all_comments.extend(resp.data)

    return all_comments


def fetch_flag_audit_log(supabase, object_int_ids: list[int]) -> list[dict]:
    """Fetch flag audit log entries for the given object integer IDs."""
    if not object_int_ids:
        return []

    all_entries = []
    batch_size = 50
    for i in range(0, len(object_int_ids), batch_size):
        batch = object_int_ids[i:i + batch_size]
        resp = supabase.table('flag_audit_log').select('*').in_('object_id', batch).execute()
        all_entries.extend(resp.data)

    return all_entries


# === SQL Generation ===

def generate_auth_users_sql() -> str:
    """Generate INSERT statements for auth.users test accounts."""
    lines = ['-- ============================================']
    lines.append('-- 1. Auth Users (test accounts)')
    lines.append('-- ============================================')
    lines.append('')

    for user in TEST_USERS:
        lines.append(f"""INSERT INTO auth.users (
    id, instance_id, aud, role, email,
    encrypted_password, email_confirmed_at,
    created_at, updated_at, confirmation_token,
    recovery_token, email_change_token_new, email_change
) VALUES (
    {sql_escape(user['id'])},
    '00000000-0000-0000-0000-000000000000',
    'authenticated',
    'authenticated',
    {sql_escape(user['email'])},
    {PASSWORD_SQL},
    NOW(),
    NOW(),
    NOW(),
    '',
    '',
    '',
    ''
);""")
        lines.append('')

        # Also insert identity for each user
        lines.append(f"""INSERT INTO auth.identities (
    id, user_id, identity_data, provider, provider_id,
    last_sign_in_at, created_at, updated_at
) VALUES (
    gen_random_uuid(),
    {sql_escape(user['id'])},
    jsonb_build_object('sub', {sql_escape(user['id'])}, 'email', {sql_escape(user['email'])}),
    'email',
    {sql_escape(user['id'])},
    NOW(),
    NOW(),
    NOW()
);""")
        lines.append('')

    return '\n'.join(lines)


def generate_programs_sql(programs: list[dict]) -> str:
    """Generate INSERT statements for programs."""
    lines = ['-- ============================================']
    lines.append('-- 2. Programs')
    lines.append('-- ============================================')
    lines.append('')

    for p in programs:
        lines.append(f"""INSERT INTO public.programs (slug, program_name, pi_name, description, cycle, is_public)
VALUES ({sql_escape(p['slug'])}, {sql_escape(p['program_name'])}, {sql_escape(p['pi_name'])}, {sql_escape(p.get('description', ''))}, {sql_escape(p.get('cycle'))}, {sql_escape(p.get('is_public', False))});""")

    lines.append('')
    return '\n'.join(lines)


def generate_observations_sql(observations: list[dict]) -> str:
    """Generate INSERT statements for observations."""
    lines = ['-- ============================================']
    lines.append('-- 2b. Observations')
    lines.append('-- ============================================')
    lines.append('')

    for obs in observations:
        lines.append(f"""INSERT INTO public.observations (name, program_slug, jwst_program_id, field)
VALUES ({sql_escape(obs['name'])}, {sql_escape(obs['program_slug'])}, {obs['jwst_program_id']}, {sql_escape(obs['field'])});""")

    lines.append('')
    return '\n'.join(lines)


def generate_flag_definitions_sql() -> str:
    """Generate INSERT statements for flag_definitions."""
    lines = ['-- ============================================']
    lines.append('-- 3. Flag Definitions')
    lines.append('-- ============================================')
    lines.append('')

    for cat, bit, val, label, short, icon, color, desc in FLAG_DEFINITIONS:
        bit_sql = str(bit) if bit is not None else 'NULL'
        lines.append(f"""INSERT INTO public.flag_definitions (category, bit_position, value, label, short_label, icon, color, description)
VALUES ({sql_escape(cat)}, {bit_sql}, {val}, {sql_escape(label)}, {sql_escape(short)}, {sql_escape(icon)}, {sql_escape(color)}, {sql_escape(desc)});""")

    lines.append('')
    return '\n'.join(lines)


def generate_user_profiles_sql() -> str:
    """Generate INSERT statements for user_profiles."""
    lines = ['-- ============================================']
    lines.append('-- 4. User Profiles')
    lines.append('-- ============================================')
    lines.append('')

    for user in TEST_USERS:
        lines.append(f"""INSERT INTO public.user_profiles (user_id, full_name, is_admin, can_comment)
VALUES ({sql_escape(user['id'])}, {sql_escape(user['full_name'])}, {sql_escape(user['is_admin'])}, {sql_escape(user['can_comment'])});""")

    lines.append('')
    return '\n'.join(lines)


def generate_objects_sql(objects: list[dict]) -> str:
    """Generate INSERT statements for objects (skipping generated columns: redshift, max_snr)."""
    lines = ['-- ============================================']
    lines.append('-- 5. Objects (from production)')
    lines.append('-- ============================================')
    lines.append('')

    for obj in objects:
        # Remap last_inspected_by to admin test user if set
        inspected_by = sql_escape(ADMIN_UUID) if obj.get('last_inspected_by') else 'NULL'
        inspected_at = sql_escape(obj.get('last_inspected_at')) if obj.get('last_inspected_at') else 'NULL'

        # Handle redshift_inspected (numeric type)
        redshift_inspected = obj.get('redshift_inspected')
        if redshift_inspected is not None:
            redshift_inspected_sql = str(redshift_inspected)
        else:
            redshift_inspected_sql = 'NULL'

        lines.append(f"""INSERT INTO public.objects (id, object_id, program_slug, observation, field, ra, dec, redshift_auto, redshift_inspected, redshift_quality, spectral_features, object_flags, dq_flags, last_inspected_at, last_inspected_by, has_sed_plot)
VALUES ({obj['id']}, {sql_escape(obj['object_id'])}, {sql_escape(obj['program_slug'])}, {sql_escape(obj.get('observation', ''))}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {obj.get('spectral_features', 0)}, {obj.get('object_flags', 0)}, {obj.get('dq_flags', 0)}, {inspected_at}, {inspected_by}, {sql_escape(obj.get('has_sed_plot', False))});""")

    lines.append('')
    return '\n'.join(lines)


def generate_spectra_sql(spectra: list[dict]) -> str:
    """Generate INSERT statements for spectra."""
    lines = ['-- ============================================']
    lines.append('-- 6. Spectra (from production)')
    lines.append('-- ============================================')
    lines.append('')

    for spec in spectra:
        lines.append(f"""INSERT INTO public.spectra (id, object_id, grating, fits_path, reduction_version, signal_to_noise, thumbnail_svg_fnu, thumbnail_svg_flambda)
VALUES ({spec['id']}, {sql_escape(spec['object_id'])}, {sql_escape(spec['grating'])}, {sql_escape(spec['fits_path'])}, {sql_escape(spec.get('reduction_version', 'v0.1'))}, {sql_escape(spec.get('signal_to_noise'))}, {sql_escape(spec.get('thumbnail_svg_fnu'))}, {sql_escape(spec.get('thumbnail_svg_flambda'))});""")

    lines.append('')
    return '\n'.join(lines)


def generate_user_program_access_sql(programs: list[dict]) -> str:
    """Generate INSERT statements for user_program_access."""
    lines = ['-- ============================================']
    lines.append('-- 7. User Program Access')
    lines.append('-- ============================================')
    lines.append('')

    # Admin gets all programs
    for p in programs:
        lines.append(f"""INSERT INTO public.user_program_access (user_id, program_slug)
VALUES ({sql_escape(ADMIN_UUID)}, {sql_escape(p['slug'])});""")

    # Regular user gets public programs only
    for p in programs:
        if p.get('is_public', False):
            lines.append(f"""INSERT INTO public.user_program_access (user_id, program_slug)
VALUES ({sql_escape(USER_UUID)}, {sql_escape(p['slug'])});""")

    # Viewer gets public programs
    for p in programs:
        if p.get('is_public', False):
            lines.append(f"""INSERT INTO public.user_program_access (user_id, program_slug)
VALUES ({sql_escape(VIEWER_UUID)}, {sql_escape(p['slug'])});""")

    lines.append('')
    return '\n'.join(lines)


def generate_comments_sql(comments: list[dict], object_id_map: dict[int, int]) -> str:
    """Generate INSERT statements for comments."""
    lines = ['-- ============================================']
    lines.append('-- 8. Comments')
    lines.append('-- ============================================')
    lines.append('')

    for comment in comments:
        # Remap object_id (integer) and user_id to test users
        obj_int_id = comment['object_id']
        if obj_int_id not in object_id_map:
            continue

        lines.append(f"""INSERT INTO public.comments (id, object_id, user_id, content, created_at, is_deleted)
VALUES ({comment['id']}, {obj_int_id}, {sql_escape(ADMIN_UUID)}, {sql_escape(comment['content'])}, {sql_escape(comment.get('created_at', 'now()'))}, {sql_escape(comment.get('is_deleted', False))});""")

    # Add a sample comment if none exist
    if not comments:
        lines.append("-- No comments found in production for selected objects")
        lines.append("-- Adding sample comments")

    lines.append('')
    return '\n'.join(lines)


def generate_flag_audit_log_sql(entries: list[dict], object_id_map: dict[int, int]) -> str:
    """Generate INSERT statements for flag_audit_log."""
    lines = ['-- ============================================']
    lines.append('-- 9. Flag Audit Log')
    lines.append('-- ============================================')
    lines.append('')

    for entry in entries:
        obj_int_id = entry['object_id']
        if obj_int_id not in object_id_map:
            continue

        lines.append(f"""INSERT INTO public.flag_audit_log (id, object_id, user_id, field_name, old_value, new_value, changed_at)
VALUES ({entry['id']}, {obj_int_id}, {sql_escape(ADMIN_UUID)}, {sql_escape(entry['field_name'])}, {sql_escape(entry.get('old_value'))}, {sql_escape(entry.get('new_value'))}, {sql_escape(entry.get('changed_at', 'now()'))});""")

    if not entries:
        lines.append("-- No flag audit entries found in production for selected objects")

    lines.append('')
    return '\n'.join(lines)


def generate_access_codes_sql() -> str:
    """Generate INSERT statements for access_codes."""
    lines = ['-- ============================================']
    lines.append('-- 10. Access Codes')
    lines.append('-- ============================================')
    lines.append('')

    lines.append(f"""INSERT INTO public.access_codes (code, description, grants_all_programs, is_active, created_by)
VALUES ('CAMPFIRE-DEV', 'Development access code - grants all programs', TRUE, TRUE, {sql_escape(ADMIN_UUID)});""")
    lines.append(f"""INSERT INTO public.access_codes (code, description, grants_all_programs, program_slugs, is_active, created_by)
VALUES ('EMBER-ACCESS', 'EMBER program access code', FALSE, ARRAY['ember'], TRUE, {sql_escape(ADMIN_UUID)});""")

    lines.append('')
    return '\n'.join(lines)


def generate_sequence_resets(objects: list[dict], spectra: list[dict],
                             comments: list[dict], flag_entries: list[dict]) -> str:
    """Generate sequence reset statements."""
    lines = ['-- ============================================']
    lines.append('-- 11. Materialized View Refresh')
    lines.append('-- ============================================')
    lines.append('')
    lines.append('REFRESH MATERIALIZED VIEW public.mv_filter_options;')
    lines.append('REFRESH MATERIALIZED VIEW public.mv_programs_overview;')
    lines.append('')
    lines.append('-- ============================================')
    lines.append('-- 12. Reset Sequences')
    lines.append('-- ============================================')
    lines.append('')

    # Objects
    max_obj_id = max((o['id'] for o in objects), default=0)
    lines.append(f"SELECT setval('public.objects_id_seq', {max_obj_id + 1}, false);")

    # Spectra
    max_spec_id = max((s['id'] for s in spectra), default=0)
    lines.append(f"SELECT setval('public.spectra_id_seq', {max_spec_id + 1}, false);")

    # Comments
    max_comment_id = max((c['id'] for c in comments), default=0)
    if max_comment_id > 0:
        lines.append(f"SELECT setval('public.comments_id_seq', {max_comment_id + 1}, false);")

    # Flag audit log
    max_audit_id = max((e['id'] for e in flag_entries), default=0)
    if max_audit_id > 0:
        lines.append(f"SELECT setval('public.flag_audit_log_id_seq', {max_audit_id + 1}, false);")

    lines.append('')
    return '\n'.join(lines)


# === Main ===

def main():
    parser = argparse.ArgumentParser(
        description='Generate Supabase seed data from production database'
    )
    parser.add_argument(
        '--objects-per-program', type=int, default=5,
        help='Maximum objects to select per program (default: 5)'
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    output_path = project_root / 'supabase' / 'seed.sql'

    # Load configuration
    print("Loading configuration...")
    config = load_config()
    programs = load_programs()

    # Connect to production Supabase
    print("Connecting to production Supabase...")
    supabase = create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key']
    )

    # Select representative objects
    print(f"\nSelecting up to {args.objects_per_program} objects per program...")
    objects = select_objects(supabase, args.objects_per_program)
    print(f"\nTotal objects selected: {len(objects)}")

    if not objects:
        print("Error: No objects found in production database!")
        sys.exit(1)

    # Inject synthetic GTO WIDE EGS objects (PID 1213 has no production data yet)
    # so we can test the multi-PID merge: gto_wide = {1213 (egs), 1214 (cosmos)}
    max_id = max(o['id'] for o in objects) + 1000
    gto_wide_cosmos = [o for o in objects if o.get('program_id') == 1214]
    if gto_wide_cosmos and not any(o.get('program_id') == 1213 for o in objects):
        print("\n  Injecting synthetic GTO WIDE EGS objects (PID 1213)...")
        template = gto_wide_cosmos[0]
        for i, sid in enumerate([90001, 90002, 90003]):
            synth = {
                'id': max_id + i,
                'object_id': f'gto_wide_egs_p1_{sid}',
                'program_id': 1213,
                'program_slug': 'gto_wide',
                'field': 'egs',
                'observation': 'gto_wide_egs_p1',
                'ra': 214.8 + i * 0.01,
                'dec': 52.8 + i * 0.01,
                'redshift_auto': 2.0 + i * 0.5,
                'redshift_inspected': None,
                'redshift_quality': [4, 2, 0][i],
                'spectral_features': 0,
                'object_flags': 0,
                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
                'has_sed_plot': False,
                'max_snr': None,
                'max_exposure_time': None,
            }
            objects.append(synth)
        print(f"    Added 3 synthetic objects (gto_wide_egs_p1)")

    # Build maps
    object_ids = [o['object_id'] for o in objects]
    object_int_ids = [o['id'] for o in objects]
    object_id_map = {o['id']: o['id'] for o in objects}  # identity map (keep original IDs)

    # Build observations from objects (production has no observations table yet)
    observations = build_observations_from_objects(objects)
    print(f"  Built {len(observations)} observation records")

    print("Fetching spectra...")
    spectra = fetch_spectra(supabase, object_ids)
    print(f"  Found {len(spectra)} spectra")

    print("Fetching comments...")
    comments = fetch_comments(supabase, object_int_ids)
    print(f"  Found {len(comments)} comments")

    print("Fetching flag audit log...")
    flag_entries = fetch_flag_audit_log(supabase, object_int_ids)
    print(f"  Found {len(flag_entries)} audit entries")

    # Generate SQL
    print(f"\nGenerating {output_path}...")

    sql_parts = []

    # Header
    sql_parts.append(f"""-- ============================================
-- CAMPFIRE Seed Data
-- Generated: {datetime.now().isoformat()}
-- Objects: {len(objects)} | Observations: {len(observations)} | Spectra: {len(spectra)}
-- Comments: {len(comments)} | Audit Entries: {len(flag_entries)}
--
-- Test Users:
--   admin@campfire.dev / password123 (admin, all programs)
--   user@campfire.dev  / password123 (regular, public programs)
--   viewer@campfire.dev / password123 (read-only, public programs)
-- ============================================

-- Migration sets search_path to empty; restore it for seed
SET search_path TO public, auth, extensions;

""")

    sql_parts.append(generate_auth_users_sql())
    sql_parts.append(generate_programs_sql(programs))
    sql_parts.append(generate_observations_sql(observations))
    sql_parts.append(generate_flag_definitions_sql())
    sql_parts.append(generate_user_profiles_sql())
    sql_parts.append(generate_objects_sql(objects))
    sql_parts.append(generate_spectra_sql(spectra))
    sql_parts.append(generate_user_program_access_sql(programs))
    sql_parts.append(generate_comments_sql(comments, object_id_map))
    sql_parts.append(generate_flag_audit_log_sql(flag_entries, object_id_map))
    sql_parts.append(generate_access_codes_sql())
    sql_parts.append(generate_sequence_resets(objects, spectra, comments, flag_entries))

    # Write output
    full_sql = '\n'.join(sql_parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(full_sql)

    print(f"\nSeed file written to: {output_path}")
    print(f"  Size: {len(full_sql):,} bytes")
    print(f"\nTo apply: cd supabase && supabase db reset")


if __name__ == '__main__':
    main()
