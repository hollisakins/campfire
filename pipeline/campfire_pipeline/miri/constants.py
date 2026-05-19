"""
MIRI imaging filter list and detector geometry constants.

MIRI has a single 1024x1024 imager detector (MIRIMAGE, on the same 1032x1024
focal-plane array shared with LRS/coronagraph subarrays). All-sky imaging
filters span 5–28 μm.
"""

# MIRI imaging filters, ordered by central wavelength.
# F2550WR is a redundant filter-wheel slot for F2550W; both produce the same
# bandpass. Coronagraphic filters (F1065C / F1140C / F1550C / F2300C) are
# excluded — they're not imaging products and the pipeline path differs.
MIRI_IMAGING_FILTERS = (
    'f560w',
    'f770w',
    'f1000w',
    'f1130w',
    'f1280w',
    'f1500w',
    'f1800w',
    'f2100w',
    'f2550w',
    'f2550wr',
)

ALL_FILTERS = MIRI_IMAGING_FILTERS

# MIRIMAGE active imaging area. The full FPA is 1032x1024, but the left
# 256 columns are reserved for LRS slitless / coronagraph subarrays;
# imaging only uses the rightmost 1024x1024 region.
MIRI_DETECTOR_SHAPE = (1024, 1024)
