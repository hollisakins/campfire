"""Extended-wavelength reference-file generation for the opt-in
``[nirspec.stage2].extend_g140m_g235m`` feature.

The F100LP / F170LP long-pass filters pass light redward of the nominal G140M /
G235M cutoffs, but the CRDS flats and ``wavelengthrange`` reference files stop at
the nominal limit. This module regenerates extended ``fflat`` / ``sflat`` /
``wavelengthrange`` reference files on the fly (cached under ``$CAMPFIRE_ROOT/cache``)
so ``Spec2Pipeline`` can extract the redder 1st-order light. The flux calibration of
that extended region is supplied by the committed extended ``photom`` reference
(``config.get_extended_photom_path()``), which Anthony derived once against the SPURS
program (see ``data/Generate_Extended_Cals.py``).

The flat-extension math here is a faithful port of ``generate_extended_cals()`` in
that script — the live extended flats MUST match the ones the committed photom was
calibrated against. ``verify_extension_crds_compatibility()`` enforces that the active
CRDS context still resolves the flats/photom to the calibration baseline; if CRDS has
shipped newer flats, the calibration must be re-derived with the maintainer script.

Only ``EXTENDED_GRATING_FILTERS`` (G140M/F100LP, G235M/F170LP) are handled; every
other config is left to the normal CRDS path.
"""

import os

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.constants import (
    EXTENDED_GRATING_FILTERS,
    EXTENDED_GRID_MAX_UM,
    EXTENDED_WAVELENGTHRANGE_M,
    FLAT_HOLD_WAVELENGTH_UM,
    EXTENDED_CALIBRATION_CONTEXT,
    EXTENDED_CALIBRATION_REFERENCES,
)

# Flat-field reference HDUs that hold a wavelength-dependent flat table.
_FFLAT_TABLE_HDUS = (5, 10, 15, 20)
_SFLAT_TABLE_HDU = 5
_FLAT_GRID_STEP_UM = 0.001


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


def _extended_output_path(src_path, cache_dir):
    """Cache path for the extended version of a CRDS reference file.

    The source basename carries the CRDS reference version (e.g.
    ``jwst_nirspec_fflat_0168.fits``), so a CRDS bump that changes the underlying
    flat produces a different output name and naturally invalidates the cache.
    """
    return os.path.join(cache_dir, "extended_" + os.path.basename(src_path))


# ---------------------------------------------------------------------------
# Flat / wavelengthrange extension (faithful port of generate_extended_cals)
# ---------------------------------------------------------------------------

def _build_extended_fflat(src_path, grating, filt, out_path):
    """Extend an fflat reference file's wavelength-dependent flat tables to 5.3 um."""
    hold = FLAT_HOLD_WAVELENGTH_UM[(grating, filt)]
    with fits.open(src_path) as fflat:
        newflat = fits.HDUList()
        for num, hdu in enumerate(fflat):
            if num in _FFLAT_TABLE_HDUS:
                tab = Table.read(src_path, hdu=num)
                wav0 = np.asarray(tab["wavelength"][0], dtype=float)
                newwave = np.concatenate([wav0, np.round(np.arange(np.round(np.max(tab['wavelength'][0])+_FLAT_GRID_STEP_UM, 3), EXTENDED_GRID_MAX_UM, _FLAT_GRID_STEP_UM), 3)])
                newflux=np.ones_like(newwave)
                newflux[:len(wav0)]=tab['data'][0]
                # Hold the flat constant beyond the last measured wavelength.
                hold_idx = int(np.argmin(np.abs(newwave - hold)))
                newflux[newwave >= hold] = newflux[hold_idx]
                newflux[newflux<=0] = np.nan
                tab["newwavelength"] = newwave[:, None].T.astype("float32")
                tab["newdata"] = newflux[:, None].T.astype("float32")
                tab["newerror"] = newflux[:, None].T.astype("float32") * np.nan
                for col in ("wavelength", "data", "error"):
                    del tab[col]
                    tab["new" + col].name = col
                tab["nelem"] = len(newflux)
                newflat.append(fits.table_to_hdu(tab))
            else:
                newflat.append(hdu.copy())
        newflat.writeto(out_path, overwrite=True)


def _build_extended_sflat(src_path, grating, filt, out_path):
    """Extend an sflat reference file's flat table to 5.3 um (copy, never mutate src)."""
    from scipy.interpolate import interp1d

    hold = FLAT_HOLD_WAVELENGTH_UM[(grating, filt)]
    with fits.open(src_path) as sflat:
        tab = Table.read(src_path, hdu=_SFLAT_TABLE_HDU)
        wav0 = np.asarray(tab["wavelength"][0], dtype=float)
        newwave = np.concatenate([wav0, np.round(np.arange(np.round(np.max(tab['wavelength'][0])+_FLAT_GRID_STEP_UM, 3), EXTENDED_GRID_MAX_UM, _FLAT_GRID_STEP_UM), 3)])
        newflux=np.ones_like(newwave)
        newflux[:len(wav0)]=tab['data'][0]
        # Hold the flat constant beyond the last measured wavelength.
        hold_idx = int(np.argmin(np.abs(newwave - hold)))
        newflux[newwave >= hold] = newflux[hold_idx]
        newflux[newflux<=0] = np.nan
        tab["newwavelength"] = newwave[:, None].T.astype("float32")
        tab["newdata"] = newflux[:, None].T.astype("float32")
        for col in ("wavelength", "data"):
            del tab[col]
            tab["new" + col].name = col
        tab["nelem"] = len(newflux)

        # Build an independent HDUList copy so the CRDS-cached file is never mutated.
        out = fits.HDUList([hdu.copy() for hdu in sflat])
        out[_SFLAT_TABLE_HDU] = fits.table_to_hdu(tab)
        out[1].header["FLAT_05"] = EXTENDED_GRID_MAX_UM - 0.001  # 5.3
        out[4].data[-1] = (EXTENDED_GRID_MAX_UM - 0.001,)
        out.writeto(out_path, overwrite=True)
        out.close()


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
    ``$CAMPFIRE_ROOT/cache``. The committed extended ``photom`` is used as-is.

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

    result = {}
    built = set()  # output paths already (re)built in this call
    for det, grating, filt, hdr in _gated_configs(rate_files):
        log(f"extend_g140m_g235m: building extended references for "
            f"{det}/{grating}/{filt}")
        refs = crds.getreferences(
            _crds_params(det, grating, filt, hdr),
            reftypes=["fflat", "sflat", "wavelengthrange"], observatory="jwst")

        entry = {"photom": photom_path}
        for reftype, builder in (
            ("fflat", lambda s, o: _build_extended_fflat(s, grating, filt, o)),
            ("sflat", lambda s, o: _build_extended_sflat(s, grating, filt, o)),
            ("wavelengthrange", _build_extended_wavelengthrange),
        ):
            src = refs.get(reftype, "")
            if not src or "N/A" in src.upper():
                raise FileNotFoundError(
                    f"CRDS returned no {reftype} for {det}/{grating}/{filt}; "
                    "cannot build extended reference files.")
            out_path = _extended_output_path(src, cache_dir)
            if out_path not in built and not os.path.exists(out_path):
                builder(src, out_path)
                log(f"  wrote {os.path.basename(out_path)}")
            elif out_path not in built:
                log(f"  using cached {os.path.basename(out_path)}")
            built.add(out_path)
            entry[reftype] = out_path

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
    ``getrecommendations`` — names only, no downloads). Baseline fields recorded as
    ``None`` are not yet pinned and are skipped with a warning.

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

    problems = []
    unpinned = []
    for det, grating, filt, hdr in _gated_configs(rate_files):
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
