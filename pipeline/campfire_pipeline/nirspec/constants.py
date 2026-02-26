"""
NIRSpec-specific constants: grating wavelength ranges and default stage configurations.
"""

GRATING_LIMITS = {
    "prism": [0.54, 5.51, 0.01],
    "g140m": [0.55, 3.35, 0.00063],
    "g235m": [1.58, 5.3, 0.00106],
    "g395m": [2.68, 5.51, 0.00179],
    "g140h": [0.68, 1.9, 0.000238],
    "g235h": [1.66, 3.17, 0.000396],
    "g395h": [2.83, 5.24, 0.000666],
}

GRATINGS = [k.upper() for k in GRATING_LIMITS]

DEFAULT_STAGE1_CONFIG = {
    'overwrite': False,
    'do_clean_flicker_noise': True,
    'mask_science_regions': True,
    'cleanup_uncal': True,
    'cleanup_rateints': True,
    'subtract_background': True,
    'subtract_2d': False,
    'box_size': 8,
    'sigma_clip': True,
    'bkg_estimator': 'median',
    'plot': True,
}

DEFAULT_STAGE2_CONFIG = {
    'overwrite': False,
    'set_stellarity': 1.0,
    'rectify': True,
    'plot_bkgsub': False,
}

DEFAULT_STAGE3_CONFIG = {
    'overwrite': False,
    'method': 'nodded',
    'combine_method': '2d',  # '2d' = stack all dithers in 2D then extract; '1d' = extract per-group then combine in 1D
    'cleanup_asn': True,
    'cleanup_crfs': True,
    'plot_profiles': True,
    'plot_optext': True,
    # 1D combination params (used when combine_method='1d')
    'sigma_clip': True,
    'sigma_clip_low': 3.0,
    'sigma_clip_high': 3.0,
    'sigma_clip_maxiters': 5,
}
