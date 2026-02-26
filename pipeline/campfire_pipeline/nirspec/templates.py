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


def assemble_full_template_grid(continuum_templates, zgrid, template_wav):
    """
    Assemble the full template grid from all component template types.

    Parameters
    ----------
    continuum_templates : dict
        Loaded continuum template grid (from pickle)
    zgrid : ndarray
        Redshift grid
    template_wav : ndarray
        Template wavelength grid in microns

    Returns
    -------
    templates : ndarray, shape (n_templates, n_z, n_wav)
        Stacked template grid
    """
    line_templates = build_emission_line_templates(zgrid, template_wav)
    broadline_templates = build_broadline_templates(zgrid, template_wav)
    blackbody_templates = build_blackbody_templates(zgrid, template_wav)
    mod_blackbody_templates = build_modified_blackbody_templates(zgrid, template_wav)

    templates = np.vstack((
        continuum_templates['grid'],
        line_templates['grid'],
        blackbody_templates['grid'],
        mod_blackbody_templates['grid'],
        broadline_templates['grid'],
    ))

    return templates
