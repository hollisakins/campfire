"""Triangle/asterism catalog matcher for the NIRCam ``align`` phase.

:class:`TriangleMatch` is a ``tweakwcs`` ``MatchCatalogs`` subclass that finds
source correspondences by matching triangle *shape* (side ratios) — invariant
to translation, rotation, and scale — so it recovers the match with **no prior
on the WCS offset**. That is exactly the regime where ``tweakwcs``'s default
``XYXYMatch`` (2-D histogram + nearest-neighbour) fails: once the WCS offset
exceeds the search radius, NN grabs the wrong neighbours and the histogram peak
lands near zero, so the fit "succeeds" with a spurious ~zero shift. Triangle
matching demands geometric consistency, so chance can't fake a match.

**Color-free.** Magnitude is dropped from the correspondence entirely; the
``mag_col`` is used *only* to cap each catalog to its brightest-N vertices
before triangle building (to bound the O(N^3) triangle count) — never as a
match constraint. This is what lets the align phase feed a reference catalog
whose magnitude zeropoint is inconsistent across build backends.

Wraps ``tristars.match.match_catalog_tri`` on its correspondence-only path
(``auto_keep=False``): the matcher returns matched index pairs; the
sigma-clipped transform fit is ``tweakwcs``'s job (``align_wcs`` / ``fit_wcs``),
not the matcher's.
"""

import numpy as np
from tweakwcs.matchutils import MatchCatalogs

from campfire_pipeline.common.io import log

_EMPTY = (np.array([], dtype=int), np.array([], dtype=int))


class TriangleMatch(MatchCatalogs):
    """Match two tangent-plane catalogs by triangle shape.

    Instances are passed as the ``match=`` callable to ``tweakwcs.align_wcs``.
    ``__call__`` follows the ``MatchCatalogs`` contract: it receives the
    reference and image catalogs (astropy ``Table``s carrying ``TPx``/``TPy``
    tangent-plane columns, in arcsec for ``JWSTWCSCorrector``) and returns a
    tuple ``(ref_idx, im_idx)`` — **reference indices first** — of matched rows.

    Parameters
    ----------
    brightest : int or None
        Cap each catalog to this many brightest vertices before building
        triangles (bounds the triangle count). ``None`` uses every source.
    mag_col : str
        Column used to rank brightness for the ``brightest`` cap (smaller =
        brighter, i.e. an AB magnitude). Used for vertex selection ONLY, never
        as a match constraint. If absent, the cap falls back to input order.
    size_limit : (float, float)
        Min/max triangle side length passed to ``match_catalog_tri``. In the
        JWST tangent plane these are **arcsec**; the default ``(5, 800)`` keeps
        within- and cross-detector triangles for a pooled exposure. Tune on
        real data during align validation.
    ignore_rot, ignore_scale, ba_max, max_keep :
        Passed straight through to ``tristars.match.match_catalog_tri``.
    """

    def __init__(self, *, brightest=150, mag_col='mag',
                 size_limit=(5.0, 800.0), ignore_rot=True, ignore_scale=True,
                 ba_max=0.9, max_keep=10):
        self.brightest = brightest
        self.mag_col = mag_col
        self.size_limit = [float(size_limit[0]), float(size_limit[1])]
        self.ignore_rot = ignore_rot
        self.ignore_scale = ignore_scale
        self.ba_max = ba_max
        self.max_keep = max_keep

    def _vertices(self, cat):
        """Return ``(xy [N, 2] float, orig_idx [N] int)`` — the brightest-N
        ``TPx``/``TPy`` vertices and their row indices into the ORIGINAL *cat*.

        Selecting a subset means the tristars indices are into the subset, so we
        carry ``orig_idx`` to map matched pairs back to the caller's rows.
        """
        for col in ('TPx', 'TPy'):
            if col not in cat.colnames:
                raise KeyError(
                    f"TriangleMatch: catalog is missing the '{col}' column "
                    f"(tangent-plane coordinates)."
                )
        n = len(cat)
        order = np.arange(n)
        if self.brightest is not None and n > self.brightest:
            if self.mag_col in cat.colnames:
                # smaller magnitude == brighter
                order = np.argsort(np.asarray(cat[self.mag_col], dtype=float),
                                   kind='stable')
            else:
                log(f"TriangleMatch: no '{self.mag_col}' column to rank "
                    f"brightness; capping to the first {self.brightest} of {n} "
                    f"sources in input order.")
            order = order[:self.brightest]
        tpx = np.asarray(cat['TPx'], dtype=float)[order]
        tpy = np.asarray(cat['TPy'], dtype=float)[order]
        return np.column_stack([tpx, tpy]), order.astype(int)

    def __call__(self, refcat, imcat, tp_pscale=1.0, tp_units=None, **kwargs):
        # tp_pscale / tp_units are part of the MatchCatalogs contract but unused
        # here: triangle matching is scale-invariant and works directly in the
        # tangent-plane units the correctors supply (arcsec for JWSTWCSCorrector).
        ref_xy, ref_orig = self._vertices(refcat)
        im_xy, im_orig = self._vertices(imcat)

        # A starved exposure must yield zero matches so the solve phase falls to
        # the NOT_ALIGNED / identity sentinel — never crash the align worker.
        if len(ref_xy) < 3 or len(im_xy) < 3:
            return _EMPTY

        from tristars.match import match_catalog_tri
        pair_ix = match_catalog_tri(
            ref_xy, im_xy, auto_keep=False, maxKeep=self.max_keep,
            size_limit=self.size_limit, ignore_rot=self.ignore_rot,
            ignore_scale=self.ignore_scale, ba_max=self.ba_max,
        )
        pair_ix = np.asarray(pair_ix)
        if pair_ix.size == 0:
            return _EMPTY

        # match_catalog_tri returns [M, 2]: column 0 indexes V1 (=refcat),
        # column 1 indexes V2 (=imcat). Remap subset -> original rows and return
        # reference-first, per the MatchCatalogs contract.
        ref_idx = ref_orig[pair_ix[:, 0]]
        im_idx = im_orig[pair_ix[:, 1]]
        return ref_idx, im_idx
