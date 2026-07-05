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
