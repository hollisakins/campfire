#!/usr/bin/env python3
"""
One-time migration: reorganize an existing ``$CAMPFIRE_ROOT`` into the issue #212
instrument-parity layout. Run **after** deploying the #212 pipeline code.

DRY-RUN by default — it only prints the plan. Pass ``--apply`` to make changes.
Idempotent and re-runnable: it skips anything already at its destination and
never overwrites existing data.

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
import filecmp
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import toml

# Reducer-decision TOMLs that move out of the products workspace into reference/.
# (expected source basename built from the obs name, destination basename)
REDUCER_TOMLS = [
    ('_{obs}_stuck_closed_shutters.toml', 'stuck_closed_shutters.toml'),
    ('_{obs}_nodded_background_overrides.toml', 'nodded_background_overrides.toml'),
]
# Any file matching these suffixes that is NOT the expected per-obs name is a
# stray (e.g. the program-named macs0647 duplicate) — flagged, never moved.
TOML_SUFFIXES = ('_stuck_closed_shutters.toml', '_nodded_background_overrides.toml')

# A directory "looks like" a NIRSpec obs workspace if it holds any of these.
# Deliberately NIRSpec-specific (jw*_nrs[12]_* / *_msa*), NOT bare jw*.fits, so a
# stray flat NIRCam dir can never be misfiled into products/nirspec/.
NIRSPEC_MARKERS = ('*_spec.fits', '*_rate.fits', 'jw*_nrs[12]_*.fits', '*_msa*.fits',
                   '*_summary.ecsv', '_*_stuck_closed_shutters.toml',
                   '_*_nodded_background_overrides.toml')
NIRCAM_MARKERS = ('jw*_nrc[ab]*.fits', 'jw*_nrcalong*.fits', 'jw*_nrcblong*.fits')

# Stale per-exposure intermediate suffixes (loose quartet), cleaned only with
# --clean-intermediates, and only when the basename starts jw...­_nrs[12]_.
QUARTET_SUFFIXES = ('_cal.fits', '_cal_bkgsub.fits', '_s2d.fits', '_s2d_bkgsub.fits')
INTERMEDIATE_TARBALLS = ('cal.tar.gz', 'cal_bkgsub.tar.gz', 's2d.tar.gz',
                         's2d_bkgsub.tar.gz', 'bkgsub.tar.gz')


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


def _has_any(d, patterns):
    """True if directory d (following symlinks) contains a file matching any glob."""
    return any(next(d.glob(p), None) is not None for p in patterns)


class Migrator:
    def __init__(self, root, apply, clean_intermediates):
        self.root = root
        self.apply = apply
        self.clean_intermediates = clean_intermediates
        self.counts = {}
        self.warnings = []
        self._removed = set()       # paths removed (real in --apply, virtual in dry-run)
        self._created = set()       # move destinations created this run (for accurate dry-run)
        self._mf = None             # JSONL manifest file handle (apply only)
        self._manifest_path = None

    # --- manifest / logging -------------------------------------------------

    def open_manifest(self):
        if not self.apply:
            return
        ts = datetime.now().strftime('%Y%m%dT%H%M%S')
        self._manifest_path = self.root / f'migration_212_manifest_{ts}.jsonl'
        self._mf = open(self._manifest_path, 'w')

    def _record(self, action, src, dst):
        if self._mf is not None:
            self._mf.write(json.dumps({'action': action, 'src': src, 'dst': dst}) + '\n')
            self._mf.flush()
            os.fsync(self._mf.fileno())

    def _tag(self, action):
        self.counts[action] = self.counts.get(action, 0) + 1

    def _rel(self, p):
        try:
            return str(Path(p).relative_to(self.root))
        except ValueError:
            return str(p)

    def log(self, action, msg):
        print(f"  [{'' if self.apply else 'DRY '}{action}] {msg}")
        self._tag(action)

    def warn(self, msg):
        self.warnings.append(msg)
        print(f"  [WARN] {msg}")
        self._tag('WARN')

    # --- mutation primitives (all crash-safe via _record-before-effect) ------

    def _occupied(self, dst):
        """A destination is occupied if it physically exists OR was created
        earlier this run, unless it has since been removed."""
        s = str(dst)
        return (os.path.lexists(dst) or s in self._created) and s not in self._removed

    def mkdir(self, d):
        d = Path(d)
        if d.exists() or str(d) in self._created:
            return
        self._record('mkdir', None, str(d))
        if self.apply:
            d.mkdir(parents=True, exist_ok=True)
        self._created.add(str(d))

    def move(self, src, dst):
        """Rename src -> dst (preserves symlinks; intra-root, never cross-device).
        Refuses to clobber an occupied destination."""
        src, dst = Path(src), Path(dst)
        if self._occupied(dst):
            self.warn(f"target exists, NOT overwriting: {self._rel(dst)} (left {self._rel(src)})")
            return False
        self.mkdir(dst.parent)
        self._record('move', str(src), str(dst))   # record BEFORE the effect
        if self.apply:
            os.rename(src, dst)
        self.log('MOVE', f"{self._rel(src)} -> {self._rel(dst)}"
                          + ('  (symlink)' if os.path.islink(src) else ''))
        self._created.add(str(dst))
        self._removed.discard(str(src))
        return True

    def remove_file(self, p, action='REMOVE', why=''):
        p = Path(p)
        self._record(action.lower(), str(p), None)   # record BEFORE the effect
        if self.apply:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                p.rmdir()
        self.log(action, f"{self._rel(p)}{('  (' + why + ')') if why else ''}")
        self._removed.add(str(p))
        self._created.discard(str(p))

    def rmdir_if_empty(self, d):
        d = Path(d)
        if d.is_dir() and not any(d.iterdir()):
            self._record('rmdir', str(d), None)
            if self.apply:
                d.rmdir()
            self.log('RMDIR', self._rel(d))

    # --- reducer TOML relocation -------------------------------------------

    def relocate_reducer_tomls(self, src_dir, obs):
        """Move the per-obs reducer TOMLs out of src_dir into reference/nirspec/<obs>/.
        Dedups byte-identical leftovers; never clobbers a differing target; flags
        stray (non-per-obs-named) reducer TOMLs without touching them."""
        ref = self.root / 'reference' / 'nirspec' / obs
        for src_tmpl, dst_name in REDUCER_TOMLS:
            src = src_dir / src_tmpl.format(obs=obs)
            if not src.exists():
                continue
            dst = ref / dst_name
            if os.path.lexists(dst):
                if dst.is_file() and src.is_file() and filecmp.cmp(src, dst, shallow=False):
                    self.remove_file(src, action='DEDUP',
                                     why='identical copy already at reference')
                else:
                    self.warn(f"reference TOML differs, kept both: {self._rel(dst)} "
                              f"vs stale {self._rel(src)}")
                continue
            self.move(src, dst)
        expected = {t.format(obs=obs) for t, _ in REDUCER_TOMLS}
        for f in sorted(src_dir.glob('_*')):
            if f.name not in expected and f.name.endswith(TOML_SUFFIXES):
                self.warn(f"stray reducer TOML left in place (name != _{obs}_*): "
                          f"{self._rel(f)} — review manually")

    # --- sections -----------------------------------------------------------

    def migrate_products(self):
        print("\n=== NIRSpec products: products/<obs>/ -> products/nirspec/<obs>/ ===")
        products = self.root / 'products'
        if not products.is_dir():
            self.warn(f"no products/ dir at {self._rel(products)}")
            return
        self.mkdir(products / 'nirspec')
        for entry in sorted(products.iterdir()):
            name = entry.name
            if not entry.is_dir() or name in ('nirspec', 'nircam'):
                continue
            if name.startswith('_') or name.startswith('.'):
                continue  # backups (_*_baseline), .DS_Store, etc.
            old_dir = products / name          # always exists (iterated entry)
            new_dir = products / 'nirspec' / name
            if not _has_any(old_dir, NIRSPEC_MARKERS):
                self.warn(f"products/{name}/ has no NIRSpec markers — skipped (review manually)")
                continue
            if new_dir.exists():
                # Already-migrated copy exists alongside the old one: new is
                # authoritative. Fix any stray TOML in new; never clobber old.
                self.relocate_reducer_tomls(new_dir, name)
                self.warn(f"BOTH products/{name} and products/nirspec/{name} exist — "
                          f"left old in place (new authoritative); resolve manually")
                continue
            # Only old exists: extract reducer TOMLs first, then move the dir.
            self.relocate_reducer_tomls(old_dir, name)
            moved = self.move(old_dir, new_dir)
            if self.clean_intermediates:
                self._clean_intermediates(new_dir if (moved and self.apply) else old_dir, name)

    def _clean_intermediates(self, obs_dir, obs):
        n = 0
        for suf in QUARTET_SUFFIXES:
            for f in obs_dir.glob(f'jw*_nrs[12]_*{suf}'):
                self.remove_file(f, action='CLEAN', why='stale per-exposure intermediate')
                n += 1
        tarballs = [t for t in INTERMEDIATE_TARBALLS if (obs_dir / t).exists()]
        if tarballs:
            self.warn(f"{obs}: intermediate tarball(s) present (NOT auto-removed): "
                      + ', '.join(tarballs))
        if n == 0 and not tarballs:
            print(f"  [info] {obs}: no stale loose intermediates found")

    def migrate_raw(self, obs_cfg):
        print("\n=== NIRSpec raw: raw/<data_subdir>/ -> raw/nirspec/<data_subdir>/ ===")
        raw = self.root / 'raw'
        if not raw.is_dir():
            self.warn(f"no raw/ dir at {self._rel(raw)}")
            return
        self.mkdir(raw / 'nirspec')
        referenced = {str(v['data_subdir']) for v in obs_cfg.values()
                      if v.get('data_subdir') is not None}
        seen = set()
        # Iterate the flat raw dirs themselves (each unique subdir appears once, so
        # shared data_subdirs never double-move). Move EVERY NIRSpec raw dir —
        # referenced by an obs OR not (unreduced programs migrate too) — classified
        # by content; leave genuine NIRCam / unclassified dirs in place.
        for entry in sorted(raw.iterdir()):
            name = entry.name
            if name in ('nirspec', 'nircam') or name.startswith('.'):
                continue
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            seen.add(name)
            new = raw / 'nirspec' / name
            has_nrs = _has_any(entry, ('*_nrs[12]_*.fits', '*_msa*.fits'))
            has_nrc = _has_any(entry, NIRCAM_MARKERS)
            is_ref = name in referenced
            if has_nrc and not has_nrs:
                self.warn(f"raw/{name} looks like NIRCam raw — left in place "
                          f"(NIRCam raw lives under raw/nircam/<PID>/).")
                continue
            if not (has_nrs or is_ref):
                link = '  (symlink, target unreadable?)' if entry.is_symlink() else ''
                self.warn(f"raw/{name}{link} unclassified — no NIRSpec markers and not in "
                          f"observations.toml; left in place, review manually.")
                continue
            # NIRSpec raw (referenced or unreduced) -> raw/nirspec/<name>.
            if os.path.lexists(new):
                if os.path.islink(new) and os.path.realpath(new) == os.path.realpath(entry):
                    self.remove_file(new, action='FIX', why='self-referential test symlink')
                else:
                    self.warn(f"BOTH raw/{name} and raw/nirspec/{name} exist — left old in "
                              f"place; resolve manually")
                    continue
            if self.move(entry, new) and not is_ref:
                print(f"  [info] raw/{name}: NIRSpec program not yet in observations.toml — migrated")
        # Sanity: referenced data_subdirs with no raw in the old OR new location.
        for sub in sorted(referenced):
            if sub in seen or os.path.lexists(raw / 'nirspec' / sub):
                continue
            self.warn(f"observations.toml references data_subdir '{sub}' but no raw found at "
                      f"raw/{sub} or raw/nirspec/{sub}")

    def migrate_nircam_shared(self):
        print("\n=== NIRCam: reference/nircam/<field>/{flats,wisps} -> reference/nircam/shared/ ===")
        nircam = self.root / 'reference' / 'nircam'
        if not nircam.is_dir():
            print("  [info] no reference/nircam/ — nothing to hoist")
            return
        shared = nircam / 'shared'
        for sub in ('flats', 'wisps'):
            self.mkdir(shared / sub)
        for field_dir in sorted(nircam.iterdir()):
            if not field_dir.is_dir() or field_dir.name == 'shared':
                continue
            for sub in ('flats', 'wisps'):
                fdir = field_dir / sub
                if not fdir.is_dir():
                    continue
                for f in sorted(fdir.iterdir()):
                    if f.is_dir():
                        self.warn(f"unexpected subdir in {self._rel(fdir)}: {f.name} — skipped")
                        continue
                    dst = shared / sub / f.name
                    if self._occupied(dst):
                        if f.is_file() and dst.is_file() and filecmp.cmp(f, dst, shallow=False):
                            self.remove_file(f, action='DEDUP', why='identical, already in shared')
                        elif os.path.lexists(dst):
                            self.warn(f"flat/wisp collision (differing content), kept both: "
                                      f"{self._rel(f)} vs {self._rel(dst)}")
                        else:  # only virtually created this run (dry-run sibling)
                            self.warn(f"flat/wisp same name from multiple fields: {f.name} — "
                                      f"apply will dedup-if-identical or keep-both")
                        continue
                    self.move(f, dst)
                self.rmdir_if_empty(fdir)

    # --- driver -------------------------------------------------------------

    def check_paths_config(self):
        cfg = self.root / 'config' / 'config.toml'
        if not cfg.exists():
            return
        try:
            paths = toml.load(cfg).get('paths', {})
        except Exception:
            return
        stale = {k: paths[k] for k in ('data_dir', 'products_dir') if k in paths}
        if stale:
            self.warn(f"config.toml still sets [paths] {stale} — issue #212 removed those "
                      f"overrides (everything now derives from $CAMPFIRE_ROOT). If they point "
                      f"OUTSIDE {self._rel(self.root)}, that data is NOT migrated by this script. "
                      f"Remove the [paths] block after confirming.")

    def run(self, obs_cfg):
        self.open_manifest()
        self.check_paths_config()
        self.migrate_products()
        self.migrate_raw(obs_cfg)
        self.migrate_nircam_shared()

    def finalize(self, interrupted=False):
        if self._mf is not None:
            self._mf.close()
        print("\n=== SUMMARY ===")
        for action in sorted(self.counts):
            print(f"  {action}: {self.counts[action]}")
        if not self.apply:
            print("\nDRY RUN — no changes made. Re-run with --apply to execute.")
            return
        note = " (INTERRUPTED — partial)" if interrupted else ""
        print(f"\nAPPLIED{note}. Manifest (every committed action, for audit/undo): "
              f"{self._manifest_path}")
        if self.warnings:
            print(f"{len(self.warnings)} warning(s) above need manual review.")


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

    m = Migrator(root, apply=args.apply, clean_intermediates=args.clean_intermediates)
    interrupted = False
    try:
        m.run(obs_cfg)
    except (KeyboardInterrupt, Exception) as e:   # always leave an accurate manifest
        interrupted = True
        print(f"\n!! ABORTED mid-migration: {type(e).__name__}: {e}")
        m.finalize(interrupted=True)
        raise
    m.finalize(interrupted=interrupted)


if __name__ == '__main__':
    main()
