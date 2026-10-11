#!/usr/bin/env python3
"""
Fail if supabase/seed.sql would leak production identities or non-public data.

The seed is committed to a public repository and applied to every preview
branch, so it must carry only the synthetic accounts and public programs.
This scans the generated file (no database needed) and exits non-zero when:

* any uuid other than the synthetic account ids (and the all-zero GoTrue
  instance id) appears anywhere — a real `auth.users` id in
  `last_inspected_by`, `user_id`, `deployed_by`, `uploaded_by`, ...;
* any email address outside `@campfire.dev` / `@shared.invalid` appears;
* an IPv4 address appears;
* a `programs` row is inserted with `is_public` other than TRUE, or any
  inserted row names a `program_slug` (or an element of a `programs` /
  `program_slugs` text[] — `objects.programs`, `access_codes.program_slugs`,
  `pending_invites.program_slugs`) that is not one of the inserted public
  programs.

Usage:
    python scripts/check_seed_privacy.py [path/to/seed.sql]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SYNTHETIC_UUIDS = {
    '11111111-1111-1111-1111-111111111111',  # admin@campfire.dev
    '22222222-2222-2222-2222-222222222222',  # user@campfire.dev
    '33333333-3333-3333-3333-333333333333',  # viewer@campfire.dev
    '44444444-4444-4444-4444-444444444444',  # live share-link account
    '55555555-5555-5555-5555-555555555555',  # revoked share-link account
    '00000000-0000-0000-0000-000000000000',  # GoTrue instance_id
}
ALLOWED_EMAIL_DOMAINS = ('campfire.dev', 'shared.invalid')

UUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
IPV4_RE = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
INSERT_RE = re.compile(r'^\s*INSERT\s+INTO\s+(?:public\.)?([a-z_]+)\s*\(([^)]*)\)\s*VALUES\s*\(', re.I | re.S)


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside quoted strings, dollar quotes and comments."""
    statements, buf = [], []
    i, n = 0, len(sql)
    in_quote = False
    while i < n:
        c = sql[i]
        if in_quote:
            buf.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_quote = False
        elif c == '-' and sql.startswith('--', i):
            j = sql.find('\n', i)
            i = n if j < 0 else j
            continue
        elif c == "'":
            in_quote = True
            buf.append(c)
        elif c == ';':
            statements.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def split_values(text: str) -> tuple[list[str], int]:
    """Split the body of a VALUES tuple at top-level commas (quotes and brackets aware).

    Returns (parts, index just past the tuple's closing parenthesis)."""
    parts, buf = [], []
    depth = 0
    in_quote = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_quote:
            buf.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_quote = False
        elif c == "'":
            in_quote = True
            buf.append(c)
        elif c in '([':
            depth += 1
            buf.append(c)
        elif c in ')]':
            if depth == 0:
                i += 1
                break  # end of the tuple
            depth -= 1
            buf.append(c)
        elif c == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append(''.join(buf).strip())
    return parts, i


def sql_string(token: str) -> str | None:
    """The Python value of a quoted SQL literal, else None."""
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    if token.endswith("'::uuid") and token.startswith("'"):
        return token[1:-len("'::uuid")]
    return None


# Columns whose text[] elements are program slugs (every table that has one).
PROGRAM_ARRAY_COLUMNS = ('programs', 'program_slugs')

ARRAY_LITERAL_RE = re.compile(r"^'\{(.*)\}'(?:::text\[\])?$", re.S)
ARRAY_ELEMENT_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([^,]+)')


def sql_text_array(token: str) -> list[str]:
    """Elements of an ARRAY['a', 'b'] or '{a,"b"}' text[] literal ('{}' is empty)."""
    token = token.strip()
    if token.upper().startswith('ARRAY[') and token.endswith(']'):
        parts, _ = split_values(token[len('ARRAY['):-1])
        return [v for v in (sql_string(t) for t in parts) if v is not None]
    m = ARRAY_LITERAL_RE.match(token)
    if m:
        body = m.group(1).replace("''", "'").strip()
        if not body:
            return []
        return [(q if q else bare).strip().replace('\\"', '"').replace('\\\\', '\\')
                for q, bare in ARRAY_ELEMENT_RE.findall(body)]
    return []


def parse_inserts(sql: str):
    """Yield (table, {column: raw value token}) for every row of every INSERT ... VALUES
    (single- and multi-row); INSERT ... SELECT statements are skipped."""
    for stmt in split_statements(sql):
        m = INSERT_RE.match(stmt)
        if not m:
            continue
        table = m.group(1).lower()
        columns = [c.strip().strip('"') for c in m.group(2).split(',')]
        rest = stmt[m.end():]
        while True:
            values, end = split_values(rest)
            if len(values) != len(columns):
                raise SystemExit(f"cannot parse INSERT INTO {table}: {len(columns)} columns, "
                                 f"{len(values)} values:\n{stmt[:200]}")
            yield table, dict(zip(columns, values))
            rest = rest[end:].lstrip()
            if not rest.startswith(','):
                break
            rest = rest[1:].lstrip()
            if not rest.startswith('('):
                raise SystemExit(f"cannot parse multi-row INSERT INTO {table} near: {rest[:80]!r}")
            rest = rest[1:]


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / 'supabase' / 'seed.sql'
    sql = path.read_text(encoding='utf-8')
    problems: list[str] = []

    # 1. uuids
    foreign = sorted({u.lower() for u in UUID_RE.findall(sql)} - SYNTHETIC_UUIDS)
    if foreign:
        problems.append(f"{len(foreign)} non-synthetic uuid(s): {foreign[:5]}{' ...' if len(foreign) > 5 else ''}")

    # 2. emails
    bad_emails = sorted({e for e in EMAIL_RE.findall(sql)
                         if not e.lower().endswith(tuple('@' + d for d in ALLOWED_EMAIL_DOMAINS))})
    if bad_emails:
        problems.append(f"{len(bad_emails)} email(s) outside {ALLOWED_EMAIL_DOMAINS}: {bad_emails[:5]}")

    # 3. IPv4 addresses
    ips = sorted(set(IPV4_RE.findall(sql)))
    if ips:
        problems.append(f"IPv4 address(es) present: {ips[:5]}")

    # 4. programs: only public, and every reference stays inside that set
    public: set[str] = set()
    inserts = list(parse_inserts(sql))
    for table, row in inserts:
        if table != 'programs':
            continue
        slug = sql_string(row.get('slug', ''))
        if row.get('is_public', '').strip().upper() != 'TRUE':
            problems.append(f"program {slug!r} inserted with is_public = {row.get('is_public')!r}")
        elif slug:
            public.add(slug)
    if not public:
        problems.append("no public programs inserted (parser found no programs rows)")

    referenced: dict[str, set[str]] = {}
    for table, row in inserts:
        if table == 'programs':
            continue
        if 'program_slug' in row:
            slug = sql_string(row['program_slug'])
            if slug is not None and slug not in public:
                referenced.setdefault(table, set()).add(slug)
        for column in PROGRAM_ARRAY_COLUMNS:
            if column in row:
                for slug in sql_text_array(row[column]):
                    if slug not in public:
                        referenced.setdefault(table, set()).add(slug)
    for table, slugs in sorted(referenced.items()):
        problems.append(f"{table}: references non-public program(s) {sorted(slugs)}")

    n_rows = sum(1 for _ in inserts)
    if problems:
        print(f"FAIL {path} ({n_rows} inserts scanned):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK {path}: {n_rows} inserts, {len(public)} public programs, "
          f"only synthetic uuids/emails, no IPv4 literals")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
