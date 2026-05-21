"""CRDS reference file pre-fetching for imaging-arm pipelines.

Multiple workers downloading the same CRDS file simultaneously can leave one
reading a partially-written file. Running CRDS lookups serially — one query
per unique detector / filter / pupil combination — before ``dispatch()``
ensures the cache is fully populated by the time parallel workers start.

Both NIRCam and MIRI imaging use the stock ``Detector1Pipeline`` +
``Image2Pipeline`` JWST pipelines, so this module is instrument-agnostic — the
``Detector1Pipeline.reference_file_types`` aggregation covers MIRI's
detector1 reftypes (firstframe, lastframe, reset, rscd, emicorr) the same
way it covers NIRCam's.

Two correctness anchors keep this in lockstep with the pipeline so we don't
download references the run will never use:

1. The reftype lists come from ``Detector1Pipeline.reference_file_types`` and
   ``Image2Pipeline.reference_file_types`` — the same aggregation jwst itself
   uses, so we cover everything (incl. ``persat``/``trapdensity``/``trappars``)
   and nothing extra.
2. The CRDS lookup parameters come from ``model.get_crds_parameters()`` on the
   uncal datamodel — the same call ``Step.get_reference_file()`` makes — so
   the rmap match resolves to byte-identical files.
"""

import os

from astropy.io import fits

from campfire_pipeline.common.io import log


_REFTYPES_CACHE = {}


def _reftypes(pipeline_name):
    """Return the deduplicated ``reference_file_types`` for a JWST pipeline."""
    if pipeline_name in _REFTYPES_CACHE:
        return _REFTYPES_CACHE[pipeline_name]
    from jwst import pipeline as jwst_pipeline
    cls = getattr(jwst_pipeline, pipeline_name)
    # ``reference_file_types`` is an instance property aggregated from substeps
    # (with duplicates when steps share refs); dedup while preserving order.
    seen = set()
    out = []
    for r in cls().reference_file_types:
        if r not in seen:
            seen.add(r)
            out.append(r)
    _REFTYPES_CACHE[pipeline_name] = out
    return out


def _crds_params(uncal_file):
    """Build the canonical CRDS lookup parameters dict from an uncal.

    Goes through the JWST datamodel layer so the rmap match resolves to the
    same files ``Step.get_reference_file()`` will pick at run-time. Building
    the dict by hand from ``fits.getheader`` risks drift on derived/normalized
    keys (e.g. ``CHANNEL`` for NIRCam) and silently downloads the wrong
    references.
    """
    from stdatamodels.jwst import datamodels
    with datamodels.open(uncal_file) as model:
        return dict(model.get_crds_parameters())


def _fetch(params, reftypes, key_label):
    """Run one ``crds.getreferences()`` call; non-fatal on failure."""
    import crds
    try:
        refs = crds.getreferences(params, reftypes=reftypes, observatory='jwst')
        cached = [
            k for k, v in refs.items()
            if v and 'N/A' not in v.upper() and 'NOT FOUND' not in v.upper()
        ]
        log(f"  Cached {len(cached)}/{len(reftypes)} references: "
            f"{', '.join(sorted(cached))}")
    except Exception as e:
        log(f"  CRDS prefetch warning for {key_label}: {e}")


def prefetch_detector1_references(uncal_files):
    """Pre-cache Detector1Pipeline CRDS references; dedup on
    ``(DETECTOR, READPATT, SUBARRAY)`` (read cheaply from the FITS header)."""
    reftypes = _reftypes('Detector1Pipeline')
    seen = set()
    for f in uncal_files:
        hdr = fits.getheader(f)
        key = (hdr.get('DETECTOR'), hdr.get('READPATT'), hdr.get('SUBARRAY'))
        if key in seen:
            continue
        seen.add(key)
        log(f"Pre-fetching Detector1Pipeline CRDS references for "
            f"{key[0]} ({key[1]}/{key[2]}) using {os.path.basename(f)}")
        _fetch(_crds_params(f), reftypes,
               key_label='/'.join(map(str, key)))
    log("Detector1Pipeline CRDS reference pre-fetch complete")


def prefetch_image2_references(uncal_files):
    """Pre-cache Image2Pipeline (+ wisp/striping flat) CRDS references; dedup
    on ``(DETECTOR, FILTER, PUPIL)`` (read cheaply from the FITS header)."""
    reftypes = _reftypes('Image2Pipeline')
    seen = set()
    for f in uncal_files:
        hdr = fits.getheader(f)
        key = (hdr.get('DETECTOR'), hdr.get('FILTER'), hdr.get('PUPIL'))
        if key in seen:
            continue
        seen.add(key)
        log(f"Pre-fetching Image2Pipeline CRDS references for "
            f"{key[0]}/{key[1]}/{key[2]} using {os.path.basename(f)}")
        _fetch(_crds_params(f), reftypes,
               key_label='/'.join(map(str, key)))
    log("Image2Pipeline CRDS reference pre-fetch complete")


def prefetch_process_references(field, filters, n_processes):
    """Gather uncals across ``filters`` and pre-fetch detector1 + image2
    CRDS references. No-op when ``n_processes <= 1``."""
    if n_processes <= 1:
        return
    uncals = []
    for filt in filters:
        try:
            uncals.extend(field.get_uncal_files(filt))
        except RuntimeError:
            continue
    if not uncals:
        return
    prefetch_detector1_references(uncals)
    prefetch_image2_references(uncals)
