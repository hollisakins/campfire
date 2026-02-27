"""
NIRSpec-specific constants: grating wavelength ranges.
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
