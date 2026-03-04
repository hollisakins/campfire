"""
Generate multi-wavelength SED inspection plots.

Extracted from scripts/cosmos_inspec.py for integration into the deploy CLI.
Currently only supports the COSMOS field.
"""

import glob
import os
import socket
import warnings
from pathlib import Path

import astropy.units as u
import matplotlib as mpl
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.cosmology import Planck18 as cosmo
from astropy.io import fits
from astropy.nddata.utils import Cutout2D, NoOverlapError
from astropy.stats import sigma_clipped_stats
from astropy.table import Table
from astropy.wcs import WCS

from campfire_deploy.generate_rgb import gen_rgb_image, get_source_pos

warnings.simplefilter('ignore')


# ---------------------------------------------------------------------------
# Inline replacement for htools.plotting.set_style('sans')
# ---------------------------------------------------------------------------

_SANS_STYLE = {
    'font.family': 'sans-serif',
    'font.size': 10.0,
    'text.usetex': False,
    'xtick.direction': 'in',
    'ytick.direction': 'in',
    'xtick.top': True,
    'ytick.right': True,
    'xtick.minor.visible': True,
    'ytick.minor.visible': True,
    'legend.frameon': False,
    'image.origin': 'lower',
    'lines.linewidth': 1,
    'errorbar.capsize': 0,
    'axes.linewidth': 0.8,
    'xtick.major.width': 0.8,
    'xtick.minor.width': 0.6,
    'xtick.major.size': 3.5,
    'xtick.minor.size': 2.0,
    'ytick.major.width': 0.8,
    'ytick.minor.width': 0.6,
    'ytick.major.size': 3.5,
    'ytick.minor.size': 2.0,
    'savefig.dpi': 300,
}


# ---------------------------------------------------------------------------
# Inline replacement for htools.utils.filters.Filters
# Pre-computed (wav, wav_min, wav_max) in microns for each filter.
# ---------------------------------------------------------------------------

FILTER_WAVELENGTHS = {
    'vis': (0.718086, 0.495885, 0.930629),
    'f435w': (0.433444, 0.359500, 0.488300),
    'f606w': (0.596043, 0.462700, 0.717900),
    'f814w': (0.807304, 0.686800, 0.962600),
    'f098m': (0.987520, 0.889000, 1.084297),
    'f090w': (0.904228, 0.788550, 1.023550),
    'f115w': (1.157002, 0.998200, 1.305200),
    'f140m': (1.406032, 1.304350, 1.505350),
    'f150w': (1.503988, 1.303790, 1.693790),
    'f182m': (1.846590, 1.695500, 2.000500),
    'f200w': (1.993392, 1.723400, 2.258400),
    'f210m': (2.096375, 1.961600, 2.232600),
    'f250m': (2.503802, 2.393530, 2.616900),
    'f277w': (2.769332, 2.365900, 3.216190),
    'f335m': (3.363887, 3.118640, 3.642920),
    'f356w': (3.576787, 3.070000, 4.078020),
    'f360m': (3.626058, 3.322680, 3.902360),
    'f410m': (4.084378, 3.775340, 4.402310),
    'f430m': (4.281818, 4.122610, 4.444200),
    'f444w': (4.415974, 3.802370, 5.099550),
    'f460m': (4.630470, 4.465820, 4.813090),
    'f480m': (4.819237, 4.582030, 5.088740),
    'f770w': (7.663456, 6.475000, 8.830000),
}


# ---------------------------------------------------------------------------
# Host-aware mosaic/catalog paths (COSMOS only)
# ---------------------------------------------------------------------------

def _resolve_hostname():
    hostname = socket.gethostname()
    match hostname:
        case 'ASTR-A65432':
            return 'patrick'
        case 'Holliss-MacBook-Pro.local':
            return 'gerald'
        case _:
            return 'candide'


def _get_cosmos_paths():
    """Return (IMAGE_DATA, CATALOG_FILE, DETECTION_IMAGE, SEGMENTATION_IMAGE) for the current host."""
    hostname = _resolve_hostname()

    if hostname == 'patrick':
        image_data = {
            'u': '/Users/hba423/simmons/mosaics/cosmos/cfht/cfht_u_{ext}_{tile}.fits',
            'g': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_g_{ext}_{tile}.fits',
            'r': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_r_{ext}_{tile}.fits',
            'i': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_i_{ext}_{tile}.fits',
            'z': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_z_{ext}_{tile}.fits',
            'y': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_y_{ext}_{tile}.fits',
            'Y': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_Y_{ext}_{tile}.fits',
            'J': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_J_{ext}_{tile}.fits',
            'H': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_H_{ext}_{tile}.fits',
            'Ks': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_Ks_{ext}_{tile}.fits',
            'f435w': '/V/maurice/mosaics/cosmos/f435w/mosaic_cosmos_all_hst_acs_wfc_f435w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f606w': '/V/maurice/mosaics/cosmos/f606w/mosaic_cosmos_all_hst_acs_wfc_f606w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f814w': '/V/maurice/mosaics/cosmos/f814w/mosaic_cosmos_web_30mas_tile_{tile}_hst_acs_wfc_f814w_{ext}.fits',
            'f098m': '/V/maurice/mosaics/cosmos/f098m/mosaic_cosmos_all_hst_wfc3_ir_f098m_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f090w': '/V/maurice/mosaics/cosmos/f090w/mosaic_nircam_f090w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f115w': '/V/maurice/mosaics/cosmos/f115w/mosaic_nircam_f115w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f140m': '/V/maurice/mosaics/cosmos/f140m/mosaic_nircam_f140m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f150w': '/V/maurice/mosaics/cosmos/f150w/mosaic_nircam_f150w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f182m': '/V/maurice/mosaics/cosmos/f182m/mosaic_nircam_f182m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f200w': '/V/maurice/mosaics/cosmos/f200w/mosaic_nircam_f200w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f210m': '/V/maurice/mosaics/cosmos/f210m/mosaic_nircam_f210m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f250m': '/V/maurice/mosaics/cosmos/f250m/mosaic_nircam_f250m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f277w': '/V/maurice/mosaics/cosmos/f277w/mosaic_nircam_f277w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f335m': '/V/maurice/mosaics/cosmos/f335m/mosaic_nircam_f335m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f356w': '/V/maurice/mosaics/cosmos/f356w/mosaic_nircam_f356w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f360m': '/V/maurice/mosaics/cosmos/f360m/mosaic_nircam_f360m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f410m': '/V/maurice/mosaics/cosmos/f410m/mosaic_nircam_f410m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f430m': '/V/maurice/mosaics/cosmos/f430m/mosaic_nircam_f430m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f444w': '/V/maurice/mosaics/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f460m': '/V/maurice/mosaics/cosmos/f460m/mosaic_nircam_f460m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f480m': '/V/maurice/mosaics/cosmos/f480m/mosaic_nircam_f480m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f770w': '/Users/hba423/simmons/cosmos/f770w/mosaic_miri_f770w_COSMOS-Web_30mas_{tile}_v1.0_{ext}.fits',
            'gri': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_gri_{ext}_{tile}.fits',
            'vis': '/V/maurice/mosaics/cosmos/vis/mosaic_euclid_vis_cosmos_30mas_v1_{tile}_{ext}.fits',
        }
        catalog_file = '/Users/hba423/simmons/cosmos/catalog_cosmos_v1.1_merged.fits'
        detection_image = '/research/COSMOS-3D/highz/catalog/cosmos_v1.0/detection_images/detection_image_ivw_{tile}.fits'
        segmentation_image = '/research/COSMOS-3D/highz/catalog/cosmos_v1.0/detection_images/segmap_ivw_hot+cold_{tile}.fits'

    elif hostname == 'gerald':
        image_data = {
            'u': '/Users/hba423/simmons/mosaics/cosmos/cfht/cfht_u_{ext}_{tile}.fits',
            'g': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_g_{ext}_{tile}.fits',
            'r': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_r_{ext}_{tile}.fits',
            'i': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_i_{ext}_{tile}.fits',
            'z': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_z_{ext}_{tile}.fits',
            'y': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_y_{ext}_{tile}.fits',
            'Y': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_Y_{ext}_{tile}.fits',
            'J': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_J_{ext}_{tile}.fits',
            'H': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_H_{ext}_{tile}.fits',
            'Ks': '/Users/hba423/simmons/mosaics/cosmos/uvista/uvista_Ks_{ext}_{tile}.fits',
            'f435w': '/V/maurice/mosaics/cosmos/f435w/mosaic_cosmos_all_hst_acs_wfc_f435w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f606w': '/V/maurice/mosaics/cosmos/f606w/mosaic_cosmos_all_hst_acs_wfc_f606w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f814w': '/V/maurice/mosaics/cosmos/f814w/mosaic_cosmos_web_30mas_tile_{tile}_hst_acs_wfc_f814w_{ext}.fits',
            'f098m': '/V/maurice/mosaics/cosmos/f098m/mosaic_cosmos_all_hst_wfc3_ir_f098m_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f090w': '/V/maurice/mosaics/cosmos/f090w/mosaic_nircam_f090w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f115w': '/V/maurice/mosaics/cosmos/f115w/mosaic_nircam_f115w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f140m': '/V/maurice/mosaics/cosmos/f140m/mosaic_nircam_f140m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f150w': '/V/maurice/mosaics/cosmos/f150w/mosaic_nircam_f150w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f182m': '/V/maurice/mosaics/cosmos/f182m/mosaic_nircam_f182m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f200w': '/V/maurice/mosaics/cosmos/f200w/mosaic_nircam_f200w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f210m': '/V/maurice/mosaics/cosmos/f210m/mosaic_nircam_f210m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f250m': '/V/maurice/mosaics/cosmos/f250m/mosaic_nircam_f250m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f277w': '/V/maurice/mosaics/cosmos/f277w/mosaic_nircam_f277w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f335m': '/V/maurice/mosaics/cosmos/f335m/mosaic_nircam_f335m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f356w': '/V/maurice/mosaics/cosmos/f356w/mosaic_nircam_f356w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f360m': '/V/maurice/mosaics/cosmos/f360m/mosaic_nircam_f360m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f410m': '/V/maurice/mosaics/cosmos/f410m/mosaic_nircam_f410m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f430m': '/V/maurice/mosaics/cosmos/f430m/mosaic_nircam_f430m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f444w': '/V/maurice/mosaics/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f460m': '/V/maurice/mosaics/cosmos/f460m/mosaic_nircam_f460m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f480m': '/V/maurice/mosaics/cosmos/f480m/mosaic_nircam_f480m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'gri': '/Users/hba423/simmons/mosaics/cosmos/hsc/hsc_gri_{ext}_{tile}.fits',
            'vis': '/V/maurice/mosaics/cosmos/vis/mosaic_euclid_vis_cosmos_30mas_v1_{tile}_{ext}.fits',
        }
        catalog_file = '/Users/hba423/simmons/cosmos/catalog_cosmos_v1.1_merged.fits'
        detection_image = '/research/COSMOS-3D/highz/catalog/cosmos_v1.0/detection_images/detection_image_ivw_{tile}.fits'
        segmentation_image = '/research/COSMOS-3D/highz/catalog/cosmos_v1.0/detection_images/segmap_ivw_hot+cold_{tile}.fits'

    else:  # candide
        image_data = {
            'u': '/V/maurice/mosaics/cosmos/cfht/cfht_u_{ext}_{tile}.fits',
            'g': '/V/maurice/mosaics/cosmos/hsc/hsc_g_{ext}_{tile}.fits',
            'r': '/V/maurice/mosaics/cosmos/hsc/hsc_r_{ext}_{tile}.fits',
            'i': '/V/maurice/mosaics/cosmos/hsc/hsc_i_{ext}_{tile}.fits',
            'z': '/V/maurice/mosaics/cosmos/hsc/hsc_z_{ext}_{tile}.fits',
            'y': '/V/maurice/mosaics/cosmos/hsc/hsc_y_{ext}_{tile}.fits',
            'Y': '/V/maurice/mosaics/cosmos/uvista/uvista_Y_{ext}_{tile}.fits',
            'J': '/V/maurice/mosaics/cosmos/uvista/uvista_J_{ext}_{tile}.fits',
            'H': '/V/maurice/mosaics/cosmos/uvista/uvista_H_{ext}_{tile}.fits',
            'Ks': '/V/maurice/mosaics/cosmos/uvista/uvista_Ks_{ext}_{tile}.fits',
            'f435w': '/V/maurice/mosaics/cosmos/f435w/mosaic_cosmos_all_hst_acs_wfc_f435w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f606w': '/V/maurice/mosaics/cosmos/f606w/mosaic_cosmos_all_hst_acs_wfc_f606w_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f814w': '/V/maurice/mosaics/cosmos/f814w/mosaic_cosmos_web_30mas_tile_{tile}_hst_acs_wfc_f814w_{ext}.fits',
            'f098m': '/V/maurice/mosaics/cosmos/f098m/mosaic_cosmos_all_hst_wfc3_ir_f098m_30mas_tile_{tile}_v0.3_{ext}.fits',
            'f090w': '/V/maurice/mosaics/cosmos/f090w/mosaic_nircam_f090w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f115w': '/V/maurice/mosaics/cosmos/f115w/mosaic_nircam_f115w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f140m': '/V/maurice/mosaics/cosmos/f140m/mosaic_nircam_f140m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f150w': '/V/maurice/mosaics/cosmos/f150w/mosaic_nircam_f150w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f182m': '/V/maurice/mosaics/cosmos/f182m/mosaic_nircam_f182m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f200w': '/V/maurice/mosaics/cosmos/f200w/mosaic_nircam_f200w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f210m': '/V/maurice/mosaics/cosmos/f210m/mosaic_nircam_f210m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f250m': '/V/maurice/mosaics/cosmos/f250m/mosaic_nircam_f250m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f277w': '/V/maurice/mosaics/cosmos/f277w/mosaic_nircam_f277w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f335m': '/V/maurice/mosaics/cosmos/f335m/mosaic_nircam_f335m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f356w': '/V/maurice/mosaics/cosmos/f356w/mosaic_nircam_f356w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f360m': '/V/maurice/mosaics/cosmos/f360m/mosaic_nircam_f360m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f410m': '/V/maurice/mosaics/cosmos/f410m/mosaic_nircam_f410m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f430m': '/V/maurice/mosaics/cosmos/f430m/mosaic_nircam_f430m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f444w': '/V/maurice/mosaics/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f460m': '/V/maurice/mosaics/cosmos/f460m/mosaic_nircam_f460m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'f480m': '/V/maurice/mosaics/cosmos/f480m/mosaic_nircam_f480m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
            'vis': '/V/maurice/mosaics/cosmos/vis/mosaic_euclid_vis_cosmos_30mas_v1_{tile}_{ext}.fits',
        }
        catalog_file = '/V/maurice/catalog_cosmos_v1.1_merged.fits'
        detection_image = '/V/maurice/detection_images/detection_image_ivw_{tile}.fits'
        segmentation_image = '/V/maurice/detection_images/segmap_ivw_hot+cold_{tile}.fits'

    return image_data, catalog_file, detection_image, segmentation_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_ranges_from_wc(wc, nom):
    """Compute wavelength coverage ranges from wavelength coverage array."""
    ranges = []
    if wc[1] != -1:
        if wc[0] == -1 and wc[1] == -2:
            ranges.append((nom[0], nom[1]))
        elif wc[1] == -2:
            ranges.append((wc[0], nom[1]))
        else:
            ranges.append((wc[0] if wc[0] > 0 else nom[0], wc[1]))
    if wc[2] != -2:
        if wc[2] == -1 and wc[3] == -2:
            ranges.append((nom[0], nom[1]))
        elif wc[2] == -1:
            ranges.append((nom[0], wc[3]))
        elif wc[3] == -2:
            ranges.append((wc[2], nom[1]))
        else:
            ranges.append((wc[2], wc[3]))
    return ranges


# ---------------------------------------------------------------------------
# InspectionPlotGenerator
# ---------------------------------------------------------------------------

class InspectionPlotGenerator:

    def __init__(
        self,
        field,
        output_dir='inspection_plots/',
        output_file_base='cosmos_sed',
        output_format='pdf',
        dpi=500,
        cutout_kwargs=dict(vmin=-3, vmax=8, cmap="Greys"),
        verbose=False,
        lazy_runs=None,
    ):
        self.field = field
        self.output_dir = output_dir
        self.output_file_base = output_file_base
        self.output_format = output_format
        self.dpi = dpi
        self.cutout_kwargs = cutout_kwargs
        self.verbose = verbose
        self.lazy_runs = lazy_runs

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        image_data, catalog_file, detection_image, segmentation_image = _get_cosmos_paths()
        self.image_data = image_data
        self.catalog_file = catalog_file
        self.detection_image_file = detection_image
        self.segmentation_image_file = segmentation_image

        self.catalog = Table.read(self.catalog_file)

    def get_id_of_nearest_object(self, coord):
        catalog_coords = SkyCoord(self.catalog['ra'], self.catalog['dec'], unit='deg')
        sep = catalog_coords.separation(coord)
        i = np.argmin(sep)
        minsep = sep[i].to('arcsec').value
        return self.catalog['id'][i], minsep

    def make_nircam_rgb_cutout(self, tile, coord, cutout_width):
        cutouts = {}
        for band in ['f115w', 'f150w', 'f182m', 'f200w', 'f210m', 'f277w', 'f356w', 'f444w']:
            if band not in self.image_data:
                return None, None

            filepath = self.image_data[band]
            if '{tile}' in filepath:
                filepath = filepath.replace('{tile}', tile)
            if '{ext}' in filepath:
                filepath = filepath.replace('{ext}', 'sci')

            try:
                with fits.open(filepath) as hdul:
                    sci = hdul[0].data
                    wcs = WCS(hdul[0].header)
                    cutout = Cutout2D(sci.data, coord, size=cutout_width * 3, wcs=wcs)
                del sci
                if not np.all(np.isnan(cutout.data)) or np.sum(cutout.data) == 0:
                    cutouts[band] = cutout
            except (FileNotFoundError, NoOverlapError):
                pass

        if np.all(np.isin(['f115w', 'f150w', 'f200w', 'f277w', 'f356w', 'f444w'], list(cutouts.keys()))):
            rgb_dict = {}
            rgb_dict['f115w'] = {'colors': np.array([0.0, 0.0, 1.0]), 'data': cutouts['f115w'].data}
            rgb_dict['f150w'] = {'colors': np.array([0.0, 0.2, 0.8]), 'data': cutouts['f150w'].data}
            rgb_dict['f200w'] = {'colors': np.array([0.0, 0.9, 0.1]), 'data': cutouts['f200w'].data}
            rgb_dict['f277w'] = {'colors': np.array([0.1, 0.9, 0.0]), 'data': cutouts['f277w'].data}
            rgb_dict['f356w'] = {'colors': np.array([8.0, 0.2, 0.0]), 'data': cutouts['f356w'].data}
            rgb_dict['f444w'] = {'colors': np.array([1.0, 0.0, 0.0]), 'data': cutouts['f444w'].data}

        elif np.all(np.isin(['f115w', 'f150w', 'f182m', 'f210m', 'f277w', 'f356w', 'f444w'], list(cutouts.keys()))):
            rgb_dict = {}
            rgb_dict['f115w'] = {'colors': np.array([0.0, 0.0, 1.0]), 'data': cutouts['f115w'].data}
            rgb_dict['f150w'] = {'colors': np.array([0.0, 0.2, 0.8]), 'data': cutouts['f150w'].data}
            rgb_dict['f200w'] = {'colors': np.array([0.0, 0.9, 0.1]), 'data': np.nanmean([cutouts['f182m'].data, cutouts['f210m'].data], axis=0)}
            rgb_dict['f277w'] = {'colors': np.array([0.1, 0.9, 0.0]), 'data': cutouts['f277w'].data}
            rgb_dict['f356w'] = {'colors': np.array([8.0, 0.2, 0.0]), 'data': cutouts['f356w'].data}
            rgb_dict['f444w'] = {'colors': np.array([1.0, 0.0, 0.0]), 'data': cutouts['f444w'].data}

        elif np.all(np.isin(['f115w', 'f150w', 'f277w', 'f444w'], list(cutouts.keys()))):
            rgb_dict = {}
            rgb_dict['f115w'] = {'colors': np.array([0.0, 0.0, 1.0]), 'data': cutouts['f115w'].data}
            rgb_dict['f150w'] = {'colors': np.array([0.0, 0.3, 0.8]), 'data': cutouts['f150w'].data}
            rgb_dict['f277w'] = {'colors': np.array([0.1, 0.9, 0.0]), 'data': cutouts['f277w'].data}
            rgb_dict['f444w'] = {'colors': np.array([1.0, 0.0, 0.0]), 'data': cutouts['f444w'].data}
        else:
            return None, None

        imrgb = gen_rgb_image(rgb_dict, noisesig=1, noiselum=0.1, satpercent=0.8)
        wcs = cutouts['f277w'].wcs
        ps = wcs.proj_plane_pixel_scales()[0].to(u.arcsec).value
        size = np.shape(cutout.data)[0]
        extent = [-size * ps / 2, size * ps / 2, -size * ps / 2, size * ps / 2]
        return imrgb, extent

    def load_cutout_for_band(self, coord, tile, band, cutout_width):
        if band not in self.image_data:
            return None, None

        filepath = self.image_data[band]
        if '{tile}' in filepath:
            filepath = filepath.replace('{tile}', tile)
        if '{ext}' in filepath:
            if os.path.exists(filepath.replace('{ext}', 'sci')):
                filepath = filepath.replace('{ext}', 'sci')
            elif os.path.exists(filepath.replace('{ext}', 'drz')):
                filepath = filepath.replace('{ext}', 'drz')
            else:
                return None, None

        try:
            with fits.open(filepath) as hdul:
                sci = hdul[0].data
                wcs = WCS(hdul[0].header)
                cutout = Cutout2D(sci.data, coord, size=cutout_width, wcs=wcs)
            del sci

            _, median, std = sigma_clipped_stats(cutout.data)
            snr = (cutout.data - median) / std
            wcs = cutout.wcs

            ps = wcs.proj_plane_pixel_scales()[0].to(u.arcsec).value
            size = np.shape(snr)[0]
            extent = [-size * ps / 2, size * ps / 2, -size * ps / 2, size * ps / 2]

            return snr, extent
        except (FileNotFoundError, NoOverlapError):
            return None, None

    def get_output_filepath(self, ID):
        outfilename = f"{self.output_file_base}_{ID}"
        outpath = os.path.join(self.output_dir, outfilename)
        match self.output_format:
            case 'png':
                outpath += '.png'
            case 'pdf':
                outpath += '.pdf'
            case _:
                outpath += f'.{self.output_format}'
        return outpath

    def get_catalog_entries_for_object(self, ID):
        idx = np.where(self.catalog['id'] == ID)[0][0]
        result = {}
        result['id'] = ID
        for key in self.catalog.columns:
            result[key] = self.catalog[key][idx]
        return result

    @staticmethod
    def plot_data(ax,
                  wav, wav_min, wav_max,
                  flux, flux_err,
                  colors, sizes, zorders,
                  plot_xerr=True, annotate=True, label=None,
                  marker='o', mew=1):

        min_mag, max_mag = 0, -32

        colors = np.array(colors)
        wav = wav[flux_err > 0]
        wav_min = wav_min[flux_err > 0]
        wav_max = wav_max[flux_err > 0]
        colors = colors[flux_err > 0]
        flux = flux[flux_err > 0]
        flux_err = flux_err[flux_err > 0]

        snrs = flux / flux_err
        flux_uplim = 2.5 * np.log10(2 * flux_err / 3631e6)
        flux_upper_err = 2.5 * np.log10((flux + flux_err) / 3631e6) - 2.5 * np.log10(flux / 3631e6)
        flux_lower_err = 2.5 * np.log10(flux / 3631e6) - 2.5 * np.log10((flux - flux_err) / 3631e6)
        flux = 2.5 * np.log10(flux / 3631e6)
        for w, w1, w2, f, f_up_err, f_lo_err, f_up_lim, snr, c, s, z in zip(
            wav, wav_min, wav_max, flux, flux_upper_err, flux_lower_err, flux_uplim, snrs, colors, sizes, zorders
        ):
            if f + f_up_err > -16:
                continue
            if snr > 1.5:
                try:
                    ax.errorbar(w, f, yerr=[[f_lo_err], [f_up_err]], linewidth=0, marker=marker, ms=s,
                                mfc=c, mec=c, elinewidth=mew, ecolor=c, mew=mew, capthick=0, capsize=0, zorder=z)
                    if plot_xerr:
                        ax.errorbar(w, f, xerr=[[w - w1], [w2 - w]], linewidth=0, marker='none',
                                    elinewidth=mew, ecolor=c, mew=mew, capthick=0, capsize=0, zorder=z)
                except ValueError:
                    continue
                if not np.isinf(f):
                    min_mag = np.nanmin([min_mag, f - f_lo_err])
                    max_mag = np.nanmax([max_mag, f + f_up_err])
            else:
                ax.errorbar(w, f_up_lim, yerr=0.3, uplims=True, linewidth=0,
                            mfc='none', mec=c, elinewidth=mew, ecolor=c, mew=mew, capthick=1, capsize=s / 2, zorder=z)
                if plot_xerr:
                    ax.errorbar(w, f_up_lim, xerr=[[w - w1], [w2 - w]], linewidth=0, marker='none',
                                elinewidth=mew, ecolor=c, mew=mew, capthick=0, capsize=0, zorder=z)
                if not np.isinf(f_up_lim):
                    min_mag = np.nanmin([min_mag, f_up_lim - 0.3])
                    max_mag = np.nanmax([max_mag, f_up_lim])

        if label:
            handle = ax.errorbar(-100, -100, xerr=1, yerr=1, linewidth=0, marker=marker, ms=sizes[0], mfc=colors[0],
                                 elinewidth=mew, mec=colors[0], ecolor=colors[0], mew=mew, capthick=0, capsize=0, zorder=1)
            return min_mag, max_mag, handle

        return min_mag, max_mag

    def make_single_object_plot(self, ID,
                                overwrite=False, notes=None, msata=False,
                                prism_wavelength_coverage=None,
                                g395m_wavelength_coverage=None,
                                prism_shutter=None,
                                g395m_shutter=None):

        plt.rcParams.update(_SANS_STYLE)

        outfile = self.get_output_filepath(ID)
        if os.path.exists(outfile) and not overwrite:
            return

        cat = self.get_catalog_entries_for_object(ID)
        ra = cat['ra']
        dec = cat['dec']
        tile = cat['tile']
        coord = SkyCoord(ra=ra, dec=dec, unit=u.deg)
        coordstring = coord.to_string('hmsdms', precision=2).split(' ')

        npix = cat['npix']
        display_width_options = [1.5, 2, 3, 4, 5]
        i = 0
        display_width = display_width_options[i] * u.arcsec
        while np.sqrt(npix * 4 * 0.0009) > display_width.to(u.arcsec).value:
            try:
                display_width = display_width_options[i] * u.arcsec
                i += 1
            except Exception:
                display_width = display_width_options[-1] * u.arcsec
                break

        if msata:
            display_width = 1.7 * u.arcsec

        cutout_width = display_width * 1.5

        ######################################################################
        fig = plt.figure(figsize=(10, 6.8), constrained_layout=False)
        gs = mpl.gridspec.GridSpec(ncols=64, nrows=40, figure=fig)
        gs.update(left=0.01, right=0.99, top=0.92, bottom=0.02)

        ax_rgb = plt.subplot(gs[0:8, 0:8])
        ax_detec = plt.subplot(gs[0:8, 8:16])
        ax_segm = plt.subplot(gs[0:8, 16:24])

        ground_bands = ['u', 'g', 'r', 'i', 'z', 'y', 'Y', 'J', 'H', 'Ks', 'gri']
        ground_axes = [plt.subplot(gs[0:4, i:i + 4]) for i in range(24, 63, 4)] + [plt.subplot(gs[4:8, 56:60])]

        hst_bands = ['f435w', 'f606w', 'f814w', 'f098m', 'f105w', 'f125w', 'f140w', 'f160w', 'vis']
        hst_axes = [plt.subplot(gs[4:8, i:i + 4]) for i in range(24, 56, 4)] + [plt.subplot(gs[4:8, 60:64])]

        jwst_bands = ['f090w', 'f115w', 'f140m', 'f150w', 'f162m', 'f182m', 'f200w', 'f210m', 'f250m', 'f277w',
                       'f300m', 'f335m', 'f356w', 'f360m', 'f410m', 'f430m', 'f444w', 'f480m', 'f560w', 'f770w',
                       'f1000w', 'f1280w', 'f1500w', 'f1800w', 'f2100w']
        jwst_axes = ([plt.subplot(gs[8:12, i:i + 4]) for i in range(0, 63, 4)]
                     + [plt.subplot(gs[12:16, i:i + 4]) for i in range(0, 36, 4)])

        ax_flags = plt.subplot(gs[12:16, 36:44])
        ax_flags.axis('off')
        ax_flags.annotate(f"kron_corr: {cat['kron_corr']:.2f}", (0.025, 0.95), color='k', ha='left', va='top',
                          xycoords='axes fraction', weight='bold', fontsize=7)

        for ax in [ax_rgb, ax_detec, ax_segm] + ground_axes + hst_axes + jwst_axes:
            ax.set_aspect('equal')
            ax.tick_params(labelleft=False, labelbottom=False, left=False, right=False, top=False, bottom=False, which='both')

        fig.text(0.015, 0.980, f'ID: {ID}', va='top', ha='left', fontsize=12, weight='bold')
        if tile is not None:
            fig.text(0.015, 0.955, f'{self.field}-{tile}', va='top', ha='left', fontsize=12)
        else:
            fig.text(0.015, 0.955, f'{self.field}', va='top', ha='left', fontsize=12)

        if notes is not None:
            fig.text(0.15, 0.955, str(notes), va='top', ha='left', fontsize=12, color='k')

        fig.text(0.985, 0.980, f'RA, Dec: ({coordstring[0]}, {coordstring[1]})', va='top', ha='right', fontsize=12)
        fig.text(0.985, 0.955, f'({coord.ra.value:.7f}, {coord.dec.value:.6f})', va='top', ha='right', fontsize=12)

        ax_sed = plt.subplot(gs[17:-3, 3:-20])
        ax_sed.set_xlabel('Observed Wavelength [µm]')
        ax_sed.set_ylabel('AB mag')
        ax_sed.tick_params(right=False, which='both')
        ax_sed.semilogx()
        ax_sed.set_xlim(0.3, 11)
        ax_sed.set_ylim(-31, -23)
        ax_sed.set_xticks([0.3, 0.4, 0.6, 1.0, 1.5, 2.0, 3.0, 4.0, 5, 7, 10],
                           ['0.3', '0.4', '0.6', '1', '1.5', '2', '3', '4', '5', '7', '10'])

        ax_pz = plt.subplot(gs[12:21, -19:-1])
        ax_pz.set_xlabel('Redshift')
        ax_pz.set_ylim(0, 1.1)
        ax_pz.set_xlim(0, 21)
        ax_pz.tick_params(labelleft=False, left=False, right=False, top=False, which='both')
        ax_pz.tick_params(direction='inout', which='both')
        ax_pz.tick_params(axis='x', which='major', length=5)
        ax_pz.tick_params(axis='x', which='minor', length=3)
        ax_pz.spines['left'].set_visible(False)
        ax_pz.spines['right'].set_visible(False)
        ax_pz.spines['top'].set_visible(False)

        ax_table = plt.subplot(gs[23:, -19:-1])
        ax_table.axis('off')
        ax_table.set_xlim(0, 1)
        ax_table.set_ylim(0, 1)
        table = {}

        for ax, name in zip(ground_axes, ground_bands):
            if name == 'Ks':
                name = 'K_s'
            ax.text(0.05, 0.95, f'${name}$', transform=ax.transAxes, color='k', va='top', ha='left',
                    path_effects=[pe.withStroke(linewidth=1.3, foreground='w')], size=8)

        for ax, name in zip(hst_axes, hst_bands):
            ax.text(0.05, 0.95, f'{name.upper()}', transform=ax.transAxes, color='k', va='top', ha='left',
                    path_effects=[pe.withStroke(linewidth=1.3, foreground='w')], size=8)

        for ax, name in zip(jwst_axes, jwst_bands):
            ax.text(0.05, 0.95, f'{name.upper()}', transform=ax.transAxes, color='k', va='top', ha='left',
                    path_effects=[pe.withStroke(linewidth=1.3, foreground='w')], size=8)

        if msata:
            ax_detec.text(0.04, 0.96, r'MSATA', transform=ax_detec.transAxes, color='white', va='top', ha='left',
                          path_effects=[pe.withStroke(linewidth=2, foreground='k')], size=10)
        else:
            ax_detec.text(0.04, 0.96, r'Detection', transform=ax_detec.transAxes, color='white', va='top', ha='left',
                          path_effects=[pe.withStroke(linewidth=2, foreground='k')], size=10)
        ax_segm.text(0.04, 0.96, r'Segmentation', transform=ax_segm.transAxes, color='white', va='top', ha='left',
                     path_effects=[pe.withStroke(linewidth=2, foreground='k')], size=10)
        ax_rgb.text(0.04, 0.96, r'NIRCam RGB', transform=ax_rgb.transAxes, color='white', va='top', ha='left',
                    path_effects=[pe.withStroke(linewidth=2, foreground='k')], size=10)

        vmin = self.cutout_kwargs.get('vmin', -3)
        vmax = self.cutout_kwargs.get('vmax', 8)
        cmap = self.cutout_kwargs.get('cmap', 'Greys')
        for ax, band in zip(ground_axes, ground_bands):
            snr, extent = self.load_cutout_for_band(coord, tile, band, cutout_width * 2.5)
            if snr is not None:
                if np.all(np.isnan(snr)) or np.all(snr == 0):
                    ax.set_facecolor('#dececc')
                else:
                    ax.imshow(snr, vmin=vmin, vmax=vmax, cmap=cmap, origin='lower', extent=extent)
                    ax.set_xlim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
                    ax.set_ylim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
            else:
                ax.set_facecolor('#dececc')

        for ax, band in zip(hst_axes, hst_bands):
            snr, extent = self.load_cutout_for_band(coord, tile, band, cutout_width * 1.5)
            if snr is not None:
                if np.all(np.isnan(snr)) or np.all(snr == 0):
                    ax.set_facecolor('#dececc')
                else:
                    ax.imshow(snr, vmin=vmin, vmax=vmax, cmap=cmap, origin='lower', extent=extent)
                    ax.set_xlim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
                    ax.set_ylim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
            else:
                ax.set_facecolor('#dececc')

        for ax, band in zip(jwst_axes, jwst_bands):
            snr, extent = self.load_cutout_for_band(coord, tile, band, cutout_width)
            if snr is not None:
                if np.all(np.isnan(snr)) or np.all(snr == 0):
                    ax.set_facecolor('#dececc')
                else:
                    ax.imshow(snr, vmin=vmin, vmax=vmax, cmap=cmap, origin='lower', extent=extent)
                    ax.set_xlim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
                    ax.set_ylim(-0.5 * display_width.to(u.arcsec).value, 0.5 * display_width.to(u.arcsec).value)
            else:
                ax.set_facecolor('#dececc')

        detec = fits.open(self.detection_image_file.replace('{tile}', tile))
        detec_sci_cutout = Cutout2D(detec[1].data, coord, size=cutout_width * 2, wcs=WCS(detec[1].header))
        detec_err_cutout = Cutout2D(detec[2].data, coord, size=cutout_width * 2, wcs=WCS(detec[1].header))
        del detec

        detec_snr = detec_sci_cutout.data / detec_err_cutout.data
        wcs = detec_sci_cutout.wcs
        ps = wcs.proj_plane_pixel_scales()[0].to(u.arcsec).value
        size = np.shape(detec_snr)[0]
        extent = [-size * ps / 2, size * ps / 2, -size * ps / 2, size * ps / 2]

        d = detec_snr
        cen = np.shape(d)[0] // 2

        if msata:
            from scipy.ndimage import uniform_filter
            box_size = 9
            sci_smoothed = uniform_filter(detec_sci_cutout.data * 0.02115399, size=box_size, mode='constant') * (box_size ** 2)
            sci = 2.5 * np.log10(sci_smoothed / 3631e6)
            cmap_detec = plt.colormaps['Greys_r']
            cmap_detec.set_bad('k')
            vmin_d = -26.5
            vmax_d = np.nanmax(sci[cen - 5:cen + 5, cen - 5:cen + 5])
            if vmax_d < vmin_d:
                vmin_d = vmax_d - 1
            ax_detec.imshow(sci, extent=extent, vmin=vmin_d, vmax=vmax_d, cmap=cmap_detec, interpolation='nearest')
            ax_detec.plot([-0.15, 0.15], [-0.15, -0.15], color='m', linestyle='--', linewidth=0.5, zorder=100)
            ax_detec.plot([-0.15, 0.15], [0.15, 0.15], color='m', linestyle='--', linewidth=0.5, zorder=100)
            ax_detec.plot([-0.15, -0.15], [-0.15, 0.15], color='m', linestyle='--', linewidth=0.5, zorder=100)
            ax_detec.plot([0.15, 0.15], [-0.15, 0.15], color='m', linestyle='--', linewidth=0.5, zorder=100)
        else:
            vmax_d = np.nanpercentile(d[cen - 20:cen + 20, cen - 20:cen + 20], 95)
            if vmax_d < 12:
                vmax_d = 12
            ax_detec.imshow(d, extent=extent, vmin=1, vmax=vmax_d, cmap='Greys_r', interpolation='nearest')

        ax_detec.set_xlim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)
        ax_detec.set_ylim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)

        del detec_sci_cutout, detec_err_cutout

        if not msata:
            a, b, theta = cat['kron1_a'], cat['kron1_b'], np.degrees(cat['theta'])
            ax_detec.add_patch(mpl.patches.Ellipse((0, 0), width=2 * a, height=2 * b, angle=theta,
                                                    facecolor='none', edgecolor='salmon', linestyle='--', linewidth=0.5))
            ax_detec.add_patch(mpl.patches.Circle((0, 0), radius=0.1,
                                                   facecolor='none', edgecolor='w', linestyle='-.', linewidth=0.5))

        segm = fits.open(self.segmentation_image_file.replace('{tile}', tile))
        segm_cutout = Cutout2D(segm[0].data, coord, size=cutout_width * 2, wcs=WCS(segm[0].header))
        del segm

        d = segm_cutout.data
        for i_val, unq_val in enumerate(np.sort(np.unique(d))):
            d[d == unq_val] = i_val

        from photutils.utils.colormaps import make_random_cmap
        from matplotlib.colors import to_rgba
        segm_cmap = make_random_cmap(len(np.unique(d)))
        segm_cmap.colors[0] = to_rgba('k')

        ax_segm.imshow(d, extent=extent, cmap=segm_cmap, interpolation='none')
        ax_segm.set_xlim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)
        ax_segm.set_ylim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)

        del segm_cutout

        imrgb, extent = self.make_nircam_rgb_cutout(tile, coord, cutout_width * 4)

        if imrgb is not None:
            ax_rgb.imshow(imrgb, extent=extent)
        else:
            imrgb = np.zeros((100, 100, 3))
            ax_rgb.imshow(imrgb, extent=[-cutout_width.to(u.arcsec).value / 2, cutout_width.to(u.arcsec).value / 2,
                                          -cutout_width.to(u.arcsec).value / 2, cutout_width.to(u.arcsec).value / 2])
        ax_rgb.set_xlim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)
        ax_rgb.set_ylim(-display_width.to(u.arcsec).value, display_width.to(u.arcsec).value)

        handles, labels = [], []

        short_names = ['vis', 'f435w', 'f606w', 'f814w', 'f098m', 'f090w', 'f115w', 'f140m', 'f150w', 'f182m',
                        'f200w', 'f210m', 'f250m', 'f277w', 'f335m', 'f356w', 'f360m', 'f410m', 'f430m', 'f444w',
                        'f460m', 'f480m', 'f770w']
        wav = np.array([FILTER_WAVELENGTHS[n][0] for n in short_names])
        wav_min = np.array([FILTER_WAVELENGTHS[n][1] for n in short_names])
        wav_max = np.array([FILTER_WAVELENGTHS[n][2] for n in short_names])

        flux_cols = [f'f_auto_{band}' for band in short_names]
        error_cols = [f'e_auto_{band}' for band in short_names]

        flux = np.array([cat[col] for col in flux_cols])
        error = np.array([cat[col] for col in error_cols])
        cond = ~np.isfinite(error) | ~np.isfinite(flux)
        flux[cond] = np.nan
        error[cond] = np.nan
        colors = ['tab:red'] * len(flux)
        sizes = [6] * len(flux)
        zorders = [100] * len(flux)

        min_mag, max_mag, handle = self.plot_data(ax_sed, wav, wav_min, wav_max, flux, error, colors, sizes, zorders,
                                                   annotate=False, plot_xerr=True, label=True)
        handles.append(handle)
        labels.append("AUTO")

        ymin, ymax = ax_sed.get_ylim()
        ymin = np.floor(min_mag - 0.1 * (max_mag - min_mag))
        ymax = np.ceil(max_mag + 0.5 * (max_mag - min_mag))
        while ymax - ymin < 6.5:
            ymin -= 0.01
            ymax += 0.2
        ax_sed.set_ylim(ymin, ymax)

        flux_cols = [f'f_aper_hom_{band}' for band in short_names]
        error_cols = [f'e_aper_hom_{band}' for band in short_names]
        flux = np.array([cat[col][3] for col in flux_cols])
        error = np.array([cat[col][3] for col in error_cols])
        colors = ['#eb8889'] * len(flux)
        sizes = [2] * len(flux)
        zorders = [10] * len(flux)

        _, _, handle = self.plot_data(ax_sed, wav, wav_min, wav_max, flux, error, colors, sizes, zorders,
                                       marker='s', mew=0.7, annotate=False, plot_xerr=True, label=True)
        handles.append(handle)
        labels.append("CIRC ($d=0.3''$)")

        if self.lazy_runs:
            for key, lazy_run in self.lazy_runs.items():
                label_text = lazy_run.get('label', None)
                file = lazy_run.get('file', None)
                file_lowz = lazy_run.get('file_lowz', None)
                color = lazy_run.get('color', None)

                if file_lowz:
                    lazy = fits.getdata(file_lowz, ext=1)
                    i = np.where(lazy['ID'] == ID)[0][0]
                    z_best = lazy['z_best'][i]
                    lazy_chi2_lowz = lazy['chi2'][i]

                    lazy_sed = fits.getdata(file_lowz, ext=-1)
                    izbest = np.argmin(np.abs(z_best - lazy_sed['z']))
                    template_names = [n for n in lazy_sed.dtype.names if n != 'z']
                    templates = np.array([lazy_sed[template][izbest] for template in template_names])
                    coeffs = lazy['coeffs'][i]
                    x = lazy_sed[template_names[0]][0] * (1 + z_best) / 1e4
                    y = np.dot(templates.T, coeffs)
                    y = 2.5 * np.log10(y / 3631e6)
                    y = np.where(~np.isfinite(y), -99, y)
                    p_lowz, = ax_sed.plot(x, y, linewidth=0.8, color='0.7', zorder=1)

                    lazy_pz = fits.getdata(file_lowz, ext=2)
                    x = lazy_pz['Pz'][0]
                    y = lazy_pz['Pz'][i + 1]
                    y = y / np.max(y)
                    ax_pz.fill_between(x, y, facecolor='0.7', edgecolor='none', alpha=0.2)
                    ax_pz.plot(x, y, color='0.7', linewidth=0.8)

                lazy = fits.getdata(file, ext=1)
                i = np.where(lazy['ID'] == ID)[0][0]
                z_best = lazy['z_best'][i]
                lazy_chi2 = lazy['chi2'][i]

                if file_lowz:
                    delta_chi2 = lazy_chi2_lowz - lazy_chi2

                lazy_sed = fits.getdata(file, ext=3)
                izbest = np.argmin(np.abs(z_best - lazy_sed['z']))
                template_names = [n for n in lazy_sed.dtype.names if n != 'z']
                templates = np.array([lazy_sed[template][izbest] for template in template_names])
                coeffs = lazy['coeffs'][i]
                lam_rest = lazy_sed[template_names[0]][0] / 1e4
                lam_obs = lam_rest * (1 + z_best)
                fnu = np.dot(templates.T, coeffs)
                y = 2.5 * np.log10(fnu / 3631e6)
                y = np.where(~np.isfinite(y), -99, y)
                zorder = 2
                if 'fiducial' in key:
                    zorder = 4
                p, = ax_sed.plot(lam_obs, y, linewidth=1, color=color, label=label_text, zorder=zorder)

                tophat = np.array((lam_rest > 0.145) & (lam_rest < 0.155), dtype=int)
                nu = 1 / lam_rest
                fUV = np.trapezoid(fnu * tophat / nu, x=nu) / np.trapezoid(tophat / nu, x=nu)

                dL = cosmo.luminosity_distance(z_best).to(u.pc).value
                mUV = -2.5 * np.log10(fUV / (1 + z_best) / 3631e6)
                MUV = mUV - 5 * (np.log10(dL) - 1)

                windows = (((lam_rest >= .1268) & (lam_rest <= .1284)) | ((lam_rest >= .1309) & (lam_rest <= .1316))
                           | ((lam_rest >= .1342) & (lam_rest <= .1371)) | ((lam_rest >= .1407) & (lam_rest <= .1515))
                           | ((lam_rest >= .1562) & (lam_rest <= .1583)) | ((lam_rest >= .1677) & (lam_rest <= .1740))
                           | ((lam_rest >= .1760) & (lam_rest <= .1833)) | ((lam_rest >= .1866) & (lam_rest <= .1890))
                           | ((lam_rest >= .1930) & (lam_rest <= .1950)) | ((lam_rest >= .2400) & (lam_rest <= .2580)))
                fl = fnu / lam_rest ** 2
                beta = np.polyfit(np.log10(lam_rest[windows]), np.log10(fl[windows]), deg=1)[0]

                lazy_pz = fits.getdata(file, ext=2)
                x = lazy_pz['Pz'][0]
                y = lazy_pz['Pz'][i + 1]
                y = y / np.max(y)
                ax_pz.fill_between(x, y, facecolor=color, edgecolor='none', alpha=0.2, zorder=zorder)
                ax_pz.plot(x, y, color=color, linewidth=1, zorder=zorder + 1)
                Pzgtr8 = np.sum(y[x > 8]) / np.sum(y)

                cdf = np.cumsum(y) / np.sum(y)
                zmed = np.interp(0.5, cdf, x)
                zuperr = np.interp(0.84, cdf, x) - zmed
                zloerr = zmed - np.interp(0.16, cdf, x)

                handles.append(p)
                labels.append(label_text + r' ($z =' f'{zmed:.1f}' r'^{+' f'{zuperr:.1f}' r'}_{-' f'{zloerr:.1f}' r'}$)')

                if file_lowz:
                    handles.append(p_lowz)
                    labels.append(label_text + ' (forced $z<7$)')

                if 'fiducial' in key:
                    Sz = lazy['Sz'][i]
                    table[r'$z_{\rm best}$'] = f'{z_best:.2f}'
                    table[r'$\chi^2$'] = f'{lazy_chi2:.1f}'
                    table[r'$M_{\rm UV}$'] = f'{MUV:.1f}'
                    table[r'$\beta$'] = f'{beta:.1f}'
                    table[r'$S_z$'] = str(int(Sz))
                    try:
                        table[r'$P(z>8)$'] = fr'{int(round(Pzgtr8 * 100))}%'
                    except ValueError:
                        table[r'$P(z>8)$'] = '...'
                    table[r'$\Delta\chi^2$'] = f'{delta_chi2:.1f}'

        table[r'$N_{\rm pix}$'] = str(int(npix))

        table_positions = [
            (0, 0.95), (0, 0.885), (0, 0.82), (0, 0.755),
            (0.5, 0.95), (0.5, 0.885), (0.5, 0.82), (0.5, 0.755),
        ]

        i = 0
        for key, value in table.items():
            pos = table_positions[i]
            ax_table.annotate(f'{key} $=$ {value}', pos, xycoords='axes fraction', va='top', ha='left')
            i += 1

        snr_table = {}
        for b in hst_bands + jwst_bands:
            if b in short_names:
                f = cat[f'f_auto_{b}']
                if np.isfinite(f):
                    try:
                        snr_auto = round(cat[f'f_auto_{b}'] / cat[f'e_auto_{b}'], 1)
                    except TypeError:
                        snr_auto = np.nan
                    try:
                        snr_circ = round(cat[f'f_aper_hom_{b}'][3] / cat[f'e_aper_hom_{b}'][3], 1)
                    except TypeError:
                        snr_circ = np.nan

                    apers = cat[f'f_aper_nat_{b}'] / cat[f'e_aper_nat_{b}']
                    try:
                        snr_0p05 = round(apers[0], 1)
                    except TypeError:
                        snr_0p05 = np.nan
                    try:
                        snr_0p1 = round(apers[1], 1)
                    except TypeError:
                        snr_0p1 = np.nan
                    try:
                        snr_0p2 = round(apers[2], 1)
                    except TypeError:
                        snr_0p2 = np.nan
                    snr_table[b.upper()] = (snr_0p05, snr_0p1, snr_0p2, snr_circ, snr_auto)

        def map_snr_color(snr_val):
            if snr_val < 1.5:
                return 'tab:red'
            elif snr_val < 3.0:
                return '#E7B416'
            else:
                return 'forestgreen'

        ax_table.annotate('SNRs', (0.0, 0.65), ha='left', va='top', xycoords='axes fraction', weight='bold')
        ax_table.annotate("0.05''", (0.27, 0.65), ha='center', va='top', xycoords='axes fraction', weight='bold')
        ax_table.annotate("0.1''", (0.43, 0.65), ha='center', va='top', xycoords='axes fraction', weight='bold')
        ax_table.annotate("0.2''", (0.59, 0.65), ha='center', va='top', xycoords='axes fraction', weight='bold')
        ax_table.annotate("CIRC", (0.74, 0.65), ha='center', va='top', xycoords='axes fraction', weight='bold')
        ax_table.annotate("AUTO", (0.90, 0.65), ha='center', va='top', xycoords='axes fraction', weight='bold')
        ax_table.fill_between([0, 1], 0.595, 0.665, edgecolor='none', facecolor='0.9', zorder=-100)
        table_positions_snr = [0.59, 0.52, 0.45, 0.38, 0.31, 0.24, 0.17, 0.10, 0.03]
        if len(snr_table) > len(table_positions_snr):
            snr_table = {k: v for i, (k, v) in enumerate(snr_table.items()) if i < len(table_positions_snr)}
        i = 0
        for key, value in snr_table.items():
            pos = table_positions_snr[i]
            ax_table.annotate(key, (0.0, pos), ha='left', va='top', xycoords='axes fraction')
            ax_table.annotate(str(value[0]) if np.isfinite(value[0]) else '', (0.26, pos), ha='center', va='top',
                              color=map_snr_color(value[0]), xycoords='axes fraction')
            ax_table.annotate(str(value[1]) if np.isfinite(value[1]) else '', (0.42, pos), ha='center', va='top',
                              color=map_snr_color(value[1]), xycoords='axes fraction')
            ax_table.annotate(str(value[2]) if np.isfinite(value[2]) else '', (0.58, pos), ha='center', va='top',
                              color=map_snr_color(value[2]), xycoords='axes fraction')
            ax_table.annotate(str(value[3]) if np.isfinite(value[3]) else '', (0.74, pos), ha='center', va='top',
                              color=map_snr_color(value[3]), xycoords='axes fraction')
            ax_table.annotate(str(value[4]) if np.isfinite(value[3]) else '', (0.90, pos), ha='center', va='top',
                              color=map_snr_color(value[3]), xycoords='axes fraction')
            if i % 2 != 0:
                ax_table.fill_between([0, 1], pos - 0.055, pos + 0.015, edgecolor='none', facecolor='0.9', zorder=-100)
            i += 1

        if len(handles) > 0:
            ax_sed.legend(handles, labels, loc='upper left', frameon=False, fontsize=9)

        def labels_fmt(x, pos):
            return f'{int(round(-x, 0))}'
        ax_sed.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(labels_fmt))
        ax_sed.grid(linewidth=0.5, color='0.85', zorder=-1000)

        ymin, ymax = ax_sed.get_ylim()
        if prism_wavelength_coverage is not None:
            if all([wc == 0 for wc in prism_wavelength_coverage]):
                prism_wavelength_coverage = None
            else:
                ranges = compute_ranges_from_wc(prism_wavelength_coverage, (0.6, 5.3))
                for r in ranges:
                    ax_sed.fill_between(r, ymax - 0.05 * (ymax - ymin), ymax - 0.03 * (ymax - ymin),
                                         edgecolor='none', facecolor='b', zorder=1000, alpha=0.2)

        if g395m_wavelength_coverage is not None:
            if all([wc == 0 for wc in g395m_wavelength_coverage]):
                g395m_wavelength_coverage = None
            else:
                ranges = compute_ranges_from_wc(g395m_wavelength_coverage, (2.87, 5.27))
                for r in ranges:
                    ax_sed.fill_between(r, ymax - 0.03 * (ymax - ymin), ymax - 0.01 * (ymax - ymin),
                                         edgecolor='none', facecolor='r', zorder=1000, alpha=0.2)

        if prism_shutter is not None:
            ra_s = prism_shutter[0]
            dec_s = prism_shutter[1]
            shutter_c = SkyCoord(ra_s, dec_s, unit='deg')
            pa = prism_shutter[2] * u.deg
            for i in [-1, 0, 1]:
                c = shutter_c.directional_offset_by(-pa, i * 0.53 * u.arcsec)
                p = mpl.patches.Rectangle(
                    ((c.ra.value - coord.ra.value - 0.22 / 3600 / 2) * 3600,
                     (c.dec.value - coord.dec.value - 0.46 / 3600 / 2) * 3600),
                    width=0.22, height=0.46,
                    facecolor='none', edgecolor='lightblue',
                    angle=pa.value, rotation_point='center',
                    zorder=10000, alpha=0.5, linewidth=0.5,
                )
                ax_rgb.add_patch(p)

        if g395m_shutter is not None:
            ra_s = g395m_shutter[0]
            dec_s = g395m_shutter[1]
            shutter_c = SkyCoord(ra_s, dec_s, unit='deg')
            pa = g395m_shutter[2] * u.deg
            for i in [-1, 0, 1]:
                c = shutter_c.directional_offset_by(-pa, i * 0.53 * u.arcsec)
                p = mpl.patches.Rectangle(
                    ((c.ra.value - coord.ra.value - 0.22 / 3600 / 2) * 3600,
                     (c.dec.value - coord.dec.value - 0.46 / 3600 / 2) * 3600),
                    width=0.22, height=0.46,
                    facecolor='none', edgecolor='salmon',
                    angle=pa.value, rotation_point='center',
                    zorder=10000, alpha=0.5, linewidth=0.5,
                )
                ax_rgb.add_patch(p)

        assert self.dpi is not None, 'dpi must be specified for png output'
        plt.savefig(outfile, dpi=self.dpi)
        plt.close()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def generate_sed_plots(
    obs_name: str,
    obs_dir: Path,
    field: str,
    *,
    overwrite: bool = False,
    source_ids: list[int] | None = None,
) -> int:
    """
    Generate SED inspection plots.

    Currently only supports field='cosmos'. For other fields, prints a
    warning and returns 0.

    Returns count of newly generated files.
    """
    if field != 'cosmos':
        print(f"Warning: SED generation not supported for field '{field}' (only cosmos). Skipping.")
        return 0

    spec_pattern = str(obs_dir / f'{obs_name}_*_spec.fits')
    spec_files_all = glob.glob(spec_pattern)
    if not spec_files_all:
        print("No spec files found for SED generation.")
        return 0

    srcids = sorted(set(int(f.split('_')[-2]) for f in spec_files_all))
    if source_ids:
        srcids = [s for s in srcids if s in source_ids]

    if not srcids:
        print("No matching source IDs for SED generation.")
        return 0

    object_ids = [f'{obs_name}_{s}' for s in srcids]

    # Get source positions
    ra_list, dec_list = [], []
    for srcid in srcids:
        spec_files = glob.glob(str(obs_dir / f'{obs_name}_*_{srcid}_spec.fits'))
        rai, deci = get_source_pos(spec_files[0])
        ra_list.append(rai)
        dec_list.append(deci)

    # Cross-match with COSMOS positions catalog
    _, catalog_file, _, _ = _get_cosmos_paths()
    pos_catalog_path = catalog_file.replace('catalog_cosmos_v1.1_merged.fits',
                                            '../target_selection/cosmos/data/cosmos_v1.1_positions.ecsv')
    # Try standard positions file locations
    positions_candidates = [
        '/research/EMBER/target_selection/cosmos/data/cosmos_v1.1_positions.ecsv',
        pos_catalog_path,
    ]
    root = os.environ.get('CAMPFIRE_ROOT', '')
    if root:
        positions_candidates.insert(0, os.path.join(root, 'config', 'cosmos_v1.1_positions.ecsv'))

    pos = None
    for path in positions_candidates:
        if os.path.exists(path):
            pos = Table.read(path)
            break

    if pos is None:
        print("Warning: Could not find COSMOS positions catalog. Skipping SED generation.")
        return 0

    coords = SkyCoord(pos['ra'], pos['dec'], unit='deg')

    generator = InspectionPlotGenerator(
        field='cosmos',
        output_dir=str(obs_dir),
        output_file_base='cosmos_sed',
        lazy_runs={
            'fiducial': {
                'label': "Lazy.jl AUTO",
                'file': '/Users/hba423/simmons/cosmos/catalog_cosmos_v1.1_merged_lazy.fits',
                'file_lowz': '/Users/hba423/simmons/cosmos/catalog_cosmos_v1.1_merged_lazy_lowz.fits',
                'color': '#1751ff',
            },
            'auto': {
                'label': 'Lazy.jl CIRC',
                'file': '/Users/hba423/simmons/cosmos/catalog_cosmos_v1.1_merged_lazy_aper.fits',
                'color': '#A4D0A4',
            },
        },
    )

    generated = 0
    for i, (srcid, object_id) in enumerate(zip(srcids, object_ids)):
        sed_output = obs_dir / f'{object_id}_sed.pdf'
        if sed_output.exists() and not overwrite:
            continue
        
        print(f"  Generating SED plot for {object_id}")
        
        c = SkyCoord(ra_list[i], dec_list[i], unit='deg')
        sep = coords.separation(c).to('arcsec').value
        idx = np.argmin(sep)

        if sep[idx] >= 0.2:
            print(f"  Warning: No catalog match within 0.2'' for {object_id} (nearest: {sep[idx]:.2f}'')")
            continue

        ID = pos['id'][idx]

        try:
            generator.make_single_object_plot(ID, overwrite=True, notes=object_id)

            # Rename from generator output name to our convention
            gen_output = obs_dir / f'cosmos_sed_{ID}.pdf'
            if gen_output.exists():
                gen_output.rename(sed_output)
                generated += 1
        except Exception as e:
            print(f"  Warning: SED generation failed for {object_id}: {e}")

    return generated
