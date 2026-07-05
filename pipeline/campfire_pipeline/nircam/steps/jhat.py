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
output; we copy those into the canonical exposure's directory so they're
preserved when the scratch directory goes away.
"""

import os
import shutil
import tempfile
import warnings

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


# Sentinel stamped into CFP_JHAT when an exposure cannot be aligned because it
# lies at/beyond the edge of the reference catalog footprint (zero refcat
# sources land on the detector). The input WCS is left untouched. Recorded so
# the exposure reads as intentionally-not-aligned and isn't retried every run.
NO_REFCAT_SENTINEL = 'NO_REFCAT_OVERLAP'


def _disable_jhat_plots():
    """No-op jhat's diagnostic plot functions so they can't abort a run.

    jhat's dx/dy diagnostic plotters compute a NaN axis limit and raise
    ``ValueError: Axis limits cannot be NaN or Inf`` on exposures whose
    best-match panel is degenerate (common near the edge of the reference
    catalog's footprint, where few sources match). That crash happens *inside*
    ``run_all`` and takes down the whole batch -- in some code paths *after* a
    perfectly good WCS solution has already been written. The plots are purely
    diagnostic, but jhat gates them on several independent flags (``saveplots``,
    ``showplots``, ``show_initial_plot``), not all reachable through
    ``align_wcs``, so the config flag alone can't suppress them. We disable them
    at the source by replacing the plot functions with no-ops.

    Reaching the right namespace is subtle: jhat's package ``__init__`` rebinds
    the ``st_wcs_align`` attribute on the ``jhat`` package from the *submodule*
    to a same-named *class*, so ``import jhat.st_wcs_align`` yields the class,
    not the module. The plot functions are module-level globals referenced by
    ``run_all`` at call time, so we patch the dict ``run_all`` actually resolves
    against: ``run_all.__globals__``.
    """
    from jhat.st_wcs_align import st_wcs_align as _WcsAlign
    plot_globals = _WcsAlign.run_all.__globals__

    def _noop(*args, **kwargs):
        return None

    for fn_name in ('dxdy_plot', 'initial_dxdy_plot', 'infoplots',
                    'plot_rotated'):
        if fn_name in plot_globals:
            plot_globals[fn_name] = _noop


def _stamp_jhat(exposure_file, value):
    """Atomically set CFP_JHAT=*value* on *exposure_file* (header only).

    Used for the no-refcat-overlap skip path, where jhat produces no output
    to promote -- we stamp the canonical file in place. Writes to a sibling
    .tmp on the same filesystem then renames, mirroring the atomicity the rest
    of this module relies on for networked-FS clusters.
    """
    base, ext = os.path.splitext(exposure_file)
    tmp_path = f'{base}.tmp{ext}'
    with fits.open(exposure_file) as hdul:
        hdul[0].header['CFP_JHAT'] = (value, cfp.CFP_COMMENTS['CFP_JHAT'])
        hdul.writeto(tmp_path, overwrite=True)
    os.replace(tmp_path, exposure_file)


def _resolve_refcat(step_config, refcat_dir, filtname):
    """Resolve the reference-catalog path for *filtname* from *step_config*.

    A single ``refcat`` string is used for every filter; a ``refcat_dict``
    maps filter -> filename for per-filter catalogs. When both are set,
    ``refcat_dict`` entries win per-filter and ``refcat`` is the fallback for
    any filter it doesn't list. The chosen filename is joined onto
    *refcat_dir*. Raises ``ValueError`` if neither is configured, or if only a
    ``refcat_dict`` is set and it lacks an entry for *filtname*.
    """
    refcat_dict = step_config.get('refcat_dict')
    refcat_single = step_config.get('refcat')

    if refcat_dict is None and refcat_single is None:
        raise ValueError(
            "jhat config missing 'refcat'/'refcat_dict'. Define either "
            "[<field>.jhat].refcat = \"<file>\" (one catalog for all filters) "
            "or [<field>.jhat.refcat_dict] mapping filter names to refcat "
            "filenames in fields.toml."
        )

    if refcat_dict is not None and filtname in refcat_dict:
        refcat_name = refcat_dict[filtname]
    elif refcat_single is not None:
        refcat_name = refcat_single
    else:
        raise ValueError(
            f"jhat.refcat_dict has no entry for filter '{filtname}' and no "
            f"single 'refcat' fallback is set. Available: "
            f"{list(refcat_dict.keys())}"
        )
    return os.path.join(refcat_dir, refcat_name)


def _copy_diagnostics(scratch_subdir, diag_dir, rootname):
    """Move jhat's diagnostic PDFs and ECSV photometry tables to ``diag_dir``."""
    if not os.path.isdir(scratch_subdir):
        return
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


def jhat_step(exposure_file, field, step_config, overwrite=False, status=None):
    """Align WCS of a single canonical exposure against a reference catalog.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` (post-variance; cal-stage data with
        WCS to be refined).
    field : Field
    step_config : dict
        ``[nircam.jhat]`` (legacy ``[nircam.stage3.jhat]``). Must specify the
        reference catalog(s) in ``field.refcat_dir`` via either ``refcat`` (a
        single filename used for every filter) or ``refcat_dict`` (a mapping of
        filter name to refcat filename). If both are given, ``refcat_dict``
        entries win per-filter and ``refcat`` is the fallback for any filter
        not listed.
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    # Lazy import: ``jhat`` transitively imports stpipe at module load, which
    # constructs CRDS's server proxy from the env. Importing it at module-load
    # time fires before ``setup_environment`` has populated CRDS_SERVER_URL,
    # locking the proxy into serverless mode for the rest of the process.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from jhat import align_wcs_batch

    # Unless diagnostics are explicitly requested, neutralize jhat's plotting:
    # it crashes the run on degenerate-match (footprint-edge) exposures and the
    # config ``saveplots`` flag can't fully suppress it. See _disable_jhat_plots.
    if not step_config.get('saveplots', False):
        _disable_jhat_plots()

    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]

    if cfp.should_skip(exposure_file, 'CFP_JHAT', rootname,
                       'jhat', status, overwrite):
        return

    refcat = _resolve_refcat(step_config, field.refcat_dir, filtname)

    log(f"Running jhat on {rootname}")

    input_dir = os.path.dirname(exposure_file)

    with tempfile.TemporaryDirectory(prefix='jhat-') as scratch:
        # JHAT's load_image (jhat/simple_jwst_phot.py) infers image type from a
        # filename regex and only accepts ``_cal.fits`` / ``_tweakregstep.fits``
        # / ``_assignwcsstep.fits`` / ``_i2d.fits``. Our canonical exposures end
        # in plain ``.fits``, so we feed JHAT a ``<rootname>_cal.fits`` symlink
        # in a scratch input dir.
        shim_dir = os.path.join(scratch, 'input')
        os.makedirs(shim_dir, exist_ok=True)
        shim_name = f'{rootname}_cal.fits'
        shim_path = os.path.join(shim_dir, shim_name)
        os.symlink(exposure_file, shim_path)

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
            [shim_name],
            directory=shim_dir,
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
                # Off by default; jhat's plotters are also no-op'd above unless
                # this is set (see _disable_jhat_plots). Re-enable diagnostics
                # per-field via [<field>.jhat].saveplots = true.
                saveplots=step_config.get('saveplots', False),
                savephottable=step_config.get('savephottable', True),
            )
        except KeyError as e:
            # jhat raises a bare ``KeyError(None)`` when it finds zero
            # reference sources within the image bounds: it prints
            # "0 sources from reference catalog within the image bounderies"
            # and skips the matching steps, leaving ``refcat_xcol`` unset, then
            # indexes ``phot.t[None]``. This means the exposure lies at/over
            # the edge of the refcat footprint -- a data-coverage condition,
            # not a campfire failure -- so it must not abort the whole run.
            # Record the skip and move on; the input WCS is preserved.
            if e.args == (None,):
                log(f"jhat: no refcat overlap for {rootname}; skipping "
                    f"alignment (input WCS preserved, CFP_JHAT="
                    f"{NO_REFCAT_SENTINEL})")
                _stamp_jhat(exposure_file, NO_REFCAT_SENTINEL)
                return
            log(f"jhat failed on {exposure_file}")
            raise
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
        _copy_diagnostics(
            os.path.join(scratch, filtname), input_dir, rootname,
        )

        # Stage to a sibling .tmp on the products filesystem before the
        # atomic rename. ``scratch`` lives on TMPDIR (often node-local /tmp),
        # which on networked-FS clusters like CANDIDE is a different device
        # from the products tree -- a direct ``os.replace`` would fail with
        # EXDEV. Copying into a sibling .tmp keeps the final swap atomic.
        base, ext = os.path.splitext(exposure_file)
        tmp_path = f'{base}.tmp{ext}'
        shutil.copy2(scratch_out, tmp_path)
        os.replace(tmp_path, exposure_file)
        log(f"jhat done: {rootname}")
