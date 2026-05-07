"""
wcs_shift: pre-JHAT bulk astrometric shift (opt-in, rule-driven).

Some NIRCam exposures land far enough off the reference catalog that
``jhat`` can't converge — the WCS is off by tens of arcseconds, beyond
the rough-cut radius that JHAT's source-matching uses. This step
applies a per-rule bulk shift (``delta_ra``, ``delta_dec``,
``delta_roll``, ``scale``) to the GWCS via
``jwst.tweakreg.utils.adjust_wcs`` so JHAT has something close enough
to refine.

Rules are declared per-field in ``fields.toml`` as an array of tables::

    [[<field>.wcs_shift]]
        files     = ['jw01837002007*', 'jw01837002009*']
        filters   = ['f090w']           # optional; default = all field filters
        delta_ra  = -0.0022538           # degrees
        delta_dec = -0.003637
        delta_roll = 0.0                 # default 0.0
        scale     = 1.0                  # default 1.0

Idempotency: on first apply, the original GWCS is stashed into a
``WCS_BAK`` extension (a 1-D ``uint8`` ImageHDU containing a standalone
ASDF blob). On ``--overwrite``, the original WCS is restored from
``WCS_BAK`` *before* the (possibly different) shift is applied, so the
operation stays declarative — config specifies the desired shift, the
step makes the on-disk state match.
"""

import io
import os
import warnings
from fnmatch import fnmatch

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


WCS_BAK_EXTNAME = 'WCS_BAK'


def _match_rule(rootname, filtname, rules):
    """Return the first matching rule, or None.

    A rule matches when (a) ``filtname`` is in its ``filters`` list and
    (b) any of its ``files`` globs matches ``rootname``. Rules are
    evaluated in declaration order; the first match wins.
    """
    matches = []
    for i, rule in enumerate(rules):
        if filtname not in rule['filters']:
            continue
        if any(fnmatch(rootname, glob) for glob in rule['files']):
            matches.append((i, rule))
    if not matches:
        return None
    if len(matches) > 1:
        winner = matches[0]
        losers = [m[0] for m in matches[1:]]
        log(f"wcs_shift: {rootname} matches multiple rules "
            f"(#{winner[0]} wins, also: {losers}); using first")
    return matches[0][1]


def _serialize_gwcs_to_hdu(wcs, name=WCS_BAK_EXTNAME):
    """Stash a GWCS as a 1-D uint8 ImageHDU containing an ASDF blob.

    ``asdf`` writes the standalone ASDF tree (header + binary blocks for
    lookup tables) into a single byte stream, which we wrap as a FITS
    extension so it rides alongside the canonical exposure rather than
    spilling into a sidecar file.
    """
    import asdf
    af = asdf.AsdfFile({'wcs': wcs})
    buf = io.BytesIO()
    af.write_to(buf)
    data = np.frombuffer(buf.getvalue(), dtype=np.uint8).copy()
    return fits.ImageHDU(data=data, name=name)


def _deserialize_gwcs_from_hdu(hdu):
    """Inverse of ``_serialize_gwcs_to_hdu``. Eagerly loads to detach
    from the BytesIO before it's closed."""
    import asdf
    buf = io.BytesIO(hdu.data.tobytes())
    with asdf.open(buf, lazy_load=False) as af:
        return af['wcs']


def _format_cfp_value(rule):
    return (
        f'dra={rule["delta_ra"]:.6g},'
        f'ddec={rule["delta_dec"]:.6g},'
        f'droll={rule["delta_roll"]:.6g},'
        f'scale={rule["scale"]:.6g}'
    )


def wcs_shift_step(exposure_file, field, step_config, overwrite=False,
                   status=None):
    """Apply a pre-JHAT astrometric shift to a canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` (post-variance, pre-jhat).
    field : Field
    step_config : dict
        Resolved ``[nircam.wcs_shift]`` config; the orchestrator injects
        the parsed field-level rules under ``step_config['rules']``.
    overwrite : bool
    status : StepStatus, optional
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]

    rules = step_config.get('rules') or []
    rule = _match_rule(rootname, filtname, rules)
    if rule is None:
        # No-match exposures aren't stamped — re-runs cheaply re-evaluate
        # the rule list against the current rootname without a fits.open.
        return

    already_applied = (status.has(exposure_file, 'CFP_SHIFT')
                       if status is not None
                       else cfp.has_step(exposure_file, 'CFP_SHIFT'))
    if already_applied and not overwrite:
        log(f"Skipping wcs_shift on {rootname}: CFP_SHIFT already set")
        return

    log(f"Running wcs_shift on {rootname}: {_format_cfp_value(rule)}")

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        from jwst.datamodels import ImageModel
        from jwst.tweakreg.utils import adjust_wcs
        from jwst.assign_wcs.util import update_fits_wcsinfo

    # Preserve any non-datamodel extensions we'll need to re-attach: the
    # existing WCS_BAK (re-applied case), the prior SCI-side SRCMASK
    # (so it survives the datamodel save round-trip).
    existing_wcs_bak = None
    srcmask_hdu = None
    with fits.open(exposure_file) as hdul:
        if WCS_BAK_EXTNAME in hdul:
            wb = hdul[WCS_BAK_EXTNAME]
            existing_wcs_bak = fits.ImageHDU(
                data=wb.data.copy(), header=wb.header.copy(),
                name=WCS_BAK_EXTNAME,
            )
        if 'SRCMASK' in hdul:
            sm = hdul['SRCMASK']
            srcmask_hdu = fits.ImageHDU(
                data=sm.data.copy(), header=sm.header.copy(), name='SRCMASK',
            )

    model = ImageModel(exposure_file)

    if already_applied:
        if existing_wcs_bak is None:
            raise RuntimeError(
                f"wcs_shift overwrite on {rootname}: CFP_SHIFT is set but "
                f"WCS_BAK extension is missing. Cannot safely re-apply. "
                f"Run `cfpipe nircam reset --from image2` and re-run upstream."
            )
        model.meta.wcs = _deserialize_gwcs_from_hdu(existing_wcs_bak)
        wcs_bak_to_save = existing_wcs_bak
    else:
        # Fresh apply — current WCS becomes the baseline for any future
        # overwrite of this step.
        wcs_bak_to_save = _serialize_gwcs_to_hdu(model.meta.wcs)

    model.meta.wcs = adjust_wcs(
        model.meta.wcs,
        delta_ra=rule['delta_ra'],
        delta_dec=rule['delta_dec'],
        delta_roll=rule['delta_roll'],
        scale_factor=rule['scale'],
    )
    update_fits_wcsinfo(model)

    extra_hdus = [wcs_bak_to_save]
    if srcmask_hdu is not None:
        extra_hdus.append(srcmask_hdu)

    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(CFP_SHIFT=_format_cfp_value(rule)),
        extra_hdus=extra_hdus,
    )
    model.close()
    log(f"wcs_shift done: {rootname}")
