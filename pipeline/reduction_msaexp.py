"""
NIRSpec reduction

Usage:
python reduction.py --obs capers_uds_p2
"""

import os
import sys

# Check if we should disable threading BEFORE importing any numerical libraries
# This can be controlled by setting NIRSPEC_DISABLE_THREADING=1 in environment
if os.environ.get('NIRSPEC_DISABLE_THREADING', '0') == '1':
    print("Disabling internal threading (NIRSPEC_DISABLE_THREADING=1)")
    threading_vars = {
        'OPENBLAS_NUM_THREADS': '1',
        'MKL_NUM_THREADS': '1', 
        'NUMEXPR_NUM_THREADS': '1',
        'OMP_NUM_THREADS': '1',
        'VECLIB_MAXIMUM_THREADS': '1',  # macOS Accelerate
        'NUMBA_NUM_THREADS': '1',       # Numba
        'BLAS_NUM_THREADS': '1',        # Generic BLAS
        'LAPACK_NUM_THREADS': '1',      # LAPACK
    }
    
    for var, value in threading_vars.items():
        os.environ[var] = value

import glob
import warnings
import numpy as np
import matplotlib.pyplot as plt
from astropy.io import fits
from pathlib import Path
import toml
import logging
import argparse
import warnings
import shutil
import functools
from asdf.exceptions import AsdfConversionWarning
warnings.simplefilter('ignore', category=AsdfConversionWarning)

try:
    import msaexp
    from msaexp import slit_combine
    import jwst
    MSAEXP_AVAILABLE = True
except ImportError:
    MSAEXP_AVAILABLE = False
    msaexp = None
    slit_combine = None
    jwst = None

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

BAD_PIXEL_NAMES = [
    "DO_NOT_USE",
    "MSA_FAILED_OPEN",
    "HOT",
    "DEAD",
    "OPEN",
    "ADJ_OPEN",
    "SATURATED",
]

DEFAULT_DRIZZLE_KWARGS = dict(
    step=1,
    with_pathloss=True,
    wave_sample=1.05,
    ny=15,
    dkws=dict(oversample=16, pixfrac=0.8),
    grating_limits=GRATING_LIMITS,
)

DEFAULT_FIT_PARAMS_KWARGS = dict(
    sn_percentile=95,
    sigma_threshold=0,
    degree_sn=[[-10000], [0]],
    verbose=True,
)

DEFAULT_EXTENDED_CALIBRATION_KWARGS = {
    "threshold": 0.00,
    "fixed_slit_correction": 1,
    "quadrant_correction": True,
}

DEFAULT_FLAG_PERCENTILE_KWARGS = dict(
    plevels=[0.95, -4, -0.1],
    yslit=[-2, 2],
    scale=2.0,
    dilate=[[1, 1, 1], [1, 1, 1], [1, 1, 1]], 
)


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


def silence_jwst_logging():
    """Simple function to silence JWST pipeline logging."""
    jwst_loggers = [
        'jwst',
        'jwst.stpipe', 
        'jwst.pipeline',
        'stpipe',
        'crds'
    ]
    for logger_name in jwst_loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.WARNING)


def process_single_rate_file(rate_file_path, workspace_dir, source_ids, observation_name, log_level='INFO', allow_threading=False, primary_sources=True):
    """
    Process a single rate file through the msaexp pipeline.
    
    This function is designed to work with both sequential and parallel processing.
    
    Parameters:
    -----------
    rate_file_path : str
        Path to the rate file to process
    workspace_dir : str
        Workspace directory containing the files
    source_ids : list or None
        Source IDs to process, or None for all
    observation_name : str
        Name of observation for logging
    log_level : str
        Logging level for this worker
    allow_threading : bool
        Whether to allow internal threading in numerical libraries
        
    Returns:
    --------
    tuple : (success: bool, visit_sca: str, error_message: str)
    """
    # Limit threading to prevent oversubscription when multiprocessing
    if not allow_threading:
        # Set environment variables (must be done before importing libraries)
        threading_vars = {
            'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1', 
            'NUMEXPR_NUM_THREADS': '1',
            'OMP_NUM_THREADS': '1',
            'VECLIB_MAXIMUM_THREADS': '1',  # macOS Accelerate
            'NUMBA_NUM_THREADS': '1',       # Numba
            'BLAS_NUM_THREADS': '1',        # Generic BLAS
            'LAPACK_NUM_THREADS': '1',      # LAPACK
        }
        
        for var, value in threading_vars.items():
            os.environ[var] = value
        
        # Try to limit threading in already-imported libraries
        try:
            import numpy as np
            # Some numpy builds support runtime thread limiting
            if hasattr(np, 'seterr'):
                # This is a workaround - numpy doesn't have direct thread control
                pass
        except ImportError:
            pass
            
        try:
            # If using threadpoolctl (common in scientific Python)
            import threadpoolctl
            threadpoolctl.threadpool_limits(limits=1)
        except ImportError:
            pass
    # Extract basename and visit_sca identifier first
    rate_file_base = os.path.basename(rate_file_path)
    visit_sca = rate_file_base.replace('_rate.fits', '') + '_msaexp'
    
    # Set up independent logging for this worker (similar to ReductionEngine approach)
    logger = logging.getLogger(f'process_single_rate_file.{visit_sca}')
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # Prevent propagation to root logger (which STPIPE might control)
    logger.propagate = False
    
    # Clear any existing handlers and add our own
    logger.handlers.clear()
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper()))
    
    # Use same format as ReductionEngine
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    logger.info(f'Working on preprocessing for visit/sca {visit_sca}, observation {observation_name}')
    
    # Handle directory changes
    prev_cwd = os.getcwd()
    
    try:
        os.chdir(workspace_dir)
        
        # Import msaexp pipeline
        from msaexp import pipeline
        
        logger.info(f"Creating NirspecPipeline for {visit_sca}")
        
        # Use basename since we're in the workspace directory
        pipe = pipeline.NirspecPipeline(
            mode=visit_sca, 
            files=[rate_file_base],
            source_ids=source_ids,
            primary_sources=primary_sources,
        )
        logger.info(f'primary_sources: {primary_sources}')

        logger.info("Running full pipeline")
        pipe.full_pipeline(
            run_extractions=False,
            initialize_bkg=False,
            load_saved=None,
            scale_rnoise=True,
        )
        
        logger.info(f"Completed preprocessing for {visit_sca}")
        return (True, visit_sca, "")
                            
    except Exception as e:
        error_msg = f"Failed preprocessing for {visit_sca}: {e}"
        logger.error(error_msg)
        return (False, visit_sca, str(e))
    
    finally:
        # Always restore working directory
        os.chdir(prev_cwd)




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
        
        # Set up independent logger that coexists with STPIPE
        self.logger = self.setup_independent_logger()

        # Set up environment variables (especially CRDS settings)
        self.setup_environment()
        
        # Set up directories from config - make them absolute paths for multiprocessing
        paths = self.config.get('paths', {})
        
        # Get version from config and substitute in paths
        pipeline_config = self.config.get('pipeline', {})
        version = pipeline_config.get('version', 'unversioned')
        self.version = version
        
        # New directory structure with version substitution
        self.raw_dir = os.path.abspath(paths.get('raw_dir', 'data/raw'))
        preprocessed_template = paths.get('preprocessed_dir', 'data/preprocessed')
        extractions_template = paths.get('extractions_dir', 'data/extractions')
        
        # # Substitute {version} placeholder
        self.preprocessed_dir = os.path.abspath(preprocessed_template)#.replace('{version}', version))
        self.extractions_dir = os.path.abspath(extractions_template)#.replace('{version}', version))
        
        # Create base directories if they don't exist
        os.makedirs(self.raw_dir, exist_ok=True)
        os.makedirs(self.preprocessed_dir, exist_ok=True)
        os.makedirs(self.extractions_dir, exist_ok=True)
        

        # Store observation configs by name for easy lookup
        self.observations = {}
        
        self.logger.info("Initialized ReductionEngine")
        
        # Configure msaexp at class level to prevent first-source capture issues
        if MSAEXP_AVAILABLE:
            try:
                self.logger.info("Configuring msaexp settings")
                # silence_jwst_logging()
                
                # Configure bad pixel flags
                slit_combine.BAD_PIXEL_FLAG = 1 | 1024
                for _bp in BAD_PIXEL_NAMES:
                    slit_combine.BAD_PIXEL_FLAG |= jwst.datamodels.dqflags.pixel[_bp]
                
                # Configure sflat straightening
                sflat_straighten = 3
                msaexp.utils.SFLAT_STRAIGHTEN = sflat_straighten
                
                self.logger.info("msaexp configuration completed")
            except Exception as e:
                self.logger.warning(f"Failed to configure msaexp: {e}")
        else:
            self.logger.warning("msaexp not available - spectrum extraction will fail")
    
    def copy_config_to_pipeline(self, pipeline_dir):
        """Copy the configuration file used for this reduction to the pipeline directory."""
        import shutil
        
        config_dest = os.path.join(pipeline_dir, 'config_used.toml')
        try:
            shutil.copy2(self.config_path, config_dest)
            self.logger.info(f"Copied config file to {config_dest}")
        except Exception as e:
            self.logger.warning(f"Failed to copy config file: {e}")
    
    def setup_independent_logger(self):
        """
        Set up independent logger that coexists with STPIPE/JWST logging.
        
        Returns:
        --------
        logging.Logger : Configured logger instance
        """
        # Get logging configuration from config file
        log_config = self.config.get('logging', {})
        log_level = log_config.get('level', 'INFO').upper()
        log_format = log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        # Create logger with unique name to avoid conflicts
        logger = logging.getLogger('nirspec_reduction')
        logger.setLevel(getattr(logging, log_level))
        
        # Prevent propagation to root logger (which STPIPE might control)
        logger.propagate = False
        
        # Clear any existing handlers
        logger.handlers.clear()
        
        # Create console handler with our custom formatter
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level))
        
        # Set our custom formatter
        formatter = logging.Formatter(log_format)
        console_handler.setFormatter(formatter)
        
        # Add handler to logger
        logger.addHandler(console_handler)
        
        return logger
    
    def reinforce_logger(self):
        """
        Reinforce our logger setup after STPIPE operations that might interfere.
        Call this after importing/using STPIPE components.
        """
        # Re-ensure our logger configuration is active
        if hasattr(self, 'logger'):
            # Make sure our logger is still independent
            self.logger.propagate = False
            # Add a test message to verify logger is working
            self.logger.debug("Logger configuration reinforced after STPIPE operations")
    
    def setup_environment(self):
        """Set environment variables from config file."""
        if 'environment' in self.config:
            env = self.config['environment']
            for key, value in env.items():
                os.environ[key] = str(value)
                self.logger.info(f"Set environment variable {key} = {value}")
    
    def setup_observation_workspace(self, observation, overwrite=False):
        """
        Create and populate preprocessing workspace for an observation.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
        overwrite : bool
            Whether to overwrite existing workspace
            
        Returns:
        --------
        str : Path to the workspace directory
        """
        obs_name = observation['name']
        workspace_dir = os.path.join(self.preprocessed_dir, obs_name, self.version)
        
        # Create workspace directory
        if os.path.exists(workspace_dir) and overwrite:
            self.logger.info(f"Removing existing workspace: {workspace_dir}")
            shutil.rmtree(workspace_dir)
        
        os.makedirs(workspace_dir, exist_ok=True)
        self.logger.info(f"Created workspace directory: {workspace_dir}")
        
        # Copy rate files from raw directory using data_subdir
        data_subdir = observation.get('data_subdir')
        if data_subdir:
            raw_subdir = os.path.join(self.raw_dir, data_subdir)
            if os.path.exists(raw_subdir):
                self.copy_rate_files(observation, raw_subdir, workspace_dir)
            else:
                self.logger.warning(f"Raw data directory not found: {raw_subdir}")
        else:
            self.logger.warning(f"No data_subdir specified for observation {obs_name}")
        
        return workspace_dir
    
    def copy_rate_files(self, observation, raw_subdir, workspace_dir):
        """
        Copy required rate files and associated MSA meta files from raw directory to workspace.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
        raw_subdir : str
            Source directory containing raw rate files
        workspace_dir : str
            Destination workspace directory
        """
        # Find rate files needed for this observation
        rate_files_to_copy = []
        if isinstance(observation['files'], list):
            for file_pattern in observation['files']:
                pattern_path = os.path.join(raw_subdir, file_pattern + '_rate.fits')
                rate_files_to_copy.extend(glob.glob(pattern_path))
        else:
            pattern_path = os.path.join(raw_subdir, observation['files'] + '_rate.fits')
            rate_files_to_copy.extend(glob.glob(pattern_path))
        
        # Copy rate files to workspace and track MSA meta files needed
        copied_files = []
        msa_meta_files_needed = set()
        
        for src_file in rate_files_to_copy:
            dst_file = os.path.join(workspace_dir, os.path.basename(src_file))
            if not os.path.exists(dst_file):
                self.logger.debug(f"Copying {os.path.basename(src_file)} to workspace")
                shutil.copy2(src_file, dst_file)
            copied_files.append(dst_file)
            
            # Read MSAMETFL header from rate file to find associated MSA meta file
            try:
                with fits.open(src_file) as hdul:
                    msametfl = hdul[0].header.get('MSAMETFL', '')
                    if msametfl:
                        # MSAMETFL contains the basename of the MSA meta file
                        msa_meta_files_needed.add(msametfl)
                        self.logger.debug(f"Rate file {os.path.basename(src_file)} requires MSA meta file: {msametfl}")
                    else:
                        self.logger.warning(f"No MSAMETFL header found in {os.path.basename(src_file)}")
            except Exception as e:
                self.logger.warning(f"Could not read MSAMETFL header from {os.path.basename(src_file)}: {e}")
        
        # Copy MSA meta files to workspace
        msa_files_copied = 0
        msa_files_already_exist = 0
        msa_files_missing = 0
        
        self.logger.info(f"Found {len(msa_meta_files_needed)} unique MSA meta files needed: {list(msa_meta_files_needed)}")
        
        for msa_filename in msa_meta_files_needed:
            src_msa_file = os.path.join(raw_subdir, msa_filename)
            dst_msa_file = os.path.join(workspace_dir, msa_filename)
            
            if os.path.exists(src_msa_file):
                if not os.path.exists(dst_msa_file):
                    self.logger.debug(f"Copying MSA meta file {msa_filename} to workspace")
                    shutil.copy2(src_msa_file, dst_msa_file)
                    msa_files_copied += 1
                else:
                    self.logger.debug(f"MSA meta file {msa_filename} already exists in workspace")
                    msa_files_already_exist += 1
            else:
                self.logger.warning(f"MSA meta file not found: {src_msa_file}")
                msa_files_missing += 1
        
        total_msa_files = msa_files_copied + msa_files_already_exist
        self.logger.info(f"Copied {len(copied_files)} rate files and {total_msa_files} MSA meta files to workspace")
        return copied_files
    
    # def cleanup_workspace(self, observation):
    #     """
    #     Clean up preprocessing workspace for an observation.
    #     (not called within the pipeline, just provided as a utility )
        
    #     Parameters:
    #     -----------
    #     observation : dict
    #         Observation configuration dictionary
    #     """
    #     obs_name = observation['name']
    #     workspace_dir = os.path.join(self.preprocessed_dir, obs_name)
        
    #     if os.path.exists(workspace_dir):
    #         shutil.rmtree(workspace_dir)
    #         self.logger.info(f"Cleaned up workspace: {workspace_dir}")
    
    def check_preprocessing_completed(self, observation):
        """
        Check if preprocessing has been completed for an observation by examining output files.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
            
        Returns:
        --------
        bool : True if preprocessing appears complete, False otherwise
        """
        obs_name = observation['name']
        workspace_dir = os.path.join(self.preprocessed_dir, obs_name, self.version)
        print(workspace_dir)
        
        if not os.path.exists(workspace_dir):
            self.logger.debug(f"Preprocessing workspace not found: {workspace_dir}")
            return False
        
        # Check if we have the rate files in the workspace
        rate_files_found = []
        if isinstance(observation['files'], list):
            for file_pattern in observation['files']:
                pattern_path = os.path.join(workspace_dir, file_pattern + '_rate.fits')
                rate_files_found.extend(glob.glob(pattern_path))
        else:
            pattern_path = os.path.join(workspace_dir, observation['files'] + '_rate.fits')
            rate_files_found.extend(glob.glob(pattern_path))
        
        if not rate_files_found:
            self.logger.debug(f"No rate files found in workspace: {workspace_dir}")
            return False
        
        # Check if we have corresponding slits.yaml files (indicates msaexp pipeline ran)
        slit_files_found = 0
        phot_files_found = 0
        
        for rate_file in rate_files_found:
            rate_file_base = os.path.basename(rate_file)
            visit_sca = rate_file_base.replace('_rate.fits', '') + '_msaexp'
            
            # Check for slits.yaml file
            slit_file = os.path.join(workspace_dir, f'{visit_sca}.slits.yaml')
            if os.path.exists(slit_file):
                slit_files_found += 1
            
            # Check for any phot files from this rate file
            phot_pattern = rate_file_base.replace('_rate.fits', '_phot*.fits')
            phot_files = glob.glob(os.path.join(workspace_dir, phot_pattern))
            phot_files_found += len(phot_files)
        
        preprocessing_complete = (slit_files_found > 0 and phot_files_found > 0)
        
        if preprocessing_complete:
            self.logger.info(f"Detected completed preprocessing: {slit_files_found} slit files, {phot_files_found} phot files")
            
            # Update the observation object to include workspace info and mark as preprocessed
            observation['workspace_dir'] = workspace_dir
            observation['rate_files'] = rate_files_found
            observation['preprocessed'] = True
            
            # Parse source IDs from phot files if not already set
            if observation['source_ids'] == 'all':
                all_source_ids = []
                for rate_file in rate_files_found:
                    rate_file_base = os.path.basename(rate_file)
                    photfiles = glob.glob(os.path.join(workspace_dir, rate_file_base.replace('_rate.fits', '_phot*.fits')))
                    for photfile in photfiles:
                        i = os.path.basename(photfile).split('_')[-1].replace('.fits', '')
                        if not ('m' in i or 'b' in i or 'BKG' in i):
                            all_source_ids.append(int(i))
                
                all_source_ids = sorted(list(set(all_source_ids)))
                observation['source_ids'] = all_source_ids
                self.logger.info(f"Found {len(all_source_ids)} source IDs from preprocessing: {all_source_ids}")
        else:
            self.logger.debug(f"Preprocessing appears incomplete: {slit_files_found} slit files, {phot_files_found} phot files")
        
        return preprocessing_complete
    
    # Note: _check_preprocessing_needed method removed - logic moved to preprocess_observation 
    # to determine work upfront for better multiprocessing compatibility
    
    def preprocess_observation(self, observation, overwrite=False, n_processes=1, allow_threading=False):
        """
        Run MSA preprocessing pipeline using new workspace structure.

        Runs msaexp.pipeline.NirspecPipeline, which effectively just runs the JWST Spec2Pipeline.
        Supports both sequential and parallel processing.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
        overwrite : bool
            Whether to overwrite existing files
        n_processes : int
            Number of processes for multiprocessing. If 1, runs sequentially.
            If > 1, uses multiprocessing.Pool for parallel processing.
        allow_threading : bool
            Whether to allow internal threading in numerical libraries
        """

        # Store observation in engine for tracking
        self.observations[observation['name']] = observation
        
        # Create workspace and copy rate files
        workspace_dir = self.setup_observation_workspace(observation, overwrite)
        
        # Find rate files in workspace
        rate_files = []
        if isinstance(observation['files'], list):
            for file_pattern in observation['files']:
                pattern_path = os.path.join(workspace_dir, file_pattern + '_rate.fits')
                rate_files.extend(glob.glob(pattern_path))
        else:
            pattern_path = os.path.join(workspace_dir, observation['files'] + '_rate.fits')
            rate_files.extend(glob.glob(pattern_path))
        
        observation['rate_files'] = rate_files
        observation['workspace_dir'] = workspace_dir

        # Parse any source ID specification for the observation
        if observation['source_ids'] == 'all':
            source_ids = None
        else:
            source_ids = observation['source_ids']

        primary_sources = self.config['preprocessing'].get('primary_sources', True)
        if primary_sources:
            self.logger.info("Processing only PRIMARY sources (primary_sources = True)")
        else:
            self.logger.info("Processing all sources (primary_sources = False)")

        # Determine which rate files actually need preprocessing
        rate_files_to_process = []
        for rate_file in rate_files:
            rate_file_base = os.path.basename(rate_file)
            visit_sca = rate_file_base.replace('_rate.fits', '') + '_msaexp'
            slit_file = os.path.join(workspace_dir, f'{visit_sca}.slits.yaml')
            
            if not os.path.exists(slit_file) or overwrite:
                rate_files_to_process.append(rate_file)

        if len(rate_files_to_process)==0:
            self.logger.info(f"Skipping preprocessing for {observation['name']} - all files exist and overwrite=False")
            return  # Early return if no work to do
            
        self.logger.info(f"Starting preprocessing for {observation['name']} - processing {len(rate_files_to_process)}/{len(rate_files)} rate files")

        from msaexp import pipeline
        
        # Reinforce our logger setup after importing STPIPE components
        self.reinforce_logger()

        # Process rate files - choose sequential or parallel processing
        log_level = self.config.get('logging', {}).get('level', 'INFO')
        
        if n_processes == 1:
            # Sequential processing
            self.logger.info("Processing rate files sequentially")
            for rate_file in rate_files_to_process:
                success, visit_sca, error = process_single_rate_file(
                    rate_file, workspace_dir, source_ids, observation['name'], log_level, allow_threading=allow_threading, primary_sources=primary_sources
                )
                if not success:
                    self.logger.error(f"Failed preprocessing {visit_sca}: {error}")
                    raise Exception(error)
        else:
            # Parallel processing
            import multiprocessing
            self.logger.info(f"Processing rate files in parallel with {n_processes} processes")
            
            # Set threading limits at main process level if not allowing threading
            if not allow_threading:
                self.logger.info("Disabling internal threading to prevent oversubscription")
                threading_vars = {
                    'OPENBLAS_NUM_THREADS': '1',
                    'MKL_NUM_THREADS': '1', 
                    'NUMEXPR_NUM_THREADS': '1',
                    'OMP_NUM_THREADS': '1',
                    'VECLIB_MAXIMUM_THREADS': '1',  # macOS Accelerate
                    'NUMBA_NUM_THREADS': '1',       # Numba
                    'BLAS_NUM_THREADS': '1',        # Generic BLAS
                    'LAPACK_NUM_THREADS': '1',      # LAPACK
                }
                
                for var, value in threading_vars.items():
                    os.environ[var] = value
                    self.logger.debug(f"Set {var} = {value}")
            
            # Create partial function with fixed arguments
            worker_func = functools.partial(
                process_single_rate_file,
                workspace_dir=workspace_dir,
                source_ids=source_ids, 
                observation_name=observation['name'],
                log_level=log_level,
                allow_threading=allow_threading,
                primary_sources=primary_sources
            )
            
            with multiprocessing.Pool(n_processes) as pool:
                # Now we only need to pass the rate_file argument to each worker
                results = pool.map(worker_func, rate_files_to_process)
                
                # Handle any worker failures
                failed_count = 0
                for success, visit_sca, error in results:
                    if not success:
                        self.logger.error(f"Failed preprocessing {visit_sca}: {error}")
                        failed_count += 1
                
                if failed_count > 0:
                    raise Exception(f"{failed_count} rate files failed preprocessing")
                    
                self.logger.info(f"Successfully completed parallel preprocessing of {len(results)} rate files")

        
        # Parse the list of source IDs that were successfully pre-processed
        all_source_ids = []
        for rate_file in rate_files:
            # Search for phot files in workspace directory
            rate_file_base = os.path.basename(rate_file)
            photfiles = glob.glob(os.path.join(workspace_dir, rate_file_base.replace('_rate.fits', '_phot*.fits')))
            ids = []
            for photfile in photfiles:
                i = os.path.basename(photfile).split('_')[-1].replace('.fits', '')
                if not ('m' in i or 'b' in i or 'BKG' in i):
                    ids.append(int(i))
            all_source_ids += ids
        
        all_source_ids = sorted(list(set(all_source_ids)))
        if source_ids is not None:
            all_source_ids = [i for i in all_source_ids if i in source_ids]
        self.logger.info(f'Preprocessing yields {len(all_source_ids)} objects for extraction')

        # Update source IDs in observation object
        if observation['source_ids'] == 'all':
            observation['source_ids'] = all_source_ids

        # Set preprocessed flag to True, so future steps know preprocessing was successful
        observation['preprocessed'] = True
        

    def get_visit_groups(self, observation, source_id, logger=None):
        """
        Group visit files for processing.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
        source_id : int
            Source ID to process
            
        Returns:
        --------
        dict : Dictionary of grouped files by visit/grating
        """
        if logger is None:
            logger = self.logger
        
        logger.info('Splitting visit groups')

        gratings = observation['gratings']
        rate_files = observation['rate_files']
        
        try:
            groups = {}

            for rate_file in rate_files:
                # rate_file is now absolute path, extract basename and search in workspace directory
                rate_file_base = os.path.basename(rate_file)
                phot_pattern = rate_file_base.replace('_rate.fits', f'_phot*_{source_id}.fits')
                workspace_dir = observation.get('workspace_dir', self.output_dir)  # fallback to legacy
                phot_files = glob.glob(os.path.join(workspace_dir, phot_pattern))

                for phot_file in phot_files:
                    try:
                        with fits.open(phot_file) as im:
                            grating = im[0].header["GRATING"]
                            filt = im[0].header["FILTER"]
                            if im[0].header["GRATING"] not in gratings:
                                continue
                        
                        # jw06585004001_07101_00001_nrs1_phot.516.6585_6.fits
                        # jw<ppppp><ooo><vvv>_<gg><s><aa>_<eeeee>(-<”seg”NNN>)_<detector
                        name = os.path.basename(phot_file)
                        pov = name.split('_')[0]
                        gsa = name.split('_')[1]
                        det = name.split('_')[3]
                        key = f'{pov}_{gsa}_{det}_{grating}_{filt}'.lower()
                        if key in groups:
                            groups[key].append(phot_file)
                        else:
                            groups[key] = [phot_file]
                                
                    except Exception as e:
                        logger.warning(f"Failed to process file {phot_file}: {e}")
                        continue

            # Sort files within each group
            for key in list(groups.keys()):
                n = len(groups[key])
                if n > 3:
                    logger.warning(f'Visit group {key} has too many files ({n}, should be <=3)')
                    del groups[key]
                    continue

                groups[key] = sorted(groups[key])
                logger.info(f"Group {key}: {groups[key]}")
                                    
            logger.info(f"Created {len(groups)} visit groups")
            
            return groups
            
        except Exception as e:
            logger.error(f"Failed to create visit groups: {e}")
            raise
    
    def create_pipeline_convenience_structure(self, observation, obs_extractions_dir):
        """Create symlinks and metadata for pipeline operations."""
        import glob
        
        pipeline_dir = os.path.join(obs_extractions_dir, '_pipeline')
        os.makedirs(pipeline_dir, exist_ok=True)
        
        # Copy config file used for this reduction
        self.copy_config_to_pipeline(pipeline_dir)
        
        # Create all_spectra symlink directory for batch operations
        all_spectra_dir = os.path.join(pipeline_dir, 'all_spectra')
        os.makedirs(all_spectra_dir, exist_ok=True)
        
        # Create symlinks to all spectrum files
        for source_dir in glob.glob(os.path.join(obs_extractions_dir, "*/")):
            if source_dir.endswith('_pipeline/'):
                continue  # Skip pipeline metadata directory
                
            for spec_file in glob.glob(os.path.join(source_dir, "*_spec.fits")):
                link_name = os.path.join(all_spectra_dir, os.path.basename(spec_file))
                if not os.path.exists(link_name):
                    try:
                        os.symlink(os.path.relpath(spec_file, all_spectra_dir), link_name)
                        self.logger.debug(f"Created symlink: {link_name}")
                    except OSError as e:
                        self.logger.warning(f"Failed to create symlink {link_name}: {e}")
        
        self.logger.info(f"Pipeline convenience structure created in {pipeline_dir}")
    
    def extract_spectra_for_observation(self, observation, overwrite=False, n_processes=1, allow_threading=False):
        """
        Perform spectral extractions for a single observation.
        Supports both sequential and parallel processing.
        
        Parameters:
        -----------
        observation : dict
            Observation configuration dictionary
        overwrite : bool
            Whether to overwrite existing files
        n_processes : int
            Number of processes for multiprocessing. If 1, runs sequentially.
            If > 1, uses multiprocessing.Pool for parallel processing.
        allow_threading : bool
            Whether to allow internal threading in numerical libraries
        """
        self.logger.info(f'Starting extractions for {observation["name"]}')

        # Check if preprocessing is complete (either in memory or by file inspection)
        if not observation.get('preprocessed', False):
            self.logger.info("Observation not marked as preprocessed in memory, checking files...")
            if not self.check_preprocessing_completed(observation):
                self.logger.error(f"Could not extract spectra for observation {observation['name']}, not yet pre-processed!")
                raise Exception(f"Observation {observation['name']} not yet pre-processed")
            else:
                self.logger.info("Preprocessing completion detected from files")

        self.logger.info(f'Starting extractions for {observation["name"]} - processing {len(observation["source_ids"])} sources')

        # Create observation-specific extractions directory
        obs_extractions_dir = os.path.join(self.extractions_dir, observation['name'])
        os.makedirs(obs_extractions_dir, exist_ok=True)
        self.logger.info(f"Extractions will be saved to: {obs_extractions_dir}")

        # Initialize summary tracking
        summary_data = []
        config_extractions = self.config.get('extractions', {})
        write_summary = config_extractions.get('write_summary_file', True)
        
        if write_summary:
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            # Store summary in pipeline directory
            pipeline_dir = os.path.join(obs_extractions_dir, '_pipeline')
            os.makedirs(pipeline_dir, exist_ok=True)
            summary_file = f"{pipeline_dir}/extraction_summary_{observation['name']}_{self.version}_{timestamp}.csv"
            self.logger.info(f"Summary will be written to: {summary_file}")

        # Process sources (sequential or parallel based on n_processes) 
        # Both use same worker function - let worker determine if work needed
        successful_count = 0
        already_exist_count = 0
        skipped_count = 0
        failed_count = 0
        
        if n_processes == 1:
            # Sequential processing using same worker function
            for source_id in observation['source_ids']:
                self.logger.info(f'Working on extractions for {observation["name"]} MSA ID {source_id}')

                try:
                    result = extract_single_source(
                        source_id=source_id,
                        observation_dict=observation.copy(),
                        config_dict=self.config.copy(),
                        extractions_dir=obs_extractions_dir,
                        version=self.version,
                        overwrite=overwrite,
                        log_level=self.config.get('logging', {}).get('level', 'INFO').upper(),
                        allow_threading=allow_threading
                    )
                    
                    # Process result and update counts
                    status = categorize_status(result)
                    if status == 'extracted':
                        successful_count += 1
                    elif status == 'already_exist':
                        already_exist_count += 1
                    elif status == 'skipped':
                        skipped_count += 1
                    elif status == 'failed':
                        failed_count += 1
                    
                    # Add to summary tracking
                    if write_summary:
                        from datetime import datetime
                        result_row = {
                            'observation_name': observation['name'],
                            'source_id': result['source_id'],
                            'status': status,
                            'message': result['message'],
                            'initial_groups_found': result['groups_tracking']['initial_groups_found'],
                            'groups_after_validation': result['groups_tracking']['groups_after_validation'],
                            'groups_skipped_reasons': format_skip_reasons(
                                result['groups_tracking']['groups_skipped_validation'] + 
                                result['groups_tracking']['groups_skipped_processing']
                            ),
                            'valid_groups_after_checks': result['groups_tracking']['valid_groups_final'],
                            'spectrum_files_created': result['spectrum_files_created'],
                            'output_files': result['output_files'],
                            'processing_time_seconds': result['processing_time'],
                            'timestamp': datetime.now().isoformat()
                        }
                        summary_data.append(result_row)
                    
                    if result['success']:
                        self.logger.info(f"Completed processing for source {source_id}: {result['message']}")
                    else:
                        self.logger.error(f"Failed to process source {source_id}: {result['message']}")
            
                except Exception as e:
                    failed_count += 1
                    self.logger.error(f"Failed to process source {source_id}: {e}")
        else:
            # Parallel processing
            import multiprocessing 
            
            self.logger.info(f"Starting parallel extraction with {n_processes} processes")
            
            # Get logging configuration
            log_config = self.config.get('logging', {})
            log_level = log_config.get('level', 'INFO').upper()
            
            # Create worker partial function with fixed arguments
            worker_func = functools.partial(
                extract_single_source,
                observation_dict=observation.copy(),
                config_dict=self.config.copy(),
                extractions_dir=obs_extractions_dir,
                version=self.version,
                overwrite=overwrite,
                log_level=log_level,
                allow_threading=allow_threading
            )
            
            try:
                with multiprocessing.Pool(n_processes) as pool:
                    # Process all sources in parallel
                    results = pool.map(worker_func, observation['source_ids'])
                    
                    # Handle worker results
                    for result in results:
                        # Process result and update counts
                        status = categorize_status(result)
                        if status == 'extracted':
                            successful_count += 1
                        elif status == 'already_exist':
                            already_exist_count += 1
                        elif status == 'skipped':
                            skipped_count += 1
                        elif status == 'failed':
                            failed_count += 1
                        
                        # Add to summary tracking
                        if write_summary:
                            from datetime import datetime
                            result_row = {
                                'observation_name': observation['name'],
                                'source_id': result['source_id'],
                                'status': status,
                                'message': result['message'],
                                'initial_groups_found': result['groups_tracking']['initial_groups_found'],
                                'groups_after_validation': result['groups_tracking']['groups_after_validation'],
                                'groups_skipped_reasons': format_skip_reasons(
                                    result['groups_tracking']['groups_skipped_validation'] + 
                                    result['groups_tracking']['groups_skipped_processing']
                                ),
                                'valid_groups_after_checks': result['groups_tracking']['valid_groups_final'],
                                'spectrum_files_created': result['spectrum_files_created'],
                                'output_files': result['output_files'],
                                'processing_time_seconds': result['processing_time'],
                                'timestamp': datetime.now().isoformat()
                            }
                            summary_data.append(result_row)
                        
                        if not result['success']:
                            self.logger.error(f"Failed extraction for source {result['source_id']}: {result['message']}")
                    
                    if failed_count > 0:
                        raise Exception(f"{failed_count} sources failed extraction")
                        
                    self.logger.info(f"Successfully completed parallel extraction of {len(results)} sources")
                    
            except Exception as e:
                self.logger.error(f"Multiprocessing extraction failed: {e}")
                raise

        # Write summary file if enabled
        if write_summary and summary_data:
            write_summary_csv(summary_file, summary_data)
            self.logger.info(f"Summary written to: {summary_file}")
        
        # Create pipeline convenience structure
        self.create_pipeline_convenience_structure(observation, obs_extractions_dir)
        
        total_sources = len(observation['source_ids'])
        self.logger.info(f"Extraction complete for {observation['name']}: {successful_count}/{total_sources} extracted ({already_exist_count} already exist, {skipped_count} skipped, {failed_count} failed)")
        observation['extracted'] = True
        
        # No longer need to restore cwd since we didn't change it
        # os.chdir(cwd)


def format_skip_reasons(skip_list):
    """Convert list of skip reasons to readable string."""
    if not skip_list:
        return ""
    
    # Group by reason type
    reason_counts = {}
    for reason in skip_list:
        reason_type = reason.split(': ', 1)[1] if ': ' in reason else reason
        if reason_type not in reason_counts:
            reason_counts[reason_type] = 0
        reason_counts[reason_type] += 1
    
    # Format as "2 groups: insufficient pixels; 1 group: single shutter"
    formatted = []
    for reason, count in reason_counts.items():
        plural = "groups" if count > 1 else "group"
        formatted.append(f"{count} {plural}: {reason}")
    
    return "; ".join(formatted)


def categorize_status(result):
    """Categorize extraction result into status."""
    if not result['success']:
        return 'failed'
    elif 'files exist' in result['message']:
        return 'already_exist'
    elif 'skipped' in result['message']:
        return 'skipped'
    else:
        return 'extracted'


def write_summary_csv(summary_file, summary_data):
    """Write extraction summary to CSV file."""
    import csv
    import os
    from datetime import datetime
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(summary_file), exist_ok=True)
    
    fieldnames = [
        'observation_name', 'source_id', 'status', 'message',
        'initial_groups_found', 'groups_after_validation', 'groups_skipped_reasons',
        'valid_groups_after_checks', 'spectrum_files_created', 'output_files',
        'processing_time_seconds', 'timestamp'
    ]
    
    with open(summary_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for result_row in summary_data:
            # Format the row
            formatted_row = {
                'observation_name': result_row['observation_name'],
                'source_id': result_row['source_id'],
                'status': result_row['status'],
                'message': result_row['message'],
                'initial_groups_found': result_row['initial_groups_found'],
                'groups_after_validation': result_row['groups_after_validation'],
                'groups_skipped_reasons': result_row['groups_skipped_reasons'],
                'valid_groups_after_checks': result_row['valid_groups_after_checks'],
                'spectrum_files_created': result_row['spectrum_files_created'],
                'output_files': "; ".join(result_row['output_files']) if result_row['output_files'] else "",
                'processing_time_seconds': result_row['processing_time_seconds'],
                'timestamp': result_row['timestamp']
            }
            
            writer.writerow(formatted_row)


def extract_single_source(source_id, observation_dict, config_dict, extractions_dir, version, overwrite=False, log_level='INFO', allow_threading=False):
    """
    Extract spectrum for a single source ID.
    
    This function is designed to work with both sequential and parallel processing.
    
    Parameters:
    -----------
    source_id : int
        Source ID to process
    observation_dict : dict
        Observation configuration dictionary (serializable copy)
    config_dict : dict
        Configuration dictionary (serializable copy)
    extractions_dir : str
        Directory for extracted spectra output
    version: str
        Reduction version, passed from ReductionEngine
    overwrite : bool
        Whether to overwrite existing files
    log_level : str
        Logging level for this worker
    allow_threading : bool
        Whether to allow internal threading in numerical libraries
        
    Returns:
    --------
    dict : Enhanced result dictionary with visit groups tracking
        {
            'success': bool,
            'source_id': int,
            'message': str,
            'groups_tracking': {
                'initial_groups_found': int,
                'groups_after_validation': int,
                'groups_skipped_validation': list,
                'groups_skipped_processing': list,
                'valid_groups_final': int
            },
            'spectrum_files_created': int,
            'output_files': list,
            'processing_time': float
        }
    """
    import time
    start_time = time.time()
    
    # Initialize tracking
    groups_tracking = {
        'initial_groups_found': 0,
        'groups_after_validation': 0,
        'groups_skipped_validation': [],
        'groups_skipped_processing': [],
        'valid_groups_final': 0
    }
    
    # Initialize spectrum files tracking
    spectrum_files = {}
    
    # Limit threading to prevent oversubscription when multiprocessing
    if not allow_threading:
        # Set environment variables (must be done before importing libraries)
        threading_vars = {
            'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1', 
            'NUMEXPR_NUM_THREADS': '1',
            'OMP_NUM_THREADS': '1',
            'VECLIB_MAXIMUM_THREADS': '1',  # macOS Accelerate
            'NUMBA_NUM_THREADS': '1',       # Numba
            'BLAS_NUM_THREADS': '1',        # Generic BLAS
            'LAPACK_NUM_THREADS': '1',      # LAPACK
        }
        
        for var, value in threading_vars.items():
            os.environ[var] = value
    
    # Set up independent logger for this worker
    logger = logging.getLogger(f'nirspec_extract_worker_{source_id}')
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.propagate = False
    logger.handlers.clear()
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper()))
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    try:
        logger.info(f"Starting spectrum extraction for source {source_id}")
        
        # Check msaexp availability
        if not MSAEXP_AVAILABLE:
            error_msg = "msaexp not available - cannot perform spectrum extraction"
            logger.error(error_msg)
            return {
                'success': False,
                'source_id': source_id,
                'message': error_msg,
                'groups_tracking': groups_tracking,
                'spectrum_files_created': 0,
                'output_files': [],
                'processing_time': time.time() - start_time
            }
        
        # Import required libraries here (after threading control)
        try:
            import msaexp.slit_combine
            import numpy as np
            import matplotlib.pyplot as plt
            from astropy.io import fits
        except ImportError as e:
            error_msg = f"Failed to import required libraries: {e}"
            logger.error(error_msg)
            return {
                'success': False,
                'source_id': source_id,
                'message': error_msg,
                'groups_tracking': groups_tracking,
                'spectrum_files_created': 0,
                'output_files': [],
                'processing_time': time.time() - start_time
            }
        
        observation = observation_dict.copy()
        config = config_dict.get('extractions', {})
        program_id = observation.get('program_id')
        
        # Get unique groups/visits - exact copy of get_visit_groups logic
        logger.info('Splitting visit groups')
        gratings = observation['gratings']
        rate_files = observation['rate_files']
        workspace_dir = observation.get('workspace_dir')
        
        groups = {}
        for rate_file in rate_files:
            # rate_file is absolute path, extract basename and search in workspace directory
            rate_file_base = os.path.basename(rate_file)
            phot_pattern = rate_file_base.replace('_rate.fits', f'_phot*_{source_id}.fits')
            phot_files = glob.glob(os.path.join(workspace_dir, phot_pattern))

            for phot_file in phot_files:
                try:
                    with fits.open(phot_file) as im:
                        grating = im[0].header["GRATING"]
                        filt = im[0].header["FILTER"]
                        if im[0].header["GRATING"] not in gratings:
                            continue
                    
                    # jw06585004001_07101_00001_nrs1_phot.516.6585_6.fits
                    # jw<ppppp><ooo><vvv>_<gg><s><aa>_<eeeee>(-<"seg"NNN>)_<detector
                    name = os.path.basename(phot_file)
                    pov = name.split('_')[0]
                    gsa = name.split('_')[1]
                    det = name.split('_')[3]
                    key = f'{pov}_{gsa}_{det}_{grating}_{filt}'.lower()
                    if key in groups:
                        groups[key].append(phot_file)
                    else:
                        groups[key] = [phot_file]
                        
                except Exception as e:
                    logger.warning(f"Failed to process file {phot_file}: {e}")
                    continue

        # Track initial groups found (before validation)
        groups_tracking['initial_groups_found'] = len(groups)
        
        # Sort files within each group and validate
        groups_to_remove = []
        for key in list(groups.keys()):
            n = len(groups[key])
            if n > 3:
                groups_tracking['groups_skipped_validation'].append(f"group {key}: {n} files > 3")
                logger.warning(f'Visit group {key} has too many files ({n}, should be <=3)')
                groups_to_remove.append(key)
                continue

            groups[key] = sorted(groups[key])
            logger.info(f"Group {key}: {groups[key]}")
        
        # Remove groups that failed validation
        for key in groups_to_remove:
            del groups[key]
        
        # Track groups after validation
        groups_tracking['groups_after_validation'] = len(groups)
        logger.info(f"Created {len(groups)} visit groups")
        
        if len(groups) == 0:
            error_msg = f"No visit groups found for source {source_id}"
            logger.warning(error_msg)
            return {
                'success': False,
                'source_id': source_id,
                'message': error_msg,
                'groups_tracking': groups_tracking,
                'spectrum_files_created': 0,
                'output_files': [],
                'processing_time': time.time() - start_time
            }
        
        # Work determination - check if extraction needed (matches _check_extraction_needed logic)
        if not overwrite:
            logger.info(f"Checking if extraction needed for source {source_id}")
            all_files_exist = True
            for group_key in groups:
                gratingfilter = "-".join(group_key.split("_")[-2:])
                from pathlib import Path
                # Check in source subdirectory with original naming format
                source_dir = Path(extractions_dir) / str(source_id)
                specfile = source_dir / f'{observation["name"]}_{gratingfilter}_{source_id}_{version}_spec.fits'
                if not specfile.exists():
                    all_files_exist = False
                    break
            
            if all_files_exist:
                logger.info(f"All spectrum files exist for source {source_id}, skipping extraction")
                return {
                    'success': True,
                    'source_id': source_id,
                    'message': "skipped - files exist",
                    'groups_tracking': groups_tracking,
                    'spectrum_files_created': 0,
                    'output_files': [],
                    'processing_time': time.time() - start_time
                }
                
            logger.info(f"Extraction needed for source {source_id}, proceeding...")
        

        initial_theta = config.get("initial_theta", "auto")
        if initial_theta == "auto":
            # Use first group key to determine background vs target
            first_key = list(groups.keys())[0] if groups else ""
            if "b" in first_key:
                initial_theta = [5.0, -0.5]
            elif "background" in first_key:
                initial_theta = [5.0, -0.5]
            else:
                initial_theta = None
        initial_sigma = 5

        #### Keyword arguments for SlitGroup
        kwargs_slitgroup = dict(
            diffs=config.get('diffs', True),  # For nod differences
            grating_diffs=True,
            position_key=config.get('position_key', 'y_index'),
            sky_arrays=None,

            # barshadow    
            undo_barshadow=config.get('undo_barshadow', 2),  # For msaexp barshadow correction
            min_bar=config.get('min_bar', 0.35),  # minimum allowed value for the (inverse) bar shadow correction
            bar_corr_mode='wave',

            undo_pathloss=config.get('undo_pathloss', 1),
            stuck_threshold=config.get('stuck_threshold', 0.3),

            trace_with_ypos=config.get('trace_with_ypos', True),  # Include expected y shutter offset in the trace
            trace_from_yoffset=config.get('trace_from_yoffset', False),
            trace_with_xpos=config.get('trace_with_xpos', True),
            reference_exposure=config.get('reference_exposure', 'auto'),
            pad_border=0,

            # Additional parameters
            hot_cold_kwargs=None,
            flag_profile_kwargs=None,  # Turn off profile flag
            bad_shutter_names=None,
            dilate_failed_open=True,
            slit_hotpix_kwargs={},
            sky_file=None,
            set_background_spectra_kwargs={},
            global_sky_df=7,
            fit_shutter_offset_kwargs=None,
            shutter_offset=0.0,
            nod_offset=None,
            do_multiple_mask=True,
            flag_trace_kwargs={},
            flag_percentile_kwargs={},
            with_fs_offset=False,
            weight_type='ivm',
            lookup_prf=None,
    
            fix_prism_norm=config.get('fix_prism_norm', False),
            with_sflat_correction=config.get('with_sflat_correction', True),
            extended_calibration_kwargs=DEFAULT_EXTENDED_CALIBRATION_KWARGS, 
        )

        kwargs_slitgroup["estimate_sky_kwargs"] = None
        kwargs_slitgroup["extended_calibration_kwargs"] = None

        valid_frac_threshold = 0.1

        # Process each group
        xobj = {}
        for ig, g in enumerate(groups):
            
            logger.info(f'Working on group {g}')

            # Capture msaexp output for SlitGroup creation
            prf = msaexp.slit_combine.set_lookup_prf(
                slit_file=groups[g][0],
                version="001",
                lookup_prf_type="merged",
            )

            try:
                obj = msaexp.slit_combine.SlitGroup(
                    groups[g], g, **kwargs_slitgroup,
                )
            except Exception as e:
                groups_tracking['groups_skipped_processing'].append(f"group {g}: SlitGroup creation failed - {str(e)}")
                logger.error(f"Failed to create SlitGroup for {g}: {e}")
                print(g, groups[g])
                import traceback
                print(traceback.format_exc())
                quit()
                continue
            
            if obj.mask.sum() < 256:
                groups_tracking['groups_skipped_processing'].append(f"group {g}: insufficient valid pixels ({obj.mask.sum()})")
                logger.info("Skipping group - insufficient valid pixels")
                continue

            # Quality checks for difference mode
            if obj.meta["diffs"]:
                valid_frac = obj.mask.sum() / obj.mask.size

                if obj.N == 1:
                    groups_tracking['groups_skipped_processing'].append(f"group {g}: single shutter")
                    logger.info("Skipping group - single shutter")
                    continue
                elif len(obj.meta["bad_shutter_names"]) == obj.N:
                    groups_tracking['groups_skipped_processing'].append(f"group {g}: all bad shutters")
                    logger.info("Skipping group - all bad shutters")
                    continue
                elif (len(obj.unp.values) == 1) & (obj.meta["diffs"]):
                    groups_tracking['groups_skipped_processing'].append(f"group {g}: one position")
                    logger.info("Skipping group - one position")
                    continue
                elif valid_frac < valid_frac_threshold:
                    groups_tracking['groups_skipped_processing'].append(f"group {g}: valid pixels {valid_frac:.2f} < {valid_frac_threshold}")
                    logger.info(f"Skipping group - valid pixels {valid_frac:.2f} < {valid_frac_threshold}")
                    continue

            xobj[g] = {"obj": obj}

        # Track final valid groups count
        groups_tracking['valid_groups_final'] = len(xobj)
        
        if len(xobj) == 0:
            logger.warning("No valid spectra found")
            return {
                'success': True,
                'source_id': source_id,
                'message': "skipped - no valid spectra found",
                'groups_tracking': groups_tracking,
                'spectrum_files_created': 0,
                'output_files': [],
                'processing_time': time.time() - start_time
            }

        # Fit parameters setup
        fit_params_kwargs = {}
        fit_params_kwargs.update(DEFAULT_FIT_PARAMS_KWARGS)
        if initial_theta is not None:
            if len(initial_theta) > 1:
                fit_params_kwargs["theta"] = initial_theta

        # Sort in order of decreasing S/N
        sn_keys = []
        for k in xobj:
            obj = xobj[k]["obj"]
            _sn, sn_val, _, _ = obj.fit_params_by_sn(**fit_params_kwargs)
            if "prism" in k:
                sn_val *= 2
            if not np.isfinite(sn_val):
                sn_keys.append(-1)
            else:
                sn_keys.append(sn_val)

            so = np.argsort(sn_keys)[::-1]
        keys = [list(xobj.keys())[j] for j in so]

        obj0 = xobj[keys[0]]["obj"]
        _sn, sn_val, do_fix_sigma, offset_degree = obj0.fit_params_by_sn(**fit_params_kwargs)
        
        if do_fix_sigma:
            recenter_all = False
            fix_params = True
            if initial_theta is None:
                initial_theta = np.array([initial_sigma, 0.0])

        if initial_theta is not None:
            CENTER_PRIOR = initial_theta[-1]
            SIGMA_PRIOR = initial_theta[0] / 10.0
        else:
            CENTER_PRIOR = 0.0
            SIGMA_PRIOR = 0.6
        fix_sigma_across_groups = True
        fix_sigma = -1
        
        # Process traces for each group
        for i, k in enumerate(keys):
            logger.info(f"Processing traces for group #{i+1} / {len(xobj)}: {k}")
            obj = xobj[k]["obj"]

            prf = msaexp.slit_combine.set_lookup_prf(
                slit_file=obj.files[0],
                version="001",
                lookup_prf_type="merged",
            )

            ref_exp = obj.calc_reference_exposure

            trace_kwargs = dict(
                niter=config.get('trace_niter', 4),
                force_positive=False,
                degree=config.get('offset_degree', 1),
                ref_exp=ref_exp,
                sigma_bounds=(3, 12),
                with_bounds=False,
                trace_bounds=(-1.0, 1.0),
                initial_sigma=initial_sigma,
                x0=initial_theta,
                method="powell",
                tol=1.0e-8,
            )
            recenter_all = config.get('recenter_all', False)

            if (i == 0) or recenter_all:
                tfit = obj.fit_all_traces(**trace_kwargs)
                theta = tfit[obj.unp.values[0]]["theta"]

                if i == 0:
                    theta = theta[1:]
                    fix_sigma = tfit[obj.unp.values[0]]["sigma"] * 10

            else:
                trace_kwargs["x0"] = theta
                trace_kwargs["with_bounds"] = False
                trace_kwargs["evaluate"] = True
                trace_kwargs["fix_sigma"] = fix_sigma

                tfit = obj.fit_all_traces(**trace_kwargs)

            xobj[k] = {"obj": obj, "fit": tfit}

        # List of gratings
        gratings_dict = {}
        max_size = {}

        for k in keys:
            gr = "_".join(k.split("_")[-2:])
            if gr in gratings_dict:
                gratings_dict[gr].append(k)
                max_size[gr] = np.maximum(max_size[gr], xobj[k]["obj"].sh[1])
            else:
                gratings_dict[gr] = [k]
                max_size[gr] = xobj[k]["obj"].sh[1]

        # Generate 2D plots
        make_2d_plots = True
        if make_2d_plots:
            for k in keys:
                logger.info(f"Generating 2D plot for {k}")
                obj = xobj[k]["obj"]
            
                if "fit" in xobj[k]:
                    fit = xobj[k]["fit"]
                else:
                    fit = None

                # try:
                #     fig2d = obj.plot_2d_differences(fit=fit)
                #     filename = f"{observation['name']}_{k}_differences.pdf".lower()
                #     # Save plot to extractions directory
                #     filepath = os.path.join(extractions_dir, filename)
                #     logger.info(f"Saving 2D plot: {filepath}")
                #     fig2d.savefig(filepath)
                #     plt.close(fig2d)

                # except Exception as e:
                #     logger.warning(f"Failed to generate 2D plot for {k}: {e}")

        # Combine gratings and save spectrum files
        for g in gratings_dict:
            logger.info(f"Combining grating group: {g}")
            
            try:
                hdul = msaexp.slit_combine.combine_grating_group(
                    xobj,
                    gratings_dict[g],
                    drizzle_kws=DEFAULT_DRIZZLE_KWARGS,
                    include_full_pixtab=['PRISM'],
                )

                header = hdul[1].header
                gr, fl = header['GRATING'].lower(), header['FILTER'].lower()
                srcname = header['SRCNAME']

                # Create source-specific subdirectory (keep original filename format)
                source_dir = os.path.join(extractions_dir, str(source_id))
                os.makedirs(source_dir, exist_ok=True)
                
                # Add pipeline version to FITS header
                pipeline_config = config_dict.get('pipeline', {})
                if 'version' in pipeline_config:
                    hdul[0].header['PIPEVER'] = (pipeline_config['version'], 'Pipeline version')
                    hdul[0].header['PIPEDATE'] = (pipeline_config.get('date', ''), 'Pipeline version date')
                    hdul[0].header['PIPEDESC'] = (pipeline_config.get('description', ''), 'Pipeline version description')
                
                # Add package versions to FITS header
                try:
                    import msaexp
                    hdul[0].header['MSAEXPV'] = (msaexp.__version__, 'msaexp package version')
                except (ImportError, AttributeError):
                    hdul[0].header['MSAEXPV'] = ('unknown', 'msaexp package version')
                
                try:
                    import grizli
                    hdul[0].header['GRIZLIV'] = (grizli.__version__, 'grizli package version')
                except (ImportError, AttributeError):
                    hdul[0].header['GRIZLIV'] = ('unknown', 'grizli package version')
                
                try:
                    import jwst
                    hdul[0].header['JWSTV'] = (jwst.__version__, 'jwst package version')
                except (ImportError, AttributeError):
                    hdul[0].header['JWSTV'] = ('unknown', 'jwst package version')
                
                # Use source_id format: {obs}_{grating}-{filter}_{source_id}_{version}_spec.fits  
                specfile = f'{observation["name"]}_{gr}-{fl}_{source_id}_{version}_spec.fits'
                specpath = os.path.join(source_dir, specfile)
                logger.info(f'Saving spectrum to {specpath}')
                hdul.writeto(specpath, overwrite=True)
                
                # Update observation with the actual saved file path
                spectrum_files[f'{gr}-{fl}'] = specpath

            except Exception as e:
                logger.error(f"Failed to combine grating group {g}: {e}")
                continue

        # Cleanup
        for k in xobj:
            try:
                obj = xobj[k]["obj"]
                for sl in obj.slits:
                    sl.close()
            except Exception as e:
                logger.warning(f"Failed to cleanup slit {k}: {e}")
        
        logger.info(f"Successfully extracted {len(spectrum_files)} spectra for source {source_id}")
        return {
            'success': True,
            'source_id': source_id,
            'message': "",
            'groups_tracking': groups_tracking,
            'spectrum_files_created': len(spectrum_files),
            'output_files': list(spectrum_files.values()),
            'processing_time': time.time() - start_time
        }
        
    except Exception as e:
        error_msg = f"Failed to extract spectrum for source {source_id}: {e}"
        logger.error(error_msg)
        return {
            'success': False,
            'source_id': source_id,
            'message': error_msg,
            'groups_tracking': groups_tracking,
            'spectrum_files_created': 0,
            'output_files': [],
            'processing_time': time.time() - start_time
        }


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
    parser.add_argument('--preprocess', action='store_true',
                       help='Run preprocessing step')
    parser.add_argument('--extract', action='store_true',
                       help='Run spectrum extraction step')
    parser.add_argument('--overwrite', action='store_true',
                       help='Overwrite existing files')
    parser.add_argument('--processes', type=int, default=1,
                       help='Number of processes for multiprocessing (default: 1 for sequential)')
    parser.add_argument('--no-parallel', action='store_true',
                       help='Force sequential processing even if --processes > 1')
    parser.add_argument('--allow-threading', action='store_true',
                       help='Allow internal threading in workers (default: disabled to prevent oversubscription)')
    parser.add_argument('--version', type=str,
                       help='Pipeline version (maps to config_<version>.toml if --config not specified)')
    
    args = parser.parse_args()
    
    # Handle version argument - if version specified but not config, map to config file
    if args.version and args.config == 'config.toml':  # Default config not overridden
        args.config = f'config_{args.version}.toml'
        print(f"Using version {args.version} -> {args.config}")
    
    # Handle threading control - restart script with environment variable if needed
    n_processes = 1 if args.no_parallel else args.processes
    if n_processes > 1 and not args.allow_threading:
        if os.environ.get('NIRSPEC_DISABLE_THREADING', '0') != '1':
            print("Restarting with threading disabled...")
            os.environ['NIRSPEC_DISABLE_THREADING'] = '1'
            # Re-exec the script with the same arguments
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return  # This won't be reached, but good practice
    
    # If neither preprocess nor extract specified, do nothing (for testing convenience)
    if not args.preprocess and not args.extract:
        print(f"No processing steps specified for observation: {args.obs}")
        print("Use --preprocess to run preprocessing step")
        print("Use --extract to run extraction step") 
        print("Use both flags to run complete pipeline")
        return
    
    # Handle multiprocessing arguments
    if n_processes > 1:
        print(f"Using {n_processes} processes for multiprocessing")
        if not args.allow_threading:
            print("Internal threading disabled to prevent oversubscription")
        else:
            print("Internal threading enabled (may cause oversubscription)")
    
    # Load configurations
    observations = load_observations(args.observations)
    observation_config = get_observation_config(args.obs, observations)
    
    # Initialize reduction engine
    engine = ReductionEngine(args.config)
    
    try:
        if args.preprocess:
            print(f"Starting preprocessing for observation: {args.obs}")
            engine.preprocess_observation(
                observation_config, 
                overwrite=args.overwrite, 
                n_processes=n_processes,
                allow_threading=args.allow_threading,
            )
            print(f"Completed preprocessing for observation: {args.obs}")
        else:
            print(f"Skipping preprocessing for observation: {args.obs}")
            print("Use --preprocess to run preprocessing step")

        
        if args.extract:
            print(f"Starting extraction for observation: {args.obs}")
            engine.extract_spectra_for_observation(
                observation_config, 
                overwrite=args.overwrite, 
                n_processes=n_processes, 
                allow_threading=args.allow_threading
            )
            print(f"Completed extraction for observation: {args.obs}")
        else:
            print(f"Skipping extraction for observation: {args.obs}")
            print("Use --extract to run extraction step") 
            
    except Exception as e:
        print(f"Error processing observation {args.obs}: {e}")
        raise


if __name__ == '__main__':
    main()
