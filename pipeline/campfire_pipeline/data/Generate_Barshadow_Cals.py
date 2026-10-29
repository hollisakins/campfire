"""Maintainer script: derive the empirical NIRSpec MSA bar-shadow reference for one
disperser from CAMPFIRE stage-2a canonicals, and write it (plus a QA report) here.

    conda run -n campfire python pipeline/campfire_pipeline/data/Generate_Barshadow_Cals.py \\
        --grating PRISM --obs ember_cosmos_p1 ember_egs_p1 ember_uds_p2 ember_cosmos_p14 \\
        --held-out ember_egs_p1 --processes 8

Follows the extended-wavelength calibration's pattern (``Generate_Extended_Cals.py``,
``config.get_extended_photom_path``): CAMPFIRE products derive the calibration, the
calibration is committed with the pipeline. The derivation itself is
``campfire_pipeline.nirspec.barshadow_calibration``; this script only parses options,
runs it, writes the file and draws the report.
"""
import argparse
import os
import sys

import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--grating', required=True, help='PRISM, G140M, G235M or G395M')
    ap.add_argument('--filter', default=None, help='restrict to one filter (e.g. F100LP)')
    ap.add_argument('--obs', nargs='+', required=True, help='observation names (stage 2a done)')
    ap.add_argument('--held-out', default=None, help='derive without this observation and test on it')
    ap.add_argument('--processes', type=int, default=4)
    ap.add_argument('--source-pad-px', type=float, default=2.0, help='rows excluded beyond the source shutter edges (5+ leaves only the shutters two away)')
    ap.add_argument('--tail-px', type=int, default=None, help='footprint extension past the cutoff (default: coverage.TAIL_PX)')
    ap.add_argument('--smooth-cols', type=float, default=2.0, help='Gaussian smoothing along wavelength, in columns')
    ap.add_argument('--n-min', type=int, default=20, help='pixels a (segment, row, column) cell needs')
    ap.add_argument('--out-dir', default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument('--qa', default=None, help='QA PDF path (default <out-dir>/empirical_barshadow_<grating>_qa.pdf)')
    ap.add_argument('--no-write', action='store_true', help='derive and report only')
    args = ap.parse_args()

    from campfire_pipeline.config import load_config, setup_environment, resolve_paths
    from campfire_pipeline.nirspec import barshadow_calibration as B
    from campfire_pipeline.nirspec.barshadow_qa import qa_report
    cfg = load_config(None)
    setup_environment(cfg)
    paths = resolve_paths(cfg)
    cache_dir = os.path.join(os.environ.get('CAMPFIRE_ROOT', os.path.expanduser('~/campfire-data')), 'cache', 'coverage')
    try:
        from campfire_pipeline import __version__ as version
    except Exception:
        version = 'unknown'

    res = B.derive(args.obs, args.grating.upper(), paths['products_dir'], cache_dir, filter_name=args.filter,
                   source_pad_px=args.source_pad_px, tail_px=args.tail_px, processes=args.processes,
                   smooth_cols=args.smooth_cols, n_min=args.n_min, held_out=args.held_out)
    res['provenance']['version'] = version
    name = f'empirical_jwst_nirspec_barshadow_{args.grating.lower()}.fits'
    out = os.path.join(args.out_dir, name)
    if not args.no_write:
        B.write_reference(res['tables'], res['template'], out, res['provenance'])
        print(f'wrote {out}')
    qa = args.qa or os.path.join(args.out_dir, f'empirical_barshadow_{args.grating.lower()}_qa.pdf')
    qa_report(res, qa)
    # the per-shutter centre spectra, for analyses beyond the report
    keys = list(res['shutters'])
    np.savez(os.path.splitext(qa)[0] + '_shutters.npz', keys=np.array([list(map(str, k)) for k in keys]),
             spectra=np.array([res['shutters'][k][0] for k in keys]), counts=np.array([res['shutters'][k][1] for k in keys]),
             scale=np.array([res['scales'][k][0] for k in keys]), clean=np.array([res['scales'][k][1] for k in keys]),
             sky=res['sky'], wavelength=res['grid'].wavelengths_um)
    print(f'wrote {qa}')


if __name__ == '__main__':
    main()
