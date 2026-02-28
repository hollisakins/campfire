"""
IGM (Intergalactic Medium) attenuation model from Inoue et al. (2014).

Provides redshift-dependent UV absorption for rest-frame wavelengths below
Ly-alpha (1215.67 A). Includes an optional CGM damping wing model
(Asada+24) for z > 6.

The Inoue+2014 grid covers rest-frame 1-1225 A at 2002 redshift points
(z = 0 to 20.01). For wavelengths above the grid, transmission = 1.0.
"""

import os
import numpy as np
import h5py


# Ly-alpha rest wavelength in microns
LYA_REST_UM = 0.121567


def load_igm_grid():
    """Load the Inoue+2014 IGM transmission grid from package data.

    Returns
    -------
    dict with keys:
        'wav_um': (n_wav,) float64 — rest-frame wavelengths in microns
        'redshifts': (n_z,) float64 — redshift grid
        'transmission': (n_wav, n_z) float64 — IGM transmission values [0, 1]
    """
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    igm_file = os.path.join(data_dir, 'inoue14_igm.hdf5')

    with h5py.File(igm_file, 'r') as f:
        wav_ang = f['wavelengths'][:]      # Angstroms, 1-1225
        redshifts = f['redshifts'][:]      # 0-20.01
        transmission = f['transmission'][:, :]  # (n_wav, n_z)

    return {
        'wav_um': np.ascontiguousarray(wav_ang / 1e4, dtype=np.float64),
        'redshifts': np.ascontiguousarray(redshifts, dtype=np.float64),
        'transmission': np.ascontiguousarray(transmission, dtype=np.float64),
    }


def igm_transmission(wave_rest_um, z, igm_data):
    """Compute IGM transmission at given rest-frame wavelengths and redshift.

    Parameters
    ----------
    wave_rest_um : ndarray
        Rest-frame wavelengths in microns
    z : float
        Source redshift
    igm_data : dict
        Output of load_igm_grid()

    Returns
    -------
    T : ndarray, same shape as wave_rest_um
        Transmission values in [0, 1]
    """
    T = np.ones_like(wave_rest_um, dtype=np.float64)

    igm_wav = igm_data['wav_um']
    igm_z = igm_data['redshifts']
    igm_trans = igm_data['transmission']

    # Only affects wavelengths below the IGM grid maximum (~0.1225 um)
    mask = wave_rest_um <= igm_wav[-1]
    if not np.any(mask):
        # Still need to check CGM for z >= 6
        if z >= 6.0:
            T *= cgm_damping_wing(wave_rest_um, z)
        return T

    # Interpolate IGM grid in redshift to get T(lambda) at this z
    iz = np.searchsorted(igm_z, z)
    if iz == 0:
        T_z = igm_trans[:, 0]
    elif iz >= len(igm_z):
        T_z = igm_trans[:, -1]
    else:
        alpha = (z - igm_z[iz - 1]) / (igm_z[iz] - igm_z[iz - 1])
        T_z = (1 - alpha) * igm_trans[:, iz - 1] + alpha * igm_trans[:, iz]

    # Interpolate T(lambda) to requested wavelengths
    T[mask] = np.interp(wave_rest_um[mask], igm_wav, T_z)

    # Apply CGM damping wing for z >= 6
    if z >= 6.0:
        T *= cgm_damping_wing(wave_rest_um, z)

    return T


def cgm_damping_wing(wave_rest_um, z,
                      cgm_A=3.5918, cgm_a=1.8414, cgm_c=18.001):
    """Compute CGM damping wing transmission (Asada+24).

    Models DLA absorption from circumgalactic neutral hydrogen at z >= 6.
    The Lorentzian damping wing of Ly-alpha extends above the line center,
    so this affects wavelengths both below and above 1216 A.

    Parameters
    ----------
    wave_rest_um : ndarray
        Rest-frame wavelengths in microns
    z : float
        Source redshift
    cgm_A, cgm_a, cgm_c : float
        Sigmoid parameters for log10(N_HI) vs z

    Returns
    -------
    T : ndarray, same shape as wave_rest_um
        Transmission values in [0, 1]
    """
    if z < 6.0:
        return np.ones_like(wave_rest_um)

    log10_NHI = cgm_A / (1.0 + np.exp(-cgm_a * (z - 6.0))) + cgm_c
    N_HI = 10.0 ** log10_NHI

    c_ang = 2.99792458e18   # speed of light in Angstroms/s
    wave_rest_ang = wave_rest_um * 1e4
    nu_rest = c_ang / wave_rest_ang

    # Ly-alpha absorption cross-section (Lorentzian damping wing)
    Lam_a = 6.255486e8
    nu_lya = 2.46607e15
    C = 6.9029528e22

    nu_ratio = nu_rest / nu_lya
    sig = (C * nu_ratio**4 /
           (4.0 * np.pi**2 * (nu_rest - nu_lya)**2
            + Lam_a**2 * nu_ratio**6 / 4.0))
    sig *= 1e-16  # cm^2

    return np.exp(-N_HI * sig)
