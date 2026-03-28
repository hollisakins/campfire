"""
MetaFile dataclass: MSA metadata (shutter tables, source tables, MSAMETFL headers).
"""

import os
import numpy as np
from copy import copy, deepcopy
from dataclasses import dataclass
from astropy.io import fits
from astropy.table import Table


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
        shutter_table['shutter_column'][shutter_table['source_id'] == source_id]
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
        ids = np.unique(self.shutter_table['source_id'][self.shutter_table['msa_metadata_id'] == self.msametid])
        return ids[ids > 0]

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

        slits = np.unique(mf.shutter_table['slitlet_id'][mf.shutter_table['source_id'] == source_id])
        condition = np.logical_and.reduce((
            mf.shutter_table['msa_metadata_id'] == mf.msametid,
            np.isin(mf.shutter_table['slitlet_id'], slits)
        ))

        mf.shutter_table = mf.shutter_table[condition]

        if len(mf.shutter_table) == 0:
            raise RuntimeError("No IDs matched in metafile!")

        # Remove detached background shutters that are far from the source.
        # Some MSA plans place a background shutter many columns away from
        # the slitlet; the JWST pipeline interprets the full column range
        # as one slit, producing an absurdly wide extraction.  We keep only
        # the contiguous group of shutter columns containing the source.
        mf.shutter_table = _prune_detached_shutters(
            mf.shutter_table, source_id)

        is_primary = (mf.shutter_table['source_id'] == source_id) & (mf.shutter_table['estimated_source_in_shutter_x'] > 0)
        mf.shutter_table['primary_source'][is_primary] = 'Y'
        mf.shutter_table['primary_source'][~is_primary] = 'N'
        mf.shutter_table['source_id'][~is_primary] = 0

        mf.shutter_table['background'][mf.shutter_table['primary_source'] == 'Y'] = 'N'
        mf.shutter_table['background'][mf.shutter_table['primary_source'] == 'N'] = 'Y'
        mf.shutter_table['estimated_source_in_shutter_x'][mf.shutter_table['primary_source'] == 'N'] = np.nan
        mf.shutter_table['estimated_source_in_shutter_y'][mf.shutter_table['primary_source'] == 'N'] = np.nan

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
