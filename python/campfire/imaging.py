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

# Default shutter colors
DEFAULT_SHUTTER_COLORS: Dict[str, Dict[str, Union[str, float, Tuple]]] = {
    "current": {
        "facecolor": (0, 1, 0, 0.2),
        "edgecolor": "#00ff00",
        "linewidth": 1.0,
        "linestyle": "-",
    },
    "other": {
        "facecolor": (0.67, 0.67, 0.67, 0.15),
        "edgecolor": "#cccccc",
        "linewidth": 0.8,
        "linestyle": "-",
    },
    "stuck_closed": {
        "facecolor": "none",
        "edgecolor": "#ef4444",
        "linewidth": 1.5,
        "linestyle": "--",
    },
}


def plot_cutout(
    image_path: Union[str, Path],
    shutters: Optional[Union[List[dict], dict]] = None,
    object_id: Optional[str] = None,
    fov: float = 5.0,
    center_ra: Optional[float] = None,
    center_dec: Optional[float] = None,
    ax=None,
    shutter_colors: Optional[Dict[str, dict]] = None,
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
    shutter_colors : dict, optional
        Custom color mapping. Keys: ``'current'``, ``'other'``,
        ``'stuck_closed'``. Each value is a dict with matplotlib patch
        kwargs (facecolor, edgecolor, linewidth, linestyle).
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

    >>> # Or with shutters (pass full get_shutters() result):
    >>> result = cf.get_shutters('cosmos_ddt_66964', fov=3.2)
    >>> plot_cutout(path, shutters=result, object_id='cosmos_ddt_66964', fov=3.2, ax=ax)
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.image as mpimg
        import matplotlib.patches as mpatches
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
        colors = {**DEFAULT_SHUTTER_COLORS, **(shutter_colors or {})}
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

            is_current = object_id and shutter.get("object_id") == object_id
            is_stuck = shutter.get("shutter_state") == "stuck_closed"

            if is_stuck:
                style = colors["stuck_closed"]
            elif is_current:
                style = colors["current"]
            else:
                style = colors["other"]

            # SVG uses rotate(-PA) with +Y down; matplotlib has +Y up,
            # so the sign flips to +PA
            angle = shutter["position_angle"]

            rect = mpatches.Rectangle(
                (cx - SHUTTER_WIDTH_ARCSEC / 2, cy - SHUTTER_HEIGHT_ARCSEC / 2),
                SHUTTER_WIDTH_ARCSEC,
                SHUTTER_HEIGHT_ARCSEC,
                angle=angle,
                rotation_point="center",
                facecolor=style["facecolor"],
                edgecolor=style["edgecolor"],
                linewidth=style["linewidth"],
                linestyle=style["linestyle"],
            )
            ax.add_patch(rect)

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
