"""
NIRSpec Plotting Script

Generate comprehensive plots for all extracted spectra in a given observation.
Adapted from EMBER pipeline standalone_plotting.py with NIRSpec directory structure integration.

Usage:
python plots.py --obs capers_cosmos_p2
python plots.py --obs capers_cosmos_p2 --plots 2d thumbnail
python plots.py --obs capers_cosmos_p2 --overwrite
"""

from typing import Dict, Optional, Tuple, Any, List
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib.figure import Figure
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.utils.exceptions import AstropyWarning
import warnings
import os
import glob
import argparse
import logging
import toml


def load_config(config_path="config.toml"):
    """Load and parse configuration file with path template expansion."""
    with open(config_path, 'r') as f:
        config = toml.load(f)
    
    return config


def load_observations(obs_path="observations.toml"):
    """Load and parse observations configuration file."""
    with open(obs_path, 'r') as f:
        observations = toml.load(f)
    return observations


def get_observation_config(obs_name, observations):
    """Get configuration for a specific observation."""
    if obs_name not in observations:
        raise ValueError(f"Observation '{obs_name}' not found in configuration")
    
    obs_config = observations[obs_name].copy()
    obs_config['name'] = obs_name
    
    # Convert 'ids' field to 'source_ids' and handle 'all' case
    if 'ids' in obs_config:
        obs_config['source_ids'] = obs_config.pop('ids')
    
    # Extract program ID from files field if not explicitly provided
    if 'program_id' not in obs_config and 'files' in obs_config:
        files_pattern = obs_config['files']
        if files_pattern.startswith('jw'):
            # Extract program ID from JWST filename pattern: jw<ppppp>...
            obs_config['program_id'] = files_pattern[2:7]
    
    return obs_config


def create_2d_spectrum_plot(hdul: fits.HDUList, output_path: Optional[Path] = None,
                           title: Optional[str] = None, figure_size: Tuple[int, int] = (8, 6),
                           dpi: int = 200) -> Tuple[bool, Optional[Figure], Optional[str]]:
    """
    Generate 2D spectrum plot with spatial profile.
    
    Parameters:
    -----------
    hdul : fits.HDUList
        FITS HDU list containing spectrum data with SPEC1D, SCI, WHT, PROF1D extensions
    output_path : Path, optional
        Path to save the plot
    title : str, optional
        Plot title
    figure_size : tuple
        Figure size (width, height) in inches
    dpi : int
        Figure DPI
        
    Returns:
    --------
    tuple : (success, figure, error_message)
        success: bool - Whether plot generation succeeded
        figure: Figure or None - Matplotlib figure object
        error_message: str or None - Error message if failed
    """
    try:
        # Check required extensions
        required_extensions = ['SPEC1D', 'SCI', 'WHT', 'PROF1D']
        for ext in required_extensions:
            if ext not in hdul:
                return False, None, f"Missing required extension: {ext}"
        
        # Extract data
        spec1d = hdul['SPEC1D'].data    
        wave = spec1d['wave']
        fnu = spec1d['flux']
        fnu_err = spec1d['err']
        
        # Dual flux units with proper conversion
        flam = fnu/wave**2 * 2.99792458e-19
        flam_err = fnu_err/wave**2 * 2.99792458e-19

        valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)
        
        # Create figure with 3 rows: 2D spectrum, f_nu, f_lambda
        fig = plt.figure(figsize=figure_size, constrained_layout=True, dpi=dpi)
        gs = mpl.gridspec.GridSpec(nrows=3, ncols=2, width_ratios=[9,1], 
                                  height_ratios=[1,2.5,2.5], figure=fig)

        ax_2d = plt.subplot(gs[0,0])
        ax_1d_fnu = plt.subplot(gs[1,0])
        ax_1d_flam = plt.subplot(gs[2,0])
        ax_prof = plt.subplot(gs[0,1])

        # 2D spectrum with S/N calculation
        sci = hdul['SCI'].data
        wht = hdul['WHT'].data
        prof = hdul['PROF1D'].data
        
        nsci = sci * np.sqrt(wht)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=AstropyWarning)
            std = sigma_clipped_stats(nsci, sigma=3)[2]
        snr_2d = nsci / std

        # S/N range and colormap
        vmin, vmax = -3, 8
        cmap = plt.colormaps['viridis']
        cmap.set_bad('0.8')

        im = ax_2d.pcolormesh(wave, prof['pix'], snr_2d, 
                             vmin=vmin, vmax=vmax, cmap=cmap)
        ax_2d.set_ylabel('$y$ [pix]')
        ax_2d.set_ylim(-10, 10)
        ax_2d.minorticks_on()
        ax_2d.tick_params(direction='in', which='both', axis='y')
        
        # Dual 1D spectrum plots
        valid = np.isfinite(fnu) & np.isfinite(fnu_err)
        
        # f_ν plot
        ax_1d_fnu.step(wave, fnu, where='mid', color='k', linewidth=1)
        ax_1d_fnu.fill_between(wave, (fnu - fnu_err), (fnu + fnu_err), 
            alpha=0.15, color='k', step='mid')
        ax_1d_fnu.set_ylabel(r'$f_{\nu}$ [μJy]')
        
        # f_λ plot
        ax_1d_flam.step(wave, flam, where='mid', color='k', linewidth=1)
        ax_1d_flam.fill_between(wave, (flam - flam_err), (flam + flam_err), 
            alpha=0.15, color='k', step='mid')
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
        ax_prof.step(prof['profile'], prof['pix'], where='post', color='k')
        ax_prof.fill_betweenx(prof['pix'], np.zeros_like(prof['pix']), prof['pfit'], 
                             color='r', alpha=0.3, step='pre')
        ax_prof.set_ylim(-10, 10)
        ax_prof.minorticks_on()
        ax_prof.tick_params(labelbottom=False, bottom=False, labelleft=False,
                           direction='in', which='both')

        # Smart x and y limits
        xmin = wave.min()
        xmax = wave.max()
        ax_2d.set_xlim(xmin, xmax)
        ax_1d_fnu.set_xlim(xmin, xmax)
        ax_1d_flam.set_xlim(xmin, xmax)

        # Percentile-based y-limits
        ymax = np.nanpercentile(fnu+fnu_err, 97)
        ax_1d_fnu.set_ylim(-0.1*ymax, ymax)
        ymax = np.nanpercentile(flam+flam_err, 97)
        ax_1d_flam.set_ylim(-0.1*ymax, ymax)
        
        if title:
            fig.suptitle(title)
        
        # Save if output path provided
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, format='png', dpi=dpi, bbox_inches='tight')
        
        return True, fig, None
        
    except Exception as e:
        return False, None, f"Failed to generate 2D spectrum plot: {e}"


def create_redshift_fit_plot(hdul: fits.HDUList, zfit_data: Optional[Dict] = None,
                            output_path: Optional[Path] = None, title: Optional[str] = None,
                            figure_size: Tuple[int, int] = (10, 3), dpi: int = 200) -> Tuple[bool, Optional[Figure], Optional[str]]:
    """
    Generate redshift fitting plot with spectrum and chi-squared curve.
    
    Parameters:
    -----------
    hdul : fits.HDUList
        FITS HDU list containing spectrum data with SPEC1D extension
    zfit_data : Dict, optional
        Redshift fitting results with keys: 
        - 'best_fit_spectrum': model flux array
        - 'redshift_grid': redshift values 
        - 'chi2_curve': chi-squared values
        - 'redshift': best-fit redshift
        - 'chi2_min': minimum chi-squared
    output_path : Path, optional
        Path to save the plot
    title : str, optional
        Plot title
    figure_size : tuple
        Figure size (width, height) in inches
    dpi : int
        Figure DPI
        
    Returns:
    --------
    tuple : (success, figure, error_message)
        success: bool - Whether plot generation succeeded
        figure: Figure or None - Matplotlib figure object
        error_message: str or None - Error message if failed
    """
    try:
        # Extract spectrum
        if 'SPEC1D' not in hdul:
            return False, None, "No SPEC1D extension found"
        
        spec1d = hdul['SPEC1D'].data    
        wave = spec1d['wave']
        fnu = spec1d['flux']
        fnu_err = spec1d['err']
        valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)

        # Side-by-side horizontal layout
        fig = plt.figure(figsize=figure_size, constrained_layout=True, dpi=dpi)
        gs = mpl.gridspec.GridSpec(nrows=1, ncols=2, width_ratios=[3,1], figure=fig)

        ax_1d = plt.subplot(gs[0])
        ax_chi2 = plt.subplot(gs[1])

        # 1D spectrum with advanced styling
        ax_1d.step(wave, fnu, where='mid', color='k', linewidth=1)
        ax_1d.fill_between(wave, (fnu - fnu_err), (fnu + fnu_err), 
                           alpha=0.15, edgecolor='none', facecolor='k', step='mid')

        # Model plotting with white outline + salmon fill
        if zfit_data and 'best_fit_spectrum' in zfit_data:
            model_flux = zfit_data['best_fit_spectrum']
            ax_1d.plot(wave, model_flux, color='w', linewidth=4)  # White outline
            ax_1d.plot(wave, model_flux, color='salmon', linewidth=2)  # Salmon fill

        ax_1d.set_ylabel(r'$f_{\nu}$ [μJy]')
        ax_1d.set_xlabel('Observed Wavelength [μm]')

        # Advanced grid and tick styling
        ax_1d.grid(True, alpha=0.2, linewidth=1, zorder=-1000)
        ax_1d.minorticks_on()
        ax_1d.tick_params(direction='in', which='both')        
        xmin = wave.min()
        xmax = wave.max()
        ax_1d.set_xlim(xmin, xmax)

        # Chi2 plot
        if zfit_data and 'chi2_curve' in zfit_data and 'redshift_grid' in zfit_data:
            ax_chi2.plot(zfit_data['redshift_grid'], zfit_data['chi2_curve'], color='k')
            
            # Mark best fit if available
            if 'redshift' in zfit_data:
                best_z = zfit_data['redshift']
                best_chi2 = zfit_data.get('chi2_min', np.min(zfit_data['chi2_curve']))
                ax_chi2.axvline(best_z, color='r', linestyle='--', alpha=0.7)
                ax_chi2.plot(best_z, best_chi2, 'ro', markersize=6)
                
        ax_chi2.set_xlabel('Redshift')
        ax_chi2.set_ylabel(r'$\chi^2$')

        # Percentile-based y-limits
        ymax = np.nanpercentile(fnu+fnu_err, 97)
        ax_1d.set_ylim(-0.1*ymax, ymax)
        
        if title:
            fig.suptitle(title)
        
        # Save if output path provided
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, format='png', dpi=dpi, bbox_inches='tight')
        
        return True, fig, None
        
    except Exception as e:
        return False, None, f"Failed to generate redshift fit plot: {e}"


def create_thumbnail_plot(hdul: fits.HDUList, output_path: Optional[Path] = None,
                         figure_size: Tuple[int, int] = (4, 3), dpi: int = 200) -> Tuple[bool, Optional[Figure], Optional[str]]:
    """
    Generate thumbnail plot for table display.
    
    Parameters:
    -----------
    hdul : fits.HDUList
        FITS HDU list containing spectrum data with SPEC1D extension
    output_path : Path, optional
        Path to save the plot
    figure_size : tuple
        Figure size (width, height) in inches
    dpi : int
        Figure DPI
        
    Returns:
    --------
    tuple : (success, figure, error_message)
        success: bool - Whether plot generation succeeded
        figure: Figure or None - Matplotlib figure object
        error_message: str or None - Error message if failed
    """
    try:
        if 'SPEC1D' not in hdul:
            return False, None, "No SPEC1D extension found"
        
        spec1d = hdul['SPEC1D'].data    
        wave = spec1d['wave']
        fnu = spec1d['flux']
        fnu_err = spec1d['err']

        valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)
        
        if not np.any(valid):
            return False, None, "No valid data points"
                
        # Create clean thumbnail figure
        fig, ax = plt.subplots(figsize=figure_size, constrained_layout=True, dpi=dpi)

        # Step plot with proper line width
        ax.step(wave, fnu, where='mid', color='k', linewidth=1.1)
        
        # Clean thumbnail - no axis elements
        ax.axis('off')

        # Smart limits based on data
        xmin = wave.min()
        xmax = wave.max()
        ax.set_xlim(xmin, xmax)

        # Percentile-based y-limits
        ymax = np.nanpercentile(fnu+fnu_err, 99)
        ax.set_ylim(-0.1*ymax, ymax)
        
        # Save if output path provided
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, format='png', dpi=dpi, bbox_inches='tight')
        
        return True, fig, None
        
    except Exception as e:
        return False, None, f"Failed to generate thumbnail plot: {e}"


def extract_zfit_data(zfit_hdul: fits.HDUList) -> Optional[Dict]:
    """
    Extract redshift fitting data from zfit FITS file.
    
    Parameters:
    -----------
    zfit_hdul : fits.HDUList
        FITS HDU list containing MODEL and CHI2 extensions
        
    Returns:
    --------
    Dict or None : Extracted zfit data with keys:
        - 'best_fit_spectrum': model flux array
        - 'redshift_grid': redshift values
        - 'chi2_curve': chi-squared values  
        - 'redshift': best-fit redshift
        - 'chi2_min': minimum chi-squared
    """
    try:
        if 'MODEL' not in zfit_hdul or 'CHI2' not in zfit_hdul:
            return None
            
        # Extract model spectrum
        model_data = zfit_hdul['MODEL'].data
        best_fit_spectrum = model_data['fnu']
        
        # Extract chi-squared curve
        chi2_data = zfit_hdul['CHI2'].data
        redshift_grid = chi2_data['z']
        chi2_curve = chi2_data['chi2']
        
        # Find best redshift
        min_idx = np.argmin(chi2_curve)
        best_redshift = float(redshift_grid[min_idx])
        chi2_min = float(chi2_curve[min_idx])
        
        return {
            'best_fit_spectrum': best_fit_spectrum,
            'redshift_grid': redshift_grid,
            'chi2_curve': chi2_curve,
            'redshift': best_redshift,
            'chi2_min': chi2_min
        }
        
    except Exception as e:
        print(f"Failed to extract zfit data: {e}")
        return None


class PlottingEngine:
    """NIRSpec plotting engine for generating observation plots."""
    
    def __init__(self, config_path="config.toml"):
        """Initialize plotting engine with configuration."""
        self.config = load_config(config_path)
        
        # Set up paths from config with version substitution
        paths = self.config.get('paths', {})
        pipeline_config = self.config.get('pipeline', {})
        version = pipeline_config.get('version', 'unversioned')
        
        extractions_template = paths.get('extractions_dir', 'data/extractions')
        self.extractions_dir = os.path.abspath(extractions_template.replace('{version}', version))
        
        # Set up logging
        log_config = self.config.get('logging', {})
        log_level = log_config.get('level', 'INFO').upper()
        log_format = log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        logging.basicConfig(level=getattr(logging, log_level), format=log_format)
        self.logger = logging.getLogger('nirspec_plotting')
        
        self.logger.info(f"PlottingEngine initialized with extractions_dir: {self.extractions_dir}")

    def find_spectrum_files(self, observation, source_ids=None):
        """Find all spectrum files for a given observation, optionally filtered by source IDs."""
        obs_extractions_dir = os.path.join(self.extractions_dir, observation['name'])
        if not os.path.exists(obs_extractions_dir):
            self.logger.error(f"Observation extractions directory not found: {obs_extractions_dir}")
            return []
        
        spec_files = []
        
        # Look in each source subdirectory
        for source_dir in glob.glob(os.path.join(obs_extractions_dir, "*/")):
            if source_dir.endswith('_pipeline/'):
                continue  # Skip pipeline metadata
            
            source_id = os.path.basename(source_dir.rstrip('/'))
            
            # Filter by source IDs if specified
            if source_ids and source_id not in [str(sid) for sid in source_ids]:
                continue
                
            spec_files.extend(glob.glob(os.path.join(source_dir, "*_spec.fits")))
        
        spec_files.sort()  # Consistent ordering
        
        self.logger.info(f"Found {len(spec_files)} spectrum files in {obs_extractions_dir}")
        return spec_files

    def process_single_spectrum(self, spec_file, plot_types=['2d', 'thumbnail', 'zfit'], overwrite=False):
        """Process plots for a single spectrum file."""
        spec_path = Path(spec_file)
        base_name = spec_path.stem.replace('_spec', '')
        output_dir = spec_path.parent
        
        self.logger.info(f"Processing plots for {base_name}")
        
        plot_counts = {'success': 0, 'skip': 0, 'error': 0}
        
        try:
            with fits.open(spec_file) as hdul:
                # Generate 2D spectrum plot 
                if '2d' in plot_types:
                    output_path = output_dir / f"{base_name}_spec.png"
                    if overwrite or not output_path.exists():
                        success, fig, error = create_2d_spectrum_plot(
                            hdul, 
                            output_path,
                            title=f"2D Spectrum: {base_name}"
                        )
                        if success:
                            self.logger.debug(f"✓ Generated 2D spectrum plot: {output_path.name}")
                            plt.close(fig)
                            plot_counts['success'] += 1
                        else:
                            self.logger.error(f"✗ Failed to generate 2D spectrum plot: {error}")
                            plot_counts['error'] += 1
                    else:
                        self.logger.debug(f"↻ 2D spectrum plot exists: {output_path.name}")
                        plot_counts['skip'] += 1
                
                # Generate thumbnail (keep separate for pipeline use)
                if 'thumbnail' in plot_types:
                    output_path = output_dir / f"{base_name}_thumbnail.png"
                    if overwrite or not output_path.exists():
                        success, fig, error = create_thumbnail_plot(hdul, output_path)
                        if success:
                            self.logger.debug(f"✓ Generated thumbnail plot: {output_path.name}")
                            plt.close(fig)
                            plot_counts['success'] += 1
                        else:
                            self.logger.error(f"✗ Failed to generate thumbnail plot: {error}")
                            plot_counts['error'] += 1
                    else:
                        self.logger.debug(f"↻ Thumbnail plot exists: {output_path.name}")
                        plot_counts['skip'] += 1
                
                # Generate redshift fit plot if zfit file exists
                if 'zfit' in plot_types:
                    zfit_file = spec_file.replace('_spec.fits', '_zfit.fits')
                    output_path = output_dir / f"{base_name}_zfit.png"
                    
                    if Path(zfit_file).exists():
                        if overwrite or not output_path.exists():
                            with fits.open(zfit_file) as zfit_hdul:
                                zfit_data = extract_zfit_data(zfit_hdul)
                                success, fig, error = create_redshift_fit_plot(
                                    hdul,
                                    zfit_data,
                                    output_path,
                                    title=f"Redshift Fit: {base_name}"
                                )
                                if success:
                                    self.logger.debug(f"✓ Generated redshift fit plot: {output_path.name}")
                                    if zfit_data:
                                        self.logger.debug(f"  Best redshift: z = {zfit_data['redshift']:.4f}")
                                    plt.close(fig)
                                    plot_counts['success'] += 1
                                else:
                                    self.logger.error(f"✗ Failed to generate redshift fit plot: {error}")
                                    plot_counts['error'] += 1
                        else:
                            self.logger.debug(f"↻ Redshift fit plot exists: {output_path.name}")
                            plot_counts['skip'] += 1
                    else:
                        self.logger.debug(f"⊘ No zfit file found for {base_name}, skipping redshift plot")
                        # Don't count as error - this is expected when redshift fitting hasn't been implemented
        
        except Exception as e:
            self.logger.error(f"Error processing spectrum file {spec_file}: {e}")
            plot_counts['error'] += 1
        
        return plot_counts

    def plot_observation(self, observation, plot_types=['2d', 'thumbnail', 'zfit'], source_ids=None, overwrite=False):
        """Generate plots for all spectra in an observation, optionally filtered by source IDs."""
        self.logger.info(f"Starting plot generation for observation: {observation['name']}")
        if source_ids:
            self.logger.info(f"Limiting to source IDs: {source_ids}")
        self.logger.info(f"Plot types: {', '.join(plot_types)}")
        
        # Find spectrum files
        spec_files = self.find_spectrum_files(observation, source_ids)
        if not spec_files:
            self.logger.warning(f"No spectrum files found for observation {observation['name']}")
            return
        
        # Process each spectrum file
        total_counts = {'success': 0, 'skip': 0, 'error': 0}
        
        for spec_file in spec_files:
            plot_counts = self.process_single_spectrum(spec_file, plot_types, overwrite)
            for key in total_counts:
                total_counts[key] += plot_counts[key]
        
        # Summary
        total_plots = total_counts['success'] + total_counts['skip'] + total_counts['error']
        self.logger.info(f"Plot generation complete for {observation['name']}: "
                        f"{total_counts['success']}/{total_plots} generated "
                        f"({total_counts['skip']} skipped, {total_counts['error']} errors)")


def main():
    """Main function to run NIRSpec plotting."""
    parser = argparse.ArgumentParser(description='NIRSpec Plotting Script')
    parser.add_argument('--obs', type=str, required=True, 
                       help='Observation name from observations.toml')
    parser.add_argument('--config', type=str, default='config.toml',
                       help='Path to configuration file (default: config.toml)')
    parser.add_argument('--observations', type=str, default='observations.toml',
                       help='Path to observations file (default: observations.toml)')
    parser.add_argument('--plots', nargs='+', choices=['2d', 'thumbnail', 'zfit'], 
                       default=['2d', 'thumbnail', 'zfit'],
                       help='Plot types to generate (default: all)')
    parser.add_argument('--sources', nargs='+', type=str,
                       help='Specific source IDs to plot (default: all sources)')
    parser.add_argument('--overwrite', action='store_true',
                       help='Overwrite existing plot files')
    parser.add_argument('--version', type=str,
                       help='Pipeline version (maps to config_<version>.toml if --config not specified)')
    
    args = parser.parse_args()
    
    # Handle version argument - if version specified but not config, map to config file
    if args.version and args.config == 'config.toml':  # Default config not overridden
        args.config = f'config_{args.version}.toml'
        print(f"Using version {args.version} -> {args.config}")
    
    try:
        # Load configurations
        observations = load_observations(args.observations)
        observation_config = get_observation_config(args.obs, observations)
        
        # Initialize plotting engine
        engine = PlottingEngine(args.config)
        
        # Generate plots
        engine.plot_observation(observation_config, args.plots, args.sources, args.overwrite)
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())