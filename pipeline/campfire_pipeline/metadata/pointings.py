"""
Generate pointings ECSV with one row per NIRSpec MSA pointing.

A "pointing" is one MSA design at one nominal sky position, identified by
MSAMETID (a stable integer in the cal-file primary header). Sub-arcsec
dithers within an MSAMETID are collapsed; truly distinct MSA designs in a
single observation (e.g. ceers1, uncover1) yield separate rows.

Each row carries pointing geometry, exposure aggregates, and a 4-quadrant
sky footprint computed via JWST_FOV_plotter.
"""

from collections import defaultdict
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log


def generate_pointings_table(obs_name, obs_dir, field):
    """Generate a pointings table for an observation.

    Reads cal-file headers in obs_dir, deduplicates exposures across
    per-source copies via (visit, exp_spec, exp_num, detector), groups
    unique exposures by MSAMETID, and aggregates per-pointing metadata
    plus a 4-quadrant footprint per pointing.

    Parameters
    ----------
    obs_name : str
    obs_dir : Path or str
    field : str

    Returns
    -------
    astropy.table.Table
        Columns: msametid, msametfl, ra_center, dec_center, pa_aper,
        gratings, filters, jwst_program, jwst_obs_ids, n_exposures,
        n_dithers, exptime_total, date_obs_start, date_obs_end,
        footprint (4 x 4 x 2 array of [RA, Dec] corners).
    """
    obs_dir = Path(obs_dir)
    cal_files = sorted(obs_dir.glob('*_cal.fits'))
    if not cal_files:
        log(f"No cal files found in {obs_dir}; skipping pointings")
        return Table()

    # Dedup exposures across per-source copies.
    # Cal-file naming: jw{visit}_{expspec}_{expnum}_{detector}_{srcid}_cal.fits
    # The first four parts identify a unique exposure on a unique detector chip.
    seen = {}
    for cal_file in cal_files:
        parts = cal_file.stem.split('_')
        if len(parts) < 5:
            continue
        key = '_'.join(parts[:4])
        if key not in seen:
            seen[key] = cal_file

    if not seen:
        log(f"No valid cal files to read in {obs_dir}")
        return Table()

    # Read headers from one cal file per unique exposure
    exposures = []
    for key, cal_file in sorted(seen.items()):
        try:
            h0 = fits.getheader(cal_file, ext=0)
            h1 = fits.getheader(cal_file, ext=1)
        except Exception as e:
            log(f"Warning: failed to read headers from {cal_file.name}: {e}")
            continue

        msametid = h0.get('MSAMETID')
        if msametid is None:
            continue

        exposures.append({
            'key': key,
            'msametid': int(msametid),
            'msametfl': _strip_source_suffix(h0.get('MSAMETFL', '')),
            'jwst_program': int(h0.get('PROGRAM', 0)),
            'observtn': h0.get('OBSERVTN', ''),
            'visit_id': h0.get('VISIT_ID', ''),
            'grating': h0.get('GRATING', ''),
            'filter': h0.get('FILTER', ''),
            'exptime': float(h0.get('EFFEXPTM', 0.0)),
            'date_obs': h0.get('DATE-OBS', ''),
            'patt_num': h0.get('PATT_NUM'),
            'numdthpt': h0.get('NUMDTHPT'),
            'ra_ref': float(h1.get('RA_REF', 0.0)),
            'dec_ref': float(h1.get('DEC_REF', 0.0)),
            'pa_aper': float(h1.get('PA_APER', 0.0)),
            'detector': key.split('_')[-1],
        })

    if not exposures:
        return Table()

    # Group by MSAMETID. Each group is one pointing.
    groups = defaultdict(list)
    for exp in exposures:
        groups[exp['msametid']].append(exp)

    rows = []
    for msametid, exps in sorted(groups.items()):
        ra_center = float(np.mean([e['ra_ref'] for e in exps]))
        dec_center = float(np.mean([e['dec_ref'] for e in exps]))
        pa_aper = float(np.mean([e['pa_aper'] for e in exps]))

        # Each (visit, exp_spec, exp_num) is one MSA exposure; the two
        # detectors (nrs1, nrs2) record the same exposure simultaneously,
        # so collapse them for n_exposures and exptime accounting.
        unique_exps = {}
        for e in exps:
            exp_key = '_'.join(e['key'].split('_')[:3])
            if exp_key not in unique_exps:
                unique_exps[exp_key] = e
        n_exposures = len(unique_exps)
        exptime_total = float(sum(e['exptime'] for e in unique_exps.values()))

        n_dithers = len({e['patt_num'] for e in exps if e['patt_num']})
        gratings = sorted({e['grating'] for e in exps if e['grating']})
        filters = sorted({e['filter'] for e in exps if e['filter']})
        obs_ids = sorted({e['observtn'] for e in exps if e['observtn']})
        dates = sorted({e['date_obs'] for e in exps if e['date_obs']})
        msametfls = sorted({e['msametfl'] for e in exps if e['msametfl']})
        jwst_program = exps[0]['jwst_program']

        try:
            footprint = _compute_msa_footprint(ra_center, dec_center, pa_aper)
        except Exception as e:
            log(f"Warning: failed to compute footprint for MSAMETID={msametid}: {e}")
            footprint = np.zeros((4, 4, 2))

        rows.append({
            'msametid': msametid,
            'msametfl': msametfls[0] if msametfls else '',
            'ra_center': ra_center,
            'dec_center': dec_center,
            'pa_aper': pa_aper,
            'gratings': ';'.join(gratings),
            'filters': ';'.join(filters),
            'jwst_program': jwst_program,
            'jwst_obs_ids': ';'.join(obs_ids),
            'n_exposures': n_exposures,
            'n_dithers': n_dithers,
            'exptime_total': exptime_total,
            'date_obs_start': dates[0] if dates else '',
            'date_obs_end': dates[-1] if dates else '',
            'footprint': footprint,
        })

    if not rows:
        return Table()

    table = Table(rows=rows, names=list(rows[0].keys()))
    table.meta['obs_name'] = obs_name
    table.meta['field'] = field

    log(f"Generated pointings table: {len(table)} pointings for {obs_name}")
    return table


def write_pointings_ecsv(table, obs_dir, obs_name):
    """Write the pointings table to an ECSV file."""
    obs_dir = Path(obs_dir)
    output_path = obs_dir / f"{obs_name}_pointings.ecsv"
    table.write(output_path, format='ascii.ecsv', overwrite=True)
    log(f"Wrote pointings ECSV: {output_path} ({len(table)} rows)")
    return output_path


def _strip_source_suffix(msametfl):
    """Strip per-source suffix from MSAMETFL.

    Stage2 writes per-source copies named like
    jw{visit}_{expspec}_{expnum}_{detector}_msa_{srcid}.fits;
    return the stem without the trailing _{srcid}.
    """
    if not msametfl:
        return ''
    stem = Path(msametfl).stem
    parts = stem.split('_')
    # Pattern: ..._msa_<srcid>
    if len(parts) >= 2 and parts[-2] == 'msa' and parts[-1].isdigit():
        return '_'.join(parts[:-1]) + '.fits'
    return msametfl


def _compute_msa_footprint(ra, dec, pa_aper):
    """Compute 4-quadrant NIRSpec MSA sky footprint.

    Uses JWST_FOV_plotter.radec_FOV with NIRSpec as the reference
    instrument and pa_aper as the rotation angle. Returns an array
    of shape (4, 4, 2) — four quadrants, each with four [RA, Dec]
    corners.
    """
    from JWST_FOV_plotter.plotter import radec_FOV

    df = radec_FOV(
        ra, dec,
        ref_instr='NIRSpec',
        rot=pa_aper,
        instr_to_plot=['NIRSpec'],
        NIRSpec_MSA=True,
        NIRSpec_IFU=False,
        NIRSpec_fixed_slits=False,
        NIRCam_long_wl=False, NIRCam_short_wl=False, NIRCam_coron=False,
        MIRI_imag=False, MIRI_IFU=False, MIRI_4QPM=False,
        MIRI_Lyot=False, MIRI_slit=False,
        NIRISS_WFSS=False, NIRISS_AMI=False,
    )

    df = df[df['Aperture'].str.startswith('NRS_FULL_MSA')].sort_values('Aperture')
    if len(df) != 4:
        raise ValueError(f"Expected 4 MSA quadrants, got {len(df)}")

    footprint = np.zeros((4, 4, 2))
    for i, (_, row) in enumerate(df.iterrows()):
        for j in range(4):
            footprint[i, j, 0] = row[f'RA_{j+1}']
            footprint[i, j, 1] = row[f'DEC_{j+1}']
    return footprint
