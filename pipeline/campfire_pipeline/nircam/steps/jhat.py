"""
jhat: WCS alignment via JHAT against a reference catalog.

Per-exposure step. Runs ``jhat.align_wcs_batch`` on a single canonical
exposure file inside a private scratch directory, stamps ``CFP_JHAT`` with
the refcat name on the scratch output, and atomically replaces the
canonical file with the WCS-corrected version. JHAT preserves all FITS
extensions (verified against ``jhat/st_wcs_align.py:1088-1096``), so
``SRCMASK`` and any other non-standard extensions ride through the round
trip without extra handling.

JHAT also writes diagnostic PDFs and photometry tables alongside its
output; we copy those to ``exposures/<filter>/diagnostics/`` so they're
preserved when the scratch directory goes away.
"""

import os
import shutil
import tempfile
import warnings

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from jhat import align_wcs_batch


def _copy_diagnostics(scratch_subdir, diag_dir, rootname):
    """Move jhat's diagnostic PDFs and ECSV photometry tables to ``diag_dir``."""
    if not os.path.isdir(scratch_subdir):
        return
    os.makedirs(diag_dir, exist_ok=True)
    for fname in os.listdir(scratch_subdir):
        if not fname.startswith(rootname):
            continue
        if fname.endswith(('.pdf', '.ecsv', '.txt')):
            src = os.path.join(scratch_subdir, fname)
            dst = os.path.join(diag_dir, fname)
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                log(f"jhat: failed to copy diagnostic {fname}: {e}")


def jhat_step(exposure_file, field, step_config, overwrite=False):
    """Align WCS of a single canonical exposure against a reference catalog.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` (post-variance; cal-stage data with
        WCS to be refined).
    field : Field
    step_config : dict
        ``[nircam.jhat]`` (legacy ``[nircam.stage3.jhat]``). Must include a
        ``refcat_dict`` mapping filter names to refcat filenames in
        ``field.refcat_dir``.
    overwrite : bool
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]

    if not overwrite and cfp.has_step(exposure_file, 'CFP_JHAT'):
        log(f"Skipping jhat on {rootname}: CFP_JHAT already set")
        return

    if 'refcat_dict' not in step_config:
        raise ValueError(
            "jhat config missing 'refcat_dict'. Define "
            "[<field>.jhat.refcat_dict] in fields.toml mapping filter names "
            "to refcat filenames."
        )
    refcat_dict = step_config['refcat_dict']
    if filtname not in refcat_dict:
        raise ValueError(
            f"jhat.refcat_dict has no entry for filter '{filtname}'. "
            f"Available: {list(refcat_dict.keys())}"
        )
    refcat = os.path.join(field.refcat_dir, refcat_dict[filtname])

    log(f"Running jhat on {rootname}")

    input_dir = os.path.dirname(exposure_file)

    with tempfile.TemporaryDirectory(prefix='jhat-') as scratch:
        align_batch = align_wcs_batch()
        align_batch.verbose = step_config.get('verbose', True)
        align_batch.debug = step_config.get('debug', True)
        align_batch.sip_err = step_config.get('sip_err', 1.0)
        align_batch.replace_sip = True
        align_batch.sip_degree = 3
        align_batch.sip_points = 128
        align_batch.rough_cut_px_min = step_config.get('rough_cut_px_min', 2.5)
        align_batch.rough_cut_px_max = step_config.get('rough_cut_px_max', 2.5)
        align_batch.d_rotated_Nsigma = step_config.get('d_rotated_Nsigma', 3.0)

        align_batch.get_input_files(
            [os.path.basename(exposure_file)],
            directory=input_dir,
            detectors=None, filters=None, pupils=None,
        )

        ixs = align_batch.getindices()
        if not len(ixs):
            log(f"jhat: no input found for {rootname}")
            return

        align_batch.get_output_filenames(
            ixs=ixs, outrootdir=scratch, outsubdir=filtname,
            addfilter2outsubdir=False,
        )

        try:
            align_batch.align_wcs(
                ixs, overwrite=True, outrootdir=scratch, outsubdir=filtname,
                addfilter2outsubdir=False,
                photometry_method='aperture',
                find_stars_threshold=3.0,
                sci_xy_catalog=None,
                use_dq=False,
                refcatname=refcat,
                refcat_racol='RA',
                refcat_deccol='DEC',
                refcat_magcol='mag',
                refcat_magerrcol='mag_err',
                refcat_colorcol=None,
                pmflag=False,
                pm_median=False,
                load_photcat_if_exists=False,
                rematch_refcat=False,
                SNR_min=10.0,
                d2d_max=step_config.get('d2d_max', 1.5),
                dmag_max=0.1,
                sharpness_lim=(None, None),
                roundness1_lim=(None, None),
                delta_mag_lim=step_config.get('delta_mag_lim', [-3, 4]),
                objmag_lim=step_config.get('objmag_lim', [19, 28]),
                refmag_lim=(None, None),
                slope_min=-10 / 2048.0,
                Nbright4match=None, Nbright=None,
                histocut_order=step_config.get('histocut_order', 'dxdy'),
                xshift=0.0, yshift=0.0,
                iterate_with_xyshifts=step_config.get('iterate_with_xyshifts',
                                                     True),
                showplots=0,
                saveplots=step_config.get('saveplots', True),
                savephottable=step_config.get('savephottable', True),
            )
        except Exception:
            log(f"jhat failed on {exposure_file}")
            raise

        align_batch.write()

        scratch_out = align_batch.t.loc[ixs[0], 'outfilename']
        if not os.path.exists(scratch_out):
            log(f"jhat: expected output {scratch_out} not found; aborting")
            return

        # Stamp CFP_JHAT on the scratch output before promoting to canonical
        with fits.open(scratch_out, mode='update') as hdul:
            hdul[0].header['CFP_JHAT'] = (
                os.path.basename(refcat),
                cfp.CFP_COMMENTS['CFP_JHAT'],
            )

        # Preserve diagnostic plots/tables before the scratch dir is removed
        diag_dir = os.path.join(input_dir, 'diagnostics')
        _copy_diagnostics(
            os.path.join(scratch, filtname), diag_dir, rootname,
        )

        os.replace(scratch_out, exposure_file)
        log(f"jhat done: {rootname}")
