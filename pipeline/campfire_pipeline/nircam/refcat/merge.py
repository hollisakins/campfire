"""
Combine multiple reference catalogs with positional dedup.

Catalogs are merged in the order given: the first wins, and each
subsequent catalog contributes only the rows that have no positional
counterpart in the running output. This matches the
``vstack(refcat, hsc_unmatched)`` pattern from the UDS notebook.

A ``source`` column is added to record which input catalog each row
came from, named after the file basename (or a caller-supplied label).
"""

import os

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table, vstack

from campfire_pipeline.common.io import log


def merge_refcats(catalogs, *, labels=None, match_radius=3.0 * u.arcsec):
    """Stack catalogs, keeping only sources with no positional match in
    the earlier catalogs.

    Parameters
    ----------
    catalogs : list of astropy.table.Table
        Two or more refcats with at least ``RA``/``DEC`` columns.
    labels : list of str, optional
        One label per catalog. Defaults to ``['cat1', 'cat2', ...]``.
        Stored in a new ``source`` column on the output.
    match_radius : Quantity (angle)
        Sky-radius dedup tolerance.

    Returns
    -------
    merged : Table
        Single stacked table; the ``source`` column records origin per row.
    info : dict
        Per-input counts (``n_in``, ``n_kept``).
    """
    if len(catalogs) < 2:
        raise ValueError(f"merge_refcats needs >=2 catalogs, got {len(catalogs)}")
    if labels is None:
        labels = [f"cat{i+1}" for i in range(len(catalogs))]
    if len(labels) != len(catalogs):
        raise ValueError("labels must match the number of catalogs")

    pieces = []
    info = {"match_radius_arcsec": match_radius.to(u.arcsec).value,
            "inputs": []}

    base = _with_source(catalogs[0], labels[0])
    pieces.append(base)
    info["inputs"].append({"label": labels[0], "n_in": len(catalogs[0]),
                           "n_kept": len(base)})

    running_coords = SkyCoord(base["RA"], base["DEC"], unit="deg")

    for tab, label in zip(catalogs[1:], labels[1:]):
        n_in = len(tab)
        if n_in == 0:
            log(f"refcat merge: {label} is empty")
            info["inputs"].append({"label": label, "n_in": 0, "n_kept": 0})
            continue
        coords = SkyCoord(tab["RA"], tab["DEC"], unit="deg")
        idx, d2d, _ = coords.match_to_catalog_sky(running_coords)
        unmatched_mask = d2d > match_radius
        kept = _with_source(tab[unmatched_mask], label)
        pieces.append(kept)
        running_coords = SkyCoord(
            np.concatenate([running_coords.ra.deg, kept["RA"]]),
            np.concatenate([running_coords.dec.deg, kept["DEC"]]),
            unit="deg",
        )
        info["inputs"].append(
            {"label": label, "n_in": n_in, "n_kept": int(unmatched_mask.sum())}
        )
        log(f"refcat merge: {label}: {n_in} in, "
            f"{int(unmatched_mask.sum())} unmatched -> kept")

    merged = vstack(pieces, metadata_conflicts="silent")
    info["n_total"] = len(merged)
    return merged, info


def _with_source(tab, label):
    """Return a copy of ``tab`` with a ``source`` column set to ``label``."""
    out = Table(tab, copy=True)
    out["source"] = np.array([label] * len(out), dtype=object)
    return out


def label_from_path(path):
    """Use the basename (without ``.ecsv`` suffix) as the catalog label."""
    return os.path.splitext(os.path.basename(path))[0]
