"""
NIRCam reduction engine — thin backwards-compatibility wrapper.

All orchestration logic lives in the stage modules (stage1, stage2, stage3)
and config helpers (config.py). This class delegates to those functions so
that existing scripts and interactive usage continue to work.
"""

import os

from campfire_pipeline.config import (
    load_config, setup_environment, get_nircam_stage_config,
)
from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.field import Field


class ReductionEngine:
    """Backwards-compatible NIRCam orchestration wrapper.

    Prefer calling stage functions directly for new code.
    """

    def __init__(self, config_path=None):
        self.config = load_config(config_path)
        self.config_path = config_path
        setup_environment(self.config)
        self.campfire_root = os.environ.get('CAMPFIRE_ROOT')
        log("Initialized NIRCam ReductionEngine")

    def get_stage_config(self, stage_name, field):
        return get_nircam_stage_config(stage_name, self.config, field)

    def run_stage1(self, field, filters=None, n_processes=1, overwrite=False):
        from campfire_pipeline.nircam.stage1 import run_stage1
        stage_config = self.get_stage_config('stage1', field)
        run_stage1(field, stage_config, filters=filters,
                   n_processes=n_processes, overwrite=overwrite)

    def run_stage2(self, field, filters=None, n_processes=1, overwrite=False):
        from campfire_pipeline.nircam.stage2 import run_stage2
        stage_config = self.get_stage_config('stage2', field)
        run_stage2(field, stage_config, filters=filters,
                   n_processes=n_processes, overwrite=overwrite)

    def run_stage3(self, field, filters=None, n_processes=1, overwrite=False):
        from campfire_pipeline.nircam.stage3 import run_stage3
        stage_config = self.get_stage_config('stage3', field)
        run_stage3(field, stage_config, filters=filters,
                   n_processes=n_processes, overwrite=overwrite)
