#!/usr/bin/env python
"""Repack + manifest builder for the NIRCam spike footprint model set.

Sibling of ``build_wisp_manifest.py``; see ``docs/design-nircam-spike-masking.md``
§3.4 and ``pipeline/WISP_TEMPLATE_HOSTING.md`` (the hosting flow carries over
as-is, with a ``spike_models/<version>/`` path prefix).

Two subcommands:

``repack``
    Turn the raw WebbPSF ``PSF+scatlight_<λ>micron.fits`` set (float64,
    detector-sampled, sum-normalized) into the published two-grade set:

    - **photometric** grade: full resolution, float32 — the Phase-3 candidate.
    - **mask** grade: block-MAX downsampled (default x4 in both channels =
      a 4-native-px cell, 0.124 arcsec SW / 0.252 arcsec LW), float32.
      Max — not mean —
      because the grade exists to make footprints: a coarse cell records
      "some sub-pixel exceeds this SB", so a threshold mask from the mask
      grade is a superset of the full-res mask at every level (never
      under-masks); overshoot is bounded by the block diagonal. Mean
      aggregation was measured to dilute narrow spike ridges below
      threshold and silently drop whole arm segments — see
      ``experiments/spike_model_grade/``. Values stay in the same SB unit
      (flux fraction per raw detector px), so one absolute threshold works
      at both grades.

    Non-NIRCam files (the 5.0 μm MIRI model rides along in the raw set) are
    skipped. Output names are the flat published namespace:

        SPIKE_MODEL_<SW|LW>_<λ.λλ>UM_<grade>.fits

``manifest``
    Scan a repacked directory, verify both grades exist for every anchor, and
    write ``campfire_pipeline/data/spike_model_manifest.toml`` with sha256 +
    byte sizes and per-entry (channel, wavelength, grade, pixscale) metadata,
    with the public ``base_url`` baked in.

Publish loop (mirrors the wisp flow):
    1. python scripts/build_spike_models.py repack <raw_dir> --out <pub_dir>
    2. Upload <pub_dir> to the public bucket under spike_models/<version>/.
    3. python scripts/build_spike_models.py manifest <pub_dir> \
           --base-url https://<host>/spike_models/<version> --version <version>
    4. Commit the regenerated spike_model_manifest.toml.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

_DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "campfire_pipeline" / "data" / "spike_model_manifest.toml"
)

_RAW_RE = re.compile(r"^PSF\+scatlight_(?P<wave>[0-9.]+)micron\.fits$")
_PUB_RE = re.compile(
    r"^SPIKE_MODEL_(?P<chan>SW|LW)_(?P<wave>[0-9]+\.[0-9]{2})UM_"
    r"(?P<grade>mask|photometric)\.fits$"
)

# Raw-header CHANNEL values → published channel tag. Anything else (MIRI) is
# skipped with a visible note.
_CHANNELS = {"Short": "SW", "Long": "LW"}

# Validated by experiments/spike_model_grade: block-max, uniform x4 factor —
# a 4-native-px cell in each channel (0.124" SW / 0.252" LW), so overshoot
# is ~4 px in each channel's own exposure pixels (the unit `grow` uses).
DEFAULT_MASK_FACTORS = {"SW": 4, "LW": 4}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _block_max(img, f):
    """Center-crop to a multiple of f and block-max. Returns (out, offset)."""
    n = img.shape[0]
    m = (n // f) * f
    o = (n - m) // 2
    c = img[o:o + m, o:o + m]
    return c.reshape(m // f, f, m // f, f).max(axis=(1, 3)), o


def cmd_repack(args):
    import numpy as np
    from astropy.io import fits

    raw = sorted(p for p in args.raw_dir.iterdir() if _RAW_RE.match(p.name))
    if not raw:
        sys.exit(f"no PSF+scatlight_*micron.fits files in {args.raw_dir}")
    args.out.mkdir(parents=True, exist_ok=True)

    written = 0
    for path in raw:
        wave_um = float(_RAW_RE.match(path.name).group("wave"))
        with fits.open(path) as hdul:
            hdr = hdul[0].header
            chan = _CHANNELS.get(str(hdr.get("CHANNEL", "")).strip())
            if chan is None:
                print(f"  skip {path.name}: non-NIRCam "
                      f"(DET_NAME={hdr.get('DET_NAME')})", file=sys.stderr)
                continue
            img = hdul[0].data
        src_sha = _sha256(path)
        n0 = img.shape[0]
        ctr0 = (n0 - 1) / 2.0  # 0-indexed PSF center (odd grids: exact pixel)

        for grade in ("photometric", "mask"):
            if grade == "photometric":
                out_img = img.astype(np.float32)
                pixscl = hdr["PIXELSCL"]
                factor, ctr, agg = 1, ctr0, "none"
            else:
                f = args.mask_factors[chan]
                ds, o = _block_max(img, f)
                out_img = ds.astype(np.float32)
                pixscl = hdr["PIXELSCL"] * f
                factor, ctr, agg = f, (ctr0 - o + 0.5) / f - 0.5, "max"

            out_hdr = hdr.copy()
            out_hdr["EXTNAME"] = "SPIKE_MODEL"
            out_hdr["CFSPKCHN"] = (chan, "NIRCam channel of this model")
            out_hdr["CFSPKWAV"] = (wave_um, "[um] anchor wavelength")
            out_hdr["CFSPKGRD"] = (grade, "model grade: mask | photometric")
            out_hdr["CFSPKDSF"] = (factor, "block downsample factor vs raw")
            out_hdr["CFSPKAGG"] = (agg, "block aggregator: max | none")
            out_hdr["CFSPKCTR"] = (ctr, "[px] PSF center, both axes, 0-indexed")
            out_hdr["PIXELSCL"] = (pixscl, "[arcsec/px] scale of THIS grid")
            out_hdr["PIXAREA0"] = (hdr["PIXELSCL"] ** 2,
                                   "[arcsec2] raw det px area - SB unit basis")
            out_hdr["CFSPKSB"] = ("frac_total_per_raw_px",
                                  "SB unit: flux frac / raw det px")
            out_hdr["CFSPKSRC"] = (path.name, "raw model file")
            out_hdr["CFSPKSHA"] = src_sha  # sha256 of raw file; no room for comment

            name = f"SPIKE_MODEL_{chan}_{wave_um:.2f}UM_{grade}.fits"
            fits.PrimaryHDU(data=out_img, header=out_hdr).writeto(
                args.out / name, overwrite=args.overwrite)
            written += 1
            print(f"  wrote {name}  {out_img.shape[0]}^2 float32 "
                  f"pixscl={pixscl:.4f}", file=sys.stderr)

    print(f"repacked {written} files -> {args.out}", file=sys.stderr)


def cmd_manifest(args):
    files = sorted(p for p in args.pub_dir.iterdir() if _PUB_RE.match(p.name))
    if not files:
        sys.exit(f"no SPIKE_MODEL_*.fits files in {args.pub_dir}")

    from astropy.io import fits

    # Completeness: every (channel, wavelength) must carry both grades — the
    # fetcher hard-fails on a listed-but-missing grade, so catch it at publish.
    grades = {}
    entries = []
    for p in files:
        m = _PUB_RE.match(p.name)
        key = (m.group("chan"), m.group("wave"))
        grades.setdefault(key, set()).add(m.group("grade"))
        hdr = fits.getheader(p, 0)
        print(f"  hashing {p.name} …", file=sys.stderr)
        entries.append(dict(
            name=p.name, sha256=_sha256(p), bytes=p.stat().st_size,
            channel=m.group("chan"), wavelength_um=float(m.group("wave")),
            grade=m.group("grade"), pixscale_arcsec=float(hdr["PIXELSCL"]),
            npix=int(hdr["NAXIS1"]),
        ))

    incomplete = [k for k, g in sorted(grades.items())
                  if g != {"mask", "photometric"}]
    for chan, wave in incomplete:
        print(f"  ERROR: {chan}/{wave}UM missing a grade", file=sys.stderr)
    if incomplete:
        sys.exit("one or more anchors are missing a grade; re-run repack.")

    lines = [
        "# NIRCam spike model manifest — GENERATED by "
        "scripts/build_spike_models.py.",
        "# Do not hand-edit. See the script and pipeline/WISP_TEMPLATE_HOSTING.md.",
        "",
        f'base_url = "{args.base_url.rstrip("/")}"',
        f'version = "{args.version}"',
        "",
    ]
    for e in sorted(entries, key=lambda e: e["name"]):
        lines += [
            "[[model]]",
            f'name            = "{e["name"]}"',
            f'sha256          = "{e["sha256"]}"',
            f'bytes           = {e["bytes"]}',
            f'channel         = "{e["channel"]}"',
            f'wavelength_um   = {e["wavelength_um"]}',
            f'grade           = "{e["grade"]}"',
            f'pixscale_arcsec = {e["pixscale_arcsec"]}',
            f'npix            = {e["npix"]}',
            "",
        ]

    args.out.write_text("\n".join(lines))
    print(f"\nWrote {args.out} — {len(entries)} files across "
          f"{len(grades)} anchors.", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("repack", help="raw WebbPSF set -> two-grade published set")
    rp.add_argument("raw_dir", type=Path)
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--mask-factor-sw", type=int,
                    default=DEFAULT_MASK_FACTORS["SW"],
                    help="mask-grade downsample factor, SW channel "
                         f"(default {DEFAULT_MASK_FACTORS['SW']})")
    rp.add_argument("--mask-factor-lw", type=int,
                    default=DEFAULT_MASK_FACTORS["LW"],
                    help="mask-grade downsample factor, LW channel "
                         f"(default {DEFAULT_MASK_FACTORS['LW']})")
    rp.add_argument("--overwrite", action="store_true")
    rp.set_defaults(func=cmd_repack)

    mf = sub.add_parser("manifest", help="published dir -> committed manifest")
    mf.add_argument("pub_dir", type=Path)
    mf.add_argument("--base-url", required=True,
                    help="public HTTPS prefix (e.g. "
                         "https://host/spike_models/v1)")
    mf.add_argument("--version", required=True)
    mf.add_argument("--out", type=Path, default=_DEFAULT_OUT,
                    help=f"manifest path (default: {_DEFAULT_OUT})")
    mf.set_defaults(func=cmd_manifest)

    args = ap.parse_args(argv)
    if args.cmd == "repack":
        args.mask_factors = {"SW": args.mask_factor_sw,
                             "LW": args.mask_factor_lw}
    args.func(args)


if __name__ == "__main__":
    main()
