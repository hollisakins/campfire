"""Tests for the emission-line fitter (nirspec/linefit.py, linefit_stage.py,
redshift_reference.py, linelist.py).

The numerics are exercised on synthetic spectra with known line fluxes:
recovered fluxes must be unbiased at the quoted errors, doublet ties and
blends must behave as documented, kinematics must fall back correctly, and
the stage runner must gate on the inspected-redshift file and refit only
when its inputs change. No pipeline data files are needed (a synthetic
R-curve stands in for the CRDS dispersion tables).
"""

import math
import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.nirspec import linefit as lf
from campfire_pipeline.nirspec.linefit import (
    C_KMS, FLAG_BLEND, FLAG_BLENDED, FLAG_BROAD, FLAG_KIN_DEFAULT, FLAG_KIN_GLOBAL,
    FLAG_RESOLVED, FLAG_TIED, LineFitConfig, fit_lines, gaussian_pixint, make_r_function,
    pixel_edges,
)
from campfire_pipeline.nirspec.linelist import (
    DOUBLET_OF_MEMBER, DOUBLETS, DOUBLETS_BY_NAME, LINES, LINES_BY_NAME, doublet_wave, get_label,
)
from campfire_pipeline.nirspec.redshift_reference import (
    RedshiftEntry, load_redshifts, serialize_redshifts, write_redshifts,
)


# ---------------------------------------------------------------------------
# Synthetic spectra
# ---------------------------------------------------------------------------

def _r_curve(grating):
    if grating == 'prism':
        return make_r_function([0.6, 1.0, 2.0, 3.0, 5.3], [30, 40, 100, 200, 300])
    if grating == 'g395m':
        return make_r_function([2.87, 5.2], [700, 1300])
    if grating == 'g395h':
        return make_r_function([2.87, 5.2], [1900, 3600])
    raise ValueError(grating)


def _grid(grating):
    if grating == 'prism':
        return np.linspace(0.6, 5.3, 470)
    if grating == 'g395m':
        return np.arange(2.87, 5.2, 0.00179)
    if grating == 'g395h':
        return np.arange(2.87, 5.2, 0.000666)
    raise ValueError(grating)


def synth(z, grating, lines, cont=1e-20, sigma_v=120.0, dv=50.0, noise=2e-21, seed=1, broad=None):
    """Return (wave_um, fnu_uJy, fnu_err_uJy, r_of) for a flat continuum + Gaussian lines."""
    rng = np.random.default_rng(seed)
    wave = _grid(grating)
    r_of = _r_curve(grating)
    lo, hi = pixel_edges(wave)
    flam = np.full_like(wave, cont)
    for name, F in lines.items():
        L = LINES_BY_NAME[name]
        mu = L.wave * (1 + z) * 1e-4 * (1 + dv / C_KMS)
        sig = math.hypot(mu / r_of(mu)[0] * lf.FWHM_TO_SIGMA, sigma_v / C_KMS * mu)
        flam += F * gaussian_pixint(lo, hi, mu, sig)
    for name, (F, sb) in (broad or {}).items():
        L = LINES_BY_NAME[name]
        mu = L.wave * (1 + z) * 1e-4
        sig = math.hypot(mu / r_of(mu)[0] * lf.FWHM_TO_SIGMA, sb / C_KMS * mu)
        flam += F * gaussian_pixint(lo, hi, mu, sig)
    err = np.full_like(wave, noise)
    obs = flam + rng.normal(0, noise, size=wave.size)
    conv = 2.99792458e-19 / wave ** 2
    return wave, obs / conv, err / conv, r_of


TRUTH = {
    'Halpha': 5e-18, 'NII6583': 5e-19, 'NII6548': 5e-19 / 2.94,
    'SII6716': 4e-19, 'SII6731': 3e-19,
    'Hbeta': 1.8e-18, 'OIII5007': 6e-18, 'OIII4959': 6e-18 / 2.98,
    'OIII4363': 2e-19, 'Hgamma': 8e-19,
}


# ---------------------------------------------------------------------------
# Line catalog
# ---------------------------------------------------------------------------

def test_linelist_is_consistent():
    names = [l.name for l in LINES]
    assert len(names) == len(set(names))
    for l in LINES:
        assert l.wave > 0
        if l.tie:
            primary, ratio = l.tie
            assert primary in LINES_BY_NAME and 0 < ratio < 1
    # vacuum wavelengths, not air
    assert abs(LINES_BY_NAME['Halpha'].wave - 6564.61) < 0.05
    assert abs(LINES_BY_NAME['OIII5007'].wave - 5008.24) < 0.05
    # doublet totals: their own namespace, two existing free (untied) members,
    # blue member first, close enough to blend at some resolution
    dnames = [d.name for d in DOUBLETS]
    assert len(dnames) == len(set(dnames)) and not set(dnames) & set(names)
    seen = set()
    for d in DOUBLETS:
        a, b = (LINES_BY_NAME[n] for n in d.members)
        assert a.tie is None and b.tie is None
        assert 0 < b.wave - a.wave < 20
        assert a.wave < d.wave < b.wave
        assert not set(d.members) & seen
        seen |= set(d.members)
        assert DOUBLET_OF_MEMBER[d.members[0]] is d and DOUBLETS_BY_NAME[d.name] is d
    assert abs(doublet_wave(['CIII1907', 'CIII1909']) - 1907.5) < 0.1
    assert get_label('CIII1908') == 'CIII]λλ1907,1909' and get_label('Halpha') == 'Hα'
    with pytest.raises(KeyError):
        get_label('nope')


# ---------------------------------------------------------------------------
# Numerics
# ---------------------------------------------------------------------------

def test_gaussian_pixint_conserves_flux():
    wave = np.linspace(3.0, 3.1, 200)
    lo, hi = pixel_edges(wave)
    prof = gaussian_pixint(lo, hi, 3.05, 0.004)
    total = np.sum(prof * (hi - lo) * 1e4)     # ∫ f_λ dλ  (Å)
    assert abs(total - 1.0) < 1e-6


def test_r_function_extrapolates_redward():
    w = np.linspace(1.0, 3.0, 21)
    r_of = make_r_function(w, 100.0 * w, f_lsf=1.5)
    assert np.isclose(r_of(2.0)[0], 300.0)          # scaled by f_lsf
    assert r_of(4.0)[0] > r_of(3.0)[0]               # linear extrapolation, not flat
    assert np.isclose(r_of(4.0)[0], 600.0)


def test_grating_fluxes_are_unbiased_and_tied():
    z = 5.5
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH)
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    lines = res['lines']
    # Hγ and [OIII]4363 sit blueward of the G395M coverage at z=5.5
    assert 'Hgamma' not in lines and 'OIII4363' not in lines
    pulls = []
    for name, truth in TRUTH.items():
        if name in ('Hgamma', 'OIII4363'):
            continue
        rec = lines[name]
        assert np.isfinite(rec['flux']) and rec['flux_err'] > 0
        pulls.append((rec['flux'] - truth) / rec['flux_err'])
    pulls = np.array(pulls)
    assert np.all(np.abs(pulls) < 4.0), pulls
    assert abs(np.mean(pulls)) < 1.0
    # doublet ties
    assert lines['OIII4959']['flags'] & FLAG_TIED
    assert np.isclose(lines['OIII4959']['flux'] / lines['OIII5007']['flux'], 1 / 2.98)
    assert lines['NII6548']['flags'] & FLAG_TIED
    # bright lines are detected, kinematics anchored and recovered
    assert lines['Halpha']['snr'] > 20
    s = res['summary']
    assert s['kin_source'] == 'anchor' and s['n_anchors'] >= 2
    assert abs(s['dv'] - 50.0) < 15.0
    assert abs(s['sigma_v'] - 120.0) < 25.0
    assert abs(s['z_fit'] - (1 + z) * (1 + 50.0 / C_KMS) + 1) < 3e-4
    # equivalent width: flux / continuum / (1+z)
    ha = lines['Halpha']
    assert abs(ha['ew_rest'] - 5e-18 / 1e-20 / (1 + z)) / ha['ew_rest'] < 0.1
    # faint, undetected lines get global kinematics and honest S/N
    weak = [r for n, r in lines.items() if n not in TRUTH and r['component'] == 'narrow'
            and np.isfinite(r['flux'])]
    assert weak and all(r['flags'] & FLAG_KIN_GLOBAL for r in weak)
    assert all(abs(r['snr']) < 4 for r in weak)


def test_kinematics_default_when_nothing_detected():
    z = 5.5
    wave, fnu, err, r_of = synth(z, 'g395m', {'Halpha': 1e-19}, noise=2e-21)
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    s = res['summary']
    assert s['kin_source'] == 'default' and s['n_anchors'] == 0
    assert s['dv'] == 0.0 and s['sigma_v'] == 100.0
    assert all(r['flags'] & FLAG_KIN_DEFAULT for r in res['lines'].values())


def test_broad_component_detected_on_grating_only():
    z = 5.5
    cfg = LineFitConfig()
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH, broad={'Halpha': (8e-18, 1800.0)}, seed=3)
    res = fit_lines(wave, fnu, err, z, r_of, cfg, grating='g395m')
    assert 'Halpha_broad' in res['lines']
    b = res['lines']['Halpha_broad']
    assert b['component'] == 'broad' and b['flags'] & FLAG_BROAD
    assert abs(b['flux'] - 8e-18) / b['flux_err'] < 4
    assert 1200 < b['sigma_v'] < 2500
    assert res['lines']['Halpha']['flags'] & FLAG_BROAD
    assert abs(res['lines']['Halpha']['flux'] - 5e-18) / res['lines']['Halpha']['flux_err'] < 4
    # same object without a broad line: nothing spurious
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH, seed=4)
    res = fit_lines(wave, fnu, err, z, r_of, cfg, grating='g395m')
    assert not any(r['component'] == 'broad' for r in res['lines'].values())
    # prism: never resolvable, never tried
    wave, fnu, err, r_of = synth(z, 'prism', TRUTH, broad={'Halpha': (8e-18, 1800.0)}, noise=5e-21)
    res = fit_lines(wave, fnu, err, z, r_of, cfg, grating='prism')
    assert not any(r['component'] == 'broad' for r in res['lines'].values())


def test_broad_component_survives_pass_two_on_faint_narrow_line():
    """A broad Hα accepted in pass 1 must not vanish when its narrow line is too
    faint to anchor the kinematics (the complex is refit with pinned narrow
    kinematics but a free broad component)."""
    z = 5.5
    narrow = {'Hbeta': 1.8e-18, 'OIII5007': 6e-18, 'OIII4959': 6e-18 / 2.98, 'Halpha': 6e-20}
    wave, fnu, err, r_of = synth(z, 'g395m', narrow, broad={'Halpha': (8e-18, 1800.0)}, seed=5)
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    ha = res['lines']['Halpha']
    assert ha['flags'] & FLAG_KIN_GLOBAL and ha['flags'] & FLAG_BROAD
    assert 'Halpha_broad' in res['lines']
    b = res['lines']['Halpha_broad']
    assert abs(b['flux'] - 8e-18) / b['flux_err'] < 4 and 1200 < b['sigma_v'] < 2500
    cx = next(c for c in res['complexes'] if 'Halpha' in c['lines'])
    assert cx['broad'] and not cx['anchor']


def test_prism_blends_and_coverage():
    z = 5.5
    wave, fnu, err, r_of = synth(z, 'prism', TRUTH, noise=5e-21)
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='prism')
    lines = res['lines']
    # Hα + [NII] are one blend at prism resolution: Hα carries the total
    ha = lines['Halpha']
    assert ha['flags'] & FLAG_BLEND and 'NII6583' in ha['blend_members']
    assert lines['NII6583']['flags'] & FLAG_BLENDED and np.isnan(lines['NII6583']['flux'])
    total = TRUTH['Halpha'] + TRUTH['NII6583'] + TRUTH['NII6548']
    assert abs(ha['flux'] - total) / ha['flux_err'] < 4
    # [SII] doublet likewise
    assert lines['SII6716']['flags'] & FLAG_BLEND and lines['SII6731']['flags'] & FLAG_BLENDED
    # [OIII] stays a tied doublet
    assert lines['OIII4959']['flags'] & FLAG_TIED
    assert abs(lines['OIII5007']['flux'] - 6e-18) / lines['OIII5007']['flux_err'] < 4
    # lines redward of the prism coverage are absent, not NaN rows
    assert 'Paalpha' not in lines and 'Pabeta' not in lines
    # windows never overlap
    spans = sorted((c['lo_idx'], c['hi_idx']) for c in res['complexes'])
    assert all(a[1] <= b[0] + 2 * LineFitConfig().cont_pixels for a, b in zip(spans, spans[1:]))


def test_doublet_totals_agree_across_resolutions():
    """A doublet total (component='doublet') means the same thing whether the
    grating resolves the pair or not: the covariance-propagated sum where it
    does (flag RESOLVED), the single blended measurement where it does not."""
    z = 5.5
    truth_total = TRUTH['SII6716'] + TRUTH['SII6731']
    for grating, noise in (('g395h', 2e-21), ('g395m', 2e-21), ('prism', 5e-21)):
        wave, fnu, err, r_of = synth(z, grating, TRUTH, noise=noise)
        res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating=grating)
        L = res['lines']
        d = L['SII6725']
        a, b = L['SII6716'], L['SII6731']
        assert d['component'] == 'doublet' and d['members'] == ['SII6716', 'SII6731']
        assert d['label'] == '[SII]λλ6716,6731' and d['tied_to'] is None
        assert np.isfinite(d['flux']) and d['flux_err'] > 0
        assert abs(d['flux'] - truth_total) / d['flux_err'] < 4, grating
        assert a['wave_rest'] < d['wave_rest'] < b['wave_rest']
        if grating == 'prism':
            # unresolved: the total *is* the blend primary's measurement
            assert not d['flags'] & FLAG_RESOLVED and not d['flags'] & FLAG_BLEND
            assert a['flags'] & FLAG_BLEND and b['flags'] & FLAG_BLENDED
            assert d['flux'] == a['flux'] and d['flux_err'] == a['flux_err']
        else:
            # resolved: sum of the components with their covariance, not just quadrature
            assert d['flags'] & FLAG_RESOLVED
            quad = math.hypot(a['flux_err'], b['flux_err'])
            assert math.isclose(d['flux'], a['flux'] + b['flux'], rel_tol=1e-9)
            assert not math.isclose(d['flux_err'], quad, rel_tol=1e-6)
            assert 0.5 * quad < d['flux_err'] < 2 * quad
        # totals never count as lines; the summary tracks them separately
        s = res['summary']
        assert s['n_doublets'] >= 1
        narrow = [r for r in L.values() if r['component'] == 'narrow' and np.isfinite(r['flux'])]
        assert s['n_lines'] == len(narrow)
    # a member folded into a line *outside* the doublet leaves the total
    # unmeasurable rather than contaminated: NV at prism resolution sits under Lyα
    wave, fnu, err, r_of = synth(z, 'prism', TRUTH, noise=5e-21)
    L = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='prism')['lines']
    nv = L['NV1240']
    assert nv['flags'] & FLAG_BLENDED and nv['blend_into'] == 'Lya' and np.isnan(nv['flux'])
    assert L['NV1239']['blend_into'] == 'Lya'


def test_doublet_total_is_narrow_and_inherits_broad_flag():
    """A doublet total is the narrow total; an accepted broad component on a
    member stays in <member>_broad and the total carries the BROAD flag."""
    z = 10.0     # MgII lands in G395M
    truth = {'MgII2796': 2e-18, 'MgII2803': 1.5e-18, 'OII3726': 3e-18, 'OII3729': 3e-18,
             'Hgamma': 1.5e-18, 'NeIII3869': 8e-19, 'Hdelta': 8e-19}
    wave, fnu, err, r_of = synth(z, 'g395m', truth, broad={'MgII2796': (1.2e-17, 2000.0)}, seed=3)
    L = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')['lines']
    a, b, d = L['MgII2796'], L['MgII2803'], L['MgII2800']
    assert a['flags'] & FLAG_BROAD and 'MgII2796_broad' in L
    assert d['flags'] & FLAG_BROAD and d['flags'] & FLAG_RESOLVED
    assert math.isclose(d['flux'], a['flux'] + b['flux'], rel_tol=1e-9)
    assert abs(d['flux'] - 3.5e-18) / d['flux_err'] < 4
    bw = L['MgII2796_broad']
    assert abs(bw['flux'] - 1.2e-17) / bw['flux_err'] < 4 and bw['flux'] > 2 * d['flux']
    assert d['flux'] < 1.2e-17 / 2                                # wings are not in the total
    assert not L['OII3727']['flags'] & FLAG_BROAD                 # only where a member has one


def test_blend_primary_sits_at_weighted_centroid():
    z = 5.5
    wave, fnu, err, r_of = synth(z, 'prism', TRUTH, noise=5e-21)
    ha = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='prism')['lines']['Halpha']
    assert set(ha['blend_members']) == {'NII6583', 'NII6548'}
    centroid = doublet_wave(['Halpha', 'NII6583', 'NII6548'])
    assert 6565.5 < centroid < 6566.5
    rest = ha['wave_obs'] / (1 + ha['dv'] / C_KMS) / (1 + z) * 1e4
    assert abs(rest - centroid) < 0.05 and ha['wave_rest'] == LINES_BY_NAME['Halpha'].wave
    # unblended on a grating: the model centre is the catalog wavelength
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH)
    ha = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')['lines']['Halpha']
    rest = ha['wave_obs'] / (1 + ha['dv'] / C_KMS) / (1 + z) * 1e4
    assert abs(rest - LINES_BY_NAME['Halpha'].wave) < 0.05


def test_masked_and_empty_spectra():
    z = 5.5
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH)
    fnu[:] = np.nan
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    assert res['lines'] == {} and res['summary']['n_lines'] == 0
    # a line under a masked region is not covered
    wave, fnu, err, r_of = synth(z, 'g395m', TRUTH)
    mu = LINES_BY_NAME['Halpha'].wave * (1 + z) * 1e-4
    err[np.abs(wave - mu) < 0.02] = 0.0
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    assert 'Halpha' not in res['lines'] and 'OIII5007' in res['lines']


def test_config_from_options_ignores_unknown_keys():
    cfg = LineFitConfig.from_options({'dv_max': 500, 'min_quality': 3, 'plot': True, 'unknown': 1})
    assert cfg.dv_max == 500 and cfg.dv_bound('PRISM') == cfg.dv_max_prism


# ---------------------------------------------------------------------------
# Redshift reference file
# ---------------------------------------------------------------------------

def test_redshift_reference_roundtrip(tmp_path):
    entries = [
        RedshiftEntry('ember_uds_p4_1234', 5.1234, 4, 'CAMPFIRE-J1', 7, '2026-08-30T21:14:03+00:00'),
        RedshiftEntry('ember_uds_p4_99', None, 1, 'CAMPFIRE-J2', 2, None),
        RedshiftEntry('ember_uds_p4_5', 2.0, 2),
    ]
    path = tmp_path / 'redshifts.toml'
    write_redshifts(entries, str(path), 'ember_uds_p4', generated_at='2026-09-09T00:00:00Z')
    text = path.read_text()
    assert '[targets.ember_uds_p4_1234]' in text and 'redshift = 5.123400' in text
    assert 'redshift' not in text.split('[targets.ember_uds_p4_99]')[1].split('[targets')[0]
    back = load_redshifts(str(path))
    assert back['ember_uds_p4_1234'] == entries[0]
    assert back['ember_uds_p4_99'].redshift is None and back['ember_uds_p4_99'].quality == 1
    assert back['ember_uds_p4_5'].usable and not back['ember_uds_p4_99'].usable
    assert load_redshifts(str(tmp_path / 'missing.toml')) == {}
    # deterministic output
    assert serialize_redshifts(entries, 'x', generated_at='t') == serialize_redshifts(entries[::-1], 'x', generated_at='t')


def test_resolve_redshift_gating(tmp_path):
    from campfire_pipeline.nirspec.linefit_stage import resolve_redshift
    reds = {
        't_secure': RedshiftEntry('t_secure', 5.0, 4),
        't_tentative': RedshiftEntry('t_tentative', 4.0, 2),
        't_impossible': RedshiftEntry('t_impossible', None, 1),
    }
    assert resolve_redshift('t_secure', reds, 3, False)[:2] == (5.0, 'inspected')
    assert resolve_redshift('t_tentative', reds, 3, False)[0] is None
    assert resolve_redshift('t_tentative', reds, 2, False)[:2] == (4.0, 'inspected')
    assert resolve_redshift('t_impossible', reds, 2, False)[0] is None
    assert resolve_redshift('t_unknown', reds, 3, False)[0] is None
    # min_quality below 2 is clamped: a quality-0 "redshift" is the auto fit
    # (objects.redshift = COALESCE(inspected, auto)) and is never 'inspected'
    reds['t_uninspected'] = RedshiftEntry('t_uninspected', 3.0, 0)
    assert resolve_redshift('t_uninspected', reds, 0, False)[0] is None
    assert resolve_redshift('t_uninspected', reds, 0, False)[1] == 'quality 0 below min_quality 0'
    assert resolve_redshift('t_tentative', reds, 0, False)[:2] == (4.0, 'inspected')
    # auto fallback reads the zfit header
    zfit = tmp_path / 'x_zfit.fits'
    fits.PrimaryHDU(header=fits.Header({'ZBEST': 3.25})).writeto(zfit)
    assert resolve_redshift('t_unknown', reds, 3, True, str(zfit))[:2] == (3.25, 'auto')
    assert resolve_redshift('t_tentative', reds, 3, True, str(zfit))[:2] == (3.25, 'auto')
    assert resolve_redshift('t_unknown', reds, 3, True, str(tmp_path / 'nope.fits'))[0] is None


# ---------------------------------------------------------------------------
# Product IO + stage runner
# ---------------------------------------------------------------------------

def _write_spec_fits(path, wave, fnu, fnu_err):
    t = Table()
    t['wave'] = wave
    t['fnu'] = fnu
    t['fnu_err'] = fnu_err
    t['flam'] = fnu * 2.99792458e-19 / wave ** 2
    t['flam_err'] = fnu_err * 2.99792458e-19 / wave ** 2
    hdu = fits.BinTableHDU(t, name='SPEC1D')
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)


class _Obs:
    def __init__(self, name, workspace_dir, reference_dir):
        self.name = name
        self.workspace_dir = str(workspace_dir)
        self.reference_dir = str(reference_dir)
        self.stage_overrides = {}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fake observation with two spectra and a synthetic R-curve."""
    from campfire_pipeline.nirspec import linefit_stage as st
    ws = tmp_path / 'products' / 'nirspec' / 'obs1'
    ref = tmp_path / 'reference' / 'nirspec' / 'obs1'
    ws.mkdir(parents=True)
    ref.mkdir(parents=True)
    z = 5.5
    wave, fnu, err, _ = synth(z, 'g395m', TRUTH)
    _write_spec_fits(ws / 'obs1_g395m_f290lp_10_spec.fits', wave, fnu, err)
    wave, fnu, err, _ = synth(z, 'g395m', TRUTH, seed=2)
    _write_spec_fits(ws / 'obs1_g395m_f290lp_20_spec.fits', wave, fnu, err)
    fits.PrimaryHDU(header=fits.Header({'ZBEST': 5.5})).writeto(ws / 'obs1_g395m_f290lp_20_zfit.fits')

    # stand-in for the CRDS dispersion table
    import campfire_pipeline.common.spectral as spectral
    import campfire_pipeline.config as config
    monkeypatch.setattr(spectral, 'load_r_curve',
                        lambda p: (np.array([2.87, 5.2]), np.array([700.0, 1300.0])))
    monkeypatch.setattr(config, 'get_r_curve_path', lambda g: f'/fake/{g}.fits')
    return _Obs('obs1', ws, ref), ref, z


def test_stage_writes_reads_and_skips(workspace):
    from campfire_pipeline.nirspec.linefit_stage import (
        discover_lines_files, lines_payload, read_lines_file, run_linefit,
    )
    obs, ref, z = workspace
    cfg = {'nirspec': {'line_fitting': {'min_quality': 3, 'plot': True},
                       'redshift_fitting': {'f_LSF': 1.3, 'f_LSF_g395m': 1.5}},
           'pipeline': {'version': '9.9.9'}}
    # no redshift file: nothing fits
    counts = run_linefit(obs, cfg)
    assert counts == dict(fit=0, skipped_uptodate=0, skipped_no_z=2, failed=0)

    write_redshifts([RedshiftEntry('obs1_10', z, 4, 'CAMPFIRE-J1', 3, '2026-01-01T00:00:00Z'),
                     RedshiftEntry('obs1_20', z, 2, 'CAMPFIRE-J2', 1, None)],
                    str(ref / 'redshifts.toml'), 'obs1')
    counts = run_linefit(obs, cfg)
    assert counts['fit'] == 1 and counts['skipped_no_z'] == 1
    files = discover_lines_files(obs.workspace_dir)
    assert [f.name for f in files] == ['obs1_g395m_f290lp_10_lines.fits']
    assert os.path.exists(str(files[0]).replace('_lines.fits', '_lines.pdf'))

    prod = read_lines_file(files[0])
    h = prod['header']
    assert h['ZSRC'] == 'inspected' and h['ZQUAL'] == 4 and h['OBJID'] == 'CAMPFIRE-J1'
    assert h['OBJVER'] == 3 and h['CMPFRVER'] == '9.9.9' and h['FLSF'] == 1.5
    assert h['GRATING'] == 'G395M' and h['TARGETID'] == 'obs1_10'
    # same scheme-prefixed form as spectra.file_hash (metadata/reader.py)
    assert h['SPECHASH'].startswith('sha256:') and len(h['SPECHASH']) == 7 + 64
    assert abs(prod['lines']['Halpha']['flux'] - 5e-18) / prod['lines']['Halpha']['flux_err'] < 4
    assert prod['lines']['OIII4959']['tied_to'] == 'OIII5007'
    # doublet totals round-trip with their catalog label; the product carries the version
    assert h['LFITVER'] == '2'
    sii = prod['lines']['SII6725']
    assert sii['component'] == 'doublet' and sii['label'] == '[SII]λλ6716,6731'
    assert sii['flags'] & FLAG_RESOLVED and np.isfinite(sii['flux'])
    assert sii['members'] == ['SII6716', 'SII6731'] and h['NDOUBLET'] >= 1
    assert len(prod['model']['wave']) == len(prod['model']['model'])
    payload = lines_payload(prod['lines'])
    assert payload['Halpha']['flux'] > 0 and payload['Halpha']['tied_to'] is None
    import json
    json.dumps(payload, allow_nan=False)     # NaN-free

    # second run: up to date, nothing refit
    counts = run_linefit(obs, cfg)
    assert counts['fit'] == 0 and counts['skipped_uptodate'] == 1
    # inspected redshift moved → refit exactly that spectrum
    write_redshifts([RedshiftEntry('obs1_10', z + 0.001, 4, 'CAMPFIRE-J1', 4, None),
                     RedshiftEntry('obs1_20', z, 2)], str(ref / 'redshifts.toml'), 'obs1')
    counts = run_linefit(obs, cfg)
    assert counts['fit'] == 1
    assert read_lines_file(files[0])['header']['ZUSED'] == pytest.approx(z + 0.001)
    # lowering the gate pulls the tentative object in; allow_auto uses its zfit
    cfg['nirspec']['line_fitting']['min_quality'] = 2
    counts = run_linefit(obs, cfg)
    assert counts['fit'] == 1 and counts['skipped_uptodate'] == 1
    cfg['nirspec']['line_fitting']['min_quality'] = 3
    counts = run_linefit(obs, cfg, allow_auto=True, overwrite=True)
    assert counts['fit'] == 2
    h20 = read_lines_file(obs.workspace_dir + '/obs1_g395m_f290lp_20_lines.fits')['header']
    assert h20['ZSRC'] == 'auto' and h20['ZQUAL'] == 2   # entry quality kept for provenance
    # source-id and grating filters
    counts = run_linefit(obs, cfg, source_ids=['10'], overwrite=True, allow_auto=True)
    assert counts['fit'] == 1
    counts = run_linefit(obs, cfg, gratings=['prism'], overwrite=True, allow_auto=True)
    assert counts['fit'] == 0


def test_observation_carries_line_fitting_overrides(tmp_path):
    """[<obs>.line_fitting] in observations.toml reaches obs.stage_overrides
    (the whitelist in Observation.load must include it)."""
    from campfire_pipeline.nirspec.observation import Observation
    toml_path = tmp_path / 'observations.toml'
    toml_path.write_text(
        '[obs1]\n'
        'field = "uds"\n'
        'program = "ember-uds"\n'
        'data_subdir = "6585"\n'
        'program_id = 6585\n'
        'files = ["jw06585001001_03101"]\n'
        '[obs1.line_fitting]\n'
        'min_quality = 2\n'
        'fit_broad = false\n'
    )
    obs = Observation.load('obs1', observations_file=str(toml_path))
    assert obs.stage_overrides['line_fitting'] == {'min_quality': 2, 'fit_broad': False}


def test_stage_honours_per_observation_overrides(workspace):
    """A [<obs>.line_fitting] min_quality override changes what gets fit."""
    from campfire_pipeline.nirspec.linefit_stage import run_linefit
    obs, ref, z = workspace
    write_redshifts([RedshiftEntry('obs1_10', z, 4), RedshiftEntry('obs1_20', z, 2)],
                    str(ref / 'redshifts.toml'), 'obs1')
    cfg = {'nirspec': {'line_fitting': {'min_quality': 3, 'plot': False},
                       'redshift_fitting': {'f_LSF': 1.3}}, 'pipeline': {'version': '9.9.9'}}
    obs.stage_overrides = {'line_fitting': {'min_quality': 2}}
    counts = run_linefit(obs, cfg)
    assert counts['fit'] == 2 and counts['skipped_no_z'] == 0


def test_resolve_observation_without_observations_toml(tmp_path, monkeypatch):
    """linefit must work from the observation name alone: with no
    observations.toml the directories come from the layout contract."""
    from campfire_pipeline.nirspec.linefit_stage import (
        LinefitObservation, resolve_linefit_observation, run_linefit,
    )
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    monkeypatch.chdir(tmp_path)          # no ./observations.toml fallback either
    obs = resolve_linefit_observation('obs9', {})
    assert isinstance(obs, LinefitObservation)
    assert obs.name == 'obs9' and obs.stage_overrides == {}
    assert obs.workspace_dir == str(tmp_path / 'products' / 'nirspec' / 'obs9')
    assert obs.reference_dir == str(tmp_path / 'reference' / 'nirspec' / 'obs9')
    # nothing pulled yet: a clean no-op, not a crash
    counts = run_linefit(obs, {'nirspec': {}})
    assert counts == dict(fit=0, skipped_uptodate=0, skipped_no_z=0, failed=0)


def test_resolve_observation_missing_section_falls_back_but_malformed_raises(tmp_path, monkeypatch):
    """A TOML without this observation's section is treated like no TOML
    (nothing to lose); a section that exists but is malformed must raise
    rather than silently dropping its overrides."""
    from campfire_pipeline.nirspec.linefit_stage import (
        LinefitObservation, resolve_linefit_observation,
    )
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    (cfg_dir / 'observations.toml').write_text(
        '[other]\nfield = "uds"\nprogram = "ember-uds"\ndata_subdir = "6585"\n'
        'program_id = 6585\nfiles = ["jw06585001001_03101"]\n'
        '[broken]\nfield = "uds"\n'          # missing program / data_subdir / files
        '[broken.line_fitting]\nmin_quality = 2\n'
    )
    obs = resolve_linefit_observation('obs9', {})
    assert isinstance(obs, LinefitObservation) and obs.name == 'obs9'
    with pytest.raises(KeyError):
        resolve_linefit_observation('broken', {})


def test_resolve_observation_prefers_observations_toml(tmp_path, monkeypatch):
    from campfire_pipeline.nirspec.linefit_stage import resolve_linefit_observation
    from campfire_pipeline.nirspec.observation import Observation
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    (cfg_dir / 'observations.toml').write_text(
        '[obs1]\nfield = "uds"\nprogram = "ember-uds"\ndata_subdir = "6585"\n'
        'program_id = 6585\nfiles = ["jw06585001001_03101"]\n'
        '[obs1.line_fitting]\nmin_quality = 2\n'
    )
    obs = resolve_linefit_observation('obs1', {})
    assert isinstance(obs, Observation)
    assert obs.stage_overrides['line_fitting'] == {'min_quality': 2}
    assert obs.workspace_dir == str(tmp_path / 'products' / 'nirspec' / 'obs1')
