"""Unit tests for scripts/check_seed_privacy.py (the committed-seed privacy gate).

The checker is a stand-alone script, not a package module, so it is loaded by
path from the repo root. No database or network is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'check_seed_privacy.py'

spec = importlib.util.spec_from_file_location('check_seed_privacy', SCRIPT)
check_seed_privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_seed_privacy)

ADMIN = '11111111-1111-1111-1111-111111111111'

PUBLIC_PROGRAM = """
INSERT INTO public.programs (slug, name, is_public)
VALUES ('public-program', 'A public program', TRUE);
"""


def run(tmp_path: Path, capsys, body: str) -> tuple[int, str]:
    seed = tmp_path / 'seed.sql'
    seed.write_text(PUBLIC_PROGRAM + body, encoding='utf-8')
    rc = check_seed_privacy.main(['check_seed_privacy.py', str(seed)])
    return rc, capsys.readouterr().out


def test_public_slugs_pass(tmp_path, capsys):
    rc, out = run(tmp_path, capsys, f"""
INSERT INTO public.access_codes (code, grants_all_programs, program_slugs, is_active, created_by)
VALUES ('CODE', FALSE, ARRAY['public-program'], TRUE, '{ADMIN}');
INSERT INTO public.pending_invites (email, program_slugs, invited_by)
VALUES ('someone@campfire.dev', '{{public-program}}', '{ADMIN}');
INSERT INTO public.objects (id, object_id, programs)
VALUES (1, 'OBJ-1', ARRAY['public-program']);
""")
    assert rc == 0, out
    assert out.startswith('OK ')


@pytest.mark.parametrize('table, columns, values', [
    ('access_codes', 'code, grants_all_programs, program_slugs, is_active, created_by',
     f"'CODE', FALSE, ARRAY['private-program'], TRUE, '{ADMIN}'"),
    ('access_codes', 'code, grants_all_programs, program_slugs, is_active, created_by',
     f"'CODE', FALSE, ARRAY['public-program', 'private-program'], TRUE, '{ADMIN}'"),
    ('pending_invites', 'email, program_slugs, invited_by',
     f"'someone@campfire.dev', ARRAY['private-program'], '{ADMIN}'"),
    ('pending_invites', 'email, program_slugs, invited_by',
     f"'someone@campfire.dev', '{{private-program}}', '{ADMIN}'"),
    ('pending_invites', 'email, program_slugs, invited_by',
     f"'someone@campfire.dev', '{{\"public-program\",\"private-program\"}}'::text[], '{ADMIN}'"),
])
def test_private_slug_in_program_slugs_fails(tmp_path, capsys, table, columns, values):
    rc, out = run(tmp_path, capsys, f"INSERT INTO public.{table} ({columns})\nVALUES ({values});\n")
    assert rc == 1, out
    assert out.startswith('FAIL ')
    assert f"{table}: references non-public program(s) ['private-program']" in out


def test_private_slug_in_programs_array_and_scalar_fail(tmp_path, capsys):
    rc, out = run(tmp_path, capsys, """
INSERT INTO public.objects (id, object_id, programs)
VALUES (1, 'OBJ-1', ARRAY['private-program']);
INSERT INTO public.targets (id, target_id, program_slug)
VALUES (1, 'T-1', 'private-program');
""")
    assert rc == 1, out
    assert "objects: references non-public program(s) ['private-program']" in out
    assert "targets: references non-public program(s) ['private-program']" in out


def test_non_public_program_row_fails(tmp_path, capsys):
    rc, out = run(tmp_path, capsys, """
INSERT INTO public.programs (slug, name, is_public)
VALUES ('private-program', 'A private program', FALSE);
""")
    assert rc == 1, out
    assert "program 'private-program' inserted with is_public = 'FALSE'" in out


def test_foreign_uuid_and_email_fail(tmp_path, capsys):
    rc, out = run(tmp_path, capsys, """
INSERT INTO public.comments (id, user_id, content)
VALUES (1, 'a1b2c3d4-0000-4000-8000-000000000001', 'mail me at real.person@example.org');
""")
    assert rc == 1, out
    assert 'non-synthetic uuid' in out
    assert 'real.person@example.org' in out


@pytest.mark.parametrize('token, expected', [
    ("ARRAY['a', 'b']", ['a', 'b']),
    ("'{}'", []),
    ("'{a,b}'", ['a', 'b']),
    ("'{\"a b\",c}'::text[]", ['a b', 'c']),
    ("'plain'", []),
    ("NULL", []),
])
def test_sql_text_array(token, expected):
    assert check_seed_privacy.sql_text_array(token) == expected
