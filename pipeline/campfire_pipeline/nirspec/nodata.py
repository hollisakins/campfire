"""``<prod>_nodata`` markers and what they imply for products downstream.

Stage 2a writes ``<prod>_nodata`` (one line: the reason) in place of the
canonical ``<prod>.fits`` when an exposure x source yields no data. Every
reader keys on the marker's *existence* only; the reason is for humans:

- ``NoDataOnDetectorError``: jwst found nothing on the detector.
- ``AllShuttersStuck``: every shutter of the source is listed as stuck closed,
  so nothing is left to extract.

Kept free of jwst imports so it is unit-testable without the JWST stack.
"""

import glob
import os
import re

from astropy.io import fits

from campfire_pipeline.common.io import log

NODATA_SUFFIX = '_nodata'
REASON_NO_DATA_ON_DETECTOR = 'NoDataOnDetectorError'
REASON_ALL_SHUTTERS_STUCK = 'AllShuttersStuck'

# Stage-2a outputs of one product besides the canonical: Spec2's un-promoted
# cal, the legacy pre-#212 quartet, and the per-product ASN.
_STAGE2A_SIDE_SUFFIXES = ('_cal.fits', '_cal_bkgsub.fits', '_s2d.fits',
                          '_s2d_bkgsub.fits', '.json')

# Every file of a stage-3 final '<obs>_<grating>_<filter>_<sid>' that the
# pipeline (stage 3, zfit, linefit) or a deploy writes into the workspace.
FINAL_SUFFIXES = ('_spec.fits', '_s2d.fits', '_x1d.fits', '_prof.pdf', '_spec.pdf',
                  '_zfit.fits', '_zfit.pdf', '_lines.fits', '_lines.pdf',
                  '_spec.json', '_spec_1d.json', '_zfit.json', '_lines.json')
# Per-exposure-group intermediates of combine_method='1d' ('<base>_g<N>_spec.fits' ...).
_EG_SUFFIXES = ('_spec.fits', '_s2d.fits', '_x1d.fits', '_prof.pdf', '_spec.pdf')

_MARKER_RE = re.compile(r'^(?P<prefix>.+_nrs[12])_(?P<sid>\d+)_nodata$')
_CANONICAL_RE = re.compile(r'^.+_nrs[12]_(?P<sid>\d+)\.fits$')


def write_nodata_marker(prod_name, reason, directory='.'):
    """Write ``<prod_name>_nodata`` holding ``reason`` (one line)."""
    path = os.path.join(directory, f'{prod_name}{NODATA_SUFFIX}')
    with open(path, 'w') as f:
        f.write(f'{reason}\n')
    return path


def mark_nodata(prod_name, reason, directory='.'):
    """Retire a product that yields no data, recording ``reason``.

    Removes the canonical ``<prod_name>.fits`` left by an earlier run (and its
    stage-2a side files) so stages 2b / 3 cannot pick it up as current, then
    writes the nodata marker. Returns the removed paths.
    """
    removed = []
    for suffix in ('.fits',) + _STAGE2A_SIDE_SUFFIXES:
        path = os.path.join(directory, f'{prod_name}{suffix}')
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    write_nodata_marker(prod_name, reason, directory)
    return removed


def mark_all_shutters_stuck(prod_name, directory='.'):
    """:func:`mark_nodata` with ``AllShuttersStuck``: every shutter of the
    source is listed as stuck closed, so nothing is left to extract."""
    return mark_nodata(prod_name, REASON_ALL_SHUTTERS_STUCK, directory)


def _header_filter_grating(path):
    hdr = fits.getheader(path, ext=0)
    return f"{str(hdr['GRATING']).strip().lower()}_{str(hdr['FILTER']).strip().lower()}"


def find_orphaned_finals(workspace_dir, obs_name, source_ids,
                         filter_grating_of=_header_filter_grating, mode_matches=None):
    """Finals of ``source_ids`` whose every exposure is now nodata.

    A (source, filter_grating) is orphaned when at least one nodata marker maps
    to it and no canonical does. A marker maps through its exposure's rate file
    (``<prefix>_rate.fits``, the marker's name carries no grating); a marker
    whose rate file is gone is ignored, never guessed. A final with no marker
    behind it is never reported: missing inputs are not proof of no data.

    Parameters
    ----------
    filter_grating_of : callable
        path -> ``'<grating>_<filter>'`` (lower case), read from the header.
    mode_matches : callable, optional
        ``(grating, filter) -> bool``, the observation's grating/filter
        selection; an orphan outside it is left alone.

    Returns
    -------
    dict
        ``{(source_id, filter_grating): [existing paths of that final]}``,
        only for orphans that still have files on disk.
    """
    wanted = {int(s) for s in source_ids}
    marked, present = {}, {}
    for name in sorted(os.listdir(workspace_dir)):
        m = _MARKER_RE.match(name)
        if m and int(m['sid']) in wanted:
            rate = os.path.join(workspace_dir, f"{m['prefix']}_rate.fits")
            if not os.path.exists(rate):
                log(f'Nodata marker {name} has no rate file; cannot map it to a grating')
                continue
            marked.setdefault(int(m['sid']), set()).add(filter_grating_of(rate))
            continue
        m = _CANONICAL_RE.match(name)
        if m and int(m['sid']) in wanted:
            present.setdefault(int(m['sid']), set()).add(
                filter_grating_of(os.path.join(workspace_dir, name)))

    orphans = {}
    for sid, fgs in sorted(marked.items()):
        for fg in sorted(fgs - present.get(sid, set())):
            if mode_matches is not None:
                grating, _, filt = fg.partition('_')
                if not mode_matches(grating.upper(), filt.upper()):
                    continue
            paths = final_files(workspace_dir, f'{obs_name}_{fg}_{sid}')
            if paths:
                orphans[(sid, fg)] = paths
    return orphans


def final_files(workspace_dir, base):
    """Existing files of the final ``base`` (and its per-exposure-group parts)."""
    paths = [os.path.join(workspace_dir, base + s) for s in FINAL_SUFFIXES]
    eg_re = re.compile(re.escape(base) + r'_g\d+(' +
                       '|'.join(re.escape(s) for s in _EG_SUFFIXES) + r')$')
    eg = [p for p in glob.glob(os.path.join(glob.escape(workspace_dir), glob.escape(base) + '_g*'))
          if eg_re.search(os.path.basename(p))]
    return [p for p in paths if os.path.exists(p)] + sorted(eg)


def remove_orphaned_finals(orphans):
    """Delete the files :func:`find_orphaned_finals` reported, logging each."""
    removed = []
    for (sid, fg), paths in sorted(orphans.items()):
        log(f'Source {sid} {fg}: every exposure is nodata, removing the stale final')
        for path in paths:
            if os.path.exists(path):
                os.remove(path)
                log(f'  removed {os.path.basename(path)}')
                removed.append(path)
    return removed
