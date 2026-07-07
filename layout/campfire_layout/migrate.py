"""One-time #212 ``$CAMPFIRE_ROOT`` layout migration (shared core).

Relocates an existing data root into the issue #212 instrument-parity layout.
This is the single, dependency-free implementation shared by:

  * ``pipeline/scripts/migrate_layout_212.py`` — the operator CLI (adds root
    resolution + ``observations.toml`` loading on top of this core), and
  * ``campfire sync`` — auto-detects the old layout and offers to run it.

Moves (all within one ``$CAMPFIRE_ROOT``; symlinks are moved, never
dereferenced; existing targets are never clobbered):

  NIRSpec products   products/<obs>/                  -> products/nirspec/<obs>/
  NIRSpec raw        raw/<data_subdir>/               -> raw/nirspec/<data_subdir>/
  NIRSpec reducer    products/<obs>/_<obs>_stuck_closed_shutters.toml
                                                      -> reference/nirspec/<obs>/stuck_closed_shutters.toml
                     products/<obs>/_<obs>_nodded_background_overrides.toml
                                                      -> reference/nirspec/<obs>/nodded_background_overrides.toml
  NIRCam shared      reference/nircam/<field>/flats/  -> reference/nircam/shared/flats/   (de-fielded merge)
                     reference/nircam/<field>/wisps/  -> reference/nircam/shared/wisps/

The raw migration needs the observations table (to classify referenced raw
dirs); it is skipped entirely when ``obs_cfg is None`` — so a download-only
client (which has only ``products/`` and no ``observations.toml``) migrates the
products tree and nothing else. The products + NIRCam-shared migrations run
regardless.

The core is **zero-dependency (stdlib only)**: the observations table and any
config parsing are the caller's job and are passed in as plain dicts.
"""

from __future__ import annotations

import filecmp
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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
# clean_intermediates, and only when the basename starts jw...­_nrs[12]_.
QUARTET_SUFFIXES = ('_cal.fits', '_cal_bkgsub.fits', '_s2d.fits', '_s2d_bkgsub.fits')
INTERMEDIATE_TARBALLS = ('cal.tar.gz', 'cal_bkgsub.tar.gz', 's2d.tar.gz',
                         's2d_bkgsub.tar.gz', 'bkgsub.tar.gz')

# Action tags that represent a real change to on-disk data (as opposed to
# scaffolding like mkdir/rmdir). Used to decide whether a migration is pending.
MATERIAL_ACTIONS = ('MOVE', 'DEDUP', 'CLEAN', 'FIX', 'REMOVE')


def _noop(*_args, **_kwargs) -> None:
    pass


def _has_any(d: Path, patterns) -> bool:
    """True if directory d (following symlinks) contains a file matching any glob."""
    return any(next(d.glob(p), None) is not None for p in patterns)


class LayoutMigrator:
    """Crash-safe, idempotent, dry-run-by-default #212 layout migrator.

    Parameters
    ----------
    root : Path
        The ``$CAMPFIRE_ROOT`` to migrate.
    apply : bool
        When False (default) nothing is written — the plan is only recorded in
        ``counts``/``warnings`` (and printed via ``echo``). Pass True to execute.
    clean_intermediates : bool
        Also delete the stale per-exposure NIRSpec quartet from migrated dirs.
    obs_cfg : dict | None
        The ``observations.toml`` mapping ``{obs: {data_subdir, ...}}``. When
        None, the ``raw/`` migration is skipped (products + NIRCam-shared still
        run). Callers without an observations table (download-only clients)
        pass None.
    echo : callable
        Sink for human-readable progress lines (default ``print``). Pass a no-op
        to plan silently.
    """

    def __init__(self, root, apply=False, clean_intermediates=False,
                 obs_cfg: Optional[dict] = None, echo: Callable = print):
        self.root = Path(root)
        self.apply = apply
        self.clean_intermediates = clean_intermediates
        self.obs_cfg = obs_cfg
        self.echo = echo
        self.counts: dict = {}
        self.warnings: list = []
        self._removed: set = set()   # paths removed (real in apply, virtual in dry-run)
        self._created: set = set()   # move destinations created this run (accurate dry-run)
        self._mf = None              # JSONL manifest file handle (apply only)
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
        self.echo(f"  [{'' if self.apply else 'DRY '}{action}] {msg}")
        self._tag(action)

    def warn(self, msg):
        self.warnings.append(msg)
        self.echo(f"  [WARN] {msg}")
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
        self.echo("\n=== NIRSpec products: products/<obs>/ -> products/nirspec/<obs>/ ===")
        products = self.root / 'products'
        if not products.is_dir():
            self.echo(f"  [info] no products/ dir at {self._rel(products)} — nothing to migrate")
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
            self.echo(f"  [info] {obs}: no stale loose intermediates found")

    def migrate_raw(self, obs_cfg):
        self.echo("\n=== NIRSpec raw: raw/<data_subdir>/ -> raw/nirspec/<data_subdir>/ ===")
        raw = self.root / 'raw'
        if not raw.is_dir():
            self.echo(f"  [info] no raw/ dir at {self._rel(raw)} — nothing to migrate")
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
                self.echo(f"  [info] raw/{name}: NIRSpec program not yet in observations.toml — migrated")
        # Sanity: referenced data_subdirs with no raw in the old OR new location.
        for sub in sorted(referenced):
            if sub in seen or os.path.lexists(raw / 'nirspec' / sub):
                continue
            self.warn(f"observations.toml references data_subdir '{sub}' but no raw found at "
                      f"raw/{sub} or raw/nirspec/{sub}")

    def migrate_nircam_shared(self):
        self.echo("\n=== NIRCam: reference/nircam/<field>/{flats,wisps} -> reference/nircam/shared/ ===")
        nircam = self.root / 'reference' / 'nircam'
        if not nircam.is_dir():
            self.echo("  [info] no reference/nircam/ — nothing to hoist")
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

    def run(self):
        """Execute all in-scope sections. ``raw/`` is skipped when obs_cfg is None."""
        self.open_manifest()
        self.migrate_products()
        if self.obs_cfg is not None:
            self.migrate_raw(self.obs_cfg)
        self.migrate_nircam_shared()

    @property
    def pending(self) -> bool:
        """True if the run performed (or, in dry-run, would perform) a material
        change to on-disk data — as opposed to pure scaffolding (mkdir/rmdir)."""
        return sum(self.counts.get(a, 0) for a in MATERIAL_ACTIONS) > 0

    def finalize(self, interrupted=False):
        if self._mf is not None:
            self._mf.close()
        self.echo("\n=== SUMMARY ===")
        for action in sorted(self.counts):
            self.echo(f"  {action}: {self.counts[action]}")
        if not self.apply:
            self.echo("\nDRY RUN — no changes made. Re-run with apply=True to execute.")
            return
        note = " (INTERRUPTED — partial)" if interrupted else ""
        self.echo(f"\nAPPLIED{note}. Manifest (every committed action, for audit/undo): "
                  f"{self._manifest_path}")
        if self.warnings:
            self.echo(f"{len(self.warnings)} warning(s) above need manual review.")


def plan_migration(root, obs_cfg: Optional[dict] = None,
                   clean_intermediates: bool = False) -> dict:
    """Dry-run the migration and return a structured summary (no output, no writes).

    Returns a dict with keys: ``pending`` (bool — is there material work?),
    ``counts`` (action → count), ``warnings`` (list of str).
    """
    m = LayoutMigrator(root, apply=False, clean_intermediates=clean_intermediates,
                       obs_cfg=obs_cfg, echo=_noop)
    m.run()
    return {"pending": m.pending, "counts": dict(m.counts), "warnings": list(m.warnings)}


def apply_migration(root, obs_cfg: Optional[dict] = None,
                    clean_intermediates: bool = False, echo: Callable = print) -> dict:
    """Execute the migration. Returns a dict with ``counts``, ``warnings``,
    ``manifest_path`` (str | None), and ``interrupted`` (bool).

    On any exception the manifest is finalized (so an interrupted run still
    leaves an accurate audit record) and the exception is re-raised.
    """
    m = LayoutMigrator(root, apply=True, clean_intermediates=clean_intermediates,
                       obs_cfg=obs_cfg, echo=echo)
    try:
        m.run()
    except BaseException:
        m.finalize(interrupted=True)
        raise
    m.finalize(interrupted=False)
    return {
        "counts": dict(m.counts),
        "warnings": list(m.warnings),
        "manifest_path": str(m._manifest_path) if m._manifest_path else None,
        "interrupted": False,
    }
