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
that disagrees with the DB is kept and reported.

The NIRSpec stuck-shutter and background-override lists are different: they are
two-way config, reconciled entry by entry by the config plane
(``nirspec_flags``; ``campfire config push / pull / diff``). The pull here runs
that same reconciliation in its non-interactive form: cloud additions and
changes to entries this machine has not touched are applied, and nothing is
removed or overridden.
"""

from __future__ import annotations


def pull_observation_annotations(obs_name: str, config: dict, *, dry_run: bool = False) -> None:
    """Materialize a NIRSpec observation's review annotations to reference/.

    Rate masks (``masks/*.reg``), and the stuck-shutter / nodded-background
    override lists (``stuck_closed_shutters.toml``,
    ``nodded_background_overrides.toml``) through the config plane's three-way
    rule (``nirspec_flags.pull_on_pull``): what needs a decision (a conflict, a
    removal) is reported for ``campfire config pull --obs``.
    """
    from campfire.deploy.nirspec_masks import pull_rate_masks
    from campfire.deploy import nirspec_flags as nf
    from campfire.deploy.supabase import get_supabase_client

    print(f"\nAnnotations for {obs_name}:")
    pull_rate_masks(obs_name, config, dry_run=dry_run)
    sb = get_supabase_client(config)
    if not nf.can_sync(config, sb):
        return   # admin-only table: zero rows here would not mean "no entries"
    try:
        counts = nf.pull_on_pull(sb, obs_name, config, dry_run=dry_run)
    except nf.FlagsError as e:
        print(f"  ! stuck-shutter / bkg-override lists: {e} — nothing written")
        return
    if counts['kept']:
        print(f"  run `campfire config pull --obs {obs_name}` to settle the "
              f"{counts['kept']} entr{'y' if counts['kept'] == 1 else 'ies'} kept local")


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
