"""
MetaFile dataclass: MSA metadata (shutter tables, source tables, MSAMETFL headers).
"""

import os
import numpy as np
from collections import defaultdict
from copy import copy, deepcopy
from dataclasses import dataclass
from astropy.io import fits
from astropy.table import Table


# Two catalog entries within this separation sharing a slitlet are the
# same physical source.  The gap between the largest known duplicate
# (0.095") and the smallest real multi-object pair (0.158") is wide.
DUPLICATE_SEPARATION_ARCSEC = 0.1


def _source_rows(shutter_table, source_id=None):
    """Mask of SHUTTER_INFO rows that hold a catalog source (or *source_id*).

    ``source_id == 0`` is also what the metafile writes for empty background
    shutters (jwst: "Background slits all have source_id=0 in the msa_file"),
    so a catalog source 0 (e.g. PID 2750) only owns its ``background == 'N'``
    rows.  Negative IDs are virtual sources and never match.
    """
    sid = shutter_table['source_id']
    rows = (sid > 0) | ((sid == 0) & (shutter_table['background'] == 'N'))
    if source_id is not None:
        rows &= sid == source_id
    return rows


def _prune_detached_shutters(shutter_table, source_id):
    """Remove background shutters that are not contiguous with the source.

    Splits the shutter columns into contiguous groups (allowing gaps of 1
    for stuck/closed shutters within a slitlet) and keeps only the group
    that contains the source shutter(s).  Detached background shutters —
    separated by gaps of 2+ columns — are removed.

    Parameters
    ----------
    shutter_table : Table
        Filtered shutter table for one source/slitlet.
    source_id : int
        The primary source ID.

    Returns
    -------
    Table
        Shutter table with detached columns removed.
    """
    all_cols = np.sort(np.unique(shutter_table['shutter_column']))
    if len(all_cols) <= 1:
        return shutter_table

    # Find which columns contain the source
    source_cols = set(np.unique(
        shutter_table['shutter_column'][_source_rows(shutter_table, source_id)]
    ))

    # Split columns into contiguous groups (gap of 3+ starts a new group,
    # allowing gaps of up to 2 for stuck/closed shutters within a slitlet)
    groups = []
    current_group = [all_cols[0]]
    for i in range(1, len(all_cols)):
        if all_cols[i] - all_cols[i - 1] <= 3:
            current_group.append(all_cols[i])
        else:
            groups.append(current_group)
            current_group = [all_cols[i]]
    groups.append(current_group)

    if len(groups) == 1:
        return shutter_table

    # Keep only the group containing the source column(s)
    for group in groups:
        if source_cols & set(group):
            keep_cols = set(group)
            break
    else:
        return shutter_table

    from campfire_pipeline.common.io import log
    removed = set(int(c) for c in all_cols) - set(int(c) for c in keep_cols)
    if removed:
        log(f'Pruned {len(removed)} detached shutter column(s) {sorted(removed)} '
            f'from source {source_id} (keeping columns {sorted(int(c) for c in keep_cols)})')

    mask = np.isin(shutter_table['shutter_column'], sorted(keep_cols))
    return shutter_table[mask]


@dataclass
class MetaFile:

    hdul: fits.HDUList
    filename: str
    msametid: int
    shutter_table: Table = None
    source_table: Table = None

    def __post_init__(self):
        """Initialize Tables from HDUList if not already provided."""
        if self.shutter_table is None:
            self.shutter_table = Table(self.hdul[2].data)
        if self.source_table is None:
            self.source_table = Table(self.hdul[3].data)

    def _duplicate_source_ids(self):
        """Identify duplicate source IDs that share a slitlet with a
        positionally coincident partner (< DUPLICATE_SEPARATION_ARCSEC).

        Scans ALL msa_metadata_id values (grating configs) and returns the
        union of higher-valued source IDs from each duplicate pair, ensuring
        the same ID is dropped regardless of which config is processed.

        The result is cached on the instance.
        """
        if hasattr(self, '_cached_dup_ids'):
            return self._cached_dup_ids

        # Build source_id -> (ra, dec) lookup from SOURCE_INFO table
        src_pos = {}
        for row in self.source_table:
            sid = int(row['source_id'])
            if sid >= 0:
                src_pos[sid] = (float(row['ra']), float(row['dec']))

        drop_ids = set()
        threshold = DUPLICATE_SEPARATION_ARCSEC

        for meta_id in np.unique(self.shutter_table['msa_metadata_id']):
            meta_rows = self.shutter_table[
                self.shutter_table['msa_metadata_id'] == meta_id
            ]

            # Group source_ids by slitlet
            slitlet_sources = defaultdict(set)
            for row in meta_rows[_source_rows(meta_rows)]:
                slitlet_sources[int(row['slitlet_id'])].add(int(row['source_id']))

            for sids in slitlet_sources.values():
                if len(sids) < 2:
                    continue
                sids_sorted = sorted(sids)
                for i in range(len(sids_sorted)):
                    for j in range(i + 1, len(sids_sorted)):
                        s1, s2 = sids_sorted[i], sids_sorted[j]
                        if s1 not in src_pos or s2 not in src_pos:
                            continue
                        ra1, dec1 = src_pos[s1]
                        ra2, dec2 = src_pos[s2]
                        cos_dec = np.cos(np.deg2rad((dec1 + dec2) / 2))
                        dra = (ra1 - ra2) * cos_dec * 3600
                        ddec = (dec1 - dec2) * 3600
                        if np.sqrt(dra**2 + ddec**2) < threshold:
                            drop_ids.add(s2)  # keep lowest ID

        self._cached_dup_ids = drop_ids
        return self._cached_dup_ids

    @classmethod
    def load_for_rate_file(cls, rate_file):

        with fits.open(rate_file) as rf:
            MSAMETFL = rf[0].header['MSAMETFL']
            MSAMETID = rf[0].header['MSAMETID']

        with fits.open(MSAMETFL) as mf:
            hdul = deepcopy(mf)

        return cls(hdul, MSAMETFL, MSAMETID)

    @property
    def unique_source_ids(self):
        rows = self.shutter_table[self.shutter_table['msa_metadata_id'] == self.msametid]
        ids = np.unique(rows['source_id'][_source_rows(rows)])

        drop = self._duplicate_source_ids()
        if drop:
            mask = ~np.isin(ids, list(drop))
            if mask.sum() < len(ids):
                from campfire_pipeline.common.io import log
                skipped = sorted(int(s) for s in ids[~mask])
                log(f'Skipping {len(skipped)} duplicate source(s) '
                    f'for msametid={self.msametid}: {skipped}')
                ids = ids[mask]

        return ids

    def is_fixed_slit(self, source_id):
        """Return True if *source_id* is observed through a NIRSpec fixed slit.

        The MSA metafile SHUTTER_INFO table carries a ``fixed_slit`` column that
        is ``'NONE'`` (or empty) for ordinary MSA shutters and the slit name
        (e.g. ``'S200A1'``, ``'S1600A1'``) for fixed-slit apertures. Fixed-slit
        sources need a different extended-wavelength flux calibration, so they
        must be flagged here before the extended photom override is chosen.

        Returns False if the column is absent (older metafiles) so the MSA path
        stays the default.
        """
        if 'fixed_slit' not in self.shutter_table.colnames:
            return False
        rows = self.shutter_table[
            _source_rows(self.shutter_table, source_id)
            & (self.shutter_table['msa_metadata_id'] == self.msametid)
        ]
        return any(
            str(v).strip().upper() not in ('', 'NONE') for v in rows['fixed_slit']
        )

    def fixed_slit_name(self, source_id):
        """Return the fixed-slit aperture name (e.g. ``'S200A1'``) for *source_id*,
        or ``''`` if it is not a fixed-slit source.

        Reads the SHUTTER_INFO ``fixed_slit`` column — the same column
        :meth:`is_fixed_slit` tests — so downstream stages can record *which*
        slit a hybrid fixed-slit-in-MSA source was observed through.
        """
        if 'fixed_slit' not in self.shutter_table.colnames:
            return ''
        rows = self.shutter_table[
            _source_rows(self.shutter_table, source_id)
            & (self.shutter_table['msa_metadata_id'] == self.msametid)
        ]
        for v in rows['fixed_slit']:
            s = str(v).strip().upper()
            if s not in ('', 'NONE'):
                return s
        return ''

    def slitlets_of(self, source_id):
        """The slitlet ids (this ``msametid``) whose rows hold *source_id*."""
        rows = self.shutter_table[self.shutter_table['msa_metadata_id'] == self.msametid]
        return sorted(int(s) for s in np.unique(
            rows['slitlet_id'][_source_rows(rows, source_id)]))

    def rows_for_source(self, source_id):
        """The per-source edit of this exposure's SHUTTER_INFO rows.

        Returns a *copy* of the rows (every dither point of ``msametid``) of the
        slitlet(s) holding *source_id*, edited the way one ``Spec2Pipeline`` run
        for that source wants them: detached background shutters pruned, the one
        shutter holding the source marked ``primary_source = 'Y'`` and every other
        shutter relabelled as an empty background shutter (``source_id = 0``,
        ``background = 'Y'``, no intra-shutter position). A positionally
        coincident twin sharing the slitlet (``_duplicate_source_ids``) becomes a
        background row of this slitlet in the same way.

        This is the row operation :meth:`filter_by_source_id` (one metafile per
        source) and :meth:`for_sources` (one metafile for many sources) share.
        """
        table = self.shutter_table
        slits = np.unique(table['slitlet_id'][_source_rows(table, source_id)])
        condition = np.logical_and.reduce((
            table['msa_metadata_id'] == self.msametid,
            np.isin(table['slitlet_id'], slits)
        ))

        rows = table[condition]

        if len(rows) == 0:
            raise RuntimeError("No IDs matched in metafile!")

        # Remove detached background shutters that are far from the source.
        # Some MSA plans place a background shutter many columns away from
        # the slitlet; the JWST pipeline interprets the full column range
        # as one slit, producing an absurdly wide extraction.  We keep only
        # the contiguous group of shutter columns containing the source.
        rows = _prune_detached_shutters(rows, source_id)

        is_primary = (rows['source_id'] == source_id) & (rows['estimated_source_in_shutter_x'] > 0)
        rows['primary_source'][is_primary] = 'Y'
        rows['primary_source'][~is_primary] = 'N'
        rows['source_id'][~is_primary] = 0

        rows['background'][rows['primary_source'] == 'Y'] = 'N'
        rows['background'][rows['primary_source'] == 'N'] = 'Y'
        rows['estimated_source_in_shutter_x'][rows['primary_source'] == 'N'] = np.nan
        rows['estimated_source_in_shutter_y'][rows['primary_source'] == 'N'] = np.nan
        return rows

    def background_slitlet_ids(self):
        """Slitlets of this ``msametid`` that hold no catalog source at all.

        These are the dedicated background slitlets of the MSA plan: no row is a
        catalog-source row (:func:`_source_rows`) and no row is marked
        ``primary_source = 'Y'`` (jwst's own test, which also catches a slitlet
        holding only a *virtual* source). jwst extracts them as
        ``<prog>_BKG<slitlet>`` slits with ``stellarity = 0`` (``EXTENDED``).
        """
        rows = self.shutter_table[self.shutter_table['msa_metadata_id'] == self.msametid]
        all_ids = set(int(s) for s in np.unique(rows['slitlet_id']))
        with_source = set(int(s) for s in np.unique(rows['slitlet_id'][_source_rows(rows)]))
        primary = rows['primary_source'] == 'Y'
        with_primary = set(int(s) for s in np.unique(rows['slitlet_id'][primary]))
        return sorted(all_ids - with_source - with_primary)

    def background_slitlet_rows(self, slitlet_ids=None):
        """A copy of the rows (all dither points of ``msametid``) of the
        background-only slitlets, untouched. ``slitlet_ids`` defaults to
        :meth:`background_slitlet_ids`."""
        if slitlet_ids is None:
            slitlet_ids = self.background_slitlet_ids()
        condition = np.logical_and.reduce((
            self.shutter_table['msa_metadata_id'] == self.msametid,
            np.isin(self.shutter_table['slitlet_id'], list(slitlet_ids)),
        ))
        return self.shutter_table[condition]

    def for_sources(self, rows_by_source, set_stellarity=False, filename=None,
                    background_slitlets=()):
        """One metafile carrying the per-source edits of many sources at once.

        ``rows_by_source`` maps ``source_id`` to that source's edited rows (from
        :meth:`rows_for_source`, possibly with stuck-closed shutters removed by
        the caller). The slitlet sets must not overlap: jwst refuses a slitlet
        with two ``primary_source = 'Y'`` rows, so two catalog sources sharing a
        slitlet go into different metafiles (``stage2._plan_rounds``).
        ``background_slitlets`` adds the untouched rows of those background-only
        slitlets (:meth:`background_slitlet_rows`).

        The SOURCE_INFO table is kept whole (jwst looks each source up by id; the
        rows of a relabelled twin are simply unused), with ``stellarity`` forced
        to ``set_stellarity`` for every source when it is not ``False`` — the same
        value :meth:`filter_by_source_id` would give each source on its own.
        """
        from astropy.table import vstack

        mf = copy(self)
        mf.hdul = deepcopy(self.hdul)

        parts = list(rows_by_source.values())
        if len(background_slitlets):
            parts.append(self.background_slitlet_rows(background_slitlets))
        if not parts:
            raise RuntimeError("for_sources: nothing to extract")
        mf.shutter_table = parts[0] if len(parts) == 1 else vstack(
            parts, join_type='exact', metadata_conflicts='silent')

        mf.source_table = self.source_table.copy()
        if set_stellarity is not False:
            mf.source_table['stellarity'] = set_stellarity

        if filename is None:
            mf.filename = self.filename.replace('.fits', '_edited.fits')
        else:
            mf.filename = filename
        return mf

    def filter_by_source_id(self,
            source_id,
            set_stellarity=False,
            filename=None,
            force_consistent_xy=False):
        """
        force_consistent_xy : overwrite the intrashutter x/y positions to be the same in all nods (default False)  <- useful for things close to the edge!
        """

        mf = copy(self)
        mf.hdul = deepcopy(self.hdul)

        mf.shutter_table = self.rows_for_source(source_id)

        mf.source_table = mf.source_table[mf.source_table['source_id'] == source_id]
        if set_stellarity is not False:
            mf.source_table['stellarity'] = set_stellarity

        if filename is None:
            mf.filename = self.filename.replace('.fits', f'_{source_id}.fits')
        else:
            mf.filename = filename
        return mf

    def _sync_tables_to_hdul(self):
        """Sync the Table objects back to the HDUList before writing."""
        self.hdul[2] = fits.BinTableHDU(
            data=self.shutter_table.as_array(),
            header=self.hdul[2].header,
            name=self.hdul[2].name
        )
        self.hdul[3] = fits.BinTableHDU(
            data=self.source_table.as_array(),
            header=self.hdul[3].header,
            name=self.hdul[3].name
        )

    def write(self, outdir, overwrite=False):
        outfile = os.path.join(outdir, self.filename)
        if overwrite or not os.path.exists(outfile):
            self._sync_tables_to_hdul()
            self.hdul.writeto(outfile, overwrite=overwrite)
