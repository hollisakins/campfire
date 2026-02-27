"""
Stage 3: Astrometric alignment (JHAT), bad pixel masking, sky matching,
outlier detection, and drizzle resampling for NIRCam imaging.

Ported from nircamx/stage3.py with config/path/logging refactored to use
the campfire_pipeline common infrastructure.
"""

import sys
import os
import glob
import shutil
import warnings
from datetime import datetime

import numpy as np
import numpy.ma as ma
import tqdm
from scipy.stats.mstats import trim
from scipy.optimize import curve_fit
from shapely.geometry import Polygon

from astropy.table import Table
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
from astropy.stats import SigmaClip, sigma_clipped_stats
import astropy.units as u

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from jhat import align_wcs_batch

from photutils.background import (
    Background2D,
    BiweightLocationBackground,
    BkgIDWInterpolator,
    BkgZoomInterpolator,
    MedianBackground,
    SExtractorBackground,
)

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.constants import SW_FILTERS, LW_FILTERS


# ---------------------------------------------------------------------------
# Utility helpers (ported inline from nircamx/utils.py)
# ---------------------------------------------------------------------------

def _gaussian(x, a, mu, sig):
    """Simple 1-D Gaussian."""
    return a * np.exp(-(x - mu) ** 2 / (2 * sig ** 2))


def _check_files_exist(file_paths):
    """Return True if every file in *file_paths* exists."""
    return all(os.path.exists(p) for p in file_paths)


# ---------------------------------------------------------------------------
# fit_pedestal
# ---------------------------------------------------------------------------

def fit_pedestal(data):
    """Fit distribution of sky fluxes with a Gaussian. Returns simple mean of Gaussian distribution."""
    std = sigma_clipped_stats(data)[2]
    bins = np.linspace(-10 * std, 10 * std, 500)
    h, b = np.histogram(data, bins=bins)
    h = h / np.max(h)
    bc = 0.5 * (b[1:] + b[:-1])
    binsize = b[1] - b[0]

    p0 = [1, bc[np.argmax(h)], std]
    popt, pcov = curve_fit(_gaussian, bc, h, p0=p0)

    return popt[1]


# ---------------------------------------------------------------------------
# JHAT astrometric alignment
# ---------------------------------------------------------------------------

def jhat_step(cal_file, field, stage_config, filtname=None, overwrite=False):
    """Run JHAT WCS alignment on a single cal file.

    Parameters
    ----------
    cal_file : str
        Path to a ``*_cal.fits`` file.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict (merged defaults + overrides).
    filtname : str
        Filter name for output subdirectory.
    overwrite : bool
        Overwrite existing products.
    """
    assert filtname is not None

    jhat_cfg = stage_config['jhat']

    verbose = jhat_cfg.get('verbose', True)
    debug = jhat_cfg.get('debug', True)

    input_dir = os.path.dirname(cal_file)
    cal_file_name = os.path.basename(cal_file)

    input_files = [cal_file_name.replace('_cal.fits', '*_cal.fits')]

    outrootdir = field.stage3_dir
    outsubdir = filtname

    refcat = os.path.join(field.refcat_dir, jhat_cfg['refcat_dict'][filtname])

    align_batch = align_wcs_batch()
    align_batch.verbose = verbose
    align_batch.debug = debug
    align_batch.sip_err = jhat_cfg.get('sip_err', 1.0)
    align_batch.replace_sip = True
    align_batch.sip_degree = 3
    align_batch.sip_points = 128
    align_batch.rough_cut_px_min = jhat_cfg.get('rough_cut_px_min', 2.5)
    align_batch.rough_cut_px_max = jhat_cfg.get('rough_cut_px_max', 2.5)
    align_batch.d_rotated_Nsigma = jhat_cfg.get('d_rotated_Nsigma', 3.0)

    # get the input files
    align_batch.get_input_files(input_files, directory=input_dir, detectors=None, filters=None, pupils=None)

    ixs_all = align_batch.getindices()

    if len(ixs_all) == 0:
        log('JHAT: No images found! exiting...')
        sys.exit(0)

    # get the output filenames
    ixs_exists, ixs_notexists = align_batch.get_output_filenames(
        ixs=ixs_all,
        outrootdir=outrootdir,
        outsubdir=outsubdir,
        addfilter2outsubdir=False,
    )

    ixs_todo = ixs_notexists[:]

    if len(ixs_exists) > 0:
        if overwrite:
            ixs_todo.extend(ixs_exists)
            log(f'{len(ixs_exists)} output images already exist, overwriting them since overwrite=True')
        else:
            log(f'{len(ixs_exists)} output images already exist, skipping since overwrite=False')

    if len(ixs_todo) == 0:
        return

    log(f'Output directory:{os.path.dirname(align_batch.t.loc[ixs_todo[0],"outfilename"])}')
    try:
        align_batch.align_wcs(
            ixs_todo,
            overwrite=True,
            outrootdir=outrootdir,
            outsubdir=outsubdir,
            addfilter2outsubdir=False,
            photometry_method='aperture',
            find_stars_threshold=3.0,
            sci_xy_catalog=None,
            use_dq=False,
            refcatname=refcat,
            refcat_racol='RA',
            refcat_deccol='DEC',
            refcat_magcol='mag',
            refcat_magerrcol='mag_err',
            refcat_colorcol=None,
            pmflag=False,
            pm_median=False,
            load_photcat_if_exists=False,
            rematch_refcat=False,
            SNR_min=10.0,
            d2d_max=jhat_cfg.get('d2d_max', 1.5),
            dmag_max=0.1,
            sharpness_lim=(None, None),
            roundness1_lim=(None, None),
            delta_mag_lim=jhat_cfg.get('delta_mag_lim', [-3, 4]),
            objmag_lim=jhat_cfg.get('objmag_lim', [19, 28]),
            refmag_lim=(None, None),
            slope_min=-10 / 2048.0,
            Nbright4match=None,
            Nbright=None,
            histocut_order=jhat_cfg.get('histocut_order', 'dxdy'),
            xshift=0.0,
            yshift=0.0,
            iterate_with_xyshifts=jhat_cfg.get('iterate_with_xyshifts', True),
            showplots=0,
            saveplots=jhat_cfg.get('saveplots', True),
            savephottable=jhat_cfg.get('savephottable', True),
        )
    except Exception:
        log(f'JHAT failed on {cal_file}')
        raise

    align_batch.write()


# ---------------------------------------------------------------------------
# Bad pixel mask construction
# ---------------------------------------------------------------------------

def stack_dq_by_detector(filtname, field, stage_config, overwrite=False):
    """Build per-detector bad-pixel masks by stacking DQ arrays from cal files.

    Parameters
    ----------
    filtname : str
        Filter name.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    overwrite : bool
        Overwrite existing products.
    """
    from jwst.datamodels import ImageModel

    bp_cfg = stage_config['bad_pixel']
    threshold = bp_cfg.get('threshold', 0.2)

    if filtname.lower() in SW_FILTERS:
        files = [
            f'fl_pixels_{filtname}_nrca1.fits', f'fl_pixels_{filtname}_nrca2.fits',
            f'fl_pixels_{filtname}_nrca3.fits', f'fl_pixels_{filtname}_nrca4.fits',
            f'fl_pixels_{filtname}_nrcb1.fits', f'fl_pixels_{filtname}_nrcb2.fits',
            f'fl_pixels_{filtname}_nrcb3.fits', f'fl_pixels_{filtname}_nrcb4.fits',
        ]
        files = [os.path.join(field.bad_pixel_dir, f) for f in files]
        if _check_files_exist(files) and not overwrite:
            log(f'Bad pixel masks for {filtname} already exist at {field.bad_pixel_dir}/, skipping...')
            return
    elif filtname.lower() in LW_FILTERS:
        files = [f'fl_pixels_{filtname}_nrcalong.fits', f'fl_pixels_{filtname}_nrcblong.fits']
        files = [os.path.join(field.bad_pixel_dir, f) for f in files]
        if _check_files_exist(files) and not overwrite:
            log(f'Bad pixel masks for {filtname} already exist at {field.bad_pixel_dir}/, skipping...')
            return

    log(f'Building bad pixel masks for {filtname}...')

    cal_files = glob.glob(os.path.join(field.stage2_dir, filtname, '*_cal.fits'))

    if filtname.lower() in SW_FILTERS:
        a1 = np.zeros((2048, 2048))
        a2 = np.zeros((2048, 2048))
        a3 = np.zeros((2048, 2048))
        a4 = np.zeros((2048, 2048))
        b1 = np.zeros((2048, 2048))
        b2 = np.zeros((2048, 2048))
        b3 = np.zeros((2048, 2048))
        b4 = np.zeros((2048, 2048))

        with tqdm.tqdm(total=len(cal_files)) as pbar:
            for cal_file in cal_files:
                flag = fits.getdata(cal_file, extname='DQ')
                flag[flag >= 1] = 1

                if 'nrca1' in cal_file:
                    a1 += flag
                if 'nrca2' in cal_file:
                    a2 += flag
                if 'nrca3' in cal_file:
                    a3 += flag
                if 'nrca4' in cal_file:
                    a4 += flag
                if 'nrcb1' in cal_file:
                    b1 += flag
                if 'nrcb2' in cal_file:
                    b2 += flag
                if 'nrcb3' in cal_file:
                    b3 += flag
                if 'nrcb4' in cal_file:
                    b4 += flag

                pbar.update(1)

        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrca1.fits'), a1, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrca2.fits'), a2, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrca3.fits'), a3, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrca4.fits'), a4, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcb1.fits'), b1, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcb2.fits'), b2, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcb3.fits'), b3, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcb4.fits'), b4, overwrite=True)

        a1 = a1 / np.max(a1)
        a2 = a2 / np.max(a2)
        a3 = a3 / np.max(a3)
        a4 = a4 / np.max(a4)
        b1 = b1 / np.max(b1)
        b2 = b2 / np.max(b2)
        b3 = b3 / np.max(b3)
        b4 = b4 / np.max(b4)

        a1[a1 > threshold] = 1
        a2[a2 > threshold] = 1
        a3[a3 > threshold] = 1
        a4[a4 > threshold] = 1
        b1[b1 > threshold] = 1
        b2[b2 > threshold] = 1
        b3[b3 > threshold] = 1
        b4[b4 > threshold] = 1

        a1[a1 <= threshold] = 0
        a2[a2 <= threshold] = 0
        a3[a3 <= threshold] = 0
        a4[a4 <= threshold] = 0
        b1[b1 <= threshold] = 0
        b2[b2 <= threshold] = 0
        b3[b3 <= threshold] = 0
        b4[b4 <= threshold] = 0

        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrca1.fits'), a1, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrca2.fits'), a2, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrca3.fits'), a3, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrca4.fits'), a4, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcb1.fits'), b1, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcb2.fits'), b2, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcb3.fits'), b3, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcb4.fits'), b4, overwrite=True)

    if filtname.lower() in LW_FILTERS:
        a = np.zeros((2048, 2048))
        b = np.zeros((2048, 2048))

        with tqdm.tqdm(total=len(cal_files)) as pbar:
            for cal_file in cal_files:
                flag = fits.getdata(cal_file, extname='DQ')
                flag[flag >= 1] = 1

                if 'nrcalong' in cal_file:
                    a += flag
                if 'nrcblong' in cal_file:
                    b += flag

                pbar.update(1)

        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcalong.fits'), a, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'stack_dq_{filtname}_nrcblong.fits'), b, overwrite=True)

        a = a / np.max(a)
        b = b / np.max(b)

        a[a > threshold] = 1
        b[b > threshold] = 1

        a[a <= threshold] = 0
        b[b <= threshold] = 0

        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcalong.fits'), a, overwrite=True)
        fits.writeto(os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_nrcblong.fits'), b, overwrite=True)


def remove_bad_pixels(jhat_file, field, stage_config, filtname=None):
    """Update the DQ extension of a JHAT-aligned file using the bad-pixel mask.

    Parameters
    ----------
    jhat_file : str
        Path to a ``*_jhat.fits`` file.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    filtname : str
        Filter name.
    """
    from stdatamodels import util as stutil
    from jwst.datamodels import ImageModel

    model = ImageModel(jhat_file)
    # check that image has not already been flagged
    for entry in model.history:
        if 'Masked bad pixels' in entry['description']:
            log(f'DQ mask already updated for {os.path.basename(jhat_file)}, skipping...')
            return

    log(f'Updating DQ mask given bad pixel mask for {os.path.basename(jhat_file)}...')

    detector = None
    for det_name in ['nrca1', 'nrca2', 'nrca3', 'nrca4',
                     'nrcb1', 'nrcb2', 'nrcb3', 'nrcb4',
                     'nrcalong', 'nrcblong']:
        if det_name in jhat_file:
            detector = det_name

    fl_file = os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_{detector}.fits')
    fl = fits.getdata(fl_file).astype(bool)

    model.dq[fl] |= 1

    # add history entry
    time = datetime.now()
    stepdescription = f"Masked bad pixels; {time.strftime('%Y-%m-%d %H:%M:%S')}"
    substr = stutil.create_history_entry(stepdescription)
    model.history.append(substr)

    model.save(jhat_file)


# ---------------------------------------------------------------------------
# Sky matching
# ---------------------------------------------------------------------------

def skymatch_step(jhat_files, field, stage_config, filtname):
    """Run the JWST skymatch step per-visit.

    Parameters
    ----------
    jhat_files : list of str
        JHAT-aligned image paths.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    filtname : str
        Filter name.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list

    sky_cfg = stage_config['skymatch']

    visit_list = []
    for jhat_file in jhat_files:
        visit = os.path.basename(jhat_file).split('_')[0]
        if visit not in visit_list:
            visit_list.append(visit)

    for i, visit in enumerate(visit_list):
        log(f'Running skymatch_step on visit {visit} ({i + 1}/{len(visit_list)})...')
        visit_imgfile_list = sorted(
            glob.glob(os.path.join(field.stage3_dir, filtname, f'{visit}*_jhat.fits'))
        )
        asn_file = os.path.join(field.stage3_dir, filtname, f'sky_{visit}_asn.json')
        asn = asn_from_list.asn_from_list(
            visit_imgfile_list, rule=DMS_Level3_Base, product_name='skymatch_files'
        )
        with open(asn_file, 'w') as outfile:
            name, serialized = asn.dump(format='json')
            outfile.write(serialized)

        stepsize = sky_cfg.get('stepsize', None)
        lower = sky_cfg.get('lower', None)
        upper = sky_cfg.get('upper', None)
        if stepsize == 'none':
            stepsize = None
        if lower == 'none':
            lower = None
        if upper == 'none':
            upper = None

        params = {
            'assign_mtwcs':      {'skip': True},
            'tweakreg':          {'skip': True},
            'skymatch':          {'skymethod':  sky_cfg.get('skymethod', 'match'),
                                  'match_down': sky_cfg.get('match_down', True),
                                  'subtract':   sky_cfg.get('subtract', True),
                                  'stepsize':   stepsize,
                                  'skystat':    sky_cfg.get('skystat', 'mode'),
                                  'dqbits':     sky_cfg.get('dqbits', '~DO_NOT_USE+NON_SCIENCE'),
                                  'lower':      lower,
                                  'upper':      upper,
                                  'nclip':      sky_cfg.get('nclip', 10),
                                  'binwidth':   sky_cfg.get('binwidth', 0.1)},
            'outlier_detection': {'skip': True},
            'resample':          {'skip': True},
            'source_catalog':    {'skip': True},
        }

        from jwst.pipeline import calwebb_image3
        output = calwebb_image3.Image3Pipeline.call(
            asn_file,
            output_dir=os.path.join(field.stage3_dir, filtname),
            steps=params,
            save_results=True,
        )


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------

def outlier_step_prep(visit, jhat_files, jhat_sregions, field, stage_config, filtname, overwrite=False):
    """Identify groups of overlapping visits for outlier detection.

    Generates an association file that includes the target visit plus all
    spatially overlapping exposures.

    Parameters
    ----------
    visit : str
        Visit identifier.
    jhat_files : list of str
        All JHAT file paths for this filter.
    jhat_sregions : list of str
        S_REGION strings for each file.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    filtname : str
        Filter name.
    overwrite : bool
        Overwrite existing products.

    Returns
    -------
    str or None
        Path to the generated ASN file, or None if processing can be skipped.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.associations import load_asn

    outlier_cfg = stage_config['outlier']
    max_radius = outlier_cfg.get('max_radius', 20)

    visit_imgfile_list = [f for f in jhat_files if visit in f]
    visit_sregion_list = [s for s, f in zip(jhat_sregions, jhat_files) if visit in f]
    base_dir = os.path.dirname(visit_imgfile_list[0])
    asn_file = os.path.join(base_dir, f'outlier_detection_{visit}_asn.json')

    log(f'Generating outlier asn file {os.path.basename(asn_file)}...')

    # Find additional files to include
    # these may not be in the same visit, but have some overlap on-sky
    addnl_visit_imgfile_list = []

    # Algorithm to find overlapping visits
    # > Comparing visit A (already included) and visit B (checking whether to include)
    # > If any corner of A is contained within visit B,
    # > or any corner of visit B is contained within visit A,
    # > then visit B gets included

    from matplotlib.path import Path
    for visit_A, region_A in zip(visit_imgfile_list, visit_sregion_list):
        # compute corner coordinates for visit A
        ra = [float(s) for s in region_A.split()[2::2]]
        dec = [float(s) for s in region_A.split()[3::2]]
        polygon_A = Path(np.array([ra, dec]).T, closed=True)

        for visit_B, region_B in zip(jhat_files, jhat_sregions):
            if visit_B in visit_imgfile_list:
                continue

            # compute corner coordinates for visit B
            ra = [float(s) for s in region_B.split()[2::2]]
            dec = [float(s) for s in region_B.split()[3::2]]
            polygon_B = Path(np.array([ra, dec]).T, closed=True)

            overlap = False
            for p in polygon_A.vertices:
                if overlap:
                    break
                if polygon_B.contains_point(p):
                    overlap = True

            for p in polygon_B.vertices:
                if overlap:
                    break
                if polygon_A.contains_point(p):
                    overlap = True

            if overlap:
                addnl_visit_imgfile_list.append(visit_B)

    visit_imgfile_list += addnl_visit_imgfile_list
    log(f'Including {len(visit_imgfile_list)} files for visit {visit}')

    asn = asn_from_list.asn_from_list(
        visit_imgfile_list, rule=DMS_Level3_Base, product_name='outlier_files'
    )

    # Handle existing asn files and *crf files.
    if os.path.exists(asn_file):
        jf = glob.glob(os.path.join(field.stage3_dir, filtname, f'{visit}*_jhat.fits'))
        cf = glob.glob(os.path.join(field.stage3_dir, filtname, f'{visit}*_crf.fits'))
        all_crf_files_exist = len(jf) == len(cf)

        with open(asn_file) as fp:
            asn_old = load_asn(fp)
        old_visit_imgfile_list = sorted([d['expname'] for d in asn_old['products'][0]['members']])
        visit_imgfile_list = sorted(visit_imgfile_list)
        asn_file_unchanged = visit_imgfile_list == old_visit_imgfile_list

        if all_crf_files_exist and asn_file_unchanged and not overwrite:
            log(f'Skipping outlier detection for visit {visit}; all *.crf files exist, and asn file is unchanged from previous')
            return None

        elif overwrite:
            log(f'Will overwrite *.crf files for visit {visit}; overwrite=True')

        elif all_crf_files_exist:
            log(f'Will overwrite *.crf files for visit {visit}; asn file has changed.')

        elif asn_file_unchanged:
            log(f'Will overwrite *.crf files for visit {visit}; asn file unchanged, but missing crf files.')

    else:
        log(f"Outlier step for visit {visit} will be run for the first time")

    # Export the asn file
    with open(asn_file, 'w') as outfile:
        name, serialized = asn.dump(format='json')
        outfile.write(serialized)

    return asn_file


def outlier_step_prep_by_file(jhat_file, jhat_files, jhat_sregions, field, stage_config, filtname, overwrite=False):
    """Per-file variant of outlier_step_prep.

    Identifies spatially overlapping exposures for a single JHAT file and
    generates an association file for outlier detection.

    Parameters
    ----------
    jhat_file : str
        Path to the target JHAT file.
    jhat_files : list of str
        All JHAT file paths for this filter.
    jhat_sregions : list of str
        S_REGION strings for each file.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    filtname : str
        Filter name.
    overwrite : bool
        Overwrite existing products.

    Returns
    -------
    str or None
        Path to the generated ASN file, or None if processing can be skipped.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.associations import load_asn

    outlier_cfg = stage_config['outlier']

    with fits.open(jhat_file) as f:
        jhat_sregion = f[1].header['S_REGION']
    base_dir = os.path.dirname(jhat_file)
    filename = os.path.basename(jhat_file).rstrip("_jhat.fits")
    asn_file = os.path.join(base_dir, f'outlier_detection_{filename}_asn.json')

    log(f'Generating outlier asn file {os.path.basename(asn_file)}...')

    # Find additional files to include
    addnl_visit_imgfile_list = []

    from matplotlib.path import Path
    # compute corner coordinates for the target file
    ra = [float(s) for s in jhat_sregion.split()[2::2]]
    dec = [float(s) for s in jhat_sregion.split()[3::2]]
    polygon_A = Path(np.array([ra, dec]).T, closed=True)

    for visit_B, region_B in zip(jhat_files, jhat_sregions):
        if visit_B == jhat_file:
            continue

        # compute corner coordinates for visit B
        ra = [float(s) for s in region_B.split()[2::2]]
        dec = [float(s) for s in region_B.split()[3::2]]
        polygon_B = Path(np.array([ra, dec]).T, closed=True)

        overlap = False
        for p in polygon_A.vertices:
            if overlap:
                break
            if polygon_B.contains_point(p):
                overlap = True

        for p in polygon_B.vertices:
            if overlap:
                break
            if polygon_A.contains_point(p):
                overlap = True

        if overlap:
            addnl_visit_imgfile_list.append(visit_B)

    visit_imgfile_list = [jhat_file] + addnl_visit_imgfile_list

    asn = asn_from_list.asn_from_list(
        visit_imgfile_list, rule=DMS_Level3_Base, product_name='outlier_files'
    )

    # Handle existing asn files and *crf files.
    if os.path.exists(asn_file):
        jf = glob.glob(os.path.join(field.stage3_dir, filtname, f'{filename}*_jhat.fits'))
        cf = glob.glob(os.path.join(field.stage3_dir, filtname, f'{filename}*_crf.fits'))
        all_crf_files_exist = len(jf) == len(cf)

        with open(asn_file) as fp:
            asn_old = load_asn(fp)
        old_visit_imgfile_list = sorted([d['expname'] for d in asn_old['products'][0]['members']])
        visit_imgfile_list = sorted(visit_imgfile_list)
        asn_file_unchanged = visit_imgfile_list == old_visit_imgfile_list

        if all_crf_files_exist and asn_file_unchanged and not overwrite:
            log(f'Skipping outlier detection for file {filename}; all *.crf files exist, and asn file is unchanged from previous')
            return None

        elif overwrite:
            log(f'Will overwrite *.crf files for file {filename}; overwrite=True')

        elif all_crf_files_exist:
            log(f'Will overwrite *.crf files for file {filename}; asn file has changed.')

        elif asn_file_unchanged:
            log(f'Will overwrite *.crf files for file {filename}; asn file unchanged, but missing crf files.')

    else:
        log(f"Outlier step for file {filename} will be run for the first time")

    # Export the asn file
    with open(asn_file, 'w') as outfile:
        name, serialized = asn.dump(format='json')
        outfile.write(serialized)

    return asn_file


def outlier_step(asn_file, field, stage_config, filtname):
    """Run JWST Image3Pipeline outlier detection on an association file.

    Parameters
    ----------
    asn_file : str
        Path to the ASN JSON file.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    filtname : str
        Filter name.
    """
    outlier_cfg = stage_config['outlier']

    visit = os.path.basename(asn_file).lstrip('outlier_detection_').rstrip('_asn.json')

    visit_path = os.path.join(field.stage3_dir, filtname, visit)
    if not os.path.exists(visit_path):
        os.mkdir(visit_path)

    outlier_path = os.path.join(field.stage3_dir, filtname, visit, 'outliers')
    if not os.path.exists(outlier_path):
        os.mkdir(outlier_path)

    params = {
        'assign_mtwcs':      {'skip': True},
        'tweakreg':          {'skip': True},
        'skymatch':          {'skip': True},
        'resample':          {'skip': True},
        'source_catalog':    {'skip': True},
        'outlier_detection': {
            'weight_type':               outlier_cfg.get('weight_type', 'ivm'),
            'pixfrac':                   outlier_cfg.get('pixfrac', 1.0),
            'kernel':                    outlier_cfg.get('kernel', 'square'),
            'fillval':                   outlier_cfg.get('fillval', 'INDEF'),
            'maskpt':                    outlier_cfg.get('maskpt', 0.1),
            'snr':                       outlier_cfg.get('snr', '3.0 2.0'),
            'scale':                     outlier_cfg.get('scale', '1.2 0.7'),
            'backg':                     outlier_cfg.get('backg', 0.0),
            'resample_data':             outlier_cfg.get('resample_data', True),
            'good_bits':                 outlier_cfg.get('good_bits', '~DO_NOT_USE'),
            'save_intermediate_results': True,
            'save_results':              True,
        },
    }

    from jwst.pipeline import calwebb_image3
    output = calwebb_image3.Image3Pipeline.call(
        asn_file,
        output_dir=outlier_path,
        steps=params,
        save_results=True,
    )

    crf_files = glob.glob(os.path.join(outlier_path, f'{visit}*_crf.fits'))
    for input_file in crf_files:
        output_file = os.path.join(field.stage3_dir, filtname, os.path.basename(input_file))
        shutil.move(input_file, output_file)

    # Remove the visit subdirectory (these take up lots of space,
    # and we only care about the crf files which we've already
    # moved up to the main directory)
    if os.path.exists(visit_path):
        shutil.rmtree(visit_path)


# ---------------------------------------------------------------------------
# Drizzle resampling
# ---------------------------------------------------------------------------

def resample_step(filtname, field, stage_config):
    """Drizzle-combine CRF files into mosaic tiles.

    Parameters
    ----------
    filtname : str
        Filter name.
    field : Field
        NIRCam field dataclass.
    stage_config : dict
        Stage-3 configuration dict.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list

    resample_cfg = stage_config['resample']
    files_to_skip = stage_config.get('files_to_skip', [])

    imgfile_list = field.get_crf_files(filtname, skip=files_to_skip if files_to_skip else None)

    pixel_scale = resample_cfg.get('pixel_scale', '60mas')
    if isinstance(pixel_scale, str):
        assert pixel_scale.endswith('mas')
        pixel_scale_str = str(pixel_scale)
        pixel_scale = float(pixel_scale_str[:-3]) / 1000
    elif isinstance(pixel_scale, (float, int)):
        if pixel_scale > 1:  # assumed given in mas
            pixel_scale_str = f'{str(int(pixel_scale))}mas'
            pixel_scale = float(pixel_scale) / 1000
        else:  # assumed given in arcsec
            pixel_scale_str = f'{str(int(pixel_scale * 1000))}mas'
            pixel_scale = float(pixel_scale)

    mode = resample_cfg.get('mode', 'tile')
    if mode == 'tile':

        version = resample_cfg.get('version', 'v0_1')
        tiles = resample_cfg.get('tile', None)
        if tiles is None:
            tiles = list(field.tiles.keys())
        if isinstance(tiles, str):
            tiles = [tiles]

        for tile in tiles:
            log(f'Running resample_step for tile {tile}, {filtname}, {pixel_scale_str}')

            mosaic_name = resample_cfg.get(
                'mosaic_name',
                'mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[version]_[tile]',
            )
            mosaic_name = mosaic_name.replace('[filter]', filtname)
            mosaic_name = mosaic_name.replace('[field_name]', field.name)
            mosaic_name = mosaic_name.replace('[pixel_scale]', pixel_scale_str)
            mosaic_name = mosaic_name.replace('[version]', version)
            mosaic_name = mosaic_name.replace('[tile]', tile)
            mosaic_outdir = os.path.join(field.mosaic_dir, filtname)
            mosaic_file = os.path.join(mosaic_outdir, f'{mosaic_name}_i2d.fits')

            log(f'Output will go to {mosaic_file}')

            if not os.path.exists(mosaic_file):
                ### select the files that overlap the tile we want to drizzle
                tile_polygon = Polygon(field.get_tile_corners(tile))

                selected_files = []
                for file in imgfile_list:
                    coords_rect = np.zeros((4, 2))
                    hdulist = fits.open(file, ignore_missing_simple=True)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        wcs = WCS(hdulist[1].header, naxis=2)
                    pixcoords = np.array([[0., 0.], [2048., 0.], [2048., 2048.], [0., 2048.]])
                    worldcoords = wcs.wcs_pix2world(pixcoords, 0)
                    aa = 0
                    for coords in worldcoords:
                        coords_rect[aa, 0] = coords[0]
                        coords_rect[aa, 1] = coords[1]
                        aa += 1

                    file_polygon = Polygon(coords_rect)

                    if tile_polygon.intersects(file_polygon):
                        selected_files.append(file)

                if len(selected_files) == 0:
                    log(f'No files found for tile {tile}, skipping...')
                    continue

                log(f'Preparing to drizzle+combine {len(selected_files)} images')

                asn_file = os.path.join(field.stage3_dir, filtname, f'{mosaic_name}_asn.json')
                asn = asn_from_list.asn_from_list(
                    selected_files, rule=DMS_Level3_Base, product_name=mosaic_name
                )
                with open(asn_file, 'w') as outfile:
                    name, serialized = asn.dump(format='json')
                    outfile.write(serialized)

                crpix, crval, shape, rotation = field.get_tile_wcs(tile, ps=pixel_scale_str)

                params = {
                    'assign_mtwcs':    {'skip': True},
                    'tweakreg':          {'skip': True},
                    'skymatch':          {'skip': True},
                    'outlier_detection': {'skip': True},
                    'resample':          {'pixfrac':      resample_cfg.get('pixfrac', 1),
                                          'kernel':       resample_cfg.get('kernel', 'square'),
                                          'pixel_scale':  pixel_scale,
                                          'rotation':     rotation,
                                          'output_shape': shape,
                                          'crpix':        crpix,
                                          'crval':        crval,
                                          'fillval':      'indef',
                                          'weight_type':  'ivm',
                                          'single':       False,
                                          'blendheaders': True,
                                          'save_results': True},
                    'source_catalog':    {'skip': True},
                }

                from jwst.pipeline import calwebb_image3
                output = calwebb_image3.Image3Pipeline.call(
                    asn_file,
                    output_dir=mosaic_outdir,
                    steps=params,
                    save_results=True,
                )
            else:
                log(f'Skipping resample_step for {os.path.basename(mosaic_file)}')

            if resample_cfg.get('background_subtract', True):
                from campfire_pipeline.nircam.bkgsub import SubtractBackground

                if not os.path.exists(mosaic_file.replace('_i2d.fits', '_i2d_before_bkgsub.fits')):

                    bkg = SubtractBackground(
                        ring_radius_in=resample_cfg.get('ring_radius_in', 80),
                        ring_width=resample_cfg.get('ring_width', 4),
                        ring_clip_max_sigma=resample_cfg.get('ring_clip_max_sigma', 5.0),
                        ring_clip_box_size=resample_cfg.get('ring_clip_box_size', 100),
                        ring_clip_filter_size=resample_cfg.get('ring_clip_filter_size', 3),
                        tier_kernel_size=resample_cfg.get('tier_kernel_size', [25, 15, 5, 2]),
                        tier_npixels=resample_cfg.get('tier_npixels', [15, 10, 3, 1]),
                        tier_nsigma=resample_cfg.get('tier_nsigma', [1.5, 1.5, 1.5, 1.5]),
                        tier_dilate_size=resample_cfg.get('tier_dilate_size', [33, 25, 21, 19]),
                        bg_box_size=resample_cfg.get('bg_box_size', 10),
                        bg_filter_size=resample_cfg.get('bg_filter_size', 5),
                        bg_exclude_percentile=resample_cfg.get('bg_exclude_percentile', 90),
                        bg_sigma=resample_cfg.get('bg_sigma', 3),
                        bg_interpolator=resample_cfg.get('bg_interpolator', 'zoom'),
                        suffix='bkgsub',
                        replace_sci=True,
                    )

                    bkg.call(mosaic_file)

                    mosaic_file_orig = mosaic_file.replace('_i2d.fits', '_i2d_before_bkgsub.fits')
                    log(f"Copying input to {os.path.basename(mosaic_file_orig)}")
                    shutil.copy2(mosaic_file, mosaic_file_orig)

                    log(f"Renaming {os.path.basename(bkg.outfile)} to {os.path.basename(mosaic_file)}")
                    shutil.move(bkg.outfile, mosaic_file)
                else:
                    log(f'Skipping background subtraction for {os.path.basename(mosaic_file)}')

            if resample_cfg.get('split_extensions', True):
                log('Splitting extensions')

                sci = fits.getdata(mosaic_file, extname='SCI')
                hdr = fits.getheader(mosaic_file, extname='SCI')
                err = fits.getdata(mosaic_file, extname='ERR')
                wht = fits.getdata(mosaic_file, extname='WHT')

                ext_outdir = os.path.join(mosaic_outdir, 'extensions')
                if not os.path.exists(ext_outdir):
                    os.mkdir(ext_outdir)

                hdu = fits.PrimaryHDU(data=sci, header=hdr)
                hdu.writeto(
                    os.path.join(ext_outdir, os.path.basename(mosaic_file).replace('_i2d.fits', '_sci.fits')),
                    overwrite=True,
                )

                hdr.update({'EXTNAME': 'ERR'})
                hdu = fits.PrimaryHDU(data=err, header=hdr)
                hdu.writeto(
                    os.path.join(ext_outdir, os.path.basename(mosaic_file).replace('_i2d.fits', '_err.fits')),
                    overwrite=True,
                )

                hdr.update({'EXTNAME': 'WHT'})
                hdu = fits.PrimaryHDU(data=wht, header=hdr)
                hdu.writeto(
                    os.path.join(ext_outdir, os.path.basename(mosaic_file).replace('_i2d.fits', '_wht.fits')),
                    overwrite=True,
                )

                has_srcmask = True
                try:
                    srcmask = fits.getdata(mosaic_file, extname='SRCMASK')
                except Exception:
                    log(f'{mosaic_name} has no extension SRCMASK')
                    has_srcmask = False

                if has_srcmask:
                    hdr.update({'EXTNAME': 'SRCMASK'})
                    hdu = fits.PrimaryHDU(data=srcmask, header=hdr)
                    hdu.writeto(
                        os.path.join(ext_outdir, os.path.basename(mosaic_file).replace('_i2d.fits', '_srcmask.fits')),
                        overwrite=True,
                    )

    else:
        raise Exception('only mode=tile supported atm')


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_stage3(field, stage_config, filters=None, n_processes=1, overwrite=False):
    """Orchestrate all stage-3 steps for a NIRCam field.

    Parameters
    ----------
    field : Field
        NIRCam field dataclass (must have workspace set up).
    stage_config : dict
        Merged stage-3 configuration dict.
    filters : list of str, optional
        Filters to process. If None, uses ``field.filters``.
    n_processes : int
        Number of parallel workers for parallelisable steps.
    overwrite : bool
        Overwrite existing products.
    """
    from campfire_pipeline.common.parallel import dispatch

    if filters is None:
        filters = field.filters

    files_to_skip = stage_config.get('files_to_skip', [])

    log(f"Stage 3 for field '{field.name}': filters={filters}")

    for filtname in filters:
        log(f"--- Stage 3: {filtname} ---")

        # Ensure per-filter output directories exist
        os.makedirs(os.path.join(field.stage3_dir, filtname), exist_ok=True)
        os.makedirs(os.path.join(field.mosaic_dir, filtname), exist_ok=True)

        # ----- JHAT alignment -----
        log(f'Running JHAT alignment for {filtname}...')
        cal_files = field.get_cal_files(filtname, skip=files_to_skip if files_to_skip else None)
        if cal_files:
            tasks = [(f,) for f in cal_files]
            dispatch(
                jhat_step,
                tasks,
                n_processes=n_processes,
                use_starmap=True,
                field=field,
                stage_config=stage_config,
                filtname=filtname,
                overwrite=overwrite,
            )
        else:
            log(f'No cal files found for {filtname}, skipping JHAT')

        # ----- Bad pixel masking -----
        log(f'Running bad pixel step for {filtname}...')
        stack_dq_by_detector(filtname, field, stage_config, overwrite=overwrite)
        jhat_files = field.get_jhat_files(filtname, skip=files_to_skip if files_to_skip else None)
        if jhat_files:
            tasks = [(f,) for f in jhat_files]
            dispatch(
                remove_bad_pixels,
                tasks,
                n_processes=n_processes,
                use_starmap=True,
                field=field,
                stage_config=stage_config,
                filtname=filtname,
            )

        # ----- Sky matching -----
        log(f'Running skymatch step for {filtname}...')
        jhat_files = field.get_jhat_files(filtname, skip=files_to_skip if files_to_skip else None)
        if jhat_files:
            skymatch_step(jhat_files, field, stage_config, filtname)

        # ----- Outlier detection -----
        log(f'Running outlier detection for {filtname}...')
        jhat_files = field.get_jhat_files(filtname, skip=files_to_skip if files_to_skip else None)
        if jhat_files:
            # Collect S_REGION headers for overlap computation
            jhat_sregions = []
            for jf in jhat_files:
                with fits.open(jf) as hdul:
                    jhat_sregions.append(hdul[1].header['S_REGION'])

            # Build unique visit list
            visit_list = []
            for jf in jhat_files:
                visit = os.path.basename(jf).split('_')[0]
                if visit not in visit_list:
                    visit_list.append(visit)

            # Prep association files (can be parallelised)
            asn_files = []
            for visit in visit_list:
                asn_file = outlier_step_prep(
                    visit, jhat_files, jhat_sregions, field, stage_config, filtname,
                    overwrite=overwrite,
                )
                if asn_file is not None:
                    asn_files.append(asn_file)

            # Run outlier detection
            if asn_files:
                tasks = [(af,) for af in asn_files]
                dispatch(
                    outlier_step,
                    tasks,
                    n_processes=n_processes,
                    use_starmap=True,
                    field=field,
                    stage_config=stage_config,
                    filtname=filtname,
                )

        # ----- Drizzle resampling -----
        log(f'Running resample step for {filtname}...')
        resample_step(filtname, field, stage_config)

    log(f"Stage 3 complete for field '{field.name}'")
