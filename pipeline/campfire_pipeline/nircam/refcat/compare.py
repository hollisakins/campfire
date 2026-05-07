"""
Diagnostic comparison of two refcats.

Reports the ΔRA/ΔDec residual distribution between matched sources
across two catalogs, both as printed summary stats and as an optional
2D histogram plot. Mirrors ``compare_two_catalogs`` from the UDS
notebook.
"""

import os

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord


def _stats(arr):
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "mad": float(np.median(np.abs(arr - np.median(arr)))),
    }


def compare_catalogs(cat_a, cat_b, *, match_radius=0.5 * u.arcsec):
    """Match A→B by sky position; return offsets and summary stats.

    ``cat_a`` and ``cat_b`` are tables with ``RA``/``DEC`` in degrees.

    Returns
    -------
    dict with keys:
        n_matched, dra_mas, ddec_mas, sep_mas,
        dra_stats, ddec_stats, sep_stats,
        match_radius_arcsec
    where ``*_mas`` arrays carry the per-pair residuals in milliarcseconds.
    """
    coords_a = SkyCoord(cat_a["RA"], cat_a["DEC"], unit="deg")
    coords_b = SkyCoord(cat_b["RA"], cat_b["DEC"], unit="deg")
    idx, d2d, _ = coords_a.match_to_catalog_sky(coords_b)
    match = d2d < match_radius
    a_match = coords_a[match]
    b_match = coords_b[idx[match]]

    # Convert to mas, accounting for cos(dec) on RA
    cos_dec = np.cos(np.deg2rad(0.5 * (a_match.dec.deg + b_match.dec.deg)))
    dra_mas = (a_match.ra.deg - b_match.ra.deg) * 3600.0 * 1000.0 * cos_dec
    ddec_mas = (a_match.dec.deg - b_match.dec.deg) * 3600.0 * 1000.0
    sep_mas = d2d[match].to(u.mas).value

    return {
        "n_matched": int(match.sum()),
        "n_a": len(cat_a),
        "n_b": len(cat_b),
        "match_radius_arcsec": match_radius.to(u.arcsec).value,
        "dra_mas": dra_mas,
        "ddec_mas": ddec_mas,
        "sep_mas": sep_mas,
        "dra_stats": _stats(dra_mas) if len(dra_mas) else None,
        "ddec_stats": _stats(ddec_mas) if len(ddec_mas) else None,
        "sep_stats": _stats(sep_mas) if len(sep_mas) else None,
    }


def plot_comparison(result, *, name_a=None, name_b=None,
                    half_extent_mas=500, save_path=None):
    """Render a 2D ΔRA/ΔDec histogram with summary annotations.

    ``result`` is the dict returned by :func:`compare_catalogs`.
    Saves to ``save_path`` if given, else returns the matplotlib Figure.
    """
    import matplotlib as mpl
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5), dpi=150)
    if result["n_matched"] == 0:
        ax.text(0.5, 0.5, "No matches", ha="center", va="center",
                transform=ax.transAxes)
    else:
        # Scale bin count with sqrt(N) per axis so the central peak isn't
        # diluted at low N (Gaia subsamples can be ~10) or overbinned at
        # high N (deep mosaics × full LS DR10 → 10^4+).
        n_bins = int(np.clip(2 * np.sqrt(result["n_matched"]), 8, 80))
        bins = np.linspace(-half_extent_mas, half_extent_mas, n_bins + 1)
        ax.hist2d(result["dra_mas"], result["ddec_mas"], bins=bins,
                  norm=mpl.colors.LogNorm(), cmap="Blues")
        ax.set_aspect("equal")
        ax.axhline(0, color="k", lw=0.5, alpha=0.3)
        ax.axvline(0, color="k", lw=0.5, alpha=0.3)

        s = (
            fr"mean($\Delta$RA) = {result['dra_stats']['mean']:.1f} mas" + "\n"
            fr"med($\Delta$RA)  = {result['dra_stats']['median']:.1f} mas" + "\n"
            fr"MAD($\Delta$RA) = {result['dra_stats']['mad']:.1f} mas"
        )
        ax.annotate(s, (0.05, 0.95), ha="left", va="top",
                    xycoords="axes fraction", fontsize=8,
                    family="monospace")
        s = (
            fr"mean($\Delta$Dec) = {result['ddec_stats']['mean']:.1f} mas" + "\n"
            fr"med($\Delta$Dec)  = {result['ddec_stats']['median']:.1f} mas" + "\n"
            fr"MAD($\Delta$Dec) = {result['ddec_stats']['mad']:.1f} mas"
        )
        ax.annotate(s, (0.05, 0.05), ha="left", va="bottom",
                    xycoords="axes fraction", fontsize=8,
                    family="monospace")

    ax.set_xlabel(r"$\Delta$RA [mas]")
    ax.set_ylabel(r"$\Delta$Dec [mas]")
    title_bits = []
    if name_a or name_b:
        title_bits.append(f"{name_a or 'A'} vs. {name_b or 'B'}")
    title_bits.append(f"N = {result['n_matched']}")
    ax.set_title(" — ".join(title_bits))
    fig.tight_layout()

    if save_path:
        os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".",
                    exist_ok=True)
        fig.savefig(save_path, dpi=150)
        plt.close(fig)
        return None
    return fig
