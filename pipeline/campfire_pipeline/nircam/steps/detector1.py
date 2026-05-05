"""
detector1: JWST ``Detector1Pipeline`` → canonical per-exposure file.

Reads ``<rootname>_uncal.fits`` from ``raw/nircam/<PID>/<filter>/``, runs
``Detector1Pipeline`` in memory, atomically saves the calibrated rate-stage
ImageModel to ``exposures/<filter>/<rootname>.fits``, and stamps ``CFP_DET1``.

The jump substep keeps ``save_results=True`` so that ``<rootname>_jump.fits``
lands alongside the canonical file. The persistence step that follows reads
those jump products via ``input_dir`` and removes them once flagging is
complete.
"""

import os

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.constants import SW_FILTERS, LW_FILTERS


def detector1_step(uncal_file, field, step_config, overwrite=False):
    """Run JWST Detector1Pipeline on a single ``_uncal.fits`` exposure.

    Parameters
    ----------
    uncal_file : str
        Full path to ``<rootname>_uncal.fits``.
    field : Field
        NIRCam field with workspace set up.
    step_config : dict
        ``[nircam.detector1]`` (or legacy ``[nircam.stage1.detector1]``) block.
    overwrite : bool
        If True, re-run even when ``CFP_DET1`` is already stamped on the
        canonical file.
    """
    from jwst.pipeline import calwebb_detector1

    clean_flicker_noise = step_config.get('clean_flicker_noise', False)

    filtname = uncal_file.split('/')[-2]
    assert (filtname.lower() in SW_FILTERS) or (filtname.lower() in LW_FILTERS)

    rootname = os.path.basename(uncal_file).removesuffix('_uncal.fits')
    output_dir = os.path.join(field.exposures_dir, filtname)
    os.makedirs(output_dir, exist_ok=True)
    canonical = field.get_exposure_path(rootname, filtname)

    if (os.path.exists(canonical)
            and not overwrite
            and cfp.has_step(canonical, 'CFP_DET1')):
        log(f"Skipping detector1 on {rootname}, CFP_DET1 already set")
        return

    log(f"Running detector1 on {rootname}")

    # Pipeline-level save_results=False suppresses both _rate.fits and
    # _rateints.fits auto-save; we save the result explicitly to the canonical
    # path below. The jump substep keeps save_results=True so _jump.fits lands
    # in output_dir for the persistence step to consume.
    kwargs = {
        'output_dir': output_dir,
        'save_results': False,
        'steps': {
            'group_scale': {'skip': False},
            'dq_init': {'skip': False},
            'emicorr': {'skip': True},
            'saturation': {
                'skip': False,
                'n_pix_grow_sat': 1,
                'use_readpatt': True,
            },
            'ipc': {'skip': False},
            'superbias': {'skip': False},
            'refpix': {
                'skip': False,
                'odd_even_columns': True,
                'odd_even_rows': True,
                'gaussmooth': 1.0,
                'halfwidth': 30,
                'side_gain': 1.0,
                'side_smoothing_length': 11,
                'sigreject': 4.0,
                'use_side_ref_pixels': True,
                'irs2_mean_subtraction': False,
                'ovr_corr_mitigation_ftr': 3.0,
                'preserve_irs2_refpix': False,
                'refpix_algorithm': 'median',
            },
            'rscd': {'skip': False},
            'firstframe': {'skip': False, 'bright_use_group1': False},
            'lastframe': {'skip': False},
            'linearity': {'skip': False},
            'dark_current': {
                'skip': False,
                'average_dark_current': None,
                'dark_output': None,
            },
            'reset': {'skip': False},
            'persistence': {
                'skip': False,
                'flag_pers_cutoff': 40.0,
                'save_persistence': False,
                'save_results': False,
                'save_trapsfilled': False,
            },
            'charge_migration': {'skip': True},
            'jump': {
                'skip': False,
                'after_jump_flag_dn1': 0.0,
                'after_jump_flag_dn2': 0.0,
                'after_jump_flag_time1': 0.0,
                'after_jump_flag_time2': 0.0,
                'edge_size': 25,
                'expand_factor': 2.2,
                'expand_large_events': True,
                'extend_ellipse_expand_ratio': 1.1,
                'extend_inner_radius': 1.0,
                'extend_min_area': 90,
                'extend_outer_radius': 2.6,
                'extend_snr_threshold': 1.2,
                'find_showers': False,
                'flag_4_neighbors': True,
                'four_group_rejection_threshold': 5.0,
                'mask_snowball_core_next_int': True,
                'max_extended_radius': 200,
                'max_jump_to_flag_neighbors': 300.0,
                'max_shower_amplitude': 4.0,
                'maximum_cores': 'none',
                'min_diffs_single_pass': 10,
                'min_jump_area': 15.0,
                'min_jump_to_flag_neighbors': 15.0,
                'min_sat_area': 1.0,
                'min_sat_radius_extend': 2.0,
                'minimum_groups': 3,
                'minimum_sigclip_groups': 100,
                'only_use_ints': True,
                'rejection_threshold': 4.0,
                'sat_expand': 2,
                'sat_required_snowball': False,
                'save_results': True,
                'search_output_file': True,
                'snowball_time_masked_next_int': 4000,
                'three_group_rejection_threshold': 6.0,
                'time_masked_after_shower': 15.0,
                'use_ellipses': True,
            },
            'clean_flicker_noise': {
                'skip': not clean_flicker_noise,
                'fit_by_channel': True,
            },
            'ramp_fit': {
                'skip': False,
                'algorithm': 'OLS_C',
                'maximum_cores': 'none',
            },
            'gain_scale': {'skip': False},
        },
    }

    result = calwebb_detector1.Detector1Pipeline.call(uncal_file, **kwargs)
    if result is None:
        return

    atomic_save(result, canonical, header_updates=cfp.format(CFP_DET1=None))
    result.close()
    log(f"Saved {os.path.basename(canonical)} (CFP_DET1)")
