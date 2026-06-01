"""
CAMPFIRE NIRSpec Detector1 customization for jwst >= 2.0.

This module defines:

- ``CampfireRampBkgStep`` — a per-group picture-frame + 1/f background
  correction that runs on the 4D ramp, *replacing* the stock jwst
  ``picture_frame`` and ``clean_flicker_noise`` steps for NIRSpec full-frame
  data. It reuses CAMPFIRE's WCS-derived open-slit mask (``mask_slits``) and
  the shared iterative background model (``_iterate_background``) from
  ``stage1``.
- ``CampfireDetector1Pipeline`` — a thin ``Detector1Pipeline`` subclass that
  inserts ``CampfireRampBkgStep`` between ``jump`` and ``ramp_fit`` and skips
  the two stock steps it replaces.

This module imports ``jwst`` at import time (it subclasses ``Step`` /
``Detector1Pipeline``), so it is imported lazily — only from within the
stage-1 worker — to keep the ``cfpipe`` CLI startup fast.
"""

import logging
import os

import numpy as np
from astropy.io import fits

from jwst.stpipe import Step
from jwst.pipeline import Detector1Pipeline
from jwst import datamodels

from campfire_pipeline.nirspec.stage1 import mask_slits, _iterate_background

# jwst 2.0 deprecates Step.log; use a module logger instead.
logger = logging.getLogger(__name__)


# Central horizontal band (rows) always masked as the fixed-slit region.
# Mirrors the constant used in subtract_background_from_rate_file.
_FIXED_SLIT_HALFHEIGHT = 100


class CampfireRampBkgStep(Step):
    """Per-group picture-frame + 1/f background correction on the 4D ramp.

    Replaces the stock ``picture_frame`` + ``clean_flicker_noise`` steps for
    NIRSpec full-frame exposures. Builds CAMPFIRE's open-slit background mask
    once (WCS-derived, from a draft rate), then for each integration/group
    subtracts an iterative picture-frame template fit + column/row 1/f model
    via ``_iterate_background``, operating on the zero-group-subtracted image
    exactly like the stock picture_frame step.

    Manual (DS9) masks are intentionally NOT applied here — they remain a
    rate-level concern handled by the phase-2 ``subtract_background_from_rate_file``
    cleanup pass, which keeps the restorable ``CFBKG`` machinery intact.
    """

    class_alias = "campfire_bkg"

    spec = """
        do_picture_frame = boolean(default=True)  # Subtract per-quarter picture-frame template
        subtract_2d = boolean(default=False)  # Subtract a 2D background
        box_size = integer(default=64)  # 2D background box size
        sigma_clip = boolean(default=True)  # Sigma-clip the component fits
        bkg_estimator = string(default='median')  # 2D background estimator
        do_col_1f = boolean(default=True)  # Subtract column 1/f
        do_row_1f = boolean(default=True)  # Subtract row 1/f (PRISM only)
        col_1f_method = option('median', 'template', default='template')  # Column 1/f method
        n_iter = integer(default=1)  # Background fit iterations per group
        plot = boolean(default=True)  # Emit a *_ramp_bkg.pdf diagnostic
    """

    reference_file_types = ['pictureframe']

    # Per-grating wavelength-range overrides for the slit mask (extends the
    # masked region to cover higher-order spectra). A dict can't live in the
    # configobj `spec`, so it's a plain attribute set by the caller.
    override_wavelength_range = {}

    def process(self, input_data):
        model = input_data  # in-memory RampModel handed down from the pipeline

        instrument = str(model.meta.instrument.name).upper()
        subarray = str(getattr(model.meta.subarray, 'name', '') or '').upper()
        if instrument != 'NIRSPEC' or subarray != 'FULL':
            logger.warning(
                'CampfireRampBkgStep applies only to NIRSpec full frame; skipping.')
            return model

        if not isinstance(model, datamodels.RampModel):
            logger.warning('CampfireRampBkgStep expects a RampModel; skipping.')
            return model

        nint, ngroup, ny, nx = model.data.shape
        if ngroup < 2:
            logger.warning(
                'CampfireRampBkgStep cannot run on single-group data; skipping.')
            return model

        input_dir = self.input_dir or ''

        # --- Build the open-slit background mask once (WCS-derived) ---
        # make_rate runs a RampFitStep on a copy to get a draft rate the WCS
        # can be assigned to; the actual per-group fits below use the ramp.
        from jwst.clean_flicker_noise import clean_flicker_noise as cfn
        draft = cfn.make_rate(model, input_dir=input_dir)
        try:
            slitmask = np.full(draft.data.shape, True)
            mid = draft.data.shape[0] // 2
            slitmask[mid - _FIXED_SLIT_HALFHEIGHT:mid + _FIXED_SLIT_HALFHEIGHT, :] = False
            slitmask = mask_slits(
                draft, input_dir, slitmask,
                override_wavelength_range=self.override_wavelength_range,
            )
            slitmask[draft.dq > 0] = False
        finally:
            draft.close()

        # --- Picture-frame reference template ---
        use_pictureframe = False
        template = None
        if self.do_picture_frame:
            pf_file = self.get_reference_file(model, 'pictureframe')
            if pf_file and pf_file.strip().upper() not in ('', 'N/A'):
                template = fits.getdata(pf_file)
                use_pictureframe = True
                logger.info(f'Using pictureframe reference: {pf_file}')
            else:
                logger.warning(
                    'No pictureframe reference available; skipping picture-frame term.')

        do_row_1f = bool(self.do_row_1f) and ('PRISM' in str(model.meta.instrument.grating))

        # --- Diagnostic accumulators (rate-equivalent; no extra ramp fit) ---
        # Accumulate each component with OLS slope weights over the groups so
        # the result is a true per-group slope (-> per-second rate after
        # dividing by group_time), directly comparable to the phase-2 rate
        # plot. Group 0 carries no correction (anchored at zero), consistent
        # with fitting on (group - group0). A uniform mean would overstate the
        # rate by ~ngroup/2; the slope weights are the correct estimator.
        collect = bool(self.plot)
        t = np.arange(ngroup, dtype=np.float64)
        _wnum = t - t.mean()
        _wden = float(np.sum(_wnum * _wnum))
        slope_w = (_wnum / _wden) if _wden > 0 else np.zeros_like(_wnum)
        data_rate = np.zeros((ny, nx), dtype=np.float64) if collect else None
        comp_rate = ({k: np.zeros((ny, nx), dtype=np.float64)
                      for k in ('pictureframe', 'bkg2d', 'col', 'row')}
                     if collect else None)
        last_mask = slitmask

        # --- Per-group correction loop ---
        # Mirrors stock picture_frame: fit on (group - zero_group), then
        # subtract the fitted background from the group in place.
        for i in range(nint):
            zero = model.data[i, 0]
            for g in range(1, ngroup):
                img = model.data[i, g] - zero
                bkg_total, mask, components = _iterate_background(
                    img, slitmask,
                    n_iter=int(self.n_iter),
                    do_picture_frame=use_pictureframe, template=template,
                    subtract_2d=bool(self.subtract_2d), box_size=int(self.box_size),
                    bkg_estimator=str(self.bkg_estimator), sigma_clip=bool(self.sigma_clip),
                    do_col_1f=bool(self.do_col_1f), col_1f_method=str(self.col_1f_method),
                    do_row_1f=do_row_1f, verbose=False,
                )
                model.data[i, g] = model.data[i, g] - bkg_total

                if collect:
                    w = slope_w[g]
                    data_rate += w * img
                    for k in comp_rate:
                        if components[k] is not None:
                            comp_rate[k] += w * components[k]
                    last_mask = mask

        # --- Diagnostic plot (Option A: single, rate-equivalent 2D) ---
        if collect:
            self._plot_diagnostic(
                model, data_rate, last_mask, comp_rate, nint,
                use_pictureframe, do_row_1f,
            )

        # History note (cal_step has no field for a custom step).
        from stdatamodels import util as stutil
        model.history.append(stutil.create_history_entry(
            'CAMPFIRE per-group picture-frame + 1/f background subtraction'))

        return model

    def _plot_diagnostic(self, model, data_rate, mask, comp_rate, nint,
                         use_pictureframe, do_row_1f):
        """Emit ``*_ramp_bkg.pdf`` showing the rate-equivalent correction.

        ``data_rate`` / ``comp_rate`` are slope-weighted sums over groups (one
        OLS slope per integration); dividing by ``nint`` averages over
        integrations and by ``group_time`` converts the per-group slope to a
        per-second rate, so the panels are in the same units as — and directly
        comparable to — the phase-2 rate-level plot.
        """
        from campfire_pipeline.nirspec.plots import plot_bkg_subtraction

        group_time = getattr(model.meta.exposure, 'group_time', None) or 1.0
        scale = 1.0 / (max(nint, 1) * group_time)

        data = data_rate * scale
        pf = comp_rate['pictureframe'] * scale if use_pictureframe else None
        b2 = comp_rate['bkg2d'] * scale if bool(self.subtract_2d) else None
        col = comp_rate['col'] * scale if bool(self.do_col_1f) else None
        row = comp_rate['row'] * scale if do_row_1f else None

        base = (model.meta.filename or 'ramp')
        for suffix in ('_uncal.fits', '_ramp.fits', '.fits'):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        out_pdf = os.path.join(self.input_dir or '.', f'{base}_ramp_bkg.pdf')

        try:
            plot_bkg_subtraction(
                out_pdf, data, mask,
                pictureframe_model=pf, bkg2d_model=b2,
                col_model=col, row_model=row, output_pdf=out_pdf,
            )
        except Exception as exc:  # diagnostics must never fail the reduction
            logger.warning(f'Ramp background diagnostic plot failed: {exc}')


class CampfireDetector1Pipeline(Detector1Pipeline):
    """``Detector1Pipeline`` that runs CAMPFIRE's per-group background step in
    place of the stock ``picture_frame`` + ``clean_flicker_noise`` steps.

    The ``process`` override mirrors the stock jwst 2.0.x body exactly except
    that, after ``jump``, NIRSpec full-frame data is corrected by
    ``self.campfire_bkg`` instead of the stock ``picture_frame`` +
    ``clean_flicker_noise`` steps. Any other instrument/subarray falls back to
    the stock steps, so non-NIRSpec data is never silently left uncorrected.
    """

    step_defs = {**Detector1Pipeline.step_defs, 'campfire_bkg': CampfireRampBkgStep}

    def process(self, input_data):
        logger.info("Starting CAMPFIRE calwebb_detector1 ...")

        input_data = self.prepare_output(input_data, open_as_type=datamodels.RampModel)

        # propagate output_dir to steps that might need it
        self.dark_current.output_dir = self.output_dir
        self.ramp_fit.output_dir = self.output_dir

        instrument = input_data.meta.instrument.name
        if instrument == "MIRI":
            # CAMPFIRE only processes NIRSpec, but keep the stock MIRI branch
            # for parity in case this pipeline is ever pointed at MIRI data.
            logger.debug("Processing a MIRI exposure")
            input_data = self.group_scale.run(input_data)
            input_data = self.dq_init.run(input_data)
            input_data = self.emicorr.run(input_data)
            input_data = self.saturation.run(input_data)
            input_data = self.ipc.run(input_data)
            input_data = self.firstframe.run(input_data)
            input_data = self.lastframe.run(input_data)
            input_data = self.reset.run(input_data)
            input_data = self.linearity.run(input_data)
            input_data = self.rscd.run(input_data)
            input_data = self.dark_current.run(input_data)
            input_data = self.refpix.run(input_data)
        else:
            logger.debug("Processing a Near-IR exposure")
            input_data = self.group_scale.run(input_data)
            input_data = self.dq_init.run(input_data)
            input_data = self.saturation.run(input_data)
            input_data = self.ipc.run(input_data)
            input_data = self.superbias.run(input_data)
            input_data = self.refpix.run(input_data)
            input_data = self.linearity.run(input_data)
            if instrument != "NIRSPEC":
                input_data = self.persistence.run(input_data)
            input_data = self.dark_current.run(input_data)

        input_data = self.charge_migration.run(input_data)
        input_data = self.jump.run(input_data)

        # CAMPFIRE per-group picture-frame + 1/f replaces the stock
        # picture_frame + clean_flicker_noise steps for NIRSpec full-frame data
        # (the only data CAMPFIRE reduces). For any other instrument/subarray,
        # fall back to the stock steps so we never silently drop both — the
        # CampfireRampBkgStep would otherwise no-op (it guards on NIRSpec FULL).
        subarray = str(getattr(input_data.meta.subarray, 'name', '') or '').upper()
        if instrument == "NIRSPEC" and subarray == "FULL":
            input_data = self.campfire_bkg.run(input_data)
        else:
            input_data = self.picture_frame.run(input_data)
            input_data = self.clean_flicker_noise.run(input_data)

        if self.save_calibrated_ramp:
            self.save_model(input_data, "ramp")

        if self.ramp_fit.skip:
            input_data = self.ramp_fit.run(input_data)
            ints_model = None
        else:
            input_data, ints_model = self.ramp_fit.run(input_data)

        if input_data is not None:
            self.gain_scale.suffix = "gain_scale"
            input_data = self.gain_scale.run(input_data)
        else:
            logger.info("NoneType returned from ramp_fit.  Gain Scale step skipped.")

        if ints_model is not None:
            self.gain_scale.suffix = "gain_scaleints"
            ints_model = self.gain_scale.run(ints_model)
            self.save_model(ints_model, "rateints")

        self.setup_output(input_data)
        logger.info("... ending CAMPFIRE calwebb_detector1")
        return input_data
