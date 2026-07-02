"""
jhat_report: classification, diagnostics, and end-of-run reporting for the
NIRCam ``jhat`` WCS-alignment step.

jhat alignment terminates in one of two ways. On success the refcat *filename*
is written to the ``CFP_JHAT`` header keyword. On failure one of three
sentinels is written instead, and (for the cases worth triaging) a small
per-exposure JSON sidecar is dropped next to the exposure so the end-of-run
summary can re-surface it:

* ``NO_REFCAT_OVERLAP`` -- zero reference-catalog sources land on the detector.
  The exposure is off the refcat footprint (expected near a field edge). Benign.
* ``ALIGN_FAILED`` -- refcat sources are present but the cross-matched set is too
  sparse or degenerate to fit a WCS correction. Needs attention: either the input
  WCS shipped too far off for jhat to find matches (fixable with a ``wcs_shift``
  rule) or the exposure is too shallow (tune the jhat cuts).
* ``ERROR`` -- an unexpected exception. Caught so one bad exposure can't abort
  the whole worker pool; surfaced loudly in the report so it gets investigated.

``is_failure`` tells a sentinel apart from a (successful) refcat filename by
membership in ``FAILURE_SENTINELS``. Downstream (``resample``) uses it to drop
non-aligned exposures from the mosaic so a bad WCS can't smear the stack.

The diagnostics that drive the triage hint are scraped from jhat's own verbose
stdout (see ``parse_diagnostics``) rather than its in-memory object model: at the
point alignment raises, the useful quantities (the 1st-iteration match-median
offset in particular) live only in locals that are gone after the exception, but
they have already been *printed*. The printed strings are user-facing and stable
across jhat versions; version drift only degrades the hint, never the
classification.
"""

import glob
import json
import os
import re

from campfire_pipeline.common.io import log


# CFP_JHAT sentinel values (see module docstring).
NO_REFCAT_SENTINEL = 'NO_REFCAT_OVERLAP'
ALIGN_FAILED_SENTINEL = 'ALIGN_FAILED'
ERROR_SENTINEL = 'ERROR'
FAILURE_SENTINELS = frozenset(
    {NO_REFCAT_SENTINEL, ALIGN_FAILED_SENTINEL, ERROR_SENTINEL})

# Per-exposure failure sidecar: ``<rootname>.jhat_fail.json`` next to the
# canonical exposure. One file per failing exposure keeps the worker pool
# lock-free (no shared append target).
SHARD_SUFFIX = '.jhat_fail.json'

# Fewer detected image sources than this and a failure reads as "too shallow"
# rather than "bad WCS". 20 is comfortably below a normal NIRCam field's source
# count (hundreds) yet above the handful a genuinely empty/striped frame yields.
_SHALLOW_NDETECT = 20


def is_failure(cfp_jhat_value):
    """True if a ``CFP_JHAT`` value marks a non-aligned exposure.

    Success stores the refcat filename, so only the explicit sentinels count as
    failures. A falsy value (keyword absent) is *not* treated as a failure here
    so fields that don't run jhat aren't penalized downstream.
    """
    return cfp_jhat_value in FAILURE_SENTINELS


# ---------------------------------------------------------------------------
# Diagnostics: scrape jhat's verbose stdout
# ---------------------------------------------------------------------------

class TeeStdout:
    """File-like that fans writes out to several streams.

    Wrapped around the align call so jhat's verbose output still reaches the
    real stdout (unchanged console behavior) while a copy lands in an in-memory
    buffer we can parse if the call raises.
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


_RE_MATCHING = re.compile(r'Matching (\d+) image objects to (\d+) refcat objects')
_RE_KEEPING = re.compile(r'Keeping (\d+) out of \d+ .*?objects within x=')
_RE_PASSED = re.compile(r'(\d+) matches are passed to tweakreg')
_RE_DXDY = re.compile(
    r'dx median of best matched objects of 1st iteration:\s*([-+0-9.eE]+|nan)\s+'
    r'dy median of best matched objects of 1st iteration:\s*([-+0-9.eE]+|nan)')
_RE_ZERO_REF = re.compile(
    r'0 sources from reference catalog within the image bounderies')


def parse_diagnostics(text):
    """Best-effort source/match statistics from jhat's verbose stdout.

    Every field is optional; if jhat changes its wording the dict just comes
    back sparser and the hint degrades to "inspect manually".
    """
    d = {}
    m = _RE_MATCHING.search(text)
    if m:
        d['n_detected'] = int(m.group(1))
        d['n_refcat_in_bounds'] = int(m.group(2))
    else:
        km = _RE_KEEPING.search(text)
        if km:
            d['n_refcat_in_bounds'] = int(km.group(1))
    passed = _RE_PASSED.findall(text)
    if passed:
        # Last value is the count handed to the (failing) final fit.
        d['n_matched'] = int(passed[-1])
    xm = _RE_DXDY.search(text)
    if xm and 'nan' not in (xm.group(1), xm.group(2)):
        try:
            d['rough_dx_px'] = float(xm.group(1))
            d['rough_dy_px'] = float(xm.group(2))
        except ValueError:
            pass
    if _RE_ZERO_REF.search(text):
        d['n_refcat_in_bounds'] = 0
    return d


# ---------------------------------------------------------------------------
# Classification + triage hint
# ---------------------------------------------------------------------------

def _is_alignment_failure(exc):
    """True for the known "matched set too sparse/degenerate to fit" signatures.

    These all mean refcat coverage exists but cross-matching collapsed; they
    surface at different points in the tweakwcs/stcal stack depending on exactly
    how few matches survive jhat's cuts:

    * ``ValueError("No valid polygons provided")`` -- the matched-source convex
      hull is collinear/zero-area, so ``SphericalPolygon.multi_union`` rejects it.
    * ``NotEnoughPointsError`` -- fewer than 2 matched points reach the linear
      fit.
    * stcal's own "Not enough sources" / "Too few input images" /
      "Number of output coordinates exceeded allocation" guards.
    """
    if isinstance(exc, ValueError) and 'No valid polygons provided' in str(exc):
        return True
    if type(exc).__name__ == 'NotEnoughPointsError':
        return True
    msg = str(exc)
    return ('Not enough sources' in msg
            or 'Too few input images' in msg
            or 'Number of output coordinates exceeded allocation' in msg)


def classify(exc, diag):
    """Map a caught jhat exception (+ scraped diagnostics) to a sentinel."""
    if ((isinstance(exc, KeyError) and exc.args == (None,))
            or diag.get('n_refcat_in_bounds') == 0):
        return NO_REFCAT_SENTINEL
    if _is_alignment_failure(exc):
        return ALIGN_FAILED_SENTINEL
    return ERROR_SENTINEL


def hint(category, diag):
    """One-line, actionable triage note for the report."""
    if category == NO_REFCAT_SENTINEL:
        return 'off refcat footprint (expected near a field edge); skip is benign'
    if category == ERROR_SENTINEL:
        return 'unexpected error — inspect the run-log traceback'
    nd = diag.get('n_detected')
    if nd is None:
        return 'coverage present but alignment failed; inspect manually'
    if nd < _SHALLOW_NDETECT:
        return (f'only {nd} sources detected — likely too shallow; relax jhat '
                f'cuts (SNR_min / objmag_lim) or accept the skip')
    off = ''
    if diag.get('rough_dx_px') is not None:
        off = (f'; 1st-iter match-median offset ~dx={diag["rough_dx_px"]:.1f}, '
               f'dy={diag["rough_dy_px"]:.1f} px — seed for a wcs_shift rule')
    return (f'{nd} sources detected but matches collapse — likely bad input '
            f'WCS{off}')


# ---------------------------------------------------------------------------
# Per-exposure failure sidecar I/O (lock-free across the worker pool)
# ---------------------------------------------------------------------------

def _shard_path(input_dir, rootname):
    return os.path.join(input_dir, rootname + SHARD_SUFFIX)


def write_failure(input_dir, rootname, filtname, category, exc, diag, hint_text):
    """Atomically drop a failure sidecar next to the exposure."""
    rec = {
        'rootname': rootname,
        'filter': filtname,
        'category': category,
        'exception': f'{type(exc).__name__}: {exc}',
        'hint': hint_text,
    }
    rec.update(diag)
    path = _shard_path(input_dir, rootname)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as fp:
            json.dump(rec, fp, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        log(f"jhat: could not write failure record for {rootname}: {e}")


def clear_failure(input_dir, rootname):
    """Remove a stale failure sidecar after a (re-)successful alignment."""
    try:
        os.remove(_shard_path(input_dir, rootname))
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"jhat: could not clear stale failure record for {rootname}: {e}")


# ---------------------------------------------------------------------------
# End-of-run summary
# ---------------------------------------------------------------------------

def collect_failures(field, filters):
    """Load every failure sidecar across ``filters`` into a list of dicts."""
    out = []
    for filt in filters:
        try:
            d = field.filter_dir(filt)
        except Exception:
            continue
        for p in sorted(glob.glob(os.path.join(d, '*' + SHARD_SUFFIX))):
            try:
                with open(p) as fp:
                    out.append(json.load(fp))
            except (OSError, ValueError):
                continue
    return out


def _write_report_table(field, records):
    """Write the aggregated failures as an ECSV alongside the field products."""
    try:
        from astropy.table import Table
    except Exception:
        return None
    cols = ['rootname', 'filter', 'category', 'n_detected',
            'n_refcat_in_bounds', 'n_matched', 'rough_dx_px', 'rough_dy_px',
            'hint']
    rows = {c: [] for c in cols}
    for r in records:
        for c in cols:
            rows[c].append(r.get(c, ''))
    path = os.path.join(field.products_dir, 'jhat_alignment_report.ecsv')
    try:
        Table({c: rows[c] for c in cols}).write(
            path, format='ascii.ecsv', overwrite=True)
        return path
    except Exception as e:
        log(f"jhat: could not write alignment report table: {e}")
        return None


def report(field, filters):
    """Print the end-of-run jhat alignment summary and write an ECSV.

    Silent when no failures were recorded. Reads the per-exposure sidecars, so
    it re-surfaces the same lists on every run until the exposures are fixed
    (and their sidecars cleared by a successful re-alignment).
    """
    records = collect_failures(field, filters)
    if not records:
        return

    by_cat = {NO_REFCAT_SENTINEL: [], ALIGN_FAILED_SENTINEL: [], ERROR_SENTINEL: []}
    for r in records:
        by_cat.setdefault(r.get('category', ERROR_SENTINEL), []).append(r)

    def _label(r):
        return f"{r.get('rootname', '?')} [{r.get('filter', '?')}]"

    log('')
    log('=' * 74)
    log(f"jhat alignment report — field '{field.name}': "
        f"{len(records)} exposure(s) not aligned")
    log('=' * 74)

    no_cov = by_cat.get(NO_REFCAT_SENTINEL, [])
    if no_cov:
        log(f"  • {len(no_cov)} off refcat footprint (skipped, benign) — "
            f"OK unless you expected coverage here:")
        for r in no_cov:
            log(f"      {_label(r)}")

    failed = by_cat.get(ALIGN_FAILED_SENTINEL, [])
    if failed:
        log(f"  • {len(failed)} have coverage but FAILED to align — action "
            f"needed (wcs_shift rule or relax jhat cuts):")
        for r in failed:
            log(f"      {_label(r)}: {r.get('hint', '')}")

    errored = by_cat.get(ERROR_SENTINEL, [])
    if errored:
        log(f"  • {len(errored)} hit an UNEXPECTED error (caught; not aligned) "
            f"— investigate:")
        for r in errored:
            log(f"      {_label(r)}: {r.get('exception', '')}")

    path = _write_report_table(field, records)
    if path:
        log(f"  full table: {path}")
    log('=' * 74)
    log('')
