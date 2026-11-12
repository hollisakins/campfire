"""Visit grouping and the per-visit chain (steps replaced by recorders)."""

import pytest
from astropy.io import fits

from campfire_pipeline.hst import visit as visit_mod
from campfire_pipeline.hst.visit import Visit, group_visits, process_visit, visit_id


def _file(tmp_path, root, instrume='ACS', detector='WFC', **filters):
    hdu = fits.PrimaryHDU()
    hdu.header.update({'INSTRUME': instrume, 'DETECTOR': detector, 'ROOTNAME': root})
    hdu.header.update(filters or {'FILTER1': 'CLEAR1L', 'FILTER2': 'F814W'})
    path = str(tmp_path / f'{root}_flc.fits')
    hdu.writeto(path)
    return path


def test_visit_id_is_the_first_six_characters():
    assert visit_id('JE2860JUQ') == 'je2860'
    with pytest.raises(ValueError):
        visit_id('je2860')


def test_grouping_by_visit_detector_and_filter(tmp_path):
    a = _file(tmp_path, 'je2860juq')
    b = _file(tmp_path, 'je2860jvq')
    c = _file(tmp_path, 'je2860k0q', FILTER1='F606W', FILTER2='CLEAR2L')
    d = _file(tmp_path, 'je2861aaq')                       # another visit
    e = _file(tmp_path, 'ie2860abq', 'WFC3', 'IR', FILTER='F160W')
    visits = group_visits([e, d, c, b, a])
    assert [v.key for v in visits] == [
        'ie2860-wfc3ir-f160w', 'je2860-acswfc-f606w', 'je2860-acswfc-f814w',
        'je2861-acswfc-f814w']
    assert visits[2].files == (a, b)

    uvis = _file(tmp_path, 'ie2860acq', 'WFC3', 'UVIS', FILTER='F606W')
    with pytest.raises(ValueError, match='Unsupported'):
        group_visits([uvis])


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    class Solution:
        status = 'SOLVED'

    import campfire_pipeline.hst.steps.align as align
    import campfire_pipeline.hst.steps.crreject as crreject
    import campfire_pipeline.hst.steps.sky as sky
    import campfire_pipeline.hst.steps.wcs_init as wcs_init

    monkeypatch.setattr(wcs_init, 'wcs_init_step',
                        lambda f, **kw: calls.append(('wcs_init', f)))
    monkeypatch.setattr(sky, 'sky_step', lambda f, **kw: calls.append(('sky', f)))
    monkeypatch.setattr(crreject, 'crreject_step',
                        lambda files, **kw: calls.append(('crreject', tuple(files))))

    def fake_align(files, refcat, **kw):
        calls.append(('align', tuple(files), kw['key']))
        return Solution()
    monkeypatch.setattr(align, 'align_visit', fake_align)
    return calls, Solution


def test_chain_order_and_result(recorder):
    calls, _ = recorder
    v = Visit('je2860', 'acswfc', 'f606w', ('a.fits', 'b.fits'))
    out = process_visit(v, refcat=object())
    assert [c[0] for c in calls] == ['wcs_init', 'sky', 'wcs_init', 'sky',
                                     'crreject', 'align']
    assert calls[-1] == ('align', ('a.fits', 'b.fits'), 'je2860-acswfc-f606w')
    assert out == {'key': 'je2860-acswfc-f606w', 'cosmic_rays': 'visit',
                   'align': 'SOLVED', 'ready': True}


def test_single_exposures_and_failed_alignment(recorder):
    calls, Solution = recorder
    acs = process_visit(Visit('je2860', 'acswfc', 'f606w', ('a.fits',)), refcat=object())
    assert acs['cosmic_rays'] == 'none' and acs['ready'] is False
    ir = process_visit(Visit('ie2860', 'wfc3ir', 'f160w', ('c.fits',)), refcat=object())
    assert ir['cosmic_rays'] == 'ramp' and ir['ready'] is True
    assert 'crreject' not in [c[0] for c in calls]

    Solution.status = 'NOT_ALIGNED'
    bad = process_visit(Visit('je2860', 'acswfc', 'f606w', ('a.fits', 'b.fits')),
                        refcat=object())
    assert bad['align'] == 'NOT_ALIGNED' and bad['ready'] is False

    # without a reference catalog the visit is processed but not aligned
    calls.clear()
    plain = process_visit(Visit('je2860', 'acswfc', 'f606w', ('a.fits', 'b.fits')))
    assert plain['align'] is None and plain['ready'] is True
    assert 'align' not in [c[0] for c in calls]
    assert visit_mod.Visit('x' * 6, 'acswfc', 'f606w', ()).key == 'xxxxxx-acswfc-f606w'
