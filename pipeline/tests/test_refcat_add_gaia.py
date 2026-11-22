"""`cfpipe nircam refcat add-gaia`: an existing catalog takes Gaia's stars.

A small made-up catalog: galaxies, stars that Gaia lists (displaced, as a
catalog from another year would have them), a faint star Gaia does not list,
and a Gaia star without a proper motion. The Gaia table is read from disk, so
nothing is queried.
"""

import numpy as np
import pytest
from astropy.table import Table
from click.testing import CliRunner

from campfire_pipeline.nircam.refcat.cli import refcat
from campfire_pipeline.nircam.refcat.io import read_refcat

FIELDS = """
[hstonly]
tangent_point = [150.0, 2.0]
[hstonly.T]
rotation = 0
[hstonly.T.30mas]
crpix = [50.5, 50.5]
naxis = [100, 100]
"""

N_STARS = 12
MAS = 1.0 / 3.6e6


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'fields.toml').write_text(FIELDS)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    return tmp_path


def _catalogs(tmp_path, size_column='radius_sersic'):
    rng = np.random.default_rng(2)
    # Gaia: twelve stars with a proper motion and one without
    g_ra = 150.0 + rng.uniform(-0.02, 0.02, N_STARS + 1)
    g_dec = 2.0 + rng.uniform(-0.02, 0.02, N_STARS + 1)
    pmra = np.append(rng.uniform(-8, 8, N_STARS), np.nan)
    pmdec = np.append(rng.uniform(-8, 8, N_STARS), np.nan)
    gaia = Table({'source_id': np.arange(1, N_STARS + 2), 'RA': g_ra, 'DEC': g_dec,
                  'ref_epoch': np.full(N_STARS + 1, 2016.0), 'pmra': pmra,
                  'pmdec': pmdec, 'mag': np.full(N_STARS + 1, 18.0, 'float32'),
                  'mag_err': np.full(N_STARS + 1, 0.01, 'float32')})
    gaia_path = tmp_path / 'gaia.ecsv'
    gaia.write(gaia_path, format='ascii.ecsv')

    # the catalog: the same stars 30 mas away, one faint star Gaia lacks, and
    # forty galaxies; sizes in arbitrary units (stars 1.0, galaxies 5 and up)
    star_ra = g_ra + 30 * MAS
    faint = (150.01, 2.01)
    gal_ra = 150.0 + rng.uniform(-0.02, 0.02, 40)
    gal_dec = 2.0 + rng.uniform(-0.02, 0.02, 40)
    cat = Table({
        'ra': np.concatenate([star_ra, [faint[0]], gal_ra]),
        'dec': np.concatenate([g_dec, [faint[1]], gal_dec]),
        'mag': np.full(N_STARS + 2 + 40, 22.0, 'float32'),
        'mag_err': np.full(N_STARS + 2 + 40, 0.05, 'float32'),
        size_column: np.concatenate([rng.normal(1.0, 0.02, N_STARS + 2),
                                     rng.uniform(5, 20, 40)]),
    })
    return cat, gaia, str(gaia_path), (gal_ra, gal_dec)


def test_stars_take_gaia_and_unvouched_point_sources_go(root, tmp_path):
    cat, gaia, gaia_path, (gal_ra, gal_dec) = _catalogs(tmp_path)
    src = tmp_path / 'survey_cut.ecsv'
    cat.write(src, format='ascii.ecsv')
    result = CliRunner().invoke(refcat, [
        'add-gaia', '--field', 'hstonly', str(src),
        '--size-column', 'radius_sersic', '--gaia-catalog', gaia_path])
    assert result.exit_code == 0, result.output

    # default home: the field's catalog directory, named after the input
    out = root / 'reference' / 'nircam' / 'hstonly' / 'astrom_cats' / 'survey_cut_gaia.ecsv'
    got = read_refcat(str(out))
    assert got.meta['source'] == 'add-gaia' and got.meta['field'] == 'hstonly'
    info = got.meta['params']
    assert info['n_gaia_stars'] == N_STARS
    assert info['n_dropped_no_motion'] == 1 and info['n_dropped_compact'] == 1
    assert len(got) == N_STARS + 40

    stars = got[np.isfinite(np.asarray(got['pmra'], float))]
    assert len(stars) == N_STARS
    # Gaia's own position, epoch and motion, not the catalog's
    order = np.argsort(np.asarray(stars['source_id']))
    assert np.allclose(np.asarray(stars['RA'])[order], gaia['RA'][:N_STARS], atol=1e-10)
    assert np.all(np.asarray(stars['ref_epoch']) == 2016.0)
    assert np.allclose(np.asarray(stars['pmra'])[order], gaia['pmra'][:N_STARS])
    # the galaxies are where they were
    rest = got[~np.isfinite(np.asarray(got['pmra'], float))]
    assert sorted(np.asarray(rest['RA'])) == pytest.approx(sorted(gal_ra))


def test_without_a_size_column_nothing_is_dropped_on_size(root, tmp_path):
    cat, _gaia, gaia_path, _ = _catalogs(tmp_path)
    cat.remove_column('radius_sersic')
    src = tmp_path / 'plain.fits'                  # any table astropy reads
    cat.write(src)
    out = tmp_path / 'out.ecsv'
    result = CliRunner().invoke(refcat, [
        'add-gaia', '--field', 'hstonly', str(src), '--gaia-catalog', gaia_path,
        '--out', str(out)])
    assert result.exit_code == 0, result.output
    assert "no 'rhalf' column" in result.output
    got = read_refcat(str(out))
    assert len(got) == N_STARS + 1 + 40             # only the motionless star went


def test_gaia_query_stays_local_for_a_catalog_across_ra_zero(root, tmp_path, monkeypatch):
    """Sources on both sides of RA 0: the Gaia cone is centered among them and
    is as small as the catalog, not half the sky."""
    from campfire_pipeline.nircam.refcat import cli

    cat, gaia, _path, _ = _catalogs(tmp_path)
    cat['ra'] = (np.asarray(cat['ra']) - 150.0) % 360.0     # RA 359.98 .. 0.02
    src = tmp_path / 'wrap.ecsv'
    cat.write(src, format='ascii.ecsv')
    asked = {}

    def fake_query(backend, center, radius):
        asked.update(backend=backend, center=center, radius=radius)
        out = gaia.copy()
        out['RA'] = (np.asarray(out['RA']) - 150.0) % 360.0
        return out

    monkeypatch.setattr(cli, 'query_external', fake_query)
    result = CliRunner().invoke(refcat, [
        'add-gaia', '--field', 'hstonly', str(src), '--size-column', 'radius_sersic',
        '--out', str(tmp_path / 'out.ecsv')])
    assert result.exit_code == 0, result.output
    assert asked['backend'] == 'gaia'
    assert min(asked['center'][0], 360.0 - asked['center'][0]) < 0.01
    assert asked['radius'] < 0.05
    assert read_refcat(str(tmp_path / 'out.ecsv')).meta['params']['n_gaia_stars'] == N_STARS


def test_a_file_that_is_not_a_catalog_is_refused(root, tmp_path):
    src = tmp_path / 'odd.ecsv'
    Table({'x': [1.0], 'y': [2.0]}).write(src, format='ascii.ecsv')
    result = CliRunner().invoke(refcat, ['add-gaia', '--field', 'hstonly', str(src)])
    assert result.exit_code != 0
    assert 'not in the reference-catalog layout' in result.output
