#!/usr/bin/env python3
"""
Generate slit geometry JSON for an observation.

Computes MSA shutter positions (centers + position angles) for all objects
in an observation, applying astrometric corrections from an MSA catalog
cross-matched against a reference catalog.

Usage:
    python scripts/generate_slits.py --obs ember_uds_p4 --approve-shifts
    python scripts/generate_slits.py --obs capers_cosmos_p1 --skip-shifts

Output:
    pipeline/products/{obs_name}/{obs_name}_slits.json
"""

import argparse
import json
import glob
import os
import sys

import numpy as np
import toml
from astropy.coordinates import SkyCoord
from astropy.io import fits
import astropy.units as u

# Add pipeline directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

from slits import get_source_pos, compute_slit_centers
from plot_slits import (
    fit_offsets,
    COSMOS_CATALOG, UDS_CATALOG, EGS_CATALOG, A2744_CATALOG,
)


def main():
    parser = argparse.ArgumentParser(description='Generate slit geometry JSON')
    parser.add_argument('--obs', type=str, required=True,
                        help='Observation name from observations.toml')
    parser.add_argument('--skip-shifts', action='store_true',
                        help='Skip fitting astrometric shifts')
    parser.add_argument('--approve-shifts', action='store_true',
                        help='Auto approve astrometric shifts (no interactive prompt)')
    args = parser.parse_args()

    pipeline_dir = os.path.join(os.path.dirname(__file__), '..', 'pipeline')
    os.chdir(pipeline_dir)

    obs_config = toml.load('observations.toml')[args.obs]
    field = obs_config.get('field')
    obs = args.obs
    gratings = obs_config.get('gratings', [])

    # Discover source IDs
    srcids = sorted(list(set([
        int(f.split('_')[-2])
        for f in glob.glob(f'products/{obs}/{obs}_*_spec.fits')
    ])))
    object_ids = [f'{obs}_{i}' for i in srcids]

    if not srcids:
        print(f"No spec files found in products/{obs}/")
        sys.exit(1)

    print(f"Observation: {obs}")
    print(f"Field: {field}")
    print(f"Found {len(srcids)} objects")

    # Fit astrometric corrections
    dra_interp = None
    ddec_interp = None

    if not args.skip_shifts:
        msacat_file = f'products/{obs}/{obs}_msacat.csv'
        if not os.path.exists(msacat_file):
            print(f"Error: No MSA catalog file found at {msacat_file}")
            print(f"Either provide one or use --skip-shifts")
            sys.exit(1)

        from astropy.table import Table
        msa_cat = Table.read(msacat_file)[1:]
        msa_coords = SkyCoord(msa_cat['RA'], msa_cat['DEC'], unit='deg')

        # Load reference catalog
        match field:
            case 'uds':
                ref_cat = fits.open(UDS_CATALOG)
                ref_coords = SkyCoord(ref_cat[1].data['RA'], ref_cat[1].data['DEC'], unit='deg')
            case 'egs':
                ref_cat = fits.open(EGS_CATALOG)
                ref_coords = SkyCoord(ref_cat[1].data['RA'], ref_cat[1].data['DEC'], unit='deg')
            case 'cosmos':
                ref_cat = fits.open(COSMOS_CATALOG)
                ref_coords = SkyCoord(ref_cat[1].data['ra'], ref_cat[1].data['dec'], unit='deg')
            case 'a2744':
                ref_cat = fits.open(A2744_CATALOG)
                ref_coords = SkyCoord(ref_cat[1].data['ra'], ref_cat[1].data['dec'], unit='deg')
            case _:
                print(f"Error: Unknown field '{field}' for astrometric correction")
                sys.exit(1)

        # Get observed positions
        obs_ra, obs_dec = [], []
        for srcid in srcids:
            spec_files = glob.glob(f'products/{obs}/{obs}_*_{srcid}_spec.fits')
            ra, dec = get_source_pos(spec_files[0])
            obs_ra.append(ra)
            obs_dec.append(dec)
        obs_coords = SkyCoord(obs_ra, obs_dec, unit='deg')

        # Filter MSA catalog to near observed objects
        idx, d2d, d3d = msa_coords.match_to_catalog_sky(obs_coords)
        msa_coords = msa_coords[d2d < 1.5 * u.arcmin]

        if not args.approve_shifts:
            dra_interp, ddec_interp = fit_offsets(msa_coords, ref_coords, method='poly2d', smoothing=0.01, plot=True)
            cont = input('Satisfied with offset fits [Y/n]? ')
            if cont != 'Y':
                print("Aborted.")
                return
        else:
            dra_interp, ddec_interp = fit_offsets(msa_coords, ref_coords, method='poly2d', smoothing=0.01, plot=False)

        print("Astrometric corrections fitted successfully")

    # Compute slit centers for all objects
    print("Computing slit geometry...")
    all_slits = []

    import tqdm
    for i in tqdm.tqdm(range(len(object_ids))):
        object_id = object_ids[i]
        srcid = srcids[i]

        spec_files = glob.glob(f'products/{obs}/{obs}_*_{srcid}_spec.fits')
        ra, dec = get_source_pos(spec_files[0])

        # Apply astrometric correction
        corrected_pos = None
        if dra_interp is not None:
            drai = dra_interp(ra, dec) / 3600
            ddeci = ddec_interp(ra, dec) / 3600
            if drai > 8e-5 or ddeci > 8e-5:
                drai, ddeci = 0, 0
            corrected_pos = (ra + drai, dec + ddeci)

        slits = compute_slit_centers(spec_files, corrected_pos=corrected_pos)

        # Determine grating from filename
        # Pattern: {obs}_{grating}_{srcid}_spec.fits
        grating = None
        if len(spec_files) == 1:
            parts = spec_files[0].split('/')[-1].replace('_spec.fits', '').split('_')
            # object_id is obs_srcid, grating is the part between obs name and srcid
            obs_parts = obs.split('_')
            grating_parts = parts[len(obs_parts):-1]
            if grating_parts:
                grating = '_'.join(grating_parts).upper()

        for slit in slits:
            slit['object_id'] = object_id
            slit['observation'] = obs
            slit['field'] = field
            slit['grating'] = grating or (gratings[0] if gratings else None)
            all_slits.append(slit)

    # Write output
    output_path = f'products/{obs}/{obs}_slits.json'
    with open(output_path, 'w') as f:
        json.dump(all_slits, f, indent=2)

    print(f"\nWrote {len(all_slits)} slit records to {output_path}")
    print(f"  ({len(object_ids)} objects x ~{len(all_slits) // max(len(object_ids), 1)} slits/object)")


if __name__ == '__main__':
    main()
