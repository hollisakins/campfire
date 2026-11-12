"""wcs_init: write the distortion model into a calibrated exposure's headers.

``calacs`` and ``calwf3`` leave a linear WCS in the FLC/FLT. The distortion
(the polynomial from the ``IDCTAB``; for ACS also the ``NPOLFILE`` and
``D2IMFILE`` lookup tables, the time-dependent skew and velocity aberration) is
added by ``stwcs.updatewcs``, which in the archive runs as part of the drizzle
stage. This step runs it with the astrometry database switched off
(``use_db=False``): every exposure gets the plain distortion-model WCS from the
pinned reference files, named ``IDC_<idctab>``, and no catalog-fitted solution
that depends on the state of the archive. Absolute astrometry is the ``align``
step's job.

``updatewcs`` finds its reference files through the ``jref`` (ACS) and ``iref``
(WFC3) environment variables. They are set for the duration of the call only,
from ``reference_env``, so the HST reference tree never leaks into the rest of
the process (see docs/hst/design.md, section 2.2).
"""

import os
from contextlib import contextmanager

from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log


@contextmanager
def reference_environment(reference_env):
    """Temporarily set HST reference-file variables (``jref``, ``iref``, ...).

    Values are directory paths; a trailing separator is added because the HST
    convention ``jref$name.fits`` is expanded by plain concatenation.
    """
    saved = {}
    try:
        for key, value in (reference_env or {}).items():
            saved[key] = os.environ.get(key)
            os.environ[key] = os.path.join(str(value), '')
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def wcs_init_step(exposure_file, reference_env=None, overwrite=False):
    """Run ``stwcs.updatewcs`` (no database) on one canonical HST exposure.

    Returns the resulting ``WCSNAME`` of the first chip, or ``None`` when the
    file already carries ``CFP_WCS`` and ``overwrite`` is not set. The file is
    updated in place by ``stwcs``.
    """
    rootname = os.path.basename(exposure_file).split('_')[0]
    if cfp.should_skip(exposure_file, 'CFP_WCS', rootname, 'wcs_init', None,
                       overwrite, keyset=cfp.HST):
        return None

    from stwcs import updatewcs

    with reference_environment(reference_env):
        updatewcs.updatewcs(exposure_file, use_db=False)

    with fits.open(exposure_file, mode='update', memmap=False) as hdul:
        wcsname = str(hdul['SCI', 1].header.get('WCSNAME', ''))
        if 'A_ORDER' not in hdul['SCI', 1].header:
            raise RuntimeError(
                f"{os.path.basename(exposure_file)}: updatewcs ran but left no "
                f"distortion polynomial in [SCI,1] (WCSNAME={wcsname!r})")
        for key, val in cfp.format(keyset=cfp.HST, CFP_WCS=wcsname or None).items():
            hdul[0].header[key] = val
    log(f"wcs_init: {rootname} -> {wcsname}")
    return wcsname
