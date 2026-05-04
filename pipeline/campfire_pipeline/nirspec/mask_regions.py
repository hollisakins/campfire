"""Pure helpers for converting between DS9 ``polygon(...)`` region text and
lists of vertex tuples.

Used by the matplotlib-backed mask editor; importable without any GUI deps.

DS9 ``image`` coords are 1-indexed (pixel center at 1,1). matplotlib (and
numpy/astropy.regions's image-system rasterizer when fed ``image``-header
text) is 0-indexed. The +/-1 offset is applied here on the boundary so a
polygon drawn in editor coords masks the same pixels when the saved .reg
file is later rasterized via ``masks.parse_regions_to_mask``.
"""

from __future__ import annotations

import re
from typing import Iterable


_PIXEL_OFFSET = 1.0

_POLYGON_RE = re.compile(r"polygon\(([^)]+)\)", re.IGNORECASE)


def reg_to_polygons(reg_text: str | None) -> list[list[tuple[float, float]]]:
    """Extract polygons (image coords, **0-indexed**) from a DS9 region string.
    Returns one list of ``(x, y)`` tuples per polygon."""
    if not reg_text:
        return []
    polys: list[list[tuple[float, float]]] = []
    for match in _POLYGON_RE.finditer(reg_text):
        nums = [n.strip() for n in match.group(1).split(",")]
        try:
            vals = [float(n) for n in nums]
        except ValueError:
            continue
        if len(vals) < 6 or len(vals) % 2 != 0:
            continue
        pts = [
            (vals[i] - _PIXEL_OFFSET, vals[i + 1] - _PIXEL_OFFSET)
            for i in range(0, len(vals), 2)
        ]
        polys.append(pts)
    return polys


def polygons_to_reg_text(
    polygons: Iterable[Iterable[tuple[float, float]]],
) -> str:
    """Serialize 0-indexed polygons to DS9 image-coord ``.reg`` text.

    Returns ``""`` if no polygons of >=3 vertices are provided.
    """
    lines: list[str] = []
    for poly in polygons:
        pts = list(poly)
        if len(pts) < 3:
            continue
        coords = []
        for x, y in pts:
            coords.append(f"{x + _PIXEL_OFFSET:g}")
            coords.append(f"{y + _PIXEL_OFFSET:g}")
        lines.append(f"polygon({','.join(coords)})")
    if not lines:
        return ""
    return "image\n" + "\n".join(lines)
