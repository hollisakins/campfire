"""
image2: JWST ``Image2Pipeline`` against the canonical exposure file.

Reads the canonical ``<rootname>.fits`` (rate-stage data after striping),
runs ``Image2Pipeline`` in memory (``save_results=False``), and atomically
writes the calibrated cal-stage ImageModel back to the same path.

``Image2Pipeline`` returns a fresh model, so the ``SRCMASK`` extension that
striping wrote to the canonical file is dropped on the round-trip. We pull
``SRCMASK`` out before the pipeline runs and re-attach it via
``atomic_save(..., extra_hdus=...)`` after.
"""

import os

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.constants import SW_FILTERS, LW_FILTERS


def _extract_srcmask(exposure_file):
    """Return a copy of the SRCMASK HDU from ``exposure_file``, or None."""
    with fits.open(exposure_file) as hdul:
        if 'SRCMASK' not in hdul:
            return None
        hdu = hdul['SRCMASK']
        return fits.ImageHDU(hdu.data.copy(), header=hdu.header.copy(),
                             name='SRCMASK')


def image2_step(exposure_file, field, step_config, overwrite=False,
                status=None):
    """Run JWST Image2Pipeline on a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` (post-striping; rate-stage data).
    field : Field
    step_config : dict
        ``[nircam.image2]`` (legacy ``[nircam.stage2.image2]``).
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    from jwst.pipeline import calwebb_image2

    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]
    assert (filtname.lower() in SW_FILTERS) or (filtname.lower() in LW_FILTERS)

    # WFSS/TSGRISM exposures hit calc_nircam's imaging branch which only
    # matches filter+pupil — the phot_table has one row per (filter, pupil,
    # order) for grism, so find_row returns >1 and raises. Skip here as a
    # backstop; primary gates are query.py (download) and _run_detector1.
    exp_type = fits.getval(exposure_file, 'EXP_TYPE', ext=0)
    if exp_type in ('NRC_WFSS', 'NRC_TSGRISM'):
        log(f"image2: skipping {rootname}: EXP_TYPE={exp_type} not imaging")
        return

    if cfp.should_skip(exposure_file, 'CFP_IMG2', rootname,
                       'image2', status, overwrite):
        return

    log(f"Running image2 on {rootname}")

    # Hold SRCMASK across the round-trip — Image2Pipeline returns a fresh
    # cal-stage model that doesn't carry our extra extension.
    srcmask_hdu = _extract_srcmask(exposure_file)

    kwargs = {
        'output_dir': os.path.dirname(exposure_file),
        'save_results': False,
        'steps': {
            'bkg_subtract': {'skip': True},
            'assign_wcs': {
                'skip': False,
                'save_results': False,
                'sip_approx': True,
                'sip_degree': None,
                'sip_inv_degree': None,
                'sip_max_inv_pix_error': 0.25,
                'sip_max_pix_error': 0.25,
                'sip_npoints': 32,
                'slit_y_high': 0.55,
                'slit_y_low': -0.55,
            },
            'flat_field': {'skip': False},
            'photom': {'skip': False},
            'resample': {'skip': True},
        },
    }

    if step_config.get('use_custom_flat', False):
        detector = rootname.split('_')[-1]
        flat_file = os.path.join(
            field.flats_dir,
            f'flat_nircam_{filtname.upper()}_{detector.upper()}_CLEAR.fits',
        )
        if os.path.exists(flat_file):
            kwargs['steps']['flat_field']['user_supplied_flat'] = flat_file
        else:
            log(f"Custom flat {os.path.basename(flat_file)} not in "
                f"{field.flats_dir}; falling back to CRDS")

    try:
        result = calwebb_image2.Image2Pipeline.call(exposure_file, **kwargs)
    except ValueError:
        log(f"image2 failed on {exposure_file}")
        raise

    if isinstance(result, list):
        if len(result) != 1:
            raise RuntimeError(
                f"Expected single Image2Pipeline result for {rootname}, "
                f"got {len(result)}"
            )
        result = result[0]

    extras = [srcmask_hdu] if srcmask_hdu is not None else None
    atomic_save(
        result, exposure_file,
        header_updates=cfp.format(CFP_IMG2=None),
        extra_hdus=extras,
    )
    result.close()
    log(f"image2 done: {rootname}")
