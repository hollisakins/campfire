#!/usr/bin/env python
"""Generate the golden cutout-plan fixture for the TS parity test (epic #337, Phase 5).

Runs `fitsgl.cutout.plan_cutout` (the Python producer's read API) over a set of
cases against a real band manifest and writes:
  - `<band>-manifest.json`  — a copy of the manifest the TS port reads.
  - `plan-cases.json`       — each case's inputs + the plan fitsgl-py produced.

The TS arm (`web/lib/cutout/plan.test.ts`) asserts `planCutout(...)` reproduces
every case, so the port can never drift from fitsgl-py / the browser.

Run in the `campfire` conda env (has `fitsgl` editable-installed):
    conda run -n campfire python web/lib/cutout/__fixtures__/generate_plan_fixture.py \
        /path/to/rj0911__venus/f444w/manifest.json
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from fitsgl import read_manifest, plan_cutout

HERE = Path(__file__).resolve().parent
BAND_TAG = "rj0911-f444w"

# (name, center[ra,dec], fov_arcsec, output_size|None, target_scale|None, rounding)
CASES = [
    ("on_source_small", (137.788282, 17.782160), 6.0, 512, None, "nearest"),
    ("on_source_mid", (137.788282, 17.782160), 30.0, 512, None, "nearest"),
    ("on_source_wide", (137.788282, 17.782160), 120.0, 400, None, "nearest"),
    ("center_gap", (137.805142, 17.799882), 20.0, 256, None, "nearest"),
    ("anisotropic", (137.788282, 17.782160), (60.0, 20.0), (600, 200), None, "nearest"),
    ("target_scale", (137.788282, 17.782160), 30.0, None, 0.06, "nearest"),
    ("rounding_finer", (137.788282, 17.782160), 30.0, 300, None, "finer"),
    ("rounding_coarser", (137.788282, 17.782160), 30.0, 300, None, "coarser"),
    ("native_no_hint", (137.788282, 17.782160), 4.0, None, None, "nearest"),
    ("out_of_field", (200.0, -10.0), 20.0, 256, None, "nearest"),
]


def plan_to_json(plan) -> dict:
    return {
        "levelIndex": plan.level_index,
        "outputScaleArcsec": plan.output_scale_arcsec,
        "bbox": {"x0": plan.pixel_bbox.x0, "y0": plan.pixel_bbox.y0, "x1": plan.pixel_bbox.x1, "y1": plan.pixel_bbox.y1},
        "tiles": [
            {"tileX": t.tile.tile_x, "tileY": t.tile.tile_y, "supertileIndex": t.supertile_index,
             "localX": t.local_x, "localY": t.local_y}
            for t in plan.tiles
        ],
        "missing": [{"tileX": c.tile_x, "tileY": c.tile_y} for c in plan.missing],
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    manifest_path = Path(sys.argv[1])
    shutil.copyfile(manifest_path, HERE / f"{BAND_TAG}-manifest.json")
    manifest = read_manifest(str(manifest_path))

    cases_out = []
    for name, center, fov, output_size, target_scale, rounding in CASES:
        plan = plan_cutout(
            manifest, center=center, fov=fov,
            output_size=output_size, target_scale_arcsec=target_scale, rounding=rounding,
        )
        cases_out.append({
            "name": name, "center": list(center), "fov": fov,
            "outputSize": output_size, "targetScaleArcsec": target_scale, "rounding": rounding,
            "expected": plan_to_json(plan),
        })

    (HERE / "plan-cases.json").write_text(json.dumps(cases_out, indent=2) + "\n")
    print(f"wrote {len(cases_out)} cases + manifest to {HERE}")


if __name__ == "__main__":
    main()
