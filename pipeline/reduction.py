"""
NIRSpec reduction — backwards compatibility shim.

The canonical location for all code is now campfire_pipeline/.
This file re-exports everything so that existing scripts
(e.g., ``python reduction.py --obs ...``) continue to work.

Usage:
python reduction.py --obs capers_uds_p2
"""

import argparse

# --- Re-exports from campfire_pipeline (backwards compat) ---

from campfire_pipeline.nirspec.constants import (
    GRATING_LIMITS, GRATINGS,
    DEFAULT_STAGE1_CONFIG, DEFAULT_STAGE2_CONFIG, DEFAULT_STAGE3_CONFIG,
)
from campfire_pipeline.config import load_config
from campfire_pipeline.common.io import log, files_to_glob
from campfire_pipeline.nirspec.metafile import MetaFile
from campfire_pipeline.nirspec.observation import Observation
from campfire_pipeline.common.wcs import boundingbox_to_indices, wcs_to_dq
from campfire_pipeline.nirspec.stage1 import (
    mask_slits, subtract_background_from_rate_file, run_stage1_single_uncal,
)
from campfire_pipeline.nirspec.stage2 import (
    run_stage2a_single_rate, fix_units, resample_single_exposure,
    pad_to_common_detector_region, unpad_model, run_stage2b_single_slitlet,
)
from campfire_pipeline.nirspec.extraction import (
    boxcar_profile, optext_profile, extract_with_profile, combine_1d_spectra,
)
from campfire_pipeline.nirspec.stage3 import (
    run_stage3_single_source, opt_ext_single_source, combine_per_eg_spectra,
)
from campfire_pipeline.nirspec.plots import plot_stage2a_results
from campfire_pipeline.nirspec.engine import ReductionEngine


def main():
    """Main function to run NIRSpec data reduction."""
    parser = argparse.ArgumentParser(description='NIRSpec Data Reduction Pipeline')
    parser.add_argument('--obs', type=str, required=True,
                        help='Observation name from observations.toml')
    parser.add_argument('--config', type=str, default='config.toml',
                        help='Path to configuration file (default: config.toml)')
    parser.add_argument('--stage1', action='store_true',
                        help='Run stage 1 processing (Detector1Pipeline)')
    parser.add_argument('--stage2a', action='store_true',
                        help='Run stage 2a processing (Spec2Pipeline, no bkg subtraction)')
    parser.add_argument('--stage2b', action='store_true',
                        help='Run stage 2b processing (Spec2Pipeline, with bkg subtraction)')
    parser.add_argument('--stage3', action='store_true',
                        help='Run stage 3 processing (Spec3Pipeline)')
    parser.add_argument('--summary', action='store_true',
                        help='Generate observation summary ECSV')
    parser.add_argument('--source-ids', nargs='+', type=int,
                        help='Individual source IDs to restrict processing to')
    parser.add_argument('--processes', type=int, default=1,
                        help='Number of processes for multiprocessing (default: 1 for sequential)')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing products')

    args = parser.parse_args()
    if args.source_ids is None:
        args.source_ids = 'all'

    if args.processes > 1:
        log(f"Using {args.processes} processes for multiprocessing")

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

    if args.summary:
        log(f"Generating summary for observation {obs.name}")
        engine.run_summarize(obs)

    if not args.stage1 and not args.stage2a and not args.stage2b and not args.stage3 and not args.summary:
        raise RuntimeError("No stages to run!")


if __name__ == '__main__':
    main()
