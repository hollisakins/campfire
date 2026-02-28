"""
Stage 2: WCS assignment (2a) and nodded background subtraction (2b).
"""

import os
import glob
import warnings
import numpy as np
from typing import List
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.metafile import MetaFile
from campfire_pipeline.nirspec.observation import Observation


def run_stage2a(obs, stage_config, source_ids='all', overwrite=False,
                n_processes=1, plot=True, data_dir=None, products_dir=None):
    """Orchestrate stage 2a: WCS assignment + unit fixing + optional resampling/plotting.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage2 configuration.
    source_ids : list or 'all'
    overwrite, plot : bool
    n_processes : int
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.plots import plot_stage2a_results

    log(f"Stage 2a config for {obs.name}: {stage_config}")

    kwargs = dict(
        set_stellarity=stage_config.get('set_stellarity'),
        source_ids=source_ids,
        overwrite=overwrite,
    )

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Run Spec2Pipeline on each rate file
    results = dispatch(
        run_stage2a_single_rate,
        obs.rate_files,
        n_processes=n_processes,
        obs=obs,
        **kwargs,
    )

    # Flatten source IDs from all workers
    source_ids_processed = list(set(sid for result in results for sid in result))

    # Discover cal files and fix units
    files = obs.discover_files(ext='cal', source_ids=source_ids_processed)
    files = Observation.group_files(files)

    dispatch(fix_units, list(files), n_processes=n_processes)

    if plot:
        dispatch(resample_single_exposure, list(files), n_processes=n_processes)
        plot_inputs = [files[files['source_id'] == sid] for sid in source_ids_processed]
        dispatch(plot_stage2a_results, plot_inputs, n_processes=n_processes)


def run_stage2b(obs, stage_config, source_ids='all', overwrite=False,
                n_processes=1, data_dir=None, products_dir=None):
    """Orchestrate stage 2b: nodded background subtraction.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage2 configuration.
    source_ids : list or 'all'
    overwrite : bool
    n_processes : int
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.plots import plot_stage2a_results

    log(f"Stage 2b config for {obs.name}: {stage_config}")

    rectify = stage_config.get('rectify', True)
    plot_bkgsub = stage_config.get('plot_bkgsub', False)

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Discover and group cal files
    files = obs.discover_files(ext='cal', source_ids=source_ids)
    if len(files) == 0:
        log(f"No cal files found for {obs.name}")
        return
    files = Observation.group_files(files)

    # Build task list by iterating over bkg_groups
    tasks = []
    for bg in np.unique(files['bkg_group']):
        bg_files = files[files['bkg_group'] == bg]
        source_id = bg_files['source_id'][0]
        root = bg_files['root'][0]
        detector = bg_files['detector'][0]

        # Skip check: do products already exist?
        all_bkgsub_exist = all(
            os.path.exists(f['path'].replace('_cal.fits', '_cal_bkgsub.fits'))
            for f in bg_files
        )
        if rectify:
            all_s2d_exist = all(
                os.path.exists(f['path'].replace('_cal.fits', '_s2d_bkgsub.fits'))
                for f in bg_files
            )
            skip = all_bkgsub_exist and all_s2d_exist and not overwrite
        else:
            skip = all_bkgsub_exist and not overwrite

        if skip:
            log(f'ID{source_id}: bkgsub products exist for {root}_*_{detector}_{source_id}, skipping (overwrite=False)')
            continue

        bkg_overrides = obs.bkg_overrides.get(root)
        if bkg_overrides is not None:
            bkg_overrides = bkg_overrides.get(str(source_id))

        log(f'ID{source_id}: Running stage2b for bkg_group {bg} ({root}_{detector})')
        tasks.append((list(bg_files['name']), obs.workspace_dir, bkg_overrides))

    # Execute
    dispatch(
        run_stage2b_single_slitlet,
        tasks,
        n_processes=n_processes,
        use_starmap=True,
        rectify=rectify,
    )

    # Optional: plot background-subtracted 2D cutouts
    if plot_bkgsub and rectify:
        bkgsub_files = obs.discover_files(ext='cal_bkgsub', source_ids=source_ids)
        if len(bkgsub_files) > 0:
            bkgsub_files = Observation.group_files(bkgsub_files)
            source_ids_done = np.unique(bkgsub_files['source_id'])
            plot_tasks = [
                (bkgsub_files[bkgsub_files['source_id'] == sid], 'bkgsub')
                for sid in source_ids_done
            ]
            dispatch(
                plot_stage2a_results,
                plot_tasks,
                n_processes=n_processes,
                use_starmap=True,
            )


def run_stage2a_single_rate(
        rate_file,
        obs: Observation,
        set_stellarity: bool = False,
        source_ids='all',
        overwrite: bool = False,
        **kwargs
    ):

    log(f'Starting stage2a for {os.path.basename(rate_file)}')

    from jwst.pipeline import Spec2Pipeline
    from jwst.associations import asn_from_list
    from jwst.assign_wcs.util import NoDataOnDetectorError

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(obs.workspace_dir)


    # figure out list of source ids to extract from this rate file (using meta file)
    main_metafile = MetaFile.load_for_rate_file(rate_file)
    # update rate file header to point to the right metafile
    with fits.open(rate_file, mode='update') as hdul:
        hdul[0].header['OGMETFL'] = main_metafile.filename
        hdul.flush()

    root = '_'.join(os.path.basename(rate_file).split('_')[:2])
    nod = int(os.path.basename(rate_file).split('_')[2])
    dither_point_index = fits.getheader(rate_file)['PATT_NUM']

    source_ids_processed = []
    try:
        source_ids_to_process = main_metafile.unique_source_ids
        if source_ids != 'all':
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]


        # if overwrite:
        #     # Remove any existing per-source metafiles
        #     metafiles = glob.glob(main_metafile.filename.replace('.fits','_*.fits'))
        #     for metafile in metafiles:
        #         if int(metafile.split('_')[-1].split('.')[0]) in source_ids_to_process:
        #             try:
        #                 os.remove(metafile)
        #             except:
        #                 pass


        if len(source_ids_to_process)==0:
            log('No source IDs to process, exiting...')
            return []

        for source_id in source_ids_to_process:
            prod_name = os.path.basename(rate_file).replace('_rate.fits', f'_{source_id}')

            nodata_marker = f'{prod_name}_nodata'
            if (os.path.exists(f'{prod_name}_cal.fits') or os.path.exists(nodata_marker)) and not overwrite:
                log(f'Skipping stage2a for {prod_name}, overwrite=False')
                source_ids_processed.append(source_id)
                continue

            # Remove stale marker before re-processing
            if os.path.exists(nodata_marker):
                os.remove(nodata_marker)

            # copy the metafile to a source-specific metafile
            # modify that source-specific meta file to only have 1 source in it
            source_metafile = main_metafile.filter_by_source_id(source_id, filename=os.path.basename(rate_file).replace('_rate.fits',f'_msa_{source_id}.fits'), set_stellarity=set_stellarity)
            # source_metafile.shutter_table.pprint()

            # remove shutters that are marked as stuck closed
            # adapted from Anthony & Pablo's routines
            cards = []
            stuck = obs.stuck_closed_shutters[(obs.stuck_closed_shutters['root']==root)&(obs.stuck_closed_shutters['source_id']==source_id)]
            cards.append(('STKSHFIL', os.path.basename(obs.stuck_closed_shutters_file), 'Stuck shutter file name'))
            cards.append(('STKSHTIM', obs.stuck_closed_shutters_mtime, 'Stuck shutter file mtime'))
            if len(stuck) > 0:
                cards.append(('STKSHTRS', str(stuck['shutters'][0]), 'Stuck shutters masked'))
                for stuck_shutter in np.sort(stuck['shutters'][0])[::-1]:
                    print(stuck_shutter)
                    stuck_shutter_column = np.sort(np.unique(source_metafile.shutter_table['shutter_column']))[stuck_shutter-1] # get number of stuck shutter
                    source_metafile.shutter_table = source_metafile.shutter_table[source_metafile.shutter_table['shutter_column'] != stuck_shutter_column] # remove shutter from metafile
            else:
                cards.append(('STKSHTRS', 'N/A', 'Stuck shutters masked'))

            # Skip extraction if all shutters are stuck closed
            if len(source_metafile.shutter_table) == 0:
                log(f'All shutters marked as stuck closed for {prod_name}, skipping extraction')
                continue  # Skip to next source_id in the loop

            source_metafile.write(obs.workspace_dir, overwrite=True)

            # update rate file header to point to the right metafile
            with fits.open(rate_file, mode='update') as hdul:
                hdul[0].header['MSAMETFL'] = source_metafile.filename
                hdul.flush()

            association = [(os.path.basename(rate_file), 'science')]
            asn = asn_from_list.asn_from_list(association, with_exptype=True, product_name=prod_name)
            suggested_name, serialization = asn.dump()
            asn_file = f'{prod_name}.json'
            with open(asn_file,'w') as asn_out:
                asn_out.write(serialization)


            steps = {
                'extract_1d': {
                    'skip': True,
                },
                'barshadow': {
                    'skip': True,
                },
                'bkg_subtract': {
                    'skip': True,
                },
                'resample_spec':{
                    'skip': True,
                },
                # for extended wavelength range testing
                # 'pathloss':{
                #     'skip':True,
                # },
                # 'flat_field':{
                #     'skip': True,
                #     #'override_fflat':'modified_jwst_nirspec_fflat_0163.fits'
                # },
                # 'photom':{
                #     'skip': True,
                # },
                # 'wavecorr':{
                #     'skip':True,
                # },
                # 'assign_wcs':{
                #     'skip':False,
                #     'override_wavelengthrange': '/Users/hba423/simmons/crds/references/jwst/nirspec/jwst_nirspec_wavelengthrange_0008_ext5p5.asdf',
                # },
            }

            log(f"Running Spec2Pipeline for {prod_name}")
            try:
                result = Spec2Pipeline.call(
                    asn_file,
                    save_results=True,
                    steps=steps)
                log(f'Completed Spec2Pipeline for {prod_name}')
                source_ids_processed.append(source_id)

            except NoDataOnDetectorError:
                log(f'No data on detector for {prod_name}')
                with open(nodata_marker, 'w') as f:
                    f.write('NoDataOnDetectorError\n')

            for ext in ['cal','s2d']:
                if os.path.exists(f'{prod_name}_{ext}.fits'):
                    with fits.open(f'{prod_name}_{ext}.fits', mode='update') as hdul:

                        for card in cards:
                            hdul['PRIMARY'].header[card[0]] = card[1:3]

                        hdul.flush()

            if os.path.exists(asn_file):
                os.remove(asn_file)

            if os.path.exists(source_metafile.filename):
                os.remove(source_metafile.filename)

    except KeyboardInterrupt:
        return source_ids_processed

    except Exception as e:
        log(f"ERROR in source ID {source_id}", e)
        raise
        return source_ids_processed

    finally:

        # Restore original metafile info in header
        with fits.open(rate_file, mode='update') as hdul:
            if 'OGMETFL' in hdul[0].header:
                hdul[0].header['MSAMETFL'] = hdul[0].header['OGMETFL']
            else:
                hdul[0].header['MSAMETFL'] = main_metafile.filename

        # Cleanup any remaining ASN files
        asn_files = glob.glob(rate_file.replace('_rate.fits','*.json'))
        for asn_file in asn_files:
            os.remove(asn_file)

        os.chdir(prev_cwd)

    return source_ids_processed


def fix_units(file):
    """
    Fixes unit issues arising from the JWST pipeline encoding units improperly when the source isn't present in the slitlet
    """

    with fits.open(file['path'], mode='update') as hdul:

        if 'PTHLOSS' in hdul['SCI'].header:
            pthloss = hdul['SCI'].header['PTHLOSS']
        else:
            pthloss = 'POINT'

        if pthloss == 'UNIFORM' and hdul['SCI'].header['PHOTMJSR'] != 1.:
            log(f"Correcting units for {file['name']}")

            photmjsr = hdul['SCI'].header['PHOTMJSR']
            scale_factor = 1 / photmjsr
            hdul['SCI'].data *= scale_factor # SCI
            hdul['ERR'].data *= scale_factor # ERR
            hdul['VAR_POISSON'].data *= scale_factor**2 # VAR
            hdul['VAR_RNOISE'].data *= scale_factor**2 # VAR
            hdul['VAR_FLAT'].data *= scale_factor**2 # VAR
            hdul['SCI'].header['PHOTMJSR'] = 1.0
            hdul['PRIMARY'].header['SRCFLUX'] = ('F', 'Source flux present in exposure? T/F')
            hdul['PRIMARY'].header['BUNIT'] = 'MJy'
            hdul['SCI'].header['BUNIT'] = 'MJy'

        else:
            hdul['PRIMARY'].header['SRCFLUX'] = ('T', 'Source flux present in exposure? T/F')
            hdul['PRIMARY'].header['BUNIT'] = 'MJy'
            hdul['SCI'].header['BUNIT'] = 'MJy'

        hdul.flush()


def resample_single_exposure(file: Table):

    cal_file = file['path']
    workspace_dir = os.path.dirname(cal_file)

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)


    try:
        # from jwst.assign_wcs import AssignWcsStep
        from jwst.datamodels import MultiSlitModel, ImageModel
        from jwst.pixel_replace import PixelReplaceStep
        from jwst.resample import ResampleSpecStep
        pixel_replace = PixelReplaceStep()
        resample_spec = ResampleSpecStep() # do these need args?

        model = MultiSlitModel(cal_file)

        resampled = model.copy()
        if resampled.meta.cal_step.pathloss == 'COMPLETE':
            from jwst.pathloss import PathLossStep
            pathloss = PathLossStep()
            resampled = pathloss.call(resampled, inverse=True)
        resampled = pixel_replace.call(resampled)
        resampled = resample_spec.call(resampled)
        s2d_file_out = cal_file.replace('_cal.fits', '_s2d.fits')
        resampled.save(s2d_file_out)
        resampled.close()
        model.close()

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)


def pad_to_common_detector_region(models):
    """
    Returns padded models and padding info needed to un-pad later
    """
    # Find the common bounding box in detector coordinates
    min_y = min(model.slits[0].ystart for model in models)
    min_x = min(model.slits[0].xstart for model in models)

    max_y = max(model.slits[0].ystart + model.slits[0].ysize for model in models)
    max_x = max(model.slits[0].xstart + model.slits[0].xsize for model in models)

    common_shape = (max_y - min_y, max_x - min_x)

    # Pad each cutout to the common region
    new_models = []
    padding_info = []

    for model in models:
        data = model.slits[0].data
        err = model.slits[0].err
        dq = model.slits[0].dq
        start_y, start_x = model.slits[0].ystart, model.slits[0].xstart

        # Calculate padding needed on each side
        pad_top = start_y - min_y
        pad_bottom = max_y - (start_y + data.shape[0])
        pad_left = start_x - min_x
        pad_right = max_x - (start_x + data.shape[1])

        pad_width = ((pad_top, pad_bottom), (pad_left, pad_right))

        padded_data = np.pad(data, pad_width,
                            mode='constant', constant_values=0)
        padded_err = np.pad(err, pad_width,
                           mode='constant', constant_values=0)
        padded_dq = np.pad(dq, pad_width,
                          mode='constant', constant_values=0)

        new_model = model.copy()
        new_model.slits[0].data = padded_data
        new_model.slits[0].err = padded_err
        new_model.slits[0].dq = padded_dq
        new_models.append(new_model)

        # Store padding info for later un-padding
        padding_info.append({
            'pad_top': pad_top,
            'pad_bottom': pad_bottom,
            'pad_left': pad_left,
            'pad_right': pad_right
        })

    return new_models, padding_info


def unpad_model(model, padding_info):
    """
    Remove padding from a model using stored padding info
    """
    data = model.slits[0].data
    err = model.slits[0].err
    dq = model.slits[0].dq
    pad = padding_info

    # Slice out the original region
    unpadded_data = data[
        pad['pad_top'] : data.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : data.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_err = err[
        pad['pad_top'] : err.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : err.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_dq = dq[
        pad['pad_top'] : dq.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : dq.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]

    new_model = model.copy()
    new_model.slits[0].data = unpadded_data
    new_model.slits[0].err = unpadded_err
    new_model.slits[0].dq = unpadded_dq
    return new_model

def run_stage2b_single_slitlet(
        cal_files: List,
        workspace_dir: str,
        bkg_overrides,
        rectify: bool = False, # whether to produce rectified (i.e., s2d) products as well
    ):
    # this function shouldn't be doing any file discovery!

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        # from jwst.assign_wcs import AssignWcsStep
        from jwst.pathloss import PathLossStep
        from jwst.background import BackgroundStep
        from jwst.datamodels import MultiSlitModel, ImageModel
        # assign_wcs = AssignWcsStep()
        bkg_subtract = BackgroundStep()
        pathloss = PathLossStep()

        if rectify:
            from jwst.pixel_replace import PixelReplaceStep
            from jwst.resample import ResampleSpecStep
            pixel_replace = PixelReplaceStep()
            resample_spec = ResampleSpecStep() # do these need args?

        if len(cal_files)==1:
            raise RuntimeError("Single exposure only, no background subtraction to be done!")

        elif len(cal_files) in [2, 3, 5]:
            # Load cal files as MultiSlitModels, but undo any pathloss corrections
            models = []
            do_pathloss = []
            for cal_file in cal_files:
                model = MultiSlitModel(cal_file)
                if 'COMPLETE' in model.meta.cal_step.pathloss.upper():
                    log(f'Inverting the pathloss for {os.path.basename(cal_file)}')
                    inverted = pathloss.call(model, inverse=True)
                    do_pathloss.append(True)
                else:
                    inverted = model
                    do_pathloss.append(False)

                models.append(inverted)

            shapes = [model[0].data.shape for model in models]
            # print(shapes)
            padded = False
            if not len(list(set(shapes))) == 1:
                models, padding_info = pad_to_common_detector_region(models)
                padded = True
                # raise ValueError('Inconsistent shapes!')

            # shapes = [model[0].data.shape for model in models]
            # print(shapes)
            # assert 1==2

            for i in range(len(cal_files)):
                science = models[i][0]


                bkg = [mod[0] for j,mod in enumerate(models) if j != i]

                if bkg_overrides is not None:
                    nods = [int(os.path.basename(cal_file).split('_')[2]) for cal_file in cal_files]
                    if str(nods[i]) in bkg_overrides:
                        nods_to_use = bkg_overrides[str(nods[i])]
                        print(nods_to_use)
                        log(f'{os.path.basename(cal_files[i])}: Only using nods {nods_to_use} for bkg subtraction for nod {nods[i]}')
                        bkg = [b[0] for n,b in zip(nods,models) if n in nods_to_use]

                bkgsub = bkg_subtract.call(science, bkg)


                result = models[i].copy()
                # # janky, but this is the best way I could find to ensure that all metadata gets retained through file I/O
                result.slits[0].data = bkgsub.data
                result.slits[0].err = bkgsub.err
                result.slits[0].dq = bkgsub.dq
                result.slits[0].var_rnoise = bkgsub.var_rnoise
                result.slits[0].var_poisson = bkgsub.var_poisson
                result.meta.cal_step.bkg_subtract = 'COMPLETE'
                result[0].meta.cal_step.bkg_subtract = 'COMPLETE'

                im = ImageModel(cal_files[i])
                result[0].meta.pointing = im.meta.pointing

                if padded:
                    result = unpad_model(result, padding_info[i])

                # apply the pathloss correction again
                if do_pathloss[i]:
                    result = pathloss.call(result)



                cal_file_out = cal_files[i].replace('_cal.fits', '_cal_bkgsub.fits')
                result.save(cal_file_out)

                if rectify:
                    # Call pixel replace, followed by resample_spec for 2D slit data
                    resampled = result.copy()
                    resampled = pixel_replace.call(resampled)
                    resampled = resample_spec.call(resampled)
                    s2d_file_out = cal_file_out.replace('_cal_bkgsub.fits', '_s2d_bkgsub.fits')
                    resampled.save(s2d_file_out)
                    resampled.close()

                result.close()

        else:
            raise Exception("not sure how to handle this # of exposures!")

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)
