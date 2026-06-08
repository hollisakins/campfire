"""Capture the CRDS reference basenames the extended-wavelength calibration was
derived against, for the gated configs, under the calibration context.

The committed extended photom (extended_jwst_nirspec_photom_0015.fits) divides out
the extended fflat/sflat. To keep that valid, the live pipeline regenerates the
extended flats under whatever CRDS context is active, and
``verify_extension_crds_compatibility`` refuses to run if the active context resolves
fflat/sflat/photom to versions *newer* than the baseline captured here.

This prints a ready-to-paste ``EXTENDED_CALIBRATION_REFERENCES`` dict for
``campfire_pipeline/nirspec/constants.py``. Uses ``crds.getrecommendations`` so it
resolves names without downloading the (multi-GB) reference files.

Run:  python scripts/capture_extended_calibration_refs.py
"""
import os

# Resolve names under the context the calibration was derived against.
os.environ.setdefault("CRDS_PATH", os.path.join(
    os.environ.get("CAMPFIRE_ROOT", os.path.expanduser("~/campfire")), "cache", "crds"))
os.environ["CRDS_SERVER_URL"] = "https://jwst-crds.stsci.edu"

import crds  # noqa: E402

CALIBRATION_CONTEXT = "jwst_1464.pmap"
GATED_CONFIGS = [
    ("NRS1", "G140M", "F100LP"),
    ("NRS2", "G140M", "F100LP"),
    ("NRS1", "G235M", "F170LP"),
    ("NRS2", "G235M", "F170LP"),
]
REFTYPES = ["fflat", "sflat", "photom"]


def resolve(det, grating, filt):
    params = {
        "INSTRUME": "NIRSPEC", "DETECTOR": det, "EXP_TYPE": "NRS_MSASPEC",
        "FILTER": filt, "GRATING": grating,
        "DATE-OBS": "2023-01-01", "TIME-OBS": "00:00:00",
    }
    recs = crds.getrecommendations(
        params, reftypes=REFTYPES, context=CALIBRATION_CONTEXT,
        observatory="jwst", fast=True)
    return {k: os.path.basename(v) for k, v in recs.items()}


def main():
    print(f"# Resolved under {CALIBRATION_CONTEXT}")
    print("EXTENDED_CALIBRATION_REFERENCES = {")
    for det, grating, filt in GATED_CONFIGS:
        refs = resolve(det, grating, filt)
        print(f'    ("{det}", "{grating}", "{filt}"): '
              f'{{"fflat": "{refs.get("fflat")}", '
              f'"sflat": "{refs.get("sflat")}", '
              f'"photom": "{refs.get("photom")}"}},')
    print("}")


if __name__ == "__main__":
    main()
