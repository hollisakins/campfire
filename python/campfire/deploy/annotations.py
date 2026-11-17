"""Annotation materialization — the cloud→local half of the review loop.

Web-authored review state (masks, stuck shutters, background overrides,
exposure exclusions) lives in the database; the pipeline consumes it as files
under ``$CAMPFIRE_ROOT/reference/``. These helpers regenerate those files for
a scope — they are the annotation half of ``campfire pull`` (admin-only; the
storage engine handles the products half).

Unlike products, annotations are KB-scale DB rows with no content-hash
tracking. The files are local configuration too (hand-edited as well as pulled),
so every materializer is local-first (``local_first``): a pull creates what is
missing and never overwrites or deletes a local file or entry; a local entry
that disagrees with the DB is kept and reported. Local config reaches the DB on
deploy (``nirspec_flags.push_review_flags``).
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


def field_instruments(client, field: str) -> list[str]:
    """The imaging instruments with exposures of ``field`` in the review table.

    One ``LIMIT 1`` probe per instrument rather than a scan of the field's rows.
    """
    from campfire.deploy.nircam import IMAGING_INSTRUMENTS

    found = []
    for instrument in IMAGING_INSTRUMENTS:
        resp = (client.table('exposures')
                .select('id')
                .eq('instrument', instrument)
                .eq('field', field)
                .limit(1)
                .execute())
        if resp.data:
            found.append(instrument)
    return found


def pull_field_annotations(field: str, config: dict, *, dry_run: bool = False,
                           instruments: list[str] | None = None) -> None:
    """Materialize an imaging field's review annotations to reference/.

    For every imaging instrument with exposures of the field in the cloud
    (NIRCam, ACS/WFC, WFC3/IR; or just ``instruments``): masks
    (``reference/<instrument>/<field>/masks/<filter>/*.reg``), excluded
    exposures (``exposures.json``, honored by ``cfpipe <instrument> combine``),
    plus a drift report of local .reg vs DB masks.
    """
    from campfire.deploy.nircam_exclusions import pull_exclusions
    from campfire.deploy.nircam_masks import mask_drift_report, pull_masks
    from campfire.deploy.supabase import get_supabase_client

    if instruments is None:
        instruments = field_instruments(get_supabase_client(config), field)
    print(f"\nAnnotations for field {field}:")
    if not instruments:
        print("  no exposures of this field in the review table")
    for instrument in instruments:
        pull_masks(field, config, dry_run=dry_run, instrument=instrument)
        pull_exclusions(field, config, dry_run=dry_run, instrument=instrument)
        if not dry_run:
            mask_drift_report(field, config, instrument=instrument)
