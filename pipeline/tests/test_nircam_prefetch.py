"""The NIRCam CRDS warm-up covers every reference a pending exposure uses (#700)."""

import os
from types import SimpleNamespace

from campfire_pipeline.nircam import prefetch


# A dark selected by DATE-OBS (USEAFTER), as jwst_nircam_dark_0054.rmap does,
# and a flat selected by FILTER. Neither depends on READPATT.
def _fake_recommend(params, reftypes):
    out = {}
    if 'dark' in reftypes:
        out['dark'] = ('jwst_nircam_dark_0619.fits'
                       if params['META.OBSERVATION.DATE'] >= '2025-01-29'
                       else 'jwst_nircam_dark_0473.fits')
    if 'flat' in reftypes:
        out['flat'] = f"flat_{params['META.INSTRUMENT.FILTER']}.fits"
    if 'pars-image2pipeline' in reftypes:
        out['pars-image2pipeline'] = 'jwst_nircam_pars-image2pipeline_0003.asdf'
    if 'pars-jumpstep' in reftypes:
        out['pars-jumpstep'] = 'N/A'
    return out


class _Field:
    def __init__(self, tmp_path, uncals):
        self.tmp_path = tmp_path
        self.uncals = uncals

    def get_uncal_files(self, filt):
        return [str(self.tmp_path / f"{root}_uncal.fits") for root in self.uncals]

    def get_exposure_path(self, rootname, filt):
        return str(self.tmp_path / f"{rootname}_cal.fits")


def _setup(monkeypatch, headers):
    fetched = []
    monkeypatch.setattr(prefetch, '_reftypes', lambda name: {
        'Detector1Pipeline': ['dark', 'pars-jumpstep'],
        'Image2Pipeline': ['flat', 'pars-image2pipeline'],
    }[name])
    monkeypatch.setattr(
        prefetch, '_crds_params',
        lambda f: headers[os.path.basename(f).removesuffix('_uncal.fits')])
    monkeypatch.setattr(prefetch, '_recommend', _fake_recommend)
    monkeypatch.setattr(
        prefetch, '_fetch',
        lambda params, reftypes, key_label: fetched.append((params, reftypes)))
    return fetched


def _warmed(fetched):
    return {v for params, reftypes in fetched
            for v in _fake_recommend(params, reftypes).values()
            if prefetch._is_file(v)}


def test_both_sides_of_a_useafter_boundary_are_warmed(monkeypatch, tmp_path):
    # Same detector / readpatt / subarray / filter, dates either side of the
    # 2025-01-29 USEAFTER boundary: the old (DETECTOR, READPATT, SUBARRAY)
    # dedup warmed only the first one's dark.
    hdr = {'META.INSTRUMENT.DETECTOR': 'NRCALONG',
           'META.EXPOSURE.READPATT': 'MEDIUM8',
           'META.SUBARRAY.NAME': 'FULL',
           'META.INSTRUMENT.FILTER': 'F444W'}
    headers = {
        'early': {**hdr, 'META.OBSERVATION.DATE': '2024-06-01'},
        'late': {**hdr, 'META.OBSERVATION.DATE': '2025-03-01'},
        'late2': {**hdr, 'META.OBSERVATION.DATE': '2025-04-01'},
    }
    fetched = _setup(monkeypatch, headers)
    prefetch.prefetch_process_references(
        _Field(tmp_path, ['early', 'late', 'late2']), ['f444w'])

    assert _warmed(fetched) == {
        'jwst_nircam_dark_0473.fits', 'jwst_nircam_dark_0619.fits',
        'flat_F444W.fits', 'jwst_nircam_pars-image2pipeline_0003.asdf'}
    # One download per distinct reference set, not one per exposure.
    assert len(fetched) == 2
    # The pars-* references are warmed too; N/A ones are not requested.
    assert all('pars-image2pipeline' in r and 'pars-jumpstep' not in r
               for _, r in fetched)


def test_finished_exposures_are_not_opened(monkeypatch, tmp_path):
    (tmp_path / 'done_cal.fits').write_bytes(b'')
    headers = {'todo': {'META.OBSERVATION.DATE': '2025-03-01',
                        'META.INSTRUMENT.FILTER': 'F444W'}}  # no 'done' entry
    fetched = _setup(monkeypatch, headers)
    status = SimpleNamespace(has=lambda path, key: path.endswith('done_cal.fits'))
    prefetch.prefetch_process_references(
        _Field(tmp_path, ['done', 'todo']), ['f444w'], status=status)
    assert _warmed(fetched) >= {'jwst_nircam_dark_0619.fits'}


def test_image2_only_skips_detector1_reftypes(monkeypatch, tmp_path):
    (tmp_path / 'a_cal.fits').write_bytes(b'')
    headers = {'a': {'META.OBSERVATION.DATE': '2025-03-01',
                     'META.INSTRUMENT.FILTER': 'F444W'}}
    fetched = _setup(monkeypatch, headers)
    status = SimpleNamespace(has=lambda path, key: key == 'CFP_DET1')
    prefetch.prefetch_process_references(
        _Field(tmp_path, ['a']), ['f444w'], status=status)
    assert len(fetched) == 1
    assert 'dark' not in _fake_recommend(*fetched[0])


def test_failed_recommendation_falls_back_to_direct_fetch(monkeypatch, tmp_path):
    headers = {'a': {'META.OBSERVATION.DATE': '2025-03-01',
                     'META.INSTRUMENT.FILTER': 'F444W'}}
    fetched = _setup(monkeypatch, headers)

    def boom(params, reftypes):
        raise RuntimeError('no rmap')
    monkeypatch.setattr(prefetch, '_recommend', boom)
    prefetch.prefetch_process_references(_Field(tmp_path, ['a']), ['f444w'])
    assert len(fetched) == 1
