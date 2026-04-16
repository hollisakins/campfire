#!/usr/bin/env python3
"""
Generate Supabase seed data from production database.

Queries the production Supabase for a representative subset of real targets
(with files already in R2) and generates supabase/seed.sql for local dev
and preview branch seeding.

The output seed.sql is a committed artifact checked into the repo. Supabase's
GitHub integration uses it to seed preview branches automatically when PRs are
opened, and locally it's applied via `supabase db reset`. Because of this,
seed.sql must stay compatible with the current migration state — if a migration
changes the schema in a way that breaks seed inserts (e.g. renaming a table,
adding a NOT NULL column without a default), regenerate the seed file.

This script requires a live connection to the production Supabase instance
(via $CAMPFIRE_ROOT/config/deploy.toml), but the generated seed.sql does not
contain any production credentials or sensitive data — just a small stratified
sample of scientific data and synthetic test user accounts.

Targets are cross-matched into `objects` table entries in-process (per-field
friends-of-friends via `campfire.deploy.objects`), so no `cfdeploy objects`
follow-up is needed after `supabase db reset`.

Usage:
    python scripts/generate_seed.py
    python scripts/generate_seed.py --objects-per-program 10
"""

import argparse
import os
import sys
from collections import defaultdict
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

try:
    from campfire.deploy.objects import cluster_targets, build_objects
except ImportError:
    print("Error: campfire package not installed. Install with: "
          "pip install -e ./python[deploy]")
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
        'username': 'admin',
        'full_name': 'Admin User',
        'is_admin': True,
        'can_comment': True,
    },
    {
        'id': USER_UUID,
        'email': 'user@campfire.dev',
        'username': 'user',
        'full_name': 'Regular User',
        'is_admin': False,
        'can_comment': True,
    },
    {
        'id': VIEWER_UUID,
        'email': 'viewer@campfire.dev',
        'username': 'viewer',
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


# === Query Production Data ===

def select_targets(supabase, targets_per_program: int, program_slugs: list[str]) -> list[dict]:
    """
    Select a representative subset of targets from production.

    For each program, picks targets with variety across quality levels:
    - 1-2 with quality 4 (secure) with flags set
    - 1 with quality 2-3 (tentative/probable)
    - 1 with quality 0 (uninspected)
    - 1 with quality 1 (impossible) if available
    """
    slugs = sorted(program_slugs)

    print(f"Sampling from {len(slugs)} programs: {slugs}")

    all_targets = []
    seen_ids = set()

    for slug in slugs:
        program_targets = []

        # Quality 4 (secure) - prefer targets with flags set
        q4 = supabase.table('targets').select('*') \
            .eq('program_slug', slug) \
            .eq('redshift_quality', 4) \
            .gt('spectral_features', 0) \
            .limit(2) \
            .execute()
        for t in q4.data:
            if t['target_id'] not in seen_ids:
                program_targets.append(t)
                seen_ids.add(t['target_id'])

        # If we didn't get 2 quality-4, get more without flag requirement
        if len([t for t in program_targets if t['redshift_quality'] == 4]) < 2:
            q4b = supabase.table('targets').select('*') \
                .eq('program_slug', slug) \
                .eq('redshift_quality', 4) \
                .limit(2) \
                .execute()
            for t in q4b.data:
                if t['target_id'] not in seen_ids:
                    program_targets.append(t)
                    seen_ids.add(t['target_id'])
                    if len(program_targets) >= 2:
                        break

        # Quality 2-3 (tentative/probable)
        for q in [3, 2]:
            qn = supabase.table('targets').select('*') \
                .eq('program_slug', slug) \
                .eq('redshift_quality', q) \
                .limit(1) \
                .execute()
            for t in qn.data:
                if t['target_id'] not in seen_ids:
                    program_targets.append(t)
                    seen_ids.add(t['target_id'])
                    break

        # Quality 0 (uninspected)
        q0 = supabase.table('targets').select('*') \
            .eq('program_slug', slug) \
            .eq('redshift_quality', 0) \
            .limit(1) \
            .execute()
        for t in q0.data:
            if t['target_id'] not in seen_ids:
                program_targets.append(t)
                seen_ids.add(t['target_id'])

        # Quality 1 (impossible)
        q1 = supabase.table('targets').select('*') \
            .eq('program_slug', slug) \
            .eq('redshift_quality', 1) \
            .limit(1) \
            .execute()
        for t in q1.data:
            if t['target_id'] not in seen_ids:
                program_targets.append(t)
                seen_ids.add(t['target_id'])

        # Cap at targets_per_program
        program_targets = program_targets[:targets_per_program]

        print(f"  Program {slug}: selected {len(program_targets)} targets "
              f"(qualities: {[t['redshift_quality'] for t in program_targets]})")
        all_targets.extend(program_targets)

    return all_targets


def fetch_observations(supabase, obs_names: list[str]) -> list[dict]:
    """Fetch observations for the given observation names."""
    if not obs_names:
        return []

    all_obs = []
    batch_size = 50
    for i in range(0, len(obs_names), batch_size):
        batch = obs_names[i:i + batch_size]
        resp = supabase.table('observations').select('*').in_('name', batch).execute()
        all_obs.extend(resp.data)
    return all_obs


def fetch_spectra(supabase, target_ids: list[str]) -> list[dict]:
    """Fetch all spectra for the given target_ids."""
    if not target_ids:
        return []

    all_spectra = []
    # Batch to avoid URL length limits
    batch_size = 50
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = supabase.table('spectra').select('*').in_('target_id', batch).execute()
        all_spectra.extend(resp.data)

    return all_spectra



def fetch_comments(supabase, target_int_ids: list[int]) -> list[dict]:
    """Fetch comments for the given target integer IDs."""
    if not target_int_ids:
        return []

    all_comments = []
    batch_size = 50
    for i in range(0, len(target_int_ids), batch_size):
        batch = target_int_ids[i:i + batch_size]
        resp = supabase.table('comments').select('*').in_('target_id', batch).execute()
        all_comments.extend(resp.data)

    return all_comments


def fetch_flag_audit_log(supabase, target_int_ids: list[int]) -> list[dict]:
    """Fetch flag audit log entries for the given target integer IDs."""
    if not target_int_ids:
        return []

    all_entries = []
    batch_size = 50
    for i in range(0, len(target_int_ids), batch_size):
        batch = target_int_ids[i:i + batch_size]
        resp = supabase.table('flag_audit_log').select('*').in_('target_id', batch).execute()
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
        lines.append(f"""INSERT INTO public.user_profiles (user_id, username, full_name, is_admin, can_comment)
VALUES ({sql_escape(user['id'])}, {sql_escape(user['username'])}, {sql_escape(user['full_name'])}, {sql_escape(user['is_admin'])}, {sql_escape(user['can_comment'])});""")

    lines.append('')
    return '\n'.join(lines)


def generate_objects_sql(
    objects: list[dict],
    target_to_object_db_id: dict[int, int] | None = None,
) -> str:
    """Generate INSERT statements for targets (skipping generated columns: redshift, max_snr)."""
    lines = ['-- ============================================']
    lines.append('-- 5. Targets (from production)')
    lines.append('-- ============================================')
    lines.append('')

    target_to_object_db_id = target_to_object_db_id or {}

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

        object_fk = target_to_object_db_id.get(obj['id'])
        object_fk_sql = str(object_fk) if object_fk else 'NULL'

        lines.append(f"""INSERT INTO public.targets (id, target_id, program_slug, observation, field, ra, dec, redshift_auto, redshift_inspected, redshift_quality, spectral_features, dq_flags, last_inspected_at, last_inspected_by, has_sed_plot, object_id)
VALUES ({obj['id']}, {sql_escape(obj['target_id'])}, {sql_escape(obj['program_slug'])}, {sql_escape(obj.get('observation', ''))}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {obj.get('spectral_features', 0)}, {obj.get('dq_flags', 0)}, {inspected_at}, {inspected_by}, {sql_escape(obj.get('has_sed_plot', False))}, {object_fk_sql});""")

    lines.append('')
    return '\n'.join(lines)


# === Object Cross-Matching ===

def build_seed_objects(
    targets: list[dict],
    spectra: list[dict],
    radius_arcsec: float = 0.2,
) -> tuple[list[dict], dict[int, int]]:
    """Cluster seed targets into objects (per-field FoF) and assign synthetic ids.

    Mirrors the production `cfdeploy objects` flow so that a fresh
    `supabase db reset` yields a fully-populated `objects` table with
    target FKs and list members linked.

    Returns (objects, target_db_id -> object_db_id map). Each object dict
    is augmented with a `_db_id` integer used when emitting SQL.
    """
    spectra_map: dict[str, list[dict]] = defaultdict(list)
    for s in spectra:
        tid = s.get('target_id') or s.get('object_id')
        if tid is None:
            continue
        spectra_map[tid].append({
            'target_id': tid,
            'grating': s.get('grating'),
            'signal_to_noise': s.get('signal_to_noise'),
            'exposure_time': s.get('exposure_time'),
        })

    by_field: dict[str, list[dict]] = defaultdict(list)
    for t in targets:
        by_field[t['field']].append(t)

    all_objects: list[dict] = []
    target_to_object_db_id: dict[int, int] = {}
    next_id = 1

    for field in sorted(by_field):
        field_targets = by_field[field]
        groups = cluster_targets(field_targets, radius_arcsec)
        field_objects = build_objects(field_targets, groups, spectra_map)
        for obj in field_objects:
            obj['_db_id'] = next_id
            next_id += 1
            for target_db_id in obj['_member_db_ids']:
                target_to_object_db_id[target_db_id] = obj['_db_id']
        all_objects.extend(field_objects)

    return all_objects, target_to_object_db_id


def generate_objects_table_sql(objects: list[dict]) -> str:
    """Generate INSERT statements for the objects table."""
    lines = ['-- ============================================']
    lines.append('-- 4b. Objects (cross-matched from targets)')
    lines.append('-- ============================================')
    lines.append('')

    for obj in objects:
        lines.append(f"""INSERT INTO public.objects (id, object_id, field, ra, dec, n_targets, n_spectra, programs, gratings, observations, max_snr, max_exposure_time, best_redshift, best_redshift_quality)
VALUES ({obj['_db_id']}, {sql_escape(obj['object_id'])}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {obj['n_targets']}, {obj['n_spectra']}, {sql_escape(obj['programs'])}, {sql_escape(obj['gratings'])}, {sql_escape(obj['observations'])}, {sql_escape(obj['max_snr'])}, {sql_escape(obj['max_exposure_time'])}, {sql_escape(obj['best_redshift'])}, {obj.get('best_redshift_quality', 0)});""")

    lines.append('')
    return '\n'.join(lines)


def generate_spectra_sql(spectra: list[dict]) -> str:
    """Generate INSERT statements for spectra."""
    lines = ['-- ============================================']
    lines.append('-- 6. Spectra (from production)')
    lines.append('-- ============================================')
    lines.append('')

    for spec in spectra:
        lines.append(f"""INSERT INTO public.spectra (id, target_id, grating, fits_path, reduction_version, signal_to_noise, thumbnail_svg_fnu, thumbnail_svg_flambda)
VALUES ({spec['id']}, {sql_escape(spec.get('target_id') or spec.get('object_id'))}, {sql_escape(spec['grating'])}, {sql_escape(spec['fits_path'])}, {sql_escape(spec.get('reduction_version', 'v0.1'))}, {sql_escape(spec.get('signal_to_noise'))}, {sql_escape(spec.get('thumbnail_svg_fnu'))}, {sql_escape(spec.get('thumbnail_svg_flambda'))});""")

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
        obj_int_id = comment['target_id']
        if obj_int_id not in object_id_map:
            continue

        lines.append(f"""INSERT INTO public.comments (id, target_id, user_id, content, created_at, is_deleted)
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
        obj_int_id = entry['target_id']
        if obj_int_id not in object_id_map:
            continue

        lines.append(f"""INSERT INTO public.flag_audit_log (id, target_id, user_id, field_name, old_value, new_value, changed_at)
VALUES ({entry['id']}, {obj_int_id}, {sql_escape(ADMIN_UUID)}, {sql_escape(entry['field_name'])}, {sql_escape(entry.get('old_value'))}, {sql_escape(entry.get('new_value'))}, {sql_escape(entry.get('changed_at', 'now()'))});""")

    if not entries:
        lines.append("-- No flag audit entries found in production for selected objects")

    lines.append('')
    return '\n'.join(lines)


# Mapping from object_flags bitmask bits to system list slugs
# (matches the 9 system lists seeded by the object_lists migration)
OBJECT_FLAG_TO_LIST_SLUG = [
    (1, 'lrd'),
    (2, 'blagn'),
    (4, 'lae'),
    (8, 'bbg'),
    (16, 'o3e'),
    (32, 'hae'),
    (64, 'qg'),
    (128, 'dusty'),
    (256, 'star'),
]


def generate_object_list_members_sql(
    targets: list[dict],
    target_to_object_db_id: dict[int, int],
    objects_by_db_id: dict[int, dict],
) -> str:
    """Generate INSERT statements for object_list_members from production object_flags.

    List members key off the *object* centroid (not the target position), matching
    production semantics where a flag applies to the cross-matched object.
    """
    lines = ['-- ============================================']
    lines.append('-- 9b. Object List Members (from object_flags)')
    lines.append('-- ============================================')
    lines.append('-- System lists are created by the migration. This maps')
    lines.append('-- production object_flags bitmask values to list memberships.')
    lines.append('')

    count = 0
    for target in targets:
        flags = target.get('object_flags', 0) or 0
        if flags == 0:
            continue
        obj_db_id = target_to_object_db_id.get(target['id'])
        if obj_db_id is None:
            continue
        obj = objects_by_db_id[obj_db_id]
        for bit_value, slug in OBJECT_FLAG_TO_LIST_SLUG:
            if flags & bit_value:
                lines.append(
                    f"INSERT INTO public.object_list_members (list_id, object_id, ra, dec) "
                    f"SELECT id, {obj_db_id}, {obj['ra']}, {obj['dec']} FROM public.object_lists "
                    f"WHERE slug = {sql_escape(slug)} "
                    f"ON CONFLICT (list_id, ra, dec) DO NOTHING;"
                )
                count += 1

    if count == 0:
        lines.append("-- No object_flags set on selected targets")
    else:
        lines.append(f'\n-- {count} list memberships from object_flags')

    lines.append('')
    return '\n'.join(lines)


def generate_user_lists_sql(
    targets: list[dict],
    target_to_object_db_id: dict[int, int],
    objects_by_db_id: dict[int, dict],
) -> str:
    """Generate INSERT statements for example user-created lists with sample members."""
    lines = ['-- ============================================']
    lines.append('-- 9c. User-Created Lists (example data)')
    lines.append('-- ============================================')
    lines.append('')

    user_lists = [
        {
            'name': 'High-z Candidates',
            'slug': f'admin/high-z-candidates',
            'description': 'Objects at z > 5 worth following up',
            'visibility': 'private',
            'icon': '\U0001F680',  # rocket
            'color': '#bbdefb',
            'created_by': ADMIN_UUID,
        },
        {
            'name': 'Interesting Spectra',
            'slug': f'user/interesting-spectra',
            'description': 'Unusual or noteworthy spectra',
            'visibility': 'public_read',
            'icon': '\u2B50',  # star
            'color': '#fff59d',
            'created_by': USER_UUID,
        },
        {
            'name': 'Follow-up Needed',
            'slug': f'admin/follow-up-needed',
            'description': 'Objects needing additional observations or re-inspection',
            'visibility': 'public_edit',
            'icon': '\U0001F3AF',  # target
            'color': '#ffccbc',
            'created_by': ADMIN_UUID,
        },
    ]

    for lst in user_lists:
        lines.append(
            f"INSERT INTO public.object_lists (name, slug, description, visibility, is_system, icon, color, created_by) "
            f"VALUES ({sql_escape(lst['name'])}, {sql_escape(lst['slug'])}, {sql_escape(lst['description'])}, "
            f"{sql_escape(lst['visibility'])}, false, {sql_escape(lst['icon'])}, {sql_escape(lst['color'])}, "
            f"{sql_escape(lst['created_by'])}::uuid);"
        )

    lines.append('')

    # Add a few sample members to each list, using object centroids as the
    # durable (ra, dec) key and setting object_id for fast query access.
    sample_targets = [
        t for t in targets
        if t.get('ra') is not None and target_to_object_db_id.get(t['id'])
    ][:12]
    if sample_targets:
        for i, lst in enumerate(user_lists):
            # Each list gets 3-4 members from different parts of the sample
            start = i * 3
            members = sample_targets[start:start + 4]
            for target in members:
                obj_db_id = target_to_object_db_id[target['id']]
                obj = objects_by_db_id[obj_db_id]
                lines.append(
                    f"INSERT INTO public.object_list_members (list_id, object_id, ra, dec, added_by) "
                    f"SELECT id, {obj_db_id}, {obj['ra']}, {obj['dec']}, {sql_escape(lst['created_by'])}::uuid "
                    f"FROM public.object_lists WHERE slug = {sql_escape(lst['slug'])} "
                    f"ON CONFLICT (list_id, ra, dec) DO NOTHING;"
                )

    lines.append(f'\n-- {len(user_lists)} user lists with sample members')
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
                             comments: list[dict], flag_entries: list[dict],
                             cross_matched_objects: list[dict]) -> str:
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

    # Targets
    max_obj_id = max((o['id'] for o in objects), default=0)
    lines.append(f"SELECT setval('public.targets_id_seq', {max_obj_id + 1}, false);")

    # Objects (cross-matched)
    max_objects_id = max((o['_db_id'] for o in cross_matched_objects), default=0)
    if max_objects_id > 0:
        lines.append(f"SELECT setval('public.objects_id_seq', {max_objects_id + 1}, false);")

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

    # Object lists and members (auto-increment from whatever the migration + seed created)
    lines.append("SELECT setval('public.object_lists_id_seq', COALESCE((SELECT MAX(id) FROM public.object_lists), 0) + 1, false);")
    lines.append("SELECT setval('public.object_list_members_id_seq', COALESCE((SELECT MAX(id) FROM public.object_list_members), 0) + 1, false);")

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
    supabase_dir = project_root / 'supabase'
    output_path = supabase_dir / 'seed.sql'

    generate_sample_seed(args, project_root, supabase_dir, output_path)


def generate_sample_seed(args, project_root: Path, supabase_dir: Path, output_path: Path):
    """Generate a small stratified sample seed via Python API queries."""
    print("=== Sample seed ===\n")

    # Load configuration
    print("Loading configuration...")
    config = load_config()
    all_programs = load_programs()
    programs = [p for p in all_programs if p.get('is_public', False)]
    print(f"Filtered to {len(programs)} public programs "
          f"(skipped {len(all_programs) - len(programs)} non-public)")

    # Connect to production Supabase
    print("Connecting to production Supabase...")
    supabase = create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key']
    )

    # Select representative targets
    print(f"\nSelecting up to {args.objects_per_program} targets per program...")
    program_slugs = [p['slug'] for p in programs]
    targets = select_targets(supabase, args.objects_per_program, program_slugs)
    print(f"\nTotal targets selected: {len(targets)}")

    if not targets:
        print("Error: No targets found in production database!")
        sys.exit(1)

    # Build maps
    target_ids = [t['target_id'] for t in targets]
    target_int_ids = [t['id'] for t in targets]
    target_id_map = {t['id']: t['id'] for t in targets}  # identity map (keep original IDs)

    # Fetch observations for selected targets
    obs_names = sorted(set(t['observation'] for t in targets if t.get('observation')))
    print(f"Fetching observations for {len(obs_names)} observation names...")
    observations = fetch_observations(supabase, obs_names)
    print(f"  Found {len(observations)} observations")

    print("Fetching spectra...")
    spectra = fetch_spectra(supabase, target_ids)
    print(f"  Found {len(spectra)} spectra")

    print("Fetching comments...")
    comments = fetch_comments(supabase, target_int_ids)
    print(f"  Found {len(comments)} comments")

    print("Fetching flag audit log...")
    flag_entries = fetch_flag_audit_log(supabase, target_int_ids)
    print(f"  Found {len(flag_entries)} audit entries")

    # Cross-match targets into objects (per-field friends-of-friends)
    print("\nCross-matching targets into objects...")
    cross_matched_objects, target_to_object_db_id = build_seed_objects(
        targets, spectra,
    )
    objects_by_db_id = {o['_db_id']: o for o in cross_matched_objects}
    n_multi = sum(1 for o in cross_matched_objects if o['n_targets'] > 1)
    print(f"  {len(cross_matched_objects)} objects "
          f"({len(cross_matched_objects) - n_multi} singletons, {n_multi} multi-target)")

    # Generate SQL
    print(f"\nGenerating {output_path}...")

    sql_parts = []

    # Header
    sql_parts.append(f"""-- ============================================
-- CAMPFIRE Seed Data (sample)
-- Generated: {datetime.now().isoformat()}
-- Targets: {len(targets)} | Observations: {len(observations)} | Spectra: {len(spectra)}
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
    sql_parts.append(generate_objects_table_sql(cross_matched_objects))
    sql_parts.append(generate_objects_sql(targets, target_to_object_db_id))
    sql_parts.append(generate_spectra_sql(spectra))
    sql_parts.append(generate_user_program_access_sql(programs))
    sql_parts.append(generate_comments_sql(comments, target_id_map))
    sql_parts.append(generate_flag_audit_log_sql(flag_entries, target_id_map))
    sql_parts.append(generate_object_list_members_sql(
        targets, target_to_object_db_id, objects_by_db_id,
    ))
    sql_parts.append(generate_user_lists_sql(
        targets, target_to_object_db_id, objects_by_db_id,
    ))
    sql_parts.append(generate_access_codes_sql())
    sql_parts.append(generate_sequence_resets(
        targets, spectra, comments, flag_entries, cross_matched_objects,
    ))

    # Write output
    full_sql = '\n'.join(sql_parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(full_sql)

    print(f"\nSeed file written to: {output_path}")
    print(f"  Size: {len(full_sql):,} bytes")
    print(f"\nTo apply: supabase db reset")


if __name__ == '__main__':
    main()
