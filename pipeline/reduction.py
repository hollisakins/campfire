"""
NIRSpec reduction

Usage:
python reduction.py --obs capers_uds_p2
"""

import os, sys, glob, warnings, toml, logging, argparse, shutil, functools
from copy import copy, deepcopy
import numpy as np
import matplotlib.pyplot as plt
from astropy.io import fits
from pathlib import Path
from multiprocessing import Pool
from time import sleep
from datetime import datetime
import subprocess
from textwrap import dedent
from asdf.exceptions import AsdfConversionWarning
warnings.simplefilter('ignore', category=AsdfConversionWarning)
from astropy.visualization import ImageNormalize, ZScaleInterval
from functools import partial
from jwst import pipeline, associations
from jwst.assign_wcs.util import NoDataOnDetectorError

plt.style.use('hba_sans')

GRATING_LIMITS = {
    "prism": [0.54, 5.51, 0.01],
    "g140m": [0.55, 3.35, 0.00063],
    "g235m": [1.58, 5.3, 0.00106],
    "g395m": [2.68, 5.51, 0.00179],
    "g140h": [0.68, 1.9, 0.000238],
    "g235h": [1.66, 3.17, 0.000396],
    "g395h": [2.83, 5.24, 0.000666],
}

GRATINGS = [k.upper() for k in GRATING_LIMITS]

DEFAULT_STAGE1_CONFIG = {
    'overwrite': False,
    'do_clean_flicker_noise': True, 
    'mask_science_regions': True, 
    'cleanup_uncal': True, 
    'cleanup_rateints': True, 
    'subtract_background': True,
    'box_size': 8,
    'sigma_clip': True,
    'bkg_estimator': 'median',
    'plot': True,
}

DEFAULT_STAGE2_CONFIG = {
    'overwrite': False,
    'set_stellarity': 1.0, 
    'rectify': True,
}

DEFAULT_STAGE3_CONFIG = {
    'overwrite': False,
    'method': 'nodded',
    'cleanup_asn': True,
    'cleanup_crfs': True,
    'plot_profiles': True,
    'plot_optext': True,
}

def load_config(config_path="config.toml"):
    """Load and parse configuration file with path template expansion."""
    with open(config_path, 'r') as f:
        config = toml.load(f)    
    return config

from dataclasses import dataclass
from typing import Union, List
from astropy.table import Table, Column
from astropy.io.fits import table_to_hdu


def log(*args, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}]", *args, **kwargs)


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
            #MSACONID = hdul[0].header['MSACONID']
        
        with fits.open(MSAMETFL) as mf:
            hdul = deepcopy(mf)

        return cls(hdul, MSAMETFL, MSAMETID)

    @property
    def unique_source_ids(self):
        ids = np.unique(self.shutter_table['source_id'][self.shutter_table['msa_metadata_id']==self.msametid])
        return ids[ids>0]
    

    def filter_by_source_id(self, 
            source_id, 
            set_stellarity=False, 
            filename=None,
            force_consistent_xy=False):
        """
        force_consistent_xy : overwrite the intrashutter x/y positions to be the same in all nods (default False)  <- useful for things close to the edge! 
        """

        mf = copy(self)

        slits = np.unique(mf.shutter_table['slitlet_id'][mf.shutter_table['source_id']==source_id])
        condition = np.logical_and.reduce((
            mf.shutter_table['msa_metadata_id']==mf.msametid,
            np.isin(mf.shutter_table['slitlet_id'], slits)
        ))

        mf.shutter_table = mf.shutter_table[condition]

        if len(mf.shutter_table) == 0:
            raise RuntimeError("No IDs matched in metafile!")
        

        is_primary = (mf.shutter_table['source_id'] == source_id) & (mf.shutter_table['estimated_source_in_shutter_x'] > 0)
        mf.shutter_table['primary_source'][is_primary]='Y'
        mf.shutter_table['primary_source'][~is_primary]='N'
        mf.shutter_table['source_id'][~is_primary]=0

        # mf.shutter_table['source_id'] = source_id
        mf.shutter_table['background'][mf.shutter_table['primary_source']=='Y'] = 'N'
        mf.shutter_table['background'][mf.shutter_table['primary_source']=='N'] = 'Y'
        mf.shutter_table['estimated_source_in_shutter_x'][mf.shutter_table['primary_source'] == 'N'] = np.nan
        mf.shutter_table['estimated_source_in_shutter_y'][mf.shutter_table['primary_source'] == 'N'] = np.nan

        # if force_consistent_xy:
        #     is_primary = mf.shutter_table['primary_source']=='Y'
        #     log(f'Forcing consistent xy positions')
        #     log(f"x = {[f'{x:.3f}' for x in mf.shutter_table['estimated_source_in_shutter_x'][is_primary]]} -> {np.mean(mf.shutter_table['estimated_source_in_shutter_x'][is_primary]):.3f}")
        #     log(f"y = {[f'{y:.3f}' for y in mf.shutter_table['estimated_source_in_shutter_y'][is_primary]]} -> {np.mean(mf.shutter_table['estimated_source_in_shutter_y'][is_primary]):.3f}")
        #     mf.shutter_table['estimated_source_in_shutter_x'][is_primary] = np.mean(mf.shutter_table['estimated_source_in_shutter_x'][is_primary])
        #     mf.shutter_table['estimated_source_in_shutter_y'][is_primary] = np.mean(mf.shutter_table['estimated_source_in_shutter_y'][is_primary])

        mf.source_table = mf.source_table[mf.source_table['source_id']==source_id]
        if set_stellarity is not False: 
            mf.source_table['stellarity'] = set_stellarity

        if filename is None:
            mf.filename = self.filename.replace('.fits',f'_{source_id}.fits')
        else:
            mf.filename = filename
        return mf
    
    def _sync_tables_to_hdul(self):
        """Sync the Table objects back to the HDUList before writing."""
        # Convert Tables back to FITS binary table format
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
        # sleep(0.2)
        

        


@dataclass
class Observation:
    name: str
    field: str
    program_id: int 
    data_subdir: str
    files: List[str]
    gratings: List[str]
    combine_dither: bool = True

    directories_setup: bool = False


    @classmethod
    def load(cls, name, observations_file='observations.toml'):
        
        if not isinstance(observations_file, str):
            raise ValueError
        with open(observations_file, 'r') as f:
            observations_config = toml.load(f)

        if name not in observations_config:
            raise ValueError(f"Observation '{name}' not found in {observations_file}")
        
        obs = observations_config[name]

        field = obs['field']
        data_subdir = obs['data_subdir']
        gratings = obs['gratings']

        files = obs['files']
        if isinstance(files, str):
            files = [files]
        elif isinstance(files, list):
            pass
        else:
            raise TypeError
        
        # # Convert 'ids' field to 'source_ids' and handle 'all' case
        # if 'ids' in obs_config:
        #     obs_config['source_ids'] = obs_config.pop('ids')
        if 'program_id' in obs:
            program_id = int(obs['program_id'])
        else:
            # Extract program ID from JWST filename pattern: jw<ppppp>...
            assert files[0].startswith('jw')
            program_id = int(files[0][2:7])

        if 'combine_dither' in obs:
            combine_dither = bool(obs['combine_dither'])
        else:
            combine_dither = True

        return cls(
            name=name, 
            field=field,
            program_id=program_id,
            data_subdir=data_subdir,
            files=files,
            gratings=gratings,
            combine_dither=combine_dither,
        )
        
    def setup_workspace_directory(self, data_dir, product_dir, overwrite=False):
        """
        Create working directories for an observation.
        
        Parameters:
        -----------
        overwrite : bool
            Whether to overwrite existing workspace

        Returns:
        --------
        str : Path to the workspace directory
        """
        self.workspace_dir = os.path.join(product_dir, self.name)
        
        # Create workspace directory
        if os.path.exists(self.workspace_dir) and overwrite:
            log(f"Removing existing workspace: {self.workspace_dir}")
            shutil.rmtree(self.workspace_dir)
        
        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir, exist_ok=True)
            log(f"Created workspace directory: {self.workspace_dir}")
        
        self.raw_dir = os.path.join(data_dir, self.data_subdir)
        self.uncal_files = self.glob('_uncal.fits', check_exp_type=True, directory=self.raw_dir)

        if len(self.uncal_files)==0:
            raise RuntimeError(f"No raw data files found for observation {self.name}!")
        
        msa_meta_files_needed = set()
        for src_file in self.uncal_files:
            # Read MSAMETFL header from rate file to find associated MSA meta file
            try:
                with fits.open(src_file) as hdul:
                    msametfl = hdul[0].header.get('MSAMETFL', '')
                    if msametfl:
                        # MSAMETFL contains the basename of the MSA meta file
                        msa_meta_files_needed.add(os.path.join(self.raw_dir, msametfl))
                        # log(f"Uncal file {os.path.basename(src_file)} requires MSA meta file: {msametfl}")
                    else:
                        log(f"No MSAMETFL header found in {os.path.basename(src_file)}")
            except Exception as e:
                log(f"Could not read MSAMETFL header from {os.path.basename(src_file)}: {e}")
        self.msa_meta_files = msa_meta_files_needed


        self.rate_files = []
        for uncal_file in self.uncal_files:
            rate_file = uncal_file.replace('_uncal.fits','_rate.fits').replace(self.raw_dir, self.workspace_dir)
            self.rate_files.append(rate_file)

        self.directories_setup = True


    def copy_uncal_files(self, overwrite=False):

        log(self.rate_files)
        if all([os.path.exists(f) for f in self.rate_files]) and not overwrite:
            log('All rate files already exist, and overwrite=False! aborting stage1')
            return False
            
        # Copy rate files to workspace and track MSA meta files needed
        copied_files = []        
        for src_file in self.uncal_files:
            dst_file = os.path.join(self.workspace_dir, os.path.basename(src_file))
            rate_file = dst_file.replace('_uncal.fits','_rate.fits')
            if not os.path.exists(dst_file) and (not os.path.exists(rate_file) or overwrite):
                log(f"Copying {os.path.basename(src_file)} to workspace")
                shutil.copy2(src_file, dst_file)
            copied_files.append(dst_file)
            
        for msa_meta_file in self.msa_meta_files:
            dst_msa_meta_file = os.path.join(self.workspace_dir, os.path.basename(msa_meta_file))
            
            if os.path.exists(msa_meta_file):
                if not os.path.exists(dst_msa_meta_file):
                    log(f"Copying MSA meta file {os.path.basename(msa_meta_file)} to workspace")
                    shutil.copy2(msa_meta_file, dst_msa_meta_file)
                else:
                    log(f"MSA meta file {os.path.basename(msa_meta_file)} already exists in workspace")
            else:
                log(f"MSA meta file not found: {msa_meta_file}")

        return True

    @property 
    def stuck_closed_shutters_file(self):
        return os.path.join(self.workspace_dir, f'_{self.name}_stuck_closed_shutters.toml')
 
    @property 
    def bkg_override_file(self):
        return os.path.join(self.workspace_dir, f'_{self.name}_nodded_background_overrides.toml')

    @property 
    def stuck_closed_shutters(self):
        
        file = os.path.join(self.stuck_closed_shutters_file)
        if not os.path.exists(file):
            log(f'No stuck closed shutter file found, creating blank {file}')
            with open(file, 'w') as f:
                docstring = dedent(f"""
                # vetted stuck closed shutter list for {self.name}
                # format is a table for each "root" file name, which 
                # consists of obs/visit/config, e.g. "jw06368001001_03101"
                # the list of stuck closed shutters for a source ID should
                # be given as a key-value pair in the table; for example:
                # [jw06368001001_03101] 
                #     12345 = [1,2,3]
                """)
                f.write(docstring)
        
        data = toml.load(file)
        
        # Build lists for each column
        roots = []
        source_ids = []
        shutters = []
        
        for root in data:
            for entry in data[root]:
                roots.append(root)
                source_ids.append(int(entry))
                shutters.append(list(data[root][entry]))
        
        # Create table from columns
        tab = Table({
            'root': roots,
            'source_id': source_ids,
            'shutters': shutters
        })
        
        return tab
    

    @property 
    def bkg_overrides(self):
        
        file = os.path.join(self.bkg_override_file)
        if not os.path.exists(file):
            log(f'No nodded background override found, creating blank {file}')
            with open(file, 'w') as f:
                docstring = dedent(f"""
                # contaminated shutter list for {self.name}
                # format is like the stuck closed shutter list, e.g., there's
                # a table for each "root" file name, which 
                # consists of obs/visit/config, e.g. "jw06368001001_03101"
                # the contaminated shutters in each nod for a source ID should
                # be given as a key-value pair in the table; for example:""" + """
                # [jw06368001001_03101] 
                #     12345 = {3: [1]}
                # (for source 12345, shutter 1 in nod 3 is contaminated and
                #  should be considered closed)
                """)
                f.write(docstring)
        
        data = toml.load(file)
        result = {}
        for root in data:
            result[root] = {}
            for srcid in data[root]:
                result[root][srcid] = {}
                for nod in data[root][srcid]:
                    result[root][srcid][nod] = list(data[root][srcid][nod])
        
        return result
    
    @property
    def stuck_closed_shutters_mtime(self):
        """Return the modification time of the vetted stuck closed shutters file.
        
        Returns
        -------
        float
            Unix timestamp of last modification, suitable for storing in FITS headers
        """
        return os.path.getmtime(self.stuck_closed_shutters_file)

    @property
    def bkg_override_mtime(self):
        """Return the modification time of the nodded background override file.
        
        Returns
        -------
        float
            Unix timestamp of last modification, suitable for storing in FITS headers
        """
        return os.path.getmtime(self.bkg_override_file)


    def glob(self, ext, check_exp_type=False, directory=None):
        """
        Search for files in a the Observation's workspace directory, matching the Observaion file specification and a given extension. 
        If directory kwarg is provided, search that directory instead of Observation.workspace_dir
        """
        if directory is None:
            directory = self.workspace_dir

        result = []
        for file_pattern in self.files:
            pattern_path = os.path.join(directory, file_pattern + ext)
            resulti = glob.glob(pattern_path)

            if check_exp_type:
                result += [r for r in resulti if fits.getheader(r)['EXP_TYPE']=='NRS_MSASPEC']
            else:
                result += resulti

        return sorted(result)
    

    def discover(self, ext):
        """
        IN DEVELOPMENT, discover files and group appropriately         
        """
        # Discover *_cal.fits files in the workspace directory
        files = Table()
        files['path'] = self.glob(ext=ext)
        files['name'] = [os.path.basename(f['path']) for f in files]
        files['source_id'] = [int(f['name'].split('_')[-2]) for f in files]
        files['detector'] = ['nrs'+f['name'].split('nrs')[-1][0] for f in files]
        files['obs'] = [f['name'].split('_')[0] for f in files]
        files['filter']=[fits.getheader(f['path'])['FILTER'] for f in files]
        files['grating']=[fits.getheader(f['path'])['GRATING'] for f in files]
        files['filter_grating'] = [f['grating'].lower()+'_'+f['filter'].lower() for f in files]
        files['config'] = [f['name'].split('_')[1] for f in files]
        files['nod'] = [f['name'].split('_')[2] for f in files]

        # PATTTYPE = 'NONE' for most, PATTTYPE = '...' for UNCOVER
        # NOD_TYPE = '3-SHUTTER-SLITLET'

        # files.pprint()

        source_ids_to_process = np.unique(files['source_id'])
        if source_ids != 'all': 
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]

        for source_id in source_ids_to_process:
            files1 = files[files['source_id']==source_id]
            
            for detector in np.unique(files1['detector']):
                files2 = files1[files1['detector']==detector]

                for observation in np.unique(files2['obs']):
                    files3 = files2[files2['obs']==observation]

                    for config in np.unique(files3['config']):
                        files4 = files3[files3['config'] == config]

                        files4.pprint()
                        log('########################################################################')
                        # continue

SHUTTERS_PER_ROW = 365
def flag_pixels_for_shutter(input_model, shutter_row, shutter_col, shutter_quadrant):
    from stdatamodels.jwst.transforms.models import Slit
    from jwst.msaflagopen.msaflag_open import flag
    from jwst.msaflagopen.msaflagopen_step import create_reference_filename_dictionary

    result = input_model.copy()
    wcs_reffile_names = create_reference_filename_dictionary(result)
    print(wcs_reffile_names)

    shutter_id = shutter_row + (shutter_col - 1) * SHUTTERS_PER_ROW
    slitlets = [Slit(0, shutter_id, 0, shutter_row, shutter_col, -0.5, 0.5, shutter_quadrant, 0, "x", slit_id=0)]

    result = flag(result, slitlets, wcs_reffile_names)

    return result


def mask_slits(input_model, mask):
    """
    Flag pixels within science regions.

    Find pixels located within MOS or fixed slit footprints
    and flag them in the mask, so that they do not get used.

    Adapted from jwst.clean_flicker_noise.clean_flicker_noise 
    to extend the masks to cover the full traces

    Parameters
    ----------
    input_model : `~jwst.datamodels.JwstDataModel`
        Science data model.

    mask : array-like of bool
        2D input mask that will be updated. True indicates background
        pixels to be used. Slit regions will be set to False.

    Returns
    -------
    mask : array-like of bool
        2D output mask with additional flags for slit pixels
    """
    from gwcs.utils import _toindex
    from jwst.assign_wcs import AssignWcsStep, nirspec

    # Get the slits from the WCS object
    slits = input_model.meta.wcs.get_transform("gwa", "slit_frame").slits

    # Loop over the slits, marking all the pixels within each bounding
    # box as False (do not use) in the mask.
    match input_model.meta.instrument.grating:
        case 'PRISM':
            dxlo, dxhi = 10, 25
        case 'G395M': 
            dxlo, dxhi = 10, 120
        case _: 
            raise NotImplementedError

    for slit in slits:
        slit_wcs = nirspec.nrs_wcs_set_input(input_model, slit.name)
        xlo, xhi = _toindex(slit_wcs.bounding_box[0])
        ylo, yhi = _toindex(slit_wcs.bounding_box[1])
        xlo = np.max([0, xlo-dxlo])
        xhi = np.min([2048, xhi+dxhi])
        ylo += 3
        yhi -= 3
        mask[..., ylo:yhi, xlo:xhi] = False

    return mask

# import jwst
# jwst.clean_flicker_noise.clean_flicker_noise.mask_slits = mask_slits

def subtract_background_from_rate_file(
        rate_file: str,
        box_size: int = 8,
        sigma_clip: bool = True,
        bkg_estimator: str = 'median',
        plot: bool = True,
        save_backup: bool = False,
        pictureframe_dir: str = None,
    ):
    from stdatamodels import util as stutil
    from jwst.datamodels import ImageModel
    from jwst.clean_flicker_noise.clean_flicker_noise import _make_processed_rate_image
    from astropy.stats import median_absolute_deviation

    with ImageModel(rate_file) as model:
        
        for entry in model.history:
            if 'Subtracted pedestal, rescaled variance' in entry['description']:
                log(f'Variance rescaling already done for {os.path.basename(rate_file)}, skipping...')
                return

        log(f'Subtracting background and rescaling variance for {os.path.basename(rate_file)}')
        processed_model = _make_processed_rate_image(model, single_mask=True, input_dir=os.path.dirname(rate_file), exp_type="NRS_MSASPEC", mask_science_regions=True, flat=None)
        
        mask_file = rate_file.replace('_rate.fits','_mask.fits')
        if not os.path.exists(mask_file): 
            raise FileNotFoundError("No mask file found!")
        log(f'Using existing mask {os.path.basename(mask_file)}')
        mask = np.array(fits.getdata(mask_file,ext=1),dtype=bool)

        new_mask = ~mask_slits(processed_model, mask)
        new_mask |= model.dq>0


        detector = 'nrs2' 
        if 'nrs1' in rate_file:
            detector = 'nrs1'
        
        if pictureframe_dir:
            log(f'Subtracting "picture frame" template files')
            if detector == 'nrs1':
                pictureframe_file = os.path.join(pictureframe_dir,'jwst_nirspec_pictureframe_0002.fits')
            else:
                pictureframe_file = os.path.join(pictureframe_dir,'jwst_nirspec_pictureframe_0001.fits')

            pictureframe_template = fits.getdata(pictureframe_file)
            
            # rescale the picture frame template so that its ~close to the data median
            pictureframe_template *= np.nanmedian(model.data[~new_mask])

            coeffs = np.linspace(0.5, 1.5, 100)
            var = np.zeros_like(coeffs)
            for i,c in enumerate(coeffs):
                sub = model.data - c*pictureframe_template
                sub[new_mask] = np.nan
                sigma_mad = median_absolute_deviation(sub, ignore_nan=True)
                var[i] = sigma_mad**2

            bkg2d = pictureframe_template * coeffs[np.argmin(var)]
        else:
            bkg2d = np.zeros_like(model.data)


        from photutils.background import Background2D, MedianBackground
        from astropy.stats import SigmaClip
        match bkg_estimator:
            case 'median': 
                bkg_est = MedianBackground()
            case _:
                bkg_est = None
        if sigma_clip: 
            sclip = SigmaClip(sigma=3.0, maxiters=5)
        else:
            sclip = None

        bkg = Background2D(
            model.data-bkg2d,
            box_size=box_size,
            filter_size=(3,3),
            mask=new_mask,
            sigma_clip=sclip,
            bkg_estimator=bkg_est,
        )

        bkg2d += bkg.background

        rate_masked = model.data - bkg2d
        rate_masked[new_mask] = np.nan

        col = np.nanmedian(rate_masked, axis=0)[np.newaxis,:]
        rate_masked = rate_masked - col

        full_row_masked = np.sum(np.isfinite(rate_masked),axis=1)==0
        rate_masked[full_row_masked,:] = np.nanmedian(rate_masked) 
        row = np.nanmedian(rate_masked, axis=1)[:,np.newaxis]

        rate_new = model.data - bkg2d - col - row

        if plot:
            from astropy.visualization import ImageNormalize, ZScaleInterval
            norm = ImageNormalize(model.data[~new_mask], interval=ZScaleInterval())
            fig, ax = plt.subplots(2,3,figsize=(8,6),sharex=True,sharey=True)
            ax[0,0].imshow(model.data, norm=norm)
            ax[0,0].set_title('Raw rate file')
            ax[0,1].imshow(bkg2d, norm=norm)
            if pictureframe_dir:
                ax[0,1].set_title('Picture frame (+2D bkg model)')
            else:
                ax[0,1].set_title('Modeled 2D background')
            ax[0,2].imshow(model.data-bkg2d, norm=norm)
            ax[0,2].set_title('Raw - 2D')
            ax[1,0].imshow(np.zeros_like(model.data)+col, norm=norm)
            ax[1,0].set_title('Column 1/f')
            ax[1,1].imshow(np.zeros_like(model.data)+row, norm=norm)
            ax[1,1].set_title('Row 1/f')
            ax[1,2].imshow(model.data-bkg2d-col-row, norm=norm)
            ax[1,2].set_title('Final (raw-2D-col-row)')
            plot_file = rate_file.replace('_rate.fits','_bkg.pdf')
            log(f'Saving to {plot_file}')
            plt.savefig(plot_file)
            plt.close()

        model.data = rate_new

        nsci = model.data / np.sqrt(model.var_rnoise)
        from astropy.stats import sigma_clipped_stats
        rms = sigma_clipped_stats(nsci[~new_mask])[2]
        log(f'Scaling up VAR_RNOISE by {rms**2:.2f}')
        model.var_rnoise = model.var_rnoise * rms**2


        log(f"Saving to {os.path.basename(rate_file)}")
        time = datetime.now()
        stepdescription = f"Subtracted pedestal, rescaled variance {time.strftime('%Y-%m-%d %H:%M:%S')}"
        substr = stutil.create_history_entry(stepdescription)
        model.history.append(substr)

        if save_backup:
            shutil.copy2(rate_file, rate_file.replace('_rate.fits', '_rate_before_bkgsub.fits'))

        model.save(rate_file)


            
def run_stage1_single_uncal(
        uncal_file, 
        workspace_dir, 
        do_clean_flicker_noise=True, 
        mask_science_regions=True,
        cleanup_uncal=True, 
        cleanup_rateints=True,
    ):
    """
    Runs the JWST Detector1Pipeline on a single *_uncal.fits file. 
    Optionally includes the clean_flicker_noise step. 
    """
        
    # Handle directory changes
    prev_cwd = os.getcwd()
    
    os.chdir(workspace_dir)
    
    try:
        from jwst.pipeline import Detector1Pipeline
        steps = {
                'clean_flicker_noise' :{
                    'skip': not do_clean_flicker_noise,
                    'mask_science_regions':mask_science_regions,
                    'save_mask': True,
                },
                'jump': {
                    'skip': False, # testing, should be False normally
                    'expand_large_events': True, # testing, should be True normally
                }
            }
        Detector1Pipeline.call(uncal_file,
            save_results=True,
            steps=steps,
        )
        if cleanup_uncal:
            log(f'Finished Detector1Pipeline for {uncal_file}, removing...')   
            os.remove(uncal_file)
        if cleanup_rateints:
            os.remove(uncal_file.replace('_uncal.fits', '_rateints.fits'))

        return 1

    except Exception as e:
        error_msg = f"Failed stage1 processing for {uncal_file}: {e}"
        log(error_msg)
        # logger.error(error_msg)
        return 0
    
    finally:
        # Always restore working directory
        os.chdir(prev_cwd)





    
def run_stage2a_single_rate(
        rate_file, 
        obs: Observation, 
        set_stellarity: bool = False, 
        source_ids='all', 
        overwrite: bool = False, 
        **kwargs
    ): 

    log(f'Starting stage2a for {os.path.basename(rate_file)}')

    from jwst.pipeline import Spec2Pipeline
    from jwst.associations import asn_from_list
    
    # Handle directory changes
    prev_cwd = os.getcwd()
    
    os.chdir(obs.workspace_dir)


    # figure out list of source ids to extract from this rate file (using meta file)
    main_metafile = MetaFile.load_for_rate_file(rate_file)
    # update rate file header to point to the right metafile
    with fits.open(rate_file, mode='update') as hdul:
        hdul[0].header['OGMETFL'] = main_metafile.filename
        hdul.flush()
    
    root = '_'.join(os.path.basename(rate_file).split('_')[:2])
    nod = int(os.path.basename(rate_file).split('_')[2])
    dither_point_index = fits.getheader(rate_file)['PATT_NUM']

    source_ids_processed = []
    try:
        source_ids_to_process = main_metafile.unique_source_ids
        if source_ids != 'all':
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]


        # if overwrite: 
        #     # Remove any existing per-source metafiles
        #     metafiles = glob.glob(main_metafile.filename.replace('.fits','_*.fits'))
        #     for metafile in metafiles:
        #         if int(metafile.split('_')[-1].split('.')[0]) in source_ids_to_process:
        #             try:
        #                 os.remove(metafile)
        #             except: 
        #                 pass

        
        if len(source_ids_to_process)==0:
            log('No source IDs to process, exiting...')
            return []
        
        for source_id in source_ids_to_process:
            prod_name = os.path.basename(rate_file).replace('_rate.fits', f'_{source_id}')

            if os.path.exists(f'{prod_name}_cal.fits') and not overwrite:
                log(f'Skipping stage2a for {prod_name}, overwrite=False')
                source_ids_processed.append(source_id)
                continue


            # copy the metafile to a source-specific metafile
            # modify that source-specific meta file to only have 1 source in it 
            source_metafile = main_metafile.filter_by_source_id(source_id, filename=os.path.basename(rate_file).replace('_rate.fits',f'_msa_{source_id}.fits'), set_stellarity=set_stellarity)
            # source_metafile.shutter_table.pprint()

            # remove shutters that are marked as stuck closed 
            # adapted from Anthony & Pablo's routines
            cards = []
            stuck = obs.stuck_closed_shutters[(obs.stuck_closed_shutters['root']==root)&(obs.stuck_closed_shutters['source_id']==source_id)]
            cards.append(('STKSHFIL', os.path.basename(obs.stuck_closed_shutters_file), 'Stuck shutter file name'))
            cards.append(('STKSHTIM', obs.stuck_closed_shutters_mtime, 'Stuck shutter file mtime'))
            if len(stuck) > 0:
                cards.append(('STKSHTRS', str(stuck['shutters'][0]), 'Stuck shutters masked'))
                for stuck_shutter in np.sort(stuck['shutters'][0])[::-1]:
                    print(stuck_shutter)
                    stuck_shutter_column = np.sort(np.unique(source_metafile.shutter_table['shutter_column']))[stuck_shutter-1] # get number of stuck shutter
                    source_metafile.shutter_table = source_metafile.shutter_table[source_metafile.shutter_table['shutter_column'] != stuck_shutter_column] # remove shutter from metafile
            else:
                cards.append(('STKSHTRS', 'N/A', 'Stuck shutters masked'))

            source_metafile.write(obs.workspace_dir, overwrite=True)

            # update rate file header to point to the right metafile
            with fits.open(rate_file, mode='update') as hdul:
                hdul[0].header['MSAMETFL'] = source_metafile.filename
                hdul.flush()

            association = [(os.path.basename(rate_file), 'science')]
            asn = asn_from_list.asn_from_list(association, with_exptype=True, product_name=prod_name)
            suggested_name, serialization = asn.dump()
            asn_file = f'{prod_name}.json'
            with open(asn_file,'w') as asn_out: 
                asn_out.write(serialization)


            steps = {
                'extract_1d': {
                    'skip': True,
                },
                'barshadow': {
                    'skip': True,
                },
                'bkg_subtract': {
                    'skip': True,
                },
                'resample_spec':{
                    'skip': True,
                },
                # for extended wavelength range testing
                # 'pathloss':{
                #     'skip':True,
                # },
                # 'flat_field':{
                #     'skip': True,
                #     #'override_fflat':'modified_jwst_nirspec_fflat_0163.fits'
                # },
                # 'photom':{
                #     'skip': True,
                # },
                # 'wavecorr':{
                #     'skip':True,
                # },
                # 'assign_wcs':{
                #     'skip':False,
                #     'override_wavelengthrange': '/Users/hba423/simmons/crds/references/jwst/nirspec/jwst_nirspec_wavelengthrange_0008_ext5p5.asdf',
                # },
            }
            
            log(f"Running Spec2Pipeline for {prod_name}")
            try:
                result = Spec2Pipeline.call(
                    asn_file, 
                    save_results=True,
                    steps=steps)
                log(f'Completed Spec2Pipeline for {prod_name}')
                source_ids_processed.append(source_id)

            except NoDataOnDetectorError:
                log(f'No data on detector for {prod_name}')

            for ext in ['cal','s2d']:
                if os.path.exists(f'{prod_name}_{ext}.fits'):
                    with fits.open(f'{prod_name}_{ext}.fits', mode='update') as hdul:
                        
                        for card in cards:
                            hdul['PRIMARY'].header[card[0]] = card[1:3]
                            
                        hdul.flush()

            if os.path.exists(asn_file):
                os.remove(asn_file)
            
            if os.path.exists(source_metafile.filename):
                os.remove(source_metafile.filename)

    except KeyboardInterrupt:
        return source_ids_processed

    except Exception as e:
        log(f"ERROR in source ID {source_id}", e)
        raise
        return source_ids_processed

    finally:

        # Restore original metafile info in header
        with fits.open(rate_file, mode='update') as hdul:
            if 'OGMETFL' in hdul[0].header:
                hdul[0].header['MSAMETFL'] = hdul[0].header['OGMETFL']
            else:
                hdul[0].header['MSAMETFL'] = main_metafile.filename

        # Cleanup any remaining ASN files
        asn_files = glob.glob(rate_file.replace('_rate.fits','*.json'))
        for asn_file in asn_files:
            os.remove(asn_file)

        os.chdir(prev_cwd)
    
    return source_ids_processed


def fix_units(source_id, workspace_dir):
    """
    Fixes unit issues arising from the JWST pipeline using different pathloss corrections when the source isn't present in the slitlet
    """
    # TODO MAYBE JUST USE UNIFORM PATHLOSS FOR EVERYTHING??

    all_cal_files = sorted(glob.glob(os.path.join(workspace_dir, f"jw*_{source_id}_cal.fits")))
    roots = sorted(list(set(['_'.join(os.path.basename(f).split('_')[0:2]) for f in all_cal_files])))
    # TODO replace this logic with a more careful parsing of files, a la stage2b (break off into separate function/method?)

    for root in roots:
        log(f'Checking output units for {root}')
        nrs1_cal_files = sorted(glob.glob(os.path.join(workspace_dir, f'{root}_*_nrs1_{source_id}_cal.fits')))
        nrs2_cal_files = sorted(glob.glob(os.path.join(workspace_dir, f'{root}_*_nrs2_{source_id}_cal.fits')))
        nrs1_nods = [f.split('_')[-4] for f in nrs1_cal_files]
        nrs2_nods = [f.split('_')[-4] for f in nrs2_cal_files]
        nods = list(set(nrs1_nods + nrs2_nods))

        if len(nods) in [2,3]:

            needs_correction = False
            if len(nrs1_cal_files)>0 and len(nrs2_cal_files)>0:
                nods_to_correct = {'nrs1':[], 'nrs2':[]}
            elif len(nrs1_cal_files)>0:
                nods_to_correct = {'nrs1':[]}
            elif len(nrs2_cal_files)>0:
                nods_to_correct = {'nrs2':[]}

            for nod in nods:
                nrs1_file = os.path.join(workspace_dir, f'{root}_{nod}_nrs1_{source_id}_cal.fits')
                nrs2_file = os.path.join(workspace_dir, f'{root}_{nod}_nrs2_{source_id}_cal.fits')
                if os.path.exists(nrs1_file):
                    with fits.open(nrs1_file, mode='update') as nrs1:
                        
                        if 'PTHLOSS' in nrs1['SCI'].header: 
                            pthloss = nrs1['SCI'].header['PTHLOSS']
                        else:
                            pthloss = 'POINT'
                        
                        if pthloss == 'UNIFORM' and nrs1['SCI'].header['PHOTMJSR'] != 1.:
                            nods_to_correct['nrs1'].append(nod)
                            needs_correction = True
                        else:
                            nrs1['PRIMARY'].header['SRCFLUX'] = ('T', 'Source flux present in exposure? T/F')
                            nrs1['PRIMARY'].header['BUNIT'] = 'MJy'
                            nrs1['SCI'].header['BUNIT'] = 'MJy'
                            nrs1.flush()

                if os.path.exists(nrs2_file):
                    with fits.open(nrs2_file, mode='update') as nrs2:
                        
                        if 'PTHLOSS' in nrs2['SCI'].header: 
                            pthloss = nrs2['SCI'].header['PTHLOSS']
                        else:
                            pthloss = 'POINT'
                        
                        if pthloss == 'UNIFORM' and nrs2['SCI'].header['PHOTMJSR'] != 1.:
                            nods_to_correct['nrs2'].append(nod)
                            needs_correction = True
                        else:
                            nrs2['PRIMARY'].header['SRCFLUX'] = ('T', 'Source flux present in exposure? T/F')
                            nrs2['PRIMARY'].header['BUNIT'] = 'MJy'
                            nrs2['SCI'].header['BUNIT'] = 'MJy'
                            nrs2.flush()

            #print(nods_to_correct)

            if needs_correction:
                log(f'Correcting units for {root}: {nods_to_correct}')

                for detector in nods_to_correct:

                    for n in nods_to_correct[detector]:
                        with fits.open(os.path.join(workspace_dir, f'{root}_{n}_{detector}_{source_id}_cal.fits'), mode='update') as hdul:
                            #pathloss_un = hdul['PATHLOSS_UN'].data
                            photmjsr = hdul['SCI'].header['PHOTMJSR']
                            scale_factor = 1 / photmjsr # undo pathloss_un and apply pathloss_ps (from the other nods!)
                            hdul['SCI'].data *= scale_factor # SCI
                            hdul['ERR'].data *= scale_factor # ERR
                            hdul['VAR_POISSON'].data *= scale_factor**2 # VAR
                            hdul['VAR_RNOISE'].data *= scale_factor**2 # VAR
                            hdul['VAR_FLAT'].data *= scale_factor**2 # VAR

                            hdul['SCI'].header['PHOTMJSR'] = 1.0
                            # hdul['SCI'].header['PTHLOSS'] = 'POINT'

                            hdul['PRIMARY'].header['SRCFLUX'] = ('F', 'Source flux present in exposure? T/F')
                            hdul['PRIMARY'].header['BUNIT'] = 'MJy'
                            hdul['SCI'].header['BUNIT'] = 'MJy'
                            hdul.flush()

            else:
                pass
                # for detector in nods_to_correct:
                #     log(f'Using average PATHLOSS_PS from nods {nods}')

                #     avg_pathloss_ps = None
                #     for n in nods:
                #         with fits.open(os.path.join(workspace_dir, f'{root}_{n}_{detector}_{source_id}_cal.fits')) as hdul:
                #             if avg_pathloss_ps is None:
                #                 avg_pathloss_ps = hdul['PATHLOSS_PS'].data
                #             else:
                #                 avg_pathloss_ps += hdul['PATHLOSS_PS'].data

                #     avg_pathloss_ps /= len(nods)

                #     for n in nods:
                #         with fits.open(os.path.join(workspace_dir, f'{root}_{n}_{detector}_{source_id}_cal.fits'), mode='update') as hdul:
                #             pathloss_ps = hdul['PATHLOSS_PS'].data
                #             scale_factor = pathloss_ps / avg_pathloss_ps # undo pathloss_ps and apply average pathloss_ps (from the valid nods!)
                #             hdul['SCI'].data *= scale_factor # SCI
                #             hdul['ERR'].data *= scale_factor # ERR
                #             hdul['VAR_POISSON'].data *= scale_factor**2 # VAR
                #             hdul['VAR_RNOISE'].data *= scale_factor**2 # VAR
                #             hdul['VAR_FLAT'].data *= scale_factor**2 # VAR
                #             hdul.flush()

        else:
            raise NotImplementedError



def resample_single_exposure(
        cal_file: str,
        workspace_dir: str, 
    ):
    # this function shouldn't be doing any file discovery! 

    # Handle directory changes
    prev_cwd = os.getcwd()
    
    os.chdir(workspace_dir)

    try:
        # from jwst.assign_wcs import AssignWcsStep
        from jwst.datamodels import MultiSlitModel, ImageModel
        from jwst.pixel_replace import PixelReplaceStep
        from jwst.resample import ResampleSpecStep
        pixel_replace = PixelReplaceStep()
        resample_spec = ResampleSpecStep() # do these need args? 

        model = MultiSlitModel(cal_file)
        
        resampled = model.copy()
        if resampled.meta.cal_step.pathloss == 'COMPLETE':
            from jwst.pathloss import PathLossStep
            pathloss = PathLossStep()
            resampled = pathloss.call(resampled, inverse=True)
        resampled = pixel_replace.call(resampled)
        resampled = resample_spec.call(resampled)
        s2d_file_out = cal_file.replace('_cal.fits', '_s2d.fits')
        resampled.save(s2d_file_out)
        resampled.close()
        model.close()
                
    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)        



def plot_stage2a_results(source_id, workspace_dir):
    all_s2d_files = sorted(glob.glob(os.path.join(workspace_dir, f"jw*_{source_id}_s2d.fits")))
    roots = sorted(list(set(['_'.join(os.path.basename(f).split('_')[0:2]) for f in all_s2d_files])))
    # TODO replace this logic with a more careful parsing of files, a la stage2b (break off into separate function/method?)

    for root in roots:
        log(f'Plotting {root}_{source_id}')
        nrs1_s2d_files = sorted(glob.glob(os.path.join(workspace_dir, f'{root}_*_nrs1_{source_id}_s2d.fits')))
        nrs2_s2d_files = sorted(glob.glob(os.path.join(workspace_dir, f'{root}_*_nrs2_{source_id}_s2d.fits')))
        nrs1_nods = [f.split('_')[-4] for f in nrs1_s2d_files]
        nrs2_nods = [f.split('_')[-4] for f in nrs2_s2d_files]
        nods = sorted(list(set(nrs1_nods + nrs2_nods)))

        if len(nrs1_s2d_files)>0:
            nrs1_s2d_files = list(np.array(nrs1_s2d_files)[np.argsort(nods)])
        if len(nrs2_s2d_files)>0:
            nrs2_s2d_files = list(np.array(nrs2_s2d_files)[np.argsort(nods)])

        if len(nrs1_s2d_files)==0 and len(nrs2_s2d_files)==0: # has no data
            raise RuntimeError("No files to plot!")
        
        elif len(nrs1_s2d_files)>0 and len(nrs2_s2d_files)>0: # has multiple 
            Nnods = len(nods)

            nrs1_shape = np.shape(fits.getdata(nrs1_s2d_files[0], ext=1))
            nrs2_shape = np.shape(fits.getdata(nrs2_s2d_files[0], ext=1))

            nrs1_ratio = nrs1_shape[1]/(nrs1_shape[1]+nrs2_shape[1]) * 6
            nrs2_ratio = 6 - nrs1_ratio

            fig, ax = plt.subplots(Nnods, 4, 
                figsize=(7*1.5,Nnods*1.5),
                width_ratios=[nrs1_ratio,0.5,nrs2_ratio,0.5], 
                constrained_layout=True) # figsize, width_ratios
            
            fig.suptitle(f'{root}_*_nrs?_{source_id}_s2d.fits', fontname='monospace')

            data = np.array([])
            for i in range(Nnods):
                nrs1_file = os.path.join(workspace_dir, f'{root}_{nods[i]}_nrs1_{source_id}_s2d.fits')
                nrs2_file = os.path.join(workspace_dir, f'{root}_{nods[i]}_nrs2_{source_id}_s2d.fits')
                if os.path.exists(nrs1_file):
                    nrs1 = fits.getdata(nrs1_file, ext=1)
                    data = np.append(data, nrs1[np.isfinite(nrs1)].flatten())
                if os.path.exists(nrs2_file):
                    nrs2 = fits.getdata(nrs2_file, ext=1)
                    data = np.append(data, nrs2[np.isfinite(nrs2)].flatten())
            norm = ImageNormalize(data, interval=ZScaleInterval())

            for i in range(Nnods):
                nrs1_file = os.path.join(workspace_dir, f'{root}_{nods[i]}_nrs1_{source_id}_s2d.fits')
                nrs2_file = os.path.join(workspace_dir, f'{root}_{nods[i]}_nrs2_{source_id}_s2d.fits')
                nrs1 = fits.getdata(nrs1_file, ext=1)
                nrs2 = fits.getdata(nrs2_file, ext=1)

                if os.path.exists(nrs1_file):
                    nrs1 = fits.getdata(nrs1_file)
                    ax[i,0].imshow(nrs1, norm=norm, origin='lower', aspect='auto', interpolation='nearest')   
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmean(nrs1, axis=1)
                    ax[i,1].step(prof, np.arange(np.shape(nrs1)[0])-0.5, where='pre', linewidth=1, color='k')

                if os.path.exists(nrs2_file):
                    ax[i,2].imshow(nrs2, norm=norm, origin='lower', aspect='auto', interpolation='nearest')   
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmean(nrs2, axis=1)
                    ax[i,3].step(prof, np.arange(np.shape(nrs2)[0])-0.5, where='pre', linewidth=1, color='k')

                ax[i,1].tick_params(labelleft=False)
                ax[i,2].tick_params(labelleft=False)
                ax[i,3].tick_params(labelleft=False)
                ax[i,1].set_ylim(*ax[i,0].get_ylim())
                ax[i,2].set_ylim(*ax[i,0].get_ylim())
                ax[i,3].set_ylim(*ax[i,0].get_ylim())
                if i==0:
                    ax[i,0].set_title(f'nrs1', fontname='monospace')
                    ax[i,2].set_title(f'nrs2', fontname='monospace')
                ax[i,3].set_ylabel(nods[i], fontname='monospace')
                ax[i,3].yaxis.set_label_position("right")

            xmins, xmaxs = [], []
            for i in range(Nnods-1):
                ax[i,0].tick_params(labelbottom=False)
                ax[i,1].tick_params(labelbottom=False)
                ax[i,2].tick_params(labelbottom=False)
                ax[i,3].tick_params(labelbottom=False)
            
            xmins, xmaxs = [], []
            for i in range(Nnods):
                xmini, xmaxi = ax[i,1].get_xlim()
                xmins.append(xmini)
                xmaxs.append(xmaxi)
            for i in range(Nnods):
                ax[i,1].set_xlim(min(xmins), max(xmaxs))
            
            xmins, xmaxs = [], []
            for i in range(Nnods):
                xmini, xmaxi = ax[i,3].get_xlim()
                xmins.append(xmini)
                xmaxs.append(xmaxi)
            for i in range(Nnods):
                ax[i,3].set_xlim(min(xmins), max(xmaxs))

            plt.savefig(os.path.join(workspace_dir, f'{root}_{source_id}_nods.pdf'))
            plt.close()

        else: 
            files = nrs1_s2d_files or nrs2_s2d_files
            Nnods = len(files)

            fig, ax = plt.subplots(Nnods, 2, 
                figsize=(7*1.5,Nnods*1.5),
                width_ratios=[6,1], 
                constrained_layout=True) # figsize, width_ratios
            
            if 'nrs1' in files[0]:
                fig.suptitle(f'{root}_*_nrs1_{source_id}_s2d.fits', fontname='monospace')
            else:
                fig.suptitle(f'{root}_*_nrs2_{source_id}_s2d.fits', fontname='monospace')

            data = np.array([])
            for i in range(Nnods):
                datai = fits.getdata(files[i], ext=1)
                data = np.append(data, datai[np.isfinite(datai)].flatten())
            norm = ImageNormalize(data, interval=ZScaleInterval())

            for i in range(Nnods):
                data = fits.getdata(files[i], ext=1)

                ax[i,0].imshow(data, norm=norm, origin='lower', aspect='auto', interpolation='nearest')   

                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmean(data, axis=1)
                ax[i,1].step(prof, np.arange(np.shape(data)[0])-0.5, where='pre', linewidth=1, color='k')

                ax[i,1].tick_params(labelleft=False)

                ax[i,1].set_ylim(*ax[i,0].get_ylim())
                
                ax[i,1].set_ylabel(files[i].split('_')[-4], fontname='monospace')
                ax[i,1].yaxis.set_label_position("right")

            for i in range(Nnods-1):
                ax[i,0].tick_params(labelbottom=False)
                ax[i,1].tick_params(labelbottom=False)
            
            xmins, xmaxs = [], []
            for i in range(Nnods):
                xmini, xmaxi = ax[i,1].get_xlim()
                xmins.append(xmini)
                xmaxs.append(xmaxi)
            
            for i in range(Nnods):
                ax[i,1].set_xlim(min(xmins), max(xmaxs))

            plt.savefig(os.path.join(workspace_dir, f'{root}_{source_id}_nods.pdf'), dpi=300)
            plt.close()


def pad_to_common_detector_region(models):
    """
    Returns padded models and padding info needed to un-pad later
    """
    # Find the common bounding box in detector coordinates
    min_y = min(model.slits[0].ystart for model in models)
    min_x = min(model.slits[0].xstart for model in models)
    
    max_y = max(model.slits[0].ystart + model.slits[0].ysize for model in models)
    max_x = max(model.slits[0].xstart + model.slits[0].xsize for model in models)
    
    common_shape = (max_y - min_y, max_x - min_x)
    
    # Pad each cutout to the common region
    new_models = []
    padding_info = []
    
    for model in models:
        data = model.slits[0].data
        err = model.slits[0].err
        dq = model.slits[0].dq
        start_y, start_x = model.slits[0].ystart, model.slits[0].xstart
        
        # Calculate padding needed on each side
        pad_top = start_y - min_y
        pad_bottom = max_y - (start_y + data.shape[0])
        pad_left = start_x - min_x
        pad_right = max_x - (start_x + data.shape[1])
        
        pad_width = ((pad_top, pad_bottom), (pad_left, pad_right))
        
        padded_data = np.pad(data, pad_width, 
                            mode='constant', constant_values=0)
        padded_err = np.pad(err, pad_width,
                           mode='constant', constant_values=0)
        padded_dq = np.pad(dq, pad_width,
                          mode='constant', constant_values=0)
        
        new_model = model.copy()
        new_model.slits[0].data = padded_data
        new_model.slits[0].err = padded_err
        new_model.slits[0].dq = padded_dq
        new_models.append(new_model)
        
        # Store padding info for later un-padding
        padding_info.append({
            'pad_top': pad_top,
            'pad_bottom': pad_bottom,
            'pad_left': pad_left,
            'pad_right': pad_right
        })
    
    return new_models, padding_info


def unpad_model(model, padding_info):
    """
    Remove padding from a model using stored padding info
    """
    data = model.slits[0].data
    err = model.slits[0].err
    dq = model.slits[0].dq
    pad = padding_info
    
    # Slice out the original region
    unpadded_data = data[
        pad['pad_top'] : data.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : data.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_err = err[
        pad['pad_top'] : err.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : err.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_dq = dq[
        pad['pad_top'] : dq.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : dq.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    
    new_model = model.copy()
    new_model.slits[0].data = unpadded_data
    new_model.slits[0].err = unpadded_err
    new_model.slits[0].dq = unpadded_dq
    return new_model

def run_stage2b_single_slitlet(
        cal_files: List, 
        workspace_dir: str, 
        bkg_overrides,
        rectify: bool = False, # whether to produce rectified (i.e., s2d) products as well
    ):
    # this function shouldn't be doing any file discovery! 

    # Handle directory changes
    prev_cwd = os.getcwd()
    
    os.chdir(workspace_dir)

    try:
        # from jwst.assign_wcs import AssignWcsStep
        from jwst.pathloss import PathLossStep
        from jwst.background import BackgroundStep
        from jwst.datamodels import MultiSlitModel, ImageModel
        # assign_wcs = AssignWcsStep()
        bkg_subtract = BackgroundStep()
        pathloss = PathLossStep()

        if rectify: 
            from jwst.pixel_replace import PixelReplaceStep
            from jwst.resample import ResampleSpecStep
            pixel_replace = PixelReplaceStep()
            resample_spec = ResampleSpecStep() # do these need args? 

        if len(cal_files)==1:
            raise RuntimeError("Single exposure only, no background subtraction to be done!")

        elif len(cal_files) in [2, 3, 5]:
            # Load cal files as MultiSlitModels, but undo any pathloss corrections
            models = []
            do_pathloss = []
            for cal_file in cal_files:
                model = MultiSlitModel(cal_file)
                if 'COMPLETE' in model.meta.cal_step.pathloss.upper():
                    log(f'Inverting the pathloss for {os.path.basename(cal_file)}')
                    inverted = pathloss.call(model, inverse=True)
                    do_pathloss.append(True)
                else:
                    inverted = model
                    do_pathloss.append(False)
                    
                models.append(inverted)

            shapes = [model[0].data.shape for model in models]
            # print(shapes)
            padded = False
            if not len(list(set(shapes))) == 1:
                models, padding_info = pad_to_common_detector_region(models)
                padded = True
                # raise ValueError('Inconsistent shapes!')
            
            # shapes = [model[0].data.shape for model in models]
            # print(shapes)
            # assert 1==2
                        
            for i in range(len(cal_files)):
                science = models[i][0]


                bkg = [mod[0] for j,mod in enumerate(models) if j != i]
                
                if bkg_overrides is not None:
                    nods = [int(os.path.basename(cal_file).split('_')[2]) for cal_file in cal_files]
                    if str(nods[i]) in bkg_overrides:
                        nods_to_use = bkg_overrides[str(nods[i])]
                        print(nods_to_use)
                        log(f'{os.path.basename(cal_files[i])}: Only using nods {nods_to_use} for bkg subtraction for nod {nods[i]}')
                        bkg = [b[0] for n,b in zip(nods,models) if n in nods_to_use]
                
                bkgsub = bkg_subtract.call(science, bkg)

                    
                result = models[i].copy()
                # # janky, but this is the best way I could find to ensure that all metadata gets retained through file I/O
                result.slits[0].data = bkgsub.data
                result.slits[0].err = bkgsub.err
                result.slits[0].dq = bkgsub.dq
                result.slits[0].var_rnoise = bkgsub.var_rnoise
                result.slits[0].var_poisson = bkgsub.var_poisson
                result.meta.cal_step.bkg_subtract = 'COMPLETE'
                result[0].meta.cal_step.bkg_subtract = 'COMPLETE'

                im = ImageModel(cal_files[i])
                result[0].meta.pointing = im.meta.pointing

                if padded:
                    result = unpad_model(result, padding_info[i])

                # apply the pathloss correction again
                if do_pathloss[i]:
                    result = pathloss.call(result)



                cal_file_out = cal_files[i].replace('_cal.fits', '_cal_bkgsub.fits')
                result.save(cal_file_out)

                if rectify: 
                    # Call pixel replace, followed by resample_spec for 2D slit data
                    resampled = result.copy()
                    resampled = pixel_replace.call(resampled)
                    resampled = resample_spec.call(resampled)
                    s2d_file_out = cal_file_out.replace('_cal_bkgsub.fits', '_s2d_bkgsub.fits')
                    resampled.save(s2d_file_out)
                    resampled.close()
                
                result.close()

        else:
            raise Exception("not sure how to handle this # of exposures!")

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)        



def run_stage3_single_source(
        cal_files: List, 
        workspace_dir: str, 
        product_name: str, 
        cleanup_asn: bool = True,
        cleanup_crfs: bool = True,
    ):
    from jwst.pipeline import Spec3Pipeline
    

    # Handle directory changes
    prev_cwd = os.getcwd()
    
    os.chdir(workspace_dir)

    try:
        association= [(file, 'science') for file in cal_files]
        
        asn = associations.asn_from_list.asn_from_list(
            association,
            with_exptype=True,
            product_name=product_name
        )
        suggested_name, serialization = asn.dump()

        asn_file = f'{product_name}_spec3.json'
        with open(asn_file, 'w') as asn_file_out: 
            asn_file_out.write(serialization)


        Spec3Pipeline.call(asn_file,
            save_results=True,
            # steps={'extract_1d':{'override_extract1d':('jwst_nirspec_extract1d_4px.json')}}
        )

        source_id = int(product_name.split('_')[-1])
        if os.path.exists(f'{product_name}_s{source_id:09d}_s2d.fits'):
            os.rename(f'{product_name}_s{source_id:09d}_s2d.fits', f'{product_name}_s2d.fits')
        if os.path.exists(f'{product_name}_s{source_id:09d}_x1d.fits'):
            os.rename(f'{product_name}_s{source_id:09d}_x1d.fits', f'{product_name}_x1d.fits')

        
        # Create "exposures" table 
        hdrs0 = [fits.getheader(cal_file,ext=0) for cal_file in cal_files]
        exposures = Table()
        exposures['filename'] = cal_files
        exposures['dither_type'] = [hdr['PATTTYPE'] for hdr in hdrs0]
        exposures['nod_type'] = [hdr['NOD_TYPE'] for hdr in hdrs0]
        exposures['nod_number'] = [hdr['PRIDTPTS'] for hdr in hdrs0]
        exposures['dither_number'] = [hdr['PATT_NUM'] for hdr in hdrs0]
        exposures['exptime'] = [hdr['EFFEXPTM'] for hdr in hdrs0]
        exposures['stuck_shutter_list'] = [hdr['STKSHTRS'] if 'STKSHTRS' in hdr else 'N/A' for hdr in hdrs0]
        hdrs1 = [fits.getheader(cal_file,ext=1) for cal_file in cal_files]
        exposures['shutter_state'] = [hdr['SHUTSTA'] for hdr in hdrs1]
        exposures['source_ra'] = [hdr['SRCRA'] for hdr in hdrs1]
        exposures['source_dec'] = [hdr['SRCDEC'] for hdr in hdrs1]
        exposures['source_xpos'] = [hdr['SRCXPOS'] for hdr in hdrs1]
        exposures['source_ypos'] = [hdr['SRCYPOS'] for hdr in hdrs1]
        exposures['v3pa'] = [hdr['PA_V3'] for hdr in hdrs1]


        s2d_file = f'{product_name}_s2d.fits'
        with fits.open(s2d_file, mode='update') as hdul:
            exposures = table_to_hdu(exposures)
            exposures.name = 'EXPOSURES'
            hdul.append(exposures)

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        
        if cleanup_asn:
            # Cleanup any remaining ASN files
            asn_file = f'{product_name}_spec3.json'
            if os.path.exists(asn_file):
                os.remove(asn_file)
        
        if cleanup_crfs:
            for cal_file in cal_files:
                crf_file = cal_file.replace('.fits', '_a3001_crf.fits') # e.g. jw07076019001_03101_00003_nrs1_10059_cal_bkgsub_a3001_crf
                if os.path.exists(crf_file):
                    os.remove(crf_file)
            
            more_crf_files = glob.glob(f'{product_name}_*_crf.fits') + glob.glob(f'{product_name}_*_cal.fits')
            if len(more_crf_files) > 0:
                for file in more_crf_files:
                    os.remove(file)

        os.chdir(prev_cwd)        
def boxcar_profile(start, end, n_pixels):
    """
    Generate a boxcar extraction profile with fractional pixel weights.
    
    Parameters:
    -----------
    start : float
        Starting position (can be fractional)
    end : float
        Ending position (can be fractional)
    n_pixels : int
        Total number of pixels in the profile
    
    Returns:
    --------
    profile : ndarray
        1D array of weights for each pixel
    """
    profile = np.zeros(n_pixels)
    
    # Clip start and end to valid range [0, n_pixels]
    start = np.clip(start, 0, n_pixels)
    end = np.clip(end, 0, n_pixels)
    
    # Handle edge case where start >= end after clipping
    if start >= end:
        return profile
    
    # Get integer bounds
    start_int = int(np.floor(start))
    end_int = int(np.floor(end))
    
    # Clip integer bounds to valid indices
    start_int = np.clip(start_int, 0, n_pixels - 1)
    end_int = np.clip(end_int, 0, n_pixels - 1)
    
    # Calculate fractional contributions
    start_frac = 1.0 - (start - np.floor(start))  # fraction of first pixel
    end_frac = end - np.floor(end)  # fraction of last pixel
    
    # Fill in the profile
    if start_int == end_int:
        # Entire extraction is within a single pixel
        profile[start_int] = end - start
    else:
        # Multiple pixels involved
        profile[start_int] = start_frac
        if start_int + 1 <= end_int - 1:
            profile[start_int+1:end_int] = 1.0  # fully included pixels
        if end_frac > 0:  # Only add end contribution if there's a fractional part
            profile[end_int] = end_frac
    
    return profile


def optext_profile(collapsed, start, end):

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        
        x = np.arange(len(collapsed)+1)
        profile = np.zeros_like(collapsed)
        profile[(x[:-1] > start)&(x[1:] <= end)] = collapsed[(x[:-1] > start)&(x[1:] <= end)]
        profile[profile < 0] = 0
        profile /= np.nansum(profile)

    return profile 


def extract_with_profile(profile, data, error, mask=None, ivw=False):
    variance = error**2
    variance[np.isnan(data)] = np.nan

    if np.ndim(profile)==1:
        profile = profile[:,np.newaxis]

    if mask is not None:
        data[mask] = np.nan
        variance[mask] = np.nan

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        if ivw:
            fnu = np.nansum(profile*data/variance,axis=0)/np.nansum(profile**2/variance, axis=0)
            fnu_err = np.sqrt(np.nansum(profile, axis=0)/np.nansum(profile**2/variance,axis=0))
        else:
            fnu = np.nansum(profile*data, axis=0)
            fnu_err = np.sqrt(np.nansum(profile*variance, axis=0))

    return fnu, fnu_err


def opt_ext_single_source(
      product_name, 
      workspace_dir,
      optimal_extraction_profile = 'single', # or 'wavelength-dependent'  
      plot_profiles = False,
      plot_optext = False,
      overwrite = False,
      version = 'v0.1',
    ):
    """
    Runs an optimal extraction routine and saves an msaexp-style "_spec.fits" file (combining 2D and 1D) for a single object.
    """
    log(f"Running optimal extraction and compiling *_spec.fits file for {product_name}")

    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    s2d = fits.open(s2d_file)
    x1d = fits.open(x1d_file)
    s2d_sci = s2d['SCI'].data * 1e12 # convert from MJy to uJy
    s2d_err = s2d['ERR'].data * 1e12 # convert from MJy to uJy
    s2d_mask = s2d['WHT'].data==0

    out_filename = s2d_file.replace('_s2d.fits','_spec.fits')

    ph = fits.Header()
    ph['EXTEND'] = 'T'
    ph['FILENAME'] = (os.path.basename(out_filename), 'Name of the file')
    for keyword in ['ORIGIN','TIMESYS','TIMEUNIT','TELESCOP','PROGRAM','PI_NAME','CATEGORY','DATE-OBS',
                    'TIME-OBS','VISIT_ID','OBSERVTN','TARGPROP','TARGNAME','FILTER','GRATING','MSAMETFL',
                    'MSAMETID','MSACONID','EXP_TYPE','READPATT','EFFEXPTM','CAL_VER','CAL_VCS','CRDS_VER',
                    'CRDS_CTX','R_AREA','R_CAMERA','R_COLLIM','R_DARK','R_DISPER','R_DISTOR','R_EXTR1D',
                    'R_FILOFF','R_FLAT','R_DFLAT','R_FFLAT','R_SFLAT','R_FORE','R_FPA','R_GAIN','R_IFUFOR',
                    'R_IFUPOS','R_IFUSLI','R_LINEAR','R_MASK','R_MSA','R_OTE','R_PTHLOS','R_PHOTOM','R_READNO',
                    'R_REFPIX','R_REGION','R_SATURA','R_SPCWCS','R_SUPERB','R_WAVCOR','R_WAVRAN','S_WCS',
                    'S_BKDSUB','S_BPXSLF','S_BARSHA','S_CHGMIG','S_CLNFNS','S_DARK','S_DQINIT','S_EXTR1D',
                    'S_EXTR2D','S_FLAT','S_GANSCL','S_GRPSCL','S_IMPRNT','S_IPC','S_JUMP','S_LINEAR',
                    'S_MSBSUB','S_MSAFLG','S_NSCLEN','S_OUTLIR','S_PTHLOS','S_PHOTOM','S_PXREPL','S_RAMP',
                    'S_REFPIX','S_RESAMP','S_SATURA','S_SRCTYP','S_SUPERB','S_WAVCOR','NDRIZ','RESWHT',
                    'PIXFRAC','PXSCLRT']:
        try:
            ph[keyword] = (x1d['PRIMARY'].header[keyword], x1d['PRIMARY'].header.comments[keyword])
        except KeyError:
            log(f'Keyword {keyword} not found')
            continue
        
    ph['CMPFRTIM'] = (str(datetime.now()), 'Date/time of CAMPFIRE reduction')
    ph['CMPFRVER'] = (version, 'Version of CAMPFIRE reduction')

    primary = fits.PrimaryHDU(header=ph)

    # Optimal extraction
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR']-1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    cen = (x1d_start+x1d_stop)/2
    profile_opt = optext_profile(collapsed, x1d_start, x1d_stop)
    if all(np.isnan(profile_opt)):
        # fallback to 3px boxcar
        profile_opt = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))

    fnu_opt, fnu_opt_err = extract_with_profile(profile_opt, s2d_sci, s2d_err, mask=s2d_mask, ivw=True)

    # 3 pixel boxcar, centered on the expected position of the source
    profile_3px = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))
    fnu_3px, fnu_3px_err = extract_with_profile(profile_3px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 4 pixel boxcar, centered on the expected position of the source
    profile_4px = boxcar_profile(cen-2.0, cen+2.0, len(collapsed))
    fnu_4px, fnu_4px_err = extract_with_profile(profile_4px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 5 pixel boxcar, centered on the expected position of the source
    profile_5px = boxcar_profile(cen-2.5, cen+2.5, len(collapsed))
    fnu_5px, fnu_5px_err = extract_with_profile(profile_5px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    if plot_profiles:
        fig, axes = plt.subplots(1,4,figsize=(10,2))
        for ax, prof, label in zip(axes, [profile_opt, profile_3px, profile_4px, profile_5px], ['Optimal', '3px boxcar', '4px boxcar', '5px boxcar']):
            ax.stairs(collapsed/np.nanmax(collapsed), np.arange(len(collapsed)+1), color='k', zorder=1000)
            ax.set_ylim(*ax.get_ylim())
            ax.stairs(prof/np.nanmax(prof), np.arange(len(collapsed)+1), color='tab:red')
            ax.stairs(prof/np.nanmax(prof), np.arange(len(collapsed)+1), color='tab:red', fill=True, alpha=0.2)
            ax.axhline(0, color='0.3', linewidth=0.5, linestyle='--')
            ax.axvline(x1d_start, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(x1d_stop, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(cen, linewidth=0.5, color='b', linestyle=':')    
            ax.set_title(label)

        plt.savefig(out_filename.replace('_spec.fits','_prof.pdf'))
        plt.close()
    

    wave = x1d['EXTRACT1D'].data['WAVELENGTH']
    
    def fnu_to_flam(fnu, wave): # Assumed fnu in uJy and wave in um
        return fnu / wave**2 * 2.99792458e-19

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=wave, description='Wavelength', unit='um'))
    spec1d.add_column(Column(name='fnu', data=fnu_opt, description='Optimally extracted flux, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='fnu_err', data=fnu_opt_err, description='Optimally extracted flux error, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='flam', data=fnu_to_flam(fnu_opt, wave=wave), description='Optimally extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
    spec1d.add_column(Column(name='flam_err', data=fnu_to_flam(fnu_opt_err, wave=wave), description='Optimally extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))
    
    for size, fnu, fnu_err in zip(['3px','4px','5px'],[fnu_3px,fnu_4px,fnu_5px],[fnu_3px_err,fnu_4px_err,fnu_5px_err]):
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu, description=f'{size} boxcar extracted flux, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_err, description=f'{size} boxcar extracted flux error, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=fnu_to_flam(fnu, wave=wave), description=f'{size} boxcar extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=fnu_to_flam(fnu_err, wave=wave), description=f'{size} boxcar extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))

    # indices = np.where(fnu_3px_err!=0)[0]
    # spec1d = spec1d[indices[0]:indices[-1] + 1]

    spec1d = table_to_hdu(spec1d)
    spec1d.name = 'SPEC1D'

    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed))+0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    prof1d = table_to_hdu(prof1d)
    prof1d.name = 'PROF1D'

    s2dh = fits.Header()
    for keyword in ['EXTNAME', 'SRCTYPE','XPOSURE','PHOTMJSR','PHOTUJA2','PIXAR_SR','PIXAR_A2','DISPAXIS',
                    'SPORDER','SOURCEID','STLARITY','SRCRA','SRCDEC','WAVECOR','PTHLOSS']:
        if keyword in s2d['SCI'].header:
            s2dh[keyword] = (s2d['SCI'].header[keyword], s2d['SCI'].header.comments[keyword])

    sci = fits.ImageHDU(data=s2d['SCI'].data, header=s2dh, name='SCI')
    err = fits.ImageHDU(data=s2d['ERR'].data, header=s2dh, name='ERR')
    wav = fits.ImageHDU(data=s2d['WAVELENGTH'].data, header=s2dh, name='WAVELENGTH')
    wht = fits.ImageHDU(data=s2d['WHT'].data, header=s2dh, name='WHT')
    # prof = Table({''})
    exp = s2d['EXPOSURES']

    
    spec = fits.HDUList(hdus=[primary, spec1d, sci, err, wav, wht, prof1d, exp])
    spec.writeto(out_filename, overwrite=True)


    # plt.step(x1d[1].data['WAVELENGTH'], fnu*1.2, where='mid', label='NEW')
    # plt.step(msaexp['wave'], msaexp['flux'], label='msaexp')
    # # plt.step(x1d[1].data['WAVELENGTH'], x1d[1].data['FLUX']*1e6*1.3, where='mid', label='pipeline default')
    # plt.loglog()
    # plt.show()

    if plot_optext:
        import matplotlib as mpl
        from astropy.stats import sigma_clipped_stats
        from astropy.utils.exceptions import AstropyWarning


        # Extract data
        wave = spec1d.data['wave']
        fnu = spec1d.data['fnu']
        fnu_err = spec1d.data['fnu_err']
        flam = spec1d.data['flam']
        flam_err = spec1d.data['flam_err']
        
        valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)
        
        # Create figure with 3 rows: 2D spectrum, f_nu, f_lambda
        fig = plt.figure(figsize=(8,6), constrained_layout=True, dpi=300)
        gs = mpl.gridspec.GridSpec(nrows=3, ncols=2, width_ratios=[9,1], 
                                  height_ratios=[1,2.5,2.5], figure=fig)

        ax_2d = plt.subplot(gs[0,0])
        ax_1d_fnu = plt.subplot(gs[1,0])
        ax_1d_flam = plt.subplot(gs[2,0])
        ax_prof = plt.subplot(gs[0,1])

        # 2D spectrum with S/N calculation
        sci = s2d['SCI'].data
        err = s2d['ERR'].data
        
        nsci = sci/err 
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=AstropyWarning)
            std = sigma_clipped_stats(nsci, sigma=3)[2]
        snr_2d = nsci / std

        # S/N range and colormap
        vmin, vmax = -3, 8
        cmap = plt.colormaps['viridis']
        cmap.set_bad('0.8')

        im = ax_2d.pcolormesh(wave, prof1d.data['ypos']-cen, snr_2d, 
                             vmin=vmin, vmax=vmax, cmap=cmap, rasterized=True)
        ax_2d.set_ylabel('$y$ [pix]')
        ax_2d.set_ylim(-10, 10)
        ax_2d.minorticks_on()
        ax_2d.tick_params(direction='in', which='both', axis='y')
        
        # Dual 1D spectrum plots
        valid = np.isfinite(fnu) & np.isfinite(fnu_err)
        
        # f_ν plot
        ax_1d_fnu.step(wave, fnu, where='mid', color='k', linewidth=1)
        ax_1d_fnu.fill_between(wave, (fnu - fnu_err), (fnu + fnu_err), 
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
        ax_1d_fnu.set_ylabel(r'$f_{\nu}$ [μJy]')
        
        # f_λ plot
        ax_1d_flam.step(wave, flam, where='mid', color='k', linewidth=1)
        ax_1d_flam.fill_between(wave, (flam - flam_err), (flam + flam_err), 
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
        ax_1d_flam.set_ylabel(r'$f_{\lambda}$ [erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$]')
        ax_1d_flam.set_xlabel('Observed Wavelength [μm]')
        
        # Advanced grid and tick styling
        ax_1d_fnu.grid(True, alpha=0.2, linewidth=1, zorder=-1000)
        ax_1d_flam.grid(True, alpha=0.2, linewidth=1, zorder=-1000)
        ax_1d_fnu.minorticks_on()
        ax_1d_flam.minorticks_on()
        ax_1d_fnu.tick_params(direction='in', which='both')
        ax_1d_flam.tick_params(direction='in', which='both')

        # Spatial profile plot
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            p = np.nanmedian(sci, axis=1)
            p /= np.nanmax(p[prof1d.data['opt']!=0])
        ax_prof.step(p, prof1d.data['ypos']-cen, where='post', color='k')
        ax_prof.fill_betweenx(prof1d.data['ypos']-cen, np.zeros_like(prof1d.data['ypos']), prof1d.data['opt']/np.nanmax(prof1d.data['opt']), 
                             facecolor='r', alpha=0.3, edgecolor='none', step='pre')
        ax_prof.axvline(0, color='k', linewidth=1, zorder=-1000, alpha=0.2)
        ax_prof.minorticks_on()
        ax_prof.set_xlim(-0.3, 1.2)
        ax_prof.set_ylim(-10, 10)
        ax_prof.tick_params(labelbottom=False, bottom=False, labelleft=False,
                           direction='in', which='both')

        # Smart x and y limits
        xmin = wave.min()
        xmax = wave.max()
        ax_2d.set_xlim(xmin, xmax)
        ax_1d_fnu.set_xlim(xmin, xmax)
        ax_1d_flam.set_xlim(xmin, xmax)

        # Percentile-based y-limits
        ymax = np.nanpercentile(fnu+fnu_err, 97)*1.2
        ax_1d_fnu.set_ylim(-0.1*ymax, ymax)
        ymax = np.nanpercentile(flam+flam_err, 97)*1.2
        ax_1d_flam.set_ylim(-0.1*ymax, ymax)
        
        fig.suptitle(product_name+'_spec', fontname='monospace')

        plt.savefig(out_filename.replace('_spec.fits','_spec.pdf'))
        plt.close()

    s2d.close()
    x1d.close()






class ReductionEngine:
    """
    Core data reduction engine.
    
    This class contains the data reduction functions.
    """
    
    def __init__(self, config_path="config.toml"):
        """
        Initialize data reduction engine.
        
        Parameters:
        -----------
        config_path : str
            Path to configuration TOML file
        """

        # Load configuration
        self.config = load_config(config_path)
        self.config_path = config_path  # Store config path for copying later

        # Set up environment variables (especially CRDS settings)
        self.setup_environment()
        
        # Set up directories from config - make them absolute paths for multiprocessing
        paths = self.config.get('paths', {})
        self.data_dir = paths.get('data_dir')
        self.products_dir = paths.get('products_dir')
        self.pictureframe_dir = paths.get('pictureframe_dir')
        
        # Get version from config and substitute in paths
        # pipeline_config = self.config.get('pipeline', {})
        # version = pipeline_config.get('version', 'unversioned')
        # self.version = version


        # Create base directories if they don't exist
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.products_dir, exist_ok=True)
        os.makedirs(self.pictureframe_dir, exist_ok=True)
        
        log("Initialized ReductionEngine")
        
    
    
    def setup_environment(self):
        """Set environment variables from config file."""
        if 'environment' in self.config:
            env = self.config['environment']
            for key, value in env.items():
                os.environ[key] = str(value)
                log(f"Set environment variable {key} = {value}")
    
    def run_stage1(self, 
            obs: Observation, 
            n_processes: int = 1,
            overwrite: bool = False,
        ):

        config = self.config.get('stage1', DEFAULT_STAGE1_CONFIG)

        # Create workspace and copy over uncal files
        if not obs.directories_setup:
            obs.setup_workspace_directory(self.data_dir, self.products_dir, overwrite=overwrite)

        obs.copy_uncal_files(overwrite=overwrite)
        
        uncal_files = obs.glob("_uncal.fits")
        if not overwrite:
            uncal_files = [f for f in uncal_files if not os.path.exists(f.replace('_uncal.fits','_rate.fits'))]

        uncal_files = [os.path.basename(f) for f in uncal_files]

        kwargs = dict(
            do_clean_flicker_noise = config['do_clean_flicker_noise'],
            mask_science_regions = config['mask_science_regions'],
            cleanup_uncal = config['cleanup_uncal'],
            cleanup_rateints = config['cleanup_rateints'],
        )


        if n_processes == 1: 
            
            log(f'Processing {len(uncal_files)} uncal files')
            for uncal_file in uncal_files:
                run_stage1_single_uncal(uncal_file, obs.workspace_dir, **kwargs)

                if config['subtract_background'] and config['do_clean_flicker_noise']:
                    rate_file = uncal_file.replace('_uncal.fits','_rate.fits')
                    subtract_background_from_rate_file(
                        os.path.join(obs.workspace_dir, rate_file), 
                        box_size = config.get('box_size',8), 
                        sigma_clip = config.get('sigma_clip',True),
                        bkg_estimator = config.get('bkg_estimator','median'),
                        plot = config.get('plot', True),
                        save_backup = False,
                        pictureframe_dir = self.pictureframe_dir,
                    )
            
        else:
            log(f'Multiprocessing {len(uncal_files)} uncal files across {n_processes} workers')
            sleep(1)
            from multiprocessing import Pool
            from functools import partial
            process_func = partial(
                run_stage1_single_uncal,
                workspace_dir=obs.workspace_dir,
                **kwargs
            )
            with Pool(processes=n_processes) as pool:
                pool.map(process_func, uncal_files)

            if config['subtract_background'] and config['do_clean_flicker_noise']:
                rate_files = [os.path.join(obs.workspace_dir, f.replace('_uncal.fits','_rate.fits')) for f in uncal_files]
                process_func = partial(
                    subtract_background_from_rate_file,
                    box_size = config.get('box_size',8), 
                    sigma_clip = config.get('sigma_clip',True),
                    bkg_estimator = config.get('bkg_estimator','median'),
                    plot = config.get('plot', True),
                    save_backup = False,
                    pictureframe_dir = self.pictureframe_dir,
                )
                with Pool(processes=n_processes) as pool:
                    pool.map(process_func, rate_files)


    def run_stage2a(
        self,
        obs: Observation, 
        source_ids = 'all', 
        overwrite: bool = False,
        n_processes: int = 1,
        plot: bool = True,
    ):    

        config = self.config.get('stage2', DEFAULT_STAGE2_CONFIG)
        
        kwargs = dict(
            set_stellarity = config.get('set_stellarity'),
            source_ids = source_ids, 
            overwrite = overwrite,
            # do_clean_flicker_noise = config['do_clean_flicker_noise'],
            # mask_science_regions = config['mask_science_regions'],
            # cleanup_uncal = config['cleanup_uncal'],
            # cleanup_rateints = config['cleanup_rateints'],
        )
        # Create workspace 
        # if not obs.directories_setup:
        obs.setup_workspace_directory(self.data_dir, self.products_dir, overwrite=False)
        
        if n_processes == 1:
            source_ids_processed = []
            for rate_file in obs.rate_files:
                source_ids_processed += run_stage2a_single_rate(rate_file, obs, **kwargs)


        else:
            from multiprocessing import Pool
            from functools import partial
  
            # Create partial function with fixed arguments
            process_func = partial(
                run_stage2a_single_rate,
                obs=obs,
                **kwargs
            )
            
            # Process files in parallel and collect results
            with Pool(processes=n_processes) as pool:
                results = pool.map(process_func, obs.rate_files)
            
            # Flatten the list of lists into a single list
            source_ids_processed = []
            for result in results:
                source_ids_processed += result


        source_ids_processed = list(set(source_ids_processed))

        all_cal_files = obs.glob("_cal.fits")
        if n_processes==1:
            for source_id in source_ids_processed:
                fix_units(source_id, obs.workspace_dir)

                cal_files = [f for f in all_cal_files if f'_{source_id}_' in f]
                for cal_file in cal_files:
                    resample_single_exposure(os.path.basename(cal_file), obs.workspace_dir)

                if plot: 
                    plot_stage2a_results(source_id, obs.workspace_dir)
        else:
            from multiprocessing import Pool
            from functools import partial

            # first, fix units for everything
            with Pool(processes=n_processes) as pool:
                pool.map(partial(fix_units, workspace_dir=obs.workspace_dir), source_ids_processed)

            # then, collect all cal files and make s2ds
            cal_files = [os.path.basename(f) for f in all_cal_files if int(f.split('_')[-2]) in source_ids_processed]
            with Pool(processes=n_processes) as pool:
                pool.map(partial(resample_single_exposure, workspace_dir=obs.workspace_dir), cal_files)

            # finally, make plots!
            with Pool(processes=n_processes) as pool:
                pool.map(partial(plot_stage2a_results, workspace_dir=obs.workspace_dir), source_ids_processed)
                
                    


    def run_stage2b(
        self,
        obs: Observation, 
        source_ids = 'all',
        overwrite: bool = False,
        n_processes=1,
    ):    

        # config = self.config.get('stage2', DEFAULT_STAGE2_CONFIG)
        config = self.config['stage2']
        
        kwargs = dict(
            rectify = config.get('rectify'),
        )

        # Create workspace 
        obs.setup_workspace_directory(self.data_dir, self.products_dir, overwrite=False)
        

        # Discover *_cal.fits files in the workspace directory
        files = Table()
        files['path'] = obs.glob(ext='_cal.fits')
        files['name'] = [os.path.basename(f['path']) for f in files]
        files['source_id'] = [int(f['name'].split('_')[-2]) for f in files]
        files['detector'] = ['nrs'+f['name'].split('nrs')[-1][0] for f in files]
        files['obs'] = [f['name'].split('_')[0] for f in files]
        files['filter']=[fits.getheader(f['path'])['FILTER'] for f in files]
        files['grating']=[fits.getheader(f['path'])['GRATING'] for f in files]
        files['filter_grating'] = [f['grating'].lower()+'_'+f['filter'].lower() for f in files]
        files['config'] = [f['name'].split('_')[1] for f in files]
        files['nod'] = [f['name'].split('_')[2] for f in files]

        # PATTTYPE = 'NONE' for most, PATTTYPE = '...' for UNCOVER
        # NOD_TYPE = '3-SHUTTER-SLITLET'

        # files.pprint()

        source_ids_to_process = np.unique(files['source_id'])
        if source_ids != 'all': 
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]

        # Preparation stage - build task list
        tasks = []
        for source_id in source_ids_to_process:
            files1 = files[files['source_id']==source_id]
            
            for detector in np.unique(files1['detector']):
                files2 = files1[files1['detector']==detector]

                for observation in np.unique(files2['obs']):
                    files3 = files2[files2['obs']==observation]

                    for config in np.unique(files3['config']):
                        files4 = files3[files3['config'] == config]

                        files4.pprint()
                    
                        if len(files4)==0: 
                            raise Exception("This shouldn't happen!")
                        elif len(files4)==2:
                            # open each file and ensure its 2 shutter slitlet
                            pass
                        elif len(files4)==3:
                            # open each file and ensure its 3 shutter slitlet
                            pass
                        elif len(files4)==5:
                            # open each file and ensure its 5 shutter slitlet
                            pass
                        else:
                            # some weird dither pattern! 
                            # e.g., for UNCOVER, there would be 6 files 
                            # we need to deduce from the header that this is a 3-shutter slitlet and 
                            # split those 6 files into two groups 1-3, 4-6
                            pass

                        # TODO come back to this logic! 
                        target_files = files4 # for now

                        # Check if we should skip
                        if kwargs['rectify']:
                            if all([os.path.exists(f['path'].replace('_cal.fits','_cal_bkgsub.fits')) for f in target_files]) and all([os.path.exists(f['path'].replace('_cal.fits','_s2d_bkgsub.fits')) for f in target_files]) and not overwrite:
                                log(f'ID{source_id}: Background-subtracted stage2 products already exist for {observation}_{config}_*_{detector}_{source_id}, skipping (overwrite=False)')
                                continue 
                        else:
                            if all([os.path.exists(f['path'].replace('_cal.fits','_cal_bkgsub.fits')) for f in target_files]) and not overwrite:
                                log(f'ID{source_id}: Background-subtracted stage2 products already exist for {observation}_{config}_*_{detector}_{source_id}, skipping (overwrite=False)')
                                continue 

                        root = '_'.join(target_files['name'][0].split('_')[:2])
                        bkg_overrides = obs.bkg_overrides.get(root)
                        if bkg_overrides is not None:
                            bkg_overrides = bkg_overrides.get(str(source_id))

                        log(f'ID{source_id}: Running stage2b (bkg-subtraction) for {observation}_{config}_*_{detector}_{source_id}')
                        
                        # Store arguments for run_stage2b_single_slitlet
                        tasks.append((target_files['name'], obs.workspace_dir, bkg_overrides))

        # Execution stage
        if n_processes > 1:
            from multiprocessing import Pool
            # Create partial function with kwargs fixed
            worker = partial(run_stage2b_single_slitlet, **kwargs)
            with Pool(n_processes) as pool:
                pool.starmap(worker, tasks)
        else:
            for target_names, workspace, bkg_overrides in tasks:
                run_stage2b_single_slitlet(target_names, workspace, bkg_overrides, **kwargs)

                
    def run_stage3(
        self,
        obs: Observation, 
        source_ids = 'all',
        n_processes=1,
        overwrite=False,
    ):    

        version = self.config.get('pipeline')['version']
        config = self.config.get('stage3', DEFAULT_STAGE3_CONFIG)
        
        bkg_subtraction_method = config.get('method', DEFAULT_STAGE3_CONFIG['method']) # nodded or local
        kwargs = dict(
            cleanup_asn = config.get('cleanup_asn', DEFAULT_STAGE3_CONFIG['cleanup_asn']),
            cleanup_crfs = config.get('cleanup_crfs', DEFAULT_STAGE3_CONFIG['cleanup_crfs']),

        )
        plot_profiles = config.get('plot_profiles', DEFAULT_STAGE3_CONFIG['plot_profiles'])
        plot_optext = config.get('plot_optext', DEFAULT_STAGE3_CONFIG['plot_optext'])

        # Create workspace 
        obs.setup_workspace_directory(self.data_dir, self.products_dir, overwrite=False)
        

        # Discover *_cal.fits files in the workspace directory
        files = Table()
        if bkg_subtraction_method == 'nodded':
            files['path'] = obs.glob(ext='_cal_bkgsub.fits')
            # files['path'] = sorted(glob.glob(os.path.join(obs.workspace_dir, "jw*_cal_bkgsub.fits")))
        else:
            raise NotImplementedError
        
        
        files['name'] = [os.path.basename(f['path']) for f in files]
        files['source_id'] = [int(f['name'].split('_')[-3]) for f in files]
        files['detector'] = ['nrs'+f['name'].split('nrs')[-1][0] for f in files]
        files['obs'] = [f['name'].split('_')[0] for f in files]
        files['filter']=[fits.getheader(f['path'])['FILTER'] for f in files]
        files['grating']=[fits.getheader(f['path'])['GRATING'] for f in files]
        files['filter_grating'] = [f['grating'].lower()+'_'+f['filter'].lower() for f in files]
        files['config'] = [f['name'].split('_')[1] for f in files]
        files['nod'] = [f['name'].split('_')[2] for f in files]
        files['srcflux'] = [fits.getheader(f['path'])['SRCFLUX']=='T' if 'SRCFLUX' in fits.getheader(f['path']) else True  for f in files ] # temporary patch!

        # files.pprint()

        source_ids_to_process = np.unique(files['source_id'])
        if source_ids != 'all': 
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]

        tasks = []
        for source_id in source_ids_to_process:
            files1 = files[files['source_id']==source_id]

            for filter_grating in np.unique(files1['filter_grating']):
                target_files = files1[files1['filter_grating']==filter_grating]
                target_files = target_files[target_files['srcflux']]

                target_files.pprint()

                if obs.combine_dither:
                    # if we're combining all dithers (or, if there is only one dither) proceed from here
                    product_name = f"{obs.name}_{filter_grating}_{source_id}"
                    if len(target_files)==0: 
                        continue
                        #raise Exception("This shouldn't happen!")
                    
                    # TODO handle overwrite?
                    if os.path.exists(obs.workspace_dir + product_name + "_spec.fits") and not overwrite:
                        continue

                    tasks.append((list(target_files['name']), obs.workspace_dir, product_name))
                    
                else:
                    # otherwise, we need more logic to understand the dither pattern
                    raise NotImplementedError

        # Execution stage
        if n_processes > 1:
            from multiprocessing import Pool
            # Create partial function with kwargs fixed
            worker = partial(run_stage3_single_source, **kwargs)
            with Pool(n_processes) as pool:
                pool.starmap(worker, tasks)
    
            tasks2 = [(task[2], task[1]) for task in tasks]
            worker = partial(opt_ext_single_source, plot_profiles=plot_profiles, plot_optext=plot_optext, version=version)
            with Pool(n_processes) as pool:
                pool.starmap(worker, tasks2)
        else:
            for target_file_names, workspace_dir, product_name in tasks:
                run_stage3_single_source(target_file_names, workspace_dir, product_name, **kwargs)
                opt_ext_single_source(product_name, workspace_dir, plot_profiles=plot_profiles, plot_optext=plot_optext, version=version)


    

def main():
    """Main function to run NIRSpec data reduction."""
    # Parse arguments first to check for threading control
    parser = argparse.ArgumentParser(description='NIRSpec Data Reduction Pipeline')
    parser.add_argument('--obs', type=str, required=True, 
                        help='Observation name from observations.toml')
    parser.add_argument('--config', type=str, default='config.toml',
                        help='Path to configuration file (default: config.toml)')
    parser.add_argument('--observations', type=str, default='observations.toml',
                        help='Path to observations file (default: observations.toml)')
    parser.add_argument('--stage1', action='store_true',
                        help='Run stage 1 processing (Detector1Pipeline)')
    parser.add_argument('--stage2a', action='store_true',
                        help='Run stage 2a processing (Spec2Pipeline, no bkg subtraction)')
    parser.add_argument('--stage2b', action='store_true',
                        help='Run stage 2b processing (Spec2Pipeline, with bkg subtraction)')
    parser.add_argument('--stage3', action='store_true',
                        help='Run stage 3 processing (Spec3Pipeline)')
    parser.add_argument('--source-ids', nargs='+', type=int,
                        help='Individual source IDs to restrict processing to')
    parser.add_argument('--processes', type=int, default=1,
                        help='Number of processes for multiprocessing (default: 1 for sequential)')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing products')
    
    args = parser.parse_args()
    if args.source_ids is None:
        args.source_ids = 'all'


    # # If neither preprocess nor extract specified, do nothing (for testing convenience)
    # if not args.stage1 and not args.stage2 and not args.stage3:
    #     log(f"No steps specified for observation: {args.obs}")
    #     log("Use --stage1 to run Detector1Pipeline")
    #     log("Use --stage2 to run Spec2Pipeline")
    #     log("Use --stage3 to run Spec3Pipeline")
    #     log("Use multiple flags to run complete pipelines")
    #     return
    
    # Handle multiprocessing arguments
    if args.processes > 1:
        log(f"Using {args.processes} processes for multiprocessing")
    
    # Initialize reduction engine
    engine = ReductionEngine(args.config)


    obs = Observation.load(args.obs)

    obs.setup_workspace_directory(engine.data_dir, engine.products_dir, overwrite=False)

    if args.stage1:
        log(f"Running stage1 for observation {obs.name}")
        engine.run_stage1(obs, n_processes=args.processes, overwrite=args.overwrite)

    if args.stage2a:
        log(f"Running stage2a for observation {obs.name}")
        engine.run_stage2a(obs, source_ids=args.source_ids, n_processes=args.processes, overwrite=args.overwrite)
    
    if args.stage2b:
        log(f"Running stage2b for observation {obs.name}")
        engine.run_stage2b(obs, source_ids=args.source_ids, n_processes=args.processes, overwrite=args.overwrite)
    
    if args.stage3:
        log(f"Running stage3 for observation {obs.name}")
        engine.run_stage3(obs, source_ids=args.source_ids, n_processes=args.processes, overwrite=args.overwrite)


    
    # try:
    #     if args.preprocess:
    #         log(f"Starting preprocessing for observation: {args.obs}")
    #         engine.preprocess_observation(
    #             observation_config, 
    #             overwrite=args.overwrite, 
    #             n_processes=n_processes,
    #             allow_threading=args.allow_threading,
    #         )
    #         log(f"Completed preprocessing for observation: {args.obs}")
    #     else:
    #         log(f"Skipping preprocessing for observation: {args.obs}")
    #         log("Use --preprocess to run preprocessing step")

        
    #     if args.extract:
    #         log(f"Starting extraction for observation: {args.obs}")
    #         engine.extract_spectra_for_observation(
    #             observation_config, 
    #             overwrite=args.overwrite, 
    #             n_processes=n_processes, 
    #             allow_threading=args.allow_threading
    #         )
    #         log(f"Completed extraction for observation: {args.obs}")
    #     else:
    #         log(f"Skipping extraction for observation: {args.obs}")
    #         log("Use --extract to run extraction step") 
            
    # except Exception as e:
    #     log(f"Error processing observation {args.obs}: {e}")
    #     raise


if __name__ == '__main__':
    main()

