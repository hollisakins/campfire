"""
Imaging utilities for CAMPFIRE NIRCam cutouts with shutter overlays.

Uses matplotlib for vector rendering of shutter geometry over raster cutouts.
Produces publication-quality figures that can be exported as PDF or high-DPI PNG.

Usage
-----
>>> import matplotlib.pyplot as plt
>>> from campfire import Campfire
>>> from campfire.imaging import plot_cutout
>>>
>>> cf = Campfire()
>>> path = cf.get_cutout('cosmos_ddt_66964', fov=3.2)
>>> result = cf.get_shutters('cosmos_ddt_66964', fov=3.2)
>>>
>>> fig, ax = plt.subplots(figsize=(5, 5))
>>> plot_cutout(path, shutters=result, object_id='cosmos_ddt_66964', fov=3.2, ax=ax)
>>> fig.savefig('cutout.pdf')  # vector output
"""

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# NIRSpec shutter dimensions
SHUTTER_WIDTH_ARCSEC = 0.22
SHUTTER_HEIGHT_ARCSEC = 0.46

# Default shutter style per category.
# "marker" controls shape: "box" (full rectangle) or "corners" (L-shaped corner marks).
DEFAULT_SHUTTER_STYLE: Dict[str, Dict[str, Union[str, float, Tuple]]] = {
    "target": {
        "facecolor": (0, 1, 0, 0.2),
        "edgecolor": "#00ff00",
        "linewidth": 1.0,
        "linestyle": "-",
        "marker": "box",
    },
    "other": {
        "facecolor": (0.67, 0.67, 0.67, 0.15),
        "edgecolor": "#cccccc",
        "linewidth": 0.8,
        "linestyle": "-",
        "marker": "box",
    },
    "stuck_closed": {
        "facecolor": "none",
        "edgecolor": "#ef4444",
        "linewidth": 1.5,
        "linestyle": "--",
        "marker": "box",
    },
}


def _draw_shutter_box(ax, cx, cy, w, h, angle, style):
    """Draw a full shutter rectangle."""
    import matplotlib.patches as mpatches

    patch_style = {k: v for k, v in style.items() if k != "marker"}
    rect = mpatches.Rectangle(
        (cx - w / 2, cy - h / 2), w, h,
        angle=angle,
        rotation_point="center",
        **patch_style,
    )
    ax.add_patch(rect)


def _draw_shutter_corners(ax, cx, cy, w, h, angle, style):
    """Draw L-shaped corner marks at the four corners of a shutter."""
    import numpy as np

    # Corner tick length as fraction of shutter dimensions
    tw = w * 0.35
    th = h * 0.25

    # Four corners (unrotated, centered at origin)
    corners = [
        # (corner_x, corner_y, dx_tick, dy_tick) — two line segments per corner
        (-w/2, -h/2, (tw, 0), (0, th)),    # bottom-left
        ( w/2, -h/2, (-tw, 0), (0, th)),   # bottom-right
        (-w/2,  h/2, (tw, 0), (0, -th)),   # top-left
        ( w/2,  h/2, (-tw, 0), (0, -th)),  # top-right
    ]

    rad = np.radians(angle)
    cos_a, sin_a = np.cos(rad), np.sin(rad)

    color = style.get("edgecolor", "white")
    lw = style.get("linewidth", 1.0)

    for corner_x, corner_y, (dx1, dy1), (dx2, dy2) in corners:
        # Rotate corner and tick endpoints around center
        def rot(x, y):
            return cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a

        x0, y0 = rot(corner_x, corner_y)
        x1, y1 = rot(corner_x + dx1, corner_y + dy1)
        x2, y2 = rot(corner_x + dx2, corner_y + dy2)

        ax.plot([x1, x0, x2], [y1, y0, y2], color=color, linewidth=lw,
                solid_capstyle="butt", solid_joinstyle="miter")


def plot_cutout(
    image_path: Union[str, Path],
    shutters: Optional[Union[List[dict], dict]] = None,
    object_id: Optional[str] = None,
    fov: float = 5.0,
    center_ra: Optional[float] = None,
    center_dec: Optional[float] = None,
    ax=None,
    shutter_style: Optional[Dict[str, dict]] = None,
    scalebar: bool = True,
    scalebar_length: Optional[float] = None,
):
    """Plot a cutout image with optional vector shutter overlay.

    Plots onto the provided axes, or ``plt.gca()`` if none given.
    The caller is responsible for figure creation and display.

    Parameters
    ----------
    image_path : str or Path
        Path to the PNG cutout image (from ``Campfire.get_cutout()``).
    shutters : list of dict or dict, optional
        Either the full result dict from ``Campfire.get_shutters()``
        (with ``shutters`` and ``meta`` keys — center_ra/center_dec
        are extracted automatically), or a plain list of shutter dicts.
    object_id : str, optional
        Current object ID. This object's shutters are highlighted.
    fov : float, optional
        Field of view in arcseconds (must match the cutout). Default 5.
    center_ra : float, optional
        RA of the cutout center in degrees. Auto-extracted from
        ``shutters['meta']`` if the full result dict is passed.
    center_dec : float, optional
        Dec of the cutout center in degrees. Auto-extracted from
        ``shutters['meta']`` if the full result dict is passed.
    ax : matplotlib.axes.Axes, optional
        Axes to plot on. If None, uses ``plt.gca()``.
    shutter_style : dict, optional
        Per-category style overrides. Keys: ``'target'``, ``'other'``,
        ``'stuck_closed'``. Values are dicts with any of: ``facecolor``,
        ``edgecolor``, ``linewidth``, ``linestyle``, ``marker``
        (``'box'`` or ``'corners'``). Partial overrides are merged with
        defaults.
    scalebar : bool, optional
        Draw a scalebar (default True).
    scalebar_length : float, optional
        Scalebar length in arcseconds. Defaults to a round value ~1/5 of FOV.

    Returns
    -------
    matplotlib.axes.Axes
        The axes with the plot.

    Examples
    --------
    >>> import matplotlib.pyplot as plt
    >>> fig, ax = plt.subplots(figsize=(5, 5))
    >>> plot_cutout('cutout.png', fov=3.2, ax=ax)
    >>> fig.savefig('figure.pdf')

    >>> # Corner markers instead of full boxes:
    >>> plot_cutout(path, shutters=result, object_id='obj', fov=3.2, ax=ax,
    ...            shutter_style={"target": {"marker": "corners"}})
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.image as mpimg
    except ImportError as exc:
        raise ImportError(
            "plot_cutout requires matplotlib. "
            "Install it with: pip install matplotlib"
        ) from exc

    # Unpack full get_shutters() result dict if passed
    shutter_list: Optional[List[dict]] = None
    if isinstance(shutters, dict) and "shutters" in shutters:
        meta = shutters.get("meta", {})
        if center_ra is None:
            center_ra = meta.get("center_ra")
        if center_dec is None:
            center_dec = meta.get("center_dec")
        shutter_list = shutters["shutters"]
    elif isinstance(shutters, list):
        shutter_list = shutters

    image = mpimg.imread(str(image_path))
    half = fov / 2

    if ax is None:
        ax = plt.gca()

    # Display image with arcsecond extent (0,0) at center
    ax.imshow(
        image,
        extent=[-half, half, -half, half],
        origin="upper",
        interpolation="nearest",
    )

    # Render shutters as vector patches
    if shutter_list and center_ra is not None and center_dec is not None:
        styles = {
            k: {**v, **(shutter_style or {}).get(k, {})}
            for k, v in DEFAULT_SHUTTER_STYLE.items()
        }
        cos_dec = math.cos(math.radians(center_dec))

        for shutter in shutter_list:
            # Offset from center in arcseconds
            dra = (shutter["center_ra"] - center_ra) * cos_dec * 3600
            ddec = (shutter["center_dec"] - center_dec) * 3600

            # Pixel coordinates: +X = East = -RA, +Y = up = +Dec
            cx = -dra
            cy = ddec

            # Skip if outside FOV (with padding)
            if abs(cx) > half + 1 or abs(cy) > half + 1:
                continue

            is_target = object_id and shutter.get("object_id") == object_id
            is_stuck = shutter.get("shutter_state") == "stuck_closed"

            if is_stuck:
                style = styles["stuck_closed"]
            elif is_target:
                style = styles["target"]
            else:
                style = styles["other"]

            # SVG uses rotate(-PA) with +Y down; matplotlib has +Y up,
            # so the sign flips to +PA
            angle = shutter["position_angle"]

            marker = style.get("marker", "box")
            if marker == "corners":
                _draw_shutter_corners(ax, cx, cy,
                                      SHUTTER_WIDTH_ARCSEC, SHUTTER_HEIGHT_ARCSEC,
                                      angle, style)
            else:
                _draw_shutter_box(ax, cx, cy,
                                  SHUTTER_WIDTH_ARCSEC, SHUTTER_HEIGHT_ARCSEC,
                                  angle, style)

    # Draw scalebar
    if scalebar:
        _draw_scalebar(ax, fov, scalebar_length)

    ax.set_xlim(-half, half)
    ax.set_ylim(-half, half)
    ax.set_aspect("equal")
    ax.axis("off")

    return ax


def _draw_scalebar(ax, fov: float, length: Optional[float] = None):
    """Draw a scalebar in the lower-right corner."""
    import matplotlib.patheffects as pe

    if length is None:
        # Pick a round value ~1/5 of FOV
        target = fov / 5
        for candidate in [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]:
            if candidate >= target * 0.5:
                length = candidate
                break
        else:
            length = target

    half = fov / 2
    margin = fov * 0.05
    y = -half + margin
    x_right = half - margin
    x_left = x_right - length

    ax.plot(
        [x_left, x_right], [y, y],
        color="white", linewidth=2, solid_capstyle="butt",
    )
    # Endcaps
    cap_h = fov * 0.015
    for x in [x_left, x_right]:
        ax.plot(
            [x, x], [y - cap_h, y + cap_h],
            color="white", linewidth=1.5, solid_capstyle="butt",
        )

    label = f'{length:.1f}"' if length < 1 else f'{length:.0f}"'
    ax.text(
        (x_left + x_right) / 2, y + fov * 0.03, label,
        color="white", fontsize=8, ha="center", va="bottom",
        fontweight="bold",
        path_effects=[pe.withStroke(linewidth=2, foreground="black")],
    )
