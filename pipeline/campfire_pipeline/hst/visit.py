"""Visits: the unit the per-visit steps of the HST process phase work on.

A visit here is the set of exposures of one detector and filter taken in one
HST visit. Cosmic-ray rejection and alignment both work on that set: its
exposures share guide stars, so they overlap to a few milliarcseconds before
any correction, and they were taken minutes apart.

The visit is read from the exposure's rootname. HST rootnames have the form
``ipppssoot``: ``i`` the instrument (``j`` ACS, ``i`` WFC3), ``ppp`` the
program, ``ss`` the visit ("observation set"), ``oo`` the exposure and ``t``
the transmission source (HST Data Handbooks, "HST File Names"). The first six
characters therefore name one visit of one program on one instrument.

``process_visit`` runs the per-visit chain on calibrated exposures in the order
``wcs_init`` -> ``sky`` -> ``crreject`` -> ``align``. Every step is idempotent
through its step stamp, so the chain can be re-run after an interruption.
"""

import os
from dataclasses import dataclass

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.hst.instruments import detector_for, filter_name


@dataclass(frozen=True)
class Visit:
    """Exposures of one visit, detector and filter."""

    visit: str          # first six characters of the rootname, e.g. 'je2860'
    instrument: str     # detector token, 'acswfc' or 'wfc3ir'
    filter: str         # lower case, e.g. 'f606w'
    files: tuple        # exposure paths, sorted

    @property
    def key(self):
        return f'{self.visit}-{self.instrument}-{self.filter}'


def visit_id(rootname):
    """Visit part of an HST rootname (``ipppss`` of ``ipppssoot``)."""
    root = str(rootname).strip().lower()
    if len(root) != 9:
        raise ValueError(f'not an HST rootname (expected 9 characters): {rootname!r}')
    return root[:6]


def group_visits(files):
    """Group exposure files (RAW, FLC or FLT) into :class:`Visit` objects.

    Sorted by visit, detector and filter. A file from an unsupported detector
    raises ``ValueError``; excluding those is the job of the download selection.
    """
    groups = {}
    for path in files:
        header = fits.getheader(path, 0)
        det = detector_for(header.get('INSTRUME'), header.get('DETECTOR'))
        root = header.get('ROOTNAME') or os.path.basename(path).split('_')[0]
        key = (visit_id(root), det.token, filter_name(header))
        groups.setdefault(key, []).append(path)
    return [Visit(v, token, filt, tuple(sorted(paths)))
            for (v, token, filt), paths in sorted(groups.items())]


def process_visit(visit, refcat=None, *, reference_env=None, config=None,
                  refcat_hash=None, overwrite=False):
    """Run ``wcs_init``, ``sky``, ``crreject`` and ``align`` on one visit.

    ``config`` holds one optional table per step (``sky``, ``crreject``,
    ``align``). ``reference_env`` is the ``{'jref': dir}`` / ``{'iref': dir}``
    mapping ``wcs_init`` needs. With ``refcat=None`` the visit is not aligned.

    Returns ``{'key', 'cosmic_rays', 'align', 'ready'}``. ``cosmic_rays`` is
    ``'visit'`` when the exposures were cleaned against each other, ``'ramp'``
    for a single WFC3/IR exposure (the up-the-ramp fit has removed most hits)
    and ``'single'`` for a single ACS exposure, which is cleaned from its own
    pixels (``crreject_single_step``; less complete than a visit, and refined
    by the field-level outlier pass where the exposure overlaps others).
    ``ready`` is false for a visit that failed to align.
    """
    from campfire_pipeline.hst.steps.align import align_visit
    from campfire_pipeline.hst.steps.crreject import crreject_step
    from campfire_pipeline.hst.steps.sky import sky_step
    from campfire_pipeline.hst.steps.wcs_init import wcs_init_step

    cfg = config or {}
    for f in visit.files:
        wcs_init_step(f, reference_env=reference_env, overwrite=overwrite)
        sky_step(f, step_config=cfg.get('sky'), overwrite=overwrite)

    ready = True
    if len(visit.files) >= 2:
        crreject_step(visit.files, step_config=cfg.get('crreject'), overwrite=overwrite)
        cosmic_rays = 'visit'
    elif visit.instrument == 'wfc3ir':
        cosmic_rays = 'ramp'
    else:
        from campfire_pipeline.hst.steps.crreject import crreject_single_step
        crreject_single_step(visit.files[0], step_config=cfg.get('crreject'),
                             overwrite=overwrite)
        cosmic_rays = 'single'
        log(f'visit {visit.key}: a single {visit.instrument} exposure, cleaned '
            f'from its own pixels')

    status = None
    if refcat is not None:
        sol = align_visit(visit.files, refcat, key=visit.key,
                          config=cfg.get('align'), refcat_hash=refcat_hash,
                          overwrite=overwrite)
        status = 'SOLVED' if sol is None else sol.status   # None: already aligned
        ready = ready and status == 'SOLVED'
    return {'key': visit.key, 'cosmic_rays': cosmic_rays, 'align': status,
            'ready': ready}
