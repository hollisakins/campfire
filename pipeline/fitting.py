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
import argparse
import warnings; warnings.filterwarnings('ignore')
import multiprocessing as mp
mp.set_start_method('fork')

# Extracted to campfire_pipeline.common.spectral (backwards compat re-imports)
from campfire_pipeline.common.spectral import (
    air_to_vac,
    get_wavelength_sampling,
    planck,
    MBB,
    resample_to_nonuniform_grid,
    convolve_with_lsf,
    resample_to_observed_grid,
)

# Extracted to campfire_pipeline.nirspec.redshift (backwards compat re-imports)
from campfire_pipeline.nirspec.redshift import calculate_redshift_confidence

# Extracted to campfire_pipeline.nirspec.templates (backwards compat re-imports)
from campfire_pipeline.nirspec.templates import make_template_grid

# Extracted to campfire_pipeline.nirspec.fitting (backwards compat re-imports)
from campfire_pipeline.nirspec.fitting import (
    _nnls_gram,
    _compute_gram,
    _fit_all_redshifts_numba,
    _iter,
    fit_single_spectrum,
    fit_single_spectrum_optimized,
    fit_observation,
)


def main():
    """Main function to run NIRSpec redshift fitting."""
    parser = argparse.ArgumentParser(description='NIRSpec Redshift Fitting Script')
    parser.add_argument('--config', type=str, default='config.toml', help='Path to configuration file (default: config.toml)')
    parser.add_argument('--obs', type=str, help='Observation name from observations.toml')
    parser.add_argument('--source-ids', nargs='+', type=int, help='Individual source IDs to restrict processing to')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing products')
    parser.add_argument('--make-templates', action='store_true',
                        help='Generate continuum template grids from [template_grids] config and exit')
    args = parser.parse_args()
    config_path = args.config
    from campfire_pipeline.config import load_config, setup_environment, resolve_template_grid_paths
    config = load_config(config_path)
    setup_environment(config)

    # Template generation mode: generate all grids from config and exit
    if args.make_templates:
        template_grids_config = resolve_template_grid_paths(config)
        if not template_grids_config:
            print("No [template_grids] section found in config")
            return 1
        for name, grid_config in template_grids_config.items():
            output_file = os.path.abspath(grid_config['file'])
            print(f"\n=== Generating '{name}' template grid ===")
            make_template_grid(
                z_min=grid_config.get('z_min', 0),
                z_max=grid_config.get('z_max', 20),
                dv=grid_config['dv'],
                output_file=output_file
            )
        return 0

    if not args.obs:
        parser.error("--obs is required for fitting (omit only with --make-templates)")

    return fit_observation(
        obs_name=args.obs,
        config=config,
        source_ids=args.source_ids,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    exit(main())
