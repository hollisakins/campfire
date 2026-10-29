"""Per-pixel count of slitlet traces on a rate file, from the exposure WCS.

The counter every stage that reads "sky" off the detector needs: how many slitlet
traces fall on each pixel. It is the mask stage 1's ``mask_slits`` builds — every open
slitlet of the exposure's MSA configuration plus the failed-open shutters — kept as a
count instead of an OR, and extended redward past the nominal cutoff.

Two facts drive the shape of it (docs/design-nirspec-master-background.md §9):

* A counter built from processed canonicals only knows the sources stage 2a ran, and
  on the gratings the traces of every slitlet abut with no gap (38 % of a G395M
  detector lies under more than one footprint), so it has to come from the exposure
  WCS over every open slitlet.
* The ``assign_wcs`` bounding box stops at the filter's nominal wavelength range, but
  the detector keeps receiving the trace beyond it (G395M: ~0.015 DN/s at the nominal
  5.27 µm end, reaching the inter-trace level only ~200 px further). ``tail_px``
  extends every footprint by that much along +x so the tails count as covered.
"""
import hashlib
import os

import numpy as np

# Measured distance, in pixels, over which a trace carries on past the nominal red
# end of its bounding box before reaching the inter-trace level (design doc §9).
# Dispersers not listed get 0 (the prism's spectrum ends at the detector edge).
TAIL_PX = {
    'G395M': 200,
}


def tail_px_for(grating):
    """The redward footprint extension for a grating name (``'G395M'``, ``'PRISM'``…)."""
    return TAIL_PX.get(str(grating).upper(), 0)


def coverage_from_wcs(rate_path, cache_dir=None, tail_px=0):
    """Count of slitlet footprints per pixel of ``rate_path`` (int16, detector shape).

    Every open slitlet in the metafile the rate file points at, plus the failed-open
    shutters of the ``msaoper`` reference; fixed slits are skipped (always masked
    elsewhere). Each footprint is also counted over the ``tail_px`` pixels redward of
    its nominal end. Cached as ``coverage_<rate>_<hash>[_tail<N>].npy`` under
    ``cache_dir``, keyed on the rate file's absolute path.
    """
    tail_px = int(tail_px)
    key = hashlib.sha1(os.path.abspath(rate_path).encode()).hexdigest()[:12]
    tag = f'_tail{tail_px}' if tail_px else ''
    cache = (os.path.join(cache_dir, f'coverage_{os.path.basename(rate_path)[:-5]}_{key}{tag}.npy')
             if cache_dir else None)
    if cache and os.path.exists(cache):
        return np.load(cache)

    from jwst.datamodels import ImageModel
    from jwst.assign_wcs import AssignWcsStep
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from jwst.msaflagopen.msaflagopen_step import create_reference_filename_dictionary, MSAFlagOpenStep
    from jwst.msaflagopen.msaflag_open import create_slitlets
    from gwcs.wcs import WCS
    from campfire_pipeline.common.wcs import boundingbox_to_indices, wcs_to_dq

    prev = os.getcwd()
    os.chdir(os.path.dirname(os.path.abspath(rate_path)))
    try:
        with ImageModel(os.path.basename(rate_path)) as model:
            step = AssignWcsStep()
            step.input_dir = os.getcwd()
            processed = step.run(model)
            source_slits = processed.meta.wcs.get_transform('gwa', 'slit_frame').slits
            failed = create_slitlets(MSAFlagOpenStep().get_reference_file(processed, 'msaoper'))
            refnames = create_reference_filename_dictionary(processed)
            counter = np.zeros(model.data.shape, np.int16)
            for slits in (source_slits, failed):
                if not slits:
                    continue
                wcs = WCS(slitlets_wcs(processed, refnames, slits))
                meta = ImageModel()
                meta.meta.wcs = wcs
                meta.meta.wcsinfo = processed.meta.wcsinfo
                meta.meta.exposure.type = 'NRS_MSASPEC'
                meta.meta.wcs.bounding_box = generate_compound_bbox(meta, slits)
                for slit in slits:
                    if slit.name in ('S200A1', 'S200A2', 'S400A1', 'S1600A1', 'S200B1'):
                        continue
                    bbox = meta.meta.wcs.bounding_box[slit.name]
                    xmin, xmax, ymin, ymax = boundingbox_to_indices(model.data.shape, bbox)
                    yy, xx = np.mgrid[ymin:ymax, xmin:xmax]
                    ra, dec, lam, _ = meta.meta.wcs(xx, yy, slit.name)
                    foot = wcs_to_dq((ra, dec, lam), 1) > 0
                    counter[ymin:ymax, xmin:xmax] += foot
                    if tail_px:
                        counter[ymin:ymax, :] += _tail(foot, xmin, counter.shape[1], tail_px)
    finally:
        os.chdir(prev)
    if cache:
        os.makedirs(cache_dir, exist_ok=True)
        np.save(cache, counter)
    return counter


def _tail(foot, xmin, width, tail_px):
    """The footprint's redward tail: the union of ``foot`` shifted by 1..tail_px along
    +x, minus ``foot`` itself, as a full-width row block."""
    ny, nx = foot.shape
    ext = np.zeros((ny, nx + tail_px), bool)
    for dx in range(1, tail_px + 1):
        ext[:, dx:dx + nx] |= foot
    ext[:, :nx] &= ~foot
    block = np.zeros((ny, width), bool)
    xe = min(xmin + ext.shape[1], width)
    block[:, xmin:xe] = ext[:, :xe - xmin]
    return block
