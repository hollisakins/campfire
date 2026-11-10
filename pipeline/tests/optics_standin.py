"""A small analytic stand-in for the diffraction model (``nirspec.optics.Optics``): the same
``image`` and ``transmission`` interface, closed form, so the profile-fit tests run through the
model's code path without building its tables."""

import numpy as np
from scipy.special import erf

from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH


class Analytic:
    """A Gaussian PSF of SIGMA (slit units) cut by the open shutters (hard edges, no diffraction)."""
    SIGMA = 0.12

    def _s(self, width):
        return np.sqrt(self.SIGMA ** 2 + np.asarray(width, float) ** 2)

    def image(self, mask, y, lam, centre, width):
        y, lam, centre, width = np.broadcast_arrays(y, lam, centre, width)
        s = self._s(width)
        g = np.exp(-0.5 * ((y - centre) / s) ** 2) / (np.sqrt(2 * np.pi) * s)
        inside = np.zeros(y.shape, bool)
        for k in mask:
            inside |= np.abs(y - k * MSA_SLIT_PITCH) < PF.OPEN_HALF
        return g * inside * O.DY_ROW

    def without_msa(self):
        """The same PSF with nothing cut: every opening as wide as the pitch."""
        return NoMSA()

    def x_correction(self, lam, srcx):
        return np.ones(np.shape(lam))

    def x_extended_ratio(self, lam, srcx, width):
        return np.ones(np.shape(lam))

    def transmission(self, mask, lam, centre, width):
        lam, centre, width = np.broadcast_arrays(lam, centre, width)
        s = self._s(width) * np.sqrt(2)
        return sum(0.5 * (erf((k * MSA_SLIT_PITCH + PF.OPEN_HALF - centre) / s)
                          - erf((k * MSA_SLIT_PITCH - PF.OPEN_HALF - centre) / s)) for k in mask)


class NoMSA(Analytic):
    def image(self, mask, y, lam, centre, width):
        y, lam, centre, width = np.broadcast_arrays(y, lam, centre, width)
        s = self._s(width)
        return np.exp(-0.5 * ((y - centre) / s) ** 2) / (np.sqrt(2 * np.pi) * s) * O.DY_ROW


STANDIN = Analytic()
