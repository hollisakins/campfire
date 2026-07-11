"""Annotation materialization — the cloud→local half of the review loop.

Web-authored review state (masks, stuck shutters, background overrides,
exposure exclusions) lives in the database; the pipeline consumes it as files
under ``$CAMPFIRE_ROOT/reference/``. These helpers regenerate those files for
a scope — they are the annotation half of ``campfire pull`` (admin-only; the
storage engine handles the products half).

Unlike products, annotations are KB-scale DB rows with no content-hash
tracking: regeneration is unconditional and cheap. Overwrite safety lives in
the materializers themselves (e.g. stuck-shutters merges hand > web > auto),
not in a diff layer.
"""

from __future__ import annotations


def pull_observation_annotations(obs_name: str, config: dict, *, dry_run: bool = False) -> None:
    """Materialize a NIRSpec observation's review annotations to reference/.

    Rate masks (``masks/*.reg``), stuck shutters
    (``stuck_closed_shutters.toml``, hand > web > auto merge), and nodded
    background overrides (``nodded_background_overrides.toml``).
    """
    from campfire.deploy.nirspec_masks import pull_rate_masks
    from campfire.deploy.nirspec_flags import pull_bkg_overrides, pull_stuck_shutters

    print(f"\nAnnotations for {obs_name}:")
    pull_rate_masks(obs_name, config, dry_run=dry_run)
    pull_stuck_shutters(obs_name, config, dry_run=dry_run)
    pull_bkg_overrides(obs_name, config, dry_run=dry_run)


def pull_field_annotations(field: str, config: dict, *, dry_run: bool = False) -> None:
    """Materialize a NIRCam field's review annotations to reference/.

    Masks (``masks/*.reg``), excluded exposures (``exposures.json``, honored by
    ``cfpipe nircam combine``), plus a drift report of local .reg vs DB masks.
    """
    from campfire.deploy.nircam_exclusions import pull_exclusions
    from campfire.deploy.nircam_masks import mask_drift_report, pull_masks

    print(f"\nAnnotations for field {field}:")
    pull_masks(field, config, dry_run=dry_run)
    pull_exclusions(field, config, dry_run=dry_run)
    if not dry_run:
        mask_drift_report(field, config)
