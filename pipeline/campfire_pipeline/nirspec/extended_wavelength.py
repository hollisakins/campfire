"""Extended-wavelength reference-file generation for the opt-in
``[nirspec.stage2].extend_g140m_g235m`` feature.

The F070LP / F100LP / F170LP long-pass filters pass light redward of the nominal
G140M / G235M cutoffs, but the CRDS flats and ``wavelengthrange`` reference files stop
at the nominal limit. This module regenerates extended ``fflat`` / ``sflat`` /
``wavelengthrange`` reference files on the fly (cached under ``$CAMPFIRE_ROOT/cache``)
so ``Spec2Pipeline`` can extract the redder 1st-order light. The flux calibration of
that extended region is supplied by the committed extended ``photom`` reference
(``config.get_extended_photom_path()``), which Anthony derived against the SPURS
program (see ``data/Generate_Extended_Cals.py``).

Two constructions, both ports of ``generate_extended_cals()`` in that script (kept
nearly verbatim):

* G140M/F100LP and G235M/F170LP: the flat table is continued on a 1 nm grid to
  5.3 um, holding the value nearest ``FLAT_HOLD_WAVELENGTH_UM``.
* G140M/F070LP: the *extended* F100LP flat (same quadrant / same detector) is
  stitched onto the red end of the F070LP flat, the seam smoothed with a gaussian
  (``STITCHED_FLAT_SOURCE_FILTER``).

The live extended flats MUST match the ones the committed photom was calibrated
against. ``verify_extension_crds_compatibility()`` enforces that the active CRDS
context still resolves the flats/photom (including the F100LP flats an F070LP
reduction stitches on) to the calibration baseline; if CRDS has shipped newer flats,
the calibration must be re-derived with the maintainer script.

Only ``EXTENDED_GRATING_FILTERS`` are handled; every other config is left to the
normal CRDS path.
"""

import os

import numpy as np
from astropy.io import fits
from astropy.table import Table
from scipy.ndimage import gaussian_filter

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.constants import (
    EXTENDED_GRATING_FILTERS,
    EXTENDED_GRID_MAX_UM,
    EXTENDED_MAX_WAVELENGTH_UM,
    STITCHED_FLAT_SOURCE_FILTER,
    EXTENDED_WAVELENGTHRANGE_M,
    FLAT_HOLD_WAVELENGTH_UM,
    EXTENDED_CALIBRATION_CONTEXT,
    EXTENDED_CALIBRATION_REFERENCES,
)

# Flat-field reference HDUs that hold a wavelength-dependent flat table.
_FFLAT_TABLE_HDUS = (5, 10, 15, 20)
_SFLAT_TABLE_HDU = 5
_FLAT_GRID_STEP_UM = 0.001
# F070LP stitching: half-width (samples) of the seam window and gaussian sigma.
_STITCH_HALF_WINDOW = 15
_STITCH_SIGMA = 10

# Version of the extension construction, part of every cached filename: bump it
# whenever the flat / wavelengthrange construction changes, so a cache file built by
# an older construction is never reused against a photom calibrated with a newer one.
_EXTENSION_VERSION = 3


def _crds_params(det, grating, filt, hdr=None):
    """Build a CRDS bestrefs parameter dict for one NIRSpec MSA config."""
    hdr = hdr or {}
    return {
        "INSTRUME": "NIRSPEC",
        "DETECTOR": det,
        "EXP_TYPE": hdr.get("EXP_TYPE", "NRS_MSASPEC"),
        "FILTER": filt,
        "GRATING": grating,
        "DATE-OBS": hdr.get("DATE-OBS", "2023-01-01"),
        "TIME-OBS": hdr.get("TIME-OBS", "00:00:00"),
    }


def _gated_configs(rate_files):
    """Yield unique (detector, grating, filter, header) for gated rate files only."""
    seen = set()
    for rate_file in rate_files:
        hdr = fits.getheader(rate_file)
        det = hdr.get("DETECTOR", "NRS1")
        grating = hdr.get("GRATING", "UNKNOWN")
        filt = hdr.get("FILTER", "UNKNOWN")
        if (grating, filt) not in EXTENDED_GRATING_FILTERS:
            continue
        key = (det, grating, filt)
        if key in seen:
            continue
        seen.add(key)
        yield det, grating, filt, hdr


def _extended_output_path(src_path, cache_dir, stitched_onto=None):
    """Cache path for the extended version of a CRDS reference file.

    The source basename carries the CRDS reference version (e.g.
    ``jwst_nirspec_fflat_0200.fits``), so a CRDS bump that changes the underlying
    flat produces a different output name and naturally invalidates the cache. A
    stitched flat also depends on the flat stitched onto it, whose version is
    appended (``extended_v3_jwst_nirspec_fflat_0196+0200.fits``).
    """
    stem, ext = os.path.splitext(os.path.basename(src_path))
    if stitched_onto:
        stem += "+" + os.path.splitext(os.path.basename(stitched_onto))[0].rsplit("_", 1)[-1]
    return os.path.join(cache_dir, f"extended_v{_EXTENSION_VERSION}_{stem}{ext}")


# ---------------------------------------------------------------------------
# Flat extension (port of generate_extended_cals, nearly verbatim)
# ---------------------------------------------------------------------------

def _extend_flat_table(tab, hold):
    """Continue one flat table to 5.3 um, holding the value nearest ``hold``.

    Returns ``(newwave, newflux)``. Only the positive-wavelength samples are kept
    (the S-flat tables are zero-padded). Raises if the held sample is not a finite
    positive value, which would blank the whole extended region.
    """
    wave = tab['wavelength'][0]
    newwave = np.concatenate([wave[wave > 0], np.round(np.arange(np.round(np.max(wave) + _FLAT_GRID_STEP_UM, 3), EXTENDED_GRID_MAX_UM, _FLAT_GRID_STEP_UM), 3)])
    newflux = np.ones_like(newwave)
    newflux[:len(wave[wave > 0])] = tab['data'][0][wave > 0]
    held = newflux[np.argmin(np.abs(newwave - hold))]  # flat after last real value
    if not (np.isfinite(held) and held > 0):
        raise ValueError(
            f"extended flat: the sample nearest the hold wavelength {hold} um is {held}; "
            "move FLAT_HOLD_WAVELENGTH_UM (and maxwaves in Generate_Extended_Cals.py) "
            "inside the flat's finite range")
    newflux[newwave >= hold] = held
    newflux[newflux <= 0] = np.nan
    return newwave, newflux


def _stitch_flat_table(tab, red_tab):
    """Append the samples of ``red_tab`` redward of ``tab``'s last wavelength.

    Returns ``(newwave, newflux)``: ``tab``'s positive-wavelength samples, then the
    (already extended) ``red_tab`` beyond them, non-finite samples dropped, with the
    seam smoothed by a gaussian.
    """
    wave = tab['wavelength'][0]
    red_wave = red_tab['wavelength'][0]
    newwave = np.concatenate([wave[wave > 0], red_wave[red_wave > np.max(wave)]])
    newflux = np.ones_like(newwave)
    newflux[:len(wave[wave > 0])] = tab['data'][0][wave > 0]
    newflux[newwave > np.max(wave)] = red_tab['data'][0][red_wave > np.max(wave)]
    newflux[newflux <= 0] = np.nan
    # Drop the non-finite samples before smoothing: a NaN inside the seam window would
    # spread over the whole window (the F070LP F-flat is NaN from 1.261 um).
    newflux, newwave = newflux[np.isfinite(newflux)], newwave[np.isfinite(newflux)]
    idx = np.where(newwave > np.max(wave))[0][0]
    seam = slice(idx - _STITCH_HALF_WINDOW, idx + _STITCH_HALF_WINDOW)
    newflux[seam] = gaussian_filter(newflux[seam], _STITCH_SIGMA)  # smooth the transition slightly
    return newwave, newflux


def _flat_table_hdu(tab, newwave, newflux, with_error):
    """Replace a flat table's wavelength / data (/ error) arrays with the new ones."""
    tab['newwavelength'] = newwave[:, None].T.astype('float32')
    tab['newdata'] = newflux[:, None].T.astype('float32')
    cols = ['wavelength', 'data']
    if with_error:
        tab['newerror'] = newflux[:, None].T.astype('float32') * np.nan
        cols.append('error')
    for col in cols:
        del tab[col]
        tab['new' + col].name = col
    tab['nelem'] = len(newflux)
    return fits.table_to_hdu(tab)


def _write_fflat(src_path, out_path, table_fn):
    """Write ``src_path`` with each quadrant's fast-variation table rebuilt.

    ``table_fn(tab, num)`` returns ``(newwave, newflux)`` for HDU ``num``.
    """
    with fits.open(src_path) as fflat:
        newflat = fits.HDUList()
        for num, hdu in enumerate(fflat):
            if num in _FFLAT_TABLE_HDUS:
                tab = Table.read(src_path, hdu=num)
                newflat.append(_flat_table_hdu(tab, *table_fn(tab, num), with_error=True))
            else:
                newflat.append(hdu.copy())
        newflat.writeto(out_path, overwrite=True)


def _write_sflat(src_path, out_path, newwave, newflux):
    """Write ``src_path`` with its flat table rebuilt and the last plane moved to 5.3 um."""
    tab = Table.read(src_path, hdu=_SFLAT_TABLE_HDU)
    with fits.open(src_path) as sflat:
        # Independent copy so the CRDS-cached file is never mutated.
        out = fits.HDUList([hdu.copy() for hdu in sflat])
    out[_SFLAT_TABLE_HDU] = _flat_table_hdu(tab, newwave, newflux, with_error=False)
    out[1].header[f"FLAT_{out[1].data.shape[0]:02}"] = EXTENDED_MAX_WAVELENGTH_UM
    out[4].data[-1] = (EXTENDED_MAX_WAVELENGTH_UM,)
    out.writeto(out_path, overwrite=True)
    out.close()


def _build_extended_fflat(src_path, grating, filt, out_path):
    """Extend an fflat's four quadrant tables to 5.3 um (held flat)."""
    hold = FLAT_HOLD_WAVELENGTH_UM[(grating, filt)]
    _write_fflat(src_path, out_path, lambda tab, num: _extend_flat_table(tab, hold))


def _build_stitched_fflat(src_path, extended_source_path, out_path):
    """Extend an fflat by stitching the extended source fflat's same quadrant onto it."""
    _write_fflat(src_path, out_path, lambda tab, num: _stitch_flat_table(
        tab, Table.read(extended_source_path, hdu=num)))


def _build_extended_sflat(src_path, grating, filt, out_path):
    """Extend an sflat's flat table to 5.3 um (held flat)."""
    tab = Table.read(src_path, hdu=_SFLAT_TABLE_HDU)
    _write_sflat(src_path, out_path,
                 *_extend_flat_table(tab, FLAT_HOLD_WAVELENGTH_UM[(grating, filt)]))


def _build_stitched_sflat(src_path, extended_source_path, out_path):
    """Extend an sflat by stitching the extended source sflat (same detector) onto it."""
    tab = Table.read(src_path, hdu=_SFLAT_TABLE_HDU)
    red_tab = Table.read(extended_source_path, hdu=_SFLAT_TABLE_HDU)
    _write_sflat(src_path, out_path, *_stitch_flat_table(tab, red_tab))


def _build_extended_wavelengthrange(src_path, out_path):
    """Write a wavelengthrange ASDF with the gated selectors widened to 5.3 um."""
    import asdf
    from datetime import datetime

    with asdf.open(src_path) as af:
        selectors = af["waverange_selector"]
        wranges = af["wavelengthrange"]
        for selector, new_range in EXTENDED_WAVELENGTHRANGE_M.items():
            if selector in selectors:
                wranges[selectors.index(selector)] = list(new_range)
            else:
                log(f"extend_g140m_g235m: '{selector}' not in waverange_selector "
                    f"of {os.path.basename(src_path)}")
        try:
            entry = {
                "description": "Extended wavelength ranges for "
                               + ", ".join(EXTENDED_WAVELENGTHRANGE_M),
                "software": {"author": "campfire pipeline",
                             "name": "extended_wavelength.py", "version": "1"},
                "time": datetime.now(),
            }
            if "history" in af.tree and "entries" in af.tree.get("history", {}):
                af["history"]["entries"].append(entry)
            af["meta"]["date"] = datetime.now().isoformat()
        except Exception as exc:  # provenance metadata is best-effort
            log(f"extend_g140m_g235m: could not stamp wavelengthrange history: {exc}")
        af.write_to(out_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_extended_reference_files(rate_files):
    """Generate (and cache) extended reference files for the gated configs.

    Resolves the active CRDS context's ``fflat`` / ``sflat`` / ``wavelengthrange``
    for each unique gated (detector, grating, filter) present in *rate_files*,
    extends them to ``EXTENDED_GRID_MAX_UM``, and caches the results under
    ``$CAMPFIRE_ROOT/cache``. A stitched config (G140M/F070LP) also resolves and
    extends the flats of its source filter (F100LP), whose extended versions it
    stitches on. The committed extended ``photom`` is used as-is.

    Run serially in the orchestrator (before the parallel dispatch) so the cache
    writes never race across workers.

    Parameters
    ----------
    rate_files : list of str
        Stage-1 rate files for the observation.

    Returns
    -------
    dict
        ``{(detector, grating, filter): {'fflat', 'sflat', 'wavelengthrange',
        'photom'}}`` of absolute override paths. Empty if no gated config is present.
    """
    import crds
    from campfire_pipeline.config import _get_campfire_root, get_extended_photom_path

    cache_dir = os.path.join(_get_campfire_root(), "cache")
    os.makedirs(cache_dir, exist_ok=True)
    photom_path = get_extended_photom_path()
    built = set()  # output paths already (re)built or found in this call

    def resolve(det, grating, filt, hdr, reftypes):
        refs = crds.getreferences(
            _crds_params(det, grating, filt, hdr), reftypes=reftypes, observatory="jwst")
        for reftype in reftypes:
            src = refs.get(reftype, "")
            if not src or "N/A" in src.upper():
                raise FileNotFoundError(
                    f"CRDS returned no {reftype} for {det}/{grating}/{filt}; "
                    "cannot build extended reference files.")
        return refs

    def cached(src, builder, stitched_onto=None):
        out_path = _extended_output_path(src, cache_dir, stitched_onto)
        if out_path not in built:
            if os.path.exists(out_path):
                log(f"  using cached {os.path.basename(out_path)}")
            else:
                builder(src, out_path)
                log(f"  wrote {os.path.basename(out_path)}")
            built.add(out_path)
        return out_path

    result = {}
    for det, grating, filt, hdr in _gated_configs(rate_files):
        log(f"extend_g140m_g235m: building extended references for "
            f"{det}/{grating}/{filt}")
        refs = resolve(det, grating, filt, hdr, ["fflat", "sflat", "wavelengthrange"])
        entry = {
            "photom": photom_path,
            "wavelengthrange": cached(refs["wavelengthrange"], _build_extended_wavelengthrange),
        }
        source_filt = STITCHED_FLAT_SOURCE_FILTER.get((grating, filt))
        if source_filt is None:
            entry["fflat"] = cached(
                refs["fflat"], lambda s, o: _build_extended_fflat(s, grating, filt, o))
            entry["sflat"] = cached(
                refs["sflat"], lambda s, o: _build_extended_sflat(s, grating, filt, o))
        else:
            src_refs = resolve(det, grating, source_filt, hdr, ["fflat", "sflat"])
            for reftype, extend, stitch in (
                ("fflat", _build_extended_fflat, _build_stitched_fflat),
                ("sflat", _build_extended_sflat, _build_stitched_sflat),
            ):
                extended_source = cached(
                    src_refs[reftype],
                    lambda s, o, extend=extend: extend(s, grating, source_filt, o))
                entry[reftype] = cached(
                    refs[reftype],
                    lambda s, o, stitch=stitch, e=extended_source: stitch(s, e, o),
                    stitched_onto=src_refs[reftype])
        result[(det, grating, filt)] = entry

    return result


def verify_extension_crds_compatibility(rate_files):
    """Refuse to run extended reductions if CRDS has drifted from the calibration.

    The committed extended photom was calibrated (under
    ``EXTENDED_CALIBRATION_CONTEXT``) by dividing out the extended ``fflat`` /
    ``sflat``. The live extended flats are regenerated under the *active* context;
    if that context resolves ``fflat`` / ``sflat`` / ``photom`` to a different
    (newer) version than the calibration baseline, the committed correction no
    longer matches the data and the calibration must be re-derived with
    ``data/Generate_Extended_Cals.py``.

    Compares resolved basenames against ``EXTENDED_CALIBRATION_REFERENCES`` (using
    ``getrecommendations`` — names only, no downloads). A stitched config is checked
    together with the source filter whose flats it stitches on. Baseline fields
    recorded as ``None`` are not yet pinned and are skipped with a warning.

    Raises
    ------
    RuntimeError
        If any gated config's active reference differs from the recorded baseline.
    """
    import crds

    try:
        active_context = crds.get_context_name("jwst")
    except Exception:
        active_context = os.environ.get("CRDS_CONTEXT", "<default>")

    # A stitched config (G140M/F070LP) also depends on its source filter's flats.
    checks = {}
    for det, grating, filt, hdr in _gated_configs(rate_files):
        checks.setdefault((det, grating, filt), hdr)
        source_filt = STITCHED_FLAT_SOURCE_FILTER.get((grating, filt))
        if source_filt is not None:
            checks.setdefault((det, grating, source_filt), hdr)

    problems = []
    unpinned = []
    for (det, grating, filt), hdr in checks.items():
        key = (det, grating, filt)
        baseline = EXTENDED_CALIBRATION_REFERENCES.get(key)
        if baseline is None:
            problems.append(f"{key}: no calibration baseline recorded")
            continue
        recs = crds.getrecommendations(
            _crds_params(det, grating, filt, hdr),
            reftypes=["fflat", "sflat", "photom"], observatory="jwst", fast=True)
        for reftype in ("fflat", "sflat", "photom"):
            base = baseline.get(reftype)
            active = os.path.basename(recs.get(reftype, ""))
            if base is None:
                unpinned.append(f"{key} {reftype} (active {active})")
                continue
            if active != base:
                problems.append(
                    f"{key} {reftype}: active '{active}' != calibration baseline '{base}'")

    if unpinned:
        log("extend_g140m_g235m: calibration baseline not yet pinned for: "
            + "; ".join(unpinned)
            + " — run scripts/capture_extended_calibration_refs.py and fill "
              "EXTENDED_CALIBRATION_REFERENCES to enforce the guard.")

    if problems:
        raise RuntimeError(
            "Extended-wavelength reduction blocked — the active CRDS context "
            f"({active_context}) resolves references that differ from the committed "
            f"calibration baseline ({EXTENDED_CALIBRATION_CONTEXT}):\n  "
            + "\n  ".join(problems)
            + "\n\nThe committed extended photom was calibrated against the baseline "
              "flats. Re-derive with data/Generate_Extended_Cals.py against the "
              "current references, or pin CRDS_CONTEXT to a compatible context.")
