"""
Template grid generation for redshift fitting.

Includes continuum templates (via bagpipes), emission line templates,
broadline templates, blackbody templates, and modified blackbody templates.
"""

import os
import pickle
import numpy as np
import tqdm

from campfire_pipeline.common.spectral import air_to_vac, planck, MBB


# Emission line dictionary: rest wavelengths in Angstroms.
# Lists encode [primary_wav, companion_wav, ratio, ...] for linked lines.
EMISSION_LINES = {
    'Lya': 1215.670,
    'CIV1550d': 1549.480,
    'OIII1663d': 1663.000,
    'CIII1908d': 1908.734,
    'MgII2799d': air_to_vac(2799.117),
    'OII3727d': air_to_vac(3727.424),
    'NeIII3869d': [air_to_vac(3868.760), air_to_vac(3967.470), 0.3],
    'H-gamma': [air_to_vac(4340.471), air_to_vac(4861.333), 1/.47, air_to_vac(6562.819), 2.86/.47],
    'OIII4363': air_to_vac(4363.210),
    'H-beta': [air_to_vac(4861.333), air_to_vac(6562.819), 2.86],
    'OIII5007d': [air_to_vac(4958.911), air_to_vac(5006.843), 2.98],
    'HeI5876': air_to_vac(5875.624),
    'Halpha': air_to_vac(6562.819),
    'NII6585d': [air_to_vac(6548.050), air_to_vac(6583.460), 2.94],
    'SII6716': [air_to_vac(6716.440), air_to_vac(6562.819), 1.0],
    'SII6731': [air_to_vac(6730.810), air_to_vac(6562.819), 1.0],
    'SIII9068d': [air_to_vac(9068.600), air_to_vac(9531.100), 2.5],
    'HeI': air_to_vac(10830.340),
    'Pa-gamma': [air_to_vac(10938.086), 12821.6, 1.0, 18756.1, 1.0],
    'Pa-beta': [12821.6, 18756.1, 1],
    'Pa-alpha': 18756.1,
}

# Broadline species and velocity widths (km/s)
BROADLINE_SPECIES = {
    'H-beta': air_to_vac(4861.333),
    'Halpha': air_to_vac(6562.819),
}
BROADLINE_VELOCITIES = [1500, 3000]


def make_template_grid(z_min=0, z_max=20, dv=500, output_file='templates/continuum_templates.pickle'):
    """
    Generate continuum template grid on a velocity-spaced redshift grid.

    Parameters
    ----------
    z_min : float
        Minimum redshift (default: 0)
    z_max : float
        Maximum redshift (default: 20)
    dv : float
        Velocity spacing in km/s (default: 500)
    output_file : str
        Output pickle file path
    """
    c_kms = 299792.458
    n_steps = int(np.log((1 + z_max) / (1 + z_min)) / (dv / c_kms))
    zgrid = (1 + z_min) * np.exp(np.arange(n_steps) * dv / c_kms) - 1
    spec_wavs = np.linspace(5000, 56000, 3000)
    print(f"Generating template grid: z=[{z_min}, {z_max}], dv={dv} km/s, {len(zgrid)} redshift points")

    import bagpipes as bp
    templates = {
        'age0.5_av0.8_steep': {'age': 0.5, 'tau': 2.0, 'zmet': 0.01, 'av': 0.8, 'delta': -1.5},
        'age1.0_av0.01': {'age': 0.95, 'tau': 0.1, 'zmet': 0.2, 'av': 0.1},
        'age0_av0.01':   {'age': 0,   'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
        'age0_av0.25':   {'age': 0,   'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
        'age0_av0.50':   {'age': 0,   'tau': 0.1, 'zmet': 1.0, 'av': 0.50},
        'age0_av1.00':   {'age': 0,   'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
        'age0.2_av0.01': {'age': 0.2, 'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
        'age0.2_av0.25': {'age': 0.2, 'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
        'age0.2_av0.50': {'age': 0.2, 'tau': 0.5, 'zmet': 1.0, 'av': 0.50},
        'age0.2_av1.00': {'age': 0.2, 'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
        'age0.5_av0.01': {'age': 0.5, 'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
        'age0.5_av0.50': {'age': 0.5, 'tau': 0.3, 'zmet': 0.5, 'av': 0.50},
        'age0.5_av1.00': {'age': 0.5, 'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
        'age0.8_av0.01': {'age': 0.8, 'tau': 0.1, 'zmet': 0.2, 'av': 0.0},
        'age0.8_av0.25': {'age': 0.8, 'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
        'age0.8_av0.50': {'age': 0.8, 'tau': 0.1, 'zmet': 1.0, 'av': 0.50},
    }

    boost_av = 1+3*np.exp(-0.5*(zgrid-2.8)**2/(2)**2)
    from astropy.cosmology import Planck18 as cosmo
    age = cosmo.age(zgrid).to('Gyr').value

    template_grid = np.zeros((len(templates),len(zgrid),len(spec_wavs)))

    for j,template in enumerate(templates):
        print(template)
        for i in tqdm.tqdm(range(len(zgrid))):
            z = zgrid[i]

            model_components = {}
            model_components['redshift'] = z

            model_components['delayed'] = {}
            model_components['delayed']['massformed'] = 9
            Z = templates[template]['zmet']
            model_components['delayed']['metallicity'] = np.interp(z, [0, 10], [Z, Z/2], left=Z, right=Z/2)
            frac = templates[template]['age']
            if frac==0:
                model_components['delayed']['age'] = 0.01
            else:
                model_components['delayed']['age'] = age[i] * frac
            model_components['delayed']['tau'] = templates[template]['tau']

            if 'delta' in templates[template]:
                model_components['dust_atten'] = {}
                model_components['dust_atten']['type'] = 'Salim'
                model_components['dust_atten']['delta'] = templates[template]['delta']
                model_components['dust_atten']['B'] = 0
                model_components['dust_atten']['Av'] = templates[template]['av']
            else:
                model_components['dust_atten'] = {}
                model_components['dust_atten']['type'] = 'Calzetti'
                model_components['dust_atten']['Av'] = templates[template]['av'] * boost_av[i]

            if i==0:
                mgal = bp.model_galaxy(model_components, spec_wavs=spec_wavs)
            else:
                mgal.update(model_components)

            flam = mgal.spectrum[:,1]
            fnu = flam * spec_wavs**2
            fnu = fnu / np.nanmedian(fnu)

            template_grid[j,i,:] = fnu

    output = {
        'templates': list(templates),
        'redshifts': zgrid,
        'wavelengths': spec_wavs/1e4,
        'grid': template_grid
    }
    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
    with open(output_file, 'wb') as outfile:
        pickle.dump(output, outfile)
    print(f"Saved template grid to {output_file} ({template_grid.shape})")


def build_emission_line_templates(zgrid, template_wav):
    """
    Build narrow emission line templates on the redshift grid.

    Parameters
    ----------
    zgrid : ndarray
        Redshift grid
    template_wav : ndarray
        Template wavelength grid in microns

    Returns
    -------
    dict with keys 'templates', 'redshifts', 'wavelengths', 'grid'
    """
    spec_wavs = template_wav * 1e4
    emline_templates = np.zeros((len(EMISSION_LINES), len(zgrid), len(spec_wavs)))
    for j, line in enumerate(EMISSION_LINES):
        for i in range(len(zgrid)):
            z = zgrid[i]
            fnu = np.zeros(len(spec_wavs))
            rest_wav = EMISSION_LINES[line]
            if isinstance(rest_wav, list):
                idx = np.argmin(np.abs(spec_wavs - rest_wav[0]*(1+z)))
                if idx > 1 and idx < len(fnu)-1:
                    fnu[idx] = 1
                extra_wavs = rest_wav[1::2]
                extra_ratios = rest_wav[2::2]
                for extra_num in range(len(extra_wavs)):
                    idx = np.argmin(np.abs(spec_wavs - extra_wavs[extra_num]*(1+z)))
                    if idx > 1 and idx < len(fnu)-1:
                        fnu[idx] = extra_ratios[extra_num]
            else:
                idx = np.argmin(np.abs(spec_wavs - rest_wav*(1+z)))
                if idx > 1 and idx < len(fnu)-1:
                    fnu[idx] = 1
            emline_templates[j, i, :] = fnu

    return {
        'templates': list(EMISSION_LINES),
        'redshifts': zgrid,
        'wavelengths': spec_wavs / 1e4,
        'grid': emline_templates,
    }


def build_broadline_templates(zgrid, template_wav):
    """
    Build broad emission line templates (Gaussian profiles at given velocities).

    Parameters
    ----------
    zgrid : ndarray
        Redshift grid
    template_wav : ndarray
        Template wavelength grid in microns

    Returns
    -------
    dict with keys 'templates', 'redshifts', 'wavelengths', 'grid'
    """
    spec_wavs = template_wav * 1e4
    emlines = BROADLINE_SPECIES
    velocities = BROADLINE_VELOCITIES
    broadline_templates = np.zeros((len(emlines)*len(velocities), len(zgrid), len(spec_wavs)))
    for j, broad in enumerate([[line, velo] for line in emlines for velo in velocities]):
        for i in range(len(zgrid)):
            z = zgrid[i]
            rest_wav = emlines[broad[0]]
            fnu = np.exp(-(rest_wav*(1+z)-spec_wavs)**2/(2*(broad[1]/2.355/3e5*rest_wav*(1+z))**2))
            fnu[fnu < 1e-3] = 0
            broadline_templates[j, i, :] = fnu
    broadline_templates[~np.isfinite(broadline_templates)] = 0
    return {
        'templates': list(emlines),
        'redshifts': zgrid,
        'wavelengths': spec_wavs / 1e4,
        'grid': broadline_templates,
    }


def build_blackbody_templates(zgrid, template_wav, temperatures=None):
    """
    Build blackbody templates with Lyman-alpha break at z>5.7.

    Parameters
    ----------
    zgrid : ndarray
        Redshift grid
    template_wav : ndarray
        Template wavelength grid in microns
    temperatures : list of float, optional
        Blackbody temperatures in K (default: [500, 2500, 5000])

    Returns
    -------
    dict with keys 'templates', 'redshifts', 'wavelengths', 'grid'
    """
    if temperatures is None:
        temperatures = [500, 2500, 5000]
    spec_wavs = template_wav * 1e4
    blackbody_templates = np.zeros((len(temperatures), len(zgrid), len(spec_wavs)))
    for j, temperature in enumerate(temperatures):
        for i in range(len(zgrid)):
            z = zgrid[i]
            fnu = planck(template_wav/(1+z), temperature)
            if z > 5.7:
                fnu[template_wav/(1+z) < 0.121567] = 0
            fnu = fnu / np.max(fnu)
            blackbody_templates[j, i, :] = fnu
    return {
        'templates': list(temperatures),
        'redshifts': zgrid,
        'wavelengths': spec_wavs / 1e4,
        'grid': blackbody_templates,
    }


def build_modified_blackbody_templates(zgrid, template_wav):
    """
    Build modified blackbody templates (single MBB with fixed parameters).

    Uses parameters from https://arxiv.org/pdf/2511.21820

    Parameters
    ----------
    zgrid : ndarray
        Redshift grid
    template_wav : ndarray
        Template wavelength grid in microns

    Returns
    -------
    dict with keys 'templates', 'redshifts', 'wavelengths', 'grid'
    """
    spec_wavs = template_wav * 1e4
    mod_blackbody_templates = np.zeros((1, len(zgrid), len(spec_wavs)))
    for i in range(len(zgrid)):
        z = zgrid[i]
        fnu = MBB(template_wav/(1+z), T=3973, pivot=0.55, beta=0.6656)
        fnu = fnu / np.max(fnu)
        fnu[~np.isfinite(fnu)] = 0
        fnu[fnu < 1e-3] = 0
        mod_blackbody_templates[0, i, :] = fnu
    return {
        'templates': ['MBB_3973K'],
        'redshifts': zgrid,
        'wavelengths': spec_wavs / 1e4,
        'grid': mod_blackbody_templates,
    }


# ---------------------------------------------------------------------------
# sfhz-style rest-frame template architecture
# ---------------------------------------------------------------------------

_C_KMS = 299792.458

# Default redshift bin edges (covering z=0 to z=20)
DEFAULT_Z_BIN_EDGES = np.array([0, 0.5, 1, 2, 3, 4, 5, 7, 9, 12, 15, 20])

# Continuum SED parameter grid — 16 templates spanning age/dust/metallicity
_SFHZ_CONTINUUM_PARAMS = {
    'age0.5_av0.8_steep': {'age': 0.5, 'tau': 2.0, 'zmet': 0.01, 'av': 0.8, 'delta': -1.5},
    'age1.0_av0.01': {'age': 0.95, 'tau': 0.1, 'zmet': 0.2, 'av': 0.1},
    'age0_av0.01':   {'age': 0,   'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
    'age0_av0.25':   {'age': 0,   'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
    'age0_av0.50':   {'age': 0,   'tau': 0.1, 'zmet': 1.0, 'av': 0.50},
    'age0_av1.00':   {'age': 0,   'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
    'age0.2_av0.01': {'age': 0.2, 'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
    'age0.2_av0.25': {'age': 0.2, 'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
    'age0.2_av0.50': {'age': 0.2, 'tau': 0.5, 'zmet': 1.0, 'av': 0.50},
    'age0.2_av1.00': {'age': 0.2, 'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
    'age0.5_av0.01': {'age': 0.5, 'tau': 0.5, 'zmet': 0.2, 'av': 0.0},
    'age0.5_av0.50': {'age': 0.5, 'tau': 0.3, 'zmet': 0.5, 'av': 0.50},
    'age0.5_av1.00': {'age': 0.5, 'tau': 0.1, 'zmet': 1.0, 'av': 1.00},
    'age0.8_av0.01': {'age': 0.8, 'tau': 0.1, 'zmet': 0.2, 'av': 0.0},
    'age0.8_av0.25': {'age': 0.8, 'tau': 0.3, 'zmet': 0.5, 'av': 0.25},
    'age0.8_av0.50': {'age': 0.8, 'tau': 0.1, 'zmet': 1.0, 'av': 0.50},
}


def _make_rest_frame_wave_grid(dv, lam_obs_min=0.54, lam_obs_max=5.6, z_max=20):
    """Build a log-spaced rest-frame wavelength grid in microns.

    The pixel scale is dloglam = dv / c, so one pixel equals one velocity step.
    This ensures the z-shift in the fitting loop is always an integer pixel offset.
    """
    lam_rest_min = lam_obs_min / (1 + z_max)
    lam_rest_max = lam_obs_max
    dloglam = dv / _C_KMS
    n_pix = int(np.log(lam_rest_max / lam_rest_min) / dloglam) + 1
    log_start = np.log(lam_rest_min)
    wave_rest = np.exp(log_start + np.arange(n_pix) * dloglam)
    return wave_rest, dloglam


def make_sfhz_template_grid(z_min=0, z_max=20, dv=500, n_bins=None,
                             z_bin_edges=None, output_file='templates/sfhz_templates.pickle'):
    """Generate rest-frame continuum SEDs on a log-lambda grid for each z-bin.

    For each redshift bin, generates 16 bagpipes SEDs at redshift=0, with
    physical parameters (age, metallicity, dust) appropriate for the bin's
    midpoint redshift. This decouples the template grid from the redshift grid.

    Parameters
    ----------
    z_min : float
        Minimum redshift (default: 0)
    z_max : float
        Maximum redshift (default: 20)
    dv : float
        Velocity spacing in km/s (default: 500)
    n_bins : int, optional
        Number of redshift bins (ignored if z_bin_edges provided)
    z_bin_edges : array-like, optional
        Explicit bin edges (default: DEFAULT_Z_BIN_EDGES clipped to [z_min, z_max])
    output_file : str
        Output pickle file path
    """
    import bagpipes as bp
    from astropy.cosmology import Planck18 as cosmo

    if z_bin_edges is None:
        z_bin_edges = DEFAULT_Z_BIN_EDGES
    z_bin_edges = np.asarray(z_bin_edges, dtype=float)
    # Clip to requested range
    z_bin_edges = z_bin_edges[(z_bin_edges >= z_min) & (z_bin_edges <= z_max)]
    if z_bin_edges[0] > z_min:
        z_bin_edges = np.concatenate([[z_min], z_bin_edges])
    if z_bin_edges[-1] < z_max:
        z_bin_edges = np.concatenate([z_bin_edges, [z_max]])
    n_bins = len(z_bin_edges) - 1

    wave_rest, dloglam = _make_rest_frame_wave_grid(dv, z_max=z_max)
    # Bagpipes uses Angstroms
    spec_wavs_ang = wave_rest * 1e4
    n_rest = len(wave_rest)

    templates = _SFHZ_CONTINUUM_PARAMS
    n_cont = len(templates)
    grid = np.zeros((n_bins, n_cont, n_rest), dtype=np.float32)

    print(f"Generating sfhz template grid: {n_bins} z-bins, {n_cont} SEDs, "
          f"{n_rest} rest-frame pixels, dv={dv} km/s")

    for b in range(n_bins):
        z_lo, z_hi = z_bin_edges[b], z_bin_edges[b + 1]
        z_mid = 0.5 * (z_lo + z_hi)
        age_universe = cosmo.age(z_mid).to('Gyr').value
        boost_av = 1 + 3 * np.exp(-0.5 * (z_mid - 2.8)**2 / 4)

        print(f"  Bin {b}: z=[{z_lo:.1f}, {z_hi:.1f}], z_mid={z_mid:.2f}, "
              f"age_universe={age_universe:.2f} Gyr")

        for j, (name, params) in enumerate(templates.items()):
            model_components = {'redshift': 0}  # rest-frame
            model_components['delayed'] = {
                'massformed': 9,
                'tau': params['tau'],
            }

            Z = params['zmet']
            model_components['delayed']['metallicity'] = np.interp(
                z_mid, [0, 10], [Z, Z / 2], left=Z, right=Z / 2)

            frac = params['age']
            if frac == 0:
                model_components['delayed']['age'] = 0.01
            else:
                model_components['delayed']['age'] = age_universe * frac

            if 'delta' in params:
                model_components['dust_atten'] = {
                    'type': 'Salim', 'delta': params['delta'],
                    'B': 0, 'Av': params['av'],
                }
            else:
                model_components['dust_atten'] = {
                    'type': 'Salim', 'Av': params['av'] * boost_av, 'delta': 0, 'B': 0,
                }

            if j == 0 and b == 0:
                mgal = bp.model_galaxy(model_components, spec_wavs=spec_wavs_ang)
            else:
                mgal.update(model_components)

            flam = mgal.spectrum[:, 1]
            fnu = flam * spec_wavs_ang**2
            med = np.nanmedian(fnu)
            if med > 0:
                fnu = fnu / med
            grid[b, j, :] = fnu

    output = {
        'type': 'sfhz',
        'z_bin_edges': z_bin_edges,
        'continuum_names': list(templates.keys()),
        'wave_rest': wave_rest,
        'dloglam': dloglam,
        'grid': grid,
        'dv': dv,
        'z_min': z_min,
        'z_max': z_max,
    }
    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
    with open(output_file, 'wb') as outfile:
        pickle.dump(output, outfile)
    print(f"Saved sfhz template grid to {output_file} "
          f"(shape={grid.shape}, {grid.nbytes / 1e6:.1f} MB)")


def build_rest_frame_emission_lines(wave_rest, dloglam):
    """Build narrow emission line templates on the rest-frame log-lambda grid.

    Lines are placed as delta functions with sub-pixel positioning: flux is
    split between the two bracketing pixels using linear interpolation weights.

    Parameters
    ----------
    wave_rest : ndarray, shape (n_rest,)
        Rest-frame wavelength grid in microns (log-spaced)
    dloglam : float
        Log-lambda pixel scale

    Returns
    -------
    grid : ndarray, shape (n_lines, n_rest)
    names : list of str
    """
    n_rest = len(wave_rest)
    log_start = np.log(wave_rest[0])
    n_lines = len(EMISSION_LINES)
    grid = np.zeros((n_lines, n_rest), dtype=np.float32)

    for j, (name, rest_wav) in enumerate(EMISSION_LINES.items()):
        if isinstance(rest_wav, list):
            wavs = [rest_wav[0] / 1e4]  # primary in microns
            fluxes = [1.0]
            extra_wavs = rest_wav[1::2]
            extra_ratios = rest_wav[2::2]
            for w, r in zip(extra_wavs, extra_ratios):
                wavs.append(w / 1e4)
                fluxes.append(r)
        else:
            wavs = [rest_wav / 1e4]
            fluxes = [1.0]

        for wav_um, flux_val in zip(wavs, fluxes):
            if wav_um <= 0:
                continue
            # Fractional pixel index
            frac_idx = (np.log(wav_um) - log_start) / dloglam
            idx = int(np.floor(frac_idx))
            if idx < 1 or idx >= n_rest - 2:
                continue
            alpha = frac_idx - idx
            grid[j, idx] += flux_val * (1 - alpha)
            grid[j, idx + 1] += flux_val * alpha

    return grid, list(EMISSION_LINES.keys())


def build_rest_frame_broadlines(wave_rest):
    """Build broad emission line templates on the rest-frame log-lambda grid.

    Gaussian profiles with constant width in log-lambda (velocity space).

    Parameters
    ----------
    wave_rest : ndarray, shape (n_rest,)
        Rest-frame wavelength grid in microns

    Returns
    -------
    grid : ndarray, shape (n_broad, n_rest)
    names : list of str
    """
    n_rest = len(wave_rest)
    log_wav = np.log(wave_rest)
    combos = [(line, velo) for line in BROADLINE_SPECIES for velo in BROADLINE_VELOCITIES]
    n_broad = len(combos)
    grid = np.zeros((n_broad, n_rest), dtype=np.float32)

    for j, (line_name, velocity) in enumerate(combos):
        rest_wav_ang = BROADLINE_SPECIES[line_name]
        rest_wav_um = rest_wav_ang / 1e4
        sigma_loglam = velocity / (_C_KMS * 2.355)
        log_center = np.log(rest_wav_um)
        fnu = np.exp(-(log_wav - log_center)**2 / (2 * sigma_loglam**2))
        fnu[fnu < 1e-3] = 0
        grid[j, :] = fnu

    grid[~np.isfinite(grid)] = 0
    names = [f"{line}_{velo}kms" for line, velo in combos]
    return grid, names


def build_rest_frame_blackbodies(wave_rest, temperatures=None):
    """Build blackbody templates on the rest-frame wavelength grid.

    Includes Lyman break at lambda < 0.1216 um.

    Parameters
    ----------
    wave_rest : ndarray, shape (n_rest,)
        Rest-frame wavelength grid in microns
    temperatures : list of float, optional
        Blackbody temperatures in K (default: [500, 2500, 5000])

    Returns
    -------
    grid : ndarray, shape (n_bb, n_rest)
    names : list of str
    """
    if temperatures is None:
        temperatures = [500, 2500, 5000]
    n_rest = len(wave_rest)
    grid = np.zeros((len(temperatures), n_rest), dtype=np.float32)

    for j, T in enumerate(temperatures):
        fnu = planck(wave_rest, T)
        fnu[wave_rest < 0.121567] = 0  # Lyman break
        max_val = np.max(fnu)
        if max_val > 0:
            fnu = fnu / max_val
        grid[j, :] = fnu

    return grid, [f"BB_{T}K" for T in temperatures]


def build_rest_frame_mbb(wave_rest):
    """Build modified blackbody template on the rest-frame wavelength grid.

    Parameters
    ----------
    wave_rest : ndarray, shape (n_rest,)
        Rest-frame wavelength grid in microns

    Returns
    -------
    grid : ndarray, shape (1, n_rest)
    names : list of str
    """
    fnu = MBB(wave_rest, T=3973, pivot=0.55, beta=0.6656)
    max_val = np.max(fnu)
    if max_val > 0:
        fnu = fnu / max_val
    fnu[~np.isfinite(fnu)] = 0
    fnu[fnu < 1e-3] = 0
    grid = fnu[np.newaxis, :].astype(np.float32)
    return grid, ['MBB_3973K']


def assemble_sfhz_template_grid(sfhz_templates):
    """Assemble continuum and line grids from sfhz-format templates.

    Parameters
    ----------
    sfhz_templates : dict
        Loaded sfhz pickle (with 'type': 'sfhz')

    Returns
    -------
    cont_grid : ndarray, shape (n_bins, n_cont, n_rest)
        Per-bin continuum templates
    line_grid : ndarray, shape (n_lines_bb_broad, n_rest)
        Shared z-independent templates (emission lines + BBs + MBB + broadlines)
    line_names : list of str
        Names for each row of line_grid
    """
    wave_rest = sfhz_templates['wave_rest']
    dloglam = sfhz_templates['dloglam']

    cont_grid = sfhz_templates['grid']  # (n_bins, n_cont, n_rest)

    emline_grid, emline_names = build_rest_frame_emission_lines(wave_rest, dloglam)
    bb_grid, bb_names = build_rest_frame_blackbodies(wave_rest)
    mbb_grid, mbb_names = build_rest_frame_mbb(wave_rest)
    broad_grid, broad_names = build_rest_frame_broadlines(wave_rest)

    line_grid = np.vstack([emline_grid, bb_grid, mbb_grid, broad_grid])
    line_names = emline_names + bb_names + mbb_names + broad_names

    return cont_grid, line_grid, line_names
