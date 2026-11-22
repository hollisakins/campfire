#!/usr/bin/env python
"""Size a mosaic tile to cover a field's exposures and print its fields.toml block.

Reads the ``S_REGION`` footprint of every uncal the field selects (its ``files``
globs and ``skip`` list, the same selection ``cfpipe nircam process`` uses),
projects all footprint vertices onto a TAN grid at the field's tangent point
with the requested rotation, and returns the smallest ``crpix`` / ``naxis`` that
covers them plus a margin. Uses the same WCS convention as
``Field._compute_corners_from_wcs``: ``rotation`` is the position angle of the
output +y axis, east of north, and ``crpix`` is FITS 1-indexed.

    python .claude/skills/nircam-reduction/size_tile.py --field nexus --tile main
    python .claude/skills/nircam-reduction/size_tile.py --field nexus --tile main \
        --rotation 20 --margin 5 --filters f444w

Needs the raw uncals on disk (``cfpipe download``) and the field in fields.toml.
"""

import argparse
import math

import numpy as np
from astropy.wcs import WCS

from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.geometry import read_sregion_polygon

GPIX_WARN = 0.5  # tiles above ~0.5 Gpix need a very large node to resample


def tan_wcs(crval, rotation_deg, scale_arcsec, crpix):
    s = scale_arcsec / 3600.0
    t = math.radians(rotation_deg)
    w = WCS(naxis=2)
    w.wcs.crpix = crpix
    w.wcs.crval = crval
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cd = np.array([[-s * math.cos(t), s * math.sin(t)],
                         [s * math.sin(t), s * math.cos(t)]])
    return w


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--field", required=True)
    ap.add_argument("--tile", default="main", help="tile name for the printed block")
    ap.add_argument("--rotation", type=float, default=0.0,
                    help="PA of the output +y axis, degrees east of north")
    ap.add_argument("--scale", type=int, default=30, help="pixel scale in mas")
    ap.add_argument("--margin", type=float, default=5.0, help="margin in arcsec")
    ap.add_argument("--filters", nargs="*", help="default: the field's filters")
    ap.add_argument("--tangent-point", nargs=2, type=float, metavar=("RA", "DEC"),
                    help="default: the field's tangent_point")
    ap.add_argument("--fields-file", default=None)
    a = ap.parse_args()

    field = Field.load(a.field, fields_file=a.fields_file)
    # Only what get_uncal_files needs; setup_workspace() would also create the
    # field's product and reference directories, which a read-only helper shouldn't.
    from campfire_layout import raw_dir
    from campfire_pipeline.config import _get_campfire_root
    field.raw_root = str(raw_dir("nircam", root=_get_campfire_root()))
    field.excluded_exposures = {}
    crval = list(a.tangent_point) if a.tangent_point else list(field.tangent_point)

    ra, dec, n_files, n_missing = [], [], 0, 0
    for flt in a.filters or field.filters:
        for path in field.get_uncal_files(flt):
            n_files += 1
            poly = read_sregion_polygon(path)
            if poly is None:
                n_missing += 1
                continue
            xs, ys = poly.exterior.coords.xy
            ra.extend(xs)
            dec.extend(ys)
    if not ra:
        raise SystemExit(f"no footprints found for {a.field} ({n_files} uncals selected); "
                         "download the raw data first")

    # Pixel coordinates relative to crpix = 0, then shift so the box starts at pixel 1.
    w = tan_wcs(crval, a.rotation, a.scale / 1000.0, [0.0, 0.0])
    x, y = w.all_world2pix(np.array(ra), np.array(dec), 1)
    m = a.margin / (a.scale / 1000.0)
    x0, x1 = math.floor(x.min() - m), math.ceil(x.max() + m)
    y0, y1 = math.floor(y.min() - m), math.ceil(y.max() + m)
    crpix = [1 - x0, 1 - y0]
    naxis = [x1 - x0 + 1, y1 - y0 + 1]
    gpix = naxis[0] * naxis[1] / 1e9

    print(f"# {n_files} uncals ({n_missing} without S_REGION), margin {a.margin}\", "
          f"{naxis[0]} x {naxis[1]} px = {gpix:.2f} Gpix at {a.scale} mas")
    if gpix > GPIX_WARN:
        print(f"# WARNING: above {GPIX_WARN} Gpix; consider splitting into a grid of tiles")
    print(f"[{a.field}.{a.tile}]")
    print(f"    rotation = {a.rotation:g}")
    if a.tangent_point:
        print(f"    tangent_point = [{crval[0]}, {crval[1]}]")
    print(f"    [{a.field}.{a.tile}.{a.scale}mas]")
    print(f"        crpix = [{crpix[0]}, {crpix[1]}]")
    print(f"        naxis = [{naxis[0]}, {naxis[1]}]")


if __name__ == "__main__":
    main()
