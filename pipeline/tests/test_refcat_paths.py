"""`cfpipe nircam refcat` resolves relative paths against the field's astrom_cats/
dir, so a merge run from anywhere reads and writes where align looks."""
import os
from types import SimpleNamespace

import pytest
from astropy.table import Table
from click.testing import CliRunner

from campfire_pipeline.nircam.refcat import cli as rcli
from campfire_pipeline.nircam.refcat.io import read_refcat, write_refcat


def _catalog(path, ra0):
    t = Table({"RA": [ra0, ra0 + 0.01], "DEC": [2.0, 2.01],
               "mag": [20.0, 21.0], "mag_err": [0.01, 0.02]})
    write_refcat(t, path)


@pytest.fixture
def field(tmp_path, monkeypatch):
    refcat_dir = tmp_path / "reference" / "nircam" / "f" / "astrom_cats"
    refcat_dir.mkdir(parents=True)
    obj = SimpleNamespace(name="f", refcat_dir=str(refcat_dir))
    monkeypatch.setattr(rcli, "_refcat_setup", lambda config, name: (None, obj))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return obj


def test_merge_reads_and_writes_in_astrom_cats(field):
    _catalog(os.path.join(field.refcat_dir, "a.ecsv"), 150.0)
    _catalog(os.path.join(field.refcat_dir, "b.ecsv"), 151.0)
    res = CliRunner().invoke(rcli.refcat, ["merge", "--field", "f", "a.ecsv", "b.ecsv",
                                           "--out", "ab.ecsv"])
    assert res.exit_code == 0, (res.output, res.exception)
    out = os.path.join(field.refcat_dir, "ab.ecsv")
    assert os.path.exists(out) and not os.path.exists("ab.ecsv")
    assert len(read_refcat(out)) == 4


def test_input_falls_back_to_cwd_and_absolute_out_is_kept(field, tmp_path):
    _catalog(os.path.join(field.refcat_dir, "a.ecsv"), 150.0)
    _catalog("local.ecsv", 151.0)                       # only in the current dir
    target = tmp_path / "abs.ecsv"
    res = CliRunner().invoke(rcli.refcat, ["merge", "--field", "f", "a.ecsv", "local.ecsv",
                                           "--out", str(target)])
    assert res.exit_code == 0, (res.output, res.exception)
    assert target.exists()


def test_missing_input_names_where_it_looked(field):
    _catalog(os.path.join(field.refcat_dir, "a.ecsv"), 150.0)
    res = CliRunner().invoke(rcli.refcat, ["merge", "--field", "f", "a.ecsv", "nope.ecsv",
                                           "--out", "x.ecsv"])
    assert res.exit_code != 0 and "nope.ecsv" in res.output and "astrom_cats" in res.output
