"""Shared test helper: a CRDS-free, datamodel-free JWST-structured gwcs builder.

tweakwcs ships a mock gwcs builder in its test package, but the function name
differs across versions (``make_mock_jwst_wcs`` in 0.8.x, ``make_mock_st_wcs``
in 0.9.x). This wraps whichever is available so the align tests are
version-tolerant and skip cleanly if a future tweakwcs drops the helper. Not a
``test_`` module, so pytest does not collect it.

``make_mock_wcs(v2ref=, v3ref=, roll=, crpix=, cd=, crval=)`` returns a
``gwcs.WCS`` with a 1024x2048 bounding box (x in [-0.5, 1023.5], y in
[-0.5, 2047.5]); keep synthetic source pixels inside it.
"""

try:
    from tweakwcs.tests.helper_correctors import make_mock_jwst_wcs as _mk

    def make_mock_wcs(**kw):
        return _mk(**kw)

    HAVE_MOCK_WCS = True
except Exception:  # pragma: no cover - version fallback
    try:
        from tweakwcs.correctors import JWSTWCSCorrector as _JC
        from tweakwcs.tests.helper_correctors import make_mock_st_wcs as _mk

        def make_mock_wcs(**kw):
            return _mk(corr_cls=_JC, **kw)

        HAVE_MOCK_WCS = True
    except Exception:  # pragma: no cover
        make_mock_wcs = None
        HAVE_MOCK_WCS = False


# --- persistable JWST gwcs (survives the ImageModel FITS round-trip) ---------
# The tweakwcs mock above uses a custom (untagged) V2V3ToSky model, which
# ImageModel.save silently drops. This builder uses jwst's own asdf-tagged
# `v23tosky` + Shift/Scale, so `model.meta.wcs` persists through save/reload —
# required to integration-test the align I/O layer (apply.py). jwst-dependent.

try:
    import numpy as _np
    import astropy.units as _u
    from astropy import coordinates as _coord
    from astropy.modeling import models as _models
    import gwcs as _gwcs
    from jwst.datamodels import ImageModel as _ImageModel
    from jwst.assign_wcs.pointing import v23tosky as _v23tosky

    HAVE_PERSISTABLE_WCS = True
except Exception:  # pragma: no cover
    HAVE_PERSISTABLE_WCS = False


_DEF = dict(v2ref=120.0, v3ref=500.0, roll=30.0, ra_ref=80.0, dec_ref=-30.0,
            crpix=(512.0, 512.0), scale=1e-5)


def make_persistable_wcs(*, v2ref=120.0, v3ref=500.0, roll=30.0, ra_ref=80.0,
                         dec_ref=-30.0, crpix=(512.0, 512.0), scale=1e-5):
    """A JWST-structured gwcs (detector->v2v3->world) that persists through an
    ImageModel FITS save/reload. ``scale`` is deg/pixel; the sky reference point
    is ``(ra_ref, dec_ref)`` and the injected astrometric offset is applied by
    shifting those (1 arcsec = ``1/3600`` deg / cos(dec) in RA)."""
    stub = _ImageModel()
    for k, v in [('v2_ref', v2ref), ('v3_ref', v3ref), ('roll_ref', roll),
                 ('ra_ref', ra_ref), ('dec_ref', dec_ref),
                 ('v3yangle', 0.0), ('vparity', -1)]:
        setattr(stub.meta.wcsinfo, k, v)
    det = _gwcs.coordinate_frames.Frame2D(name='detector', axes_order=(0, 1),
                                          unit=(_u.pix, _u.pix))
    v2v3 = _gwcs.coordinate_frames.Frame2D(name='v2v3', axes_order=(0, 1),
                                           unit=(_u.arcsec, _u.arcsec))
    world = _gwcs.coordinate_frames.CelestialFrame(
        reference_frame=_coord.ICRS(), name='world')
    det2v2v3 = ((_models.Shift(-crpix[0]) & _models.Shift(-crpix[1]))
                | (_models.Scale(scale * 3600) & _models.Scale(scale * 3600))
                | (_models.Shift(v2ref) & _models.Shift(v3ref)))
    w = _gwcs.wcs.WCS([(det, det2v2v3), (v2v3, _v23tosky(stub)), (world, None)])
    w.bounding_box = ((-0.5, 1023.5), (-0.5, 2047.5))
    w.array_shape = (2048, 1024)
    return w


def build_canonical(path, sci, *, err=None, dq=None, srcmask=None,
                    v2ref=120.0, v3ref=500.0, roll=30.0, ra_ref=80.0,
                    dec_ref=-30.0, crpix=(512.0, 512.0), scale=1e-5):
    """Write a canonical NIRCam exposure FITS (ImageModel: SCI/ERR/DQ + a
    persisted gwcs, plus an optional SRCMASK). Returns the gwcs so a caller can
    map source pixels to sky (e.g. to build the reference catalog)."""
    from astropy.io import fits
    from campfire_pipeline.common.io import atomic_save

    ny, nx = sci.shape
    w = make_persistable_wcs(v2ref=v2ref, v3ref=v3ref, roll=roll, ra_ref=ra_ref,
                             dec_ref=dec_ref, crpix=crpix, scale=scale)
    m = _ImageModel((ny, nx))
    m.data = _np.asarray(sci, dtype='float32')
    m.err = (_np.ones((ny, nx), 'float32') if err is None
             else _np.asarray(err, dtype='float32'))
    m.dq = (_np.zeros((ny, nx), 'uint32') if dq is None
            else _np.asarray(dq, dtype='uint32'))
    for k, v in [('v2_ref', v2ref), ('v3_ref', v3ref), ('roll_ref', roll),
                 ('ra_ref', ra_ref), ('dec_ref', dec_ref),
                 ('v3yangle', 0.0), ('vparity', -1)]:
        setattr(m.meta.wcsinfo, k, v)
    m.meta.wcs = w
    extra = None
    if srcmask is not None:
        extra = [fits.ImageHDU(data=_np.asarray(srcmask, dtype='uint8'),
                               name='SRCMASK')]
    atomic_save(m, path, extra_hdus=extra)
    m.close()
    return w
