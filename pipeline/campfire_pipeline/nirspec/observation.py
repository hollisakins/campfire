"""
Observation dataclass: NIRSpec observation configuration and workspace management.
"""

import os
import glob
import shutil
import toml
import numpy as np
from dataclasses import dataclass, field
from typing import List
from textwrap import dedent
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log


@dataclass
class Observation:
    name: str
    field: str
    program: str
    program_id: int
    data_subdir: str
    files: List[str]
    gratings: List[str] = field(default_factory=list)
    stage_overrides: dict = field(default_factory=dict)
    config_groups: dict = field(default_factory=dict)
    manual_masks: dict = field(default_factory=dict)

    directories_setup: bool = False

    @classmethod
    def load(cls, name, observations_file=None):
        from campfire_pipeline.config import resolve_observations_file

        observations_file = resolve_observations_file(observations_file)
        with open(observations_file, 'r') as f:
            observations_config = toml.load(f)

        if name not in observations_config:
            raise ValueError(f"Observation '{name}' not found in {observations_file}")

        obs = observations_config[name]

        field_name = obs['field']
        program_slug = obs['program']
        data_subdir = obs['data_subdir']
        gratings = obs.get('gratings', [])

        files = obs['files']
        if isinstance(files, str):
            files = [files]
        elif isinstance(files, list):
            pass
        else:
            raise TypeError

        if 'program_id' in obs:
            program_id = int(obs['program_id'])
        else:
            # Extract program ID from JWST filename pattern: jw<ppppp>...
            assert files[0].startswith('jw')
            program_id = int(files[0][2:7])

        # Capture per-observation stage config overrides
        stage_overrides = {}
        for key in ['stage1', 'stage2', 'stage3']:
            if key in obs and isinstance(obs[key], dict):
                stage_overrides[key] = obs[key]

        # Parse config_groups: list-of-lists → flat dict mapping each config to group label
        # e.g. [['04101','06101'], ['07101','09101']]
        # becomes {'04101': '04101', '06101': '04101', '07101': '07101', '09101': '07101'}
        config_groups = {}
        for group in obs.get('config_groups', []):
            label = group[0]
            for cfg in group:
                config_groups[cfg] = label

        # Manual masks: { rate_basename (no _rate.fits suffix): DS9 region string }
        manual_masks = {}
        masks_section = obs.get('masks', {})
        if isinstance(masks_section, dict):
            for basename, reg_string in masks_section.items():
                if not isinstance(reg_string, str):
                    raise TypeError(
                        f"masks['{basename}'] must be a string in [{name}.masks]; "
                        f"got {type(reg_string).__name__}"
                    )
                manual_masks[basename] = reg_string

        return cls(
            name=name,
            field=field_name,
            program=program_slug,
            program_id=program_id,
            data_subdir=data_subdir,
            files=files,
            gratings=gratings,
            stage_overrides=stage_overrides,
            config_groups=config_groups,
            manual_masks=manual_masks,
        )

    def setup_workspace_directory(self, data_dir, product_dir, overwrite=False):
        """
        Create working directories for an observation.

        Parameters:
        -----------
        overwrite : bool
            Whether to overwrite existing workspace

        Returns:
        --------
        str : Path to the workspace directory
        """
        self.workspace_dir = os.path.join(product_dir, self.name)

        # Create workspace directory
        if os.path.exists(self.workspace_dir) and overwrite:
            log(f"Removing existing workspace: {self.workspace_dir}")
            shutil.rmtree(self.workspace_dir)

        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir, exist_ok=True)
            log(f"Created workspace directory: {self.workspace_dir}")

        self.raw_dir = os.path.join(data_dir, self.data_subdir)
        self.rate_files = self.glob('_rate.fits')

        self.directories_setup = True

    def discover_raw_files(self):
        """Discover raw uncal files and associated MSA metadata.

        Must be called after setup_workspace_directory. Populates
        self.uncal_files, self.msa_meta_files, and self.rate_files.

        Raises RuntimeError if no raw files are found.
        """
        self.uncal_files = self.glob('_uncal.fits', check_exp_type=True, directory=self.raw_dir)

        if len(self.uncal_files) == 0:
            raise RuntimeError(f"No raw data files found for observation {self.name}!")

        msa_meta_files_needed = set()
        for src_file in self.uncal_files:
            # Read MSAMETFL header from uncal file to find associated MSA meta file
            try:
                with fits.open(src_file) as hdul:
                    msametfl = hdul[0].header.get('MSAMETFL', '')
                    if msametfl:
                        # MSAMETFL contains the basename of the MSA meta file
                        msa_meta_files_needed.add(os.path.join(self.raw_dir, msametfl))
                    else:
                        log(f"No MSAMETFL header found in {os.path.basename(src_file)}")
            except Exception as e:
                log(f"Could not read MSAMETFL header from {os.path.basename(src_file)}: {e}")
        self.msa_meta_files = msa_meta_files_needed

        self.rate_files = []
        for uncal_file in self.uncal_files:
            rate_file = uncal_file.replace('_uncal.fits', '_rate.fits').replace(self.raw_dir, self.workspace_dir)
            self.rate_files.append(rate_file)

    def symlink_uncal_files(self, overwrite=False):

        log(self.rate_files)
        if all([os.path.exists(f) for f in self.rate_files]) and not overwrite:
            log('All rate files already exist, and overwrite=False! No new rate files will be generated.')
            return False

        # Symlink uncal files into workspace and track MSA meta files needed
        for src_file in self.uncal_files:
            dst_file = os.path.join(self.workspace_dir, os.path.basename(src_file))
            rate_file = dst_file.replace('_uncal.fits', '_rate.fits')
            if not os.path.exists(dst_file) and (not os.path.exists(rate_file) or overwrite):
                log(f"Linking {os.path.basename(src_file)} to workspace")
                os.symlink(src_file, dst_file)

        for msa_meta_file in self.msa_meta_files:
            dst_msa_meta_file = os.path.join(self.workspace_dir, os.path.basename(msa_meta_file))

            if os.path.exists(msa_meta_file):
                if not os.path.exists(dst_msa_meta_file):
                    log(f"Copying MSA meta file {os.path.basename(msa_meta_file)} to workspace")
                    shutil.copy2(msa_meta_file, dst_msa_meta_file)
                else:
                    log(f"MSA meta file {os.path.basename(msa_meta_file)} already exists in workspace")
            else:
                log(f"MSA meta file not found: {msa_meta_file}")

        return True

    @property
    def stuck_closed_shutters_file(self):
        return os.path.join(self.workspace_dir, f'_{self.name}_stuck_closed_shutters.toml')

    @property
    def bkg_override_file(self):
        return os.path.join(self.workspace_dir, f'_{self.name}_nodded_background_overrides.toml')

    @property
    def stuck_closed_shutters(self):

        file = os.path.join(self.stuck_closed_shutters_file)
        if not os.path.exists(file):
            log(f'No stuck closed shutter file found, creating blank {file}')
            with open(file, 'w') as f:
                docstring = dedent(f"""
                # vetted stuck closed shutter list for {self.name}
                # format is a table for each "root" file name, which
                # consists of obs/visit/config, e.g. "jw06368001001_03101"
                # the list of stuck closed shutters for a source ID should
                # be given as a key-value pair in the table; for example:
                # [jw06368001001_03101]
                #     12345 = [1,2,3]
                """)
                f.write(docstring)

        data = toml.load(file)

        # Build lists for each column
        roots = []
        source_ids = []
        shutters = []

        for root in data:
            for entry in data[root]:
                roots.append(root)
                source_ids.append(int(entry))
                shutters.append(list(data[root][entry]))

        # Create table from columns
        tab = Table({
            'root': roots,
            'source_id': source_ids,
            'shutters': shutters
        })

        return tab

    @property
    def bkg_overrides(self):

        file = os.path.join(self.bkg_override_file)
        if not os.path.exists(file):
            log(f'No nodded background override found, creating blank {file}')
            with open(file, 'w') as f:
                docstring = dedent(f"""
                # nodded background overrides for {self.name}
                # format is like the stuck closed shutter list, e.g., there's
                # a table for each "root" file name, which
                # consists of obs/visit/config, e.g. "jw06368001001_03101"
                # for each source ID, the background shutters to use for
                # each nod should be given as a key-value pair in the table;
                # for example:""" + """
                # [jw06368001001_03101]
                #     12345 = {3=[1]}
                # (for source 12345, only use nod 1 as background for nod 3)
                #
                # NOTE: nod numbers are the exposure sequence numbers from the
                # FITS filenames (the 3rd underscore-delimited segment), NOT
                # sequential indices. If a TACONFIRM exposure is 00001, the
                # first science nod will be 2, not 1.
                """)
                f.write(docstring)

        data = toml.load(file)
        result = {}
        for root in data:
            result[root] = {}
            for srcid in data[root]:
                result[root][srcid] = {}
                for nod in data[root][srcid]:
                    result[root][srcid][nod] = list(data[root][srcid][nod])

        return result

    @property
    def stuck_closed_shutters_mtime(self):
        """Return the modification time of the vetted stuck closed shutters file.

        Returns
        -------
        float
            Unix timestamp of last modification, suitable for storing in FITS headers
        """
        return os.path.getmtime(self.stuck_closed_shutters_file)

    @property
    def bkg_override_mtime(self):
        """Return the modification time of the nodded background override file.

        Returns
        -------
        float
            Unix timestamp of last modification, suitable for storing in FITS headers
        """
        return os.path.getmtime(self.bkg_override_file)

    def glob(self, ext, check_exp_type=False, directory=None):
        """
        Search for files in the Observation's workspace directory, matching the Observation file specification and a given extension.
        If directory kwarg is provided, search that directory instead of Observation.workspace_dir.
        Patterns prefixed with '~' are exclusion patterns: matching files are removed from results.
        """
        if directory is None:
            directory = self.workspace_dir

        include_patterns = [p for p in self.files if not p.startswith('~')]
        exclude_patterns = [p[1:] for p in self.files if p.startswith('~')]

        result = []
        for file_pattern in include_patterns:
            pattern_path = os.path.join(directory, file_pattern + ext)
            resulti = glob.glob(pattern_path)

            if check_exp_type:
                result += [r for r in resulti if fits.getheader(r)['EXP_TYPE'] == 'NRS_MSASPEC']
            else:
                result += resulti

        # Remove files matching exclusion patterns
        for exc_pattern in exclude_patterns:
            exc_path = os.path.join(directory, exc_pattern + ext)
            excluded = set(glob.glob(exc_path))
            result = [r for r in result if r not in excluded]

        return sorted(result)

    def discover_files(self, ext='cal', source_ids='all'):
        """Discover pipeline product files in the workspace directory.

        Parameters
        ----------
        ext : str
            File extension to search for (e.g., 'cal', 'cal_bkgsub').
        source_ids : list or 'all'
            Source IDs to filter by, or 'all' for no filtering.

        Returns
        -------
        Table
            Astropy table with columns: path, name, source_id, detector,
            obs, filter, grating, dither_pattern_type, primary_dither_points,
            subpixel_dither_points, total_dither_points, dither_position,
            nod_type, filter_grating, config, nod, root, shutter_id.
        """
        paths = self.glob(ext=f'_{ext}.fits')

        if source_ids != 'all':
            new_paths = []
            for source_id in source_ids:
                new_paths += [p for p in paths if f'_{source_id}_' in p]
            paths = new_paths

        filt, grat = [], []
        PATTTYPE, PRIDTPTS, PATT_NUM, NUMDTHPT, NOD_TYPE, SUBPXPTS = [], [], [], [], [], []
        SHUTTRID, SHUTSTA = [], []
        for f in paths:
            hdr = fits.getheader(f, ext=0)
            filt.append(hdr['FILTER'])
            grat.append(hdr['GRATING'])
            PATTTYPE.append(hdr['PATTTYPE'])
            PRIDTPTS.append(hdr['PRIDTPTS'])
            PATT_NUM.append(hdr['PATT_NUM'])
            NUMDTHPT.append(hdr['NUMDTHPT'])
            NOD_TYPE.append(hdr['NOD_TYPE'])
            SUBPXPTS.append(hdr['SUBPXPTS'])
            hdr1 = fits.getheader(f, ext=1)
            SHUTTRID.append(hdr1['SHUTTRID'])
            SHUTSTA.append(hdr1.get('SHUTSTA', ''))

        files = Table()
        files['path'] = paths
        files['name'] = [os.path.basename(f['path']) for f in files]
        files['source_id'] = [int(os.path.basename(p).replace(f'_{ext}.fits', '').split('_')[-1]) for p in paths]
        files['detector'] = ['nrs'+f['name'].split('nrs')[-1][0] for f in files]
        files['obs'] = [f['name'].split('_')[0] for f in files]
        files['filter'] = filt
        files['grating'] = grat
        files['dither_pattern_type'] = PATTTYPE
        files['primary_dither_points'] = PRIDTPTS
        files['subpixel_dither_points'] = SUBPXPTS
        files['total_dither_points'] = NUMDTHPT
        files['dither_position'] = PATT_NUM
        files['nod_type'] = NOD_TYPE
        files['filter_grating'] = [f['grating'].lower()+'_'+f['filter'].lower() for f in files]
        files['config'] = [f['name'].split('_')[1] for f in files]
        files['nod'] = [f['name'].split('_')[2] for f in files]
        files['root'] = ['_'.join(f['name'].split('_')[:2]) for f in files]
        files['shutter_id'] = SHUTTRID
        files['shutter_state'] = SHUTSTA

        # Map configs to group labels for cross-config nod pairing
        files['config_group'] = [self.config_groups.get(f['config'], f['config']) for f in files]

        return files

    @staticmethod
    def group_files(files):
        """Group files into nod patterns for background subtraction.

        Adds columns to *files*:
        - bkg_group: unique integer per background-subtraction group (per-detector)
        - exp_group: groups files from the same exposure across both detectors
        - subpx_dither: sub-pixel dither index

        Parameters
        ----------
        files : Table
            Output of :meth:`discover_files`.

        Returns
        -------
        Table
            Same table with group columns added.
        """
        files['bkg_group'] = -1
        files['exp_group'] = -1
        files['subpx_dither'] = -1
        bkg_group_id = 0
        exp_group_id = 0

        for source_id in np.unique(files['source_id']):
            mask1 = files['source_id'] == source_id

            for observation in np.unique(files['obs'][mask1]):
                mask2 = mask1 & (files['obs'] == observation)

                group_col = 'config_group' if 'config_group' in files.colnames else 'config'
                for config in np.unique(files[group_col][mask2]):
                    mask3 = mask2 & (files[group_col] == config)
                    files3 = files[mask3]

                    root = files3['root'][0]

                    subpx_dither = np.ones(len(files3))

                    if len(files3) == 0:
                        raise Exception(f"No files found for group {root}. Something went wrong!")

                    if files3['dither_pattern_type'][0] == 'NONE':
                        pass

                    elif (files3['dither_pattern_type'][0] == '2-POINT-WITH-NIRCAM-SIZE2') and ('SHUTTER-SLITLET' in files3['nod_type'][0]) and (files3['subpixel_dither_points'][0] == 2):
                        subpx_dither = np.where(np.isin(files3['dither_position'], [1,3,5]), 1, 2)

                    else:
                        raise NotImplementedError(f"File grouping for dither pattern {files3['dither_pattern_type'][0]} not implemented")

                    for subpx in np.unique(subpx_dither):
                        mask3_indices = np.where(mask3)[0]
                        subpx_indices = mask3_indices[subpx_dither == subpx]

                        files['subpx_dither'][subpx_indices] = int(subpx)
                        files['exp_group'][subpx_indices] = exp_group_id

                        for detector in np.unique(files['detector'][mask3]):
                            det_mask = files['detector'][subpx_indices] == detector
                            files['bkg_group'][subpx_indices[det_mask]] = bkg_group_id
                            bkg_group_id += 1

                        exp_group_id += 1

        # Sanity checks
        if np.any(files['bkg_group'] == -1):
            unassigned = np.sum(files['bkg_group'] == -1)
            raise RuntimeError(f"{unassigned} files were not assigned to a bkg_group!")

        if np.any(files['exp_group'] == -1):
            unassigned = np.sum(files['exp_group'] == -1)
            raise RuntimeError(f"{unassigned} files were not assigned to an exp_group!")

        if np.any(files['subpx_dither'] == -1):
            unassigned = np.sum(files['subpx_dither'] == -1)
            raise RuntimeError(f"{unassigned} files were not assigned a subpx_dither!")

        return files
