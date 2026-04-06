"""
NIRSpec reduction engine — thin backwards-compatibility wrapper.

All orchestration logic now lives in the stage modules (stage1, stage2, stage3)
and config helpers (config.py).  This class delegates to those functions so that
existing scripts (``reduction.py``, interactive usage) continue to work.
"""

from pathlib import Path

from campfire_pipeline.config import (
    load_config, setup_environment, resolve_paths, get_stage_config,
)
from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.observation import Observation


class ReductionEngine:
    """Backwards-compatible orchestration wrapper.

    Prefer calling stage functions directly for new code.
    """

    def __init__(self, config_path=None):
        self.config = load_config(config_path)
        self.config_path = config_path
        setup_environment(self.config)

        paths = resolve_paths(self.config)
        self.data_dir = paths['data_dir']
        self.products_dir = paths['products_dir']

        log("Initialized ReductionEngine")

    # -- helpers ---------------------------------------------------------------

    def get_stage_config(self, stage_name: str, obs: Observation) -> dict:
        return get_stage_config(stage_name, self.config, obs)

    # -- stage delegates -------------------------------------------------------

    def run_stage1(self, obs, n_processes=1, overwrite=False):
        from campfire_pipeline.nirspec.stage1 import run_stage1
        stage_config = self.get_stage_config('stage1', obs)
        run_stage1(
            obs, stage_config,
            n_processes=n_processes,
            overwrite=overwrite,
            data_dir=self.data_dir,
            products_dir=self.products_dir,
        )

    def run_stage2a(self, obs, source_ids='all', overwrite=False, n_processes=1, plot=True):
        from campfire_pipeline.nirspec.stage2 import run_stage2a
        stage_config = self.get_stage_config('stage2', obs)
        run_stage2a(
            obs, stage_config,
            source_ids=source_ids,
            overwrite=overwrite,
            n_processes=n_processes,
            plot=plot,
            data_dir=self.data_dir,
            products_dir=self.products_dir,
        )

    def run_stage2b(self, obs, source_ids='all', overwrite=False, n_processes=1):
        from campfire_pipeline.nirspec.stage2 import run_stage2b
        stage_config = self.get_stage_config('stage2', obs)
        run_stage2b(
            obs, stage_config,
            source_ids=source_ids,
            overwrite=overwrite,
            n_processes=n_processes,
            data_dir=self.data_dir,
            products_dir=self.products_dir,
        )

    def run_stage3(self, obs, source_ids='all', n_processes=1, overwrite=False):
        from campfire_pipeline.nirspec.stage3 import run_stage3
        stage_config = self.get_stage_config('stage3', obs)
        run_stage3(
            obs, stage_config, self.config,
            source_ids=source_ids,
            n_processes=n_processes,
            overwrite=overwrite,
            data_dir=self.data_dir,
            products_dir=self.products_dir,
        )

    def run_redshift_fitting(self, obs, source_ids=None, n_processes=1, overwrite=False):
        from campfire_pipeline.nirspec.redshift_fitting import fit_redshifts
        return fit_redshifts(
            obs_name=obs.name,
            config=self.config,
            source_ids=source_ids,
            overwrite=overwrite,
            workspace_dir=obs.workspace_dir,
            n_processes=n_processes,
        )

    def run_summarize(self, obs):
        from campfire_pipeline.metadata.summary import (
            generate_observation_summary,
            write_effective_config,
            write_summary_ecsv,
        )
        version = self.config.get('pipeline', {}).get('version', 'unknown')
        consensus_config = self.config.get('nirspec', {}).get('redshift_consensus', {})
        obs_dir = Path(obs.workspace_dir)
        summary = generate_observation_summary(obs.name, obs_dir,
                                                reduction_version=version,
                                                consensus_config=consensus_config)
        if len(summary) > 0:
            write_summary_ecsv(summary, obs_dir, obs.name)
        else:
            log(f"No spectra found for {obs.name}, skipping summary")

        # Write effective config for provenance tracking
        write_effective_config(self.config, obs_dir, obs.name,
                               obs_stage_overrides=obs.stage_overrides)

    # -- legacy methods kept for backwards compat ------------------------------

    def discover_files(self, obs, ext='cal', source_ids='all'):
        """Delegate to Observation.discover_files()."""
        if not obs.directories_setup:
            obs.setup_workspace_directory(self.data_dir, self.products_dir, overwrite=False)
        return obs.discover_files(ext=ext, source_ids=source_ids)

    def group_files(self, files):
        """Delegate to Observation.group_files()."""
        return Observation.group_files(files)
