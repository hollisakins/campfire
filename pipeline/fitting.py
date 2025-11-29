"""
NIRSpec Redshift Fitting Script

Perform redshift fitting on extracted NIRSpec spectra using:
1. Non-negative linear combinations of stellar population templates
2. Additional Gaussian emission line components for key lines
3. Additional broad Gaussian emission line components for key lines
4. Template grids that include IGM transmission as a function of redshift
5. Blackbody grids that include a hard Lyman-alpha break at z>5.7

Usage:
python fitting.py --config config.toml

"""

import os

# import tqdm
import glob
import argparse
import logging
import toml
from pathlib import Path
import pickle
import time

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import nnls
from scipy.ndimage import generic_filter
# from scipy.interpolate import interp1d
from astropy.io import fits
# from astropy import units as u
# from astropy.constants import c
from astropy import table
import warnings; warnings.filterwarnings('ignore')
import multiprocessing as mp
from multiprocessing import Pool
mp.set_start_method('fork') 

from spectres.spectral_resampling_numba import spectres_numba as spectres


def air_to_vac(wav_air):
    """Convert air wavelengths to vacuum wavelengths using SDSS formula."""
    # from SDSS: 
    # AIR = VAC / (1.0 + 2.735182E-4 + 131.4182 / VAC^2 + 2.76249E8 / VAC^4)
    vac_wavs = np.logspace(2, 4.5, 1000)
    air_wavs = vac_wavs / (1.0 + 2.735182E-4 + 131.4182 / vac_wavs**2 + 2.76249E8 / vac_wavs**4)
    return np.interp(wav_air, air_wavs, vac_wavs)


def get_wavelength_sampling(wav_min, wav_max, R_curve_file, oversample=4):
    """Generate non-uniform wavelength sampling based on spectral resolution curve."""
    R_curve = fits.open(R_curve_file)[1].data
    x = [wav_min]
    while x[-1] <= wav_max:
        R_val = np.interp(x[-1], R_curve['WAVELENGTH'], R_curve['R'])
        dwav = x[-1]/R_val/oversample
        x.append(x[-1] + dwav)
    return np.array(x)


def resample_to_nonuniform_grid(old_wav, old_grid, R_curve_file, oversample=4):
    """
    Step 1: Resample template grid to non-uniform wavelength sampling based on R-curve.
    
    Parameters:
    -----------
    old_wav : array
        Template wavelength grid
    old_grid : array
        Template grid (n_templates, n_redshifts, n_wavelengths)
    R_curve_file : str
        Path to spectral resolution curve file
    oversample : int
        Oversampling factor for LSF convolution
        
    Returns:
    --------
    tuple : (temp_wav, temp_grid)
        temp_wav: Non-uniform wavelength grid
        temp_grid: Resampled template grid
    """
    
    old_wav = old_wav.astype(np.float64)
    old_grid = old_grid.astype(np.float64)

    temp_wav = get_wavelength_sampling(old_wav.min(), old_wav.max(), R_curve_file, oversample=oversample)
    temp_grid = spectres(temp_wav, old_wav, old_grid, fill=0, verbose=False)
    
    return temp_wav, temp_grid


def convolve_with_lsf(temp_wav, temp_grid, oversample=4):
    """
    Step 2: Convolve templates with Line Spread Function.
    
    Parameters:
    -----------
    temp_wav : array
        Non-uniform wavelength grid
    temp_grid : array
        Template grid on non-uniform wavelength grid
    oversample : int
        Oversampling factor for LSF convolution
        
    Returns:
    --------
    array : LSF-convolved template grid
    """
    sigma_pix = oversample/2.35/1.3  # sigma width of kernel in pixels
    k_size = 4*int(sigma_pix+1)
    x_kernel_pix = np.arange(-k_size, k_size+1)

    kernel = np.exp(-(x_kernel_pix**2)/(2*sigma_pix**2))
    kernel /= np.trapezoid(kernel)  # Explicitly normalise kernel

    # Disperse non-uniformly sampled spectrum
    n_templ, n_z, _ = np.shape(temp_grid)
    convolved_grid = np.zeros_like(temp_grid)
    for i in range(n_templ):
        for j in range(n_z):
            convolved_grid[i,j,:] = np.convolve(temp_grid[i,j,:], kernel, mode='same')
    return convolved_grid


def resample_to_observed_grid(temp_wav, convolved_grid, observed_wav):
    """
    Step 3: Final resampling to object's observed wavelength grid.
    
    Parameters:
    -----------
    temp_wav : array
        Non-uniform wavelength grid
    convolved_grid : array
        LSF-convolved template grid
    observed_wav : array
        Observed wavelength grid
        
    Returns:
    --------
    array : Template grid resampled to observed wavelength grid
    """
    observed_wav = observed_wav.astype(np.float64)
    new_grid = spectres(observed_wav, temp_wav, convolved_grid, fill=0, verbose=False)
    return new_grid

def calculate_redshift_confidence(z_array, chi2_array, zbest):
    """
    Calculate redshift confidence based on chi-squared distribution.
    
    Quality flag is set to 0 by default and will be updated during visual inspection.
    
    Parameters:
    -----------
    z_array : array
        Redshift grid
    chi2_array : array
        Chi-squared values corresponding to redshift grid
    zbest : float
        Best-fit redshift
        
    Returns:
    --------
    dict : Confidence metrics
        {
            'redshift': float - Best redshift rounded to 4 decimal places
            'redshift_quality': int - Quality flag (0 = unreviewed, to be set during inspection)
            'chi2_min': float - Minimum chi-squared value
            'confidence': float - Confidence percentage
        }
    """
    try:
        # Calculate confidence using numerically stable method
        # Offset chi2 by minimum to prevent underflow in exp(-large_number)
        chi2_min_val = np.min(chi2_array)
        chi2_offset = chi2_array - chi2_min_val  # Minimum becomes 0
        pz = np.exp(-chi2_offset)  # Convert to probability (max prob = 1.0)
        
        # Calculate confidence within ±0.03 of best redshift
        confidence_mask = np.abs(z_array - zbest) <= 0.03
        confidence = np.sum(pz[confidence_mask]) / np.sum(pz) * 100
        
        # Quality flag is 0 by default - will be set during visual inspection
        z_quality = 0   # Default: unreviewed, to be set during visual inspection
        
        return {
            'redshift': round(zbest, 4),        # Round to 4 decimal places
            'redshift_quality': z_quality,      # Default 0, updated during inspection
            'chi2_min': float(chi2_min_val),
            'confidence': float(confidence)
        }
        
    except Exception as e:
        # If confidence calculation fails, return safe defaults
        return {
            'redshift': round(zbest, 4),
            'redshift_quality': 0,  # Default 0 even for failed calculations
            'chi2_min': float(np.min(chi2_array)) if len(chi2_array) > 0 else 0.0,
            'confidence': 0.0
        }

def _iter(A, mask, flux, err):
    """Single iteration of chi-squared minimization using NNLS."""
    okt = A[:, mask].sum(axis=1) != 0
    Ax = A[okt, :] / err
    yx = flux / err
    x = nnls(Ax[:, mask].T, yx[mask])
    coeffs = np.zeros(A.shape[0])
    coeffs[okt] = x[0]
    model = A.T.dot(coeffs)
    chi2_i = np.sum(np.power((flux[mask] - model[mask]) / err[mask], 2))
    # BIC=np.log(np.sum(mask))*len(x[0])+chi2_i
    return chi2_i, model
        

def fit_single_spectrum(spec_file,file_paths,convolved_wav,convolved_grid,zgrid,logger,save_models=False):
    """Perform redshift fitting for a single spectrum file."""
    start_time = time.time()
    spec_path = Path(spec_file)
    base_name = spec_path.stem.replace('_spec', '')
    zfit_file = file_paths['output_path'] + f"{base_name}_zfit.fits"
    logger.debug(f"Fitting redshift for {base_name}")
        
    try:
        # Load spectrum
        tab=table.Table.read(spec_file,hdu=1)
        wav = tab['wave'].value.astype('float32')
        flux = tab['fnu'].value.astype('float32')
        err = tab['fnu_err'].value.astype('float32')
        mask = np.isfinite(flux) & np.isfinite(err) & (err > 0)
        #mask the edge pixels to avoid edge effects
        maskidx=np.where(mask)[0]
        if len(maskidx)>10:
            for idx in maskidx[:5]: mask[idx]=False
            for idx in maskidx[-5:]: mask[idx]=False
        #add 10% error floor
        err[err<0.1*np.abs(flux)]=0.1*np.abs(flux[err<0.1*np.abs(flux)])
        #require errors to be >= to a rolling median error (removes low error spikes, but keeps high spikes)
        med_err=generic_filter(err,np.nanmedian,size=15)
        err[err<med_err]=med_err[err<med_err]
        
        logger.debug('Resampling template grid to observed wavelength grid')
        templates = resample_to_observed_grid(convolved_wav, convolved_grid, wav)
        logger.debug("Running redshift fitting with chi2 minimization")
        
        # Chi-squared minimization over redshift grid
        chi2 = np.zeros_like(zgrid,dtype='float32')
        models = np.zeros((len(zgrid),len(wav)),dtype='float32')
        for iz in range(len(zgrid)):
            A = templates[:,iz,:]
            chi2[iz], models[iz] = _iter(A, mask, flux, err)
            
        # Find best-fit redshift
        izbest = np.argmin(chi2)
        zbest = zgrid[izbest]
        
        # Calculate confidence and quality
        confidence_results = calculate_redshift_confidence(zgrid, chi2, zbest)
        model = models[izbest]

        logger.debug(f"✓ Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']})")
        logger.debug(f"nans in chi2: {len(chi2[np.isnan(chi2)])}")

        # Save results with confidence information in header
        header = fits.Header({
            'EXTEND': True,
            'ZBEST': confidence_results['redshift'],
            'ZQUAL': confidence_results['redshift_quality'],
            'ZCONF': confidence_results['confidence'],
            'CHI2MIN': confidence_results['chi2_min']
        })
        t0 = fits.PrimaryHDU(header=header)
        t1 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='wav', array=wav, format='D'),fits.Column(name='fnu', array=model, format='D')]), header=fits.Header({'EXTNAME':'MODEL'}))
        t2 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='z', array=zgrid, format='D'),fits.Column(name='chi2', array=chi2, format='D')]), header=fits.Header({'EXTNAME':'CHI2'}))
        if save_models: 
            t3=fits.ImageHDU(models.astype('float32'),header=fits.Header({'EXTNAME':'MODELS'}))
            out_hdul = fits.HDUList([t0, t1, t2, t3])
        else: out_hdul = fits.HDUList([t0, t1, t2])
        out_hdul.writeto(zfit_file, overwrite=True)
        
        processing_time = time.time() - start_time
        logger.info(f"✓ Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']}, "
                       f"t={processing_time:.1f}s)")
        
        return True, f"Success: z={confidence_results['redshift']:.4f}, conf={confidence_results['confidence']:.1f}%"
        
    except Exception as e:
        logger.error(f"✗ Failed to fit redshift for {base_name}: {e}")
        # Save failed fit information with quality=0
        # try:
        #     header = fits.Header({
        #         'EXTEND': True,
        #         'ZBEST': 0.0,
        #         'ZQUAL': 0,  # Failed fit
        #         'ZCONF': 0.0,
        #         'CHI2MIN': 0.0,
        #         'ERROR': str(e)[:68]  # FITS header limit
        #     })
        #     t0 = fits.PrimaryHDU(header=header)
        #     out_hdul = fits.HDUList([t0])
        #     out_hdul.writeto(zfit_file, overwrite=True)
        # except:  pass  # If we can't save error info, that's OK
        return False, str(e)

def planck(lam,T): #lam in um, T in K, return blackbody in f_nu
    nu=299792458e6/lam
    bb = 2*6.626e-34*nu**3/(3e5)**2/(np.exp(6.626e-34*nu/1.381e-23/T)-1)
    bb[~np.isfinite(bb)]=0
    return bb
    
def main():
    """Main function to run NIRSpec redshift fitting."""
    parser = argparse.ArgumentParser(description='NIRSpec Redshift Fitting Script')
    parser.add_argument('--config', type=str, default='config.toml', help='Path to configuration file (default: config.toml)')
    parser.add_argument('--obs', type=str, required=True, help='Observation name from observations.toml')
    args = parser.parse_args()
    config_path=args.config
    with open(config_path, 'r') as f: config = toml.load(f)
    paths = config.get('paths', {})
    obs = toml.load('observations.toml')[args.obs]
    gratings = obs['gratings']
    
    # Template file paths (convert to absolute paths)
    file_paths={}
    file_paths['continuum_templates_file'] = os.path.abspath(paths.get('continuum_templates_file'))
    # file_paths['line_templates_file'] = os.path.abspath(paths.get('line_templates_file'))
    file_paths['input_path']=paths.get('products_dir') + f'/{args.obs}/'
    file_paths['output_path']=file_paths['input_path']

    options=config.get('fitting', {})
    ncores=options.get('ncores',1)
    save_models=options.get('save_models',False)
    overwrite=options.get('overwrite',False)
    
    # Set up logging
    log_config = config.get('logging', {})
    log_level = log_config.get('level', 'INFO').upper()
    log_format = log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    logging.basicConfig(level=getattr(logging, log_level), format=log_format)
    logger = logging.getLogger('nirspec_fitting')
    
    for grating in gratings:
        logger.info(f"Fitting {grating} spectra...")
        file_paths['r_curve_file'] = os.path.abspath(paths['r_curve_files'][grating.lower()])
    
        logger.debug(f"Template file paths:")
        logger.debug(f"  R-curve: {file_paths['r_curve_file']}")
        logger.debug(f"  Continuum templates: {file_paths['continuum_templates_file']}")
        # logger.debug(f"  Line templates: {file_paths['line_templates_file']}")
        logger.debug(f"Input path: {file_paths['input_path']}")
        logger.debug(f"Output path: {file_paths['output_path']}")
        
        # Check if template files exist
        for name, path in [("R-curve", file_paths['r_curve_file']), 
                        ("continuum templates", file_paths['continuum_templates_file'])]:
                        #("line templates", file_paths['line_templates_file'])]:
            if not os.path.exists(path): logger.warning(f"{name} file not found: {path}")

        try:
            with open(file_paths['continuum_templates_file'], 'rb') as f: continuum_templates = pickle.load(f)
            # with open(file_paths['line_templates_file'], 'rb') as f: line_templates = pickle.load(f)
                
            logger.info("Loaded templates successfully")
        except (FileNotFoundError, IOError) as e:
            logger.error(f"Failed to load template files: {e}")
            return False, f"Template loading failed: {e}"
            
        template_wav = continuum_templates['wavelengths']
        zgrid = continuum_templates['redshifts']
        
        # Create emission line templates
        logger.info('Creating emission line templates')
        emlines = {
            'Lya': 1215.670,
            'CIV1550d': 1549.480,
            'OIII1663d': 1663.000,
            'CIII1908d': 1908.734,
            'MgII2799d': air_to_vac(2799.117),
            'OII3727d': air_to_vac(3727.424),
            'NeIII3869d': [air_to_vac(3868.760), air_to_vac(3967.470), 0.3],
            #'H-epsilon': air_to_vac(3970.079),
            #'H-delta': air_to_vac(4101.742),
            'H-gamma': [air_to_vac(4340.471), air_to_vac(4861.333), 1/.47, air_to_vac(6562.819), 2.86/.47],
            'OIII4363': air_to_vac(4363.210),
            'H-beta': [air_to_vac(4861.333), air_to_vac(6562.819), 2.86], #Force H-alpha with HB
            'OIII5007d': [air_to_vac(4958.911), air_to_vac(5006.843), 2.98],
            'HeI5876': air_to_vac(5875.624),
            'Halpha': air_to_vac(6562.819),
            'NII6585d': [air_to_vac(6548.050), air_to_vac(6583.460), 2.94],
            'SII6716': [air_to_vac(6716.440), air_to_vac(6562.819), 1.0], #Force H-alpha presence with SII
            'SII6731': [air_to_vac(6730.810), air_to_vac(6562.819), 1.0], #Force H-alpha presence with SII
            'SIII9068d': [air_to_vac(9068.600), air_to_vac(9531.100), 2.5],
            'HeI': air_to_vac(10830.340),
            'Pa-gamma': [air_to_vac(10938.086), 12821.6, 1.0, 18756.1, 1.0], #Force Pa-alpha presence
            'Pa-beta': [12821.6, 18756.1, 1] ,#Force Pa-alpha presence
            'Pa-alpha': 18756.1
            #'Br-beta': air_to_vac(26251.29), #hurts some high-z fits
            #'Br-alpha': air_to_vac(40511.30), #hurts some high-z fits
        }

        spec_wavs=template_wav*1e4
        emline_templates = np.zeros((len(emlines), len(zgrid), len(spec_wavs)))
        for j,line in enumerate(emlines):
            for i in range(len(zgrid)):
                z = zgrid[i]
                fnu = np.zeros(len(spec_wavs))
                rest_wav = emlines[line]
                if isinstance(rest_wav, list):
                    idx=np.argmin(np.abs(spec_wavs - rest_wav[0]*(1+z)))
                    if idx>1 and idx<len(fnu)-1: fnu[idx] = 1
                    extra_wavs=rest_wav[1::2]
                    extra_ratios=rest_wav[2::2]
                    for extra_num in range(len(extra_wavs)):
                        idx=np.argmin(np.abs(spec_wavs - extra_wavs[extra_num]*(1+z)))
                        if idx>1 and idx<len(fnu)-1: fnu[idx] = extra_ratios[extra_num]
                else:
                    idx=np.argmin(np.abs(spec_wavs - rest_wav*(1+z)))
                    if idx>1 and idx<len(fnu)-1: fnu[idx] = 1 #fix argmin edge cases
                emline_templates[j,i,:] = fnu
        
        line_templates = {
            'templates': list(emlines),
            'redshifts': zgrid,
            'wavelengths': spec_wavs/1e4,
            'grid': emline_templates
        }
        # with open('line_templates.pickle', 'wb') as outfile:
        #     pickle.dump(output, outfile)


        #make broadline templates
        # Create emission line templates
        logger.info('Creating broad line templates')
        emlines = {
            'H-beta': air_to_vac(4861.333),
            'Halpha': air_to_vac(6562.819),
        }
        velocities=[1500,3000]
        spec_wavs=template_wav*1e4
        broadline_templates = np.zeros((len(emlines)*len(velocities), len(zgrid), len(spec_wavs)))
        for j,broad in enumerate([[line,velo] for line in emlines for velo in velocities]):
            for i in range(len(zgrid)):
                z = zgrid[i]
                rest_wav = emlines[broad[0]]
                fnu = np.exp(-(rest_wav*(1+z)-spec_wavs)**2/(2*(broad[1]/2.355/3e5*rest_wav*(1+z))**2))
                fnu[fnu<1e-3]=0 #fixes underflow problems
                broadline_templates[j,i,:] = fnu
        broadline_templates[~np.isfinite(broadline_templates)]=0
        broadline_templates = {
            'templates': list(emlines),
            'redshifts': zgrid,
            'wavelengths': spec_wavs/1e4,
            'grid': broadline_templates
        }

        #make blackbody templates
        logger.info('Creating blackbody templates')
        temperatures=[500,2500,5000]
        
        blackbody_templates = np.zeros((len(temperatures), len(zgrid), len(spec_wavs)))
        for j,temperature in enumerate(temperatures):
            for i in range(len(zgrid)):
                z = zgrid[i]
                fnu = planck(template_wav/(1+z),temperature)
                if z>5.7: fnu[template_wav/(1+z)<0.121567]=0 #force Lya break in blackbody at z>5.7
                fnu=fnu/np.max(fnu)
                blackbody_templates[j,i,:] = fnu
        blackbody_templates = {
            'templates': list(temperatures),
            'redshifts': zgrid,
            'wavelengths': spec_wavs/1e4,
            'grid': blackbody_templates
        }
            
        templates = np.vstack((continuum_templates['grid'], 
                            line_templates['grid'], 
                            blackbody_templates['grid'], 
                            broadline_templates['grid']
                            ))

        logger.info('Resampling template grid to non-uniform wavelength grid')
        convolved_wav, temp_grid = resample_to_nonuniform_grid(template_wav, templates, file_paths['r_curve_file'], oversample=4)
        convolved_wav=convolved_wav.astype('float32')
        
        logger.info('Convolving template grid with LSF')
        convolved_grid = convolve_with_lsf(convolved_wav, temp_grid, oversample=4)
        convolved_grid=convolved_grid.astype('float32')
        
        all_spec_files= np.array(sorted(glob.glob(file_paths['input_path']+f'/*{grating.lower()}*_spec.fits')))
        spec_files=[]
        
        if not overwrite: #check which files need to be processed if not overwriting
            spec_files=[]
            for spec_file in all_spec_files:
                spec_path = Path(spec_file)
                base_name = spec_path.stem.replace('_spec', '')
                zfit_file = file_paths['output_path'] + f"{base_name}_zfit.fits"
                if os.path.exists(zfit_file): logger.info(f"Zfit file already exists for {base_name}, skipping")
                else: spec_files.append(spec_file)
        else: spec_files=all_spec_files

        logger.info(f"Starting redshift fitting for {len(spec_files)} objects using {ncores} cores")
        start_time=time.time()
        if ncores>1: 
            with Pool(ncores) as pool: _=pool.starmap(fit_single_spectrum,[[spec_file,file_paths,convolved_wav,convolved_grid,zgrid,logger,save_models] for spec_file in spec_files])
        else: 
            for spec_file in spec_files: fit_single_spectrum(spec_file,file_paths,convolved_wav,convolved_grid,zgrid,logger,save_models)
        end_time=time.time()
        logger.info(f"Fit {len(spec_files)} redshifts in {end_time-start_time:.1f}s")


    return 0

if __name__ == "__main__": exit(main())