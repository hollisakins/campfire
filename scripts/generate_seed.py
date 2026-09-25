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
friends-of-friends via `campfire.deploy.objects`), so no `campfire deploy objects`
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
# Synthetic link account backing the seed's demo share link (admin-plane
# fixtures) — mirrors the mint flow's read-only principal.
LINK_UUID = '44444444-4444-4444-4444-444444444444'

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
        'can_inspect': True,
    },
    {
        'id': USER_UUID,
        'email': 'user@campfire.dev',
        'username': 'user',
        'full_name': 'Regular User',
        'is_admin': False,
        'can_comment': True,
        'can_inspect': True,
    },
    {
        'id': VIEWER_UUID,
        'email': 'viewer@campfire.dev',
        'username': 'viewer',
        'full_name': 'Viewer User',
        'is_admin': False,
        'can_comment': False,
        'can_inspect': False,
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
        lines.append(f"""INSERT INTO public.user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect)
VALUES ({sql_escape(user['id'])}, {sql_escape(user['username'])}, {sql_escape(user['full_name'])}, {sql_escape(user['is_admin'])}, {sql_escape(user['can_comment'])}, {sql_escape(user['can_inspect'])});""")

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

        lines.append(f"""INSERT INTO public.targets (id, target_id, program_slug, observation, field, ra, dec, redshift_auto, redshift_inspected, redshift_quality, spectral_features, dq_flags, last_inspected_at, last_inspected_by, object_id)
VALUES ({obj['id']}, {sql_escape(obj['target_id'])}, {sql_escape(obj['program_slug'])}, {sql_escape(obj.get('observation', ''))}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {obj.get('spectral_features', 0)}, {obj.get('dq_flags', 0)}, {inspected_at}, {inspected_by}, {object_fk_sql});""")

    lines.append('')
    return '\n'.join(lines)


# === Object Cross-Matching ===

def build_seed_objects(
    targets: list[dict],
    spectra: list[dict],
    radius_arcsec: float = 0.2,
) -> tuple[list[dict], dict[int, int]]:
    """Cluster seed targets into objects (per-field FoF) and assign synthetic ids.

    Mirrors the production `campfire deploy objects` flow so that a fresh
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


def backfill_spectra_from_targets(
    spectra: list[dict],
    targets: list[dict],
) -> None:
    """In-process equivalent of Phase D.1b/D.1c spectra backfill.

    Production (pre-merge) has no spectra.redshift_auto / dq_flags columns, so
    `fetch_spectra` returns rows missing those keys. The Phase A migration
    adds the columns and D.1b/D.1c backfills from the parent target. Mirror
    that here so seeded spectra land with realistic values, matching what
    post-migration production will look like.

    Mutates spectra in-place.
    """
    t_by_target_id = {t['target_id']: t for t in targets}
    for spec in spectra:
        tgt = t_by_target_id.get(spec.get('target_id'))
        if tgt is None:
            continue
        # D.1b: fill redshift_auto from target if spec doesn't have one
        if spec.get('redshift_auto') is None and tgt.get('redshift_auto') is not None:
            spec['redshift_auto'] = tgt['redshift_auto']
        # D.1c: OR target dq_flags into spec dq_flags (union)
        tgt_dq = tgt.get('dq_flags') or 0
        if tgt_dq:
            spec['dq_flags'] = (spec.get('dq_flags') or 0) | tgt_dq


def lift_inspection_state_to_objects(
    objects: list[dict],
    targets: list[dict],
    spectra: list[dict],
) -> None:
    """In-process equivalent of the Phase D.1 data migration.

    For each cross-matched object, copy inspection state from the best-quality
    member target (ties broken by max_snr), and set redshift_auto from the
    highest-S/N member spectrum. Mirrors D.1a/D.1b logic so seeded objects
    land in the same post-migration state the production D.1 migration
    produces on real data.

    Mutates objects in-place.
    """
    targets_by_id = {t['id']: t for t in targets}
    spectra_by_target: dict[str, list[dict]] = defaultdict(list)
    for s in spectra:
        tid = s.get('target_id')
        if tid is not None:
            spectra_by_target[tid].append(s)

    for obj in objects:
        member_targets = [
            targets_by_id[tid] for tid in obj['_member_db_ids']
            if tid in targets_by_id
        ]

        # D.1a: best inspected target (quality > 0), tiebreak by max_snr
        inspected = [
            t for t in member_targets if (t.get('redshift_quality') or 0) > 0
        ]
        if inspected:
            best = max(
                inspected,
                key=lambda t: (
                    t.get('redshift_quality') or 0,
                    t.get('max_snr') or 0,
                ),
            )
            obj['redshift_inspected'] = best.get('redshift_inspected')
            obj['redshift_quality'] = best.get('redshift_quality') or 0
            obj['last_inspected_at'] = best.get('last_inspected_at')
            obj['last_inspected_by'] = best.get('last_inspected_by')
            obj['last_data_change_at'] = best.get('updated_at') or best.get('last_inspected_at')
        else:
            obj['redshift_inspected'] = None
            obj['redshift_quality'] = 0
            obj['last_inspected_at'] = None
            obj['last_inspected_by'] = None
            obj['last_data_change_at'] = None

        # D.1b: object.redshift_auto = redshift_auto of highest-SNR member spectrum
        candidate_specs = [
            s for m in member_targets
            for s in spectra_by_target.get(m['target_id'], [])
            if s.get('redshift_auto') is not None
        ]
        if candidate_specs:
            best_spec = max(
                candidate_specs,
                key=lambda s: (s.get('signal_to_noise') or 0),
            )
            obj['redshift_auto'] = best_spec.get('redshift_auto')
        else:
            obj['redshift_auto'] = None


def generate_objects_table_sql(objects: list[dict]) -> str:
    """Generate INSERT statements for the objects table."""
    lines = ['-- ============================================']
    lines.append('-- 4b. Objects (cross-matched from targets)')
    lines.append('-- ============================================')
    lines.append('')

    for obj in objects:
        inspected_by = sql_escape(ADMIN_UUID) if obj.get('last_inspected_by') else 'NULL'
        inspected_at = sql_escape(obj.get('last_inspected_at')) if obj.get('last_inspected_at') else 'NULL'
        last_data_change_at = sql_escape(obj.get('last_data_change_at')) if obj.get('last_data_change_at') else 'NULL'
        redshift_inspected = obj.get('redshift_inspected')
        redshift_inspected_sql = str(redshift_inspected) if redshift_inspected is not None else 'NULL'

        lines.append(f"""INSERT INTO public.objects (id, object_id, field, ra, dec, n_targets, n_spectra, programs, gratings, observations, max_snr, max_exposure_time, redshift_auto, redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by, last_data_change_at)
VALUES ({obj['_db_id']}, {sql_escape(obj['object_id'])}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {obj['n_targets']}, {obj['n_spectra']}, {sql_escape(obj['programs'])}, {sql_escape(obj['gratings'])}, {sql_escape(obj['observations'])}, {sql_escape(obj['max_snr'])}, {sql_escape(obj['max_exposure_time'])}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {inspected_at}, {inspected_by}, {last_data_change_at});""")

    lines.append('')
    return '\n'.join(lines)


def generate_spectra_sql(spectra: list[dict]) -> str:
    """Generate INSERT statements for spectra."""
    lines = ['-- ============================================']
    lines.append('-- 6. Spectra (from production)')
    lines.append('-- ============================================')
    lines.append('')

    for spec in spectra:
        lines.append(f"""INSERT INTO public.spectra (id, target_id, grating, fits_path, cfpipe_version, signal_to_noise, exposure_time, thumbnail_svg_fnu, thumbnail_svg_flambda, redshift_auto, chi2_min, confidence, dq_flags)
VALUES ({spec['id']}, {sql_escape(spec.get('target_id') or spec.get('object_id'))}, {sql_escape(spec['grating'])}, {sql_escape(spec['fits_path'])}, {sql_escape(spec.get('cfpipe_version') or spec.get('reduction_version') or 'v0.1')}, {sql_escape(spec.get('signal_to_noise'))}, {sql_escape(spec.get('exposure_time'))}, {sql_escape(spec.get('thumbnail_svg_fnu'))}, {sql_escape(spec.get('thumbnail_svg_flambda'))}, {sql_escape(spec.get('redshift_auto'))}, {sql_escape(spec.get('chi2_min'))}, {sql_escape(spec.get('confidence'))}, {spec.get('dq_flags') or 0});""")

    lines.append('')
    return '\n'.join(lines)


def generate_storage_objects_sql(spectra: list[dict]) -> str:
    """Generate INSERT statements for the storage_objects registry (#214).

    One row per sampled spectrum (a NIRSpec final). Seed spectra carry no real
    ``file_hash``, so a deterministic synthetic ``sha256:`` token (from the key)
    + a synthetic size stand in — enough to exercise the admin RLS, the
    get_storage_budget RPC, and reconcile coverage locally and on preview
    branches. ``observation`` is left NULL to avoid an FK to a possibly
    unsampled observations row; ``id`` is omitted so the sequence assigns it.
    """
    import hashlib

    lines = ['-- ============================================']
    lines.append('-- Storage objects registry (synthetic; #214)')
    lines.append('-- ============================================')
    lines.append('')

    for spec in spectra:
        key = spec.get('fits_path')
        if not key:
            continue
        digest = hashlib.sha256(key.encode()).hexdigest()
        base = key.rsplit('/', 1)[-1]
        spectrum_id = base[:-len('_spec.fits')] if base.endswith('_spec.fits') else base
        lines.append(
            "INSERT INTO public.storage_objects "
            "(backend, bucket, storage_key, content_hash, size_bytes, content_type, "
            "product_type, instrument, spectrum_id, status) VALUES "
            f"('r2', 'data', {sql_escape(key)}, {sql_escape('sha256:' + digest)}, "
            f"1048576, 'application/fits', 'nirspec_spec', 'nirspec', "
            f"{sql_escape(spectrum_id)}, 'active');"
        )

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

def generate_admin_plane_sql() -> str:
    """Admin-plane fixtures for the dashboard (2026-08 control-center redesign).

    The production sampler covers the science catalog (targets/objects/spectra
    + their comments and audit rows) but none of the admin plane, so on a
    preview branch or local reset every dashboard panel that reads deployments,
    exposure review, downloads, or the access queues rendered empty. This block
    fills those tables with synthetic-but-plausible rows.

    Design constraints, deliberately:
      * Fully self-referential — every scope/user reference derives from rows
        seeded above via INSERT ... SELECT, so this section needs NO production
        queries and never goes stale against the sample.
      * Relative timestamps (now() - interval ...) so a fresh reset always
        shows *recent* activity instead of aging out.
      * Deterministic — state mixing is modular arithmetic, never random().
      * Placed LAST in the seed, after the hardcoded sequence resets, so
        default-sequence inserts (comments, flag_audit_log) cannot collide
        with the explicit prod ids inserted earlier.
    """
    return f"""-- ============================================
-- 13. Admin-plane fixtures (dashboard)
-- ============================================
-- Synthetic deploy/review/usage rows so the /admin control center renders
-- with live-looking data on preview branches and local resets. Everything
-- derives from the sampled rows above; timestamps are relative to now();
-- state mixing is deterministic. See generate_admin_plane_sql() in
-- scripts/generate_seed.py.

-- --- NIRCam field registry (derived from the sampled targets' fields) ------
INSERT INTO public.fields (name, display_name, filters, created_at, config_hash, config_updated_at)
SELECT f.field,
       upper(replace(f.field, '_', '-')),
       ARRAY['f277w', 'f444w'],
       now() - interval '60 days' + (f.rn * interval '9 days'),
       CASE WHEN f.rn % 2 = 0 THEN 'seedhash-' || f.field END,
       CASE WHEN f.rn % 2 = 0 THEN now() - interval '10 days' END
FROM (
  SELECT d.field, row_number() OVER (ORDER BY d.field) AS rn
  FROM (SELECT DISTINCT field FROM public.targets) d
  ORDER BY d.field
  LIMIT 4
) f
ON CONFLICT (name) DO NOTHING;

-- Config plane: mark most observations as pushed, leave two local-only so the
-- CONFIG attention rule has a small, realistic count.
UPDATE public.observations SET
  config_hash = 'seedhash-' || name,
  config_updated_at = now() - interval '15 days'
WHERE name NOT IN (SELECT name FROM public.observations ORDER BY name LIMIT 2);

-- --- Deployments -----------------------------------------------------------
-- One published deployment per observation (staggered ages), one per field,
-- two drafts (one on an off-release dev build — the deploy CLI's warn path),
-- and one revoked, so every lifecycle state renders.
INSERT INTO public.deployments (observation, status, cfpipe_version, jwst_version, crds_context,
                                n_targets, n_spectra, reduced_at, deployed_at, published_at, deployed_by)
SELECT o.name, 'published', '0.9.3', '1.17.1', 'jwst_1321.pmap',
       5 + o.rn % 20, 12 + (o.rn * 7) % 40,
       now() - (o.rn * interval '2 days') - interval '6 hours',
       now() - (o.rn * interval '2 days'),
       now() - (o.rn * interval '2 days'),
       '{ADMIN_UUID}'
FROM (SELECT name, row_number() OVER (ORDER BY name) AS rn FROM public.observations) o;

INSERT INTO public.deployments (field, status, cfpipe_version, jwst_version, crds_context,
                                n_targets, deployed_at, published_at, deployed_by)
SELECT f.name, 'published', '0.9.3', '1.17.1', 'jwst_1321.pmap',
       96 + f.rn,
       now() - (f.rn * interval '6 days'),
       now() - (f.rn * interval '6 days'),
       '{ADMIN_UUID}'
FROM (SELECT name, row_number() OVER (ORDER BY name) AS rn FROM public.fields) f;

INSERT INTO public.deployments (observation, status, cfpipe_version, jwst_version, crds_context,
                                n_targets, n_spectra, deployed_at, deployed_by)
SELECT name, 'draft', '0.9.4.dev6+ga1b2c3d', '1.17.1', 'jwst_1321.pmap',
       8, 21, now() - interval '5 days', '{ADMIN_UUID}'
FROM public.observations ORDER BY name LIMIT 1;

INSERT INTO public.deployments (field, status, cfpipe_version, jwst_version, crds_context,
                                n_targets, deployed_at, deployed_by)
SELECT name, 'draft', '0.9.3', '1.17.1', 'jwst_1321.pmap',
       64, now() - interval '30 hours', '{ADMIN_UUID}'
FROM public.fields ORDER BY name LIMIT 1;

INSERT INTO public.deployments (observation, status, cfpipe_version, jwst_version, crds_context,
                                n_targets, n_spectra, deployed_at, published_at, revoked_at, deployed_by)
SELECT name, 'revoked', '0.9.2', '1.17.1', 'jwst_1290.pmap',
       6, 14, now() - interval '40 days', now() - interval '40 days', now() - interval '9 days', '{ADMIN_UUID}'
FROM public.observations ORDER BY name OFFSET 1 LIMIT 1;

UPDATE public.observations o SET latest_deployment_id = d.id
FROM (SELECT DISTINCT ON (observation) observation, id
      FROM public.deployments WHERE observation IS NOT NULL
      ORDER BY observation, deployed_at DESC) d
WHERE d.observation = o.name;

UPDATE public.fields f SET latest_deployment_id = d.id
FROM (SELECT DISTINCT ON (field) field, id
      FROM public.deployments WHERE field IS NOT NULL
      ORDER BY field, deployed_at DESC) d
WHERE d.field = f.name;

-- Attach the sampled spectra's storage rows to their observation's deployment
-- so the "pushed, never deployed" integrity check counts only genuinely
-- unattached objects (the provisional-hash rows below). The seed's spectra
-- storage rows carry observation NULL by design, so resolve through
-- spectrum_id -> spectra -> targets -> observations.
UPDATE public.storage_objects so SET deployment_id = o.latest_deployment_id
FROM public.spectra s
JOIN public.targets t ON t.target_id = s.target_id
JOIN public.observations o ON o.name = t.observation
WHERE so.spectrum_id = s.spectrum_id
  AND so.deployment_id IS NULL AND so.status = 'active';

UPDATE public.storage_objects so SET deployment_id = o.latest_deployment_id
FROM public.observations o
WHERE so.observation = o.name AND so.deployment_id IS NULL AND so.status = 'active';

-- --- Deploy events (audit log derived from the deployments above) ----------
INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_to, affected_count, occurred_at)
SELECT deployed_by, 'upload', id, observation, field, status, COALESCE(n_spectra, n_targets), deployed_at
FROM public.deployments;

INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_from, status_to, affected_count, occurred_at)
SELECT deployed_by, 'publish', id, observation, field, 'draft', 'published', COALESCE(n_spectra, n_targets), published_at
FROM public.deployments WHERE published_at IS NOT NULL;

INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_from, status_to, affected_count, occurred_at)
SELECT deployed_by, 'revoke', id, observation, field, 'published', 'revoked', COALESCE(n_spectra, n_targets), revoked_at
FROM public.deployments WHERE revoked_at IS NOT NULL;

INSERT INTO public.deploy_events (actor, action, occurred_at, metadata)
VALUES ('{ADMIN_UUID}', 'config_sync', now() - interval '14 hours',
        '{{"kinds": ["observations", "fields"]}}'::jsonb);

-- --- NIRCam exposure review queue (2 fields x 2 filters x 4 detectors x 8) --
INSERT INTO public.nircam_exposures (field, filter, detector, filename, visit, stage,
                                     review_status, correction, mask_regions, notes,
                                     review_decided_at, date_obs)
SELECT f.name, flt.f, det.d,
       format('jw%s001001_02101_%s_%s_%s_cal.fits',
              lpad((100 + f.rn)::text, 5, '0'), lpad((10000 + n.i)::text, 5, '0'), det.d, flt.f),
       format('visit_%s', 1 + n.i % 4),
       'cal',
       CASE WHEN n.i % 8 IN (0, 1, 2, 3) THEN 'approved'
            WHEN n.i % 8 = 4 THEN 'excluded'
            ELSE 'pending' END,
       CASE WHEN n.i % 8 = 5 THEN 'needed'
            WHEN n.i % 8 = 4 THEN 'done'
            ELSE 'none' END,
       CASE WHEN n.i % 8 = 3
            THEN '[{{"kind": "polygon", "points": [[64, 64], [192, 64], [192, 192]]}}]'::jsonb END,
       CASE WHEN n.i % 8 = 5 THEN 'seed fixture: cosmic-ray cluster near detector edge' END,
       CASE WHEN n.i % 8 < 5 THEN now() - (n.i % 21) * interval '1 day' - interval '3 hours' END,
       (now() AT TIME ZONE 'utc') - interval '200 days' + (n.i * interval '1 hour')
FROM (SELECT name, row_number() OVER (ORDER BY name) AS rn
      FROM public.fields ORDER BY name LIMIT 2) f
CROSS JOIN (VALUES ('f277w'), ('f444w')) AS flt(f)
CROSS JOIN (VALUES ('nrca1'), ('nrca2'), ('nrcb1'), ('nrcb2')) AS det(d)
CROSS JOIN generate_series(0, 7) AS n(i);

-- --- NIRSpec rate-mask review queue (3 obs x 2 detectors x 4 roots) --------
INSERT INTO public.nirspec_rate_exposures (observation, exposure_root, detector, filename,
                                           grating, stage, review_status, mask_regions)
SELECT o.name,
       format('jw%s_0%s101_0000%s', lpad((7000 + o.rn)::text, 5, '0'), 1 + r.i, 1 + r.i),
       det.d,
       format('jw%s_0%s101_%s_rate.fits', lpad((7000 + o.rn)::text, 5, '0'), 1 + r.i, det.d),
       (ARRAY['prism', 'g395m'])[1 + r.i % 2],
       'rate',
       CASE WHEN (r.i * 2 + o.rn) % 5 < 3 THEN 'approved'
            WHEN (r.i * 2 + o.rn) % 5 = 3 THEN 'pending'
            ELSE 'excluded' END,
       CASE WHEN (r.i + o.rn) % 6 = 0
            THEN '[{{"kind": "polygon", "points": [[10, 900], [400, 900], [400, 1010]]}}]'::jsonb END
FROM (SELECT name, row_number() OVER (ORDER BY name) AS rn
      FROM public.observations ORDER BY name LIMIT 3) o
CROSS JOIN (VALUES ('nrs1'), ('nrs2')) AS det(d)
CROSS JOIN generate_series(0, 3) AS r(i);

-- --- NIRSpec nods review (1 obs: 2 roots x 3 nods x 2 detectors x 2 sources)
INSERT INTO public.spectrum_exposures (observation, exposure_root, nod, detector, source_id,
                                       exp_group, grating, filename, stage, review_status)
SELECT o.name,
       format('jw07076020001_0%s101', 4 + r.i),
       n.nod::text, det.d, s.sid, 1 + r.i, 'prism',
       format('jw07076020001_0%s101_%s_%s_s%s_cal.fits', 4 + r.i, n.nod, det.d, s.sid),
       'cal',
       CASE WHEN (s.sid + n.nod + r.i) % 3 = 0 THEN 'pending' ELSE 'approved' END
FROM (SELECT name FROM public.observations ORDER BY name LIMIT 1) o
CROSS JOIN generate_series(0, 1) AS r(i)
CROSS JOIN generate_series(1, 3) AS n(nod)
CROSS JOIN (VALUES (101), (102)) AS s(sid)
CROSS JOIN (VALUES ('nrs1'), ('nrs2')) AS det(d);

INSERT INTO public.nirspec_source_review (observation, exposure_root, source_id,
                                          stuck_shutters, bkg_overrides, notes)
SELECT o.name, 'jw07076020001_04101', 101,
       '[2, 3]'::jsonb, '{{"3": [1]}}'::jsonb, 'seed fixture: stuck shutter pair'
FROM (SELECT name FROM public.observations ORDER BY name LIMIT 1) o;

-- --- Storage: tombstones, provisional hashes, mosaics -----------------------
-- Superseded copies of some sampled spectra (re-upload tombstones) so the
-- status split and reclaimable figure are non-zero.
INSERT INTO public.storage_objects (backend, bucket, storage_key, content_hash, size_bytes,
                                    content_type, product_type, instrument, observation, status)
SELECT so.backend, so.bucket, so.storage_key || '.superseded-' || so.id,
       'sha256:' || md5(so.storage_key), (so.size_bytes * 0.9)::bigint,
       so.content_type, so.product_type, so.instrument, so.observation, 'superseded'
FROM public.storage_objects so
WHERE so.status = 'active'
ORDER BY so.id LIMIT 20;

-- Three provisional etag-hashed exposures with no deployment attached — the
-- integrity checks ("provisional hashes", "pushed, never deployed") demo rows.
INSERT INTO public.storage_objects (backend, bucket, storage_key, content_hash, size_bytes,
                                    content_type, product_type, instrument, field, status)
SELECT 'osn', 'data',
       format('nircam/%s/f277w/exposures/jw_seed_%s_cal.fits', f.name, g.i),
       'etag:' || md5(f.name || g.i), 420000000 + g.i * 1000000,
       'application/fits', 'nircam_exposure', 'nircam', f.name, 'active'
FROM (SELECT name FROM public.fields ORDER BY name LIMIT 1) f
CROSS JOIN generate_series(1, 3) AS g(i);

-- Mosaics (compressed at rest) for by-product-type variety.
INSERT INTO public.storage_objects (backend, bucket, storage_key, content_hash, size_bytes,
                                    stored_size_bytes, content_type, product_type, instrument,
                                    field, filter, status, deployment_id)
SELECT 'osn', 'data',
       format('nircam/%s/%s/mosaic_t1.fits', f.name, flt.f),
       'sha256:' || md5(f.name || flt.f),
       2500000000::bigint + f.rn * 130000000, 1300000000::bigint + f.rn * 70000000,
       'application/fits', 'nircam_mosaic', 'nircam', f.name, flt.f, 'active',
       f.latest_deployment_id
FROM (SELECT name, latest_deployment_id, row_number() OVER (ORDER BY name) AS rn
      FROM public.fields ORDER BY name LIMIT 2) f
CROSS JOIN (VALUES ('f277w'), ('f444w')) AS flt(f);

-- --- Downloads (uneven ~90-row series over the last 30 days) ----------------
INSERT INTO public.download_log (user_id, download_type, target_count, file_count, target_ids, requested_at)
SELECT (ARRAY['{ADMIN_UUID}', '{USER_UUID}', '{VIEWER_UUID}'])[1 + g.i % 3]::uuid,
       (ARRAY['fits_single', 'fits_object', 'fits_zip', 'csv', 'fits_sync', 'sed_plot', 'fits_batch'])[1 + g.i % 7],
       1 + g.i % 3,
       1 + g.i % 6,
       ARRAY(SELECT t.target_id FROM public.targets t ORDER BY t.id OFFSET g.i % 40 LIMIT 1 + g.i % 2),
       now() - ((g.i * g.i) % 30) * interval '1 day' - (g.i % 13) * interval '67 minutes'
FROM generate_series(0, 89) AS g(i);

-- --- Access queues ----------------------------------------------------------
INSERT INTO public.inspection_access_requests (user_id, status, message, created_at)
VALUES ('{VIEWER_UUID}', 'pending',
        'Requesting inspection access to help with the triage backlog.',
        now() - interval '3 days');

INSERT INTO public.inspection_access_requests (user_id, status, message, created_at, reviewed_at, reviewed_by)
VALUES ('{USER_UUID}', 'approved', 'Joining the inspection effort.',
        now() - interval '20 days', now() - interval '19 days', '{ADMIN_UUID}');

INSERT INTO public.pending_invites (email, full_name, can_comment, can_inspect, invited_by, created_at, program_slugs)
VALUES ('new.postdoc@example.edu', 'New Postdoc', TRUE, FALSE, '{ADMIN_UUID}',
        now() - interval '2 days', ARRAY['ember']),
       ('stale.invite@example.edu', 'Stale Invite', TRUE, FALSE, '{ADMIN_UUID}',
        now() - interval '21 days', NULL);

-- Share link backed by a synthetic link account (mirrors the mint flow).
INSERT INTO auth.users (
    id, instance_id, aud, role, email,
    encrypted_password, email_confirmed_at,
    created_at, updated_at, confirmation_token,
    recovery_token, email_change_token_new, email_change
) VALUES (
    '{LINK_UUID}', '00000000-0000-0000-0000-000000000000',
    'authenticated', 'authenticated', 'link-demo@campfire.dev',
    {PASSWORD_SQL}, NOW(), NOW(), NOW(), '', '', '', ''
);
INSERT INTO auth.identities (
    id, user_id, identity_data, provider, provider_id,
    last_sign_in_at, created_at, updated_at
) VALUES (
    gen_random_uuid(), '{LINK_UUID}',
    jsonb_build_object('sub', '{LINK_UUID}', 'email', 'link-demo@campfire.dev'),
    'email', '{LINK_UUID}', NOW(), NOW(), NOW()
);
INSERT INTO public.user_profiles (user_id, username, full_name, is_link_account, can_comment, can_inspect, is_admin)
VALUES ('{LINK_UUID}', 'link-demo', 'Shared link: demo', TRUE, FALSE, FALSE, FALSE);

INSERT INTO public.share_links (token, label, observation, link_user_id, link_password,
                                include_drafts, allow_download, created_by, created_at,
                                view_count, last_seen_at)
SELECT 'seeddemo00000000000000000000link', 'Collaborator preview (drafts)', o.name,
       '{LINK_UUID}', 'password123', TRUE, TRUE, '{ADMIN_UUID}',
       now() - interval '6 days', 14, now() - interval '1 day'
FROM (SELECT name FROM public.observations ORDER BY name LIMIT 1) o;

-- --- Recent inspection activity ---------------------------------------------
-- The sampled flag_audit_log rows carry their production timestamps (months
-- old), so the dashboard's 7-day counters would read zero; add a fresh burst.
-- Runs after the sequence resets, so default ids cannot collide with the
-- explicit prod ids above.
INSERT INTO public.flag_audit_log (object_id, user_id, field_name, old_value, new_value, changed_at)
SELECT o.id,
       (ARRAY['{ADMIN_UUID}', '{USER_UUID}'])[1 + o.rn % 2]::uuid,
       'redshift_quality', 0, 1 + o.rn % 4,
       (now() AT TIME ZONE 'utc') - (o.rn % 7) * interval '1 day' - (o.rn * interval '31 minutes')
FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn
      FROM public.objects ORDER BY id LIMIT 10) o;

INSERT INTO public.comments (object_id, user_id, content, created_at)
SELECT o.id, '{USER_UUID}',
       'Seed fixture: line identifications look consistent with the quoted redshift.',
       (now() AT TIME ZONE 'utc') - interval '2 days'
FROM (SELECT id FROM public.objects ORDER BY id LIMIT 1) o;

INSERT INTO public.comments (target_id, user_id, content, created_at)
SELECT t.id, '{ADMIN_UUID}',
       'Seed fixture: check the continuum slope before sign-off.',
       (now() AT TIME ZONE 'utc') - interval '5 days'
FROM (SELECT id FROM public.targets ORDER BY id LIMIT 1) t;
"""


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

    # Mirror Phase D.1 in-process so seed data matches post-migration shape
    backfill_spectra_from_targets(spectra, targets)
    lift_inspection_state_to_objects(cross_matched_objects, targets, spectra)
    n_inspected = sum(1 for o in cross_matched_objects if o.get('redshift_quality', 0) > 0)
    print(f"  {n_inspected} objects carry lifted inspection state")

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
    sql_parts.append(generate_storage_objects_sql(spectra))
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
    # Admin-plane fixtures go LAST: they are self-referential SQL over the
    # rows above, and their default-sequence inserts must follow the resets.
    sql_parts.append(generate_admin_plane_sql())

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
