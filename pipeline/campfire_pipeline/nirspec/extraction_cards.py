"""The extraction's header cards: every keyword the profile extraction writes into a
``_spec.fits`` (primary header; the master-background spectrum's in ``SPEC1D_MBKG``),
with its description.

This is the one list: the writers take each card's description from here (``put``), stage 3
copies exactly these cards from the intermediate into the product (``copy``), and the
docs' FITS reference lists them. A card whose value does not apply (a non-finite number,
an empty string) is left out rather than written as a placeholder, so readers use
``header.get``. Descriptions are short enough that the comment survives next to the value.
"""
import numpy as np

CARDS = {
    # versions and calibration
    'EXTRVER': 'Extraction model version',
    'OPTICVER': 'MSA optics model version',
    'FLUXWID': 'Reported flux: width across dispersion [arcsec]',
    'SLOSSY': 'Path loss along the slit: optics/jwst',
    'SLOSSX': 'Path loss across shutter: corrected/jwst',
    'BAROFF': 'Bar offset applied [slit units]',
    # the profile adopted
    'SHAPE': 'Profile: own/prism/grating/default',
    'SHAPEWHY': 'Reason for the profile',
    'SHAPESRC': 'Product the profile came from',
    'PA_V3': 'PA_V3 of the science exposures [deg]',
    'DETSNR': 'Detection S/N of the source',
    # the fitted source model
    'SRCCEN': 'Center at SRCPIVOT [slit units]',
    'SRCCENPL': 'Planned center [slit units]',
    'SRCDRIFT': 'Center change per octave [slit units]',
    'SRCWIDTH': 'Gaussian sigma at SRCPIVOT [slit units]',
    'SRCDWID': 'Sigma change per octave [slit units]',
    'SRCPIVOT': 'Pivot wavelength of the profile [um]',
    'SRCCHROM': 'Profile varies with wavelength',
    'SRCCHI2': 'Reduced chi2 of the source model',
    # other sources' light
    'NCONTAM': 'Other sources predicted on the trace',
    'CONTBOX': 'Other sources predicted from a 3-px boxcar',
    'CONTFRAC': 'Predicted contamination / fnu_prof',
    'CONTSIG': 'Predicted contamination / noise',
    'CONTNSEP': 'Fraction of fnu_prof not separable',
    'SELFSUB': 'Own flux removed by nod subtraction',
    'NFOPEN': 'Failed-open shutters on the trace',
    'MBKGSCL': 'Master background level fitted (0.01 = 1%)',
    'MBKGSCLE': 'Uncertainty of MBKGSCL',
    'MBKGSCLC': 'Scale fitted, over the cap: MBKGSCL set 0',
    'MBKGWLIM': 'Master-bkg fit width at its limit',
    # same-slitlet neighbor scan
    'NBRDET': 'Neighbor above the proposal threshold',
    'NBRDELTA': 'Neighbor offset from target [slit units]',
    'NBRPROP': 'Proposed overrides (root:nod>nods)',
    'NBRXDISP': 'Other disperser: single/agree/disagree/none',
}


def applies(value):
    """False for a value that does not apply: None, a non-finite number, an empty string."""
    if value is None:
        return False
    if isinstance(value, str):
        return value != ''
    if isinstance(value, (bool, np.bool_)):
        return True
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return True


def put(header, name, value):
    """Set card ``name`` (a key of ``CARDS``) with its description, or remove it when the
    value does not apply."""
    if name not in CARDS:
        raise KeyError(f'{name} is not an extraction card (extraction_cards.CARDS)')
    if not applies(value):
        header.remove(name, ignore_missing=True)
        return
    if isinstance(value, (np.bool_,)):
        value = bool(value)
    elif isinstance(value, (np.integer,)):
        value = int(value)
    elif isinstance(value, (np.floating,)):
        value = float(value)
    header[name] = (value, CARDS[name])


def copy(src, dst):
    """Copy every extraction card present in header ``src`` into ``dst``."""
    for name in CARDS:
        if name in src:
            dst[name] = (src[name], CARDS[name])
