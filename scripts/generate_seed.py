#!/usr/bin/env python3
"""
Generate Supabase seed data from production database.

Queries the production Supabase for a representative subset of real targets
(with files already in cloud storage) and generates supabase/seed.sql for
local dev and preview branch seeding.

The output seed.sql is a committed artifact checked into the repo. Supabase's
GitHub integration uses it to seed preview branches automatically when PRs are
opened, and locally it's applied via `supabase db reset`. Because of this,
seed.sql must stay compatible with the current migration state — if a migration
changes the schema in a way that breaks seed inserts (e.g. renaming a table,
adding a NOT NULL column without a default), regenerate the seed file.

Production access is READ-ONLY (service-role key from the environment or
`$CAMPFIRE_ROOT/config/deploy.toml`). The generated seed.sql must never carry
production credentials, user identities or free text: the repo is public.
Two scrubs are enforced here and re-checked by `scripts/check_seed_privacy.py`:

* **Public programs only.** Programs come from the production `programs`
  table and only `is_public = true` rows are kept; every emitted row that
  names a program (observation, target, spectrum, line fit, deployment, share
  link, storage object, access grant) is checked against that set and the run
  aborts on the first violation.
* **No production identities.** Every real `auth.users` uuid met in the
  sampled rows (inspectors, commenters, deployers, uploaders) is mapped onto
  the synthetic admin/user accounts; comment text is replaced by synthetic
  text; the only emails are `@campfire.dev` and `@shared.invalid`.

Targets are cross-matched into `objects` table entries in-process (per-field
friends-of-friends via `campfire.deploy.objects`), so no `campfire deploy objects`
follow-up is needed after `supabase db reset`. The objects of the anchor program
(`castellano3073`) are numbered first so that the seed-anchored harnesses in
`python/tests/sql/` (objects 1 & 2 as lifecycle victims, object 3 as the
published control) keep their ids.

Besides the catalog, the seed carries the lifecycle surface the RLS work needs:
real `storage_objects` rows (hashes and sizes) for every sampled spectrum and
its sidecars, real `spectrum_line_fits` and `object_photometry` rows, one
published `deployments` row per sampled observation plus a draft and a revoked
one (each with one spectrum attached), a handful of published `nircam_images`
of one real field plus a draft one, the `fields` rows, and two share links
(one live, one revoked) with their link accounts.

Usage:
    python scripts/generate_seed.py
    python scripts/generate_seed.py --objects-per-program 10
    python scripts/check_seed_privacy.py       # re-check the output
"""

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# === Configuration Loading ===

def load_toml(path: Path) -> dict:
    """Load a TOML file."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_config() -> dict:
    """Resolve the production Supabase url + service-role key (read-only use).

    `CAMPFIRE_SUPABASE_URL` / `CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY` win; the
    `[supabase]` block of `$CAMPFIRE_ROOT/config/deploy.toml` is the fallback.
    """
    url = os.environ.get('CAMPFIRE_SUPABASE_URL')
    key = os.environ.get('CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY')
    if url and key:
        return {'supabase': {'url': url, 'service_role_key': key}}
    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    if not campfire_root:
        print("Error: set CAMPFIRE_SUPABASE_URL + CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY "
              "or $CAMPFIRE_ROOT (for config/deploy.toml).")
        sys.exit(1)
    config_path = Path(campfire_root) / 'config' / 'deploy.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    config = load_toml(config_path)
    if 'supabase' not in config:
        print(f"Error: {config_path} has no [supabase] block and the "
              "CAMPFIRE_SUPABASE_URL / CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY env vars are unset.")
        sys.exit(1)
    return config


# === SQL Escaping ===

def sql_escape(value) -> str:
    """Escape a value for SQL insertion."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return 'NULL'
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Escape single quotes by doubling them
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, list):
        # PostgreSQL array literal; an untyped '{}' takes the column's type
        if not value:
            return "'{}'"
        if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            items = ', '.join(str(v) for v in value)
            return f"ARRAY[{items}]"
        items = ', '.join(sql_escape(v) for v in value)
        return f"ARRAY[{items}]"
    if isinstance(value, dict):
        return sql_json(value)
    return sql_escape(str(value))


def sql_json(value) -> str:
    """Escape a Python value as a jsonb literal (NULL when None)."""
    if value is None:
        return 'NULL'
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return f"'{text.replace(chr(39), chr(39) * 2)}'::jsonb"


def sql_value_or_null(row: dict, key: str) -> str:
    """Get a SQL-escaped value from a dict, returning NULL if missing."""
    return sql_escape(row.get(key))


def sql_row(row: dict, columns: list[str], json_columns: set[str] = frozenset()) -> str:
    """Emit the VALUES tuple of ``row`` for ``columns`` (jsonb columns via sql_json)."""
    parts = []
    for col in columns:
        value = row.get(col)
        parts.append(sql_json(value) if col in json_columns else sql_escape(value))
    return ', '.join(parts)


# === Test Users ===

ADMIN_UUID = '11111111-1111-1111-1111-111111111111'
USER_UUID = '22222222-2222-2222-2222-222222222222'
VIEWER_UUID = '33333333-3333-3333-3333-333333333333'
# Share-link accounts (one per seeded share link).
LINK_OBS_UUID = '44444444-4444-4444-4444-444444444444'
LINK_REVOKED_UUID = '55555555-5555-5555-5555-555555555555'

SYNTHETIC_UUIDS = (ADMIN_UUID, USER_UUID, VIEWER_UUID, LINK_OBS_UUID, LINK_REVOKED_UUID)

# The program whose objects are numbered first: python/tests/sql/b1_*.sql and
# b2_*.sql use objects 1 & 2 as lifecycle victims and object 3 as the published
# control, all in this program.
ANCHOR_PROGRAM = 'castellano3073'

# Synthetic deployments (published rows for observations that have none, the
# draft and the revoked lifecycle fixtures) are numbered from here: above
# production's id space, so a production foreign key (fields / storage rows)
# can never coincide with a synthetic row. supabase/tests/check_share_link_scoping.sql
# uses 990001-990005 for its own fixtures; stay clear of those.
SYNTHETIC_DEPLOYMENT_ID_BASE = 900001

# Password will be set using crypt() function in SQL
# This ensures compatibility with Supabase Auth's bcrypt implementation
# Note: gen_salt is in the extensions schema
PASSWORD_SQL = "extensions.crypt('password123', extensions.gen_salt('bf'))"

# Link accounts sign in with the share link's stored password; a synthetic
# constant here (production mints 32 random bytes per link).
LINK_PASSWORD = 'seed-link-password'
LINK_PASSWORD_SQL = f"extensions.crypt({sql_escape(LINK_PASSWORD)}, extensions.gen_salt('bf'))"

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

# Share links: one live observation-scoped link and one revoked link. The
# observation of each is chosen at generation time (see pick_share_link_scopes).
SHARE_LINKS = [
    {
        'token': 'seed-link-observation-0001',
        'label': 'Seed observation link (live)',
        'user_id': LINK_OBS_UUID,
        'email': 'link+seed-link-observation-0001@shared.invalid',
        'username': 'link-seed000000000001',
        'revoked': False,
    },
    {
        'token': 'seed-link-revoked-0002',
        'label': 'Seed observation link (revoked)',
        'user_id': LINK_REVOKED_UUID,
        'email': 'link+seed-link-revoked-0002@shared.invalid',
        'username': 'link-seed000000000002',
        'revoked': True,
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


# === Scrubs: public-program guard + identity map ===

class PublicGuard:
    """Refuses to emit anything that references a non-public program.

    The seed is committed to a public repository, so this is a hard stop, not
    a filter: a violation means the sampling or a fetch went wrong and the run
    aborts before anything is written.
    """

    def __init__(self, public_programs: list[dict]):
        self.public = {p['slug'] for p in public_programs if p.get('is_public') is True}
        if len(self.public) != len(public_programs):
            raise SystemExit("PublicGuard: a non-public program reached the program list")

    def slug(self, slug, what: str) -> None:
        if slug not in self.public:
            raise SystemExit(f"REFUSING to emit {what}: references non-public program {slug!r}")

    def rows(self, rows: list[dict], key: str, what: str) -> None:
        for row in rows:
            label = row.get('name') or row.get('target_id') or row.get('storage_key') or row.get('id')
            self.slug(row.get(key), f"{what} {label!r}")

    def filter_slugs(self, slugs: list[str] | None) -> list[str]:
        """Drop non-public slugs from a derived text[] (fields.programs)."""
        return [s for s in (slugs or []) if s in self.public]


class IdentityMap:
    """Maps every real auth.users uuid onto the synthetic inspect-capable users.

    Deterministic: the distinct real uuids are sorted and assigned round-robin
    to admin@ / user@ (the viewer cannot comment or inspect, so attributing an
    inspection or a comment to it would be inconsistent with its role).
    """

    TARGETS = (ADMIN_UUID, USER_UUID)

    def __init__(self):
        self.seen: set[str] = set()
        self.mapping: dict[str, str] = {}

    def note(self, value) -> None:
        if value:
            self.seen.add(str(value))

    def note_rows(self, rows: list[dict], key: str) -> None:
        for row in rows:
            self.note(row.get(key))

    def freeze(self) -> None:
        for i, real in enumerate(sorted(self.seen)):
            self.mapping[real] = self.TARGETS[i % len(self.TARGETS)]

    def __call__(self, value):
        if not value:
            return None
        value = str(value)
        if value in SYNTHETIC_UUIDS:
            return value
        if value not in self.mapping:
            raise SystemExit(f"IdentityMap: unmapped production uuid met at emit time: {value}")
        return self.mapping[value]


# === Query Production Data ===

PROGRAM_COLUMNS = ['slug', 'program_name', 'pi_name', 'description', 'cycle', 'is_public']


def fetch_public_programs(supabase) -> list[dict]:
    """Public, non-retired programs from the production registry (the authority)."""
    resp = supabase.table('programs').select(','.join(PROGRAM_COLUMNS + ['retired_at'])) \
        .eq('is_public', True).is_('retired_at', 'null').order('slug').execute()
    return [{k: p.get(k) for k in PROGRAM_COLUMNS} for p in resp.data]


def _in_batches(supabase, table: str, column: str, values: list, batch_size: int = 50,
                select: str = '*', extra=None) -> list[dict]:
    """`SELECT select FROM table WHERE column IN values`, batched for URL length."""
    out = []
    values = list(values)
    for i in range(0, len(values), batch_size):
        batch = values[i:i + batch_size]
        q = supabase.table(table).select(select).in_(column, batch)
        if extra:
            q = extra(q)
        out.extend(q.execute().data)
    return out


def select_targets(supabase, targets_per_program: int, program_slugs: list[str]) -> list[dict]:
    """
    Select a representative subset of targets from production.

    For each program, picks published targets with variety across quality levels:
    - 1-2 with quality 4 (secure) with flags set
    - 1 with quality 2-3 (tentative/probable)
    - 1 with quality 0 (uninspected)
    - 1 with quality 1 (impossible) if available
    """
    slugs = sorted(program_slugs)

    print(f"Sampling from {len(slugs)} programs: {slugs}")

    all_targets = []
    seen_ids = set()

    def base(slug):
        return supabase.table('targets').select('*') \
            .eq('program_slug', slug).eq('has_published_spectrum', True).order('id')

    for slug in slugs:
        program_targets = []

        def take(rows, cap=None):
            for t in rows:
                if t['target_id'] not in seen_ids:
                    program_targets.append(t)
                    seen_ids.add(t['target_id'])
                    if cap is not None and len(program_targets) >= cap:
                        return

        # Quality 4 (secure) - prefer targets with flags set
        take(base(slug).eq('redshift_quality', 4).gt('spectral_features', 0).limit(2).execute().data)

        # If we didn't get 2 quality-4, get more without flag requirement
        if len([t for t in program_targets if t['redshift_quality'] == 4]) < 2:
            take(base(slug).eq('redshift_quality', 4).limit(2).execute().data, cap=2)

        # Quality 2-3 (tentative/probable)
        for q in [3, 2]:
            rows = base(slug).eq('redshift_quality', q).limit(1).execute().data
            if rows:
                take(rows)
                break

        # Quality 0 (uninspected)
        take(base(slug).eq('redshift_quality', 0).limit(1).execute().data)

        # Quality 1 (impossible)
        take(base(slug).eq('redshift_quality', 1).limit(1).execute().data)

        # Cap at targets_per_program
        program_targets = program_targets[:targets_per_program]

        print(f"  Program {slug}: selected {len(program_targets)} targets "
              f"(qualities: {[t['redshift_quality'] for t in program_targets]})")
        all_targets.extend(program_targets)

    return all_targets


def select_line_fit_targets(supabase, program_slugs: list[str], seen_target_ids: set[str],
                            per_program: int = 2, max_total: int = 6) -> list[dict]:
    """Top up the sample with a few targets that carry emission-line fits.

    Line fits exist only for inspected spectra of a handful of programs, so the
    stratified sample may miss them entirely; this adds up to ``max_total``
    published targets (at most ``per_program`` each) whose spectra have a
    quality >= 3 fit with detected lines.
    """
    rows = supabase.table('spectrum_line_fits') \
        .select('target_id,program_slug') \
        .in_('program_slug', sorted(program_slugs)) \
        .gte('z_quality', 3).gt('n_detected', 0) \
        .order('fitted_at', desc=True).limit(80).execute().data
    chosen: list[str] = []
    per: dict[str, int] = defaultdict(int)
    for r in rows:
        tid = r['target_id']
        if tid in seen_target_ids or tid in chosen or per[r['program_slug']] >= per_program:
            continue
        chosen.append(tid)
        per[r['program_slug']] += 1
        if len(chosen) >= max_total:
            break
    if not chosen:
        return []
    targets = _in_batches(supabase, 'targets', 'target_id', chosen,
                          extra=lambda q: q.eq('has_published_spectrum', True))
    print(f"  Line-fit top-up: {len(targets)} targets from {dict(per)}")
    return targets


def fetch_observations(supabase, obs_names: list[str]) -> list[dict]:
    """Fetch observations for the given observation names."""
    if not obs_names:
        return []
    return _in_batches(supabase, 'observations', 'name', obs_names)


def fetch_spectra(supabase, target_ids: list[str]) -> list[dict]:
    """Fetch the *published* spectra of the given target_ids.

    Only published rows leave production: a target is admitted on
    ``has_published_spectrum`` and may carry a draft or revoked sibling whose
    thumbnails, hashes and registry rows must never reach the public seed. The
    draft / revoked lifecycle fixtures are demoted from these published rows
    (``apply_lifecycle_victims``), never taken from production's own drafts.
    """
    if not target_ids:
        return []
    return _in_batches(supabase, 'spectra', 'target_id', target_ids,
                       extra=lambda q: q.eq('deploy_status', 'published'))


def fetch_comments(supabase, target_int_ids: list[int]) -> list[dict]:
    """Fetch comments for the given target integer IDs (content is discarded)."""
    if not target_int_ids:
        return []
    return _in_batches(supabase, 'comments', 'target_id', target_int_ids,
                       select='id,target_id,object_id,user_id,created_at,edited_at,is_deleted')


def fetch_flag_audit_log(supabase, target_int_ids: list[int]) -> list[dict]:
    """Fetch flag audit log entries for the given target integer IDs."""
    if not target_int_ids:
        return []
    return _in_batches(supabase, 'flag_audit_log', 'target_id', target_int_ids)


def fetch_line_fits(supabase, spectrum_db_ids: list[int]) -> list[dict]:
    """Fetch spectrum_line_fits rows (with their `lines` jsonb) for the sampled spectra."""
    if not spectrum_db_ids:
        return []
    return _in_batches(supabase, 'spectrum_line_fits', 'spectrum_id', spectrum_db_ids)


def fetch_fields(supabase) -> list[dict]:
    """All non-retired `fields` rows (a handful; NIRCam fields only)."""
    return supabase.table('fields').select('*').is_('retired_at', 'null').order('name').execute().data


def fetch_photometry(supabase, prod_object_ids: list[int]) -> list[dict]:
    """object_photometry rows cross-matched to the production objects of the sample."""
    if not prod_object_ids:
        return []
    return _in_batches(supabase, 'object_photometry', 'object_id', prod_object_ids)


def fetch_observation_deployments(supabase, obs_names: list[str]) -> dict[str, dict]:
    """The latest *published* deployment of each sampled observation."""
    rows = _in_batches(supabase, 'deployments', 'observation', obs_names,
                       extra=lambda q: q.eq('status', 'published'))
    latest: dict[str, dict] = {}
    for r in rows:
        cur = latest.get(r['observation'])
        if cur is None or r['id'] > cur['id']:
            latest[r['observation']] = r
    return latest


SIDECAR_SUFFIXES = ('_zfit.json', '_lines.json')


def fetch_storage_rows(supabase, spectra: list[dict]) -> tuple[list[dict], list[dict]]:
    """Production storage_objects rows for the sampled spectra and their sidecars.

    Pass 1 resolves by ``spectrum_id`` (the FITS final, the full JSON and the
    1-D JSON all carry it); pass 2 fetches the zfit / line-fit sidecars by key,
    built from the final's directory (those rows carry no spectrum_id).
    Returns (rows, spectra with no production final).
    """
    spectrum_ids = [s['spectrum_id'] for s in spectra]
    rows = _in_batches(supabase, 'storage_objects', 'spectrum_id', spectrum_ids,
                       extra=lambda q: q.eq('status', 'active'))
    final_dir: dict[str, str] = {}
    for r in rows:
        if r['product_type'] == 'nirspec_spec':
            final_dir[r['spectrum_id']] = r['storage_key'].rsplit('/', 1)[0]
    sidecar_keys = [f"{d}/{sid}{suffix}" for sid, d in final_dir.items() for suffix in SIDECAR_SUFFIXES]
    rows += _in_batches(supabase, 'storage_objects', 'storage_key', sidecar_keys,
                        extra=lambda q: q.eq('status', 'active'))
    missing = [s for s in spectra if s['spectrum_id'] not in final_dir]
    return rows, missing


def select_nircam(supabase, fields: list[dict], sampled_fields: set[str],
                  preferred: str | None, n_published: int = 8) -> dict | None:
    """Pick one real NIRCam field and gather its published / draft mosaics + deployments.

    Returns ``{'field', 'images', 'deployments'}`` or None when production has
    no published mosaic in any field. Prefers ``preferred``, then fields the
    sampled targets live in.
    """
    names = [f['name'] for f in fields]
    order = ([preferred] if preferred in names else []) \
        + sorted(n for n in names if n in sampled_fields and n != preferred) \
        + sorted(n for n in names if n not in sampled_fields and n != preferred)
    for name in order:
        n_pub = supabase.table('nircam_images').select('id', count='exact') \
            .eq('field', name).eq('deploy_status', 'published').limit(0).execute().count
        if not n_pub:
            continue
        pub_dep = supabase.table('deployments').select('*').eq('field', name) \
            .eq('status', 'published').order('id', desc=True).limit(1).execute().data
        pub_dep = pub_dep[0] if pub_dep else None
        img_q = supabase.table('nircam_images').select('*').eq('field', name) \
            .eq('deploy_status', 'published').order('id')
        if pub_dep:
            img_q = img_q.eq('deployment_id', pub_dep['id'])
        images = img_q.limit(n_published).execute().data
        if not images and pub_dep:
            images = supabase.table('nircam_images').select('*').eq('field', name) \
                .eq('deploy_status', 'published').order('id').limit(n_published).execute().data
        draft = supabase.table('nircam_images').select('*').eq('field', name) \
            .eq('deploy_status', 'draft').order('id').limit(1).execute().data
        deployments = {}
        if pub_dep:
            deployments[pub_dep['id']] = pub_dep
        dep_ids = {img['deployment_id'] for img in images + draft if img.get('deployment_id')}
        for dep in _in_batches(supabase, 'deployments', 'id', sorted(dep_ids - set(deployments))):
            deployments[dep['id']] = dep
        return {
            'field': name,
            'published_deployment': pub_dep,
            'images': images,
            'draft_image': draft[0] if draft else None,
            'deployments': deployments,
        }
    return None


# === SQL Generation ===

def generate_auth_users_sql() -> str:
    """Generate INSERT statements for auth.users test accounts + link accounts."""
    lines = ['-- ============================================']
    lines.append('-- 1. Auth Users (test accounts + share-link accounts)')
    lines.append('-- ============================================')
    lines.append('')

    accounts = [(u['id'], u['email'], PASSWORD_SQL, {}) for u in TEST_USERS]
    accounts += [(sl['user_id'], sl['email'], LINK_PASSWORD_SQL, {'share_link': True}) for sl in SHARE_LINKS]

    for user_id, email, password_sql, meta in accounts:
        lines.append(f"""INSERT INTO auth.users (
    id, instance_id, aud, role, email,
    encrypted_password, email_confirmed_at,
    created_at, updated_at, confirmation_token,
    recovery_token, email_change_token_new, email_change,
    raw_user_meta_data
) VALUES (
    {sql_escape(user_id)},
    '00000000-0000-0000-0000-000000000000',
    'authenticated',
    'authenticated',
    {sql_escape(email)},
    {password_sql},
    NOW(),
    NOW(),
    NOW(),
    '',
    '',
    '',
    '',
    {sql_json(meta)}
);""")
        lines.append('')

        # Also insert identity for each user
        lines.append(f"""INSERT INTO auth.identities (
    id, user_id, identity_data, provider, provider_id,
    last_sign_in_at, created_at, updated_at
) VALUES (
    gen_random_uuid(),
    {sql_escape(user_id)},
    jsonb_build_object('sub', {sql_escape(user_id)}, 'email', {sql_escape(email)}),
    'email',
    {sql_escape(user_id)},
    NOW(),
    NOW(),
    NOW()
);""")
        lines.append('')

    return '\n'.join(lines)


def generate_programs_sql(programs: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for programs (public only, enforced)."""
    lines = ['-- ============================================']
    lines.append('-- 2. Programs (public only)')
    lines.append('-- ============================================')
    lines.append('')

    for p in programs:
        guard.slug(p['slug'], f"program {p['slug']!r}")
        if p.get('is_public') is not True:
            raise SystemExit(f"REFUSING to emit program {p['slug']!r}: is_public is not true")
        lines.append(f"""INSERT INTO public.programs (slug, program_name, pi_name, description, cycle, is_public)
VALUES ({sql_escape(p['slug'])}, {sql_escape(p['program_name'])}, {sql_escape(p.get('pi_name'))}, {sql_escape(p.get('description') or '')}, {sql_escape(p.get('cycle'))}, TRUE);""")

    lines.append('')
    return '\n'.join(lines)


OBSERVATION_COLUMNS = ['name', 'program_slug', 'jwst_program_id', 'field', 'file_globs', 'gratings',
                       'data_subdir', 'pointings', 'config', 'config_hash', 'config_updated_at']
OBSERVATION_JSON = {'pointings', 'config'}


def generate_observations_sql(observations: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for observations (latest_deployment_id set later)."""
    lines = ['-- ============================================']
    lines.append('-- 2b. Observations')
    lines.append('-- ============================================')
    lines.append('')

    for obs in observations:
        guard.slug(obs['program_slug'], f"observation {obs['name']!r}")
        lines.append(f"INSERT INTO public.observations ({', '.join(OBSERVATION_COLUMNS)})\n"
                     f"VALUES ({sql_row(obs, OBSERVATION_COLUMNS, OBSERVATION_JSON)});")

    lines.append('')
    return '\n'.join(lines)


FIELD_COLUMNS = ['name', 'display_name', 'filters', 'tiles', 'fiducial_tiles', 'epochs', 'programs',
                 'jwst_program_ids', 'file_globs', 'center_ra', 'center_dec', 'config',
                 'coverage_area_arcmin2', 'coverage_area_deg2', 'expmap_pixel_scale_arcsec',
                 'config_hash', 'config_updated_at']
FIELD_JSON = {'config'}


def generate_fields_sql(fields: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for fields (programs[] scrubbed to public slugs)."""
    lines = ['-- ============================================']
    lines.append('-- 2c. Fields (NIRCam; programs[] limited to public slugs)')
    lines.append('-- ============================================')
    lines.append('')

    for f in fields:
        row = dict(f)
        row['programs'] = guard.filter_slugs(f.get('programs'))
        lines.append(f"INSERT INTO public.fields ({', '.join(FIELD_COLUMNS)})\n"
                     f"VALUES ({sql_row(row, FIELD_COLUMNS, FIELD_JSON)});")

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
    """Generate INSERT statements for user_profiles (test users + read-only link accounts)."""
    lines = ['-- ============================================']
    lines.append('-- 4. User Profiles')
    lines.append('-- ============================================')
    lines.append('')

    for user in TEST_USERS:
        lines.append(f"""INSERT INTO public.user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect)
VALUES ({sql_escape(user['id'])}, {sql_escape(user['username'])}, {sql_escape(user['full_name'])}, {sql_escape(user['is_admin'])}, {sql_escape(user['can_comment'])}, {sql_escape(user['can_inspect'])});""")

    lines.append('')
    lines.append('-- Share-link accounts: read-only by construction (user_profiles_link_account_readonly).')
    for sl in SHARE_LINKS:
        lines.append(f"""INSERT INTO public.user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect, is_group_account, is_link_account)
VALUES ({sql_escape(sl['user_id'])}, {sql_escape(sl['username'])}, {sql_escape(sl['label'])}, FALSE, FALSE, FALSE, FALSE, TRUE);""")

    lines.append('')
    return '\n'.join(lines)


def generate_objects_sql(
    objects: list[dict],
    target_to_object_db_id: dict[int, int],
    identity: IdentityMap,
) -> str:
    """Generate INSERT statements for targets (skipping generated columns: redshift)."""
    lines = ['-- ============================================']
    lines.append('-- 5. Targets (from production)')
    lines.append('-- ============================================')
    lines.append('')

    for obj in objects:
        inspected_by = sql_escape(identity(obj.get('last_inspected_by')))
        inspected_at = sql_escape(obj.get('last_inspected_at')) if obj.get('last_inspected_at') else 'NULL'

        # Handle redshift_inspected (numeric type)
        redshift_inspected = obj.get('redshift_inspected')
        if redshift_inspected is not None:
            redshift_inspected_sql = str(redshift_inspected)
        else:
            redshift_inspected_sql = 'NULL'

        object_fk = target_to_object_db_id.get(obj['id'])
        object_fk_sql = str(object_fk) if object_fk else 'NULL'

        lines.append(f"""INSERT INTO public.targets (id, target_id, program_slug, observation, field, ra, dec, redshift_auto, redshift_inspected, redshift_quality, spectral_features, dq_flags, max_snr, max_exposure_time, last_inspected_at, last_inspected_by, object_id, has_published_spectrum)
VALUES ({obj['id']}, {sql_escape(obj['target_id'])}, {sql_escape(obj['program_slug'])}, {sql_escape(obj.get('observation', ''))}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {obj.get('spectral_features', 0)}, {obj.get('dq_flags', 0)}, {sql_escape(obj.get('max_snr'))}, {sql_escape(obj.get('max_exposure_time'))}, {inspected_at}, {inspected_by}, {object_fk_sql}, {sql_escape(obj.get('_has_published', True))});""")

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


def anchor_objects(
    objects: list[dict],
    target_to_object_db_id: dict[int, int],
    anchor_program: str = ANCHOR_PROGRAM,
    n_required: int = 3,
) -> list[dict]:
    """Renumber objects so the anchor program's objects (with spectra) come first.

    The seed-anchored harnesses assume objects 1 & 2 (lifecycle victims) and 3
    (published control) are all in ``anchor_program`` with member spectra.
    Stable: relative order is otherwise unchanged. Aborts when the sample does
    not carry enough anchor objects.
    """
    def is_anchor(o):
        return anchor_program in o['programs'] and o['n_spectra'] > 0

    ordered = [o for o in objects if is_anchor(o)] + [o for o in objects if not is_anchor(o)]
    n_anchor = sum(1 for o in ordered if is_anchor(o))
    if n_anchor < n_required:
        raise SystemExit(f"Seed anchor broken: only {n_anchor} objects of {anchor_program!r} carry "
                         f"spectra (need {n_required}); increase --objects-per-program")
    old_to_new = {}
    for new_id, obj in enumerate(ordered, start=1):
        old_to_new[obj['_db_id']] = new_id
        obj['_db_id'] = new_id
    for target_db_id, old in list(target_to_object_db_id.items()):
        target_to_object_db_id[target_db_id] = old_to_new[old]
    return ordered


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


# === Lifecycle fixtures: a draft and a revoked spectrum with their deployments ===

def pick_lifecycle_victims(
    targets: list[dict],
    spectra: list[dict],
    objects_by_db_id: dict[int, dict],
    target_to_object_db_id: dict[int, int],
) -> list[tuple[str, dict, dict, dict]]:
    """Choose one spectrum to seed as 'draft' and one as 'revoked'.

    Candidates are single-spectrum targets that form a singleton object outside
    the anchor program (so objects 1-3 and the harness anchors are untouched,
    and flipping the spectrum flips exactly one target + one object), in two
    different observations. Returns [(status, target, spectrum, object), ...].
    """
    spectra_by_target: dict[str, list[dict]] = defaultdict(list)
    for s in spectra:
        spectra_by_target[s['target_id']].append(s)

    picks: list[tuple[str, dict, dict, dict]] = []
    used_obs: set[str] = set()
    for t in sorted(targets, key=lambda t: t['target_id']):
        if t['program_slug'] == ANCHOR_PROGRAM or t['observation'] in used_obs:
            continue
        obj = objects_by_db_id.get(target_to_object_db_id.get(t['id']))
        if obj is None or obj['n_targets'] != 1:
            continue
        specs = spectra_by_target.get(t['target_id'], [])
        if len(specs) != 1:
            continue
        status = 'draft' if not picks else 'revoked'
        picks.append((status, t, specs[0], obj))
        used_obs.add(t['observation'])
        if len(picks) == 2:
            break
    if len(picks) < 2:
        raise SystemExit("Could not find two single-spectrum singleton targets outside the anchor "
                         "program for the draft/revoked fixtures; increase --objects-per-program")
    return picks


def apply_lifecycle_victims(picks, spectra: list[dict], targets: list[dict]) -> None:
    """Flip the chosen spectra and the publication flags of their target + object.

    Every sampled spectrum is a production *published* row (``fetch_spectra``
    filters on it); this never promotes a row, it only demotes the two victims.
    """
    unpublished = [s['id'] for s in spectra if s.get('deploy_status') != 'published']
    if unpublished:
        raise SystemExit(f"{len(unpublished)} non-published production spectra reached the sample "
                         f"(ids {unpublished[:5]}); fetch_spectra must filter on deploy_status")
    for t in targets:
        t['_has_published'] = True
    for status, target, spectrum, obj in picks:
        spectrum['deploy_status'] = status
        target['_has_published'] = False
        obj['_has_published'] = False


DEPLOYMENT_COLUMNS = ['id', 'observation', 'field', 'deployed_by', 'deployed_at', 'cfpipe_version',
                      'jwst_version', 'crds_context', 'config_snapshot', 'n_targets', 'n_spectra',
                      'n_new_targets', 'force_overwrite', 'source_ids_filter', 'supabase_only',
                      'stuck_shutters', 'reduced_at', 'status', 'published_at', 'revoked_at']
DEPLOYMENT_JSON = {'config_snapshot', 'stuck_shutters'}


def build_deployments(
    obs_deployments: dict[str, dict],
    nircam: dict | None,
    picks,
    observations: list[dict],
) -> tuple[list[dict], dict[str, int], set[int]]:
    """Assemble the deployments rows: real published ones + synthetic draft/revoked.

    Returns (rows, {draft|revoked: deployment id}, ids of the real rows). Real
    rows keep their production ids (traceability); synthetic rows take ids
    above production's id space (so a production foreign key can never
    coincide with one). Observations without any published deployment in
    production get a minimal synthetic published row so every seeded
    observation has one.
    """
    rows: dict[int, dict] = {}
    for dep in obs_deployments.values():
        rows[dep['id']] = dict(dep)
    if nircam:
        for dep in nircam['deployments'].values():
            rows[dep['id']] = dict(dep)
    real_ids = set(rows)

    next_id = SYNTHETIC_DEPLOYMENT_ID_BASE
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for obs in observations:
        if obs['name'] in obs_deployments:
            continue
        rows[next_id] = {
            'id': next_id, 'observation': obs['name'], 'field': None,
            'deployed_at': now.isoformat(), 'status': 'published',
            'published_at': now.isoformat(), 'supabase_only': True,
        }
        obs_deployments[obs['name']] = rows[next_id]
        next_id += 1

    lifecycle_ids: dict[str, int] = {}
    for status, target, spectrum, obj in picks:
        base = obs_deployments.get(target['observation'], {})
        when = now - timedelta(days=1 if status == 'revoked' else 0)
        rows[next_id] = {
            'id': next_id,
            'observation': target['observation'],
            'field': None,
            'deployed_at': when.isoformat(),
            'cfpipe_version': base.get('cfpipe_version'),
            'jwst_version': base.get('jwst_version'),
            'crds_context': base.get('crds_context'),
            'n_targets': 1,
            'n_spectra': 1,
            'n_new_targets': 0,
            'force_overwrite': False,
            'supabase_only': False,
            'status': status,
            'published_at': when.isoformat() if status == 'revoked' else None,
            'revoked_at': now.isoformat() if status == 'revoked' else None,
        }
        lifecycle_ids[status] = next_id
        next_id += 1

    return [rows[i] for i in sorted(rows)], lifecycle_ids, real_ids


def generate_deployments_sql(deployments: list[dict], observations: list[dict],
                             obs_deployments: dict[str, dict], fields: list[dict],
                             real_ids: set[int], identity: IdentityMap, guard: PublicGuard) -> str:
    """Generate INSERT statements for deployments + the latest_deployment_id back-links."""
    lines = ['-- ============================================']
    lines.append('-- 6b. Deployments (published per observation; one draft + one revoked; NIRCam field)')
    lines.append('-- ============================================')
    lines.append('')

    obs_program = {o['name']: o['program_slug'] for o in observations}
    lines.append(f"-- config_snapshot (the ~5 KB pipeline config of each deploy) is kept for the "
                 f"{ANCHOR_PROGRAM!r} deployments only, for size.")
    for dep in deployments:
        if dep.get('observation'):
            guard.slug(obs_program.get(dep['observation']), f"deployment {dep['id']} of {dep['observation']!r}")
        row = dict(dep)
        row['deployed_by'] = identity(dep.get('deployed_by')) or ADMIN_UUID
        if obs_program.get(dep.get('observation')) != ANCHOR_PROGRAM:
            row['config_snapshot'] = None
        lines.append(f"INSERT INTO public.deployments ({', '.join(DEPLOYMENT_COLUMNS)})\n"
                     f"VALUES ({sql_row(row, DEPLOYMENT_COLUMNS, DEPLOYMENT_JSON)});")

    lines.append('')
    present = real_ids  # a production back-link is kept only when that deployment is seeded
    for obs in observations:
        dep = obs_deployments.get(obs['name'])
        if dep:
            lines.append(f"UPDATE public.observations SET latest_deployment_id = {dep['id']} "
                         f"WHERE name = {sql_escape(obs['name'])};")
    for f in fields:
        if f.get('latest_deployment_id') in present:
            lines.append(f"UPDATE public.fields SET latest_deployment_id = {f['latest_deployment_id']} "
                         f"WHERE name = {sql_escape(f['name'])};")

    lines.append('')
    return '\n'.join(lines)


NIRCAM_IMAGE_COLUMNS = ['id', 'field', 'tile', 'filter', 'pixel_scale', 'extension', 'epoch',
                        'file_path', 'file_size', 'deploy_status', 'deployment_id', 'created_at']


def generate_nircam_images_sql(nircam: dict | None) -> str:
    """Generate INSERT statements for nircam_images (published rows + one draft)."""
    lines = ['-- ============================================']
    lines.append('-- 6c. NIRCam mosaics (one real field; published + one draft)')
    lines.append('-- ============================================')
    lines.append('')
    if not nircam:
        lines.append('-- No published NIRCam mosaics in production at generation time')
        lines.append('')
        return '\n'.join(lines)

    images = list(nircam['images'])
    if nircam['draft_image']:
        images.append(nircam['draft_image'])
    for img in images:
        lines.append(f"INSERT INTO public.nircam_images ({', '.join(NIRCAM_IMAGE_COLUMNS)})\n"
                     f"VALUES ({sql_row(img, NIRCAM_IMAGE_COLUMNS)});")

    lines.append('')
    return '\n'.join(lines)


def generate_objects_table_sql(objects: list[dict], identity: IdentityMap) -> str:
    """Generate INSERT statements for the objects table."""
    lines = ['-- ============================================']
    lines.append('-- 4b. Objects (cross-matched from targets; anchor program first)')
    lines.append('-- ============================================')
    lines.append('')

    for obj in objects:
        inspected_by = sql_escape(identity(obj.get('last_inspected_by')))
        inspected_at = sql_escape(obj.get('last_inspected_at')) if obj.get('last_inspected_at') else 'NULL'
        last_data_change_at = sql_escape(obj.get('last_data_change_at')) if obj.get('last_data_change_at') else 'NULL'
        redshift_inspected = obj.get('redshift_inspected')
        redshift_inspected_sql = str(redshift_inspected) if redshift_inspected is not None else 'NULL'

        lines.append(f"""INSERT INTO public.objects (id, object_id, field, ra, dec, n_targets, n_spectra, programs, gratings, observations, max_snr, max_exposure_time, redshift_auto, redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by, last_data_change_at, has_published_spectrum)
VALUES ({obj['_db_id']}, {sql_escape(obj['object_id'])}, {sql_escape(obj['field'])}, {obj['ra']}, {obj['dec']}, {obj['n_targets']}, {obj['n_spectra']}, {sql_escape(obj['programs'])}, {sql_escape(obj['gratings'])}, {sql_escape(obj['observations'])}, {sql_escape(obj['max_snr'])}, {sql_escape(obj['max_exposure_time'])}, {sql_escape(obj.get('redshift_auto'))}, {redshift_inspected_sql}, {obj.get('redshift_quality', 0)}, {inspected_at}, {inspected_by}, {last_data_change_at}, {sql_escape(obj.get('_has_published', True))});""")

    lines.append('')
    return '\n'.join(lines)


def generate_spectra_sql(spectra: list[dict]) -> str:
    """Generate INSERT statements for spectra (program_slug/observation are trigger-owned)."""
    lines = ['-- ============================================']
    lines.append('-- 6. Spectra (published production rows only; the two lifecycle fixtures are demoted copies of such rows)')
    lines.append('-- ============================================')
    lines.append('')

    for spec in spectra:
        lines.append(f"""INSERT INTO public.spectra (id, target_id, grating, fits_path, cfpipe_version, jwst_version, crds_context, date_obs, reduced_at, file_hash, file_size, signal_to_noise, exposure_time, thumbnail_svg_fnu, thumbnail_svg_flambda, redshift_auto, chi2_min, confidence, dq_flags, deploy_status)
VALUES ({spec['id']}, {sql_escape(spec.get('target_id') or spec.get('object_id'))}, {sql_escape(spec['grating'])}, {sql_escape(spec['fits_path'])}, {sql_escape(spec.get('cfpipe_version') or spec.get('reduction_version') or 'v0.1')}, {sql_escape(spec.get('jwst_version'))}, {sql_escape(spec.get('crds_context'))}, {sql_escape(spec.get('date_obs'))}, {sql_escape(spec.get('reduced_at'))}, {sql_escape(spec.get('file_hash'))}, {sql_escape(spec.get('file_size'))}, {sql_escape(spec.get('signal_to_noise'))}, {sql_escape(spec.get('exposure_time'))}, {sql_escape(spec.get('thumbnail_svg_fnu'))}, {sql_escape(spec.get('thumbnail_svg_flambda'))}, {sql_escape(spec.get('redshift_auto'))}, {sql_escape(spec.get('chi2_min'))}, {sql_escape(spec.get('confidence'))}, {spec.get('dq_flags') or 0}, {sql_escape(spec.get('deploy_status', 'published'))});""")

    lines.append('')
    return '\n'.join(lines)


STORAGE_COLUMNS = ['backend', 'bucket', 'storage_key', 'content_hash', 'sci_dq_hash', 'wcs_hash',
                   'size_bytes', 'stored_size_bytes', 'content_type', 'product_type', 'instrument',
                   'status', 'observation', 'field', 'filter', 'spectrum_id', 'exposure_ref',
                   'deployment_id', 'cfpipe_version', 'uploaded_by', 'created_at', 'updated_at']


def generate_storage_objects_sql(storage_rows: list[dict], missing_spectra: list[dict],
                                 picks, lifecycle_ids: dict[str, int], nircam: dict | None,
                                 obs_set: set[str], seeded_deployment_ids: set[int],
                                 identity: IdentityMap) -> str:
    """Generate INSERT statements for the storage_objects registry (#214).

    Real production rows (content_hash / size_bytes) for every sampled spectrum
    and its sidecars; a synthetic ``sha256:`` of the key + 1 MiB only for a
    spectrum that has no production final (flagged in a comment). The rows of
    the draft / revoked spectra are attached to their lifecycle deployment.
    ``id`` is omitted so the sequence assigns it.
    """
    lines = ['-- ============================================']
    lines.append('-- 6d. Storage objects registry (real hashes; #214)')
    lines.append('-- ============================================')
    lines.append('')

    victim_dep = {spectrum['spectrum_id']: lifecycle_ids[status] for status, _, spectrum, _ in picks}

    def attach(row):
        """Lifecycle deployment for a victim's rows (the zfit / lines sidecars carry no
        spectrum_id, so match on the key's basename); otherwise keep a production
        deployment_id only when that deployment is seeded."""
        basename = row['storage_key'].rsplit('/', 1)[-1]
        for spectrum_id, dep_id in victim_dep.items():
            if row.get('spectrum_id') == spectrum_id or basename.startswith(spectrum_id + '_'):
                return dep_id
        return row.get('deployment_id') if row.get('deployment_id') in seeded_deployment_ids else None

    pending: list[str] = []

    def flush():
        # Multi-row VALUES (100 per statement): the column list is a third of a row's bytes.
        for i in range(0, len(pending), 100):
            chunk = pending[i:i + 100]
            lines.append(f"INSERT INTO public.storage_objects ({', '.join(STORAGE_COLUMNS)}) VALUES\n"
                         + ',\n'.join(f"({tup})" for tup in chunk) + ';')
        pending.clear()

    def emit(row):
        out = dict(row)
        if out.get('observation') and out['observation'] not in obs_set:
            out['observation'] = None  # FK: only seeded observations
        out['uploaded_by'] = identity(row.get('uploaded_by'))
        out['deployment_id'] = attach(row)
        pending.append(sql_row(out, STORAGE_COLUMNS))

    for r in sorted(storage_rows, key=lambda r: r['storage_key']):
        emit(r)
    flush()

    if missing_spectra:
        lines.append('')
        lines.append('-- Spectra with no production registry row: synthetic hash + size stand in')
        for spec in missing_spectra:
            key = f"data/products/nirspec/{spec['observation']}/{spec['spectrum_id']}_spec.fits"
            digest = hashlib.sha256(key.encode()).hexdigest()
            emit({
                'backend': 'osn', 'bucket': 'data', 'storage_key': key,
                'content_hash': 'sha256:' + digest, 'size_bytes': 1048576,
                'content_type': 'application/fits', 'product_type': 'nirspec_spec',
                'instrument': 'nirspec', 'status': 'active', 'observation': spec['observation'],
                'spectrum_id': spec['spectrum_id'], 'deployment_id': victim_dep.get(spec['spectrum_id']),
            })
        flush()

    if nircam and nircam.get('storage_rows'):
        lines.append('')
        lines.append(f"-- NIRCam mosaic finals of field {nircam['field']!r}")
        for r in nircam['storage_rows']:
            emit(r)
        flush()

    lines.append('')
    return '\n'.join(lines)


LINE_FIT_COLUMNS = ['spectrum_id', 'target_id', 'grating', 'program_slug', 'observation', 'z_used',
                    'z_source', 'z_quality', 'object_id', 'object_version', 'z_fit', 'z_fit_err',
                    'dv', 'dv_err', 'sigma_v', 'sigma_v_err', 'kin_source', 'n_lines', 'n_detected',
                    'n_broad', 'chi2', 'dof', 'lines', 'fit_version', 'cfpipe_version', 'f_lsf',
                    'spectrum_hash', 'spectrum_science_hash', 'fitted_at', 'created_at', 'updated_at']
LINE_FIT_JSON = {'lines'}


def generate_line_fits_sql(line_fits: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for spectrum_line_fits (spectrum_lines filled by trigger)."""
    lines = ['-- ============================================']
    lines.append('-- 6e. Emission-line fits (spectrum_lines is derived by trigger)')
    lines.append('-- ============================================')
    lines.append('')
    if not line_fits:
        lines.append('-- No line fits in production for the sampled spectra')
    for fit in sorted(line_fits, key=lambda f: f['spectrum_id']):
        guard.slug(fit['program_slug'], f"line fit of spectrum {fit['spectrum_id']}")
        lines.append(f"INSERT INTO public.spectrum_line_fits ({', '.join(LINE_FIT_COLUMNS)})\n"
                     f"VALUES ({sql_row(fit, LINE_FIT_COLUMNS, LINE_FIT_JSON)});")

    lines.append('')
    return '\n'.join(lines)


PHOTOMETRY_COLUMNS = ['id', 'object_id', 'field', 'ra', 'dec', 'catalog_name', 'catalog_id',
                      'match_distance_arcsec', 'photometry', 'photo_z', 'photo_z_err_lo',
                      'photo_z_err_hi', 'has_pz', 'created_at', 'updated_at']
PHOTOMETRY_JSON = {'photometry'}


def generate_photometry_sql(photometry: list[dict], prod_to_seed_object: dict[int, int]) -> str:
    """Generate INSERT statements for object_photometry (bands by trigger; objects synced by RPC)."""
    lines = ['-- ============================================']
    lines.append('-- 6f. Photometry cross-matches (object_photometry_bands is derived by trigger)')
    lines.append('-- ============================================')
    lines.append('')
    if not photometry:
        lines.append('-- No photometry cross-matches in production for the sampled objects')
    fields = set()
    for row in sorted(photometry, key=lambda r: r['id']):
        out = dict(row)
        out['object_id'] = prod_to_seed_object[row['object_id']]
        fields.add(row['field'])
        lines.append(f"INSERT INTO public.object_photometry ({', '.join(PHOTOMETRY_COLUMNS)})\n"
                     f"VALUES ({sql_row(out, PHOTOMETRY_COLUMNS, PHOTOMETRY_JSON)});")
    if fields:
        lines.append('')
        lines.append('-- Denormalize photo_z / has_photometry onto objects (what `campfire deploy photometry` runs)')
        for field in sorted(fields):
            lines.append(f"SELECT public.sync_photometry_to_objects({sql_escape(field)});")

    lines.append('')
    return '\n'.join(lines)


def pick_share_link_scopes(targets: list[dict]) -> list[str]:
    """Observations for the live and the revoked share link: the anchor program's
    first observation, then the next distinct observation in the sample."""
    anchor = sorted(t['observation'] for t in targets if t['program_slug'] == ANCHOR_PROGRAM)
    others = sorted(set(t['observation'] for t in targets) - set(anchor))
    if not anchor or not others:
        raise SystemExit("Share links need an anchor-program observation and one other observation")
    return [anchor[0], others[0]]


def generate_share_links_sql(scopes: list[str], observations: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for share_links (one live, one revoked)."""
    lines = ['-- ============================================']
    lines.append('-- 7b. Share links (observation-scoped; one live, one revoked)')
    lines.append('-- ============================================')
    lines.append('')
    obs_program = {o['name']: o['program_slug'] for o in observations}
    for sl, obs in zip(SHARE_LINKS, scopes):
        guard.slug(obs_program.get(obs), f"share link {sl['token']!r} on {obs!r}")
        revoked = "NOW() - INTERVAL '1 day'" if sl['revoked'] else 'NULL'
        lines.append(
            "INSERT INTO public.share_links (token, label, observation, field, link_user_id, link_password, "
            "include_drafts, allow_download, created_by, created_at, revoked_at) VALUES "
            f"({sql_escape(sl['token'])}, {sql_escape(sl['label'])}, {sql_escape(obs)}, NULL, "
            f"{sql_escape(sl['user_id'])}, {sql_escape(LINK_PASSWORD)}, FALSE, TRUE, "
            f"{sql_escape(ADMIN_UUID)}, NOW() - INTERVAL '7 days', {revoked});"
        )

    lines.append('')
    return '\n'.join(lines)


def generate_user_program_access_sql(programs: list[dict], guard: PublicGuard) -> str:
    """Generate INSERT statements for user_program_access."""
    lines = ['-- ============================================']
    lines.append('-- 7. User Program Access')
    lines.append('-- ============================================')
    lines.append('')

    for user_id in (ADMIN_UUID, USER_UUID, VIEWER_UUID):
        for p in programs:
            guard.slug(p['slug'], f"program access to {p['slug']!r}")
            lines.append(f"""INSERT INTO public.user_program_access (user_id, program_slug)
VALUES ({sql_escape(user_id)}, {sql_escape(p['slug'])});""")

    lines.append('')
    return '\n'.join(lines)


def generate_comments_sql(comments: list[dict], object_id_map: dict[int, int],
                          target_names: dict[int, str], identity: IdentityMap) -> str:
    """Generate INSERT statements for comments (synthetic text, mapped authors)."""
    lines = ['-- ============================================']
    lines.append('-- 8. Comments (production text replaced by synthetic text)')
    lines.append('-- ============================================')
    lines.append('')

    n = 0
    for comment in sorted(comments, key=lambda c: c['id']):
        # Remap object_id (integer) and user_id to test users
        obj_int_id = comment['target_id']
        if obj_int_id not in object_id_map:
            continue
        n += 1
        content = (f"Seed comment {n} on {target_names.get(obj_int_id, obj_int_id)}: "
                   f"synthetic placeholder text (the production comment is not distributed).")
        lines.append(f"""INSERT INTO public.comments (id, target_id, user_id, content, created_at, edited_at, is_deleted)
VALUES ({comment['id']}, {obj_int_id}, {sql_escape(identity(comment['user_id']))}, {sql_escape(content)}, {sql_escape(comment.get('created_at', 'now()'))}, {sql_escape(comment.get('edited_at'))}, {sql_escape(comment.get('is_deleted', False))});""")

    # Add a sample comment if none exist
    if not comments:
        lines.append("-- No comments found in production for selected objects")

    lines.append('')
    return '\n'.join(lines)


def generate_flag_audit_log_sql(entries: list[dict], object_id_map: dict[int, int],
                                identity: IdentityMap) -> str:
    """Generate INSERT statements for flag_audit_log."""
    lines = ['-- ============================================']
    lines.append('-- 9. Flag Audit Log')
    lines.append('-- ============================================')
    lines.append('')

    for entry in sorted(entries, key=lambda e: e['id']):
        obj_int_id = entry['target_id']
        if obj_int_id not in object_id_map:
            continue

        lines.append(f"""INSERT INTO public.flag_audit_log (id, target_id, user_id, field_name, old_value, new_value, changed_at)
VALUES ({entry['id']}, {obj_int_id}, {sql_escape(identity(entry.get('user_id')))}, {sql_escape(entry['field_name'])}, {sql_escape(entry.get('old_value'))}, {sql_escape(entry.get('new_value'))}, {sql_escape(entry.get('changed_at', 'now()'))});""")

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
            'icon': '⭐',  # star
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


def generate_access_codes_sql(guard: PublicGuard) -> str:
    """Generate INSERT statements for access_codes.

    One grants-all code and one program-scoped code. The scoped code names the
    anchor program (public, always in the sample) so `program_slugs` never
    references a program the seed does not carry.
    """
    lines = ['-- ============================================']
    lines.append('-- 10. Access Codes')
    lines.append('-- ============================================')
    lines.append('')

    guard.slug(ANCHOR_PROGRAM, 'program-scoped access code')
    lines.append(f"""INSERT INTO public.access_codes (code, description, grants_all_programs, is_active, created_by)
VALUES ('CAMPFIRE-DEV', 'Development access code - grants all programs', TRUE, TRUE, {sql_escape(ADMIN_UUID)});""")
    lines.append(f"""INSERT INTO public.access_codes (code, description, grants_all_programs, program_slugs, is_active, created_by)
VALUES ('SEED-PROGRAM-ACCESS', {sql_escape(f'Access code scoped to {ANCHOR_PROGRAM}')}, FALSE, ARRAY[{sql_escape(ANCHOR_PROGRAM)}], TRUE, {sql_escape(ADMIN_UUID)});""")

    lines.append('')
    return '\n'.join(lines)


def generate_dr_fixture_sql() -> str:
    """A tiny published data release frozen from the seeded catalog (P4a).

    Runs the real freeze (dr_freeze_job) over the rows above, so the fixture
    can never drift from the seed: whatever is published here is what
    DR-seed carries. The RLS matrix (supabase/tests/check_dr_freeze.sql) and
    the later export / web work read it. Components: the NIRSpec catalog and
    the seeded NIRCam field's published mosaics.
    """
    lines = ['-- ============================================']
    lines.append('-- 13. Data release fixture (P4a): DR-seed, frozen by the real freeze')
    lines.append('-- ============================================')
    lines.append('')
    lines.append("INSERT INTO public.dr_releases (tag, state, components, created_by, freeze_started_at, notes)")
    lines.append("SELECT 'DR-seed', 'freezing',")
    lines.append("       (SELECT jsonb_agg(c) FROM (SELECT 'nirspec' AS c UNION ALL")
    lines.append("         SELECT DISTINCT 'nircam/' || field FROM public.nircam_images WHERE deploy_status = 'published') x),")
    lines.append(f"       '{ADMIN_UUID}', now(), 'Seed fixture: the seeded catalog frozen as a published release.';")
    lines.append("SELECT public.dr_freeze_job('DR-seed');")
    lines.append("UPDATE public.dr_releases SET state = 'published', published_at = now(),")
    lines.append(f"       published_by = '{ADMIN_UUID}' WHERE tag = 'DR-seed' AND state = 'frozen';")
    lines.append("DO $$ BEGIN")
    lines.append("  IF (SELECT state FROM public.dr_releases WHERE tag = 'DR-seed') <> 'published' THEN")
    lines.append("    RAISE EXCEPTION 'seed: DR-seed freeze failed: %', (SELECT error FROM public.dr_releases WHERE tag = 'DR-seed');")
    lines.append("  END IF;")
    lines.append("END $$;")
    lines.append('')
    return '\n'.join(lines)


def generate_sequence_resets(objects: list[dict], spectra: list[dict],
                             comments: list[dict], flag_entries: list[dict],
                             cross_matched_objects: list[dict],
                             deployments: list[dict], nircam: dict | None,
                             photometry: list[dict]) -> str:
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

    # Deployments / NIRCam mosaics / photometry (explicit ids above)
    max_dep_id = max((d['id'] for d in deployments), default=0)
    if max_dep_id > 0:
        lines.append(f"SELECT setval('public.deployments_id_seq', {max_dep_id + 1}, false);")
    nircam_rows = (nircam['images'] + ([nircam['draft_image']] if nircam['draft_image'] else [])) if nircam else []
    max_img_id = max((i['id'] for i in nircam_rows), default=0)
    if max_img_id > 0:
        lines.append(f"SELECT setval('public.nircam_images_id_seq', {max_img_id + 1}, false);")
    max_phot_id = max((p['id'] for p in photometry), default=0)
    if max_phot_id > 0:
        lines.append(f"SELECT setval('public.object_photometry_id_seq', {max_phot_id + 1}, false);")

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
    parser.add_argument(
        '--line-fit-targets', type=int, default=6,
        help='Extra targets with emission-line fits to add to the sample (default: 6)'
    )
    parser.add_argument(
        '--nircam-field', default='a2744',
        help='Preferred field for the NIRCam mosaic rows (default: a2744; falls back to any '
             'field with published mosaics)'
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

    # Connect to production Supabase (read-only use)
    print("Connecting to production Supabase...")
    supabase = create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key']
    )

    programs = fetch_public_programs(supabase)
    guard = PublicGuard(programs)
    print(f"{len(programs)} public programs in the production registry")

    # Select representative targets
    print(f"\nSelecting up to {args.objects_per_program} targets per program...")
    program_slugs = [p['slug'] for p in programs]
    targets = select_targets(supabase, args.objects_per_program, program_slugs)
    if args.line_fit_targets > 0:
        targets += select_line_fit_targets(
            supabase, program_slugs, {t['target_id'] for t in targets},
            max_total=args.line_fit_targets,
        )
    guard.rows(targets, 'program_slug', 'target')
    print(f"\nTotal targets selected: {len(targets)}")

    if not targets:
        print("Error: No targets found in production database!")
        sys.exit(1)

    # Build maps
    target_ids = [t['target_id'] for t in targets]
    target_int_ids = [t['id'] for t in targets]
    target_id_map = {t['id']: t['id'] for t in targets}  # identity map (keep original IDs)
    target_names = {t['id']: t['target_id'] for t in targets}

    # Fetch observations for selected targets
    obs_names = sorted(set(t['observation'] for t in targets if t.get('observation')))
    print(f"Fetching observations for {len(obs_names)} observation names...")
    observations = sorted(fetch_observations(supabase, obs_names), key=lambda o: o['name'])
    guard.rows(observations, 'program_slug', 'observation')
    obs_set = {o['name'] for o in observations}
    missing_obs = set(obs_names) - obs_set
    if missing_obs:
        raise SystemExit(f"Targets reference observations missing from the registry: {sorted(missing_obs)}")
    print(f"  Found {len(observations)} observations")

    print("Fetching spectra (published only)...")
    spectra = sorted(fetch_spectra(supabase, target_ids), key=lambda s: s['id'])
    guard.rows(spectra, 'program_slug', 'spectrum')
    print(f"  Found {len(spectra)} spectra")
    # A stale has_published_spectrum flag could admit a target with no published
    # spectrum; such a target would seed an empty object, so drop it here.
    with_spectra = {s['target_id'] for s in spectra}
    orphans = [t['target_id'] for t in targets if t['target_id'] not in with_spectra]
    if orphans:
        print(f"  Dropping {len(orphans)} target(s) with no published spectrum: {orphans[:5]}")
        targets = [t for t in targets if t['target_id'] in with_spectra]
        target_ids = [t['target_id'] for t in targets]
        target_int_ids = [t['id'] for t in targets]
        target_id_map = {t['id']: t['id'] for t in targets}
        target_names = {t['id']: t['target_id'] for t in targets}

    print("Fetching comments...")
    comments = fetch_comments(supabase, target_int_ids)
    print(f"  Found {len(comments)} comments")

    print("Fetching flag audit log...")
    flag_entries = fetch_flag_audit_log(supabase, target_int_ids)
    print(f"  Found {len(flag_entries)} audit entries")

    print("Fetching emission-line fits...")
    line_fits = fetch_line_fits(supabase, [s['id'] for s in spectra])
    print(f"  Found {len(line_fits)} line fits")

    print("Fetching storage registry rows...")
    storage_rows, missing_finals = fetch_storage_rows(supabase, spectra)
    print(f"  Found {len(storage_rows)} rows ({len(missing_finals)} spectra without a production final)")

    print("Fetching fields and deployments...")
    fields = fetch_fields(supabase)
    obs_deployments = fetch_observation_deployments(supabase, obs_names)
    print(f"  {len(fields)} fields; {len(obs_deployments)}/{len(obs_names)} observations have a published deployment")

    print("Fetching NIRCam mosaics...")
    nircam = select_nircam(supabase, fields, {t['field'] for t in targets}, args.nircam_field)
    if nircam:
        file_paths = [img['file_path'] for img in nircam['images'] + ([nircam['draft_image']] if nircam['draft_image'] else [])]
        nircam['storage_rows'] = _in_batches(supabase, 'storage_objects', 'storage_key', file_paths)
        print(f"  Field {nircam['field']!r}: {len(nircam['images'])} published + "
              f"{1 if nircam['draft_image'] else 0} draft mosaics, {len(nircam['storage_rows'])} registry rows")
    else:
        print("  No published NIRCam mosaics in production")

    # Cross-match targets into objects (per-field friends-of-friends)
    print("\nCross-matching targets into objects...")
    cross_matched_objects, target_to_object_db_id = build_seed_objects(
        targets, spectra,
    )
    cross_matched_objects = anchor_objects(cross_matched_objects, target_to_object_db_id)
    objects_by_db_id = {o['_db_id']: o for o in cross_matched_objects}
    n_multi = sum(1 for o in cross_matched_objects if o['n_targets'] > 1)
    print(f"  {len(cross_matched_objects)} objects "
          f"({len(cross_matched_objects) - n_multi} singletons, {n_multi} multi-target)")

    # Mirror Phase D.1 in-process so seed data matches post-migration shape
    backfill_spectra_from_targets(spectra, targets)
    lift_inspection_state_to_objects(cross_matched_objects, targets, spectra)
    n_inspected = sum(1 for o in cross_matched_objects if o.get('redshift_quality', 0) > 0)
    print(f"  {n_inspected} objects carry lifted inspection state")

    # Photometry: production object ids -> seed object ids
    prod_to_seed_object = {}
    for t in targets:
        if t.get('object_id') is not None and t['id'] in target_to_object_db_id:
            prod_to_seed_object[t['object_id']] = target_to_object_db_id[t['id']]
    print("Fetching photometry cross-matches...")
    photometry = fetch_photometry(supabase, sorted(prod_to_seed_object))
    print(f"  Found {len(photometry)} rows")

    # Lifecycle fixtures: one draft + one revoked spectrum with their deployments
    picks = pick_lifecycle_victims(targets, spectra, objects_by_db_id, target_to_object_db_id)
    apply_lifecycle_victims(picks, spectra, targets)
    for status, t, s, o in picks:
        print(f"  Lifecycle fixture: spectrum {s['id']} ({s['spectrum_id']}) -> {status}; "
              f"target {t['id']} / object {o['_db_id']} unpublished")
    deployments, lifecycle_ids, real_dep_ids = build_deployments(obs_deployments, nircam, picks, observations)
    share_scopes = pick_share_link_scopes(targets)

    # Production identities -> synthetic users
    identity = IdentityMap()
    identity.note_rows(targets, 'last_inspected_by')
    identity.note_rows(cross_matched_objects, 'last_inspected_by')
    identity.note_rows(comments, 'user_id')
    identity.note_rows(flag_entries, 'user_id')
    identity.note_rows(deployments, 'deployed_by')
    identity.note_rows(storage_rows, 'uploaded_by')
    if nircam:
        identity.note_rows(nircam['storage_rows'], 'uploaded_by')
    identity.freeze()
    print(f"  {len(identity.mapping)} production user ids mapped onto admin@/user@")

    # Anchor assertions (what python/tests/sql/b1_*.sql / b2_*.sql assume)
    for oid in (1, 2, 3):
        obj = objects_by_db_id[oid]
        assert ANCHOR_PROGRAM in obj['programs'] and obj['n_spectra'] > 0 and obj.get('_has_published', True), \
            f"object {oid} is not a published {ANCHOR_PROGRAM} object with spectra"

    # Generate SQL
    print(f"\nGenerating {output_path}...")

    sql_parts = []

    n_nircam = (len(nircam['images']) + (1 if nircam['draft_image'] else 0)) if nircam else 0
    # Header
    sql_parts.append(f"""-- ============================================
-- CAMPFIRE Seed Data (sample)
-- Generated: {datetime.now().isoformat()}
-- Programs: {len(programs)} (public only) | Observations: {len(observations)} | Fields: {len(fields)}
-- Targets: {len(targets)} | Objects: {len(cross_matched_objects)} | Spectra: {len(spectra)} (1 draft, 1 revoked)
-- Storage objects: {len(storage_rows) + len(missing_finals) + (len(nircam['storage_rows']) if nircam else 0)} | Line fits: {len(line_fits)} | Photometry: {len(photometry)}
-- Deployments: {len(deployments)} | NIRCam images: {n_nircam} | Share links: {len(SHARE_LINKS)}
-- Comments: {len(comments)} | Audit Entries: {len(flag_entries)}
--
-- No production identities: inspectors / commenters / deployers are mapped
-- onto the test users, comment text is synthetic. Verified by
-- scripts/check_seed_privacy.py.
--
-- Test Users:
--   admin@campfire.dev / password123 (admin, all programs)
--   user@campfire.dev  / password123 (regular, public programs)
--   viewer@campfire.dev / password123 (read-only, public programs)
-- Share links (/s/<token>, password {LINK_PASSWORD}):
--   {SHARE_LINKS[0]['token']} (live, {share_scopes[0]})
--   {SHARE_LINKS[1]['token']} (revoked, {share_scopes[1]})
-- ============================================

-- Migration sets search_path to empty; restore it for seed
SET search_path TO public, auth, extensions;

""")

    sql_parts.append(generate_auth_users_sql())
    sql_parts.append(generate_programs_sql(programs, guard))
    sql_parts.append(generate_observations_sql(observations, guard))
    sql_parts.append(generate_fields_sql(fields, guard))
    sql_parts.append(generate_flag_definitions_sql())
    sql_parts.append(generate_user_profiles_sql())
    sql_parts.append(generate_objects_table_sql(cross_matched_objects, identity))
    sql_parts.append(generate_objects_sql(targets, target_to_object_db_id, identity))
    sql_parts.append(generate_spectra_sql(spectra))
    sql_parts.append(generate_deployments_sql(deployments, observations, obs_deployments, fields,
                                              real_dep_ids, identity, guard))
    sql_parts.append(generate_nircam_images_sql(nircam))
    sql_parts.append(generate_storage_objects_sql(storage_rows, missing_finals, picks, lifecycle_ids,
                                                  nircam, obs_set, real_dep_ids, identity))
    sql_parts.append(generate_line_fits_sql(line_fits, guard))
    sql_parts.append(generate_photometry_sql(photometry, prod_to_seed_object))
    sql_parts.append(generate_user_program_access_sql(programs, guard))
    sql_parts.append(generate_share_links_sql(share_scopes, observations, guard))
    sql_parts.append(generate_comments_sql(comments, target_id_map, target_names, identity))
    sql_parts.append(generate_flag_audit_log_sql(flag_entries, target_id_map, identity))
    sql_parts.append(generate_object_list_members_sql(
        targets, target_to_object_db_id, objects_by_db_id,
    ))
    sql_parts.append(generate_user_lists_sql(
        targets, target_to_object_db_id, objects_by_db_id,
    ))
    sql_parts.append(generate_access_codes_sql(guard))
    sql_parts.append(generate_dr_fixture_sql())
    sql_parts.append(generate_sequence_resets(
        targets, spectra, comments, flag_entries, cross_matched_objects,
        deployments, nircam, photometry,
    ))

    # Write output
    full_sql = '\n'.join(sql_parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(full_sql)

    print(f"\nSeed file written to: {output_path}")
    print(f"  Size: {len(full_sql.encode()):,} bytes")
    print(f"\nNext: python scripts/check_seed_privacy.py && supabase db reset")


if __name__ == '__main__':
    main()
