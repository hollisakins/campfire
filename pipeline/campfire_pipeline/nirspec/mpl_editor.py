"""matplotlib-backed polygon mask editor for NIRSpec rate files.

One window per rate file, opened sequentially. The active polygon lives in a
``PolygonSelector`` (yellow, with draggable vertex handles); committed polygons
are static red ``Polygon`` patches that can be deleted by clicking on them.

Keyboard shortcuts:
    enter       — commit current selector polygon (>=3 verts) and start a new one
    delete      — undo the most recently committed polygon on this frame
    n           — save this frame and advance to next (X-ing the window does the same)
    b           — back: re-open the previous frame
    q           — abort the entire session (no changes written)
"""

from __future__ import annotations

import os
import warnings

import numpy as np
from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval
from astropy.wcs import FITSFixedWarning

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec import masks as masks_mod
from campfire_pipeline.nirspec.mask_regions import (
    polygons_to_reg_text,
    reg_to_polygons,
)


# ---------------------------------------------------------------------------
# Backend bootstrap
# ---------------------------------------------------------------------------

# Pipeline CLI forces ``matplotlib.use('Agg')`` at import for headless plotting.
# The mask editor is interactive, so swap to a GUI backend lazily.
_INTERACTIVE_CANDIDATES = ("macosx", "qtagg", "tkagg")


def _ensure_interactive_backend():
    import matplotlib.pyplot as plt

    current = plt.get_backend().lower()
    if current not in ("agg", "module://matplotlib_inline.backend_inline"):
        return
    last_err = None
    for candidate in _INTERACTIVE_CANDIDATES:
        try:
            plt.switch_backend(candidate)
            return
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        "No interactive matplotlib backend available. Tried "
        f"{_INTERACTIVE_CANDIDATES}. Last error: {last_err}"
    )


# ---------------------------------------------------------------------------
# Per-frame editor
# ---------------------------------------------------------------------------


_HELP = (
    "click vertices to draw  •  click near start (or Enter) to close  •  "
    "drag handles to refine  •  click red polygon to delete  •  "
    "Enter = commit & next polygon  •  Delete = undo last  •  "
    "n = save & next frame  •  b = back  •  q = quit all"
)


def _load_rate(path: str) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FITSFixedWarning)
        with fits.open(path) as hdul:
            return np.asarray(hdul["SCI"].data, dtype=float)


class _FrameEditor:
    """One matplotlib window for one rate file. Returns (action, polygons)."""

    def __init__(self, path: str, existing_reg_text: str, idx: int, total: int):
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPolygon
        from matplotlib.widgets import PolygonSelector

        self._plt = plt
        self._MplPolygon = MplPolygon
        self._PolygonSelector = PolygonSelector

        self.path = path
        self.basename = os.path.basename(path).replace("_rate.fits", "")
        self.completed: list[tuple[list[tuple[float, float]], MplPolygon]] = []
        self.selector: PolygonSelector | None = None
        self.action = "save"  # set by key handlers; window-close keeps default

        data = _load_rate(path)
        self.fig, self.ax = plt.subplots(figsize=(10, 10))
        try:
            self.fig.canvas.manager.set_window_title(
                f"[{idx + 1}/{total}] {self.basename}"
            )
        except AttributeError:
            pass  # some backends lack a manager
        norm = ImageNormalize(data, interval=ZScaleInterval())
        self.ax.imshow(data, origin="lower", cmap="gray", norm=norm)
        self.ax.set_title(self.basename, fontsize=11)
        self.fig.text(0.5, 0.01, _HELP, ha="center", fontsize=8, color="gray",
                      wrap=True)
        self.fig.subplots_adjust(bottom=0.08, top=0.95)

        for verts in reg_to_polygons(existing_reg_text):
            self._add_committed_patch(verts)

        self._new_selector()
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.canvas.mpl_connect("pick_event", self._on_pick)

    # ----- selector lifecycle ------------------------------------------------

    def _new_selector(self):
        # Replace any prior selector with a fresh one. We don't auto-commit on
        # PolygonSelector.onselect because that callback fires on every vertex
        # drag after the polygon closes; user presses Enter to explicitly
        # commit instead.
        if self.selector is not None:
            self.selector.set_visible(False)
            self.selector.disconnect_events()
        self.selector = self._PolygonSelector(
            self.ax,
            onselect=lambda verts: None,
            useblit=True,
            props=dict(color="yellow", linewidth=1.5),
            handle_props=dict(markersize=6, markerfacecolor="yellow",
                              markeredgecolor="yellow"),
        )

    # ----- polygon ops -------------------------------------------------------

    def _add_committed_patch(self, verts):
        patch = self._MplPolygon(
            verts, closed=True, fill=False,
            edgecolor="red", linewidth=1.5, picker=5,
        )
        self.ax.add_patch(patch)
        self.completed.append((list(verts), patch))

    def _commit_current(self) -> bool:
        if self.selector is None:
            return False
        verts = list(self.selector.verts)
        if len(verts) < 3:
            return False
        self._add_committed_patch(verts)
        self._new_selector()
        self.fig.canvas.draw_idle()
        return True

    # ----- event handlers ----------------------------------------------------

    def _on_pick(self, event):
        for i, (_, patch) in enumerate(self.completed):
            if patch is event.artist:
                patch.remove()
                self.completed.pop(i)
                self.fig.canvas.draw_idle()
                return

    def _on_key(self, event):
        k = event.key
        if k == "enter":
            self._commit_current()
        elif k == "n":
            self._commit_current()
            self.action = "save"
            self._plt.close(self.fig)
        elif k == "b":
            self.action = "back"
            self._plt.close(self.fig)
        elif k == "q":
            self.action = "quit"
            self._plt.close(self.fig)
        elif k == "delete":
            if self.completed:
                _, patch = self.completed.pop()
                patch.remove()
                self.fig.canvas.draw_idle()

    # ----- run loop ----------------------------------------------------------

    def run(self) -> tuple[str, list[list[tuple[float, float]]]]:
        self._plt.show(block=True)
        return self.action, [verts for verts, _ in self.completed]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_editor(
    rate_files: list[str],
    existing_regions: dict[str, str],
) -> dict[str, str | None] | None:
    """Iterate through ``rate_files`` and let the user edit polygon masks.

    Parameters
    ----------
    rate_files
        Absolute paths to ``*_rate.fits`` files.
    existing_regions
        Map of rate basename (without ``_rate.fits``) → existing DS9 region
        text. Missing keys are treated as empty.

    Returns
    -------
    dict or None
        ``{basename: reg_text or None}`` with one entry per file the user
        visited. ``None`` if the user pressed ``q`` to abort.
    """
    _ensure_interactive_backend()

    import matplotlib.pyplot as plt

    # Disable matplotlib's default keymaps that would otherwise intercept our
    # editor shortcuts: 's' (save dialog), backspace/left/c (view-stack back),
    # right/v (view-stack forward).
    rc_overrides = {
        "keymap.save": [],
        "keymap.back": [],
        "keymap.forward": [],
    }

    results: dict[str, str | None] = {}
    i = 0
    n = len(rate_files)
    with plt.rc_context(rc_overrides):
        while 0 <= i < n:
            path = rate_files[i]
            basename = os.path.basename(path).replace("_rate.fits", "")
            # On revisit (via 'b'), show the in-session edits, not the on-disk ones.
            existing = results.get(basename, existing_regions.get(basename, ""))
            editor = _FrameEditor(path, existing, i, n)
            action, polys = editor.run()
            if action == "quit":
                return None
            if action == "back":
                i = max(0, i - 1)
                continue
            # "save" — record current state and advance
            reg = polygons_to_reg_text(polys)
            results[basename] = reg if reg else None
            i += 1
    return results


def edit_masks_in_matplotlib(
    obs,
    observations_file: str,
    *,
    exposure: str | None = None,
) -> bool:
    """Run the matplotlib editor and write results back to observations.toml.

    Returns True if a write occurred (user saved at least one frame), False
    if cancelled or no rate files were available.
    """
    if not obs.rate_files:
        log(f"No rate files found for {obs.name}. Run `cfpipe nirspec stage1` first.")
        return False

    if exposure is not None:
        rate_files = [
            p for p in obs.rate_files
            if os.path.basename(p).replace("_rate.fits", "") == exposure
        ]
        if not rate_files:
            log(f"No rate file matching --exposure {exposure} in workspace.")
            return False
    else:
        rate_files = list(obs.rate_files)

    masks_mod.materialize_reg_files(obs)
    existing = dict(obs.manual_masks or {})

    log(f"Opening matplotlib mask editor ({len(rate_files)} frame(s))…")
    result = run_editor(rate_files, existing)

    if result is None:
        log("Editor aborted; no changes written.")
        return False

    new_masks: dict[str, str | None] = {}
    for basename, reg_text in result.items():
        canon = masks_mod.canonicalize(reg_text or "")
        if canon:
            new_masks[basename] = canon
            masks_mod.write_reg_file(
                masks_mod.workspace_reg_path(obs, basename), canon
            )
        else:
            new_masks[basename] = None

    masks_mod.write_masks_to_observations_toml(
        observations_file, obs.name, new_masks
    )

    n_set = sum(1 for v in new_masks.values() if v)
    n_cleared = sum(1 for v in new_masks.values() if not v)
    log(
        f"Wrote {n_set} mask(s) to {observations_file}"
        + (f", cleared {n_cleared}" if n_cleared else "")
        + f". Run `cfpipe nirspec mask apply --obs {obs.name}` to update rate files."
    )
    return True
