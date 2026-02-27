"""
NIRCam constants: filter lists, detector geometry, and default stage configs.

Ported from nircamx ref.py and example_config.toml.
"""

# ---------------------------------------------------------------------------
# Filter definitions
# ---------------------------------------------------------------------------

SW_FILTERS = [
    'f070w', 'f090w', 'f115w', 'f140m', 'f150w', 'f162m', 'f164n',
    'f150w2', 'f182m', 'f187n', 'f200w', 'f210m', 'f212n',
]

LW_FILTERS = [
    'f250m', 'f277w', 'f300m', 'f322w2', 'f323n', 'f335m', 'f356w',
    'f360m', 'f405n', 'f410m', 'f430m', 'f444w', 'f460m', 'f466n',
    'f470n', 'f480m',
]

ALL_FILTERS = SW_FILTERS + LW_FILTERS

# ---------------------------------------------------------------------------
# Detector reference sections (from jwst/refpix/reference_pixels.py)
# Zero-indexed slices: (rowstart, rowstop, colstart, colstop)
# ---------------------------------------------------------------------------

NIR_REFERENCE_SECTIONS = {
    'A': {
        'top': (2044, 2048, 0, 512),
        'bottom': (0, 4, 0, 512),
        'side': (0, 2048, 0, 4),
        'data': (0, 2048, 0, 512),
    },
    'B': {
        'top': (2044, 2048, 512, 1024),
        'bottom': (0, 4, 512, 1024),
        'data': (0, 2048, 512, 1024),
    },
    'C': {
        'top': (2044, 2048, 1024, 1536),
        'bottom': (0, 4, 1024, 1536),
        'data': (0, 2048, 1024, 1536),
    },
    'D': {
        'top': (2044, 2048, 1536, 2048),
        'bottom': (0, 4, 1536, 2048),
        'side': (0, 2048, 2044, 2048),
        'data': (0, 2048, 1536, 2048),
    },
}

# Data regions excluding reference rows/columns
NIR_AMPS = {
    'A': {'data': (4, 2044, 4, 512)},
    'B': {'data': (4, 2044, 512, 1024)},
    'C': {'data': (4, 2044, 1024, 1536)},
    'D': {'data': (4, 2044, 1536, 2044)},
}

