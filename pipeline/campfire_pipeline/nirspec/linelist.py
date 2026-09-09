"""Emission-line catalog for the NIRSpec line fitter (``cfpipe nirspec linefit``).

One declarative table — the single list of what ``linefit`` measures and what
``spectrum_line_fits.lines`` is keyed by. Rest wavelengths are **vacuum
Angstroms** (the web overlay in ``web/components/spectra/plotting-utils.ts``
carries the same values in microns; the redshift-fitting templates in
``templates.py`` are a separate, air→vac converted basis and are not changed
here).

Every entry is a :class:`Line`:

``name``
    Catalog key (``Halpha``, ``OIII5007``). Stable identifiers — column and
    JSON keys downstream are derived from them, so renaming one is a schema
    change.
``label``
    Display label (``Hα``, ``[OIII]λ5007``).
``wave``
    Rest vacuum wavelength in Angstroms.
``tie``
    ``(primary_name, ratio)`` when atomic physics fixes this line's flux to
    ``ratio × F(primary)`` (the [OIII], [NII], [OI] doublets). The tie is
    honored only when both lines land in the same fitted complex; a tied line
    whose primary is uncovered is fit free.
``broad``
    True for permitted lines that may carry an additional broad (AGN)
    component when ``[nirspec.line_fitting].fit_broad`` is on.
``weight``
    Rough typical strength relative to Hβ in a star-forming galaxy. Used only
    to decide which member of an unresolved blend carries the blended flux
    (the heavier line is the blend primary); never used as a prior.

Adding a line = adding a row here (and a rest-wavelength entry in the web
overlay if it should be drawn).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Line:
    name: str
    label: str
    wave: float                     # rest vacuum Angstrom
    tie: Optional[tuple[str, float]] = None
    broad: bool = False
    weight: float = 0.1


# Fixed doublet ratios (flux ratio secondary / primary). [OIII] and [NII] are
# the values the zfit templates carry (templates.py) so the two fitters agree;
# [OI] follows Storey & Zeippen (2000).
_R_OIII = 1.0 / 2.98
_R_NII = 1.0 / 2.94
_R_OI = 1.0 / 3.03

LINES: tuple[Line, ...] = (
    # --- rest-UV -------------------------------------------------------------
    Line('Lya',      'Lyα',          1215.670, weight=10),
    Line('NV1239',   'NVλ1239',      1238.821, weight=0.05),
    Line('NV1243',   'NVλ1243',      1242.804, weight=0.03),
    Line('NIV1486',  'NIV]λ1486',    1486.496, weight=0.1),
    Line('CIV1548',  'CIVλ1548',     1548.187, broad=True, weight=0.3),
    Line('CIV1551',  'CIVλ1551',     1550.772, weight=0.15),
    Line('HeII1640', 'HeIIλ1640',    1640.420, broad=True, weight=0.1),
    Line('OIII1661', 'OIII]λ1661',   1660.809, weight=0.1),
    Line('OIII1666', 'OIII]λ1666',   1666.150, weight=0.2),
    Line('NIII1750', 'NIII]λ1750',   1749.670, weight=0.05),
    Line('CIII1907', '[CIII]λ1907',  1906.683, weight=0.3),
    Line('CIII1909', 'CIII]λ1909',   1908.734, weight=0.2),
    Line('MgII2796', 'MgIIλ2796',    2796.352, broad=True, weight=0.3),
    Line('MgII2803', 'MgIIλ2803',    2803.531, weight=0.2),
    # --- rest-optical --------------------------------------------------------
    Line('NeV3426',  '[NeV]λ3426',   3426.864, weight=0.05),
    Line('OII3726',  '[OII]λ3726',   3727.092, weight=1.0),
    Line('OII3729',  '[OII]λ3729',   3729.875, weight=0.9),
    Line('NeIII3869', '[NeIII]λ3869', 3869.860, weight=0.35),
    Line('NeIII3968', '[NeIII]λ3968', 3968.590, weight=0.1),
    Line('Heps',     'Hε',           3971.198, weight=0.16),
    Line('Hdelta',   'Hδ',           4102.892, weight=0.26),
    Line('Hgamma',   'Hγ',           4341.692, broad=True, weight=0.47),
    Line('OIII4363', '[OIII]λ4363',  4364.436, weight=0.1),
    Line('HeII4686', 'HeIIλ4686',    4687.021, weight=0.05),
    Line('Hbeta',    'Hβ',           4862.692, broad=True, weight=1.0),
    Line('OIII4959', '[OIII]λ4959',  4960.295, tie=('OIII5007', _R_OIII), weight=1.3),
    Line('OIII5007', '[OIII]λ5007',  5008.240, weight=4.0),
    Line('HeI5876',  'HeIλ5876',     5877.249, weight=0.1),
    Line('OI6300',   '[OI]λ6300',    6302.046, weight=0.05),
    Line('OI6363',   '[OI]λ6363',    6365.535, tie=('OI6300', _R_OI), weight=0.02),
    Line('NII6548',  '[NII]λ6548',   6549.860, tie=('NII6583', _R_NII), weight=0.1),
    Line('Halpha',   'Hα',           6564.610, broad=True, weight=2.86),
    Line('NII6583',  '[NII]λ6583',   6585.270, weight=0.3),
    Line('SII6716',  '[SII]λ6716',   6718.290, weight=0.2),
    Line('SII6731',  '[SII]λ6731',   6732.670, weight=0.15),
    Line('ArIII7136', '[ArIII]λ7136', 7137.770, weight=0.05),
    Line('SIII9069', '[SIII]λ9069',  9071.100, weight=0.2),
    Line('SIII9531', '[SIII]λ9531',  9533.200, weight=0.5),
    # --- rest-NIR ------------------------------------------------------------
    Line('Padelta',  'Paδ',         10052.128, weight=0.05),
    Line('HeI10830', 'HeIλ10830',   10833.306, weight=0.3),
    Line('Pagamma',  'Paγ',         10941.090, weight=0.1),
    Line('Pabeta',   'Paβ',         12821.590, broad=True, weight=0.17),
    Line('Paalpha',  'Paα',         18756.130, broad=True, weight=0.33),
)

LINES_BY_NAME: dict[str, Line] = {line.name: line for line in LINES}


def get_line(name: str) -> Line:
    try:
        return LINES_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown emission line '{name}' (known: {sorted(LINES_BY_NAME)})")
