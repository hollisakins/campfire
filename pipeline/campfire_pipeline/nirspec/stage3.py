"""
Stage 3: Spec3Pipeline, optimal extraction, and 1D combination across exposure groups.
"""

import os
import glob
import warnings
import numpy as np
import matplotlib.pyplot as plt
from typing import List
from datetime import datetime
from astropy.io import fits
from astropy.table import Table, Column
from astropy.io.fits import table_to_hdu
from jwst import associations

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.extraction import (
    boxcar_profile,
    optext_profile,
    extract_with_profile,
    combine_1d_spectra,
)


def run_stage3(obs, stage_config, config, source_ids='all', n_processes=1,
               overwrite=False, data_dir=None, products_dir=None):
    """Orchestrate stage 3: Spec3Pipeline + optimal extraction + optional 1D combine.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage3 configuration.
    config : dict
        Full pipeline config (for pipeline.version).
    source_ids : list or 'all'
    n_processes, overwrite : int, bool
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.observation import Observation

    version = config.get('pipeline', {}).get('version', 'unknown')
    log(f"Stage 3 config for {obs.name}: {stage_config}")

    bkg_subtraction_method = stage_config.get('method', 'nodded')
    s3_kwargs = dict(
        cleanup_asn=stage_config.get('cleanup_asn', True),
        cleanup_crfs=stage_config.get('cleanup_crfs', True),
    )
    plot_profiles = stage_config.get('plot_profiles', True)
    plot_optext = stage_config.get('plot_optext', True)
    combine_method = stage_config.get('combine_method', '2d')

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Discover and group files
    if bkg_subtraction_method == 'nodded':
        files = obs.discover_files(ext='cal_bkgsub', source_ids=source_ids)
    else:
        raise NotImplementedError

    if len(files) == 0:
        log(f"No cal_bkgsub files found for {obs.name}")
        return

    files = Observation.group_files(files)

    # Filter out files where source flux is not present
    files['srcflux'] = [
        fits.getheader(f['path'])['SRCFLUX'] == 'T'
        if 'SRCFLUX' in fits.getheader(f['path']) else True
        for f in files
    ]

    tasks = []
    phase2_tasks = []

    for source_id in np.unique(files['source_id']):
        files1 = files[files['source_id'] == source_id]

        for filter_grating in np.unique(files1['filter_grating']):
            target_files = files1[files1['filter_grating'] == filter_grating]
            target_files = target_files[target_files['srcflux']]

            target_files.pprint()

            if combine_method == '2d':
                product_name = f"{obs.name}_{filter_grating}_{source_id}"
                if len(target_files) == 0:
                    continue

                if os.path.exists(obs.workspace_dir + product_name + "_spec.fits") and not overwrite:
                    continue

                tasks.append((list(target_files['name']), obs.workspace_dir, product_name, source_id))

            elif combine_method == '1d':
                exp_groups = np.unique(target_files['exp_group'])
                for eg in exp_groups:
                    eg_files = target_files[target_files['exp_group'] == eg]
                    if len(eg_files) == 0:
                        continue
                    product_name = f"{obs.name}_{filter_grating}_{source_id}_g{eg}"
                    if os.path.exists(os.path.join(obs.workspace_dir, product_name + "_spec.fits")) and not overwrite:
                        continue
                    tasks.append((list(eg_files['name']), obs.workspace_dir, product_name, source_id))

                final_product_name = f"{obs.name}_{filter_grating}_{source_id}"
                if os.path.exists(os.path.join(obs.workspace_dir, final_product_name + "_spec.fits")) and not overwrite:
                    continue

                per_eg_spec_files = [
                    f"{obs.name}_{filter_grating}_{source_id}_g{eg}_spec.fits"
                    for eg in exp_groups
                ]
                all_cal_files = list(target_files['name'])
                phase2_tasks.append((
                    per_eg_spec_files, all_cal_files,
                    obs.workspace_dir, final_product_name, source_id, filter_grating,
                ))

            else:
                raise ValueError(f"Unknown combine_method '{combine_method}'. Must be '2d' or '1d'.")

    # Phase 1: Spec3Pipeline + optimal extraction
    dispatch(
        run_stage3_single_source,
        tasks,
        n_processes=n_processes,
        use_starmap=True,
        **s3_kwargs,
    )

    optext_tasks = [(task[2], task[1]) for task in tasks]
    dispatch(
        opt_ext_single_source,
        optext_tasks,
        n_processes=n_processes,
        use_starmap=True,
        plot_profiles=plot_profiles,
        plot_optext=plot_optext,
        version=version,
    )

    # Phase 2: 1D combination (combine_method='1d' only)
    if phase2_tasks:
        sigma_clip = stage_config.get('sigma_clip', True)
        sigma_clip_low = stage_config.get('sigma_clip_low', 3.0)
        sigma_clip_high = stage_config.get('sigma_clip_high', 3.0)
        sigma_clip_maxiters = stage_config.get('sigma_clip_maxiters', 5)

        combine_kwargs = dict(
            sigma_clip=sigma_clip,
            sigma_clip_low=sigma_clip_low,
            sigma_clip_high=sigma_clip_high,
            sigma_clip_maxiters=sigma_clip_maxiters,
            plot_profiles=plot_profiles,
            plot_optext=plot_optext,
            version=version,
        )

        log(f"Phase 2: 1D-combining {len(phase2_tasks)} source/grating groups")
        dispatch(
            combine_per_eg_spectra,
            phase2_tasks,
            n_processes=n_processes,
            use_starmap=True,
            **combine_kwargs,
        )

        # Cleanup per-exp_group intermediates
        for per_eg_spec_files, _, workspace_dir, final_product_name, _, _ in phase2_tasks:
            final_path = os.path.join(workspace_dir, final_product_name + "_spec.fits")
            if not os.path.exists(final_path):
                continue
            for eg_spec in per_eg_spec_files:
                eg_base = eg_spec.replace('_spec.fits', '')
                for suffix in ['_spec.fits', '_s2d.fits', '_x1d.fits', '_prof.pdf', '_spec.pdf']:
                    path = os.path.join(workspace_dir, eg_base + suffix)
                    if os.path.exists(path):
                        os.remove(path)
            log(f"Cleaned up per-exp_group intermediates for {final_product_name}")


def run_stage3_single_source(
        cal_files: List,
        workspace_dir: str,
        product_name: str,
        source_id: int,
        cleanup_asn: bool = True,
        cleanup_crfs: bool = True,
    ):
    from jwst.pipeline import Spec3Pipeline


    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        association= [(file, 'science') for file in cal_files]

        asn = associations.asn_from_list.asn_from_list(
            association,
            with_exptype=True,
            product_name=product_name
        )
        suggested_name, serialization = asn.dump()

        asn_file = f'{product_name}_spec3.json'
        with open(asn_file, 'w') as asn_file_out:
            asn_file_out.write(serialization)


        Spec3Pipeline.call(asn_file,
            save_results=True,
            # steps={'extract_1d':{'override_extract1d':('jwst_nirspec_extract1d_4px.json')}}
        )

        if os.path.exists(f'{product_name}_s{source_id:09d}_s2d.fits'):
            os.rename(f'{product_name}_s{source_id:09d}_s2d.fits', f'{product_name}_s2d.fits')
        if os.path.exists(f'{product_name}_s{source_id:09d}_x1d.fits'):
            os.rename(f'{product_name}_s{source_id:09d}_x1d.fits', f'{product_name}_x1d.fits')


        # Create "exposures" table
        hdrs0 = [fits.getheader(cal_file,ext=0) for cal_file in cal_files]
        exposures = Table()
        exposures['filename'] = cal_files
        exposures['dither_type'] = [hdr['PATTTYPE'] for hdr in hdrs0]
        exposures['nod_type'] = [hdr['NOD_TYPE'] for hdr in hdrs0]
        exposures['nod_number'] = [hdr['PRIDTPTS'] for hdr in hdrs0]
        exposures['dither_number'] = [hdr['PATT_NUM'] for hdr in hdrs0]
        exposures['exptime'] = [hdr['EFFEXPTM'] for hdr in hdrs0]
        exposures['stuck_shutter_list'] = [hdr['STKSHTRS'] if 'STKSHTRS' in hdr else 'N/A' for hdr in hdrs0]
        hdrs1 = [fits.getheader(cal_file,ext=1) for cal_file in cal_files]
        exposures['shutter_state'] = [hdr['SHUTSTA'] for hdr in hdrs1]
        exposures['source_ra'] = [hdr['SRCRA'] for hdr in hdrs1]
        exposures['source_dec'] = [hdr['SRCDEC'] for hdr in hdrs1]
        exposures['source_xpos'] = [hdr['SRCXPOS'] for hdr in hdrs1]
        exposures['source_ypos'] = [hdr['SRCYPOS'] for hdr in hdrs1]
        exposures['v3pa'] = [hdr['PA_V3'] for hdr in hdrs1]


        s2d_file = f'{product_name}_s2d.fits'
        with fits.open(s2d_file, mode='update') as hdul:
            exposures = table_to_hdu(exposures)
            exposures.name = 'EXPOSURES'
            hdul.append(exposures)

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:

        if cleanup_asn:
            # Cleanup any remaining ASN files
            asn_file = f'{product_name}_spec3.json'
            if os.path.exists(asn_file):
                os.remove(asn_file)

        if cleanup_crfs:
            for cal_file in cal_files:
                crf_file = cal_file.replace('.fits', '_a3001_crf.fits') # e.g. jw07076019001_03101_00003_nrs1_10059_cal_bkgsub_a3001_crf
                if os.path.exists(crf_file):
                    os.remove(crf_file)

            more_crf_files = glob.glob(f'{product_name}_*_crf.fits') + glob.glob(f'{product_name}_*_cal.fits')
            if len(more_crf_files) > 0:
                for file in more_crf_files:
                    os.remove(file)

        os.chdir(prev_cwd)

def opt_ext_single_source(
      product_name,
      workspace_dir,
      optimal_extraction_profile = 'single', # or 'wavelength-dependent'
      plot_profiles = False,
      plot_optext = False,
      overwrite = False,
      version = 'v0.1',
    ):
    """
    Runs an optimal extraction routine and saves an msaexp-style "_spec.fits" file (combining 2D and 1D) for a single object.
    """
    log(f"Running optimal extraction and compiling *_spec.fits file for {product_name}")

    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    s2d = fits.open(s2d_file)
    x1d = fits.open(x1d_file)
    s2d_sci = s2d['SCI'].data * 1e12 # convert from MJy to uJy
    s2d_err = s2d['ERR'].data * 1e12 # convert from MJy to uJy
    s2d_mask = s2d['WHT'].data==0

    out_filename = s2d_file.replace('_s2d.fits','_spec.fits')

    ph = fits.Header()
    ph['EXTEND'] = 'T'
    ph['FILENAME'] = (os.path.basename(out_filename), 'Name of the file')
    for keyword in ['ORIGIN','TIMESYS','TIMEUNIT','TELESCOP','PROGRAM','PI_NAME','CATEGORY','DATE-OBS',
                    'TIME-OBS','VISIT_ID','OBSERVTN','TARGPROP','TARGNAME','FILTER','GRATING','MSAMETFL',
                    'MSAMETID','MSACONID','EXP_TYPE','READPATT','EFFEXPTM','CAL_VER','CAL_VCS','CRDS_VER',
                    'CRDS_CTX','R_AREA','R_CAMERA','R_COLLIM','R_DARK','R_DISPER','R_DISTOR','R_EXTR1D',
                    'R_FILOFF','R_FLAT','R_DFLAT','R_FFLAT','R_SFLAT','R_FORE','R_FPA','R_GAIN','R_IFUFOR',
                    'R_IFUPOS','R_IFUSLI','R_LINEAR','R_MASK','R_MSA','R_OTE','R_PTHLOS','R_PHOTOM','R_READNO',
                    'R_REFPIX','R_REGION','R_SATURA','R_SPCWCS','R_SUPERB','R_WAVCOR','R_WAVRAN','S_WCS',
                    'S_BKDSUB','S_BPXSLF','S_BARSHA','S_CHGMIG','S_CLNFNS','S_DARK','S_DQINIT','S_EXTR1D',
                    'S_EXTR2D','S_FLAT','S_GANSCL','S_GRPSCL','S_IMPRNT','S_IPC','S_JUMP','S_LINEAR',
                    'S_MSBSUB','S_MSAFLG','S_NSCLEN','S_OUTLIR','S_PTHLOS','S_PHOTOM','S_PXREPL','S_RAMP',
                    'S_REFPIX','S_RESAMP','S_SATURA','S_SRCTYP','S_SUPERB','S_WAVCOR','NDRIZ','RESWHT',
                    'PIXFRAC','PXSCLRT']:
        try:
            ph[keyword] = (x1d['PRIMARY'].header[keyword], x1d['PRIMARY'].header.comments[keyword])
        except KeyError:
            log(f'Keyword {keyword} not found')
            continue

    ph['CMPFRTIM'] = (str(datetime.now()), 'Date/time of CAMPFIRE reduction')
    ph['CMPFRVER'] = (version, 'Version of CAMPFIRE reduction')

    primary = fits.PrimaryHDU(header=ph)

    # Optimal extraction
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR']-1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    cen = (x1d_start+x1d_stop)/2
    profile_opt = optext_profile(collapsed, x1d_start, x1d_stop)
    if all(np.isnan(profile_opt)):
        # fallback to 3px boxcar
        profile_opt = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))

    fnu_opt, fnu_opt_err = extract_with_profile(profile_opt, s2d_sci, s2d_err, mask=s2d_mask, ivw=True)

    # 3 pixel boxcar, centered on the expected position of the source
    profile_3px = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))
    fnu_3px, fnu_3px_err = extract_with_profile(profile_3px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 4 pixel boxcar, centered on the expected position of the source
    profile_4px = boxcar_profile(cen-2.0, cen+2.0, len(collapsed))
    fnu_4px, fnu_4px_err = extract_with_profile(profile_4px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 5 pixel boxcar, centered on the expected position of the source
    profile_5px = boxcar_profile(cen-2.5, cen+2.5, len(collapsed))
    fnu_5px, fnu_5px_err = extract_with_profile(profile_5px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    if plot_profiles:
        fig, axes = plt.subplots(1,4,figsize=(10,2))
        for ax, prof, label in zip(axes, [profile_opt, profile_3px, profile_4px, profile_5px], ['Optimal', '3px boxcar', '4px boxcar', '5px boxcar']):
            ax.stairs(collapsed/np.nanmax(collapsed), np.arange(len(collapsed)+1), color='k', zorder=1000)
            ax.set_ylim(*ax.get_ylim())
            ax.stairs(prof/np.nanmax(prof), np.arange(len(collapsed)+1), color='tab:red')
            ax.stairs(prof/np.nanmax(prof), np.arange(len(collapsed)+1), color='tab:red', fill=True, alpha=0.2)
            ax.axhline(0, color='0.3', linewidth=0.5, linestyle='--')
            ax.axvline(x1d_start, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(x1d_stop, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(cen, linewidth=0.5, color='b', linestyle=':')
            ax.set_title(label)

        plt.savefig(out_filename.replace('_spec.fits','_prof.pdf'))
        plt.close()


    wave = x1d['EXTRACT1D'].data['WAVELENGTH']

    def fnu_to_flam(fnu, wave): # Assumed fnu in uJy and wave in um
        return fnu / wave**2 * 2.99792458e-19

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=wave, description='Wavelength', unit='um'))
    spec1d.add_column(Column(name='fnu', data=fnu_opt, description='Optimally extracted flux, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='fnu_err', data=fnu_opt_err, description='Optimally extracted flux error, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='flam', data=fnu_to_flam(fnu_opt, wave=wave), description='Optimally extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
    spec1d.add_column(Column(name='flam_err', data=fnu_to_flam(fnu_opt_err, wave=wave), description='Optimally extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))

    for size, fnu, fnu_err in zip(['3px','4px','5px'],[fnu_3px,fnu_4px,fnu_5px],[fnu_3px_err,fnu_4px_err,fnu_5px_err]):
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu, description=f'{size} boxcar extracted flux, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_err, description=f'{size} boxcar extracted flux error, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=fnu_to_flam(fnu, wave=wave), description=f'{size} boxcar extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=fnu_to_flam(fnu_err, wave=wave), description=f'{size} boxcar extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))

    # indices = np.where(fnu_3px_err!=0)[0]
    # spec1d = spec1d[indices[0]:indices[-1] + 1]

    spec1d = table_to_hdu(spec1d)
    spec1d.name = 'SPEC1D'

    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed))+0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    prof1d = table_to_hdu(prof1d)
    prof1d.name = 'PROF1D'

    s2dh = fits.Header()
    for keyword in ['EXTNAME', 'SRCTYPE','XPOSURE','PHOTMJSR','PHOTUJA2','PIXAR_SR','PIXAR_A2','DISPAXIS',
                    'SPORDER','SOURCEID','STLARITY','SRCRA','SRCDEC','WAVECOR','PTHLOSS']:
        if keyword in s2d['SCI'].header:
            s2dh[keyword] = (s2d['SCI'].header[keyword], s2d['SCI'].header.comments[keyword])

    sci = fits.ImageHDU(data=s2d['SCI'].data, header=s2dh, name='SCI')
    err = fits.ImageHDU(data=s2d['ERR'].data, header=s2dh, name='ERR')
    wav = fits.ImageHDU(data=s2d['WAVELENGTH'].data, header=s2dh, name='WAVELENGTH')
    wht = fits.ImageHDU(data=s2d['WHT'].data, header=s2dh, name='WHT')
    # prof = Table({''})
    exp = s2d['EXPOSURES']


    spec = fits.HDUList(hdus=[primary, spec1d, sci, err, wav, wht, prof1d, exp])
    spec.writeto(out_filename, overwrite=True)


    # plt.step(x1d[1].data['WAVELENGTH'], fnu*1.2, where='mid', label='NEW')
    # plt.step(msaexp['wave'], msaexp['flux'], label='msaexp')
    # # plt.step(x1d[1].data['WAVELENGTH'], x1d[1].data['FLUX']*1e6*1.3, where='mid', label='pipeline default')
    # plt.loglog()
    # plt.show()

    if plot_optext:
        import matplotlib as mpl
        from astropy.stats import sigma_clipped_stats
        from astropy.utils.exceptions import AstropyWarning


        # Extract data
        wave = spec1d.data['wave']
        fnu = spec1d.data['fnu']
        fnu_err = spec1d.data['fnu_err']
        flam = spec1d.data['flam']
        flam_err = spec1d.data['flam_err']

        valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)

        # Create figure with 3 rows: 2D spectrum, f_nu, f_lambda
        fig = plt.figure(figsize=(8,6), constrained_layout=True, dpi=300)
        gs = mpl.gridspec.GridSpec(nrows=3, ncols=2, width_ratios=[9,1],
                                  height_ratios=[1,2.5,2.5], figure=fig)

        ax_2d = plt.subplot(gs[0,0])
        ax_1d_fnu = plt.subplot(gs[1,0])
        ax_1d_flam = plt.subplot(gs[2,0])
        ax_prof = plt.subplot(gs[0,1])

        # 2D spectrum with S/N calculation
        sci = s2d['SCI'].data
        err = s2d['ERR'].data

        nsci = sci/err
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=AstropyWarning)
            std = sigma_clipped_stats(nsci, sigma=3)[2]
        snr_2d = nsci / std

        # S/N range and colormap
        vmin, vmax = -3, 8
        cmap = plt.colormaps['viridis']
        cmap.set_bad('0.8')

        im = ax_2d.pcolormesh(wave, prof1d.data['ypos']-cen, snr_2d,
                             vmin=vmin, vmax=vmax, cmap=cmap, rasterized=True)
        ax_2d.set_ylabel('$y$ [pix]')
        ax_2d.set_ylim(-10, 10)
        ax_2d.minorticks_on()
        ax_2d.tick_params(direction='in', which='both', axis='y')

        # Dual 1D spectrum plots
        valid = np.isfinite(fnu) & np.isfinite(fnu_err)

        # f_ν plot
        ax_1d_fnu.step(wave, fnu, where='mid', color='k', linewidth=1)
        ax_1d_fnu.fill_between(wave, (fnu - fnu_err), (fnu + fnu_err),
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
        ax_1d_fnu.set_ylabel(r'$f_{\nu}$ [μJy]')

        # f_λ plot
        ax_1d_flam.step(wave, flam, where='mid', color='k', linewidth=1)
        ax_1d_flam.fill_between(wave, (flam - flam_err), (flam + flam_err),
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
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
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            p = np.nanmedian(sci, axis=1)
            p /= np.nanmax(p[prof1d.data['opt']!=0])
        ax_prof.step(p, prof1d.data['ypos']-cen, where='post', color='k')
        ax_prof.fill_betweenx(prof1d.data['ypos']-cen, np.zeros_like(prof1d.data['ypos']), prof1d.data['opt']/np.nanmax(prof1d.data['opt']),
                             facecolor='r', alpha=0.3, edgecolor='none', step='pre')
        ax_prof.axvline(0, color='k', linewidth=1, zorder=-1000, alpha=0.2)
        ax_prof.minorticks_on()
        ax_prof.set_xlim(-0.3, 1.2)
        ax_prof.set_ylim(-10, 10)
        ax_prof.tick_params(labelbottom=False, bottom=False, labelleft=False,
                           direction='in', which='both')

        # Smart x and y limits
        xmin = wave.min()
        xmax = wave.max()
        ax_2d.set_xlim(xmin, xmax)
        ax_1d_fnu.set_xlim(xmin, xmax)
        ax_1d_flam.set_xlim(xmin, xmax)

        # Percentile-based y-limits
        ymax = np.nanpercentile(fnu+fnu_err, 97)*1.2
        ax_1d_fnu.set_ylim(-0.1*ymax, ymax)
        ymax = np.nanpercentile(flam+flam_err, 97)*1.2
        ax_1d_flam.set_ylim(-0.1*ymax, ymax)

        fig.suptitle(product_name+'_spec', fontname='monospace')

        plt.savefig(out_filename.replace('_spec.fits','_spec.pdf'))
        plt.close()

    s2d.close()
    x1d.close()

def combine_per_eg_spectra(
    per_eg_spec_files,
    all_cal_files,
    workspace_dir, product_name, source_id, filter_grating,
    sigma_clip=True, sigma_clip_low=3.0, sigma_clip_high=3.0,
    sigma_clip_maxiters=5, plot_profiles=True, plot_optext=True, version='v0.1',
):
    """
    1D combination: combine per-exp_group 1D spectra (exposure-time weighted) + produce stacked 2D.

    Parameters
    ----------
    per_eg_spec_files : list of str
        Basenames of per-exp_group _spec.fits files
    all_cal_files : list of str
        All cal_bkgsub filenames for producing the stacked 2D
    workspace_dir : str
        Working directory
    product_name : str
        Output product name (without extension)
    source_id : int
        Source ID
    filter_grating : str
        Filter/grating combination string
    """
    log(f"Stage3 (1D combine): combining {len(per_eg_spec_files)} per-exp_group spectra for {product_name}")

    # --- Step 1: Read individual 1D spectra from each per-exp_group _spec.fits ---
    extraction_cols = {
        'opt': ('fnu', 'fnu_err'),
        '3px': ('fnu_3px', 'fnu_3px_err'),
        '4px': ('fnu_4px', 'fnu_4px_err'),
        '5px': ('fnu_5px', 'fnu_5px_err'),
    }

    per_eg_data = {key: {'wavelengths': [], 'fluxes': [], 'errors': []} for key in extraction_cols}
    exposure_times = []

    for spec_file in per_eg_spec_files:
        spec_path = os.path.join(workspace_dir, spec_file)
        with fits.open(spec_path) as hdul:
            exposure_times.append(float(hdul['PRIMARY'].header.get('EFFEXPTM', 1.0)))
            spec1d = hdul['SPEC1D'].data
            wave = spec1d['wave']
            for key, (flux_col, err_col) in extraction_cols.items():
                per_eg_data[key]['wavelengths'].append(wave.copy())
                per_eg_data[key]['fluxes'].append(spec1d[flux_col].copy())
                per_eg_data[key]['errors'].append(spec1d[err_col].copy())

    # --- Step 2: Stacked 2D via Spec3Pipeline on ALL cal_bkgsub files ---
    run_stage3_single_source(all_cal_files, workspace_dir, product_name, source_id)

    # --- Step 3: Get common wavelength grid from stacked x1d ---
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d = fits.open(x1d_file)
    s2d = fits.open(s2d_file)
    common_wave = x1d['EXTRACT1D'].data['WAVELENGTH'].copy()

    # --- Step 4: Combine ALL extraction columns via exposure-time weighting ---
    combined = {}
    for key in extraction_cols:
        c_flux, c_err, c_n = combine_1d_spectra(
            per_eg_data[key]['wavelengths'],
            per_eg_data[key]['fluxes'],
            per_eg_data[key]['errors'],
            exposure_times,
            common_wave,
            sigma_clip_enabled=sigma_clip,
            sigma_clip_low=sigma_clip_low,
            sigma_clip_high=sigma_clip_high,
            sigma_clip_maxiters=sigma_clip_maxiters,
        )
        combined[key] = (c_flux, c_err, c_n)

    # --- Step 5: Extraction profiles from stacked 2D (for visualization) ---
    s2d_sci = s2d['SCI'].data * 1e12  # MJy to uJy
    s2d_err = s2d['ERR'].data * 1e12
    s2d_mask = s2d['WHT'].data == 0

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR'] - 1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    cen = (x1d_start + x1d_stop) / 2
    profile_opt = optext_profile(collapsed, x1d_start, x1d_stop)
    if all(np.isnan(profile_opt)):
        profile_opt = boxcar_profile(cen - 1.5, cen + 1.5, len(collapsed))
    profile_3px = boxcar_profile(cen - 1.5, cen + 1.5, len(collapsed))
    profile_4px = boxcar_profile(cen - 2.0, cen + 2.0, len(collapsed))
    profile_5px = boxcar_profile(cen - 2.5, cen + 2.5, len(collapsed))

    if plot_profiles:
        fig, axes = plt.subplots(1, 4, figsize=(10, 2))
        for ax, prof, label in zip(axes, [profile_opt, profile_3px, profile_4px, profile_5px],
                                   ['Optimal', '3px boxcar', '4px boxcar', '5px boxcar']):
            ax.stairs(collapsed / np.nanmax(collapsed), np.arange(len(collapsed) + 1), color='k', zorder=1000)
            ax.set_ylim(*ax.get_ylim())
            ax.stairs(prof / np.nanmax(prof), np.arange(len(collapsed) + 1), color='tab:red')
            ax.stairs(prof / np.nanmax(prof), np.arange(len(collapsed) + 1), color='tab:red', fill=True, alpha=0.2)
            ax.axhline(0, color='0.3', linewidth=0.5, linestyle='--')
            ax.axvline(x1d_start, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(x1d_stop, linewidth=0.5, color='b', linestyle=':')
            ax.axvline(cen, linewidth=0.5, color='b', linestyle=':')
            ax.set_title(label)
        out_filename = os.path.join(workspace_dir, f'{product_name}_spec.fits')
        plt.savefig(out_filename.replace('_spec.fits', '_prof.pdf'))
        plt.close()

    # --- Step 6: Package final _spec.fits ---
    # PRIMARY header
    ph = fits.Header()
    ph['EXTEND'] = 'T'
    ph['FILENAME'] = (f'{product_name}_spec.fits', 'Name of the file')
    for keyword in ['ORIGIN', 'TIMESYS', 'TIMEUNIT', 'TELESCOP', 'PROGRAM', 'PI_NAME', 'CATEGORY', 'DATE-OBS',
                    'TIME-OBS', 'VISIT_ID', 'OBSERVTN', 'TARGPROP', 'TARGNAME', 'FILTER', 'GRATING', 'MSAMETFL',
                    'MSAMETID', 'MSACONID', 'EXP_TYPE', 'READPATT', 'EFFEXPTM', 'CAL_VER', 'CAL_VCS', 'CRDS_VER',
                    'CRDS_CTX', 'R_AREA', 'R_CAMERA', 'R_COLLIM', 'R_DARK', 'R_DISPER', 'R_DISTOR', 'R_EXTR1D',
                    'R_FILOFF', 'R_FLAT', 'R_DFLAT', 'R_FFLAT', 'R_SFLAT', 'R_FORE', 'R_FPA', 'R_GAIN', 'R_IFUFOR',
                    'R_IFUPOS', 'R_IFUSLI', 'R_LINEAR', 'R_MASK', 'R_MSA', 'R_OTE', 'R_PTHLOS', 'R_PHOTOM', 'R_READNO',
                    'R_REFPIX', 'R_REGION', 'R_SATURA', 'R_SPCWCS', 'R_SUPERB', 'R_WAVCOR', 'R_WAVRAN', 'S_WCS',
                    'S_BKDSUB', 'S_BPXSLF', 'S_BARSHA', 'S_CHGMIG', 'S_CLNFNS', 'S_DARK', 'S_DQINIT', 'S_EXTR1D',
                    'S_EXTR2D', 'S_FLAT', 'S_GANSCL', 'S_GRPSCL', 'S_IMPRNT', 'S_IPC', 'S_JUMP', 'S_LINEAR',
                    'S_MSBSUB', 'S_MSAFLG', 'S_NSCLEN', 'S_OUTLIR', 'S_PTHLOS', 'S_PHOTOM', 'S_PXREPL', 'S_RAMP',
                    'S_REFPIX', 'S_RESAMP', 'S_SATURA', 'S_SRCTYP', 'S_SUPERB', 'S_WAVCOR', 'NDRIZ', 'RESWHT',
                    'PIXFRAC', 'PXSCLRT']:
        try:
            ph[keyword] = (x1d['PRIMARY'].header[keyword], x1d['PRIMARY'].header.comments[keyword])
        except KeyError:
            continue
    ph['CMPFRTIM'] = (str(datetime.now()), 'Date/time of CAMPFIRE reduction')
    ph['CMPFRVER'] = (version, 'Version of CAMPFIRE reduction')
    ph['CMPFRSTG'] = ('stage3-1d', 'CAMPFIRE stage that produced this file')
    ph['NCOMBINE'] = (len(per_eg_spec_files), 'Number of per-exp_group spectra combined')
    primary = fits.PrimaryHDU(header=ph)

    # SPEC1D: 1D-combined columns
    def fnu_to_flam(fnu, wave):
        return fnu / wave**2 * 2.99792458e-19

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=common_wave, description='Wavelength', unit='um'))

    fnu_opt, fnu_opt_err, n_opt = combined['opt']
    spec1d.add_column(Column(name='fnu', data=fnu_opt, description='1D-combined optimally extracted flux', unit='uJy'))
    spec1d.add_column(Column(name='fnu_err', data=fnu_opt_err, description='1D-combined optimally extracted flux error', unit='uJy'))
    spec1d.add_column(Column(name='flam', data=fnu_to_flam(fnu_opt, common_wave), description='1D-combined optimal flux, flam', unit='erg / (s cm2 Angstrom)'))
    spec1d.add_column(Column(name='flam_err', data=fnu_to_flam(fnu_opt_err, common_wave), description='1D-combined optimal flux error, flam', unit='erg / (s cm2 Angstrom)'))

    for size in ['3px', '4px', '5px']:
        fnu_box, fnu_box_err, _ = combined[size]
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu_box, description=f'1D-combined {size} boxcar flux', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_box_err, description=f'1D-combined {size} boxcar flux error', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=fnu_to_flam(fnu_box, common_wave), description=f'1D-combined {size} boxcar flux, flam', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=fnu_to_flam(fnu_box_err, common_wave), description=f'1D-combined {size} boxcar flux error, flam', unit='erg / (s cm2 Angstrom)'))

    spec1d.add_column(Column(name='n_combined', data=n_opt, description='Number of spectra combined per pixel'))

    spec1d_hdu = table_to_hdu(spec1d)
    spec1d_hdu.name = 'SPEC1D'

    # PROF1D
    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed)) + 0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    prof1d_hdu = table_to_hdu(prof1d)
    prof1d_hdu.name = 'PROF1D'

    # 2D extensions from stacked s2d
    s2dh = fits.Header()
    for keyword in ['EXTNAME', 'SRCTYPE', 'XPOSURE', 'PHOTMJSR', 'PHOTUJA2', 'PIXAR_SR', 'PIXAR_A2', 'DISPAXIS',
                    'SPORDER', 'SOURCEID', 'STLARITY', 'SRCRA', 'SRCDEC', 'WAVECOR', 'PTHLOSS']:
        if keyword in s2d['SCI'].header:
            s2dh[keyword] = (s2d['SCI'].header[keyword], s2d['SCI'].header.comments[keyword])

    sci_hdu = fits.ImageHDU(data=s2d['SCI'].data, header=s2dh, name='SCI')
    err_hdu = fits.ImageHDU(data=s2d['ERR'].data, header=s2dh, name='ERR')
    wav_hdu = fits.ImageHDU(data=s2d['WAVELENGTH'].data, header=s2dh, name='WAVELENGTH')
    wht_hdu = fits.ImageHDU(data=s2d['WHT'].data, header=s2dh, name='WHT')
    exp_hdu = s2d['EXPOSURES']

    out_filename = os.path.join(workspace_dir, f'{product_name}_spec.fits')
    spec = fits.HDUList(hdus=[primary, spec1d_hdu, sci_hdu, err_hdu, wav_hdu, wht_hdu, prof1d_hdu, exp_hdu])
    spec.writeto(out_filename, overwrite=True)
    log(f"Stage3 (1D combine): wrote {out_filename}")

    # Diagnostic plot
    if plot_optext:
        import matplotlib as mpl
        from astropy.stats import sigma_clipped_stats
        from astropy.utils.exceptions import AstropyWarning

        wave = common_wave
        fnu = fnu_opt
        fnu_err = fnu_opt_err
        flam = fnu_to_flam(fnu_opt, common_wave)
        flam_err = fnu_to_flam(fnu_opt_err, common_wave)

        fig = plt.figure(figsize=(8, 6), constrained_layout=True, dpi=300)
        gs = mpl.gridspec.GridSpec(nrows=3, ncols=2, width_ratios=[9, 1],
                                  height_ratios=[1, 2.5, 2.5], figure=fig)
        ax_2d = plt.subplot(gs[0, 0])
        ax_1d_fnu = plt.subplot(gs[1, 0])
        ax_1d_flam = plt.subplot(gs[2, 0])
        ax_prof = plt.subplot(gs[0, 1])

        sci_data = s2d['SCI'].data
        err_data = s2d['ERR'].data
        nsci = sci_data / err_data
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=AstropyWarning)
            std = sigma_clipped_stats(nsci, sigma=3)[2]
        snr_2d = nsci / std

        vmin, vmax = -3, 8
        cmap = plt.colormaps['viridis']
        cmap.set_bad('0.8')

        im = ax_2d.pcolormesh(wave, prof1d['ypos'] - cen, snr_2d,
                             vmin=vmin, vmax=vmax, cmap=cmap, rasterized=True)
        ax_2d.set_ylabel('$y$ [pix]')
        ax_2d.set_ylim(-10, 10)
        ax_2d.minorticks_on()
        ax_2d.tick_params(direction='in', which='both', axis='y')

        valid = np.isfinite(fnu) & np.isfinite(fnu_err)

        ax_1d_fnu.step(wave, fnu, where='mid', color='k', linewidth=1)
        ax_1d_fnu.fill_between(wave, fnu - fnu_err, fnu + fnu_err,
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
        ax_1d_fnu.set_ylabel(r'$f_{\nu}$ [μJy]')

        ax_1d_flam.step(wave, flam, where='mid', color='k', linewidth=1)
        ax_1d_flam.fill_between(wave, flam - flam_err, flam + flam_err,
            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
        ax_1d_flam.set_ylabel(r'$f_{\lambda}$ [erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$]')
        ax_1d_flam.set_xlabel('Observed Wavelength [μm]')

        ax_1d_fnu.grid(True, alpha=0.2, linewidth=1, zorder=-1000)
        ax_1d_flam.grid(True, alpha=0.2, linewidth=1, zorder=-1000)
        ax_1d_fnu.minorticks_on()
        ax_1d_flam.minorticks_on()
        ax_1d_fnu.tick_params(direction='in', which='both')
        ax_1d_flam.tick_params(direction='in', which='both')

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            p = np.nanmedian(sci_data, axis=1)
            p /= np.nanmax(p[profile_opt != 0])
        ax_prof.step(p, prof1d['ypos'] - cen, where='post', color='k')
        ax_prof.fill_betweenx(prof1d['ypos'] - cen, np.zeros_like(prof1d['ypos']),
                             profile_opt / np.nanmax(profile_opt),
                             facecolor='r', alpha=0.3, edgecolor='none', step='pre')
        ax_prof.axvline(0, color='k', linewidth=1, zorder=-1000, alpha=0.2)
        ax_prof.minorticks_on()
        ax_prof.set_xlim(-0.3, 1.2)
        ax_prof.set_ylim(-10, 10)
        ax_prof.tick_params(labelbottom=False, bottom=False, labelleft=False,
                           direction='in', which='both')

        xmin = wave.min()
        xmax = wave.max()
        ax_2d.set_xlim(xmin, xmax)
        ax_1d_fnu.set_xlim(xmin, xmax)
        ax_1d_flam.set_xlim(xmin, xmax)

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            ymax = np.nanpercentile(fnu + fnu_err, 97) * 1.2
            ax_1d_fnu.set_ylim(-0.1 * ymax, ymax)
            ymax = np.nanpercentile(flam + flam_err, 97) * 1.2
            ax_1d_flam.set_ylim(-0.1 * ymax, ymax)

        fig.suptitle(product_name + '_spec (1D combine)', fontname='monospace')

        plt.savefig(out_filename.replace('_spec.fits', '_spec.pdf'))
        plt.close()

    x1d.close()
    s2d.close()
