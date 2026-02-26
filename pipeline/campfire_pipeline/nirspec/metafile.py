"""
MetaFile dataclass: MSA metadata (shutter tables, source tables, MSAMETFL headers).
"""

import os
import numpy as np
from copy import copy, deepcopy
from dataclasses import dataclass
from astropy.io import fits
from astropy.table import Table


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

        slits = np.unique(mf.shutter_table['slitlet_id'][mf.shutter_table['source_id'] == source_id])
        condition = np.logical_and.reduce((
            mf.shutter_table['msa_metadata_id'] == mf.msametid,
            np.isin(mf.shutter_table['slitlet_id'], slits)
        ))

        mf.shutter_table = mf.shutter_table[condition]

        if len(mf.shutter_table) == 0:
            raise RuntimeError("No IDs matched in metafile!")

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
