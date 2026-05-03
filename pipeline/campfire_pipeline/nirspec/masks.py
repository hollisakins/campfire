"""
Manual masking of NIRSpec rate files via DS9 polygon regions.

Region strings (DS9 .reg format, image coords) are stored inline in
observations.toml. At stage1 phase 2 they are OR'd as ``DO_NOT_USE`` into
the rate-file DQ array before bkg subtraction so that masked pixels are
excluded from the bkg fit.

Re-running with edited masks restores the pre-bkg-sub rate state from the
``CFBKG`` extension stamped into the rate file by the previous bkg sub,
plus the ``CFBKGRMS`` header keyword (variance rescale factor), avoiding
the need for a duplicate rate-file backup. ``CFBKGSUB`` in the primary
header is the single source of truth for whether bkg sub has run.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log


DO_NOT_USE = 1  # stdatamodels.dqflags.pixel['DO_NOT_USE']


# ---------------------------------------------------------------------------
# Region string parsing & hashing
# ---------------------------------------------------------------------------


def canonicalize(reg_string: str | None) -> str:
    """Return a normalized region string for stable hashing/comparison.

    Strips comment lines, blank lines, and trailing whitespace. Preserves
    the coordinate-system declaration (e.g. ``image``) and the polygon
    bodies. Returns ``''`` for ``None`` or whitespace-only input.
    """
    if not reg_string or not reg_string.strip():
        return ""
    lines = []
    for raw in reg_string.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def hash_mask(reg_string: str | None) -> str:
    """12-char sha256 prefix of the canonical region string. ``''`` if empty."""
    canon = canonicalize(reg_string)
    if not canon:
        return ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def parse_regions_to_mask(reg_string: str, shape: tuple[int, int]) -> np.ndarray:
    """Parse a DS9 region string and rasterize it onto a 2D bool mask.

    True pixels are inside any polygon. Polygons must be in image coords
    (rate files have no useful sky WCS).
    """
    from regions import Regions

    canon = canonicalize(reg_string)
    if not canon:
        return np.zeros(shape, dtype=bool)

    # ``Regions.parse`` expects a coord-system header line; assume image if absent.
    if not any(line.lower() in ("image", "physical") for line in canon.splitlines()):
        canon = "image\n" + canon

    regs = Regions.parse(canon, format="ds9")
    mask = np.zeros(shape, dtype=bool)
    for region in regs:
        try:
            rmask = region.to_mask().to_image(shape)
        except Exception as exc:
            log(f"WARNING: failed to rasterize region {region}: {exc}")
            continue
        if rmask is None:
            continue
        mask |= rmask.astype(bool)
    return mask


# ---------------------------------------------------------------------------
# Workspace .reg mirror
# ---------------------------------------------------------------------------


def workspace_masks_dir(obs) -> str:
    return os.path.join(obs.workspace_dir, "manual_masks")


def workspace_reg_path(obs, rate_basename: str) -> str:
    return os.path.join(workspace_masks_dir(obs), f"{rate_basename}.reg")


def write_reg_file(path: str, reg_string: str) -> None:
    """Write a DS9 region string to ``path``, ensuring an ``image`` header line."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    canon = canonicalize(reg_string)
    if canon and not any(line.lower() in ("image", "physical") for line in canon.splitlines()):
        canon = "image\n" + canon
    with open(path, "w") as f:
        f.write("# Region file format: DS9 (campfire manual mask)\n")
        f.write(canon)
        if canon and not canon.endswith("\n"):
            f.write("\n")


def materialize_reg_files(obs) -> dict[str, str]:
    """Write all of an observation's manual masks to disk as ``.reg`` mirrors."""
    paths: dict[str, str] = {}
    for basename, reg_string in (obs.manual_masks or {}).items():
        path = workspace_reg_path(obs, basename)
        write_reg_file(path, reg_string)
        paths[basename] = path
    return paths


# ---------------------------------------------------------------------------
# Header sentinel (per rate file)
# ---------------------------------------------------------------------------


def read_sentinel(rate_file: str) -> str:
    with fits.open(rate_file) as hdul:
        return str(hdul[0].header.get("CFMASKSH", "")).strip()


def stamp_sentinel(rate_file: str, reg_string: str | None, n_pixels: int) -> None:
    sha = hash_mask(reg_string)
    with fits.open(rate_file, mode="update") as hdul:
        hdr = hdul[0].header
        if sha:
            hdr["CFMASKSH"] = (sha, "sha256[:12] of manual mask region string")
            hdr["CFMASKDT"] = (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "Timestamp of last manual mask apply",
            )
            hdr["CFMASKN"] = (int(n_pixels), "Pixels flagged by manual mask")
        else:
            for k in ("CFMASKSH", "CFMASKDT", "CFMASKN"):
                if k in hdr:
                    del hdr[k]


def is_stale(rate_file: str, reg_string: str | None) -> bool:
    """True if the rate file's stamped mask hash differs from the current one."""
    return read_sentinel(rate_file) != hash_mask(reg_string)


# ---------------------------------------------------------------------------
# DQ application
# ---------------------------------------------------------------------------
#
# OR'ing DO_NOT_USE into DQ is not reversible without knowing which bits we
# set. To undo a previous mask cleanly (when re-applying after edits) we
# record the *pixels we flipped* — those that did not already have
# DO_NOT_USE set — in a ``CFDQMASK`` extension inside the rate file itself.
# Restoration AND-NOTs DO_NOT_USE on exactly those pixels and then drops
# the extension.


def _drop_extensions_if_present(rate_file: str, names: tuple[str, ...]) -> None:
    """Delete the named extensions from ``rate_file`` if present. No-op for
    names that don't exist. Used to remove extensions *before* an upcoming
    ``ImageModel.save()`` — that save snapshots asdf refs to whatever
    extensions are present at save time, so deleting after would leave
    dangling refs and break the next ImageModel open."""
    with fits.open(rate_file, mode="update") as hdul:
        for name in names:
            try:
                del hdul[name]
            except KeyError:
                pass


def apply_mask_dq(rate_file: str, reg_string: str | None) -> int:
    """OR ``DO_NOT_USE`` into the rate file's DQ array, recording the diff
    as a ``CFDQMASK`` ImageHDU inside the rate file so it can be cleanly
    reverted later.

    Returns the number of pixels flagged. Returns 0 (and writes no extension)
    if the region string is empty or rasterizes to nothing.
    """
    if not canonicalize(reg_string):
        return 0

    # Drop any pre-existing CFDQMASK *before* the ImageModel save below so
    # the save doesn't snapshot an asdf ref to a soon-to-be-replaced HDU.
    _drop_extensions_if_present(rate_file, ("CFDQMASK",))

    from jwst.datamodels import ImageModel

    with ImageModel(rate_file) as model:
        mask = parse_regions_to_mask(reg_string, model.dq.shape)
        n = int(mask.sum())
        if n == 0:
            log(f"  mask for {os.path.basename(rate_file)} flags 0 pixels (out of bounds?)")
            return 0
        prior_dnu = (model.dq & DO_NOT_USE).astype(bool)
        flipped = mask & ~prior_dnu
        model.dq[mask] |= DO_NOT_USE
        model.save(rate_file)

    with fits.open(rate_file, mode="update") as hdul:
        hdul.append(fits.ImageHDU(flipped.astype(np.uint8), name="CFDQMASK"))
    log(f"  flagged {n} pixels in {os.path.basename(rate_file)} ({int(flipped.sum())} newly DNU)")
    return n


def clear_manual_mask_dq(rate_file: str) -> None:
    """Undo a previous ``apply_mask_dq`` by AND-NOT'ing DO_NOT_USE on the
    pixels recorded in the ``CFDQMASK`` extension, then drop the extension.
    No-op if no ``CFDQMASK`` extension exists."""
    with fits.open(rate_file) as hdul:
        if "CFDQMASK" not in hdul:
            return
        flipped = hdul["CFDQMASK"].data.astype(bool).copy()

    # Drop CFDQMASK *before* the ImageModel save below — see _drop_extensions_if_present.
    _drop_extensions_if_present(rate_file, ("CFDQMASK",))

    from jwst.datamodels import ImageModel

    with ImageModel(rate_file) as model:
        if flipped.shape != model.dq.shape:
            log(
                f"WARNING: CFDQMASK shape {flipped.shape} != DQ shape "
                f"{model.dq.shape} for {os.path.basename(rate_file)}; skipping clear"
            )
            return
        # AND-NOT DO_NOT_USE on the recorded pixels.
        model.dq[flipped] &= ~np.uint32(DO_NOT_USE)
        model.save(rate_file)


# ---------------------------------------------------------------------------
# Pre-bkg-sub restoration (uses CFBKG extension + CFBKGRMS header)
# ---------------------------------------------------------------------------


def _bkgsub_done(rate_file: str) -> bool:
    """True iff the rate file's primary header has CFBKGSUB=True. Local copy
    of stage1.bkgsub_done to avoid an import cycle (stage1 imports from
    masks lazily; masks should not depend on stage1 at module level)."""
    with fits.open(rate_file) as hdul:
        return bool(hdul[0].header.get("CFBKGSUB", False))


def restore_pre_bkgsub(rate_file: str) -> None:
    """Undo bkg sub in place by adding the ``CFBKG`` extension back to SCI
    and un-rescaling ``VAR_RNOISE`` by ``CFBKGRMS``.

    Inverts ``subtract_background_from_rate_file``'s
    ``model.var_rnoise *= var_rescale`` by dividing by the same factor.

    After restore the rate file is in its pre-bkgsub state: ``CFBKGSUB`` is
    cleared, the ``CFBKG`` / ``CFBKGMASK`` extensions are dropped (they
    correspond to a bkgsub that no longer holds), and any stale
    ``CFMASK*`` sentinels are stripped so the re-apply can re-stamp from
    scratch.

    Raises ``RuntimeError`` if the required restoration metadata is missing
    (rate file predates the in-rate-extension cutover, or never had bkgsub
    applied).
    """
    from jwst.datamodels import ImageModel

    # Read everything we need from the file before opening with ImageModel,
    # so the gating check fires before any in-place modification.
    with fits.open(rate_file) as hdul:
        if not bool(hdul[0].header.get("CFBKGSUB", False)):
            raise RuntimeError(
                f"Cannot restore {os.path.basename(rate_file)}: CFBKGSUB is not set. "
                f"Either bkgsub never ran, or this rate file pre-dates the in-rate "
                f"extension cutover — re-run stage1 from uncal."
            )
        if "CFBKG" not in hdul:
            raise RuntimeError(
                f"Cannot restore {os.path.basename(rate_file)}: CFBKG extension missing. "
                f"Re-run stage1 from uncal."
            )
        var_rescale = hdul[0].header.get("CFBKGRMS")
        if var_rescale is None:
            raise RuntimeError(
                f"Cannot restore {os.path.basename(rate_file)}: CFBKGRMS missing. "
                f"Re-run stage1 from uncal."
            )
        bkg = np.asarray(hdul["CFBKG"].data).copy()

    # Tear down the bkgsub state *before* reopening as ImageModel — see
    # _drop_extensions_if_present for the asdf-ref reasoning. Header keys
    # are cleared in the same pass for atomicity.
    with fits.open(rate_file, mode="update") as hdul:
        hdr = hdul[0].header
        for k in ("CFBKGSUB", "CFBKGRMS", "CFBKGDT", "CFMASKSH", "CFMASKDT", "CFMASKN"):
            if k in hdr:
                del hdr[k]
    _drop_extensions_if_present(rate_file, ("CFBKG", "CFBKGMASK"))

    with ImageModel(rate_file) as model:
        if bkg.shape != model.data.shape:
            raise RuntimeError(
                f"Shape mismatch restoring {os.path.basename(rate_file)}: "
                f"rate {model.data.shape} vs CFBKG {bkg.shape}"
            )

        # Invert the bkgsub:
        #   forward:  data -= bkg;        var_rnoise *= var_rescale
        #   reverse:  data += bkg;        var_rnoise /= var_rescale
        model.data = model.data + bkg
        model.var_rnoise = model.var_rnoise / float(var_rescale)
        model.save(rate_file)


# ---------------------------------------------------------------------------
# High-level: apply for an observation
# ---------------------------------------------------------------------------


def _rate_basename(rate_file: str) -> str:
    return os.path.basename(rate_file).replace("_rate.fits", "")


def apply_to_observation(obs, stage1_config: dict, force: bool = False) -> None:
    """Re-run masks + bkg sub for every rate file in the observation that has a mask.

    For each affected rate file:
      1. Skip if the stamped mask hash already matches the TOML hash (unless force).
      2. If the rate file has a bkg-sub history entry, restore pre-bkg-sub state.
      3. OR DO_NOT_USE into DQ from the current region string.
      4. Run subtract_background_from_rate_file with the existing stage1 config.
      5. Stamp CFMASKSH/CFMASKDT/CFMASKN.

    ``stage1_config`` is the merged stage1 dict (same one used by
    ``run_stage1``). Only the bkg-sub-relevant keys are forwarded.
    """
    from campfire_pipeline.nirspec.stage1 import subtract_background_from_rate_file

    masks = obs.manual_masks or {}
    if not masks:
        log(f"No manual masks defined for {obs.name}; nothing to apply.")
        return

    materialize_reg_files(obs)

    bkgsub_kwargs = _bkgsub_kwargs(stage1_config)

    rate_by_basename = {_rate_basename(p): p for p in obs.rate_files}

    # Process every basename that has a current mask AND any rate file that
    # has a stale sentinel (e.g. mask was deleted from the TOML — we still
    # need to clear it from the rate file's DQ).
    basenames = set(masks.keys()) | {
        _rate_basename(p) for p in obs.rate_files if read_sentinel(p)
    }
    for basename in basenames:
        rate_file = rate_by_basename.get(basename)
        if rate_file is None:
            continue
        reg_string = masks.get(basename)

        if not force and not is_stale(rate_file, reg_string):
            log(f"{basename}: mask up to date, skipping.")
            continue

        log(f"{basename}: applying manual mask")
        clear_manual_mask_dq(rate_file)
        if _bkgsub_done(rate_file):
            log(f"  restoring pre-bkg-sub state from CFBKG extension + CFBKGRMS")
            restore_pre_bkgsub(rate_file)

        n_pixels = apply_mask_dq(rate_file, reg_string)
        subtract_background_from_rate_file(rate_file, **bkgsub_kwargs)
        stamp_sentinel(rate_file, reg_string, n_pixels)


def bkgsub_with_masks(
    rate_file: str,
    manual_masks: dict | None = None,
    **bkgsub_kwargs,
) -> None:
    """Per-rate-file worker: apply manual mask (if any), run bkg sub, stamp sentinel.

    Used by ``run_stage1`` phase 2 when an observation has manual masks defined.
    Handles both fresh applies (no history yet) and stale re-applies (clear +
    restore + apply + re-bkgsub) without callers needing to distinguish.
    """
    from campfire_pipeline.nirspec.stage1 import subtract_background_from_rate_file

    basename = _rate_basename(rate_file)
    reg_string = (manual_masks or {}).get(basename)
    sentinel = read_sentinel(rate_file)
    desired_sha = hash_mask(reg_string)

    needs_restore = bool(sentinel) and sentinel != desired_sha

    if needs_restore:
        log(f"{basename}: stale mask, restoring pre-bkg-sub state")
        clear_manual_mask_dq(rate_file)
        if _bkgsub_done(rate_file):
            restore_pre_bkgsub(rate_file)

    fresh_apply = bool(reg_string) and (not sentinel or needs_restore)
    if fresh_apply:
        n_pixels = apply_mask_dq(rate_file, reg_string)
    else:
        n_pixels = 0

    subtract_background_from_rate_file(rate_file, **bkgsub_kwargs)

    if fresh_apply:
        stamp_sentinel(rate_file, reg_string, n_pixels)
    elif needs_restore and not reg_string:
        # Mask was deleted from TOML; clear stale sentinel.
        stamp_sentinel(rate_file, None, 0)


def ensure_fresh(rate_file: str, obs, stage1_config: dict) -> None:
    """At stage2a entry: if this rate file's mask is stale, re-apply.

    No-op if no mask is defined for this exposure or if the sentinel matches.
    """
    masks = obs.manual_masks or {}
    basename = _rate_basename(rate_file)
    reg_string = masks.get(basename)

    if not reg_string and not read_sentinel(rate_file):
        return  # no mask, no sentinel, nothing to do

    if not is_stale(rate_file, reg_string):
        return

    from campfire_pipeline.nirspec.stage1 import subtract_background_from_rate_file

    log(f"{basename}: stale mask detected, re-applying before stage2a")
    clear_manual_mask_dq(rate_file)
    if _bkgsub_done(rate_file):
        restore_pre_bkgsub(rate_file)
    n_pixels = apply_mask_dq(rate_file, reg_string)
    subtract_background_from_rate_file(rate_file, **_bkgsub_kwargs(stage1_config))
    stamp_sentinel(rate_file, reg_string, n_pixels)


def _bkgsub_kwargs(stage1_config: dict) -> dict:
    """Project the stage1 config dict down to the kwargs accepted by
    ``subtract_background_from_rate_file``."""
    keys = (
        "override_wavelength_range",
        "n_iter",
        "subtract_2d",
        "box_size",
        "sigma_clip",
        "bkg_estimator",
        "do_col_1f",
        "do_row_1f",
        "col_1f_method",
        "plot",
        "save_backup",
    )
    return {k: stage1_config[k] for k in keys if k in stage1_config}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_observation(obs) -> list[dict]:
    """Parse every mask string and report basic stats. Used by ``mask validate``.

    Returns one dict per entry with keys: basename, ok, n_pixels, message.
    """
    results = []
    masks = obs.manual_masks or {}
    if not masks:
        return results

    rate_by_basename = {_rate_basename(p): p for p in obs.rate_files}

    for basename, reg_string in masks.items():
        rate_file = rate_by_basename.get(basename)
        if rate_file is None:
            results.append({
                "basename": basename, "ok": False, "n_pixels": 0,
                "message": "no matching rate file in workspace",
            })
            continue
        try:
            with fits.open(rate_file) as hdul:
                shape = hdul["SCI"].data.shape if "SCI" in hdul else hdul[1].data.shape
            mask = parse_regions_to_mask(reg_string, shape)
            n = int(mask.sum())
            if n == 0:
                results.append({
                    "basename": basename, "ok": False, "n_pixels": 0,
                    "message": "polygons rasterize to zero pixels (out of bounds?)",
                })
            else:
                results.append({
                    "basename": basename, "ok": True, "n_pixels": n, "message": "",
                })
        except Exception as exc:
            results.append({
                "basename": basename, "ok": False, "n_pixels": 0, "message": f"parse error: {exc}",
            })
    return results


# ---------------------------------------------------------------------------
# observations.toml round-trip (tomlkit-based, comment-preserving)
# ---------------------------------------------------------------------------


def write_masks_to_observations_toml(
    observations_file: str,
    obs_name: str,
    masks: dict[str, str | None],
) -> None:
    """Update the ``[<obs_name>.masks]`` table in observations.toml in place.

    ``masks`` maps rate basename -> region string (or ``None``/``''`` to delete).
    Preserves the rest of the file (comments, ordering) via tomlkit.
    """
    import tomlkit

    with open(observations_file, "r") as f:
        doc = tomlkit.parse(f.read())

    if obs_name not in doc:
        raise ValueError(f"Observation '{obs_name}' not found in {observations_file}")

    obs_table = doc[obs_name]

    # Find or create the masks sub-table.
    if "masks" in obs_table:
        masks_table = obs_table["masks"]
    else:
        masks_table = tomlkit.table()
        obs_table["masks"] = masks_table

    for basename, reg_string in masks.items():
        canon = canonicalize(reg_string) if reg_string else ""
        if not canon:
            if basename in masks_table:
                del masks_table[basename]
            continue
        # Use a multi-line basic string for readability.
        masks_table[basename] = tomlkit.string(canon + "\n", multiline=True)

    # If we wiped every entry, drop the empty subtable to keep the file tidy.
    if len(masks_table) == 0 and "masks" in obs_table:
        del obs_table["masks"]

    with open(observations_file, "w") as f:
        f.write(tomlkit.dumps(doc))


def read_reg_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()
