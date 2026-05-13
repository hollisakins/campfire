"""
edge: flag noisy detector edges in the canonical exposure DQ array.

Per-exposure step. Walks each edge of the SCI array — left/right columns and
top/bottom rows — and flags as ``DO_NOT_USE`` any column or row whose mean
exceeds the standard deviation of the per-column / per-row mean array. Stops
at the first row/column that falls below threshold (so only the noisy outer
edge gets flagged, not the bulk of the detector).

In-place mutation of ``model.dq`` only — SCI and ERR are left untouched.
Stamps ``CFP_EDGE`` with an ISO timestamp.
"""

import os
from datetime import datetime

import numpy as np

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def edge_step(exposure_file, field, step_config, overwrite=False, status=None):
    """Flag noisy outer edges of a single canonical exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_EDGE', rootname,
                       'edge', status, overwrite):
        return

    log(f"Running edge flagging on {rootname}")

    from jwst.datamodels import ImageModel
    from stdatamodels import util as stutil

    with ImageModel(exposure_file) as model:
        size = model.data.shape[0]

        mean_cols = np.array([np.mean(model.data[:, ii]) for ii in range(size)])
        mean_rows = np.array([np.mean(model.data[ii, :]) for ii in range(size)])

        # Left columns
        for ii in range(size):
            if np.abs(np.mean(model.data[:, ii])) > np.std(mean_cols):
                model.dq[:, ii] = 1
            else:
                break

        # Right columns
        for ii in range(size):
            j = size - 1 - ii
            if np.abs(np.mean(model.data[:, j])) > np.std(mean_cols):
                model.dq[:, j] = 1
            else:
                break

        # Bottom rows
        for ii in range(size):
            if np.abs(np.mean(model.data[ii, :])) > np.std(mean_rows):
                model.dq[ii, :] = 1
            else:
                break

        # Top rows
        for ii in range(size):
            j = size - 1 - ii
            if np.abs(np.mean(model.data[j, :])) > np.std(mean_rows):
                model.dq[j, :] = 1
            else:
                break

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        model.history.append(stutil.create_history_entry(
            f'Removed edges; {now}'
        ))

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_EDGE=None),
        )

    log(f"Edges flagged: {rootname}")
