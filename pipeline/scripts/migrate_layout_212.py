#!/usr/bin/env python3
"""
One-time migration: reorganize an existing ``$CAMPFIRE_ROOT`` into the issue #212
instrument-parity layout. Run **after** deploying the #212 pipeline code.

DRY-RUN by default — it only prints the plan. Pass ``--apply`` to make changes.
Idempotent and re-runnable: it skips anything already at its destination and
never overwrites existing data.

The migration logic lives in ``campfire_layout.migrate`` (the shared, zero-dep
layout-contract package) so this operator CLI and ``campfire sync`` run the exact
same code. This wrapper only adds the pipeline-specific bits: resolving the root,
loading ``observations.toml``, and warning about stale ``[paths]`` overrides.

Moves (all within a single ``$CAMPFIRE_ROOT``; symlinks are moved, never
dereferenced; existing targets are never clobbered):

  NIRSpec products   products/<obs>/                  -> products/nirspec/<obs>/
  NIRSpec raw        raw/<data_subdir>/               -> raw/nirspec/<data_subdir>/
                     (every NIRSpec raw dir, classified by content — incl.
                      downloaded-but-not-yet-reduced programs not in observations.toml;
                      genuine NIRCam / unclassified dirs are left in place)
  NIRSpec reducer    products/<obs>/_<obs>_stuck_closed_shutters.toml
                                                      -> reference/nirspec/<obs>/stuck_closed_shutters.toml
                     products/<obs>/_<obs>_nodded_background_overrides.toml
                                                      -> reference/nirspec/<obs>/nodded_background_overrides.toml
  NIRCam shared      reference/nircam/<field>/flats/  -> reference/nircam/shared/flats/   (de-fielded merge)
                     reference/nircam/<field>/wisps/  -> reference/nircam/shared/wisps/

Unchanged / out of scope: products/nircam, raw/nircam, reference/nircam/<field>/
{bad_pixels,masks,astrom_cats}, cache, meta, tiles, cutouts, config.

``--clean-intermediates`` (opt-in) additionally deletes the STALE per-exposure
NIRSpec quartet left inside migrated obs dirs — jw*_nrs[12]_*_{cal,cal_bkgsub,
s2d,s2d_bkgsub}.fits. These are never re-discovered by the new pipeline (it globs
the bare canonical ``_nrs[12]_<srcid>.fits``). The glob is anchored to the
``jw``-rootname + ``_nrs[12]_`` form so it can NEVER match the deployable combined
stage3 product ``<obs>_<grating>_<filter>_<srcid>_s2d.fits``. Intermediate
*tarballs* (cal.tar.gz, s2d.tar.gz, ...) are reported but never auto-deleted.

In ``--apply`` mode every committed action is appended (and flushed) to a JSONL
manifest ``migration_212_manifest_<ts>.jsonl`` at the root, so an interrupted run
still leaves an accurate record of exactly what changed (audit / manual undo).

Usage:
    python pipeline/scripts/migrate_layout_212.py            # dry-run (default)
    python pipeline/scripts/migrate_layout_212.py --apply
    python pipeline/scripts/migrate_layout_212.py --apply --clean-intermediates
    python pipeline/scripts/migrate_layout_212.py --root /path/to/CAMPFIRE_ROOT
"""

import argparse
import os
import sys
from pathlib import Path

import toml

from campfire_layout.migrate import LayoutMigrator


def _resolve_root(cli_root):
    if cli_root:
        return Path(cli_root).expanduser().resolve()
    try:
        from campfire_pipeline.config import _get_campfire_root
        return Path(_get_campfire_root()).resolve()
    except Exception:
        return Path(os.environ.get('CAMPFIRE_ROOT', Path.home() / 'campfire')).resolve()


def _load_observations(root):
    """Return the observations.toml dict ({obs: {data_subdir, ...}})."""
    try:
        from campfire_pipeline.config import resolve_observations_file
        path = Path(resolve_observations_file())
    except Exception:
        path = root / 'config' / 'observations.toml'
    if not path.exists():
        sys.exit(f"ERROR: observations.toml not found at {path}")
    return toml.load(path)


def _check_paths_config(root):
    """Warn if config.toml still sets [paths] overrides that #212 removed."""
    cfg = root / 'config' / 'config.toml'
    if not cfg.exists():
        return
    try:
        paths = toml.load(cfg).get('paths', {})
    except Exception:
        return
    stale = {k: paths[k] for k in ('data_dir', 'products_dir') if k in paths}
    if stale:
        try:
            rel = root.name
        except Exception:
            rel = str(root)
        print(f"  [WARN] config.toml still sets [paths] {stale} — issue #212 removed those "
              f"overrides (everything now derives from $CAMPFIRE_ROOT). If they point "
              f"OUTSIDE {rel}, that data is NOT migrated by this script. "
              f"Remove the [paths] block after confirming.")


def main():
    ap = argparse.ArgumentParser(description="One-time #212 $CAMPFIRE_ROOT layout migration.")
    ap.add_argument('--apply', action='store_true',
                    help='Execute the migration (default: dry-run, print plan only).')
    ap.add_argument('--clean-intermediates', action='store_true',
                    help='Also delete stale per-exposure NIRSpec quartet files in migrated '
                         'obs dirs (anchored jw*_nrs[12]_*_{cal,cal_bkgsub,s2d,s2d_bkgsub}.fits).')
    ap.add_argument('--root', default=None,
                    help='$CAMPFIRE_ROOT to migrate (default: $CAMPFIRE_ROOT env / ~/campfire).')
    args = ap.parse_args()

    root = _resolve_root(args.root)
    if not root.is_dir():
        sys.exit(f"ERROR: $CAMPFIRE_ROOT not found: {root}")
    obs_cfg = _load_observations(root)

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    n_subdirs = len({str(v['data_subdir']) for v in obs_cfg.values()
                     if v.get('data_subdir') is not None})
    print(f"#212 layout migration [{mode}]  root={root}")
    print(f"observations.toml: {len(obs_cfg)} obs, {n_subdirs} unique NIRSpec data_subdirs")
    if args.clean_intermediates:
        print("  --clean-intermediates: stale loose quartet WILL be removed from migrated dirs")

    _check_paths_config(root)

    m = LayoutMigrator(root, apply=args.apply,
                       clean_intermediates=args.clean_intermediates, obs_cfg=obs_cfg)
    interrupted = False
    try:
        m.run()
    except (KeyboardInterrupt, Exception) as e:   # always leave an accurate manifest
        interrupted = True
        print(f"\n!! ABORTED mid-migration: {type(e).__name__}: {e}")
        m.finalize(interrupted=True)
        raise
    m.finalize(interrupted=interrupted)


if __name__ == '__main__':
    main()
