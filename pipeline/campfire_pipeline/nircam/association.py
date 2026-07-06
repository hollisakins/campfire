"""Group NIRCam canonical exposure files into per-exposure ``ExposureGroup``s.

This is the association / enumeration layer for the field-level astrometric
``align`` phase. One :class:`ExposureGroup` is one *physical* NIRCam exposure
(one dither): every detector file that shares the exposure token
``rootname.rsplit('_', 1)[0]`` (= filename ``parts[0]_parts[1]_parts[2]``,
dropping the ``_<detector>`` suffix).

A single NIRCam exposure images simultaneously through a short-wavelength (SW)
filter — modules A1-A4, B1-B4 — and a long-wavelength (LW) filter —
NRCALONG/NRCBLONG. One canonical FITS is written per detector; the SW files
land in the SW filter's directory and the LW files in the LW filter's
directory, but *all ten share one exposure token* (see ``list_products`` in
``common/query.py``). So grouping pools **across all of the field's filters**
and reunites the SW + LW halves of each dither. Groups of 10 / 5 / 2 / 1
members are all normal (both modules / single module / LW-only / subarray); a
one-member group is valid and later phases fall back to a per-detector solve
for it. Detectors are **never** grouped across dithers — a different exposure
number yields a different token.

**Imaging only.** Grism/WFSS never reaches the canonical tree (dropped at
download and at ``orchestrate._filter_imaging_uncals`` / ``steps/image2``), so
this module needs no ``EXP_TYPE`` read and performs none — it groups purely
from filenames and parent directories and never opens a FITS.

Deliberately import-light (stdlib + ``common.io.log`` only): this layer will
sit on the align orchestrator's hot path, so it does not import the step
modules (``steps/bad_pixel`` etc.) that would drag in numpy / jwst / CRDS. The
two trivial detector classifiers are re-implemented locally — an existing
convention in this codebase (cf. ``steps/bad_pixel._detector_of``,
``orchestrate._detector_sorted``, ``common/query._is_lw_detector``).
"""

import os
from dataclasses import dataclass
from typing import List, Tuple

from campfire_pipeline.common.io import log


# Canonical detector order (module-major, each module's LW after its SW).
# Mirrors steps/bad_pixel.py SW_DETECTORS/LW_DETECTORS; re-declared here to keep
# this module free of the step-module import chain.
_CANONICAL_DETECTORS = (
    'nrca1', 'nrca2', 'nrca3', 'nrca4', 'nrcalong',
    'nrcb1', 'nrcb2', 'nrcb3', 'nrcb4', 'nrcblong',
)


def exposure_key(path) -> str:
    """Exposure token for *path*: the rootname minus its ``_<detector>`` suffix.

    ``'.../jw01727028001_04101_00003_nrcalong.fits'`` ->
    ``'jw01727028001_04101_00003'``. One token identifies one physical exposure
    (one dither) across every detector and both filter channels.

    Also accepts raw ``_uncal.fits`` names (``'..._nrcalong_uncal.fits'``) — the
    trailing ``_uncal`` is stripped before the detector, so an uncal and its
    canonical map to the same token. Canonical rootnames never carry ``_uncal``,
    so this widening is a no-op for them (used by the ``--tiles`` exposure gate,
    which runs at ``detector1`` on uncals).
    """
    base = os.path.basename(path).removesuffix('.fits').removesuffix('_uncal')
    return base.rsplit('_', 1)[0]


def detector_of(path) -> str:
    """Detector token (the last ``_``-delimited field) of *path*.

    Complement of :func:`exposure_key`. Mirrors ``steps/bad_pixel._detector_of``.
    """
    base = os.path.basename(path).removesuffix('.fits')
    return base.rsplit('_', 1)[-1]


def channel_of(detector) -> str:
    """``'lw'`` for a long-wavelength detector, else ``'sw'``.

    LW iff the detector token ends in ``'long'`` (``nrcalong``/``nrcblong``) or
    ``'5'`` (the ``nrca5``/``nrcb5`` aliases). Source of truth:
    ``common/query._is_lw_detector``.
    """
    d = (detector or '').lower()
    return 'lw' if d.endswith('long') or d.endswith('5') else 'sw'


def module_of(detector) -> str:
    """NIRCam module ``'a'`` or ``'b'`` from a detector token (``''`` if unknown).

    The module is the character after the ``nrc`` prefix: ``nrca1`` -> ``'a'``,
    ``nrcblong`` -> ``'b'``.
    """
    d = (detector or '').lower()
    if d.startswith('nrc') and len(d) > 3:
        return d[3]
    return ''


@dataclass(frozen=True)
class ExposureMember:
    """One detector's canonical FITS within an exposure group."""

    path: str          # absolute canonical (or working-copy) path
    filter_name: str   # the filter DIRECTORY the file was found in
    rootname: str      # basename without '.fits' (full, incl. detector)
    detector: str      # e.g. 'nrca1' … 'nrcblong'

    @property
    def channel(self) -> str:
        return channel_of(self.detector)

    @property
    def module(self) -> str:
        return module_of(self.detector)

    @property
    def is_sw(self) -> bool:
        return self.channel == 'sw'

    @property
    def is_lw(self) -> bool:
        return self.channel == 'lw'


@dataclass(frozen=True)
class ExposureGroup:
    """All detector files of one physical NIRCam exposure (one dither).

    Purely structural — it describes what was found on disk. Solve-time policy
    (e.g. falling back to a per-detector solve for a one-member group) lives in
    the align solve phase, which reads :attr:`is_single_member`.
    """

    key: str
    members: Tuple[ExposureMember, ...]

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(m.path for m in self.members)

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def sw_members(self) -> Tuple[ExposureMember, ...]:
        return tuple(m for m in self.members if m.is_sw)

    @property
    def lw_members(self) -> Tuple[ExposureMember, ...]:
        return tuple(m for m in self.members if m.is_lw)

    @property
    def filters(self) -> frozenset:
        return frozenset(m.filter_name for m in self.members)

    @property
    def modules(self) -> frozenset:
        return frozenset(m.module for m in self.members if m.module)

    @property
    def detectors(self) -> Tuple[str, ...]:
        return tuple(m.detector for m in self.members)

    @property
    def is_single_member(self) -> bool:
        return self.n_members == 1


def _member_sort_key(member):
    try:
        idx = _CANONICAL_DETECTORS.index(member.detector)
    except ValueError:
        idx = len(_CANONICAL_DETECTORS)  # unknown detector sorts last
    return (idx, member.detector, member.filter_name, member.path)


def _make_member(field, path, filter_name) -> ExposureMember:
    """Build one member, emitting advisory (never fatal) warnings on anomalies."""
    rootname = os.path.basename(path).removesuffix('.fits')
    detector = detector_of(path)

    if detector not in _CANONICAL_DETECTORS:
        # Never drop science over an unrecognized token — keep it (it sorts
        # last) and flag it. Mirrors the non-fatal stance of
        # Field._load_excluded_exposures.
        log(f"association: unexpected detector token '{detector}' in "
            f"{os.path.basename(path)}; keeping it (grouped, sorted last).")
    else:
        det_channel = channel_of(detector)
        if field.is_sw_filter(filter_name):
            filt_channel = 'sw'
        elif field.is_lw_filter(filter_name):
            filt_channel = 'lw'
        else:
            filt_channel = None
        if filt_channel is not None and det_channel != filt_channel:
            log(f"association: {det_channel.upper()} detector '{detector}' "
                f"found in {filt_channel.upper()} filter dir '{filter_name}' "
                f"({os.path.basename(path)}); keeping it.")

    # WFSS seam: a future mode-aware path would read EXP_TYPE here to keep grism
    # out of imaging groups. The canonical tree is imaging-only (grism is gated
    # upstream), so Phase 2 performs no mode read.
    return ExposureMember(path=path, filter_name=filter_name,
                          rootname=rootname, detector=detector)


def build_exposure_groups(field, filters=None, *, skip=None, with_step=None,
                          status=None, work=False,
                          tiles=None) -> List[ExposureGroup]:
    """Group a field's canonical exposure files by physical exposure.

    Scans every filter in *filters* (default ``field.filters``) via
    :meth:`Field.get_exposure_files` — so per-filter effective-skip
    (``field.skip`` + reviewer ``excluded_exposures[filter]`` + caller *skip*)
    is applied for free — and buckets the results by :func:`exposure_key`,
    reuniting the SW and LW detectors of each dither.

    *skip*, *with_step*, *status*, *work*, and *tiles* are passed through
    verbatim to :meth:`Field.get_exposure_files`. *tiles* restricts the scan to
    exposures overlapping the named tile(s); the enumerator's exposure-union
    gate keeps a dither's full detector complement whenever any member overlaps,
    so pooled solves stay complete at tile edges. Requires
    ``field.setup_workspace()`` to have been called; otherwise
    ``Field.filter_dir`` raises ``RuntimeError``, which propagates.

    Returns a list of :class:`ExposureGroup`, sorted by ``key``, each with its
    members in canonical detector order.
    """
    filters = list(field.filters if filters is None else filters)

    buckets = {}
    for filter_name in filters:
        for path in field.get_exposure_files(filter_name, skip=skip,
                                             with_step=with_step,
                                             status=status, work=work,
                                             tiles=tiles):
            member = _make_member(field, path, filter_name)
            buckets.setdefault(exposure_key(path), []).append(member)

    groups = [
        ExposureGroup(key=key,
                      members=tuple(sorted(members, key=_member_sort_key)))
        for key, members in buckets.items()
    ]
    return sorted(groups, key=lambda g: g.key)
