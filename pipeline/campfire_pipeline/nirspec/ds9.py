"""
DS9/XPA launcher for editing manual NIRSpec masks.

DS9 itself is a *soft* dependency. If ``ds9`` or ``xpaset`` is not on PATH,
``edit_masks_in_ds9`` prints manual-edit instructions and returns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec import masks as masks_mod


_XPA_TITLE_PREFIX = "campfire-mask"


def _have(prog: str) -> bool:
    return shutil.which(prog) is not None


def _xpa_title(obs_name: str) -> str:
    return f"{_XPA_TITLE_PREFIX}-{obs_name}"


def _xpaset(title: str, *args: str, stdin: str | None = None) -> None:
    cmd = ["xpaset", "-p", title, *args]
    if stdin is not None:
        subprocess.run(cmd[:2] + [title] + list(args), input=stdin, text=True, check=True)
    else:
        subprocess.run(cmd, check=True)


def _xpaget(title: str, *args: str) -> str:
    cmd = ["xpaget", title, *args]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def _wait_for_xpa(title: str, timeout: float = 30.0) -> bool:
    """Poll xpaaccess until the named DS9 instance is responsive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["xpaaccess", title],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "yes":
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        time.sleep(0.5)
    return False


def _print_manual_instructions(obs, rate_files: list[str]) -> None:
    log("DS9 / XPA not available. To edit masks manually:")
    log("")
    log("  1. Open each rate file in DS9 (or any image viewer):")
    for rf in rate_files:
        log(f"       {rf}")
    log("")
    log("  2. Draw polygons in IMAGE coordinates around bad regions.")
    log("")
    log("  3. Save each as a DS9 .reg file with content like:")
    log("         image")
    log("         polygon(x1,y1,x2,y2,...)")
    log("")
    log(f"  4. Paste each .reg body into observations.toml under "
        f"[{obs.name}.masks] keyed by the rate basename "
        f"(e.g. \"jw01234001001_nrs1\" = '''image\\npolygon(...)\\n''').")
    log("")
    log(f"  5. Run `cfpipe nirspec mask apply --obs {obs.name}`.")


def edit_masks_in_ds9(obs, observations_file: str, exposure: str | None = None) -> None:
    """Launch DS9 with rate files as frames, capture user-drawn polygons,
    write them back to observations.toml.

    Parameters
    ----------
    obs : Observation
        Already loaded via ``Observation.load`` and workspace-set-up.
    observations_file : str
        Path to observations.toml (for round-trip writes).
    exposure : str, optional
        Rate file basename (without ``_rate.fits``) to restrict editing to a
        single exposure. If None, all rate files are loaded.
    """
    if not obs.rate_files:
        log(f"No rate files found for {obs.name}. Run `cfpipe nirspec stage1` first.")
        return

    if exposure is not None:
        rate_files = [
            p for p in obs.rate_files
            if os.path.basename(p).replace("_rate.fits", "") == exposure
        ]
        if not rate_files:
            log(f"No rate file matching --exposure {exposure} in workspace.")
            return
    else:
        rate_files = list(obs.rate_files)

    # Materialize existing reg-file mirrors for DS9 to load.
    masks_mod.materialize_reg_files(obs)

    if not (_have("ds9") and _have("xpaset") and _have("xpaget")):
        _print_manual_instructions(obs, rate_files)
        return

    title = _xpa_title(obs.name)

    # Launch DS9 in the background.
    log(f"Launching DS9 (XPA title: {title})…")
    proc = subprocess.Popen(
        ["ds9", "-title", title, "-scale", "log", "-scale", "mode", "zscale"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    if not _wait_for_xpa(title):
        log("ERROR: DS9 did not become responsive via XPA within 30s.")
        proc.terminate()
        return

    try:
        # Load each rate file as its own frame.
        for i, rate_file in enumerate(rate_files):
            if i > 0:
                _xpaset(title, "frame", "new")
            _xpaset(title, "file", rate_file)
            _xpaset(title, "scale", "log")
            _xpaset(title, "scale", "mode", "zscale")
            _xpaset(title, "regions", "system", "image")

            basename = os.path.basename(rate_file).replace("_rate.fits", "")
            reg_path = masks_mod.workspace_reg_path(obs, basename)
            if os.path.exists(reg_path):
                _xpaset(title, "regions", "load", reg_path)

        log("")
        log(f"Edit polygon regions in DS9 (image coords).")
        log(f"  Loaded {len(rate_files)} rate file(s) as frames.")
        log(f"  Use frame arrows to cycle exposures.")
        log("")
        try:
            input("Press Enter when done (Ctrl-C to abort without saving)... ")
        except (KeyboardInterrupt, EOFError):
            log("Aborted; no changes written.")
            return

        # Capture regions per frame and write back to TOML.
        new_masks: dict[str, str | None] = {}
        for i, rate_file in enumerate(rate_files):
            _xpaset(title, "frame", "frameno", str(i + 1))
            reg_text = _xpaget(title, "regions", "-format", "ds9", "-system", "image")
            basename = os.path.basename(rate_file).replace("_rate.fits", "")
            canon = masks_mod.canonicalize(reg_text)
            if canon:
                new_masks[basename] = canon
                # Refresh workspace mirror so it reflects what was just drawn.
                masks_mod.write_reg_file(masks_mod.workspace_reg_path(obs, basename), canon)
            else:
                # User cleared regions for this exposure → mark for deletion.
                new_masks[basename] = None

        masks_mod.write_masks_to_observations_toml(observations_file, obs.name, new_masks)

        n_set = sum(1 for v in new_masks.values() if v)
        n_cleared = sum(1 for v in new_masks.values() if not v)
        log(
            f"Wrote {n_set} mask(s) to {observations_file}"
            + (f", cleared {n_cleared}" if n_cleared else "")
            + f". Run `cfpipe nirspec mask apply --obs {obs.name}` to update rate files."
        )
    finally:
        # Leave DS9 running so the user can keep inspecting.
        pass
